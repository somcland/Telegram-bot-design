"""Сбор материалов из YouTube, RSS и публичных Telegram-каналов + разбор ссылок «добавить свой»."""

import calendar
import html
import json
import os
import re
import sys
from urllib.parse import urljoin, urlsplit

import feedparser
import requests
from bs4 import BeautifulSoup

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import youtube  # noqa: E402  (хайлайты видео из основной версии бота)
from bot import build_matcher, clean_url, load_config, strip_html  # noqa: E402

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/130.0 Safari/537.36",
    "Accept-Language": "ru,en;q=0.8",
}
# без этих кук YouTube в Европе показывает страницу согласия вместо канала
YT_COOKIES = {"CONSENT": "YES+cb", "SOCS": "CAI"}

_cfg = load_config()
is_design = build_matcher(_cfg.get("keywords", []))
is_stop = build_matcher(_cfg.get("stop_words", []))


def _ts(entry) -> float:
    t = entry.get("published_parsed") or entry.get("updated_parsed")
    return float(calendar.timegm(t)) if t else 0.0


def _get(url: str, **kw) -> requests.Response:
    r = requests.get(url, headers=HEADERS, timeout=30, **kw)
    r.raise_for_status()
    return r


def short(text: str, n: int) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= n else text[: n - 1].rsplit(" ", 1)[0] + "…"


# ---------- сбор ----------

def fetch(source) -> list[dict]:
    kind = source["kind"]
    try:
        if kind == "youtube":
            return fetch_youtube(source)
        if kind == "telegram":
            return fetch_telegram(source)
        return fetch_rss(source)
    except Exception as e:
        print(f"[!] {source['name']}: {e}", file=sys.stderr)
        return []


def fetch_rss(source) -> list[dict]:
    feed = feedparser.parse(_get(source["url"]).content)
    items = []
    for e in feed.entries:
        if not e.get("link"):
            continue
        title, summary = strip_html(e.get("title", "")), strip_html(e.get("summary", ""))
        tags = " ".join(t.get("term", "") for t in e.get("tags", []))
        text = f"{title} {summary} {tags}"
        if is_stop(text) or (source["filter"] and not is_design(text)):
            continue
        items.append({"link": clean_url(e["link"]), "title": title,
                      "summary": short(summary, 400), "ts": _ts(e)})
    return items


def fetch_youtube(source) -> list[dict]:
    videos = youtube.fetch_channel({"name": source["name"], "channel_id": source["url"]})
    return [{"link": v["link"], "title": v["title"], "summary": "",
             "ts": v["ts"], "extra": {"id": v["id"], "description": v["description"][:5000]}}
            for v in videos]


def fetch_telegram(source) -> list[dict]:
    soup = BeautifulSoup(_get(f"https://t.me/s/{source['url']}").text, "html.parser")
    items = []
    for msg in soup.select(".tgme_widget_message[data-post]"):
        body = msg.select_one(".tgme_widget_message_text")
        if not body:
            continue  # только картинка или опрос — пропускаем
        text = body.get_text("\n").strip()
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        if not lines:
            continue
        t = msg.select_one(".tgme_widget_message_date time[datetime]")
        ts = 0.0
        if t:
            from datetime import datetime
            ts = datetime.fromisoformat(t["datetime"]).timestamp()
        items.append({"link": f"https://t.me/{msg['data-post']}", "title": short(lines[0], 120),
                      "summary": short(" ".join(lines[1:]), 400), "ts": ts})
    return items


# ---------- «добавить свой»: ссылка → источник ----------

class NotFound(Exception):
    pass


def resolve_youtube(text: str) -> tuple[str, str]:
    """→ (channel_id, название)."""
    text = text.strip()
    m = re.search(r"(UC[\w-]{22})", text)
    if m and "watch" not in text:
        cid = m.group(1)
    else:
        if text.startswith("@"):
            url = f"https://www.youtube.com/{text}"
        elif "youtube.com" in text or "youtu.be" in text:
            url = text if text.startswith("http") else "https://" + text
        else:
            url = f"https://www.youtube.com/@{text.lstrip('@')}"
        page = _get(url, cookies=YT_COOKIES).text
        m = (re.search(r'"externalId":"(UC[\w-]{22})"', page)
             or re.search(r'<meta itemprop="channelId" content="(UC[\w-]{22})"', page)
             or re.search(r'channel_id=(UC[\w-]{22})', page)
             or re.search(r'"channelId":"(UC[\w-]{22})"', page))
        if not m:
            raise NotFound("Не нашёл канал по этой ссылке.")
        cid = m.group(1)
    try:
        feed = feedparser.parse(_get(f"https://www.youtube.com/feeds/videos.xml?channel_id={cid}").content)
    except Exception:
        raise NotFound("Канал нашёл, но YouTube не отдаёт список его видео.")
    return cid, short(feed.feed.get("title") or cid, 40)


