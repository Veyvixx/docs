"""
DynamoDB-backed persistence layer for Nana.

Public API mirrors the original SQLite module (utils/database_sqlite.py) so
cogs do not need any changes. All numeric attributes round-trip through
``Decimal`` and are normalised to ``int`` on read.

Region & credentials come from environment variables:
    AWS_REGION, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from decimal import Decimal
from typing import Any, Optional

import aioboto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from utils.cache import (
    cache,
    guild_config_key,
    whitelist_key,
    warnings_key,
)

logger = logging.getLogger(__name__)

# ── Config ───────────────────────────────────────────────────────────────────

REGION = os.environ.get("AWS_REGION", "eu-central-1")

TTL_GUILD_CONFIG = 300
TTL_WHITELIST    = 120
TTL_WARNINGS     = 30

ACTIVATION_DURATION_S = 14 * 24 * 3600
SWITCH_COOLDOWN_S     =  7 * 24 * 3600

# Table names (created out-of-band in the AWS console; see scripts/migrate_to_dynamo.py)
T_GUILD_CONFIGS  = "nana_guild_configs"
T_PREMIUM_USERS  = "nana_premium_users"
T_PREMIUM_ACT    = "nana_premium_activations"
T_GUILD_ASSETS   = "nana_guild_assets"
T_EMBEDS         = "nana_embeds"
T_EMBED_INV      = "nana_embed_inventory"
T_ARS            = "nana_autoresponders"
T_AR_INV         = "nana_ar_inventory"
T_AR_COOLDOWNS   = "nana_ar_cooldowns"
T_BUTTONS        = "nana_buttons"
T_BUTTON_INV     = "nana_button_inventory"
T_WARNINGS       = "nana_warnings"
T_MOD_ACTIONS    = "nana_mod_actions"
T_AN_WHITELIST   = "nana_antinuke_whitelist"
T_AN_LOGS        = "nana_antinuke_logs"
T_BLACKLIST      = "nana_blacklist"

# ── Resource lifecycle ───────────────────────────────────────────────────────

_session: Optional[aioboto3.Session] = None
_resource_cm = None
_dyn = None  # async DynamoDB ServiceResource
_tables: dict[str, Any] = {}


async def init_db() -> None:
    """Open the long-lived async DynamoDB resource and prefetch table handles."""
    global _session, _resource_cm, _dyn, _tables
    _session = aioboto3.Session()
    _resource_cm = _session.resource("dynamodb", region_name=REGION)
    _dyn = await _resource_cm.__aenter__()

    names = [T_GUILD_CONFIGS, T_PREMIUM_USERS, T_PREMIUM_ACT, T_GUILD_ASSETS,
             T_EMBEDS, T_EMBED_INV, T_ARS, T_AR_INV, T_AR_COOLDOWNS,
             T_BUTTONS, T_BUTTON_INV, T_WARNINGS, T_MOD_ACTIONS,
             T_AN_WHITELIST, T_AN_LOGS, T_BLACKLIST]
    for n in names:
        _tables[n] = await _dyn.Table(n)

    cache.start()
    logger.info("DynamoDB ready — region=%s, %d tables, cache started", REGION, len(_tables))


async def close_db() -> None:
    global _resource_cm, _dyn, _tables
    if _resource_cm is not None:
        try:
            await _resource_cm.__aexit__(None, None, None)
        except Exception:  # pragma: no cover - best-effort
            pass
    _resource_cm = None
    _dyn = None
    _tables = {}


def _t(name: str):
    if not _tables:
        raise RuntimeError("Database not initialised. Call init_db() first.")
    return _tables[name]


# ── Type helpers ─────────────────────────────────────────────────────────────

def _decode(obj: Any) -> Any:
    """Recursively convert Decimal → int/float for cog consumption."""
    if isinstance(obj, list):
        return [_decode(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _decode(v) for k, v in obj.items()}
    if isinstance(obj, Decimal):
        return int(obj) if obj % 1 == 0 else float(obj)
    return obj


def _encode(obj: Any) -> Any:
    """Recursively convert float → Decimal for DynamoDB."""
    if isinstance(obj, list):
        return [_encode(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _encode(v) for k, v in obj.items()}
    if isinstance(obj, float):
        return Decimal(str(obj))
    return obj


def _clean(d: dict) -> dict:
    """Drop None values — DynamoDB doesn't accept them."""
    return {k: _encode(v) for k, v in d.items() if v is not None}


