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
import time
from zoneinfo import ZoneInfo

import requests
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
    if new:
        log.info("новый подписчик %s", chat.id)


async def cmd_settings(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    storage.add_user(update.effective_chat.id, update.effective_user.full_name)
    await update.effective_chat.send_message("Твои источники 👇",
                                             reply_markup=settings_keyboard(update.effective_chat.id))


async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    storage.set_active(update.effective_chat.id, False)
    await update.effective_chat.send_message(
        "Ок, больше не присылаю. Захочешь вернуться — /start, настройки сохранятся.")


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    ctx.user_data.pop("awaiting", None)
    await update.effective_chat.send_message("Отменил.")


async def cmd_digest(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    storage.add_user(chat_id, update.effective_user.full_name)
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
        await q.edit_message_text("Сколько материалов присылать за раз? Остальное — по кнопке «Ещё».",
                                  reply_markup=batch_keyboard(chat_id))
    elif data.startswith("n:"):
        storage.set_display(chat_id, per_batch=int(data[2:]))
        await q.answer(f"По {data[2:]} за раз")
        await q.edit_message_text("Твои источники 👇", reply_markup=settings_keyboard(chat_id))
    elif data == "more":
        await q.answer()
        try:
            await q.edit_message_reply_markup(None)  # убираем кнопку со старого сообщения
        except TelegramError:
            pass
        if not await send_batch(ctx.bot, chat_id):
            await q.message.reply_text("Больше свежего нет 🙂")
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
    await asyncio.to_thread(refresh, storage.sources_by_ids([sid]))
    await chat.send_message(f"Добавил «{html.escape(name)}» ✅", parse_mode=ParseMode.HTML,
                            reply_markup=settings_keyboard(chat.id))


# ---------- сбор и рассылка ----------

def refresh(srcs) -> None:
    for s in srcs:
        first_time = not storage.known_links(s["id"])
        items = sources.fetch(s)
        if first_time and "scrape" in s.keys() and s["scrape"]:
            # у сайта без RSS нет дат: всё, что было до подключения, — не «свежее»
            for i in items:
                i["ts"] = i["ts"] or time.time() - NEW_WINDOW - 3600
        added = storage.save_items(s["id"], items)
        if added:
            log.info("%s: +%d", s["name"], added)


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
    if item["source_kind"] == "youtube" and extra.get("id"):
        vid = extra["id"]
        hq = f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
        maxres = f"https://i.ytimg.com/vi/{vid}/maxresdefault.jpg"
        try:  # у старых видео нет обложки в высоком качестве — YouTube отдаёт заглушку с 404
            ok = requests.head(maxres, timeout=10).status_code == 200
        except Exception:
            ok = False
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


async def send_batch(bot, chat_id: int, header: str = "") -> int:
    """Очередная порция свежего: N штук, остальное — по кнопке «Ещё»."""
    images, batch = display(chat_id)
    since = time.time() - NEW_WINDOW
    total = storage.count_for_user(chat_id, since, "digest")
    items = storage.items_for_user(chat_id, since, "digest", batch)  # от новых к старым
    if not items:
        return 0
    header = header.format(total=total)
    ready = await asyncio.to_thread(prepare, items)
    rest = total - len(items)
    more = InlineKeyboardMarkup([[InlineKeyboardButton(f"Ещё {min(rest, batch)} из {rest} →",
                                                       callback_data="more")]]) if rest > 0 else None

    if not images:
        texts = pack(header, [entry(i, *ready[i["link"]]) for i in items])
        for n, text in enumerate(texts):
            await bot.send_message(chat_id, text, parse_mode=ParseMode.HTML, link_preview_options=NO_PREVIEW,
                                   reply_markup=more if n == len(texts) - 1 else None)
            await asyncio.sleep(0.1)
    else:
        for n, i in enumerate(items):
            title, text = ready[i["link"]]
            markup = more if n == len(items) - 1 else None
            head = f"{header}\n\n" if header and n == 0 else ""
            pics = await asyncio.to_thread(image_candidates, i)
            sent = False
            if pics:
                await bot.send_message(chat_id, head + title_line(i, title), parse_mode=ParseMode.HTML,
                                       link_preview_options=NO_PREVIEW)
                for pic in pics:
                    try:
                        await bot.send_photo(chat_id, pic, caption=caption(i, text),
                                             parse_mode=ParseMode.HTML, reply_markup=markup)
                        sent = True
                        break
                    except Forbidden:
                        raise
                    except TelegramError as e:
                        log.info("картинка не отправилась (%s): %s", pic, e)
                if not sent:  # заголовок уже ушёл — досылаем текст без картинки
                    await bot.send_message(chat_id, caption(i, text), parse_mode=ParseMode.HTML,
                                           link_preview_options=NO_PREVIEW, reply_markup=markup)
            else:
                await bot.send_message(chat_id, head + entry(i, title, text), parse_mode=ParseMode.HTML,
                                       link_preview_options=NO_PREVIEW, reply_markup=markup)
            await asyncio.sleep(0.1)
    storage.mark_delivered(chat_id, [i["link"] for i in items], "digest")
    return len(items)


async def send_digest(bot, chat_id: int, on_demand: bool = False) -> int:
    header = ("🎨 <b>Свежее из твоих источников: {total}</b>" if on_demand
              else f"🎨 <b>Дизайн-дайджест · {today()}</b>\nСвежее из твоих источников: {{total}}")
    return await send_batch(bot, chat_id, header)


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
    ids = sorted({sid for chat_id, _ in due for sid in storage.user_source_ids(chat_id)})
    await asyncio.to_thread(refresh, storage.sources_by_ids(ids))
    log.info("рассылка: %d подписчиков", len(due))
    for chat_id, day in due:
        try:
            if not await send_digest(ctx.bot, chat_id):
                await send_recap(ctx.bot, chat_id)
        except Forbidden:
            storage.set_active(chat_id, False)  # человек заблокировал бота
            log.info("%s заблокировал бота — отписал", chat_id)
        except TelegramError as e:
            log.warning("не доставил %s: %s", chat_id, e)
            continue
        storage.set_last_sent(chat_id, day)


async def job_cleanup(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    storage.cleanup()


async def on_error(update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("ошибка: %s", ctx.error, exc_info=ctx.error)


# ---------- запуск ----------

async def post_init(app: Application) -> None:
    await app.bot.set_my_commands([
        BotCommand("settings", "Выбрать источники"),
        BotCommand("digest", "Свежее прямо сейчас"),
        BotCommand("stop", "Отписаться"),
    ])
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
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, on_text))
    app.add_error_handler(on_error)
    app.job_queue.run_repeating(job_send, interval=600, first=30, name="send")
    app.job_queue.run_repeating(job_cleanup, interval=24 * 3600, first=3600, name="cleanup")
    app.job_queue.run_repeating(job_refresh, interval=3 * 3600, first=60, name="refresh")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)


if __name__ == "__main__":
    main()
