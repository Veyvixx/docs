"""
Async SQLite layer with TTL cache, write batching, and connection pooling.
"""

import aiosqlite
import asyncio
import json
import logging
import time
from typing import Any, Optional

from utils.cache import (
    cache,
    guild_config_key,
    whitelist_key,
    warnings_key,
)

logger = logging.getLogger(__name__)

DB_PATH = "bot.db"

TTL_GUILD_CONFIG = 300
TTL_WHITELIST    = 120
TTL_WARNINGS     = 30


class _ConnPool:
    def __init__(self, path: str, size: int = 4) -> None:
        self._path = path
        self._size = size
        self._pool: asyncio.Queue = asyncio.Queue(maxsize=size)
        self._write_conn: Optional[aiosqlite.Connection] = None
        self._write_lock = asyncio.Lock()

    async def init(self) -> None:
        for _ in range(self._size):
            conn = await aiosqlite.connect(self._path)
            conn.row_factory = aiosqlite.Row
            await conn.execute("PRAGMA journal_mode=WAL")
            await conn.execute("PRAGMA synchronous=NORMAL")
            await conn.execute("PRAGMA foreign_keys=ON")
            await conn.execute("PRAGMA cache_size=-32000")
            await self._pool.put(conn)

        self._write_conn = await aiosqlite.connect(self._path)
        self._write_conn.row_factory = aiosqlite.Row
        await self._write_conn.execute("PRAGMA journal_mode=WAL")
        await self._write_conn.execute("PRAGMA synchronous=NORMAL")
        await self._write_conn.execute("PRAGMA foreign_keys=ON")
        logger.info("DB pool initialised (%d read + 1 write connections)", self._size)

    async def close(self) -> None:
        while not self._pool.empty():
            conn = await self._pool.get()
            await conn.close()
        if self._write_conn:
            await self._write_conn.close()

    class _ReadCtx:
        def __init__(self, pool: "asyncio.Queue"):
            self._pool = pool
            self._conn: Optional[aiosqlite.Connection] = None

        async def __aenter__(self) -> aiosqlite.Connection:
            self._conn = await self._pool.get()
            return self._conn

        async def __aexit__(self, *_):
            await self._pool.put(self._conn)

    def read(self) -> "_ReadCtx":
        return self._ReadCtx(self._pool)

    async def write(self, sql: str, params: tuple = ()) -> aiosqlite.Cursor:
        async with self._write_lock:
            cur = await self._write_conn.execute(sql, params)  # type: ignore[union-attr]
            await self._write_conn.commit()  # type: ignore[union-attr]
            return cur

    async def write_many(self, ops: list[tuple[str, tuple]]) -> None:
        async with self._write_lock:
            for sql, params in ops:
                await self._write_conn.execute(sql, params)  # type: ignore[union-attr]
            await self._write_conn.commit()  # type: ignore[union-attr]


_pool = _ConnPool(DB_PATH, size=4)


class _BatchWriter:
    FLUSH_INTERVAL = 2.0
    FLUSH_THRESHOLD = 20

    def __init__(self) -> None:
        self._queue: list[tuple[str, tuple]] = []
        self._lock  = asyncio.Lock()
        self._task: Optional[asyncio.Task] = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._flush_loop())

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(self.FLUSH_INTERVAL)
            await self.flush()

    async def enqueue(self, sql: str, params: tuple = ()) -> None:
        async with self._lock:
            self._queue.append((sql, params))
            if len(self._queue) >= self.FLUSH_THRESHOLD:
                await self._flush_locked()

    async def flush(self) -> None:
        async with self._lock:
            await self._flush_locked()

    async def _flush_locked(self) -> None:
        if not self._queue:
            return
        ops, self._queue = self._queue[:], []
        try:
            await _pool.write_many(ops)
        except Exception as exc:
            logger.error("Batch flush failed: %s", exc, exc_info=True)


_batch = _BatchWriter()