def _build_update(fields: dict) -> tuple[str, dict, dict]:
    """Build an UpdateItem expression from a partial-update field dict.

    Returns (UpdateExpression, ExpressionAttributeNames, ExpressionAttributeValues).
    None values are converted to REMOVE actions.
    """
    set_parts: list[str] = []
    rem_parts: list[str] = []
    names: dict[str, str] = {}
    values: dict[str, Any] = {}
    for i, (k, v) in enumerate(fields.items()):
        ph = f"#a{i}"
        names[ph] = k
        if v is None:
            rem_parts.append(ph)
        else:
            vp = f":v{i}"
            values[vp] = _encode(v)
            set_parts.append(f"{ph} = {vp}")
    expr = ""
    if set_parts:
        expr += "SET " + ", ".join(set_parts)
    if rem_parts:
        expr += (" " if expr else "") + "REMOVE " + ", ".join(rem_parts)
    return expr, names, values


# =============================================================================
# Guild Configs
# =============================================================================

async def get_guild_config(guild_id: int) -> dict:
    key = guild_config_key(guild_id)
    cached = cache.get(key)
    if cached is not None:
        return cached
    resp = await _t(T_GUILD_CONFIGS).get_item(Key={"guild_id": guild_id})
    item = resp.get("Item")
    result = _decode(item) if item else {"guild_id": guild_id}
    cache.set(key, result, ttl=TTL_GUILD_CONFIG)
    return result


async def upsert_guild_config(guild_id: int, **fields: Any) -> None:
    fields = dict(fields)
    fields["updated_at"] = int(time.time())
    expr, names, values = _build_update(fields)
    await _t(T_GUILD_CONFIGS).update_item(
        Key={"guild_id": guild_id},
        UpdateExpression=expr,
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
    )
    cache.delete(guild_config_key(guild_id))


# =============================================================================
# Anti-nuke whitelist
# =============================================================================

async def get_whitelist(guild_id: int) -> set[int]:
    key = whitelist_key(guild_id)
    cached = cache.get(key)
    if cached is not None:
        return cached
    resp = await _t(T_AN_WHITELIST).query(
        KeyConditionExpression=Key("guild_id").eq(guild_id),
    )
    result = {int(r["user_id"]) for r in resp.get("Items", [])}
    cache.set(key, result, ttl=TTL_WHITELIST)
    return result


async def add_to_whitelist(guild_id: int, user_id: int, added_by: int) -> None:
    try:
        await _t(T_AN_WHITELIST).put_item(
            Item={
                "guild_id": guild_id, "user_id": user_id,
                "added_by": added_by, "added_at": int(time.time()),
            },
            ConditionExpression="attribute_not_exists(guild_id) AND attribute_not_exists(user_id)",
        )
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
    cache.delete(whitelist_key(guild_id))


async def remove_from_whitelist(guild_id: int, user_id: int) -> bool:
    resp = await _t(T_AN_WHITELIST).delete_item(
        Key={"guild_id": guild_id, "user_id": user_id},
        ReturnValues="ALL_OLD",
    )
    cache.delete(whitelist_key(guild_id))
    return "Attributes" in resp


# =============================================================================
# Warnings  (HASH guild_user "{gid}#{uid}", RANGE created_at NUMBER)
# =============================================================================

def _warn_pk(guild_id: int, user_id: int) -> str:
    return f"{guild_id}#{user_id}"


async def add_warning(guild_id: int, user_id: int, moderator_id: int, reason: str) -> int:
    """Insert a warning. Returns the running count of warnings for this user.

    The cog displays the return value as ``Case #{n}`` and treats it purely as
    a label, so returning the post-insert warning count is more user-friendly
    than the original auto-increment id.
    """
    pk = _warn_pk(guild_id, user_id)
    # Use millisecond precision for the sort key to avoid collisions.
    created_at_ms = int(time.time() * 1000)
    # Retry on the (extremely unlikely) collision.
    for _ in range(5):
        try:
            await _t(T_WARNINGS).put_item(
                Item={
                    "guild_user": pk,
                    "created_at": created_at_ms,
                    "guild_id": str(guild_id),  # GSI indexed as String
                    "user_id": user_id,
                    "moderator_id": moderator_id,
                    "reason": reason,
                },
                ConditionExpression="attribute_not_exists(guild_user)",
            )
            break
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                created_at_ms += 1
                continue
            raise
    cache.delete(warnings_key(guild_id, user_id))
    warns = await get_warnings(guild_id, user_id)
    return len(warns)


