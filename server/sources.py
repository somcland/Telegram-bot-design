"""Сбор материалов из YouTube, RSS и публичных Telegram-каналов + разбор ссылок «добавить свой»."""

import calendar
import hashlib
import html
import threading
import time
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


_local = threading.local()


def http() -> requests.Session:
    """Своя сессия на поток: соединения с сайтами переиспользуются, без повторных рукопожатий."""
    if not hasattr(_local, "s"):
        _local.s = requests.Session()
        adapter = requests.adapters.HTTPAdapter(pool_connections=20, pool_maxsize=20)
        _local.s.mount("https://", adapter)
        _local.s.mount("http://", adapter)
    return _local.s


def _get(url: str, **kw) -> requests.Response:
    r = http().get(url, headers=HEADERS, timeout=30, **kw)
    r.raise_for_status()
    return r


def short(text: str, n: int) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= n else text[: n - 1].rsplit(" ", 1)[0] + "…"


# ---------- сбор ----------

LAST_ERROR: dict[str, str] = {}  # id источника → последняя ошибка сбора (для админа)


def fetch(source) -> list[dict]:
    kind = source["kind"]
    LAST_ERROR.pop(source["id"], None)
    try:
        if kind == "youtube":
            return fetch_youtube(source)
        if kind == "telegram":
            return fetch_telegram(source)
        if "scrape" in source.keys() and source["scrape"]:
            from . import storage
            return fetch_page(source, storage.known_links(source["id"]))
        return fetch_rss(source)
    except Exception as e:
        print(f"[!] {source['name']}: {e}", file=sys.stderr)
        LAST_ERROR[source["id"]] = f"{type(e).__name__}: {e}"[:200]
        return []


def fetch_page(source, known: set[str], max_new: int = 10) -> list[dict]:
    """Сайт без RSS: ссылки на статьи по шаблону адреса, анонс — из описания страницы статьи."""
    page = _get(source["url"])
    soup = BeautifulSoup(page.text, "html.parser")
    rx = re.compile(source["scrape"])
    found: dict[str, str] = {}
    for a in soup.find_all("a", href=True):
        link = clean_url(urljoin(page.url, a["href"]))
        if rx.match(link):
            text = a.get_text(" ", strip=True)
            if len(text) > len(found.get(link, "")):
                found[link] = text
    items, fetched = [], 0
    for link, title in found.items():
        item = {"link": link, "title": short(clean_title(title, source["name"]), 160), "summary": "", "ts": 0}
        if link not in known and fetched < max_new:  # анонс берём только у новых статей
            fetched += 1
            try:
                art = BeautifulSoup(_get(link).text, "html.parser")
                og = art.find("meta", property="og:title")
                desc = art.find("meta", property="og:description") or art.find("meta", attrs={"name": "description"})
                if og and og.get("content"):
                    item["title"] = short(clean_title(og["content"], source["name"]), 160)
                item["image"] = og_image(art, link)
                item["summary"] = short(desc.get("content", "") if desc else "", 400)
                pub = art.find("meta", property="article:published_time")
                if pub and pub.get("content"):
                    from datetime import datetime
                    item["ts"] = datetime.fromisoformat(pub["content"].replace("Z", "+00:00")).timestamp()
            except Exception:
                pass
        elif link not in known:
            continue  # остальные новые подтянем в следующий раз
        if item["title"]:
            items.append(item)
    return items


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
                      "summary": short(summary, 400), "ts": _ts(e), "image": rss_image(e)})
    return items


IMG_EXT = re.compile(r"\.(jpe?g|png|webp|gif)(\?|$)", re.I)


def _abs(url: str, base: str) -> str:
    url = (url or "").strip()
    return urljoin(base, url) if url and not url.startswith("data:") else ""


def rss_image(e) -> str:
    """Картинка материала из ленты: media:content, media:thumbnail, вложение или первая <img>."""
    base = e.get("link", "")
    for m in e.get("media_content", []) or []:
        if m.get("url") and (m.get("medium") == "image" or "image" in (m.get("type") or "")
                             or IMG_EXT.search(m["url"])):
            return _abs(m["url"], base)
    for m in e.get("media_thumbnail", []) or []:
        if m.get("url"):
            return _abs(m["url"], base)
    for l in e.get("links", []) or []:
        if l.get("rel") == "enclosure" and "image" in (l.get("type") or ""):
            return _abs(l.get("href"), base)
    raw = " ".join([e.get("summary", "")] + [c.get("value", "") for c in e.get("content", []) or []])
    m = re.search(r"<img[^>]+src=[\"']([^\"']+)", raw, re.I)
    return _abs(m.group(1), base) if m else ""