async def init_db() -> None:
    await _pool.init()

    schema = """
        CREATE TABLE IF NOT EXISTS guild_configs (
            guild_id             INTEGER PRIMARY KEY,
            welcome_channel      INTEGER,
            welcome_message      TEXT,
            welcome_title        TEXT,
            welcome_footer       TEXT,
            welcome_footer_icon  TEXT,
            welcome_image        TEXT,
            welcome_color        INTEGER,
            welcome_enabled      INTEGER DEFAULT 0,
            welcome_embed        TEXT,
            leave_channel        INTEGER,
            leave_message        TEXT,
            leave_enabled        INTEGER DEFAULT 0,
            leave_embed          TEXT,
            boost_channel        INTEGER,
            boost_message        TEXT,
            boost_enabled        INTEGER DEFAULT 0,
            boost_embed          TEXT,
            autorole_id          INTEGER,
            autorole_enabled     INTEGER DEFAULT 0,
            mod_log_channel      INTEGER,
            antinuke_enabled     INTEGER DEFAULT 1,
            antinuke_punishment  TEXT    DEFAULT 'ban',
            antinuke_ban_thresh      INTEGER DEFAULT 3,
            antinuke_kick_thresh     INTEGER DEFAULT 3,
            antinuke_chan_thresh     INTEGER DEFAULT 3,
            antinuke_role_thresh     INTEGER DEFAULT 3,
            antinuke_webhook_thresh  INTEGER DEFAULT 5,
            antinuke_window          INTEGER DEFAULT 10,
            updated_at           INTEGER DEFAULT (strftime('%s','now'))
        );

        CREATE TABLE IF NOT EXISTS antinuke_whitelist (
            guild_id    INTEGER NOT NULL,
            user_id     INTEGER NOT NULL,
            added_by    INTEGER NOT NULL,
            added_at    INTEGER DEFAULT (strftime('%s','now')),
            PRIMARY KEY (guild_id, user_id)
        );

        CREATE TABLE IF NOT EXISTS warnings (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id     INTEGER NOT NULL,
            user_id      INTEGER NOT NULL,
            moderator_id INTEGER NOT NULL,
            reason       TEXT    NOT NULL,
            created_at   INTEGER DEFAULT (strftime('%s','now'))
        );
        CREATE INDEX IF NOT EXISTS idx_warnings_guild_user ON warnings (guild_id, user_id);

        CREATE TABLE IF NOT EXISTS mod_actions (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id     INTEGER NOT NULL,
            action       TEXT    NOT NULL,
            target_id    INTEGER NOT NULL,
            moderator_id INTEGER NOT NULL,
            reason       TEXT,
            duration     INTEGER,
            created_at   INTEGER DEFAULT (strftime('%s','now'))
        );
        CREATE INDEX IF NOT EXISTS idx_mod_actions_guild ON mod_actions (guild_id, created_at DESC);

        CREATE TABLE IF NOT EXISTS antinuke_logs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id    INTEGER NOT NULL,
            action      TEXT    NOT NULL,
            offender_id INTEGER NOT NULL,
            count       INTEGER NOT NULL,
            punishment  TEXT    NOT NULL,
            created_at  INTEGER DEFAULT (strftime('%s','now'))
        );
        CREATE INDEX IF NOT EXISTS idx_antinuke_logs ON antinuke_logs (guild_id, created_at DESC);

        CREATE TABLE IF NOT EXISTS embeds (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id   INTEGER NOT NULL,
            name       TEXT    NOT NULL,
            data       TEXT    NOT NULL,
            created_at INTEGER DEFAULT (strftime('%s','now')),
            updated_at INTEGER DEFAULT (strftime('%s','now')),
            UNIQUE(guild_id, name)
        );
        CREATE INDEX IF NOT EXISTS idx_embeds_guild ON embeds (guild_id);

        CREATE TABLE IF NOT EXISTS premium_users (
            user_id    INTEGER PRIMARY KEY,
            tier       INTEGER NOT NULL DEFAULT 1,
            expires_at INTEGER,
            patreon_id TEXT,
            added_by   INTEGER,
            created_at INTEGER DEFAULT (strftime('%s','now'))
        );

        CREATE TABLE IF NOT EXISTS premium_activations (
            user_id           INTEGER PRIMARY KEY,
            guild_id          INTEGER NOT NULL,
            tier              INTEGER NOT NULL,
            activated_at      INTEGER NOT NULL,
            last_switched_at  INTEGER NOT NULL,
            expires_at        INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_premium_act_guild ON premium_activations (guild_id);

        CREATE TABLE IF NOT EXISTS guild_assets (
            guild_id   INTEGER PRIMARY KEY,
            palette    TEXT,
            emojis     TEXT,
            updated_at INTEGER DEFAULT (strftime('%s','now'))
        );

        CREATE TABLE IF NOT EXISTS blacklist (
            target_id   INTEGER PRIMARY KEY,
            target_type TEXT    NOT NULL DEFAULT 'user',
            reason      TEXT,
            added_by    INTEGER NOT NULL,
            added_at    INTEGER DEFAULT (strftime('%s','now'))
        );

        CREATE TABLE IF NOT EXISTS embed_inventory (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER NOT NULL,
            name       TEXT    NOT NULL,
            data       TEXT    NOT NULL,
            created_at INTEGER DEFAULT (strftime('%s','now')),
            updated_at INTEGER DEFAULT (strftime('%s','now')),
            UNIQUE(user_id, name)
        );
        CREATE INDEX IF NOT EXISTS idx_embed_inventory_user ON embed_inventory (user_id);

        CREATE TABLE IF NOT EXISTS autoresponders (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id       INTEGER NOT NULL,
            trigger        TEXT    NOT NULL,
            match_type     TEXT    NOT NULL DEFAULT 'exact',
            case_sensitive INTEGER NOT NULL DEFAULT 0,
            response       TEXT    NOT NULL,
            enabled        INTEGER NOT NULL DEFAULT 1,
            cooldown       INTEGER NOT NULL DEFAULT 0,
            use_count      INTEGER NOT NULL DEFAULT 0,
            created_by     INTEGER NOT NULL,
            created_at     INTEGER DEFAULT (strftime('%s','now')),
            updated_at     INTEGER DEFAULT (strftime('%s','now')),
            UNIQUE(guild_id, trigger)
        );
        CREATE INDEX IF NOT EXISTS idx_ar_guild ON autoresponders (guild_id);

        CREATE TABLE IF NOT EXISTS ar_inventory (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER NOT NULL,
            name       TEXT    NOT NULL,
            trigger    TEXT    NOT NULL,
            match_type TEXT    NOT NULL DEFAULT 'exact',
            response   TEXT    NOT NULL,
            created_at INTEGER DEFAULT (strftime('%s','now')),
            UNIQUE(user_id, name)
        );
        CREATE INDEX IF NOT EXISTS idx_ar_inv_user ON ar_inventory (user_id);

        CREATE TABLE IF NOT EXISTS ar_cooldowns (
            guild_id   INTEGER NOT NULL,
            ar_id      INTEGER NOT NULL,
            user_id    INTEGER NOT NULL,
            used_at    INTEGER NOT NULL,
            PRIMARY KEY (guild_id, ar_id, user_id)
        );

        CREATE TABLE IF NOT EXISTS buttons (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id   INTEGER NOT NULL,
            name       TEXT    NOT NULL,
            btn_type   TEXT    NOT NULL DEFAULT 'linked',
            label      TEXT    NOT NULL,
            style      TEXT    NOT NULL DEFAULT 'primary',
            emoji      TEXT,
            url        TEXT,
            response   TEXT,
            created_by INTEGER NOT NULL,
            created_at INTEGER DEFAULT (strftime('%s','now')),
            UNIQUE(guild_id, name)
        );
        CREATE INDEX IF NOT EXISTS idx_buttons_guild ON buttons (guild_id);

        CREATE TABLE IF NOT EXISTS button_inventory (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER NOT NULL,
            name       TEXT    NOT NULL,
            data       TEXT    NOT NULL,
            created_at INTEGER DEFAULT (strftime('%s','now')),
            UNIQUE(user_id, name)
        );
        CREATE INDEX IF NOT EXISTS idx_btn_inv_user ON button_inventory (user_id)
    """

    for stmt in [s.strip() for s in schema.split(";") if s.strip()]:
        try:
            await _pool.write(stmt)
        except Exception as exc:
            logger.debug("Schema stmt skipped: %s", exc)

    migrate_cols = [
        ("antinuke_ban_thresh",     "INTEGER DEFAULT 3"),
        ("antinuke_kick_thresh",    "INTEGER DEFAULT 3"),
        ("antinuke_chan_thresh",     "INTEGER DEFAULT 3"),
        ("antinuke_role_thresh",    "INTEGER DEFAULT 3"),
        ("antinuke_webhook_thresh", "INTEGER DEFAULT 5"),
        ("antinuke_window",         "INTEGER DEFAULT 10"),
        ("welcome_title",           "TEXT"),
        ("welcome_footer",          "TEXT"),
        ("welcome_footer_icon",     "TEXT"),
        ("welcome_image",           "TEXT"),
        ("welcome_color",           "INTEGER"),
        ("welcome_embed",           "TEXT"),
        ("leave_channel",           "INTEGER"),
        ("leave_message",           "TEXT"),
        ("leave_enabled",           "INTEGER DEFAULT 0"),
        ("leave_embed",             "TEXT"),
        ("boost_channel",           "INTEGER"),
        ("boost_message",           "TEXT"),
        ("boost_enabled",           "INTEGER DEFAULT 0"),
        ("boost_embed",             "TEXT"),
        ("jail_role_id",            "INTEGER"),
        ("jail_channel_id",         "INTEGER"),
        ("mute_role_id",            "INTEGER"),
        ("bot_bio",                 "TEXT"),
        ("log_channel_id",          "INTEGER"),
    ]
    for col, col_type in migrate_cols:
        try:
            await _pool.write(f"ALTER TABLE guild_configs ADD COLUMN {col} {col_type}")
        except Exception:
            pass

    cache.start()
    _batch.start()
    logger.info("Database ready — pool=%d, cache+batch writer started", _pool._size)