async def get_warnings(guild_id: int, user_id: int) -> list[dict]:
    key = warnings_key(guild_id, user_id)
    cached = cache.get(key)
    if cached is not None:
        return cached
    resp = await _t(T_WARNINGS).query(
        KeyConditionExpression=Key("guild_user").eq(_warn_pk(guild_id, user_id)),
        ScanIndexForward=False,  # newest first
    )
    items = _decode(resp.get("Items", []))
    # Cogs may rely on a `created_at` in seconds for Discord <t:..> formatting.
    for it in items:
        ms = int(it.get("created_at", 0))
        it["id"] = ms
        it["created_at"] = ms // 1000
    cache.set(key, items, ttl=TTL_WARNINGS)
    return items


async def clear_warnings(guild_id: int, user_id: int) -> int:
    pk = _warn_pk(guild_id, user_id)
    resp = await _t(T_WARNINGS).query(
        KeyConditionExpression=Key("guild_user").eq(pk),
        ProjectionExpression="created_at",
    )
    items = resp.get("Items", [])
    if not items:
        cache.delete(warnings_key(guild_id, user_id))
        return 0
    table = _t(T_WARNINGS)
    async with table.batch_writer() as bw:
        for it in items:
            await bw.delete_item(Key={"guild_user": pk, "created_at": it["created_at"]})
    cache.delete(warnings_key(guild_id, user_id))
    return len(items)


# =============================================================================
# Mod-actions  (HASH guild_id, RANGE created_at)
# =============================================================================

async def log_mod_action(
    guild_id: int, action: str, target_id: int, moderator_id: int,
    reason: Optional[str] = None, duration: Optional[int] = None,
) -> int:
    created_at_ms = int(time.time() * 1000)
    item = _clean({
        "guild_id": guild_id,
        "created_at": created_at_ms,
        "action": action,
        "target_id": target_id,
        "moderator_id": moderator_id,
        "reason": reason,
        "duration": duration,
    })
    await _t(T_MOD_ACTIONS).put_item(Item=item)
    return created_at_ms


async def get_mod_actions(guild_id: int, limit: int = 20) -> list[dict]:
    resp = await _t(T_MOD_ACTIONS).query(
        KeyConditionExpression=Key("guild_id").eq(guild_id),
        ScanIndexForward=False,
        Limit=limit,
    )
    items = _decode(resp.get("Items", []))
    for it in items:
        if "created_at" in it:
            it["created_at"] = int(it["created_at"]) // 1000
    return items


# =============================================================================
# Anti-nuke logs  (HASH guild_id, RANGE created_at)
# =============================================================================

async def log_antinuke(
    guild_id: int, action: str, offender_id: int, count: int, punishment: str
) -> None:
    await _t(T_AN_LOGS).put_item(Item={
        "guild_id": guild_id,
        "created_at": int(time.time() * 1000),
        "action": action,
        "offender_id": offender_id,
        "count": count,
        "punishment": punishment,
    })


async def get_antinuke_logs(guild_id: int, limit: int = 10) -> list[dict]:
    resp = await _t(T_AN_LOGS).query(
        KeyConditionExpression=Key("guild_id").eq(guild_id),
        ScanIndexForward=False,
        Limit=limit,
    )
    items = _decode(resp.get("Items", []))
    for it in items:
        if "created_at" in it:
            it["created_at"] = int(it["created_at"]) // 1000
    return items


# =============================================================================
# Embeds  (server-saved)
# =============================================================================