def og_image(soup, base: str) -> str:
    tag = (soup.find("meta", property="og:image") or soup.find("meta", attrs={"name": "twitter:image"})
           or soup.find("meta", property="twitter:image"))
    return _abs(tag.get("content", ""), base) if tag else ""


IMG_CACHE = os.path.join(ROOT, "data", "img")


def download_image(url: str, referer: str = "") -> bytes | None:
    """Скачиваем картинку сами: Telegram часто не может забрать её по ссылке.
    Скачанное лежит на диске — каждая картинка качается один раз, а не для каждого подписчика."""
    os.makedirs(IMG_CACHE, exist_ok=True)
    path = os.path.join(IMG_CACHE, hashlib.sha1(url.encode()).hexdigest())
    if os.path.exists(path):
        data = open(path, "rb").read()
        return data or None  # пустой файл = «картинки нет», не пробуем снова
    data = None
    try:
        r = http().get(url, headers={**HEADERS, "Referer": referer or url}, timeout=20)
        r.raise_for_status()
        if r.headers.get("content-type", "").startswith("image/") and 1000 < len(r.content) < 9_000_000:
            data = r.content
    except Exception:
        pass
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data or b"")
    os.replace(tmp, path)
    return data


def cleanup_images(days: int = 14) -> None:
    edge = time.time() - days * 86400
    if not os.path.isdir(IMG_CACHE):
        return
    for name in os.listdir(IMG_CACHE):
        p = os.path.join(IMG_CACHE, name)
        try:
            if os.path.getmtime(p) < edge:
                os.remove(p)
        except OSError:
            pass


def page_image(link: str) -> str:
    """Картинка со страницы статьи (og:image) — если в ленте её не было."""
    try:
        r = http().get(link, headers=HEADERS, timeout=10)
        r.raise_for_status()
        return og_image(BeautifulSoup(r.text, "html.parser"), r.url)
    except Exception:
        return ""


def fetch_youtube(source) -> list[dict]:
    videos = youtube.fetch_channel({"name": source["name"], "channel_id": source["url"]})
    return [{"link": v["link"], "title": v["title"], "summary": "",
             "ts": v["ts"], "image": f"https://i.ytimg.com/vi/{v['id']}/maxresdefault.jpg",
             "extra": {"id": v["id"], "description": v["description"][:5000]}}
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
        image = ""
        wrap = msg.select_one(".tgme_widget_message_photo_wrap, .tgme_widget_message_video_thumb")
        if wrap:
            m = re.search(r"url\(['\"]?([^'\")]+)", wrap.get("style", ""))
            image = m.group(1) if m else ""
        items.append({"link": f"https://t.me/{msg['data-post']}", "title": short(lines[0], 120),
                      "summary": short(" ".join(lines[1:]), 400), "ts": ts, "image": image})
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

LAST_AI_ERROR: dict[str, str] = {}


def _count_usage(provider: str, tokens_in: int, tokens_out: int) -> None:
    try:
        from . import storage
        storage.add_ai_usage(provider, int(tokens_in or 0), int(tokens_out or 0))
    except Exception as e:
        print(f"[!] учёт токенов: {e}", file=sys.stderr)
GEMINI_PAUSED_UNTIL = [0.0]  # после «квота исчерпана» не долбим API впустую
QUOTA_PAUSE = 3600


def _gemini(prompt: str, max_tokens: int = 500) -> str:
    """Gemini Flash: generateContent, а если его нет — новый Interactions API."""
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key or time.time() < GEMINI_PAUSED_UNTIL[0]:
        return ""
    model = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")
    headers = {"x-goog-api-key": key, "content-type": "application/json"}
    base = "https://generativelanguage.googleapis.com/v1beta"
    try:
        body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {"maxOutputTokens": max_tokens * 4, "temperature": 0.3}}
        for attempt in range(4):
            r = http().post(f"{base}/models/{model}:generateContent", headers=headers, timeout=120, json=body)
            if r.status_code == 429 and ("quota" in r.text.lower() and "per minute" not in r.text.lower()):
                # дневная квота или нет оплаты — повторы только сожгут запросы; пауза на час
                GEMINI_PAUSED_UNTIL[0] = time.time() + QUOTA_PAUSE
                break
            if r.status_code in (429, 500, 503) and attempt < 3:  # лимит в минуту или перегрузка — ждём
                time.sleep(min(60, 8 * (attempt + 1)))
                continue
            break
        if r.status_code in (404, 405):  # старый метод недоступен — пробуем новый
            r = http().post(f"{base}/interactions", headers=headers, timeout=120, json={
                "model": model, "input": prompt, "store": False})
        if r.status_code >= 400:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")
        data = r.json()
        um = data.get("usageMetadata") or data.get("usage") or {}
        _count_usage("gemini", um.get("promptTokenCount") or um.get("input_tokens") or 0,
                     (um.get("candidatesTokenCount") or 0) + (um.get("thoughtsTokenCount") or 0)
                     or um.get("output_tokens") or 0)
        parts = [p.get("text", "") for c in data.get("candidates", [])
                 for p in c.get("content", {}).get("parts", []) if not p.get("thought")]
        if not parts:  # ответ Interactions API
            parts = [c.get("text", "") for s in data.get("steps", []) for c in s.get("content", [])
                     if isinstance(c, dict)] or [data.get("output_text", "")]
        return "".join(parts).strip()
    except Exception as e:
        LAST_AI_ERROR["gemini"] = str(e)[:400]
        print(f"[!] Gemini: {e}", file=sys.stderr)
        return ""


