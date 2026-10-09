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
        print(f"[!] {feed['name']}: {e}", file=sys.stderr)
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
            return True
        print(f"[!] Telegram {r.status_code}: {r.text}", file=sys.stderr)
        return False
    return False


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

    # собираем всё и отбираем новое про дизайн
    fresh = []
    for feed in cfg["feeds"]:
        for item in fetch_feed(feed):
            if item["link"] in seen:
                continue
            text = f'{item["title"]} {item["summary"]} {item["tags"]}'
            if is_stop(text):
                continue
            if item["filter"] and not is_design(text):
                continue
            fresh.append(item)

    # без дублей между лентами, от старых к новым
    uniq = {i["link"]: i for i in fresh}
    fresh = sorted(uniq.values(), key=lambda i: i["ts"])

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

    sent = 0
    for item in to_send:
        if send(format_message(item)):
            seen_list.append(item["link"])
            sent += 1
            time.sleep(1.2)  # бережём лимиты Telegram

    save_seen(seen_list)
    print(f"Найдено новых: {len(fresh)}, отправлено: {sent}")


if __name__ == "__main__":
    main()