async def save_embed(guild_id: int, name: str, data: dict) -> None:
    now = int(time.time())
    name_l = name.lower()
    # Check for existence to preserve created_at on updates.
    existing = await _t(T_EMBEDS).get_item(
        Key={"guild_id": guild_id, "name": name_l},
        ProjectionExpression="created_at",
    )
    created_at = existing.get("Item", {}).get("created_at", now)
    await _t(T_EMBEDS).put_item(Item={
        "guild_id": guild_id,
        "name": name_l,
        "data": _encode(data),
        "created_at": int(created_at),
        "updated_at": now,
    })


async def get_embed(guild_id: int, name: str) -> Optional[dict]:
    resp = await _t(T_EMBEDS).get_item(
        Key={"guild_id": guild_id, "name": name.lower()}
    )
    item = resp.get("Item")
    return _decode(item.get("data")) if item else None


async def get_all_embeds(guild_id: int) -> list[dict]:
    resp = await _t(T_EMBEDS).query(
        KeyConditionExpression=Key("guild_id").eq(guild_id),
        ProjectionExpression="#n, created_at, updated_at",
        ExpressionAttributeNames={"#n": "name"},
    )
    return _decode(sorted(resp.get("Items", []), key=lambda r: r.get("name", "")))


async def delete_embed(guild_id: int, name: str) -> bool:
    resp = await _t(T_EMBEDS).delete_item(
        Key={"guild_id": guild_id, "name": name.lower()},
        ReturnValues="ALL_OLD",
    )
    return "Attributes" in resp


async def count_server_embeds(guild_id: int) -> int:
    resp = await _t(T_EMBEDS).query(
        KeyConditionExpression=Key("guild_id").eq(guild_id),
        Select="COUNT",
    )
    return int(resp.get("Count", 0))


# =============================================================================
# Premium users
# =============================================================================

async def get_premium_tier(user_id: int) -> int:
    resp = await _t(T_PREMIUM_USERS).get_item(Key={"user_id": user_id})
    item = resp.get("Item")
    if not item:
        return 0
    expires = item.get("expires_at")
    if expires is not None and int(expires) < int(time.time()):
        return 0
    return int(item.get("tier", 0))


async def get_premium_info(user_id: int) -> Optional[dict]:
    resp = await _t(T_PREMIUM_USERS).get_item(Key={"user_id": user_id})
    item = resp.get("Item")
    return _decode(item) if item else None


async def set_premium(
    user_id: int, tier: int,
    expires_at: Optional[int] = None,
    patreon_id: Optional[str] = None,
    added_by: Optional[int] = None,
) -> None:
    fields = {"tier": tier, "expires_at": expires_at, "patreon_id": patreon_id}
    if added_by is not None:
        fields["added_by"] = added_by
    expr, names, values = _build_update(fields)
    # Stamp created_at on first insert via if_not_exists.
    expr += ", #ca = if_not_exists(#ca, :cav)"
    names["#ca"] = "created_at"
    values[":cav"] = int(time.time())
    await _t(T_PREMIUM_USERS).update_item(
        Key={"user_id": user_id},
        UpdateExpression=expr,
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
    )


async def remove_premium(user_id: int) -> bool:
    resp = await _t(T_PREMIUM_USERS).delete_item(
        Key={"user_id": user_id}, ReturnValues="ALL_OLD",
    )
    return "Attributes" in resp


async def get_all_premium() -> list[dict]:
    resp = await _t(T_PREMIUM_USERS).scan()
    items = _decode(resp.get("Items", []))
    items.sort(key=lambda r: (-int(r.get("tier", 0)), int(r.get("created_at", 0))))
    return items


# =============================================================================
# Blacklist
# =============================================================================

async def is_blacklisted(target_id: int) -> bool:
    resp = await _t(T_BLACKLIST).get_item(
        Key={"target_id": target_id},
        ProjectionExpression="target_id",
    )
    return "Item" in resp


async def add_blacklist(target_id: int, target_type: str, reason: str, added_by: int) -> None:
    await _t(T_BLACKLIST).put_item(Item={
        "target_id": target_id,
        "target_type": target_type,
        "reason": reason,
        "added_by": added_by,
        "added_at": int(time.time()),
    })


async def remove_blacklist(target_id: int) -> bool:
    resp = await _t(T_BLACKLIST).delete_item(
        Key={"target_id": target_id}, ReturnValues="ALL_OLD",
    )
    return "Attributes" in resp


