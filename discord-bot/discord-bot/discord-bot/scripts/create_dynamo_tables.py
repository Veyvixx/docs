"""
Create the DynamoDB tables Nana needs for the new automod / giveaway features.

Idempotent: skips any table that already exists. Run once after pulling the
new code, then start the bot:

    python scripts/create_dynamo_tables.py

Region & credentials come from the standard AWS env vars
(AWS_REGION, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY).
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

import aioboto3
from botocore.exceptions import ClientError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("create-tables")

REGION = os.environ.get("AWS_REGION", "eu-central-1")

# Each entry: (table_name, [{name, type}], [{name, key_type}])
SPECS = [
    {
        "name": "nana_automod",
        "attrs": [{"AttributeName": "guild_id", "AttributeType": "N"},
                  {"AttributeName": "preset",   "AttributeType": "S"}],
        "keys":  [{"AttributeName": "guild_id", "KeyType": "HASH"},
                  {"AttributeName": "preset",   "KeyType": "RANGE"}],
    },
    {
        "name": "nana_giveaways",
        "attrs": [{"AttributeName": "guild_id",   "AttributeType": "N"},
                  {"AttributeName": "message_id", "AttributeType": "N"}],
        "keys":  [{"AttributeName": "guild_id",   "KeyType": "HASH"},
                  {"AttributeName": "message_id", "KeyType": "RANGE"}],
    },
    {
        "name": "nana_gw_templates",
        "attrs": [{"AttributeName": "guild_id", "AttributeType": "N"},
                  {"AttributeName": "name",     "AttributeType": "S"}],
        "keys":  [{"AttributeName": "guild_id", "KeyType": "HASH"},
                  {"AttributeName": "name",     "KeyType": "RANGE"}],
    },
]


async def main() -> None:
    session = aioboto3.Session()
    async with session.client("dynamodb", region_name=REGION) as client:
        # List existing tables (paginated)
        existing: set[str] = set()
        paginator = client.get_paginator("list_tables")
        async for page in paginator.paginate():
            for t in page.get("TableNames", []):
                existing.add(t)

        for spec in SPECS:
            name = spec["name"]
            if name in existing:
                log.info("✓ %s already exists — skipping", name)
                continue
            try:
                await client.create_table(
                    TableName=name,
                    AttributeDefinitions=spec["attrs"],
                    KeySchema=spec["keys"],
                    BillingMode="PAY_PER_REQUEST",
                )
                log.info("⏳ creating %s …", name)
                waiter = client.get_waiter("table_exists")
                await waiter.wait(TableName=name)
                log.info("✓ %s is ACTIVE", name)
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code", "?")
                log.error("× failed to create %s (%s): %s", name, code, exc)
                sys.exit(1)
        log.info("All tables ready.")


if __name__ == "__main__":
    asyncio.run(main())
