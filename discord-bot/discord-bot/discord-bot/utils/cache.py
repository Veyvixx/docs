"""
TTL-based in-memory cache with background eviction.

Usage:
    cache = TTLCache(default_ttl=60)
    cache.set("guild:123:config", {...}, ttl=300)
    val = cache.get("guild:123:config")   # None on miss
    cache.delete("guild:123:config")
    cache.evict_prefix("guild:123:")      # nuke all keys for guild 123
"""

import asyncio
import time
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


class TTLCache:
    """
    Thread-safe in-memory cache with per-key TTL.
    Eviction runs automatically in the background every `sweep_interval` seconds.
    """

    def __init__(self, default_ttl: float = 60.0, sweep_interval: float = 30.0) -> None:
        self._store: dict[str, tuple[Any, float]] = {}   # key → (value, expires_at)
        self._default_ttl   = default_ttl
        self._sweep_interval = sweep_interval
        self._hits   = 0
        self._misses = 0
        self._task: Optional[asyncio.Task] = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the background eviction loop. Call once from bot.setup_hook."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._sweep_loop())
            logger.debug("TTLCache sweep loop started (interval=%ss)", self._sweep_interval)

    def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()

    async def _sweep_loop(self) -> None:
        while True:
            await asyncio.sleep(self._sweep_interval)
            self._evict_expired()

    def _evict_expired(self) -> None:
        now = time.monotonic()
        expired = [k for k, (_, exp) in self._store.items() if exp <= now]
        for k in expired:
            del self._store[k]
        if expired:
            logger.debug("TTLCache evicted %d expired key(s)", len(expired))

    # ── Core operations ───────────────────────────────────────────────────────

    def get(self, key: str) -> Any:
        """Return cached value or None on miss/expiry."""
        entry = self._store.get(key)
        if entry is None:
            self._misses += 1
            return None
        value, expires_at = entry
        if time.monotonic() > expires_at:
            del self._store[key]
            self._misses += 1
            return None
        self._hits += 1
        return value

    def set(self, key: str, value: Any, ttl: Optional[float] = None) -> None:
        """Store a value with optional per-key TTL override."""
        ttl = ttl if ttl is not None else self._default_ttl
        self._store[key] = (value, time.monotonic() + ttl)

    def delete(self, key: str) -> None:
        self._store.pop(key, None)

    def evict_prefix(self, prefix: str) -> int:
        """Remove all keys starting with `prefix`. Returns count removed."""
        to_remove = [k for k in self._store if k.startswith(prefix)]
        for k in to_remove:
            del self._store[k]
        return len(to_remove)

    def has(self, key: str) -> bool:
        return self.get(key) is not None

    def clear(self) -> None:
        self._store.clear()

    # ── Stats ─────────────────────────────────────────────────────────────────

    @property
    def size(self) -> int:
        return len(self._store)

    @property
    def hit_rate(self) -> float:
        total = self._hits + self._misses
        return (self._hits / total * 100) if total else 0.0

    def stats(self) -> dict:
        return {
            "keys":     self.size,
            "hits":     self._hits,
            "misses":   self._misses,
            "hit_rate": f"{self.hit_rate:.1f}%",
        }


# ── Shared singleton (imported by database.py and cogs) ───────────────────────
# TTLs (seconds):
#   guild_config  → 5 min  (changes infrequently)
#   whitelist     → 2 min  (anti-nuke critical path, slightly shorter)
#   warnings      → 30 s   (moderate frequency)
#   user_data     → 10 min (rarely changes mid-session)

cache = TTLCache(default_ttl=60, sweep_interval=30)

# Convenience key builders
def guild_config_key(guild_id: int) -> str:
    return f"gc:{guild_id}"

def whitelist_key(guild_id: int) -> str:
    return f"wl:{guild_id}"

def warnings_key(guild_id: int, user_id: int) -> str:
    return f"warn:{guild_id}:{user_id}"