async def get_blacklist_all() -> list[dict]:
    resp = await _t(T_BLACKLIST).scan()
    items = _decode(resp.get("Items", []))
    items.sort(key=lambda r: -int(r.get("added_at", 0)))
    return items


# =============================================================================
# Embed inventory  (per-user)
# =============================================================================

async def save_inventory_embed(user_id: int, name: str, data: dict) -> None:
    now = int(time.time())
    name_l = name.lower()
    existing = await _t(T_EMBED_INV).get_item(
        Key={"user_id": user_id, "name": name_l},
        ProjectionExpression="created_at",
    )
    created_at = existing.get("Item", {}).get("created_at", now)
    await _t(T_EMBED_INV).put_item(Item={
        "user_id": user_id,
        "name": name_l,
        "data": _encode(data),
        "created_at": int(created_at),
        "updated_at": now,
    })


async def get_inventory_embed(user_id: int, name: str) -> Optional[dict]:
    resp = await _t(T_EMBED_INV).get_item(
        Key={"user_id": user_id, "name": name.lower()}
    )
    item = resp.get("Item")
    return _decode(item.get("data")) if item else None


async def get_all_inventory_embeds(user_id: int) -> list[dict]:
    resp = await _t(T_EMBED_INV).query(
        KeyConditionExpression=Key("user_id").eq(user_id),
        ProjectionExpression="#n, created_at, updated_at",
        ExpressionAttributeNames={"#n": "name"},
    )
    return _decode(sorted(resp.get("Items", []), key=lambda r: r.get("name", "")))


async def delete_inventory_embed(user_id: int, name: str) -> bool:
    resp = await _t(T_EMBED_INV).delete_item(
        Key={"user_id": user_id, "name": name.lower()}, ReturnValues="ALL_OLD",
    )
    return "Attributes" in resp


async def count_inventory_embeds(user_id: int) -> int:
    resp = await _t(T_EMBED_INV).query(
        KeyConditionExpression=Key("user_id").eq(user_id),
        Select="COUNT",
    )
    return int(resp.get("Count", 0))


def get_cache_stats() -> dict:
    return cache.stats()


async def ping_db() -> None:
    """Cheap round-trip used by the /ping command to measure DB latency."""
    # GetItem on a non-existent key — tiny payload, exercises the connection.
    await _t(T_GUILD_CONFIGS).get_item(
        Key={"guild_id": 0}, ProjectionExpression="guild_id",
    )


# =============================================================================
# Autoresponders
# =============================================================================

async def create_autoresponder(
    guild_id: int, trigger: str, match_type: str,
    case_sensitive: bool, response: str, cooldown: int, created_by: int,
) -> str:
    ar_id = uuid.uuid4().hex
    now = int(time.time())
    await _t(T_ARS).put_item(Item={
        "guild_id": guild_id,
        "trigger": trigger,
        "id": ar_id,
        "match_type": match_type,
        "case_sensitive": int(bool(case_sensitive)),
        "response": response,
        "enabled": 1,
        "cooldown": int(cooldown),
        "use_count": 0,
        "created_by": created_by,
        "created_at": now,
        "updated_at": now,
    })
    return ar_id


async def get_autoresponder(guild_id: int, trigger: str) -> Optional[dict]:
    resp = await _t(T_ARS).get_item(Key={"guild_id": guild_id, "trigger": trigger})
    return _decode(resp.get("Item"))


async def get_autoresponder_by_id(ar_id: Any) -> Optional[dict]:
    """Linear scan — invoked rarely. Stored only for API compatibility."""
    resp = await _t(T_ARS).scan(
        FilterExpression=Key("id").eq(ar_id),
        Limit=1,
    )
    items = resp.get("Items", [])
    return _decode(items[0]) if items else None


async def get_all_autoresponders(guild_id: int) -> list[dict]:
    resp = await _t(T_ARS).query(KeyConditionExpression=Key("guild_id").eq(guild_id))
    items = _decode(resp.get("Items", []))
    items.sort(key=lambda r: r.get("trigger", ""))
    return items


