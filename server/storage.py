"""SQLite: пользователи, источники, подписки, собранные материалы и что кому уже отправлено."""

import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ.get("BOT_DB", ROOT / "data" / "bot.db"))
CATALOG = Path(__file__).resolve().parent / "catalog.json"

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    chat_id    INTEGER PRIMARY KEY,
    name       TEXT,
    created_at REAL,
    active     INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS sources (
    id       TEXT PRIMARY KEY,
    kind     TEXT,            -- youtube | rss | telegram
    name     TEXT,
    url      TEXT,            -- channel_id / адрес RSS / имя канала
    lang     TEXT DEFAULT 'ru',
    filter   INTEGER DEFAULT 0,
    builtin  INTEGER DEFAULT 0,
    is_default INTEGER DEFAULT 0,
    added_by INTEGER
);
CREATE TABLE IF NOT EXISTS subs (
    chat_id   INTEGER,
    source_id TEXT,
    PRIMARY KEY (chat_id, source_id)
);
CREATE TABLE IF NOT EXISTS items (
    link       TEXT PRIMARY KEY,
    source_id  TEXT,
    title      TEXT,
    summary    TEXT,
    extra      TEXT,          -- json: id видео, описание и т.п.
    ts         REAL,
    fetched_at REAL,
    ru_title   TEXT,
    ru_summary TEXT
);
CREATE INDEX IF NOT EXISTS items_src_ts ON items(source_id, ts);
CREATE TABLE IF NOT EXISTS delivered (
    chat_id INTEGER,
    link    TEXT,
    kind    TEXT,             -- digest | recap
    at      REAL,
    PRIMARY KEY (chat_id, link, kind)
);
"""


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


HIGHLIGHT_VERSION = "1-sentence"


def init() -> None:
    with connect() as c:
        c.executescript(SCHEMA)
        # миграции для уже работающей базы
        cols = {r[1] for r in c.execute("PRAGMA table_info(users)")}
        if "hour" not in cols:
            c.execute("ALTER TABLE users ADD COLUMN hour INTEGER DEFAULT 10")
        if "tz" not in cols:
            c.execute("ALTER TABLE users ADD COLUMN tz TEXT DEFAULT 'Europe/Moscow'")
        if "last_sent" not in cols:
            c.execute("ALTER TABLE users ADD COLUMN last_sent TEXT")
        if "images" not in cols:
            c.execute("ALTER TABLE users ADD COLUMN images INTEGER DEFAULT 1")
        if "per_batch" not in cols:
            c.execute("ALTER TABLE users ADD COLUMN per_batch INTEGER DEFAULT 3")
        if "image" not in {r[1] for r in c.execute("PRAGMA table_info(items)")}:
            c.execute("ALTER TABLE items ADD COLUMN image TEXT")
        if "scrape" not in {r[1] for r in c.execute("PRAGMA table_info(sources)")}:
            c.execute("ALTER TABLE sources ADD COLUMN scrape TEXT")
        c.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        row = c.execute("SELECT value FROM meta WHERE key='highlight'").fetchone()
        if not row or row[0] != HIGHLIGHT_VERSION:  # формат хайлайтов поменялся — пересчитать
            c.execute("UPDATE items SET ru_title=NULL, ru_summary=NULL")
            c.execute("INSERT OR REPLACE INTO meta VALUES ('highlight', ?)", (HIGHLIGHT_VERSION,))
        for s in json.loads(CATALOG.read_text(encoding="utf-8"))["sources"]:
            c.execute(
                """INSERT INTO sources (id, kind, name, url, lang, filter, builtin, is_default, scrape)
                   VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET kind=excluded.kind, name=excluded.name,
                     url=excluded.url, lang=excluded.lang, filter=excluded.filter, scrape=excluded.scrape,
                     builtin=1, is_default=excluded.is_default""",
                (s["id"], s["kind"], s["name"], s["url"], s.get("lang", "ru"),
                 int(s.get("filter", False)), int(s.get("default", False)), s.get("scrape")),
            )


# ---------- пользователи и подписки ----------

def add_user(chat_id: int, name: str) -> bool:
    """True, если пользователь новый (ему ставим источники по умолчанию)."""
    with connect() as c:
        row = c.execute("SELECT chat_id FROM users WHERE chat_id=?", (chat_id,)).fetchone()
        if row:
            c.execute("UPDATE users SET active=1, name=? WHERE chat_id=?", (name, chat_id))
            return False
        c.execute("INSERT INTO users (chat_id, name, created_at) VALUES (?, ?, ?)",
                  (chat_id, name, time.time()))
        c.execute("""INSERT OR IGNORE INTO subs (chat_id, source_id)
                     SELECT ?, id FROM sources WHERE is_default=1""", (chat_id,))
        return True


def set_active(chat_id: int, active: bool) -> None:
    with connect() as c:
        c.execute("UPDATE users SET active=? WHERE chat_id=?", (int(active), chat_id))


def active_users() -> list[sqlite3.Row]:
    with connect() as c:
        return c.execute("SELECT * FROM users WHERE active=1").fetchall()


def get_user(chat_id: int) -> sqlite3.Row | None:
    with connect() as c:
        return c.execute("SELECT * FROM users WHERE chat_id=?", (chat_id,)).fetchone()


def set_schedule(chat_id: int, hour: int | None = None, tz: str | None = None) -> None:
    with connect() as c:
        if hour is not None:
            c.execute("UPDATE users SET hour=? WHERE chat_id=?", (hour, chat_id))
        if tz is not None:
            c.execute("UPDATE users SET tz=? WHERE chat_id=?", (tz, chat_id))


def set_display(chat_id: int, images: bool | None = None, per_batch: int | None = None) -> None:
    with connect() as c:
        if images is not None:
            c.execute("UPDATE users SET images=? WHERE chat_id=?", (int(images), chat_id))
        if per_batch is not None:
            c.execute("UPDATE users SET per_batch=? WHERE chat_id=?", (per_batch, chat_id))


def set_last_sent(chat_id: int, day: str) -> None:
    with connect() as c:
        c.execute("UPDATE users SET last_sent=? WHERE chat_id=?", (day, chat_id))


def menu_sources(chat_id: int) -> list[sqlite3.Row]:
    """Что показать в меню: общий каталог + свои источники пользователя."""
    with connect() as c:
        return c.execute(
            """SELECT s.*, (sub.chat_id IS NOT NULL) AS on_
               FROM sources s LEFT JOIN subs sub ON sub.source_id=s.id AND sub.chat_id=?
               WHERE s.builtin=1 OR s.added_by=? OR sub.chat_id IS NOT NULL
               ORDER BY s.builtin DESC, s.rowid""", (chat_id, chat_id)).fetchall()


def toggle(chat_id: int, source_id: str) -> bool:
    with connect() as c:
        if c.execute("SELECT 1 FROM subs WHERE chat_id=? AND source_id=?",
                     (chat_id, source_id)).fetchone():
            c.execute("DELETE FROM subs WHERE chat_id=? AND source_id=?", (chat_id, source_id))
            return False
        c.execute("INSERT INTO subs (chat_id, source_id) VALUES (?, ?)", (chat_id, source_id))
        return True


def user_source_ids(chat_id: int) -> list[str]:
    with connect() as c:
        return [r[0] for r in c.execute("SELECT source_id FROM subs WHERE chat_id=?", (chat_id,))]


def add_custom_source(chat_id: int, kind: str, name: str, url: str, lang: str) -> str:
    sid = f"{kind}-" + hashlib.sha1(url.lower().encode()).hexdigest()[:10]
    with connect() as c:
        c.execute("""INSERT OR IGNORE INTO sources (id, kind, name, url, lang, added_by)
                     VALUES (?, ?, ?, ?, ?, ?)""", (sid, kind, name, url, lang, chat_id))
        c.execute("INSERT OR IGNORE INTO subs (chat_id, source_id) VALUES (?, ?)", (chat_id, sid))
    return sid


def subscribed_sources() -> list[sqlite3.Row]:
    """Источники, на которые подписан хоть один активный пользователь."""
    with connect() as c:
        return c.execute("""SELECT DISTINCT s.* FROM sources s
                            JOIN subs ON subs.source_id=s.id
                            JOIN users u ON u.chat_id=subs.chat_id AND u.active=1""").fetchall()


def sources_by_ids(ids: list[str]) -> list[sqlite3.Row]:
    if not ids:
        return []
    with connect() as c:
        q = ",".join("?" * len(ids))
        return c.execute(f"SELECT * FROM sources WHERE id IN ({q})", ids).fetchall()


# ---------- материалы ----------

def known_links(source_id: str) -> set[str]:
    with connect() as c:
        return {r[0] for r in c.execute("SELECT link FROM items WHERE source_id=?", (source_id,))}


def save_items(source_id: str, items: list[dict]) -> int:
    now = time.time()
    added = 0
    with connect() as c:
        for i in items:
            cur = c.execute(
                """INSERT OR IGNORE INTO items (link, source_id, title, summary, extra, ts, fetched_at, image)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (i["link"], source_id, i["title"], i.get("summary", ""),
                 json.dumps(i.get("extra", {}), ensure_ascii=False), i.get("ts") or now, now,
                 i.get("image")))
            added += cur.rowcount
            if not cur.rowcount and i.get("image"):  # уже был, но без картинки — дописать
                c.execute("UPDATE items SET image=? WHERE link=? AND (image IS NULL OR image='')",
                          (i["image"], i["link"]))
    return added


