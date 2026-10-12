"""
Дизайн-дайджест: персональный Telegram-бот.

/start     — подписаться (включаются источники по умолчанию) и открыть настройки
/settings  — источники, время рассылки, картинки вкл/выкл, сколько материалов за раз
/digest    — прислать свежее прямо сейчас
/stop      — отписаться

Каждый получает подборку в своё время (по умолчанию 10:00 по Москве).
Если нового нет — «главное за неделю».

Запуск: python -m server.main  (токен в переменной TELEGRAM_BOT_TOKEN)
"""

import asyncio
import datetime as dt
import html
import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from zoneinfo import ZoneInfo

import requests
from telegram import BotCommandScopeChat
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions, Update
from telegram.constants import ParseMode
from telegram.error import Forbidden, TelegramError
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ContextTypes,
                          MessageHandler, filters)

from . import sources, storage

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("design-bot")

MSK = ZoneInfo("Europe/Moscow")
DEFAULT_HOUR = 10
HOURS = list(range(6, 24))
CATCH_UP_HOURS = 3   # если бот был выключен в нужный час — досылаем в течение 3 часов
TIMEZONES = [
    ("Europe/Kaliningrad", "Калининград"), ("Europe/Moscow", "Москва"),
    ("Europe/Samara", "Самара"), ("Asia/Yekaterinburg", "Екатеринбург"),
    ("Asia/Aqtau", "Актау · Астана"), ("Asia/Omsk", "Омск"),
    ("Asia/Novosibirsk", "Новосибирск"), ("Asia/Irkutsk", "Иркутск"),
    ("Asia/Vladivostok", "Владивосток"), ("Asia/Tbilisi", "Тбилиси · Ереван"),
    ("Europe/Berlin", "Европа (Берлин)"), ("Asia/Bangkok", "Бангкок"),
]
TZ_NAMES = dict(TIMEZONES)
BATCH_OPTIONS = [3, 5, 10, 15]
DEFAULT_BATCH = 3
RECAP_LIMIT = 7
NEW_WINDOW = 48 * 3600     # «новое» — не старше двух суток
WEEK = 7 * 24 * 3600

KINDS = {
    "youtube": ("📺 YouTube-каналы", "➕ Свой YouTube-канал",
                "Пришли ссылку на YouTube-канал (например, youtube.com/@dear-designers)."),
    "rss": ("📰 Сайты и статьи", "➕ Свой сайт",
            "Пришли адрес сайта или его RSS-ленты. Англоязычные статьи бот будет переводить."),
    "telegram": ("💬 Telegram-каналы", "➕ Свой Telegram-канал",
                 "Пришли ссылку на публичный канал: t.me/имя или @имя."),
}

WELCOME = (
    "🎨 <b>Дизайн-дайджест</b>\n\n"
    "Каждый день присылаю подборку из выбранных тобой источников: "
    "новости дизайна, статьи (английские — с переводом), новые видео с хайлайтами "
    "и посты из Telegram-каналов.\n\n"
    "Отметь, что тебе интересно, и выбери время 👇"
)
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
          "августа", "сентября", "октября", "ноября", "декабря"]
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


# ---------- меню ----------

def when_label(chat_id: int) -> str:
    u = storage.get_user(chat_id)
    hour = u["hour"] if u and u["hour"] is not None else DEFAULT_HOUR
    tz = u["tz"] if u and u["tz"] else "Europe/Moscow"
    return f"{hour}:00 ({TZ_NAMES.get(tz, tz)})"


def time_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    u = storage.get_user(chat_id)
    cur = u["hour"] if u and u["hour"] is not None else DEFAULT_HOUR
    btns = [InlineKeyboardButton(("✅ " if h == cur else "") + f"{h}:00", callback_data=f"h:{h}")
            for h in HOURS]
    rows = [btns[i:i + 4] for i in range(0, len(btns), 4)]
    tz = (u["tz"] if u else None) or "Europe/Moscow"
    rows.append([InlineKeyboardButton(f"🌍 Часовой пояс: {TZ_NAMES.get(tz, tz)}",
                                      callback_data="tz")])
    rows.append([InlineKeyboardButton("← К источникам", callback_data="back")])
    return InlineKeyboardMarkup(rows)


def tz_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    u = storage.get_user(chat_id)
    cur = (u["tz"] if u else None) or "Europe/Moscow"
    btns = [InlineKeyboardButton(("✅ " if z == cur else "") + name, callback_data=f"z:{z}")
            for z, name in TIMEZONES]
    rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]
    rows.append([InlineKeyboardButton("← Ко времени", callback_data="time")])
    return InlineKeyboardMarkup(rows)