async def update_autoresponder(ar_id: Any, **fields: Any) -> None:
    """Update an autoresponder by its opaque id (string UUID).

    Accepts the SQLite-era integer id only when the value can be looked up via
    a scan; new code paths pass the string UUID returned by ``create_autoresponder``.
    """
    target = await get_autoresponder_by_id(ar_id)
    if not target:
        return
    fields = dict(fields)
    fields["updated_at"] = int(time.time())
    if "case_sensitive" in fields:
        fields["case_sensitive"] = int(bool(fields["case_sensitive"]))
    if "enabled" in fields:
        fields["enabled"] = int(bool(fields["enabled"]))
    expr, names, values = _build_update(fields)
    await _t(T_ARS).update_item(
        Key={"guild_id": int(target["guild_id"]), "trigger": target["trigger"]},
        UpdateExpression=expr,
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
    )


async def delete_autoresponder(guild_id: int, trigger: str) -> bool:
    resp = await _t(T_ARS).delete_item(
        Key={"guild_id": guild_id, "trigger": trigger},
        ReturnValues="ALL_OLD",
    )
    return "Attributes" in resp


async def count_autoresponders(guild_id: int) -> int:
    resp = await _t(T_ARS).query(
        KeyConditionExpression=Key("guild_id").eq(guild_id),
        Select="COUNT",
    )
    return int(resp.get("Count", 0))


async def increment_ar_use_count(ar_id: Any) -> None:
    target = await get_autoresponder_by_id(ar_id)
    if not target:
        return
    await _t(T_ARS).update_item(
        Key={"guild_id": int(target["guild_id"]), "trigger": target["trigger"]},
        UpdateExpression="ADD use_count :one",
        ExpressionAttributeValues={":one": 1},
    )


async def check_ar_cooldown(guild_id: int, ar_id: Any, user_id: int, cooldown_secs: int) -> bool:
    """Returns True if user is on cooldown (skip), False if ok to fire."""
    if cooldown_secs <= 0:
        return False
    cd_key = f"{guild_id}#{user_id}#{ar_id}"
    now = int(time.time())
    resp = await _t(T_AR_COOLDOWNS).get_item(Key={"cooldown_key": cd_key})
    item = resp.get("Item")
    if item and (now - int(item.get("used_at", 0))) < cooldown_secs:
        return True
    await _t(T_AR_COOLDOWNS).put_item(Item={
        "cooldown_key": cd_key,
        "guild_id": guild_id,
        "ar_id": str(ar_id),
        "user_id": user_id,
        "used_at": now,
        # TTL attribute for any future TTL-enable on this table.
        "expires_at": now + max(cooldown_secs, 60) + 60,
    })
    return False


# =============================================================================
# Autoresponder inventory
# =============================================================================

async def save_ar_inventory(user_id: int, name: str, trigger: str, match_type: str, response: str) -> None:
    name_l = name.lower()
    now = int(time.time())
    existing = await _t(T_AR_INV).get_item(
        Key={"user_id": user_id, "name": name_l},
        ProjectionExpression="created_at",
    )
    created_at = existing.get("Item", {}).get("created_at", now)
    await _t(T_AR_INV).put_item(Item={
        "user_id": user_id,
        "name": name_l,
        "trigger": trigger,
        "match_type": match_type,
        "response": response,
        "created_at": int(created_at),
    })


async def get_ar_inventory(user_id: int, name: str) -> Optional[dict]:
    resp = await _t(T_AR_INV).get_item(Key={"user_id": user_id, "name": name.lower()})
    return _decode(resp.get("Item"))


async def get_all_ar_inventory(user_id: int) -> list[dict]:
    resp = await _t(T_AR_INV).query(KeyConditionExpression=Key("user_id").eq(user_id))
    items = _decode(resp.get("Items", []))
    items.sort(key=lambda r: r.get("name", ""))
    return items


async def delete_ar_inventory(user_id: int, name: str) -> bool:
    resp = await _t(T_AR_INV).delete_item(
        Key={"user_id": user_id, "name": name.lower()}, ReturnValues="ALL_OLD",
    )
    return "Attributes" in resp


async def count_ar_inventory(user_id: int) -> int:
    resp = await _t(T_AR_INV).query(
        KeyConditionExpression=Key("user_id").eq(user_id),
        Select="COUNT",
    )
    return int(resp.get("Count", 0))