async def get_guild_config(guild_id: int) -> dict:
    key = guild_config_key(guild_id)
    cached = cache.get(key)
    if cached is not None:
        return cached
    async with _pool.read() as conn:
        async with conn.execute(
            "SELECT * FROM guild_configs WHERE guild_id = ?", (guild_id,)
        ) as cur:
            row = await cur.fetchone()
    result = dict(row) if row else {"guild_id": guild_id}
    cache.set(key, result, ttl=TTL_GUILD_CONFIG)
    return result


async def upsert_guild_config(guild_id: int, **fields: Any) -> None:
    cols    = list(fields.keys())
    vals    = list(fields.values())
    placeholders = ", ".join("?" for _ in cols)
    updates = ", ".join(f"{c} = excluded.{c}" for c in cols)
    sql = (
        f"INSERT INTO guild_configs (guild_id, {', '.join(cols)}) "
        f"VALUES (?, {placeholders}) "
        f"ON CONFLICT(guild_id) DO UPDATE SET {updates}, "
        f"updated_at = strftime('%s','now')"
    )
    await _pool.write(sql, (guild_id, *vals))
    cache.delete(guild_config_key(guild_id))


async def get_whitelist(guild_id: int) -> set[int]:
    key    = whitelist_key(guild_id)
    cached = cache.get(key)
    if cached is not None:
        return cached
    async with _pool.read() as conn:
        async with conn.execute(
            "SELECT user_id FROM antinuke_whitelist WHERE guild_id = ?", (guild_id,)
        ) as cur:
            rows = await cur.fetchall()
    result = {r[0] for r in rows}
    cache.set(key, result, ttl=TTL_WHITELIST)
    return result


