"""
Парсер новостей о дизайне → Telegram.

Забирает RSS-ленты из config.json, оставляет материалы про дизайн
(по ключевым словам) и отправляет новые в Telegram-чат или канал.
Уже отправленное запоминается в seen.json, чтобы не было дублей.

Переменные окружения:
  TELEGRAM_BOT_TOKEN — токен бота от @BotFather
  TELEGRAM_CHAT_ID   — id чата или @username канала
  DRY_RUN=1          — ничего не отправлять, только напечатать в консоль
"""

import html
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import feedparser
import requests

import youtube

ROOT = Path(__file__).parent
CONFIG_PATH = ROOT / "config.json"
SEEN_PATH = ROOT / "seen.json"
SEEN_LIMIT = 3000  # сколько последних ссылок помнить

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
DRY_RUN = os.environ.get("DRY_RUN") == "1"

HEADERS = {"User-Agent": "Mozilla/5.0 (design-news-bot)"}


# ---------- хранилище ----------

def load_config() -> dict:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def load_seen() -> list[str] | None:
    if not SEEN_PATH.exists():
        return None  # первый запуск
    return json.loads(SEEN_PATH.read_text(encoding="utf-8"))


def save_seen(seen: list[str]) -> None:
    SEEN_PATH.write_text(
        json.dumps(seen[-SEEN_LIMIT:], ensure_ascii=False, indent=0),
        encoding="utf-8",
    )


# ---------- разбор лент ----------

def clean_url(url: str) -> str:
    """Убираем utm-метки и прочие параметры, чтобы одна статья = один ключ."""
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc, p.path, "", ""))


def strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def build_matcher(words: list[str]):
    """Короткие латинские слова (ux, ui, kv, 3d) ищем целиком,
    остальное — как начало слова, чтобы «айдентик» ловил «айдентику»."""
    parts = []
    for w in words:
        w = w.lower().strip()
        if not w:
            continue
        esc = re.escape(w)
        if re.fullmatch(r"[a-z0-9]{1,3}", w):
            parts.append(rf"(?<![a-zа-яё0-9]){esc}(?![a-zа-яё0-9])")
        else:
            parts.append(rf"(?<![a-zа-яё0-9]){esc}")
    if not parts:
        return lambda _t: False
    rx = re.compile("|".join(parts), re.IGNORECASE)
    return lambda text: bool(rx.search(text))


def fetch_feed(feed: dict) -> list[dict]:
    try:
        r = requests.get(feed["url"], headers=HEADERS, timeout=30)
        r.raise_for_status()
    except Exception as e:  # лента недоступна — пропускаем, остальные работают
        print(f"::warning::{feed['name']}: {e}")
        return []

    parsed = feedparser.parse(r.content)
    items = []
    for e in parsed.entries:
        link = e.get("link", "")
        if not link:
            continue
        tags = " ".join(t.get("term", "") for t in e.get("tags", []))
        items.append({
            "source": feed["name"],
            "filter": feed.get("filter", True),
            "title": strip_html(e.get("title", "")),
            "summary": strip_html(e.get("summary", "")),
            "tags": tags,
            "link": clean_url(link),
            "ts": time.mktime(e.published_parsed) if e.get("published_parsed") else 0,
        })
    print(f"::notice::{feed['name']}: {len(items)} материалов в ленте")
    return items


# ---------- Telegram ----------

def format_message(item: dict) -> str:
    title = html.escape(item["title"])
    summary = item["summary"]
    if len(summary) > 280:
        summary = summary[:277].rsplit(" ", 1)[0] + "…"
    summary = html.escape(summary)

    parts = [f"<b>{title}</b>"]
    if summary:
        parts.append(summary)
    parts.append(f'<a href="{html.escape(item["link"])}">{html.escape(item["source"])} →</a>')
    return "\n\n".join(parts)


STATS = {"ok": 0, "fail": 0}


def send(text: str) -> bool:
    if DRY_RUN:
        print("-" * 60)
        print(text)
        return True

    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}

    for _ in range(3):
        r = requests.post(url, json=payload, timeout=30)
        if r.status_code == 429:  # Telegram просит подождать
            wait = r.json().get("parameters", {}).get("retry_after", 5)
            time.sleep(wait + 1)
            continue
        if r.ok:
            STATS["ok"] += 1
            return True
        print(f"::warning::Telegram {r.status_code}: {r.text}")
        STATS["fail"] += 1
        return False
    return False


MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
          "августа", "сентября", "октября", "ноября", "декабря"]


def digest_header(count: int) -> str:
    """Первое сообщение каждой подборки: что это за бот и за какой день."""
    msk = time.gmtime(time.time() + 3 * 3600)
    date = f"{msk.tm_mday} {MONTHS[msk.tm_mon - 1]}"
    return (
        f"🎨 <b>Дизайн-дайджест · {date}</b>\n\n"
        f"Свежие новости российского дизайна: айдентика, брендинг, "
        f"интерфейсы, кейсы. Сегодня в подборке: {count}."
    )


def update_bot_profile() -> None:
    """Описание бота — его видно, когда человек открывает бота, до кнопки «Старт»."""
    if DRY_RUN:
        return
    base = f"https://api.telegram.org/bot{TOKEN}"
    calls = {
        "setMyDescription": {"description": (
            "🎨 Дизайн-дайджест\n\n"
            "Каждое утро в 10:00 по Москве присылаю подборку новостей "
            "российского дизайна: айдентика, брендинг, упаковка, интерфейсы, "
            "кейсы и статьи с Sostav, Cossa и Хабра."
        )},
        "setMyShortDescription": {"short_description":
            "Ежедневный дайджест новостей российского дизайна"},
    }
    for method, payload in calls.items():
        try:
            requests.post(f"{base}/{method}", json=payload, timeout=30)
        except Exception as e:  # описание — не критично, рассылка важнее
            print(f"::warning::{method}: {e}")