# =============================================================================
# Buttons
# =============================================================================

async def save_button(
    guild_id: int, name: str, btn_type: str, label: str, style: str,
    created_by: int, emoji: Optional[str] = None,
    url: Optional[str] = None, response: Optional[str] = None,
) -> None:
    name_l = name.lower()
    now = int(time.time())
    existing = await _t(T_BUTTONS).get_item(
        Key={"guild_id": guild_id, "name": name_l},
        ProjectionExpression="created_at",
    )
    created_at = existing.get("Item", {}).get("created_at", now)
    item = _clean({
        "guild_id": guild_id,
        "name": name_l,
        "btn_type": btn_type,
        "label": label,
        "style": style,
        "emoji": emoji,
        "url": url,
        "response": response,
        "created_by": created_by,
        "created_at": int(created_at),
    })
    await _t(T_BUTTONS).put_item(Item=item)


async def get_button(guild_id: int, name: str) -> Optional[dict]:
    resp = await _t(T_BUTTONS).get_item(Key={"guild_id": guild_id, "name": name.lower()})
    return _decode(resp.get("Item"))


async def get_all_buttons(guild_id: int) -> list[dict]:
    resp = await _t(T_BUTTONS).query(KeyConditionExpression=Key("guild_id").eq(guild_id))
    items = _decode(resp.get("Items", []))
    items.sort(key=lambda r: r.get("name", ""))
    return items


async def delete_button(guild_id: int, name: str) -> bool:
    resp = await _t(T_BUTTONS).delete_item(
        Key={"guild_id": guild_id, "name": name.lower()}, ReturnValues="ALL_OLD",
    )
    return "Attributes" in resp


async def count_buttons(guild_id: int) -> int:
    resp = await _t(T_BUTTONS).query(
        KeyConditionExpression=Key("guild_id").eq(guild_id),
        Select="COUNT",
    )
    return int(resp.get("Count", 0))


# =============================================================================
# Button inventory
# =============================================================================

async def save_button_inventory(user_id: int, name: str, data: dict) -> None:
    name_l = name.lower()
    now = int(time.time())
    existing = await _t(T_BUTTON_INV).get_item(
        Key={"user_id": user_id, "name": name_l},
        ProjectionExpression="created_at",
    )
    created_at = existing.get("Item", {}).get("created_at", now)
    await _t(T_BUTTON_INV).put_item(Item={
        "user_id": user_id,
        "name": name_l,
        "data": _encode(data),
        "created_at": int(created_at),
    })


async def get_button_inventory(user_id: int, name: str) -> Optional[dict]:
    resp = await _t(T_BUTTON_INV).get_item(Key={"user_id": user_id, "name": name.lower()})
    item = resp.get("Item")
    return _decode(item.get("data")) if item else None


async def get_all_button_inventory(user_id: int) -> list[dict]:
    resp = await _t(T_BUTTON_INV).query(
        KeyConditionExpression=Key("user_id").eq(user_id),
        ProjectionExpression="#n, created_at",
        ExpressionAttributeNames={"#n": "name"},
    )
    return _decode(sorted(resp.get("Items", []), key=lambda r: r.get("name", "")))


async def delete_button_inventory(user_id: int, name: str) -> bool:
    resp = await _t(T_BUTTON_INV).delete_item(
        Key={"user_id": user_id, "name": name.lower()}, ReturnValues="ALL_OLD",
    )
    return "Attributes" in resp


# =============================================================================
# Server premium activations  (HASH user_id, GSI guild_id-index)
# =============================================================================

async def get_user_activation(user_id: int) -> Optional[dict]:
    resp = await _t(T_PREMIUM_ACT).get_item(Key={"user_id": user_id})
    return _decode(resp.get("Item"))


async def get_server_premium(guild_id: int) -> Optional[dict]:
    """Return highest-tier active activation for the given guild, or None."""
    now = int(time.time())
    resp = await _t(T_PREMIUM_ACT).query(
        IndexName="guild_id-index",
        KeyConditionExpression=Key("guild_id").eq(guild_id),
    )
    active = [r for r in resp.get("Items", []) if int(r.get("expires_at", 0)) > now]
    if not active:
        return None
    active.sort(key=lambda r: -int(r.get("tier", 0)))
    return _decode(active[0])