async def add_to_whitelist(guild_id: int, user_id: int, added_by: int) -> None:
    await _pool.write(
        "INSERT OR IGNORE INTO antinuke_whitelist (guild_id, user_id, added_by) VALUES (?, ?, ?)",
        (guild_id, user_id, added_by),
    )
    cache.delete(whitelist_key(guild_id))


async def remove_from_whitelist(guild_id: int, user_id: int) -> None:
    await _pool.write(
        "DELETE FROM antinuke_whitelist WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id),
    )
    cache.delete(whitelist_key(guild_id))


async def add_warning(guild_id: int, user_id: int, moderator_id: int, reason: str) -> int:
    cur = await _pool.write(
        "INSERT INTO warnings (guild_id, user_id, moderator_id, reason) VALUES (?, ?, ?, ?)",
        (guild_id, user_id, moderator_id, reason),
    )
    cache.delete(warnings_key(guild_id, user_id))
    return cur.lastrowid  # type: ignore[return-value]


async def get_warnings(guild_id: int, user_id: int) -> list[dict]:
    key    = warnings_key(guild_id, user_id)
    cached = cache.get(key)
    if cached is not None:
        return cached
    async with _pool.read() as conn:
        async with conn.execute(
            "SELECT id, moderator_id, reason, created_at FROM warnings "
            "WHERE guild_id = ? AND user_id = ? ORDER BY created_at DESC",
            (guild_id, user_id),
        ) as cur:
            rows = await cur.fetchall()
    result = [dict(r) for r in rows]
    cache.set(key, result, ttl=TTL_WARNINGS)
    return result


async def clear_warnings(guild_id: int, user_id: int) -> int:
    cur = await _pool.write(
        "DELETE FROM warnings WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id),
    )
    cache.delete(warnings_key(guild_id, user_id))
    return cur.rowcount  # type: ignore[return-value]


async def log_mod_action(
    guild_id: int, action: str, target_id: int, moderator_id: int,
    reason: Optional[str] = None, duration: Optional[int] = None,
) -> int:
    cur = await _pool.write(
        "INSERT INTO mod_actions (guild_id, action, target_id, moderator_id, reason, duration) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (guild_id, action, target_id, moderator_id, reason, duration),
    )
    return cur.lastrowid  # type: ignore[return-value]