def display(chat_id: int) -> tuple[bool, int]:
    """(показывать ли картинки, сколько материалов за раз)."""
    u = storage.get_user(chat_id)
    images = bool(u["images"]) if u and u["images"] is not None else True
    batch = u["per_batch"] if u and u["per_batch"] else DEFAULT_BATCH
    return images, batch


def batch_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    cur = display(chat_id)[1]
    btns = [InlineKeyboardButton(("✅ " if n == cur else "") + str(n), callback_data=f"n:{n}")
            for n in BATCH_OPTIONS]
    return InlineKeyboardMarkup([btns, [InlineKeyboardButton("← К источникам", callback_data="back")]])


def settings_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    images, batch = display(chat_id)
    rows = [[InlineKeyboardButton(f"🕙 Присылать в {when_label(chat_id)}", callback_data="time")],
            [InlineKeyboardButton("🖼 Картинки: " + ("вкл" if images else "выкл"), callback_data="img"),
             InlineKeyboardButton(f"📦 По {batch} за раз", callback_data="batch")]]
    srcs = storage.menu_sources(chat_id)
    for kind, (title, add_label, _) in KINDS.items():
        rows.append([InlineKeyboardButton(title, callback_data="noop")])
        btns = [InlineKeyboardButton(("✅ " if s["on_"] else "▫️ ") + s["name"],
                                     callback_data=f"t:{s['id']}")
                for s in srcs if s["kind"] == kind]
        rows += [btns[i:i + 2] for i in range(0, len(btns), 2)]
        rows.append([InlineKeyboardButton(add_label, callback_data=f"add:{kind}")])
    rows.append([InlineKeyboardButton("Готово 👌", callback_data="done")])
    return InlineKeyboardMarkup(rows)


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    new = storage.add_user(chat.id, update.effective_user.full_name if update.effective_user else "")
    await chat.send_message(WELCOME, parse_mode=ParseMode.HTML,
                            reply_markup=settings_keyboard(chat.id))
    storage.log_event(chat.id, "start")
    if new:
        log.info("новый подписчик %s", chat.id)