def llm(prompt: str, max_tokens: int = 500) -> str:
    """Переводы и хайлайты: сначала Gemini Flash, если не вышло — Claude."""
    return _gemini(prompt, max_tokens) or _claude(prompt, max_tokens)


def has_llm() -> bool:
    return bool(os.environ.get("GEMINI_API_KEY", "").strip() or os.environ.get("ANTHROPIC_API_KEY", "").strip())


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
        data = r.json()
        u = data.get("usage") or {}
        _count_usage("claude", u.get("input_tokens", 0), u.get("output_tokens", 0))
        return "".join(b.get("text", "") for b in data.get("content", [])).strip()
    except Exception as e:
        print(f"[!] Claude: {e}", file=sys.stderr)
        return ""


_TITLE_SEP = re.compile(r"\s+[—|–-]\s+")


def clean_title(title: str, source_name: str = "") -> str:
    """Убираем из заголовка название сайта, которое он дописывает в og:title."""
    title = (title or "").strip()
    name = re.sub(r"[^\w\s']", "", source_name or "").strip().lower()
    parts = _TITLE_SEP.split(title)
    if len(parts) > 1 and name:
        key = name.split()[0] if name.split()[0] not in ("the", "a") else " ".join(name.split()[:2])
        for n in range(1, len(parts)):
            tail = " ".join(parts[n:]).lower()
            if key and re.search(rf"\b{re.escape(key)}\b", tail):
                return " — ".join(parts[:n]).strip() if n > 1 else parts[0].strip()
    return title


SENTENCE = re.compile(r"(?<=[.!?…])\s+")


def few_sentences(text: str, max_sentences: int = 1, max_len: int = 200) -> str:
    """Без ИИ: первое предложение текста."""
    text = re.sub(r"\s+", " ", text or "").strip()
    out = ""
    for i, sent in enumerate(SENTENCE.split(text)):
        if i >= max_sentences or (out and len(out) + len(sent) > max_len):
            break
        out = f"{out} {sent}".strip()
    return out or short(text, max_len)


def _json_from(text: str, want=None):
    """Первый корректный JSON в ответе модели (в ```json-блоке, с текстом вокруг — неважно)."""
    if not text:
        return None
    dec = json.JSONDecoder()
    for m in re.finditer(r"[\[{]", text):
        try:
            obj, _ = dec.raw_decode(text[m.start():])
        except json.JSONDecodeError:
            continue
        if want is None or isinstance(obj, want):
            return obj
    return None


HIGHLIGHT_RULES = (
    "Напиши хайлайт на русском одним коротким предложением (до 20 слов): главное, что есть "
    "в материале для дизайнера. Опирайся только на данный текст, ничего не придумывай, без вводных "
    "фраз вроде «В статье рассказывается». Заголовок переведи на русский, если он на другом языке, "
    "иначе оставь как есть."
)


