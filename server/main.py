"""
Дизайн-дайджест: персональный Telegram-бот.

/start     — подписаться (включаются источники по умолчанию) и открыть настройки
/settings  — выбрать YouTube-каналы, сайты и Telegram-каналы, добавить свои
/digest    — прислать свежее прямо сейчас
/stop      — отписаться

Каждый день в 10:00 по Москве каждый получает подборку из своих источников.
Если нового нет — «главное за неделю».

Запуск: python -m server.main  (токен в переменной TELEGRAM_BOT_TOKEN)
"""

import asyncio
import datetime as dt
import html
import logging
import os
import time
from zoneinfo import ZoneInfo

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
DIGEST_TIME = dt.time(10, 0, tzinfo=MSK)
DIGEST_LIMIT = 15
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
    "Каждое утро в 10:00 по Москве присылаю подборку из выбранных тобой источников: "
    "новости дизайна, статьи (английские — с переводом), новые видео с хайлайтами "
    "и посты из Telegram-каналов.\n\n"
    "Отметь, что тебе интересно 👇"
)
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
          "августа", "сентября", "октября", "ноября", "декабря"]
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


# ---------- меню ----------

def settings_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    rows = []
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
    elif data == "done":
        n = len(storage.user_source_ids(chat_id))
        await q.answer()
        await q.edit_message_text(
            f"Готово! Источников выбрано: {n}. Подборка придёт завтра в 10:00 по Москве.\n\n"
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
        items = sources.fetch(s)
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


def entry(item, title: str, text: str) -> str:
    link = html.escape(item["link"])
    out = f"{ICONS.get(item['source_kind'], '')}<b>{html.escape(title)}</b>"
    if text:
        out += "\n" + html.escape(text)
    return out + f'\n<a href="{link}">{html.escape(item["source_name"])} →</a>'


def pack(header: str, entries: list[str], limit: int = 4000) -> list[str]:
    """Всё одним сообщением; если не влезает в лимит Telegram — продолжение следующим."""
    messages, current = [], header
    for e in entries:
        if len(current) + 2 + len(e) > limit:
            messages.append(current)
            current = e
        else:
            current += "\n\n" + e
    messages.append(current)
    return messages


def today() -> str:
    d = dt.datetime.now(MSK)
    return f"{d.day} {MONTHS[d.month - 1]}"


async def send_digest(bot, chat_id: int, on_demand: bool = False) -> int:
    now = time.time()
    items = storage.items_for_user(chat_id, now - NEW_WINDOW, "digest", DIGEST_LIMIT)
    if not items:
        return 0
    items = list(reversed(items))  # от старых к новым
    header = (f"🎨 <b>Дизайн-дайджест · {today()}</b>\nСвежее из твоих источников: {len(items)}"
              if not on_demand else f"🎨 <b>Свежее из твоих источников: {len(items)}</b>")
    ready = await asyncio.to_thread(prepare, items)
    entries = [entry(i, *ready[i["link"]]) for i in items]
    for text in pack(header, entries):
        await bot.send_message(chat_id, text, parse_mode=ParseMode.HTML, link_preview_options=NO_PREVIEW)
        await asyncio.sleep(0.1)
    sent = [i["link"] for i in items]
    storage.mark_delivered(chat_id, sent, "digest")
    return len(sent)


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


async def job_daily(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await asyncio.to_thread(refresh, storage.subscribed_sources())
    users = storage.active_users()
    log.info("утренняя рассылка: %d подписчиков", len(users))
    for chat_id in users:
        try:
            if not await send_digest(ctx.bot, chat_id):
                await send_recap(ctx.bot, chat_id)
        except Forbidden:
            storage.set_active(chat_id, False)  # человек заблокировал бота
            log.info("%s заблокировал бота — отписал", chat_id)
        except TelegramError as e:
            log.warning("не доставил %s: %s", chat_id, e)
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
        "🎨 Дизайн-дайджест\n\nКаждое утро в 10:00 по Москве — подборка из выбранных тобой "
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
    app.job_queue.run_daily(job_daily, DIGEST_TIME, name="daily")
    app.job_queue.run_repeating(job_refresh, interval=3 * 3600, first=60, name="refresh")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)


if __name__ == "__main__":
    main()