# ---------- основной цикл ----------

def main() -> None:
    if not DRY_RUN and not (TOKEN and CHAT_ID):
        sys.exit("Нужны TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID (или DRY_RUN=1).")

    cfg = load_config()
    is_design = build_matcher(cfg.get("keywords", []))
    is_stop = build_matcher(cfg.get("stop_words", []))

    seen_list = load_seen()
    first_run = seen_list is None
    seen_list = seen_list or []
    seen = set(seen_list)

    # собираем всё про дизайн (и старое — пригодится для итогов недели)
    matched = []
    for feed in cfg["feeds"]:
        for item in fetch_feed(feed):
            text = f'{item["title"]} {item["summary"]} {item["tags"]}'
            if is_stop(text):
                continue
            if item["filter"] and not is_design(text):
                continue
            matched.append(item)

    # без дублей между лентами, от старых к новым; новое — то, чего ещё не слали
    matched = sorted({i["link"]: i for i in matched}.values(), key=lambda i: i["ts"])
    fresh = [i for i in matched if i["link"] not in seen]

    # первый запуск: не заваливаем чат архивом, шлём только последние N
    if first_run:
        n = cfg.get("first_run_send_last", 5)
        to_send = fresh[-n:] if n else []
        skip = fresh[:-n] if n else fresh
        seen_list.extend(i["link"] for i in skip)
    else:
        limit = cfg.get("max_posts_per_run", 15)
        to_send = fresh[-limit:]
        seen_list.extend(i["link"] for i in fresh[:-limit])  # переполнение не шлём

    update_bot_profile()

    sent = 0
    if to_send:
        send(digest_header(len(to_send)))
        time.sleep(1.2)
    for item in to_send:
        if send(format_message(item)):
            seen_list.append(item["link"])
            sent += 1
            time.sleep(1.2)  # бережём лимиты Telegram

    # ---------- YouTube: только новые видео, без новых — молчим ----------
    videos_sent = 0
    all_videos = []
    for channel in cfg.get("youtube", []):
        videos = youtube.fetch_channel(channel)
        if not videos:
            continue
        all_videos.extend(videos)
        new = [v for v in videos if v["link"] not in seen]
        # канал проверяется впервые: запоминаем всё, что уже есть, ничего не шлём
        if len(new) == len(videos):
            seen_list.extend(v["link"] for v in videos)
            print(f"YouTube «{channel['name']}»: первая проверка, запомнил {len(videos)} видео")
            continue
        for video in sorted(new, key=lambda v: v["ts"]):
            if send(youtube.format_video_message(video)):
                seen_list.append(video["link"])
                videos_sent += 1
                time.sleep(1.2)

    # ---------- тихий день: вместо тишины — главное за неделю ----------
    if not first_run and sent == 0 and videos_sent == 0:
        send_weekly_recap(cfg, matched, all_videos)

    save_seen(seen_list)
    if STATS["fail"] and not STATS["ok"]:
        # ни одно сообщение не дошло — красный крестик в Actions и письмо от GitHub
        print(f"::error::Telegram не принял ни одного сообщения. Проверь TELEGRAM_CHAT_ID и что бот добавлен в чат/канал.")
        sys.exit(1)
    print(f"::notice::Найдено новых: {len(fresh)}, отправлено: {sent}, видео: {videos_sent}")


# ---------- итоги недели ----------

STATE_PATH = ROOT / "state.json"
WEEK = 7 * 24 * 3600


def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def send_weekly_recap(cfg: dict, articles: list[dict], videos: list[dict]) -> None:
    """Материалы за последние 7 дней, которые ещё не попадали в итоги.
    Так два тихих дня подряд не повторяют одну и ту же подборку."""
    state = load_state()
    recapped = set(state.get("recapped", []))
    now = time.time()
    limit = cfg.get("recap_size", 7)

    week = [i for i in articles + videos if i["ts"] and now - i["ts"] <= WEEK]
    week_links = {i["link"] for i in week}
    pool = [i for i in week if i["link"] not in recapped]
    picks = sorted(pool, key=lambda i: i["ts"], reverse=True)[:limit]

    msk = time.gmtime(now + 3 * 3600)
    date = f"{msk.tm_mday} {MONTHS[msk.tm_mon - 1]}"
    today = time.strftime("%Y-%m-%d", msk)
    if state.get("quiet_day") == today:  # за сегодня уже писали — второй раз не надо
        return

    def remember_quiet_day():
        state["quiet_day"] = today
        STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=0), encoding="utf-8")

    if not picks:
        if send(f"🎨 <b>Дизайн-дайджест · {date}</b>\n\n"
                f"Сегодня новостей о дизайне нет, а главное за неделю уже было. Загляну завтра."):
            remember_quiet_day()
        return

    lines = [
        f"🎨 <b>Дизайн-дайджест · {date}</b>",
        f"Свежих новостей сегодня нет — собрал главное за неделю:",
    ]
    for i in picks:
        source = i.get("source") or i.get("channel", "")
        icon = "🎬 " if "youtube.com" in i["link"] else ""
        lines.append(f'• {icon}<a href="{html.escape(i["link"])}">{html.escape(i["title"])}</a>'
                     f' — {html.escape(source)}')

    if send("\n\n".join(lines[:2]) + "\n\n" + "\n".join(lines[2:])):
        keep = [l for l in state.get("recapped", []) if l in week_links]  # старше недели — забываем
        state["recapped"] = keep + [i["link"] for i in picks]
        remember_quiet_day()
    print(f"::notice::Итоги недели: {len(picks)} материалов")


if __name__ == "__main__":
    main()
