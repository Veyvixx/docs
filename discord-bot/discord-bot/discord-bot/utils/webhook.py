"""
Patreon webhook server — runs alongside the Discord bot inside the same asyncio loop.
Handles pledge create/update/delete events, grants/revokes premium, DMs users.

Patreon → signature: MD5 HMAC of raw body with webhook secret (X-Patreon-Signature header).
Tier mapping (by entitled amount):
    ≥ $20 → Tier 3 (Butterflies)
    ≥ $10 → Tier 2 (Tulip)
    ≥ $3  → Tier 1 (Heart)
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
from typing import Optional

import aiohttp
from aiohttp import web

log = logging.getLogger("webhook")

DEV_GUILD_ID = 1492674952447393913


def _verify_signature(body: bytes, signature: str, secret: str) -> bool:
    expected = hmac.new(secret.encode(), body, hashlib.md5).hexdigest()
    return hmac.compare_digest(expected, signature)


def _amount_to_tier(amount_cents: int) -> int:
    if amount_cents >= 2000:
        return 3
    if amount_cents >= 1000:
        return 2
    if amount_cents >= 300:
        return 1
    return 0


def _tier_name(config: dict, tier: int) -> str:
    return config.get("premium", {}).get("tiers", {}).get(str(tier), {}).get("name", f"Tier {tier}")


async def _send_premium_dm(bot, user_id: int, tier: int, expires_at: Optional[int]) -> bool:
    try:
        user = bot.get_user(user_id) or await bot.fetch_user(user_id)
    except Exception:
        return False

    config = bot.config
    pe = config.get("premium", {}).get("emojis", {})
    heart = pe.get("heart", "")
    tulip = pe.get("tulip", "")
    support = config.get("premium", {}).get("support_server", "")
    name = _tier_name(config, tier)
    billing = f"<t:{expires_at}:F>" if expires_at else "managed via Patreon"

    embed = __import__("discord").Embed(
        description=(
            f"## {heart}   thank you for subscribing !\n"
            f"- you're on the **{name}** tier\n"
            f"- to receive your role, please join the [support server]({support})\n\n"
            f"- your next billing date: {billing}\n"
            f"- you can manage your subscriptions at `/premium`\n"
            f"- thank you for your support {tulip}"
        ),
        color=bot.ef.colors["success"],
    )
    try:
        await user.send(embed=embed)
        return True
    except Exception:
        return False


async def _send_revoke_dm(bot, user_id: int) -> bool:
    try:
        user = bot.get_user(user_id) or await bot.fetch_user(user_id)
    except Exception:
        return False

    config = bot.config
    pe = config.get("premium", {}).get("emojis", {})
    heart = pe.get("heart", "")

    embed = __import__("discord").Embed(
        description=(
            f"## {heart}   your subscription has ended\n"
            f"- your premium access has been removed\n"
            f"- we hope to see you again soon!\n"
            f"- you can re-subscribe at any time with `/premium`"
        ),
        color=bot.ef.colors["secondary"],
    )
    try:
        await user.send(embed=embed)
        return True
    except Exception:
        return False


def _extract_discord_id(payload: dict) -> Optional[int]:
    included = payload.get("included", [])
    for obj in included:
        if obj.get("type") != "user":
            continue
        social = obj.get("attributes", {}).get("social_connections", {})
        discord_info = social.get("discord")
        if discord_info and discord_info.get("user_id"):
            try:
                return int(discord_info["user_id"])
            except (ValueError, TypeError):
                pass
    return None


def _extract_expires(attributes: dict) -> Optional[int]:
    next_charge = attributes.get("next_charge_date")
    if not next_charge:
        return None
    try:
        import datetime
        dt = datetime.datetime.fromisoformat(next_charge.replace("Z", "+00:00"))
        return int(dt.timestamp())
    except Exception:
        return None


def create_webhook_app(bot) -> web.Application:
    secret = os.environ.get("PATREON_WEBHOOK_SECRET", "")

    async def handle_webhook(request: web.Request) -> web.Response:
        body = await request.read()

        if secret:
            sig = request.headers.get("X-Patreon-Signature", "")
            if not _verify_signature(body, sig, secret):
                log.warning("Patreon webhook: invalid signature — rejected")
                return web.Response(status=401, text="Invalid signature")

        event_type = request.headers.get("X-Patreon-Event", "")
        log.info("Patreon webhook received: %s", event_type)

        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return web.Response(status=400, text="Invalid JSON")

        data = payload.get("data", {})
        attributes = data.get("attributes", {})
        patron_status = attributes.get("patron_status", "")
        amount_cents = attributes.get("currently_entitled_amount_cents", 0)
        patreon_user_id = data.get("relationships", {}).get("user", {}).get("data", {}).get("id")

        discord_user_id = _extract_discord_id(payload)

        if event_type in ("members:pledge:create", "members:pledge:update"):
            if patron_status == "active_patron" and discord_user_id:
                tier = _amount_to_tier(amount_cents)
                if tier > 0:
                    expires_at = _extract_expires(attributes)
                    from utils.database import set_premium
                    await set_premium(
                        discord_user_id,
                        tier,
                        expires_at=expires_at,
                        patreon_id=str(patreon_user_id) if patreon_user_id else None,
                        added_by=None,
                    )
                    log.info(
                        "Granted Tier %d premium to Discord user %d (Patreon ID: %s)",
                        tier, discord_user_id, patreon_user_id,
                    )
                    if event_type == "members:pledge:create":
                        await _send_premium_dm(bot, discord_user_id, tier, expires_at)

        elif event_type == "members:pledge:delete":
            if discord_user_id:
                from utils.database import remove_premium
                removed = await remove_premium(discord_user_id)
                if removed:
                    log.info("Revoked premium for Discord user %d", discord_user_id)
                    await _send_revoke_dm(bot, discord_user_id)

        return web.Response(status=200, text="OK")

    async def handle_health(request: web.Request) -> web.Response:
        return web.Response(status=200, text="Nana webhook server running")

    app = web.Application()
    app.router.add_post("/patreon/webhook", handle_webhook)
    app.router.add_get("/", handle_health)
    return app


async def start_webhook_server(bot) -> None:
    port = int(os.environ.get("PORT", "5000"))
    app = create_webhook_app(bot)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info("Patreon webhook server listening on port %d  →  POST /patreon/webhook", port)
