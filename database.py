import os
import aiosqlite
from datetime import datetime, timezone

DB_PATH = os.getenv("DB_PATH", "data/oneai.db")

async def init_db():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            is_admin INTEGER NOT NULL DEFAULT 0,
            is_banned INTEGER NOT NULL DEFAULT 0,
            messages INTEGER NOT NULL DEFAULT 0,
            images INTEGER NOT NULL DEFAULT 0,
            searches INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            last_seen TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            content TEXT,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_messages_user ON messages(user_id);
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        """)
        await db.commit()

def now():
    return datetime.now(timezone.utc).isoformat()

async def upsert_user(user):
    ts = now()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        INSERT INTO users(user_id, username, first_name, last_name, created_at, last_seen)
        VALUES(?,?,?,?,?,?)
        ON CONFLICT(user_id) DO UPDATE SET
          username=excluded.username, first_name=excluded.first_name,
          last_name=excluded.last_name, last_seen=excluded.last_seen
        """, (user.id, user.username, user.first_name, user.last_name, ts, ts))
        await db.commit()

async def get_user(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
        row = await cur.fetchone()
        return dict(row) if row else None

async def is_banned(user_id):
    row = await get_user(user_id)
    return bool(row and row["is_banned"])

async def set_banned(user_id, value):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET is_banned=? WHERE user_id=?", (1 if value else 0, user_id))
        await db.commit()

async def set_admin(user_id, value=True):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        INSERT INTO users(user_id, is_admin, created_at, last_seen)
        VALUES(?,?,?,?)
        ON CONFLICT(user_id) DO UPDATE SET is_admin=excluded.is_admin
        """, (user_id, 1 if value else 0, now(), now()))
        await db.commit()

async def add_message(user_id, role, content):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO messages(user_id, role, content, created_at) VALUES(?,?,?,?)",
            (user_id, role, str(content)[:20000], now())
        )
        await db.commit()

async def increment_stats(user_id, messages=0, images=0, searches=0):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET messages=messages+?, images=images+?, searches=searches+?, last_seen=? WHERE user_id=?",
            (messages, images, searches, now(), user_id)
        )
        await db.commit()

async def stats():
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT COUNT(*), COALESCE(SUM(messages),0), COALESCE(SUM(images),0), COALESCE(SUM(searches),0), SUM(is_banned) FROM users")
        users, messages, images, searches, banned = await cur.fetchone()
        cur = await db.execute("SELECT COUNT(*) FROM users WHERE last_seen >= datetime('now','-1 day')")
        active_24h = (await cur.fetchone())[0]
        return dict(users=users, messages=messages, images=images, searches=searches, banned=banned or 0, active_24h=active_24h)

async def list_users(offset=0, limit=8):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM users ORDER BY last_seen DESC LIMIT ? OFFSET ?", (limit, offset))
        return [dict(x) for x in await cur.fetchall()]

async def user_count():
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT COUNT(*) FROM users")
        return (await cur.fetchone())[0]

async def get_all_user_ids():
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT user_id FROM users WHERE is_banned=0")
        return [r[0] for r in await cur.fetchall()]