async def get_server_premium_tier(guild_id: int) -> int:
    """Effective tier — capped by activator's *current* member tier."""
    info = await get_server_premium(guild_id)
    if not info:
        return 0
    member_tier = await get_premium_tier(int(info["user_id"]))
    return min(int(info["tier"]), int(member_tier))


async def get_server_activations(guild_id: int) -> list[dict]:
    now = int(time.time())
    resp = await _t(T_PREMIUM_ACT).query(
        IndexName="guild_id-index",
        KeyConditionExpression=Key("guild_id").eq(guild_id),
    )
    items = [r for r in resp.get("Items", []) if int(r.get("expires_at", 0)) > now]
    return _decode(items)


async def activate_server_premium(user_id: int, guild_id: int, tier: int) -> dict:
    now = int(time.time())
    expires = now + ACTIVATION_DURATION_S
    await _t(T_PREMIUM_ACT).put_item(Item={
        "user_id": user_id,
        "guild_id": guild_id,
        "tier": int(tier),
        "activated_at": now,
        "last_switched_at": now,
        "expires_at": expires,
    })
    return {"guild_id": guild_id, "tier": int(tier), "activated_at": now,
            "last_switched_at": now, "expires_at": expires}


async def switch_server_premium(user_id: int, new_guild_id: int, tier: int) -> dict:
    now = int(time.time())
    expires = now + ACTIVATION_DURATION_S
    await _t(T_PREMIUM_ACT).update_item(
        Key={"user_id": user_id},
        UpdateExpression=(
            "SET guild_id = :g, tier = :t, activated_at = :a, "
            "last_switched_at = :s, expires_at = :e"
        ),
        ExpressionAttributeValues={
            ":g": new_guild_id, ":t": int(tier),
            ":a": now, ":s": now, ":e": expires,
        },
    )
    return {"guild_id": new_guild_id, "tier": int(tier), "activated_at": now,
            "last_switched_at": now, "expires_at": expires}


async def remove_server_premium(user_id: int) -> None:
    await _t(T_PREMIUM_ACT).delete_item(Key={"user_id": user_id})


# =============================================================================
# Guild custom assets (palette / emojis)
# =============================================================================

async def get_guild_palette(guild_id: int) -> Optional[dict]:
    resp = await _t(T_GUILD_ASSETS).get_item(
        Key={"guild_id": guild_id}, ProjectionExpression="palette",
    )
    item = resp.get("Item")
    palette = item.get("palette") if item else None
    return _decode(palette) if palette else None


async def set_guild_palette(guild_id: int, palette: dict) -> None:
    await _t(T_GUILD_ASSETS).update_item(
        Key={"guild_id": guild_id},
        UpdateExpression="SET palette = :p, updated_at = :u",
        ExpressionAttributeValues={":p": _encode(palette), ":u": int(time.time())},
    )


async def reset_guild_palette(guild_id: int) -> None:
    await _t(T_GUILD_ASSETS).update_item(
        Key={"guild_id": guild_id},
        UpdateExpression="REMOVE palette SET updated_at = :u",
        ExpressionAttributeValues={":u": int(time.time())},
    )


async def get_guild_emojis(guild_id: int) -> Optional[dict]:
    resp = await _t(T_GUILD_ASSETS).get_item(
        Key={"guild_id": guild_id}, ProjectionExpression="emojis",
    )
    item = resp.get("Item")
    emojis = item.get("emojis") if item else None
    return _decode(emojis) if emojis else None


async def set_guild_emojis(guild_id: int, emojis: dict) -> None:
    await _t(T_GUILD_ASSETS).update_item(
        Key={"guild_id": guild_id},
        UpdateExpression="SET emojis = :e, updated_at = :u",
        ExpressionAttributeValues={":e": _encode(emojis), ":u": int(time.time())},
    )


async def reset_guild_emojis(guild_id: int) -> None:
    await _t(T_GUILD_ASSETS).update_item(
        Key={"guild_id": guild_id},
        UpdateExpression="REMOVE emojis SET updated_at = :u",
        ExpressionAttributeValues={":u": int(time.time())},
    )