async def get_mod_actions(guild_id: int, limit: int = 20) -> list[dict]:
    async with _pool.read() as conn:
        async with conn.execute(
            "SELECT * FROM mod_actions WHERE guild_id = ? ORDER BY created_at DESC LIMIT ?",
            (guild_id, limit),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def log_antinuke(
    guild_id: int, action: str, offender_id: int, count: int, punishment: str
) -> None:
    await _batch.enqueue(
        "INSERT INTO antinuke_logs (guild_id, action, offender_id, count, punishment) VALUES (?, ?, ?, ?, ?)",
        (guild_id, action, offender_id, count, punishment),
    )


async def get_antinuke_logs(guild_id: int, limit: int = 10) -> list[dict]:
    async with _pool.read() as conn:
        async with conn.execute(
            "SELECT * FROM antinuke_logs WHERE guild_id = ? ORDER BY created_at DESC LIMIT ?",
            (guild_id, limit),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def save_embed(guild_id: int, name: str, data: dict) -> None:
    await _pool.write(
        "INSERT INTO embeds (guild_id, name, data) VALUES (?, ?, ?) "
        "ON CONFLICT(guild_id, name) DO UPDATE SET data = excluded.data, "
        "updated_at = strftime('%s','now')",
        (guild_id, name.lower(), json.dumps(data)),
    )


async def get_embed(guild_id: int, name: str) -> Optional[dict]:
    async with _pool.read() as conn:
        async with conn.execute(
            "SELECT data FROM embeds WHERE guild_id = ? AND name = ?",
            (guild_id, name.lower()),
        ) as cur:
            row = await cur.fetchone()
    if row:
        return json.loads(row[0])
    return None


async def get_all_embeds(guild_id: int) -> list[dict]:
    async with _pool.read() as conn:
        async with conn.execute(
            "SELECT name, created_at, updated_at FROM embeds WHERE guild_id = ? ORDER BY name",
            (guild_id,),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def delete_embed(guild_id: int, name: str) -> bool:
    cur = await _pool.write(
        "DELETE FROM embeds WHERE guild_id = ? AND name = ?",
        (guild_id, name.lower()),
    )
    return cur.rowcount > 0  # type: ignore[return-value]


async def count_server_embeds(guild_id: int) -> int:
    async with _pool.read() as conn:
        async with conn.execute(
            "SELECT COUNT(*) FROM embeds WHERE guild_id = ?", (guild_id,)
        ) as cur:
            row = await cur.fetchone()
    return row[0] if row else 0


async def get_premium_tier(user_id: int) -> int:
    async with _pool.read() as conn:
        async with conn.execute(
            "SELECT tier, expires_at FROM premium_users WHERE user_id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
    if not row:
        return 0
    if row[1] and row[1] < int(asyncio.get_event_loop().time()):
        import time as _t
        if row[1] < int(_t.time()):
            return 0
    return row[0]


async def get_premium_info(user_id: int) -> Optional[dict]:
    async with _pool.read() as conn:
        async with conn.execute(
            "SELECT * FROM premium_users WHERE user_id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


async def set_premium(
    user_id: int, tier: int,
    expires_at: Optional[int] = None,
    patreon_id: Optional[str] = None,
    added_by: Optional[int] = None,
) -> None:
    await _pool.write(
        "INSERT INTO premium_users (user_id, tier, expires_at, patreon_id, added_by) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET tier=excluded.tier, "
        "expires_at=excluded.expires_at, patreon_id=excluded.patreon_id",
        (user_id, tier, expires_at, patreon_id, added_by),
    )


async def remove_premium(user_id: int) -> bool:
    cur = await _pool.write(
        "DELETE FROM premium_users WHERE user_id = ?", (user_id,)
    )
    return cur.rowcount > 0  # type: ignore[return-value]


async def get_all_premium() -> list[dict]:
    async with _pool.read() as conn:
        async with conn.execute(
            "SELECT * FROM premium_users ORDER BY tier DESC, created_at"
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def is_blacklisted(target_id: int) -> bool:
    async with _pool.read() as conn:
        async with conn.execute(
            "SELECT 1 FROM blacklist WHERE target_id = ?", (target_id,)
        ) as cur:
            return await cur.fetchone() is not None


async def add_blacklist(target_id: int, target_type: str, reason: str, added_by: int) -> None:
    await _pool.write(
        "INSERT OR REPLACE INTO blacklist (target_id, target_type, reason, added_by) "
        "VALUES (?, ?, ?, ?)",
        (target_id, target_type, reason, added_by),
    )


async def remove_blacklist(target_id: int) -> bool:
    cur = await _pool.write(
        "DELETE FROM blacklist WHERE target_id = ?", (target_id,)
    )
    return cur.rowcount > 0  # type: ignore[return-value]


async def get_blacklist_all() -> list[dict]:
    async with _pool.read() as conn:
        async with conn.execute(
            "SELECT * FROM blacklist ORDER BY added_at DESC"
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def save_inventory_embed(user_id: int, name: str, data: dict) -> None:
    await _pool.write(
        "INSERT INTO embed_inventory (user_id, name, data) VALUES (?, ?, ?) "
        "ON CONFLICT(user_id, name) DO UPDATE SET data = excluded.data, "
        "updated_at = strftime('%s','now')",
        (user_id, name.lower(), json.dumps(data)),
    )


async def get_inventory_embed(user_id: int, name: str) -> Optional[dict]:
    async with _pool.read() as conn:
        async with conn.execute(
            "SELECT data FROM embed_inventory WHERE user_id = ? AND name = ?",
            (user_id, name.lower()),
        ) as cur:
            row = await cur.fetchone()
    if row:
        return json.loads(row[0])
    return None


async def get_all_inventory_embeds(user_id: int) -> list[dict]:
    async with _pool.read() as conn:
        async with conn.execute(
            "SELECT name, created_at, updated_at FROM embed_inventory WHERE user_id = ? ORDER BY name",
            (user_id,),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def delete_inventory_embed(user_id: int, name: str) -> bool:
    cur = await _pool.write(
        "DELETE FROM embed_inventory WHERE user_id = ? AND name = ?",
        (user_id, name.lower()),
    )
    return cur.rowcount > 0  # type: ignore[return-value]


async def count_inventory_embeds(user_id: int) -> int:
    async with _pool.read() as conn:
        async with conn.execute(
            "SELECT COUNT(*) FROM embed_inventory WHERE user_id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
    return row[0] if row else 0


def get_cache_stats() -> dict:
    return cache.stats()


# ── Autoresponders ────────────────────────────────────────────────────────────

async def create_autoresponder(
    guild_id: int, trigger: str, match_type: str,
    case_sensitive: bool, response: str, cooldown: int, created_by: int
) -> int:
    cur = await _pool.write(
        "INSERT INTO autoresponders "
        "(guild_id, trigger, match_type, case_sensitive, response, cooldown, created_by) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (guild_id, trigger, match_type, int(case_sensitive), response, cooldown, created_by),
    )
    return cur.lastrowid  # type: ignore[return-value]


async def get_autoresponder(guild_id: int, trigger: str) -> Optional[dict]:
    async with _pool.read() as conn:
        async with conn.execute(
            "SELECT * FROM autoresponders WHERE guild_id = ? AND trigger = ?",
            (guild_id, trigger),
        ) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


async def get_autoresponder_by_id(ar_id: int) -> Optional[dict]:
    async with _pool.read() as conn:
        async with conn.execute(
            "SELECT * FROM autoresponders WHERE id = ?", (ar_id,)
        ) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


async def get_all_autoresponders(guild_id: int) -> list[dict]:
    async with _pool.read() as conn:
        async with conn.execute(
            "SELECT * FROM autoresponders WHERE guild_id = ? ORDER BY trigger",
            (guild_id,),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def update_autoresponder(ar_id: int, **fields: Any) -> None:
    cols = list(fields.keys())
    vals = list(fields.values())
    sets = ", ".join(f"{c} = ?" for c in cols)
    await _pool.write(
        f"UPDATE autoresponders SET {sets}, updated_at = strftime('%s','now') WHERE id = ?",
        (*vals, ar_id),
    )


async def delete_autoresponder(guild_id: int, trigger: str) -> bool:
    cur = await _pool.write(
        "DELETE FROM autoresponders WHERE guild_id = ? AND trigger = ?",
        (guild_id, trigger),
    )
    return cur.rowcount > 0  # type: ignore[return-value]


async def count_autoresponders(guild_id: int) -> int:
    async with _pool.read() as conn:
        async with conn.execute(
            "SELECT COUNT(*) FROM autoresponders WHERE guild_id = ?", (guild_id,)
        ) as cur:
            row = await cur.fetchone()
    return row[0] if row else 0


async def increment_ar_use_count(ar_id: int) -> None:
    await _batch.enqueue(
        "UPDATE autoresponders SET use_count = use_count + 1 WHERE id = ?", (ar_id,)
    )


async def check_ar_cooldown(guild_id: int, ar_id: int, user_id: int, cooldown_secs: int) -> bool:
    """Returns True if user is on cooldown (should skip), False if ok to fire."""
    if cooldown_secs <= 0:
        return False
    now = int(time.time())
    async with _pool.read() as conn:
        async with conn.execute(
            "SELECT used_at FROM ar_cooldowns WHERE guild_id=? AND ar_id=? AND user_id=?",
            (guild_id, ar_id, user_id),
        ) as cur:
            row = await cur.fetchone()
    if row and (now - row[0]) < cooldown_secs:
        return True
    await _pool.write(
        "INSERT OR REPLACE INTO ar_cooldowns (guild_id, ar_id, user_id, used_at) VALUES (?,?,?,?)",
        (guild_id, ar_id, user_id, now),
    )
    return False


# ── Autoresponder Inventory ────────────────────────────────────────────────────

async def save_ar_inventory(user_id: int, name: str, trigger: str, match_type: str, response: str) -> None:
    await _pool.write(
        "INSERT INTO ar_inventory (user_id, name, trigger, match_type, response) VALUES (?,?,?,?,?) "
        "ON CONFLICT(user_id, name) DO UPDATE SET trigger=excluded.trigger, "
        "match_type=excluded.match_type, response=excluded.response",
        (user_id, name.lower(), trigger, match_type, response),
    )


async def get_ar_inventory(user_id: int, name: str) -> Optional[dict]:
    async with _pool.read() as conn:
        async with conn.execute(
            "SELECT * FROM ar_inventory WHERE user_id = ? AND name = ?",
            (user_id, name.lower()),
        ) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


async def get_all_ar_inventory(user_id: int) -> list[dict]:
    async with _pool.read() as conn:
        async with conn.execute(
            "SELECT * FROM ar_inventory WHERE user_id = ? ORDER BY name", (user_id,)
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def delete_ar_inventory(user_id: int, name: str) -> bool:
    cur = await _pool.write(
        "DELETE FROM ar_inventory WHERE user_id = ? AND name = ?",
        (user_id, name.lower()),
    )
    return cur.rowcount > 0  # type: ignore[return-value]


async def count_ar_inventory(user_id: int) -> int:
    async with _pool.read() as conn:
        async with conn.execute(
            "SELECT COUNT(*) FROM ar_inventory WHERE user_id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
    return row[0] if row else 0


# ── Buttons ───────────────────────────────────────────────────────────────────

async def save_button(
    guild_id: int, name: str, btn_type: str, label: str, style: str,
    created_by: int, emoji: Optional[str] = None,
    url: Optional[str] = None, response: Optional[str] = None,
) -> None:
    await _pool.write(
        "INSERT INTO buttons (guild_id, name, btn_type, label, style, emoji, url, response, created_by) "
        "VALUES (?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(guild_id, name) DO UPDATE SET btn_type=excluded.btn_type, "
        "label=excluded.label, style=excluded.style, emoji=excluded.emoji, "
        "url=excluded.url, response=excluded.response",
        (guild_id, name.lower(), btn_type, label, style, emoji, url, response, created_by),
    )


async def get_button(guild_id: int, name: str) -> Optional[dict]:
    async with _pool.read() as conn:
        async with conn.execute(
            "SELECT * FROM buttons WHERE guild_id = ? AND name = ?",
            (guild_id, name.lower()),
        ) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


async def get_all_buttons(guild_id: int) -> list[dict]:
    async with _pool.read() as conn:
        async with conn.execute(
            "SELECT * FROM buttons WHERE guild_id = ? ORDER BY name", (guild_id,)
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def delete_button(guild_id: int, name: str) -> bool:
    cur = await _pool.write(
        "DELETE FROM buttons WHERE guild_id = ? AND name = ?",
        (guild_id, name.lower()),
    )
    return cur.rowcount > 0  # type: ignore[return-value]


async def count_buttons(guild_id: int) -> int:
    async with _pool.read() as conn:
        async with conn.execute(
            "SELECT COUNT(*) FROM buttons WHERE guild_id = ?", (guild_id,)
        ) as cur:
            row = await cur.fetchone()
    return row[0] if row else 0


# ── Button Inventory ──────────────────────────────────────────────────────────

async def save_button_inventory(user_id: int, name: str, data: dict) -> None:
    await _pool.write(
        "INSERT INTO button_inventory (user_id, name, data) VALUES (?,?,?) "
        "ON CONFLICT(user_id, name) DO UPDATE SET data=excluded.data",
        (user_id, name.lower(), json.dumps(data)),
    )


async def get_button_inventory(user_id: int, name: str) -> Optional[dict]:
    async with _pool.read() as conn:
        async with conn.execute(
            "SELECT data FROM button_inventory WHERE user_id = ? AND name = ?",
            (user_id, name.lower()),
        ) as cur:
            row = await cur.fetchone()
    return json.loads(row[0]) if row else None


async def get_all_button_inventory(user_id: int) -> list[dict]:
    async with _pool.read() as conn:
        async with conn.execute(
            "SELECT name, created_at FROM button_inventory WHERE user_id = ? ORDER BY name",
            (user_id,),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


# ── Server Premium Activations ───────────────────────────────────────────────

ACTIVATION_DURATION_S = 14 * 24 * 3600   # 2 weeks
SWITCH_COOLDOWN_S     =  7 * 24 * 3600   # 1 week


async def get_user_activation(user_id: int) -> Optional[dict]:
    async with _pool.read() as conn:
        async with conn.execute(
            "SELECT * FROM premium_activations WHERE user_id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


async def get_server_premium(guild_id: int) -> Optional[dict]:
    """Return highest-tier active activation for the given guild, or None."""
    now = int(time.time())
    async with _pool.read() as conn:
        async with conn.execute(
            "SELECT * FROM premium_activations "
            "WHERE guild_id = ? AND expires_at > ? "
            "ORDER BY tier DESC LIMIT 1",
            (guild_id, now),
        ) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


async def get_server_premium_tier(guild_id: int) -> int:
    """Effective server tier — capped by the activator's *current* member tier.

    If a member who activated premium downgrades or cancels, the server's
    effective tier follows their current membership level (not the snapshot
    taken at activation time). Returns 0 if no active activation, or if the
    activating member no longer holds premium.
    """
    info = await get_server_premium(guild_id)
    if not info:
        return 0
    member_tier = await get_premium_tier(info["user_id"])
    return min(int(info["tier"]), int(member_tier))


async def get_server_activations(guild_id: int) -> list[dict]:
    now = int(time.time())
    async with _pool.read() as conn:
        async with conn.execute(
            "SELECT * FROM premium_activations WHERE guild_id = ? AND expires_at > ?",
            (guild_id, now),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def activate_server_premium(user_id: int, guild_id: int, tier: int) -> dict:
    now = int(time.time())
    expires = now + ACTIVATION_DURATION_S
    await _pool.write(
        "INSERT INTO premium_activations "
        "(user_id, guild_id, tier, activated_at, last_switched_at, expires_at) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET "
        "guild_id = excluded.guild_id, tier = excluded.tier, "
        "activated_at = excluded.activated_at, "
        "last_switched_at = excluded.last_switched_at, "
        "expires_at = excluded.expires_at",
        (user_id, guild_id, tier, now, now, expires),
    )
    return {"guild_id": guild_id, "tier": tier, "activated_at": now,
            "last_switched_at": now, "expires_at": expires}


async def switch_server_premium(user_id: int, new_guild_id: int, tier: int) -> dict:
    """Move an existing activation to a new guild (resets the 2-week timer)."""
    now = int(time.time())
    expires = now + ACTIVATION_DURATION_S
    await _pool.write(
        "UPDATE premium_activations SET guild_id = ?, tier = ?, "
        "activated_at = ?, last_switched_at = ?, expires_at = ? "
        "WHERE user_id = ?",
        (new_guild_id, tier, now, now, expires, user_id),
    )
    return {"guild_id": new_guild_id, "tier": tier, "activated_at": now,
            "last_switched_at": now, "expires_at": expires}


async def remove_server_premium(user_id: int) -> None:
    await _pool.write(
        "DELETE FROM premium_activations WHERE user_id = ?", (user_id,)
    )


# ── Guild Custom Assets (palette / emojis) ───────────────────────────────────

async def get_guild_palette(guild_id: int) -> Optional[dict]:
    async with _pool.read() as conn:
        async with conn.execute(
            "SELECT palette FROM guild_assets WHERE guild_id = ?", (guild_id,)
        ) as cur:
            row = await cur.fetchone()
    if row and row[0]:
        try:
            return json.loads(row[0])
        except json.JSONDecodeError:
            return None
    return None


async def set_guild_palette(guild_id: int, palette: dict) -> None:
    await _pool.write(
        "INSERT INTO guild_assets (guild_id, palette) VALUES (?, ?) "
        "ON CONFLICT(guild_id) DO UPDATE SET palette=excluded.palette, "
        "updated_at=strftime('%s','now')",
        (guild_id, json.dumps(palette)),
    )


async def reset_guild_palette(guild_id: int) -> None:
    await _pool.write(
        "UPDATE guild_assets SET palette=NULL, updated_at=strftime('%s','now') "
        "WHERE guild_id = ?", (guild_id,)
    )


async def get_guild_emojis(guild_id: int) -> Optional[dict]:
    async with _pool.read() as conn:
        async with conn.execute(
            "SELECT emojis FROM guild_assets WHERE guild_id = ?", (guild_id,)
        ) as cur:
            row = await cur.fetchone()
    if row and row[0]:
        try:
            return json.loads(row[0])
        except json.JSONDecodeError:
            return None
    return None


async def set_guild_emojis(guild_id: int, emojis: dict) -> None:
    await _pool.write(
        "INSERT INTO guild_assets (guild_id, emojis) VALUES (?, ?) "
        "ON CONFLICT(guild_id) DO UPDATE SET emojis=excluded.emojis, "
        "updated_at=strftime('%s','now')",
        (guild_id, json.dumps(emojis)),
    )


async def reset_guild_emojis(guild_id: int) -> None:
    await _pool.write(
        "UPDATE guild_assets SET emojis=NULL, updated_at=strftime('%s','now') "
        "WHERE guild_id = ?", (guild_id,)
    )


async def delete_button_inventory(user_id: int, name: str) -> bool:
    cur = await _pool.write(
        "DELETE FROM button_inventory WHERE user_id = ? AND name = ?",
        (user_id, name.lower()),
    )
    return cur.rowcount > 0  # type: ignore[return-value]