def set_translation(link: str, ru_title: str, ru_summary: str) -> None:
    with connect() as c:
        c.execute("UPDATE items SET ru_title=?, ru_summary=? WHERE link=?", (ru_title, ru_summary, link))


def items_for_user(chat_id: int, since: float, kind: str, limit: int) -> list[sqlite3.Row]:
    """Материалы из источников пользователя новее since, которые ему ещё не слали (в этом kind)."""
    with connect() as c:
        return c.execute(
            """SELECT i.*, s.name AS source_name, s.kind AS source_kind, s.lang AS lang
               FROM items i
               JOIN subs ON subs.source_id=i.source_id AND subs.chat_id=?
               JOIN sources s ON s.id=i.source_id
               WHERE i.ts >= ?
                 AND NOT EXISTS (SELECT 1 FROM delivered d
                                 WHERE d.chat_id=? AND d.link=i.link AND d.kind=?)
               ORDER BY i.ts DESC LIMIT ?""", (chat_id, since, chat_id, kind, limit)).fetchall()


def count_for_user(chat_id: int, since: float, kind: str) -> int:
    with connect() as c:
        return c.execute(
            """SELECT COUNT(*) FROM items i
               JOIN subs ON subs.source_id=i.source_id AND subs.chat_id=?
               WHERE i.ts >= ?
                 AND NOT EXISTS (SELECT 1 FROM delivered d
                                 WHERE d.chat_id=? AND d.link=i.link AND d.kind=?)""",
            (chat_id, since, chat_id, kind)).fetchone()[0]