async def cmd_settings(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    storage.add_user(update.effective_chat.id, update.effective_user.full_name)
    storage.log_event(update.effective_chat.id, "settings")
    await update.effective_chat.send_message("Твои источники 👇",
                                             reply_markup=settings_keyboard(update.effective_chat.id))


async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    storage.set_active(update.effective_chat.id, False)
    storage.log_event(update.effective_chat.id, "stop")
    await update.effective_chat.send_message(
        "Ок, больше не присылаю. Захочешь вернуться — /start, настройки сохранятся.")


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    ctx.user_data.pop("awaiting", None)
    await update.effective_chat.send_message("Отменил.")


async def cmd_digest(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    storage.add_user(chat_id, update.effective_user.full_name)
    storage.log_event(chat_id, "digest_cmd")
    await update.effective_chat.send_message("Собираю свежее из твоих источников… ⏳")
    srcs = storage.sources_by_ids(storage.user_source_ids(chat_id))
    await asyncio.to_thread(refresh, srcs)
    sent = await send_digest(ctx.bot, chat_id, on_demand=True)
    if not sent:
        await update.effective_chat.send_message("Пока ничего нового. Загляни позже 🙂")


async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    chat_id = q.message.chat.id
    data = q.data or ""
    if data == "noop":
        await q.answer()
    elif data.startswith("t:"):
        storage.log_event(chat_id, "toggle")
        on = storage.toggle(chat_id, data[2:])
        await q.answer("Включил" if on else "Выключил")
        await q.edit_message_reply_markup(settings_keyboard(chat_id))
    elif data.startswith("add:"):
        kind = data[4:]
        ctx.user_data["awaiting"] = kind
        await q.answer()
        await q.message.reply_text(KINDS[kind][2] + "\n\nПередумал — /cancel")
    elif data == "time":
        await q.answer()
        await q.edit_message_text("Во сколько присылать подборку? Время — по твоему часовому поясу.",
                                  reply_markup=time_keyboard(chat_id))
    elif data.startswith("h:"):
        storage.set_schedule(chat_id, hour=int(data[2:]))
        await q.answer(f"Буду присылать в {data[2:]}:00")
        await q.edit_message_reply_markup(time_keyboard(chat_id))
    elif data == "tz":
        await q.answer()
        await q.edit_message_text("Выбери часовой пояс:", reply_markup=tz_keyboard(chat_id))
    elif data.startswith("z:"):
        storage.set_schedule(chat_id, tz=data[2:])
        await q.answer(f"Часовой пояс: {TZ_NAMES.get(data[2:], data[2:])}")
        await q.edit_message_text("Во сколько присылать подборку? Время — по твоему часовому поясу.",
                                  reply_markup=time_keyboard(chat_id))
    elif data == "img":
        images = not display(chat_id)[0]
        storage.set_display(chat_id, images=images)
        await q.answer("Картинки включены" if images else "Без картинок")
        await q.edit_message_reply_markup(settings_keyboard(chat_id))
    elif data == "batch":
        await q.answer()
        await q.edit_message_text("Сколько материалов на одной странице подборки? Остальное — листалкой ◀ ▶.",
                                  reply_markup=batch_keyboard(chat_id))
    elif data.startswith("n:"):
        storage.set_display(chat_id, per_batch=int(data[2:]))
        await q.answer(f"По {data[2:]} за раз")
        await q.edit_message_text("Твои источники 👇", reply_markup=settings_keyboard(chat_id))
    elif data.startswith("pg:"):  # листалка выпуска: pg:<выпуск>:<страница>
        _, issue_id, page = data.split(":")
        issue = storage.get_issue(int(issue_id))
        if not issue or issue["chat_id"] != chat_id:
            await q.answer("Этот выпуск уже удалён — открой /archive", show_alert=True)
            return
        await q.answer()
        storage.log_event(chat_id, "more")
        await show_page(ctx.bot, chat_id, issue, int(page), from_message=q.message.message_id)
    elif data.startswith("open:"):  # из архива
        issue = storage.get_issue(int(data[5:]))
        if not issue or issue["chat_id"] != chat_id:
            await q.answer("Этот выпуск уже удалён", show_alert=True)
            return
        await q.answer()
        storage.log_event(chat_id, "archive_open")
        await show_page(ctx.bot, chat_id, issue, 0)
    elif data == "arch":
        await q.answer()
        await send_archive(ctx.bot, chat_id)
    elif data == "more":  # кнопка из старых подборок, до выпусков
        await q.answer("Эта кнопка устарела — все подборки теперь в /archive", show_alert=True)
    elif data == "back":
        await q.answer()
        await q.edit_message_text("Твои источники 👇", reply_markup=settings_keyboard(chat_id))
    elif data == "done":
        n = len(storage.user_source_ids(chat_id))
        await q.answer()
        await q.edit_message_text(
            f"Готово! Источников выбрано: {n}. Подборка будет приходить каждый день в {when_label(chat_id)}.\n\n"
            "Хочешь прямо сейчас — /digest. Поменять источники — /settings.")


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    kind = ctx.user_data.get("awaiting")
    chat = update.effective_chat
    if not kind:
        await chat.send_message("Настроить источники — /settings, свежее прямо сейчас — /digest.")
        return
    text = update.message.text.strip()
    await chat.send_message("Проверяю ссылку… ⏳")
    try:
        if kind == "youtube":
            url, name = await asyncio.to_thread(sources.resolve_youtube, text)
            lang = "ru"
        elif kind == "telegram":
            url, name = await asyncio.to_thread(sources.resolve_telegram, text)
            lang = "ru"
        else:
            url, name, lang = await asyncio.to_thread(sources.resolve_rss, text)
    except sources.NotFound as e:
        await chat.send_message(f"{e}\n\nПопробуй другую ссылку или /cancel.")
        return
    except Exception as e:
        log.warning("не получилось добавить %s: %s", text, e)
        await chat.send_message("Не получилось открыть эту ссылку. Проверь её или /cancel.")
        return
    ctx.user_data.pop("awaiting", None)
    sid = storage.add_custom_source(chat.id, kind, name, url, lang)
    storage.log_event(chat.id, "add_source")
    await asyncio.to_thread(refresh, storage.sources_by_ids([sid]))
    await chat.send_message(f"Добавил «{html.escape(name)}» ✅", parse_mode=ParseMode.HTML,
                            reply_markup=settings_keyboard(chat.id))


# ---------- сбор и рассылка ----------

FETCH_WORKERS = 8                  # сколько источников собираем одновременно
TG_LIMIT = threading.Semaphore(3)  # t.me не любит много запросов разом
SEND_CONCURRENCY = 8               # скольким подписчикам шлём одновременно (лимит Telegram ~30 сообщений/с)


def refresh_one(s) -> None:
    first_time = not storage.known_links(s["id"])
    if s["kind"] == "telegram":
        with TG_LIMIT:
            items = sources.fetch(s)
    else:
        items = sources.fetch(s)
    if first_time and "scrape" in s.keys() and s["scrape"]:
        # у сайта без RSS нет дат: всё, что было до подключения, — не «свежее»
        for i in items:
            i["ts"] = i["ts"] or time.time() - NEW_WINDOW - 3600
    added = storage.save_items(s["id"], items)
    if added:
        log.info("%s: +%d", s["name"], added)


def refresh(srcs) -> None:
    """Все источники параллельно: время сбора ≈ самый медленный источник, а не сумма всех."""
    srcs = list(srcs)
    if len(srcs) <= 1:
        for s in srcs:
            refresh_one(s)
        return
    with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as pool:
        for f in [pool.submit(refresh_one, s) for s in srcs]:
            try:
                f.result()
            except Exception as e:
                log.warning("сбор источника упал: %s", e)


def prepare(items) -> dict[str, tuple[str, str]]:
    """Заголовок и хайлайт для каждого материала. Готовые берём из базы, новые считаем и сохраняем."""
    ready, texts = {}, []
    for i in items:
        if i["ru_summary"] is not None:
            ready[i["link"]] = (i["ru_title"] or i["title"], i["ru_summary"])
        elif i["source_kind"] == "youtube":
            title, text, by_ai = sources.highlight_video(i)
            ready[i["link"]] = (title, text)
            if by_ai:  # без ИИ не кешируем: вдруг ключ добавят позже
                storage.set_translation(i["link"], title, text)
        else:
            texts.append(dict(i))
    if texts:
        done = sources.highlight_texts(texts)
        for i in texts:
            title, text = done.get(i["link"]) or (i["title"], sources.few_sentences(i["summary"]))
            ready[i["link"]] = (title, text)
            if i["link"] in done:  # без ИИ не кешируем: вдруг ключ добавят позже
                storage.set_translation(i["link"], title, text)
    return ready


ICONS = {"youtube": "🎬 ", "telegram": "💬 "}


def title_line(item, title: str) -> str:
    return f"{ICONS.get(item['source_kind'], '')}<b>{html.escape(title)}</b>"


def link_line(item) -> str:
    return f'<a href="{html.escape(item["link"])}">{html.escape(item["source_name"])} →</a>'


def entry(item, title: str, text: str) -> str:
    out = title_line(item, title)
    if text:
        out += "\n" + html.escape(text)
    return out + "\n" + link_line(item)


def caption(item, text: str) -> str:
    return (html.escape(text) + "\n" if text else "") + link_line(item)


def pack(header: str, entries: list[str], limit: int = 4000) -> list[str]:
    """Всё одним сообщением; если не влезает в лимит Telegram — продолжение следующим."""
    messages, current = [], header
    for e in entries:
        if current and len(current) + 2 + len(e) > limit:
            messages.append(current)
            current = e
        else:
            current = f"{current}\n\n{e}" if current else e
    messages.append(current)
    return messages


def image_candidates(item) -> list[str]:
    """Адреса картинки по порядку: что попробовать отправить."""
    img = item["image"] if "image" in item.keys() else None
    extra = json.loads(item["extra"] or "{}")
    if img:  # уже выбрана раньше (в т.ч. обложка видео) — без лишних запросов
        return [img]
    if item["source_kind"] == "youtube" and extra.get("id"):
        vid = extra["id"]
        hq = f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
        maxres = f"https://i.ytimg.com/vi/{vid}/maxresdefault.jpg"
        try:  # у старых видео нет обложки в высоком качестве — YouTube отдаёт заглушку с 404
            ok = requests.head(maxres, timeout=10).status_code == 200
        except Exception:
            ok = False
        storage.set_image(item["link"], maxres if ok else hq)  # проверяем один раз на видео
        return [maxres, hq] if ok else [hq]
    if img:
        return [img]
    if img is None and item["source_kind"] == "rss":  # в ленте не было — ищем на странице статьи
        found = sources.page_image(item["link"])
        storage.set_image(item["link"], found)  # пустая строка = «искали, нет»
        return [found] if found else []
    return []


def today() -> str:
    d = dt.datetime.now(MSK)
    return f"{d.day} {MONTHS[d.month - 1]}"


ISSUE_MAX = 100  # больше материалов в один выпуск не кладём


def page_keyboard(issue_id: int, page: int, pages: int) -> InlineKeyboardMarkup:
    rows = []
    if pages > 1:
        nav = []
        nav.append(InlineKeyboardButton("◀", callback_data=f"pg:{issue_id}:{page - 1}") if page > 0
                   else InlineKeyboardButton(" ", callback_data="noop"))
        nav.append(InlineKeyboardButton(f"{page + 1} / {pages}", callback_data="noop"))
        nav.append(InlineKeyboardButton("▶", callback_data=f"pg:{issue_id}:{page + 1}") if page < pages - 1
                   else InlineKeyboardButton("↺ В начало", callback_data=f"pg:{issue_id}:0"))
        rows.append(nav)
    rows.append([InlineKeyboardButton("📚 Архив выпусков", callback_data="arch")])
    return InlineKeyboardMarkup(rows)


async def show_page(bot, chat_id: int, issue, page: int, from_message: int | None = None) -> None:
    """Показать страницу выпуска. Прошлую страницу этого выпуска заменяем — в чате остаётся одна."""
    images, per = display(chat_id)
    all_items = storage.items_by_links(json.loads(issue["links"]))
    if not all_items:
        await bot.send_message(chat_id, "Материалы этого выпуска уже удалены из базы.")
        return
    pages = (len(all_items) + per - 1) // per
    page = max(0, min(page, pages - 1))
    items = all_items[page * per:(page + 1) * per]
    header = f"{issue['title']}\nВсего {len(all_items)}" + (f" · страница {page + 1} из {pages}" if pages > 1 else "")
    ready = await asyncio.to_thread(prepare, items)
    markup = page_keyboard(issue["id"], page, pages)
    old = set(json.loads(issue["msg_ids"] or "[]"))
    if from_message:
        old.add(from_message)

    # без картинок и страница — одно сообщение: меняем текст на месте, без мигания
    texts = pack(header, [entry(i, *ready[i["link"]]) for i in items]) if not images else []
    if texts and len(texts) == 1 and from_message and old == {from_message}:
        try:
            await bot.edit_message_text(texts[0], chat_id=chat_id, message_id=from_message,
                                        parse_mode=ParseMode.HTML, link_preview_options=NO_PREVIEW,
                                        reply_markup=markup)
            storage.set_issue_view(issue["id"], page, [from_message])
            return
        except TelegramError as e:
            log.info("не отредактировал страницу, пришлю новой: %s", e)

    ids = await send_items(bot, chat_id, header, items, ready, images, markup)
    storage.set_issue_view(issue["id"], page, ids)
    old -= set(ids)
    if old:
        try:
            await bot.delete_messages(chat_id, sorted(old))
        except TelegramError as e:  # старше 48 часов Telegram удалять не даёт — просто убираем кнопки
            log.info("не удалил прошлую страницу: %s", e)
            for mid in old:
                try:
                    await bot.edit_message_reply_markup(chat_id=chat_id, message_id=mid, reply_markup=None)
                except TelegramError:
                    pass


def user_tz(chat_id: int) -> ZoneInfo:
    u = storage.get_user(chat_id)
    try:
        return ZoneInfo((u["tz"] if u else None) or "Europe/Moscow")
    except Exception:
        return MSK


async def send_archive(bot, chat_id: int) -> None:
    issues = storage.list_issues(chat_id)
    if not issues:
        await bot.send_message(chat_id, "Архив пока пуст — первый выпуск придёт по расписанию или по /digest.")
        return
    rows = []
    for i in issues:
        d = dt.datetime.fromtimestamp(i["created_at"], user_tz(chat_id))
        n = len(json.loads(i["links"]))
        rows.append([InlineKeyboardButton(f"{d.day} {MONTHS[d.month - 1]}, {d:%H:%M} · {n} мат.",
                                          callback_data=f"open:{i['id']}")])
    await bot.send_message(chat_id, f"📚 <b>Последние выпуски</b>\nХранятся {storage.ISSUE_DAYS} дней.",
                           parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows))


async def send_items(bot, chat_id: int, header: str, items, ready, images: bool, more) -> list[int]:
    """Отправить материалы страницы. Возвращает id отправленных сообщений."""
    ids: list[int] = []
    if not images:
        texts = pack(header, [entry(i, *ready[i["link"]]) for i in items])
        for n, text in enumerate(texts):
            m = await bot.send_message(chat_id, text, parse_mode=ParseMode.HTML, link_preview_options=NO_PREVIEW,
                                       reply_markup=more if n == len(texts) - 1 else None)
            ids.append(m.message_id)
            await asyncio.sleep(0.1)
    else:
        photos = await asyncio.to_thread(collect_photos, items)
        if not await send_rich(bot, chat_id, header, items, ready, photos, more, ids):
            await send_separately(bot, chat_id, header, items, ready, photos, more, ids)
    return ids


def collect_photos(items) -> dict[str, tuple[str, bytes]]:
    """link → (адрес картинки, скачанные байты). Без картинки материала в словаре нет."""
    out = {}
    for i in items:
        for pic in image_candidates(i):
            data = sources.download_image(pic, i["link"])
            if data:
                out[i["link"]] = (pic, data)
                break
    return out


def rich_html(header: str, items, ready, photos, uploaded: bool) -> str:
    """Одна «статья»: шапка, затем для каждого материала заголовок, картинка, хайлайт, ссылка."""
    parts = [f"<p>{header.replace(chr(10), '<br/>')}</p>"] if header else []
    for n, i in enumerate(items):
        title, text = ready[i["link"]]
        if n or header:
            parts.append("<p><br/></p>")  # отступ перед следующим материалом
        parts.append(f"<p>{title_line(i, title)}</p>")
        if i["link"] in photos:
            src = f"tg://photo?id=p{n}" if uploaded else html.escape(photos[i["link"]][0])
            parts.append(f'<img src="{src}"/>')
        parts.append(f"<p>{caption(i, text).replace(chr(10), '<br/>')}</p>")
    return "".join(parts)


def _rich_request(token: str, chat_id: int, html_text: str, photos, items, uploaded: bool, more) -> dict:
    rich = {"html": html_text, "skip_entity_detection": True}
    files = {}
    if uploaded:
        rich["media"] = []
        for n, i in enumerate(items):
            if i["link"] in photos:
                rich["media"].append({"id": f"p{n}", "media": {"type": "photo", "media": f"attach://p{n}"}})
                files[f"p{n}"] = (f"p{n}.jpg", photos[i["link"]][1])
    data = {"chat_id": str(chat_id), "rich_message": json.dumps(rich, ensure_ascii=False)}
    if more:
        data["reply_markup"] = json.dumps(more.to_dict(), ensure_ascii=False)
    r = requests.post(f"https://api.telegram.org/bot{token}/sendRichMessage",
                      data=data, files=files or None, timeout=120)
    return r.json()


async def send_rich(bot, chat_id: int, header: str, items, ready, photos, more, ids: list) -> bool:
    """Всё одним сообщением-статьёй (Bot API 10.1+). Сначала картинки файлами, потом ссылками."""
    for uploaded in ([True, False] if photos else [False]):
        html_text = rich_html(header, items, ready, photos, uploaded)
        try:
            res = await asyncio.to_thread(_rich_request, bot.token, chat_id, html_text, photos,
                                          items, uploaded, more)
        except Exception as e:
            log.warning("статья не отправилась: %s", e)
            continue
        if res.get("ok"):
            ids.append(res["result"]["message_id"])
            if more and not res["result"].get("reply_markup"):  # кнопку к статье не прикрепили
                m = await bot.send_message(chat_id, "Это не всё 👇", reply_markup=more)
                ids.append(m.message_id)
            return True
        if res.get("error_code") == 403:
            raise Forbidden(res.get("description", ""))
        log.warning("статья не отправилась (%s): %s", "файлы" if uploaded else "ссылки",
                    res.get("description"))
    return False


async def send_separately(bot, chat_id: int, header: str, items, ready, photos, more, ids: list) -> None:
    """Запасной вариант: каждый материал своим сообщением с картинкой."""
    for n, i in enumerate(items):
        title, text = ready[i["link"]]
        markup = more if n == len(items) - 1 else None
        body = entry(i, title, text)
        head = f"{header}\n\n" if header and n == 0 else ""
        if i["link"] in photos and len(head + body) <= 1024:
            try:
                m = await bot.send_photo(chat_id, photos[i["link"]][1], caption=head + body,
                                         parse_mode=ParseMode.HTML, reply_markup=markup)
                ids.append(m.message_id)
                await asyncio.sleep(0.1)
                continue
            except Forbidden:
                raise
            except TelegramError as e:
                log.info("картинка не отправилась (%s): %s", i["link"], e)
        m = await bot.send_message(chat_id, head + body, parse_mode=ParseMode.HTML,
                                   link_preview_options=NO_PREVIEW, reply_markup=markup)
        ids.append(m.message_id)
        await asyncio.sleep(0.1)


async def send_digest(bot, chat_id: int, on_demand: bool = False) -> int:
    """Новый выпуск: всё свежее разом фиксируем списком и показываем первую страницу.
    Всё, что вошло в выпуск, считается отправленным — завтра не повторится, но останется в архиве."""
    items = storage.items_for_user(chat_id, time.time() - NEW_WINDOW, "digest", ISSUE_MAX)  # от новых к старым
    if not items:
        return 0
    links = [i["link"] for i in items]
    now = dt.datetime.now(user_tz(chat_id))
    title = (f"🎨 <b>Свежее · {now.day} {MONTHS[now.month - 1]}, {now:%H:%M}</b>" if on_demand
             else f"🎨 <b>Дизайн-дайджест · {today()}</b>")
    issue_id = storage.create_issue(chat_id, title, links)
    storage.mark_delivered(chat_id, links, "digest")
    await show_page(bot, chat_id, storage.get_issue(issue_id), 0)
    # хайлайты для остальных страниц готовим заранее, чтобы листалось без задержек
    asyncio.get_running_loop().run_in_executor(None, prepare, items)
    return len(items)


async def send_recap(bot, chat_id: int) -> None:
    items = storage.items_for_user(chat_id, time.time() - WEEK, "recap", RECAP_LIMIT)
    if not items:
        await bot.send_message(chat_id, f"🎨 <b>Дизайн-дайджест · {today()}</b>\n\n"
                               "Сегодня в твоих источниках тихо. Загляну завтра 🙂",
                               parse_mode=ParseMode.HTML)
        return
    lines = []
    for i in items:
        title = i["ru_title"] or i["title"]
        icon = {"youtube": "🎬 ", "telegram": "💬 "}.get(i["source_kind"], "")
        lines.append(f'• {icon}<a href="{html.escape(i["link"])}"><b>{html.escape(title)}</b></a>'
                     f' — {html.escape(i["source_name"])}')
    await bot.send_message(
        chat_id,
        f"🎨 <b>Дизайн-дайджест · {today()}</b>\n\n"
        "Свежего сегодня нет — вот главное за неделю:\n\n" + "\n".join(lines),
        parse_mode=ParseMode.HTML, link_preview_options=NO_PREVIEW)
    storage.mark_delivered(chat_id, [i["link"] for i in items], "recap")


async def job_refresh(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await asyncio.to_thread(refresh, storage.subscribed_sources())


def due_users(now: dt.datetime | None = None) -> list:
    """Кому пора отправить: наступил их час в их часовом поясе, а сегодня ещё не отправляли."""
    now = now or dt.datetime.now(dt.timezone.utc)
    due = []
    for u in storage.active_users():
        try:
            local = now.astimezone(ZoneInfo(u["tz"] or "Europe/Moscow"))
        except Exception:
            local = now.astimezone(MSK)
        hour = u["hour"] if u["hour"] is not None else DEFAULT_HOUR
        if hour <= local.hour < hour + CATCH_UP_HOURS and u["last_sent"] != local.date().isoformat():
            due.append((u["chat_id"], local.date().isoformat()))
    return due


async def job_send(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Раз в 10 минут: кому пора — тем подборка."""
    due = due_users()
    if not due:
        return
    started = time.time()
    ids = sorted({sid for chat_id, _ in due for sid in storage.user_source_ids(chat_id)})
    await asyncio.to_thread(refresh, storage.sources_by_ids(ids))
    await asyncio.to_thread(warm_up, [chat_id for chat_id, _ in due])
    log.info("рассылка: %d подписчиков (подготовка %.0f с)", len(due), time.time() - started)

    gate = asyncio.Semaphore(SEND_CONCURRENCY)

    async def deliver(chat_id: int, day: str) -> None:
        async with gate:
            try:
                if await send_digest(ctx.bot, chat_id):
                    storage.log_event(chat_id, "daily")
                else:
                    await send_recap(ctx.bot, chat_id)
                    storage.log_event(chat_id, "recap")
            except Forbidden:
                storage.set_active(chat_id, False)  # человек заблокировал бота
                log.info("%s заблокировал бота — отписал", chat_id)
            except TelegramError as e:
                log.warning("не доставил %s: %s", chat_id, e)
                return
            except Exception as e:
                log.error("рассылка %s упала: %s", chat_id, e, exc_info=e)
                return
            storage.set_last_sent(chat_id, day)

    await asyncio.gather(*(deliver(c, d) for c, d in due))
    log.info("рассылка готова за %.0f с", time.time() - started)


def warm_up(chat_ids: list[int]) -> None:
    """Готовим всё общее заранее и один раз: хайлайты и картинки материалов,
    которые уйдут хоть кому-то. Потом каждому подписчику остаётся только отправить."""
    since = time.time() - NEW_WINDOW
    pool: dict[str, object] = {}
    with_images: dict[str, object] = {}
    for chat_id in chat_ids:
        images, batch = display(chat_id)
        for n, i in enumerate(storage.items_for_user(chat_id, since, "digest", ISSUE_MAX)):
            pool[i["link"]] = i
            if images and n < batch:  # картинки нужны сразу только для первой страницы
                with_images[i["link"]] = i
    if not pool:
        return
    prepare(list(pool.values()))
    if with_images:
        with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as ex:
            list(ex.map(lambda i: collect_photos([i]), with_images.values()))


async def job_cleanup(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    storage.cleanup()
    await asyncio.to_thread(sources.cleanup_images)


async def on_error(update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("ошибка: %s", ctx.error, exc_info=ctx.error)


# ---------- статистика для админа ----------

ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_IDS", "").replace(" ", "").split(",") if x.isdigit()}


def format_stats(st: dict) -> str:
    def bar(n, top):
        return "▇" * max(1, round(8 * n / top)) if top else ""
    lines = [
        "📊 <b>Статистика бота</b>",
        "",
        f"👥 Подписчиков: <b>{st['active']}</b> (всего было {st['total']}, отписались {st['stopped']})",
        f"📈 Новых: сегодня {st['new_day']} · за неделю {st['new_week']} · за месяц {st['new_month']}",
        f"🔥 Заходили в бота за неделю: {st['alive_week']} чел.",
        f"👋 Отписались за неделю: {st['stops_week']}",
        "",
        f"📬 Утренних подборок: за сутки {st['daily_day']} · за неделю {st['daily_week']}",
        f"⚡ /digest за неделю: {st['digest_cmd_week']} · нажатий «Ещё»: {st['more_week']}",
        f"🗞 Собрано материалов за сутки: {st['items_day']}",
    ]
    if st["growth"]:
        top = max(n for _, n in st["growth"])
        lines += ["", "<b>Новые подписчики по дням</b>"]
        lines += [f"<code>{d[5:]}</code> {bar(n, top)} {n}" for d, n in st["growth"]]
    if st["top_sources"]:
        top = st["top_sources"][0]["n"]
        lines += ["", "<b>Популярные источники</b>"]
        lines += [f"{bar(r['n'], top)} {r['n']} · {html.escape(r['name'])}{'' if r['builtin'] else ' (свой)'}"
                  for r in st["top_sources"]]
    lines += ["", f"➕ Своих источников добавлено: {st['custom']}"]
    if st["hours"]:
        lines += ["🕙 Популярное время: " + ", ".join(f"{h}:00 — {n}" for h, n in st["hours"])]
    return "\n".join(lines)


async def cmd_archive(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    storage.add_user(update.effective_chat.id, update.effective_user.full_name)
    await send_archive(ctx.bot, update.effective_chat.id)


async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if not ADMIN_IDS:
        await update.effective_chat.send_message(
            f"Статистика пока закрыта для всех. Твой id: <code>{chat_id}</code>\n\n"
            "Чтобы она открылась только тебе, впиши на сервере в /home/bot/app/.env строку\n"
            f"<code>ADMIN_IDS={chat_id}</code>\nи перезапусти бота: <code>systemctl restart design-bot</code>",
            parse_mode=ParseMode.HTML)
        return
    if chat_id not in ADMIN_IDS:
        await update.effective_chat.send_message("Настроить источники — /settings, свежее прямо сейчас — /digest.")
        return
    st = await asyncio.to_thread(storage.admin_stats)
    await update.effective_chat.send_message(format_stats(st), parse_mode=ParseMode.HTML)


# ---------- запуск ----------

async def post_init(app: Application) -> None:
    await app.bot.set_my_commands([
        BotCommand("settings", "Выбрать источники"),
        BotCommand("digest", "Свежее прямо сейчас"),
        BotCommand("archive", "Прошлые выпуски"),
        BotCommand("stop", "Отписаться"),
    ])
    for admin in ADMIN_IDS:  # админу в меню команд — ещё и /stats
        try:
            await app.bot.set_my_commands([
                BotCommand("settings", "Выбрать источники"),
                BotCommand("digest", "Свежее прямо сейчас"),
                BotCommand("archive", "Прошлые выпуски"),
                BotCommand("stats", "Статистика бота"),
                BotCommand("stop", "Отписаться"),
            ], scope=BotCommandScopeChat(admin))
        except TelegramError as e:
            log.warning("меню админа %s: %s", admin, e)
    await app.bot.set_my_description(
        "🎨 Дизайн-дайджест\n\nКаждый день в удобное тебе время — подборка из выбранных тобой "
        "источников: новости дизайна, статьи с переводом, новые видео с хайлайтами и посты "
        "из Telegram-каналов. Источники выбираешь сам.")
    await app.bot.set_my_short_description("Персональный дайджест новостей дизайна")
    log.info("бот запущен, %s", storage.stats())


def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("Нет TELEGRAM_BOT_TOKEN — впиши его в /home/bot/app/.env")
    storage.init()
    app = (Application.builder().token(token).post_init(post_init).build())
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CommandHandler("digest", cmd_digest))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("archive", cmd_archive))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, on_text))
    app.add_error_handler(on_error)
    app.job_queue.run_repeating(job_send, interval=600, first=30, name="send")
    app.job_queue.run_repeating(job_cleanup, interval=24 * 3600, first=3600, name="cleanup")
    app.job_queue.run_repeating(job_refresh, interval=3 * 3600, first=60, name="refresh")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)


if __name__ == "__main__":
    main()