def highlight_texts(items: list[dict]) -> dict[str, tuple[str, str]]:
    """Статьи и посты пачкой: link → (заголовок на русском, хайлайт)."""
    result = {}
    for start in range(0, len(items), 8):
        batch = items[start:start + 8]
        payload = [{"id": n, "title": clean_title(i["title"], i.get("source_name", "")),
                    "text": (i.get("summary") or "")[:1200]} for n, i in enumerate(batch)]
        raw = llm(HIGHLIGHT_RULES + "\n\nДля каждого материала ниже ответь строго JSON-массивом "
                  '[{"id": 0, "title": "...", "highlight": "..."}] без пояснений.\n\n'
                  + json.dumps(payload, ensure_ascii=False), max_tokens=2500)
        data = _json_from(raw, list) or []
        for d in data:
            try:
                item = batch[int(d["id"])]
            except (KeyError, ValueError, IndexError, TypeError):
                continue
            if isinstance(d, dict) and d.get("highlight"):
                result[item["link"]] = (d.get("title") or item["title"], str(d["highlight"]).strip())
        missed = [i for i in batch if i["link"] not in result]
        if missed and raw:
            LAST_AI_ERROR["batch"] = f"разобрано {len(batch) - len(missed)} из {len(batch)}; ответ: {raw[:300]}"
        for i in missed if raw else []:  # модель ответила, но не всё разобралось — по одному
            one = _json_from(llm(
                HIGHLIGHT_RULES + ' Ответь строго JSON: {"title": "...", "highlight": "..."}.\n\n'
                f"Заголовок: {clean_title(i['title'], i.get('source_name', ''))}\n\n"
                f"Текст: {(i.get('summary') or '')[:1200]}", max_tokens=400), dict)
            if one and one.get("highlight"):
                result[i["link"]] = (one.get("title") or i["title"], str(one["highlight"]).strip())
    return result


def highlight_video(item) -> tuple[str, str, bool]:
    """Видео по субтитрам и описанию: (заголовок на русском, хайлайт, сделано ли ИИ)."""
    extra = json.loads(item["extra"] or "{}")
    desc = extra.get("description", "")
    transcript = youtube.get_transcript(extra["id"])[:40000] if extra.get("id") else ""
    data = _json_from(llm(
        HIGHLIGHT_RULES + ' Ответь строго JSON: {"title": "...", "highlight": "..."}.\n\n'
        f"Название видео: {item['title']}\n\nОписание:\n{desc[:4000]}\n\n"
        + (f"Субтитры:\n{transcript}" if transcript else "Субтитров нет."), max_tokens=600))
    if isinstance(data, dict) and data.get("highlight"):
        return data.get("title") or item["title"], data["highlight"].strip(), True
    # без ИИ: описание без ссылок и таймкодов
    lines = [l for l in desc.splitlines()
             if l.strip() and "http" not in l and not youtube.TIMECODE.match(l)]
    return item["title"], few_sentences(" ".join(lines)), False


def ai_check(sample=None) -> str:
    """Диагностика перевода для админа: ключи, модель и пробный запрос."""
    def mask(v):
        v = v.strip()
        return f"есть ({v[:4]}…{v[-3:]}, {len(v)} симв.)" if v else "нет"
    g = os.environ.get("GEMINI_API_KEY", "")
    lines = [f"GEMINI_API_KEY: {mask(g)}",
             f"Модель: {os.environ.get('GEMINI_MODEL', 'gemini-3.8-flash')}",
             f"ANTHROPIC_API_KEY: {mask(os.environ.get('ANTHROPIC_API_KEY', ''))}"]
    if time.time() < GEMINI_PAUSED_UNTIL[0]:
        left = int((GEMINI_PAUSED_UNTIL[0] - time.time()) / 60)
        lines.append(f"Gemini на паузе ещё {left} мин после ошибки квоты — проверяю всё равно")
        GEMINI_PAUSED_UNTIL[0] = 0
    if g.strip():
        LAST_AI_ERROR.pop("gemini", None)
        out = _gemini("Переведи на русский одной строкой: Native page transitions on the web", 100)
        lines.append(f"Ответ Gemini: {out!r}" if out else f"Ошибка Gemini: {LAST_AI_ERROR.get('gemini', 'пустой ответ')}")
        if out and sample:  # как в настоящей подборке: пачка английских материалов
            LAST_AI_ERROR.pop("batch", None)
            done = highlight_texts([dict(i) for i in sample])
            lines.append(f"\nПачка из {len(sample)} англ. материалов: переведено {len(done)}")
            for i in sample[:3]:
                t = done.get(i["link"])
                lines.append(f"• {i['title'][:50]} → {t[0][:60] if t else '—'}")
            if "batch" in LAST_AI_ERROR:
                lines.append(f"Проблема: {LAST_AI_ERROR['batch']}")
            if "gemini" in LAST_AI_ERROR:
                lines.append(f"Ошибка Gemini: {LAST_AI_ERROR['gemini']}")
    return "\n".join(lines)