def set_image(link: str, image: str) -> None:
    with connect() as c:
        c.execute("UPDATE items SET image=? WHERE link=?", (image, link))


def latest_for_user(chat_id: int, limit: int) -> list[sqlite3.Row]:
    with connect() as c:
        return c.execute(
            """SELECT i.*, s.name AS source_name, s.kind AS source_kind, s.lang AS lang
               FROM items i
               JOIN subs ON subs.source_id=i.source_id AND subs.chat_id=?
               JOIN sources s ON s.id=i.source_id
               ORDER BY i.ts DESC LIMIT ?""", (chat_id, limit)).fetchall()


def mark_delivered(chat_id: int, links: list[str], kind: str) -> None:
    now = time.time()
    with connect() as c:
        c.executemany("INSERT OR IGNORE INTO delivered (chat_id, link, kind, at) VALUES (?, ?, ?, ?)",
                      [(chat_id, l, kind, now) for l in links])


def cleanup(days: int = 60) -> None:
    edge = time.time() - days * 86400
    with connect() as c:
        c.execute("DELETE FROM items WHERE fetched_at < ?", (edge,))
        c.execute("DELETE FROM delivered WHERE at < ?", (edge,))


def stats() -> dict:
    with connect() as c:
        return {
            "users": c.execute("SELECT COUNT(*) FROM users WHERE active=1").fetchone()[0],
            "sources": c.execute("SELECT COUNT(*) FROM sources").fetchone()[0],
            "items": c.execute("SELECT COUNT(*) FROM items").fetchone()[0],
        }