def resolve_rss(text: str) -> tuple[str, str, str]:
    """→ (адрес ленты, название, язык). Принимает и RSS, и адрес сайта."""
    url = text.strip()
    if not url.startswith("http"):
        url = "https://" + url
    r = _get(url)
    candidates = []
    feed = feedparser.parse(r.content)
    if feed.entries:
        candidates = [url]
    else:
        soup = BeautifulSoup(r.text, "html.parser")
        for link in soup.find_all("link", rel="alternate"):
            if "rss" in (link.get("type") or "") or "atom" in (link.get("type") or ""):
                candidates.append(urljoin(r.url, link.get("href", "")))
        base = f"{urlsplit(r.url).scheme}://{urlsplit(r.url).netloc}"
        candidates += [base + p for p in ("/feed", "/rss", "/feed.xml", "/rss.xml", "/atom.xml", "/feed/")]
    for c in candidates:
        try:
            f = feed if c == url and feed.entries else feedparser.parse(_get(c).content)
        except Exception:
            continue
        if f.entries:
            titles = " ".join(e.get("title", "") for e in f.entries[:10])
            lang = "ru" if re.search(r"[а-яё]", titles, re.I) else "en"
            name = strip_html(f.feed.get("title", "")) or urlsplit(c).netloc
            return c, short(name, 40), lang
    raise NotFound("У этого сайта не нашёл RSS-ленты, без неё подключить не получится.")


def resolve_telegram(text: str) -> tuple[str, str]:
    """→ (имя канала, название)."""
    t = text.strip()
    m = re.search(r"(?:t\.me/|telegram\.me/)(?:s/)?([A-Za-z0-9_]{4,})", t) or re.match(r"@?([A-Za-z0-9_]{4,})$", t)
    if not m:
        raise NotFound("Пришли ссылку вида t.me/канал или @канал.")
    handle = m.group(1)
    page = _get(f"https://t.me/s/{handle}").text
    if "tgme_widget_message" not in page:
        raise NotFound("Это не публичный канал (или он пустой) — такие бот читать не может.")
    og = re.search(r'<meta property="og:title" content="([^"]+)"', page)
    return handle, short(html.unescape(og.group(1)) if og else handle, 40)


# ---------- перевод и хайлайты ----------

def _claude(prompt: str, max_tokens: int = 500) -> str:
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        return ""
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": os.environ.get("ANTHROPIC_MODEL", "claude-haiku-5-5"),
                  "max_tokens": max_tokens, "messages": [{"role": "user", "content": prompt}]},
            timeout=90)
        r.raise_for_status()
        return "".join(b.get("text", "") for b in r.json().get("content", [])).strip()
    except Exception as e:
        print(f"[!] Claude: {e}", file=sys.stderr)
        return ""


def translate(title: str, summary: str) -> tuple[str, str] | None:
    out = _claude(
        "Переведи на русский заголовок и перескажи анонс статьи о дизайне в 2–3 предложениях. "
        "Ответь строго JSON без пояснений: {\"title\": \"...\", \"summary\": \"...\"}.\n\n"
        f"Title: {title}\n\nAnnouncement: {summary[:3000]}")
    m = re.search(r"\{.*\}", out, re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
        return d.get("title") or title, d.get("summary") or ""
    except json.JSONDecodeError:
        return None


def video_highlights(item) -> str:
    extra = json.loads(item["extra"] or "{}")
    transcript = youtube.get_transcript(extra.get("id", "")) if extra.get("id") else ""
    desc = extra.get("description", "")
    return youtube.ai_highlights(item["title"], desc, transcript) or youtube.fallback_highlights(desc)
