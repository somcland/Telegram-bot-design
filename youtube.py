"""
Проверка новых видео на YouTube-каналах и короткий хайлайт: о чём видео.

Хайлайт собирается так:
  1. Пробуем достать субтитры видео (если YouTube их отдаёт).
  2. Если задан ANTHROPIC_API_KEY — Claude пишет 3–5 пунктов по субтитрам
     и описанию видео.
  3. Если ключа нет или что-то не сработало — берём таймкоды из описания,
     а если их нет — начало описания.
"""

import html
import os
import re
import sys
import time

import feedparser
import requests

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-5-5")

HEADERS = {"User-Agent": "Mozilla/5.0 (design-news-bot)"}
TIMECODE = re.compile(r"^\s*(?:\d{1,2}:)?\d{1,2}:\d{2}\b")


# ---------- лента канала ----------

def fetch_channel(channel: dict) -> list[dict]:
    url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel['channel_id']}"
    try:
        r = requests.get(url, headers=HEADERS, timeout=30)
        r.raise_for_status()
    except Exception as e:
        print(f"[!] YouTube {channel['name']}: {e}", file=sys.stderr)
        return []

    videos = []
    for e in feedparser.parse(r.content).entries:
        vid = e.get("yt_videoid") or e.get("id", "").split(":")[-1]
        if not vid:
            continue
        videos.append({
            "id": vid,
            "channel": channel["name"],
            "title": e.get("title", "").strip(),
            "link": f"https://www.youtube.com/watch?v={vid}",
            "description": (e.get("media_description") or e.get("summary") or "").strip(),
            "ts": time.mktime(e.published_parsed) if e.get("published_parsed") else 0,
        })
    return videos


# ---------- что внутри видео ----------

def get_transcript(video_id: str) -> str:
    """Субтитры видео. С серверов GitHub YouTube иногда их не отдаёт — тогда пусто."""
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
    except ImportError:
        return ""
    try:
        api = YouTubeTranscriptApi()
        if hasattr(api, "fetch"):  # версия 1.x
            parts = api.fetch(video_id, languages=["ru", "en"])
            text = " ".join(p.text for p in parts)
        else:  # старые версии
            parts = YouTubeTranscriptApi.get_transcript(video_id, languages=["ru", "en"])
            text = " ".join(p["text"] for p in parts)
    except Exception as e:
        print(f"[i] субтитры {video_id} недоступны: {type(e).__name__}", file=sys.stderr)
        return ""
    return re.sub(r"\s+", " ", text).strip()


def ai_highlights(title: str, description: str, transcript: str) -> str:
    if not ANTHROPIC_API_KEY:
        return ""
    source = transcript[:40000] if transcript else ""
    prompt = (
        "Ты пишешь хайлайты для Telegram-дайджеста о дизайне. "
        "По материалам ниже напиши 3–5 коротких пунктов на русском: о чём видео "
        "и что из него можно вынести. Каждый пункт начинай с «• », одна строка. "
        "Опирайся только на текст ниже, ничего не придумывай. Без вступления и выводов.\n\n"
        f"Название: {title}\n\nОписание:\n{description[:4000]}\n\n"
        + (f"Субтитры:\n{source}" if source else "Субтитров нет — опирайся на название и описание.")
    )
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": ANTHROPIC_MODEL,
                "max_tokens": 600,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=90,
        )
        r.raise_for_status()
        return "".join(b.get("text", "") for b in r.json().get("content", [])).strip()
    except Exception as e:
        print(f"[!] хайлайт через Claude не получился: {e}", file=sys.stderr)
        return ""


def fallback_highlights(description: str) -> str:
    """Без ИИ: таймкоды из описания, иначе его начало."""
    lines = [l.strip() for l in description.splitlines() if l.strip()]
    chapters = [l for l in lines if TIMECODE.match(l)]
    if len(chapters) >= 2:
        return "\n".join(f"• {c}" for c in chapters[:10])
    text = " ".join(l for l in lines if "http" not in l)
    if len(text) > 400:
        text = text[:397].rsplit(" ", 1)[0] + "…"
    return text


def format_video_message(video: dict) -> str:
    transcript = get_transcript(video["id"])
    body = ai_highlights(video["title"], video["description"], transcript) \
        or fallback_highlights(video["description"])

    parts = [
        f"🎬 <b>Новое видео · {html.escape(video['channel'])}</b>",
        f"<b>{html.escape(video['title'])}</b>",
    ]
    if body:
        parts.append("Что внутри:\n" + html.escape(body))
    parts.append(f'<a href="{video["link"]}">Смотреть на YouTube →</a>')
    return "\n\n".join(parts)
