"""
One-shot migration: SQLite (bot.db) → DynamoDB (eu-central-1).

Reads from the old `bot.db` using the original SQLite schema and writes each
row into its corresponding `nana_*` DynamoDB table using the new database
module's public API where possible (so we exercise the same write paths the
running bot will use).

Run from the discord-bot directory:
    python scripts/migrate_to_dynamo.py
"""

import asyncio
import json
import logging
import os
import sqlite3
import sys
import time
from decimal import Decimal

# Ensure imports work when run from project root or scripts/.
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import aioboto3
from boto3.dynamodb.conditions import Key

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("migrate")

REGION = os.environ.get("AWS_REGION", "eu-central-1")
DB_PATH = os.path.join(ROOT, "bot.db")


def _load_sqlite() -> dict[str, list[dict]]:
    if not os.path.exists(DB_PATH):
        log.error("SQLite file not found: %s", DB_PATH)
        return {}
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    out: dict[str, list[dict]] = {}
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
    for (t,) in cur.fetchall():
        if t.startswith("sqlite_"):
            continue
        rows = [dict(r) for r in conn.execute(f"SELECT * FROM {t}").fetchall()]
        out[t] = rows
    conn.close()
    return out


def _enc(v):
    if isinstance(v, float):
        return Decimal(str(v))
    return v


def _clean(item: dict) -> dict:
    return {k: _enc(v) for k, v in item.items() if v is not None}


async def _migrate(ddb, src: dict[str, list[dict]]) -> None:
    plan = [
        # (sqlite_table, dynamo_table, transform)
        ("guild_configs",    "nana_guild_configs",     None),
        ("antinuke_whitelist","nana_antinuke_whitelist", None),
        ("warnings",         "nana_warnings",          _xform_warning),
        ("mod_actions",      "nana_mod_actions",       _xform_with_ms_created_at),
        ("antinuke_logs",    "nana_antinuke_logs",     _xform_with_ms_created_at),
        ("embeds",           "nana_embeds",            _xform_json_data),
        ("premium_users",    "nana_premium_users",     None),
        ("premium_activations","nana_premium_activations", None),
        ("guild_assets",     "nana_guild_assets",      _xform_assets),
        ("blacklist",        "nana_blacklist",         None),
        ("embed_inventory",  "nana_embed_inventory",   _xform_json_data),
        ("autoresponders",   "nana_autoresponders",    _xform_autoresponder),
        ("ar_inventory",     "nana_ar_inventory",      None),
        ("ar_cooldowns",     "nana_ar_cooldowns",      _xform_ar_cooldown),
        ("buttons",          "nana_buttons",           None),
        ("button_inventory", "nana_button_inventory",  _xform_json_data),
    ]
    total = 0
    for src_t, dst_t, xform in plan:
        rows = src.get(src_t, [])
        if not rows:
            log.info("  %-20s → %-26s : 0 rows (skip)", src_t, dst_t)
            continue
        table = await ddb.Table(dst_t)
        written = 0
        async with table.batch_writer() as bw:
            for row in rows:
                # Strip auto-increment id from SQLite (Dynamo schema has different keys).
                row = {k: v for k, v in row.items() if k != "id"}
                if xform:
                    item = xform(row)
                    if item is None:
                        continue
                else:
                    item = _clean(row)
                if not item:
                    continue
                await bw.put_item(Item=item)
                written += 1
        log.info("  %-20s → %-26s : %d rows", src_t, dst_t, written)
        total += written
    log.info("Migration complete. Total rows written: %d", total)


def _xform_warning(row: dict) -> dict:
    return {
        "guild_user": f"{row['guild_id']}#{row['user_id']}",
        "created_at": int(row.get("created_at", time.time())) * 1000,
        "guild_id": str(row["guild_id"]),  # GSI indexed as String
        "user_id": row["user_id"],
        "moderator_id": row["moderator_id"],
        "reason": row.get("reason", ""),
    }


def _xform_with_ms_created_at(row: dict) -> dict:
    item = _clean(row)
    if "created_at" in item:
        item["created_at"] = int(item["created_at"]) * 1000
    return item


def _xform_json_data(row: dict):
    """For tables that store a JSON blob in `data` column → unpack to map."""
    item = _clean(row)
    raw = item.get("data")
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            # Convert any floats inside to Decimal.
            item["data"] = _deep_enc(parsed)
        except json.JSONDecodeError:
            pass
    return item


def _deep_enc(o):
    if isinstance(o, list):
        return [_deep_enc(x) for x in o]
    if isinstance(o, dict):
        return {k: _deep_enc(v) for k, v in o.items()}
    if isinstance(o, float):
        return Decimal(str(o))
    return o


def _xform_assets(row: dict):
    item = {"guild_id": row["guild_id"]}
    for col in ("palette", "emojis"):
        v = row.get(col)
        if isinstance(v, str):
            try:
                item[col] = _deep_enc(json.loads(v))
            except json.JSONDecodeError:
                pass
    if "updated_at" in row and row["updated_at"] is not None:
        item["updated_at"] = row["updated_at"]
    return item if len(item) > 1 else None


def _xform_autoresponder(row: dict):
    import uuid as _uuid
    item = _clean(row)
    item["id"] = _uuid.uuid4().hex
    return item


def _xform_ar_cooldown(row: dict):
    return {
        "cooldown_key": f"{row['guild_id']}#{row['user_id']}#{row['ar_id']}",
        "guild_id": row["guild_id"],
        "ar_id": str(row["ar_id"]),
        "user_id": row["user_id"],
        "used_at": row["used_at"],
    }


async def main() -> None:
    log.info("Region: %s", REGION)
    log.info("Source DB: %s", DB_PATH)
    src = _load_sqlite()
    if not src:
        log.warning("No source data — exiting.")
        return
    counts = {t: len(rows) for t, rows in src.items() if rows}
    log.info("SQLite tables with data: %s", counts)

    session = aioboto3.Session()
    async with session.resource("dynamodb", region_name=REGION) as ddb:
        await _migrate(ddb, src)


if __name__ == "__main__":
    asyncio.run(main())
