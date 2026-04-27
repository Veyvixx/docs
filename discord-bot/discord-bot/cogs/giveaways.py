"""
Giveaway system with templates.

Slash group: ``/gw``
    start            Begin a new giveaway (rich option set; supports template:NAME).
    end              End an active giveaway early and roll winners.
    reroll           Roll new winners on a finished giveaway.
    cancel           Cancel without picking winners.
    list             List active giveaways in this server.
    pause / resume   Pause and resume the countdown.
    edit             Edit prize / winners / duration / image of an active gw.
    template save    Save the current option set as a named preset.
    template load    Show a preset.
    template list    List saved presets.
    template delete  Remove a saved preset.

Premium gating
    Free       — max 2 concurrent giveaways, no required/blacklist/bonus roles,
                 no min-account / min-join requirements, no templates.
    Premium    — unlimited concurrent + every advanced option.

Persistence
    Active giveaways live in ``nana_giveaways`` (HASH guild_id, RANGE message_id).
    A persistent ``GiveawayView`` is registered at cog load; restarts resume.

Ticker
    A ``tasks.loop(seconds=15)`` scans for due giveaways and ends them.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
from datetime import datetime, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from utils.embeds import EmbedFactory
from utils.helpers import parse_duration, format_duration
from utils.database import (
    create_giveaway,
    get_giveaway,
    update_giveaway,
    list_giveaways,
    count_active_giveaways,
    scan_active_giveaways,
    toggle_giveaway_entry,
    delete_giveaway,
    save_gw_template,
    get_gw_template,
    list_gw_templates,
    delete_gw_template,
    count_gw_templates,
    get_server_premium_tier,
)

logger = logging.getLogger(__name__)


FREE_MAX_CONCURRENT = 2
TEMPLATE_LIMIT_FREE = 0
TEMPLATE_LIMIT_PREM = 25

GIVEAWAY_EMOJI = "🎉"


# ─────────────────────────── parsers ─────────────────────────────────────

def _parse_role_list(guild: discord.Guild, raw: Optional[str]) -> list[int]:
    if not raw:
        return []
    out: list[int] = []
    for piece in re.split(r"[,\s]+", raw.strip()):
        if not piece:
            continue
        m = re.search(r"\d{15,}", piece)
        if not m:
            continue
        rid = int(m.group(0))
        if guild.get_role(rid):
            out.append(rid)
    return out


def _parse_bonus_roles(guild: discord.Guild, raw: Optional[str]) -> dict[str, int]:
    """Parse 'role:weight,role:weight'. Weight = extra entries (1-10)."""
    if not raw:
        return {}
    out: dict[str, int] = {}
    for piece in raw.split(","):
        piece = piece.strip()
        if ":" not in piece:
            continue
        ref, w = piece.rsplit(":", 1)
        m = re.search(r"\d{15,}", ref)
        if not m:
            continue
        rid = int(m.group(0))
        if not guild.get_role(rid):
            continue
        try:
            weight = max(1, min(10, int(w.strip())))
        except ValueError:
            continue
        out[str(rid)] = weight
    return out


def _user_weight(member: discord.Member, bonus_roles: dict) -> int:
    """1 + sum of bonus weights for any matching role."""
    if not bonus_roles:
        return 1
    total = 1
    member_role_ids = {r.id for r in member.roles}
    for rid_str, weight in bonus_roles.items():
        try:
            if int(rid_str) in member_role_ids:
                total += int(weight)
        except (ValueError, TypeError):
            continue
    return total


def _eligible(member: discord.Member, gw: dict, now_ts: int) -> tuple[bool, str]:
    required = [int(r) for r in (gw.get("required_roles") or [])]
    blacklisted = [int(r) for r in (gw.get("blacklisted_roles") or [])]
    member_role_ids = {r.id for r in member.roles}
    if required and not (set(required) & member_role_ids):
        return False, "You don't have any of the required roles."
    if blacklisted and (set(blacklisted) & member_role_ids):
        return False, "One of your roles is blacklisted from this giveaway."
    min_account = int(gw.get("min_account_age_s") or 0)
    if min_account and (now_ts - int(member.created_at.timestamp())) < min_account:
        return False, "Your account isn't old enough to enter."
    min_join = int(gw.get("min_join_age_s") or 0)
    if min_join and member.joined_at:
        if (now_ts - int(member.joined_at.timestamp())) < min_join:
            return False, "You haven't been in the server long enough to enter."
    return True, ""


# ─────────────────────────── persistent view ─────────────────────────────


class GiveawayView(discord.ui.View):
    """Single persistent view shared by every giveaway message.

    The button's custom_id is the static ``gw:join`` string; per-message state
    is read from ``interaction.message`` and the DynamoDB row.
    """

    def __init__(self, ef: Optional[EmbedFactory] = None):
        super().__init__(timeout=None)
        self.ef = ef

    @discord.ui.button(
        label="Join", style=discord.ButtonStyle.primary,
        emoji=GIVEAWAY_EMOJI, custom_id="gw:join",
    )
    async def join(self, interaction: discord.Interaction, _: discord.ui.Button):
        ef: EmbedFactory = interaction.client.ef  # type: ignore[attr-defined]
        if not interaction.guild or not interaction.message:
            return await interaction.response.send_message(
                embed=ef.error("Giveaway buttons only work in a server."), ephemeral=True,
            )
        gw = await get_giveaway(interaction.guild.id, interaction.message.id)
        if not gw:
            return await interaction.response.send_message(
                embed=ef.error("This giveaway is no longer tracked."), ephemeral=True,
            )
        if int(gw.get("ended", 0)):
            return await interaction.response.send_message(
                embed=ef.info("This giveaway has already ended."), ephemeral=True,
            )
        if int(gw.get("paused", 0)):
            return await interaction.response.send_message(
                embed=ef.info("This giveaway is paused — entries are closed for now."), ephemeral=True,
            )
        member = interaction.guild.get_member(interaction.user.id)
        if not isinstance(member, discord.Member):
            return await interaction.response.send_message(
                embed=ef.error("Could not resolve your server profile."), ephemeral=True,
            )
        ok, why = _eligible(member, gw, int(datetime.now(timezone.utc).timestamp()))
        if not ok:
            return await interaction.response.send_message(
                embed=ef.error(why), ephemeral=True,
            )
        action, total = await toggle_giveaway_entry(
            interaction.guild.id, interaction.message.id, interaction.user.id,
        )
        if action == "joined":
            txt = f"You're in! There are now **{total}** entries."
        elif action == "left":
            txt = f"You left the giveaway. There are now **{total}** entries."
        else:
            txt = "Entries are not open right now."
        await interaction.response.send_message(embed=ef.info(txt), ephemeral=True)


# ─────────────────────────── embed builder ───────────────────────────────


def _build_giveaway_embed(ef: EmbedFactory, gw: dict, *, finished: bool = False, winners: Optional[list[int]] = None) -> discord.Embed:
    title = f"{GIVEAWAY_EMOJI}  {gw.get('prize', 'Giveaway')}"
    color_key = "secondary" if finished else "accent"
    desc_lines: list[str] = []
    if gw.get("description"):
        desc_lines.append(str(gw["description"]))
        desc_lines.append("")

    ends_at = int(gw.get("ends_at", 0))
    if finished:
        desc_lines.append(f"**Ended:** <t:{ends_at}:R>")
    elif int(gw.get("paused", 0)):
        remaining = int(gw.get("paused_remaining") or 0)
        desc_lines.append(f"⏸️ **Paused** — {format_duration_safe(remaining)} remaining when resumed.")
    else:
        desc_lines.append(f"**Ends:** <t:{ends_at}:R>  ·  <t:{ends_at}:F>")

    desc_lines.append(f"**Hosted by:** <@{int(gw.get('host_id', 0))}>")
    desc_lines.append(f"**Winners:** {int(gw.get('winners', 1))}")
    entries = list(gw.get("entries", []) or [])
    desc_lines.append(f"**Entries:** {len(entries)}")

    extras: list[str] = []
    req = gw.get("required_roles") or []
    if req:
        extras.append("Required roles: " + " ".join(f"<@&{int(r)}>" for r in req[:10]))
    bl = gw.get("blacklisted_roles") or []
    if bl:
        extras.append("Blacklisted: " + " ".join(f"<@&{int(r)}>" for r in bl[:10]))
    bonus = gw.get("bonus_roles") or {}
    if bonus:
        extras.append("Bonus entries: " + " ".join(
            f"<@&{int(k)}> +{int(v)}" for k, v in list(bonus.items())[:10]
        ))
    if int(gw.get("min_account_age_s") or 0):
        extras.append(f"Min account age: {format_duration_safe(int(gw['min_account_age_s']))}")
    if int(gw.get("min_join_age_s") or 0):
        extras.append(f"Min server tenure: {format_duration_safe(int(gw['min_join_age_s']))}")
    if extras:
        desc_lines.append("")
        desc_lines.extend(extras)

    if finished and winners is not None:
        desc_lines.append("")
        if winners:
            desc_lines.append("**Winners:** " + ", ".join(f"<@{w}>" for w in winners))
        else:
            desc_lines.append("**No eligible winners.**")

    e = ef.build(title=title, description="\n".join(desc_lines), color_key=color_key)
    if gw.get("image"):
        e.set_image(url=str(gw["image"]))
    return e


def format_duration_safe(seconds: int) -> str:
    from datetime import timedelta
    if seconds <= 0:
        return "0s"
    return format_duration(timedelta(seconds=seconds))


def _pick_winners(gw: dict, members: dict[int, discord.Member]) -> list[int]:
    entries = [int(x) for x in (gw.get("entries", []) or [])]
    bonus = gw.get("bonus_roles") or {}
    pool: list[int] = []
    for uid in entries:
        m = members.get(uid)
        if m is None:
            continue
        weight = _user_weight(m, bonus)
        pool.extend([uid] * weight)
    if not pool:
        return []
    n = max(1, int(gw.get("winners", 1)))
    winners: list[int] = []
    seen: set[int] = set()
    random.shuffle(pool)
    for uid in pool:
        if uid in seen:
            continue
        winners.append(uid)
        seen.add(uid)
        if len(winners) >= n:
            break
    return winners


# ─────────────────────────── cog ─────────────────────────────────────────


class Giveaways(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.ef: EmbedFactory = bot.ef  # type: ignore[attr-defined]
        bot.add_view(GiveawayView(self.ef))
        self.ticker.start()

    def cog_unload(self) -> None:
        self.ticker.cancel()

    # ── helpers ──────────────────────────────────────────────────────────

    async def _is_premium(self, guild_id: int) -> bool:
        return await get_server_premium_tier(guild_id) >= 1

    async def _resolve_message(self, guild: discord.Guild, gw: dict) -> Optional[discord.Message]:
        ch = guild.get_channel(int(gw.get("channel_id", 0)))
        if not isinstance(ch, (discord.TextChannel, discord.Thread)):
            return None
        try:
            return await ch.fetch_message(int(gw["message_id"]))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None

    async def _finish_giveaway(self, guild: discord.Guild, gw: dict, *, reason: str = "ended") -> list[int]:
        """Mark a giveaway ended, pick winners, edit the original message,
        and announce winners in the same channel. Returns the winner ID list."""
        members: dict[int, discord.Member] = {}
        for uid in (gw.get("entries") or []):
            uid_i = int(uid)
            m = guild.get_member(uid_i)
            if m:
                members[uid_i] = m
        winners = _pick_winners(gw, members) if reason != "cancelled" else []
        await update_giveaway(
            guild.id, int(gw["message_id"]),
            ended=1,
            ended_at=int(datetime.now(timezone.utc).timestamp()),
            winners_picked=winners,
        )
        gw["ended"] = 1
        msg = await self._resolve_message(guild, gw)
        if msg:
            view = discord.ui.View()
            try:
                if reason == "cancelled":
                    e = self.ef.build(
                        title=f"{GIVEAWAY_EMOJI}  {gw.get('prize', 'Giveaway')}  ·  Cancelled",
                        description="This giveaway was cancelled.",
                        color_key="warning",
                    )
                    await msg.edit(embed=e, view=view)
                else:
                    e = _build_giveaway_embed(self.ef, gw, finished=True, winners=winners)
                    await msg.edit(embed=e, view=view)
            except (discord.Forbidden, discord.HTTPException):
                pass
            if reason != "cancelled":
                announce = (
                    f"{GIVEAWAY_EMOJI} Congratulations " +
                    ", ".join(f"<@{w}>" for w in winners) +
                    f"! You won **{gw.get('prize', '')}**.\n{msg.jump_url}"
                ) if winners else (
                    f"No one entered or no eligible entries for **{gw.get('prize', '')}**. "
                    f"{msg.jump_url}"
                )
                try:
                    await msg.channel.send(announce, allowed_mentions=discord.AllowedMentions(users=True))
                except (discord.Forbidden, discord.HTTPException):
                    pass
        return winners

    # ── ticker ───────────────────────────────────────────────────────────

    @tasks.loop(seconds=15)
    async def ticker(self) -> None:
        try:
            now = int(datetime.now(timezone.utc).timestamp())
            active = await scan_active_giveaways()
            for gw in active:
                if int(gw.get("paused", 0)):
                    continue
                if int(gw.get("ends_at", 0)) > now:
                    continue
                guild = self.bot.get_guild(int(gw["guild_id"]))
                if not guild:
                    continue
                try:
                    await self._finish_giveaway(guild, gw, reason="ended")
                except Exception:
                    logger.exception("ticker: failed to finish giveaway %s in %s",
                                     gw.get("message_id"), gw.get("guild_id"))
        except Exception:
            logger.exception("Giveaway ticker tick failed")

    @ticker.before_loop
    async def _before_ticker(self):
        await self.bot.wait_until_ready()

    # ── /gw group ────────────────────────────────────────────────────────

    gw_group = app_commands.Group(
        name="gw",
        description="Run giveaways with optional templates.",
        default_permissions=discord.Permissions(manage_guild=True),
    )

    # ── /gw start ────────────────────────────────────────────────────────

    @gw_group.command(name="start", description="Start a new giveaway.")
    @app_commands.describe(
        prize="What members can win.",
        duration="How long the giveaway runs (e.g. 30m, 2h, 1d).",
        winners="Number of winners (1-50).",
        channel="Channel to post in (defaults to the current channel).",
        host="Show this user as the host (defaults to you).",
        ping_role="Role to ping in the announcement.",
        image="Image URL to display.",
        description="Extra details about the giveaway.",
        button_label="Override the join button label.",
        button_emoji="Override the join button emoji.",
        required_roles="Comma-separated role IDs/mentions a user must have. ✨",
        blacklisted_roles="Comma-separated role IDs/mentions that ban entry. ✨",
        bonus_roles="role:weight pairs, e.g. '@VIP:3,@Booster:2'. ✨",
        min_account_age="Min account age (e.g. 7d). ✨",
        min_join_age="Min server tenure (e.g. 1d). ✨",
        template="Load defaults from a saved template (overridable).",
    )
    async def gw_start(
        self, interaction: discord.Interaction,
        prize: Optional[str] = None,
        duration: Optional[str] = None,
        winners: app_commands.Range[int, 1, 50] = 1,
        channel: Optional[discord.TextChannel] = None,
        host: Optional[discord.Member] = None,
        ping_role: Optional[discord.Role] = None,
        image: Optional[str] = None,
        description: Optional[str] = None,
        button_label: Optional[app_commands.Range[str, 1, 80]] = None,
        button_emoji: Optional[str] = None,
        required_roles: Optional[str] = None,
        blacklisted_roles: Optional[str] = None,
        bonus_roles: Optional[str] = None,
        min_account_age: Optional[str] = None,
        min_join_age: Optional[str] = None,
        template: Optional[str] = None,
    ):
        assert interaction.guild
        await interaction.response.defer(ephemeral=False)
        is_premium = await self._is_premium(interaction.guild.id)

        # Concurrency cap (free tier).
        if not is_premium:
            active = await count_active_giveaways(interaction.guild.id)
            if active >= FREE_MAX_CONCURRENT:
                return await interaction.followup.send(embed=self.ef.error(
                    f"Free servers can run up to **{FREE_MAX_CONCURRENT}** concurrent giveaways. "
                    f"Upgrade to **Nana Premium** for unlimited."
                ))

        # Load template if given.
        tpl: dict = {}
        if template:
            tpl = await get_gw_template(interaction.guild.id, template) or {}
            if not tpl:
                return await interaction.followup.send(
                    embed=self.ef.error(f"No template named `{template}`.")
                )

        prize         = prize         or tpl.get("prize")
        duration      = duration      or tpl.get("duration")
        winners       = winners       if winners != 1 else int(tpl.get("winners") or 1)
        description   = description   or tpl.get("description")
        button_label  = button_label  or tpl.get("button_label")
        button_emoji  = button_emoji  or tpl.get("button_emoji")
        image         = image         or tpl.get("image")
        required_str    = required_roles    or tpl.get("required_roles_raw")
        blacklisted_str = blacklisted_roles or tpl.get("blacklisted_roles_raw")
        bonus_str       = bonus_roles       or tpl.get("bonus_roles_raw")
        min_acct_str    = min_account_age   or tpl.get("min_account_age_raw")
        min_join_str    = min_join_age      or tpl.get("min_join_age_raw")
        ping_role_id  = ping_role.id if ping_role else int(tpl.get("ping_role_id") or 0) or None
        if ping_role is None and ping_role_id:
            ping_role = interaction.guild.get_role(ping_role_id)

        if not prize or not duration:
            return await interaction.followup.send(embed=self.ef.error(
                "Both `prize` and `duration` are required (either directly or via a template)."
            ))

        td = parse_duration(duration)
        if not td or td.total_seconds() < 30:
            return await interaction.followup.send(embed=self.ef.error(
                "Invalid duration. Try `30s`, `10m`, `2h`, `1d`. Min 30 seconds."
            ))

        target = channel or interaction.channel
        if not isinstance(target, discord.TextChannel):
            return await interaction.followup.send(embed=self.ef.error(
                "Choose a text channel."
            ))
        host_user = host or interaction.user

        # Premium-gated advanced options
        required_ids: list[int] = []
        blacklisted_ids: list[int] = []
        bonus_map: dict[str, int] = {}
        min_acct_s = 0
        min_join_s = 0
        if any([required_str, blacklisted_str, bonus_str, min_acct_str, min_join_str]):
            if not is_premium:
                return await interaction.followup.send(embed=self.ef.error(
                    "Required roles, blacklists, bonus roles, and minimum-age requirements are **premium** features. "
                    "Use `/premium` to learn more."
                ))
            required_ids    = _parse_role_list(interaction.guild, required_str)
            blacklisted_ids = _parse_role_list(interaction.guild, blacklisted_str)
            bonus_map       = _parse_bonus_roles(interaction.guild, bonus_str)
            if min_acct_str:
                ta = parse_duration(min_acct_str)
                if ta:
                    min_acct_s = int(ta.total_seconds())
            if min_join_str:
                tj = parse_duration(min_join_str)
                if tj:
                    min_join_s = int(tj.total_seconds())

        now = int(datetime.now(timezone.utc).timestamp())
        ends_at = now + int(td.total_seconds())

        # Build view; override label/emoji on the button if given.
        view = GiveawayView(self.ef)
        if button_label or button_emoji:
            btn = view.children[0]
            if isinstance(btn, discord.ui.Button):
                if button_label:
                    btn.label = button_label
                if button_emoji:
                    try:
                        btn.emoji = button_emoji
                    except Exception:
                        pass

        # Send placeholder, then patch in the real ID below.
        gw_stub: dict = {
            "prize": prize, "winners": int(winners), "host_id": host_user.id,
            "ends_at": ends_at, "description": description, "image": image,
            "required_roles": required_ids, "blacklisted_roles": blacklisted_ids,
            "bonus_roles": bonus_map,
            "min_account_age_s": min_acct_s, "min_join_age_s": min_join_s,
            "entries": [],
        }
        embed = _build_giveaway_embed(self.ef, gw_stub)
        content = ping_role.mention if ping_role else None
        try:
            sent = await target.send(
                content=content, embed=embed, view=view,
                allowed_mentions=discord.AllowedMentions(roles=True),
            )
        except discord.Forbidden:
            return await interaction.followup.send(embed=self.ef.error(
                "I don't have permission to send messages there."
            ))
        except discord.HTTPException as exc:
            return await interaction.followup.send(embed=self.ef.error(f"Discord error: {exc}"))

        await create_giveaway(
            interaction.guild.id, sent.id,
            channel_id=target.id,
            prize=prize, winners=int(winners),
            host_id=host_user.id,
            ends_at=ends_at,
            description=description, image=image,
            button_label=button_label, button_emoji=button_emoji,
            ping_role_id=int(ping_role.id) if ping_role else 0,
            required_roles=required_ids,
            blacklisted_roles=blacklisted_ids,
            bonus_roles=bonus_map,
            min_account_age_s=min_acct_s,
            min_join_age_s=min_join_s,
        )

        await interaction.followup.send(embed=self.ef.success(
            f"Giveaway started in {target.mention}. Ends <t:{ends_at}:R>.\n{sent.jump_url}"
        ))

    # ── /gw end ──────────────────────────────────────────────────────────

    @gw_group.command(name="end", description="End an active giveaway early.")
    @app_commands.describe(message_id="Message ID of the giveaway.")
    async def gw_end(self, interaction: discord.Interaction, message_id: str):
        assert interaction.guild
        await interaction.response.defer(ephemeral=False)
        try:
            mid = int(message_id)
        except ValueError:
            return await interaction.followup.send(embed=self.ef.error("Invalid message ID."))
        gw = await get_giveaway(interaction.guild.id, mid)
        if not gw:
            return await interaction.followup.send(embed=self.ef.error("No giveaway with that ID."))
        if int(gw.get("ended", 0)):
            return await interaction.followup.send(embed=self.ef.info("That giveaway has already ended."))
        winners = await self._finish_giveaway(interaction.guild, gw, reason="ended")
        await interaction.followup.send(embed=self.ef.success(
            f"Ended. {len(winners)} winner(s) picked." if winners else "Ended — no eligible winners."
        ))

    # ── /gw reroll ───────────────────────────────────────────────────────

    @gw_group.command(name="reroll", description="Pick new winners on a finished giveaway.")
    @app_commands.describe(message_id="Message ID.", winners="How many new winners to roll.")
    async def gw_reroll(
        self, interaction: discord.Interaction,
        message_id: str, winners: app_commands.Range[int, 1, 50] = 1,
    ):
        assert interaction.guild
        await interaction.response.defer(ephemeral=False)
        try:
            mid = int(message_id)
        except ValueError:
            return await interaction.followup.send(embed=self.ef.error("Invalid message ID."))
        gw = await get_giveaway(interaction.guild.id, mid)
        if not gw:
            return await interaction.followup.send(embed=self.ef.error("No giveaway with that ID."))
        gw["winners"] = int(winners)
        members: dict[int, discord.Member] = {}
        for uid in (gw.get("entries") or []):
            m = interaction.guild.get_member(int(uid))
            if m:
                members[int(uid)] = m
        new_winners = _pick_winners(gw, members)
        if not new_winners:
            return await interaction.followup.send(embed=self.ef.info("No eligible entries to re-roll."))
        msg = await self._resolve_message(interaction.guild, gw)
        if msg:
            try:
                await msg.channel.send(
                    f"{GIVEAWAY_EMOJI} New winner(s) for **{gw.get('prize')}**: " +
                    ", ".join(f"<@{w}>" for w in new_winners) + f"\n{msg.jump_url}",
                    allowed_mentions=discord.AllowedMentions(users=True),
                )
            except (discord.Forbidden, discord.HTTPException):
                pass
        await interaction.followup.send(embed=self.ef.success(
            "Re-rolled: " + ", ".join(f"<@{w}>" for w in new_winners)
        ))

    # ── /gw cancel ───────────────────────────────────────────────────────

    @gw_group.command(name="cancel", description="Cancel a giveaway without picking winners.")
    @app_commands.describe(message_id="Message ID.")
    async def gw_cancel(self, interaction: discord.Interaction, message_id: str):
        assert interaction.guild
        await interaction.response.defer(ephemeral=False)
        try:
            mid = int(message_id)
        except ValueError:
            return await interaction.followup.send(embed=self.ef.error("Invalid message ID."))
        gw = await get_giveaway(interaction.guild.id, mid)
        if not gw:
            return await interaction.followup.send(embed=self.ef.error("No giveaway with that ID."))
        await self._finish_giveaway(interaction.guild, gw, reason="cancelled")
        await interaction.followup.send(embed=self.ef.success("Giveaway cancelled."))

    # ── /gw list ─────────────────────────────────────────────────────────

    @gw_group.command(name="list", description="List active giveaways in this server.")
    async def gw_list(self, interaction: discord.Interaction):
        assert interaction.guild
        await interaction.response.defer(ephemeral=False)
        items = await list_giveaways(interaction.guild.id, only_active=True)
        if not items:
            return await interaction.followup.send(embed=self.ef.info("No active giveaways."))
        e = self.ef.build(title=f"Active Giveaways ({len(items)})", color_key="accent")
        for gw in items[:25]:
            ends_at = int(gw.get("ends_at", 0))
            entries = len(gw.get("entries", []) or [])
            paused = "⏸️ paused — " if int(gw.get("paused", 0)) else ""
            e.add_field(
                name=f"{gw.get('prize', 'Giveaway')}",
                value=(
                    f"`{int(gw.get('message_id', 0))}` in <#{int(gw.get('channel_id', 0))}>\n"
                    f"{paused}Ends <t:{ends_at}:R>  ·  {entries} entries  ·  "
                    f"{int(gw.get('winners', 1))} winner(s)"
                ),
                inline=False,
            )
        await interaction.followup.send(embed=e)

    # ── /gw pause / resume ──────────────────────────────────────────────

    @gw_group.command(name="pause", description="Pause a running giveaway (entries close).")
    @app_commands.describe(message_id="Message ID.")
    async def gw_pause(self, interaction: discord.Interaction, message_id: str):
        assert interaction.guild
        await interaction.response.defer(ephemeral=False)
        try:
            mid = int(message_id)
        except ValueError:
            return await interaction.followup.send(embed=self.ef.error("Invalid message ID."))
        gw = await get_giveaway(interaction.guild.id, mid)
        if not gw or int(gw.get("ended", 0)):
            return await interaction.followup.send(embed=self.ef.error("No active giveaway with that ID."))
        if int(gw.get("paused", 0)):
            return await interaction.followup.send(embed=self.ef.info("Already paused."))
        now = int(datetime.now(timezone.utc).timestamp())
        remaining = max(0, int(gw.get("ends_at", 0)) - now)
        await update_giveaway(
            interaction.guild.id, mid,
            paused=1, paused_at=now, paused_remaining=remaining,
        )
        gw.update({"paused": 1, "paused_remaining": remaining})
        msg = await self._resolve_message(interaction.guild, gw)
        if msg:
            try:
                await msg.edit(embed=_build_giveaway_embed(self.ef, gw))
            except (discord.Forbidden, discord.HTTPException):
                pass
        await interaction.followup.send(embed=self.ef.success(
            f"Paused. {format_duration_safe(remaining)} remaining when resumed."
        ))

    @gw_group.command(name="resume", description="Resume a paused giveaway.")
    @app_commands.describe(message_id="Message ID.")
    async def gw_resume(self, interaction: discord.Interaction, message_id: str):
        assert interaction.guild
        await interaction.response.defer(ephemeral=False)
        try:
            mid = int(message_id)
        except ValueError:
            return await interaction.followup.send(embed=self.ef.error("Invalid message ID."))
        gw = await get_giveaway(interaction.guild.id, mid)
        if not gw or int(gw.get("ended", 0)):
            return await interaction.followup.send(embed=self.ef.error("No active giveaway with that ID."))
        if not int(gw.get("paused", 0)):
            return await interaction.followup.send(embed=self.ef.info("Not paused."))
        now = int(datetime.now(timezone.utc).timestamp())
        remaining = int(gw.get("paused_remaining") or 0)
        new_ends_at = now + remaining
        await update_giveaway(
            interaction.guild.id, mid,
            paused=0, paused_at=None, paused_remaining=None,
            ends_at=new_ends_at,
        )
        gw.update({"paused": 0, "ends_at": new_ends_at})
        msg = await self._resolve_message(interaction.guild, gw)
        if msg:
            try:
                await msg.edit(embed=_build_giveaway_embed(self.ef, gw))
            except (discord.Forbidden, discord.HTTPException):
                pass
        await interaction.followup.send(embed=self.ef.success(
            f"Resumed. Now ends <t:{new_ends_at}:R>."
        ))

    # ── /gw edit ─────────────────────────────────────────────────────────

    @gw_group.command(name="edit", description="Edit an active giveaway's prize/winners/duration/image.")
    @app_commands.describe(
        message_id="Message ID of the giveaway.",
        prize="New prize (optional).",
        winners="New winner count (optional).",
        add_time="Add to the duration (e.g. 30m). Use a leading - to subtract.",
        image="New image URL (optional). Use 'none' to clear.",
        description="New description (optional). Use 'none' to clear.",
    )
    async def gw_edit(
        self, interaction: discord.Interaction, message_id: str,
        prize: Optional[str] = None,
        winners: Optional[app_commands.Range[int, 1, 50]] = None,
        add_time: Optional[str] = None,
        image: Optional[str] = None,
        description: Optional[str] = None,
    ):
        assert interaction.guild
        await interaction.response.defer(ephemeral=False)
        try:
            mid = int(message_id)
        except ValueError:
            return await interaction.followup.send(embed=self.ef.error("Invalid message ID."))
        gw = await get_giveaway(interaction.guild.id, mid)
        if not gw or int(gw.get("ended", 0)):
            return await interaction.followup.send(embed=self.ef.error("No active giveaway with that ID."))

        updates: dict = {}
        if prize:
            updates["prize"] = prize
            gw["prize"] = prize
        if winners:
            updates["winners"] = int(winners)
            gw["winners"] = int(winners)
        if add_time:
            sign = -1 if add_time.startswith("-") else 1
            td = parse_duration(add_time.lstrip("-+"))
            if td:
                delta = sign * int(td.total_seconds())
                if int(gw.get("paused", 0)):
                    new_rem = max(0, int(gw.get("paused_remaining") or 0) + delta)
                    updates["paused_remaining"] = new_rem
                    gw["paused_remaining"] = new_rem
                else:
                    new_ends = max(int(datetime.now(timezone.utc).timestamp()) + 30,
                                   int(gw.get("ends_at", 0)) + delta)
                    updates["ends_at"] = new_ends
                    gw["ends_at"] = new_ends
        if image is not None:
            if image.lower() == "none":
                updates["image"] = None
                gw["image"] = None
            else:
                updates["image"] = image
                gw["image"] = image
        if description is not None:
            if description.lower() == "none":
                updates["description"] = None
                gw["description"] = None
            else:
                updates["description"] = description
                gw["description"] = description
        if not updates:
            return await interaction.followup.send(embed=self.ef.info("Nothing to update."))
        await update_giveaway(interaction.guild.id, mid, **updates)
        msg = await self._resolve_message(interaction.guild, gw)
        if msg:
            try:
                await msg.edit(embed=_build_giveaway_embed(self.ef, gw))
            except (discord.Forbidden, discord.HTTPException):
                pass
        await interaction.followup.send(embed=self.ef.success("Giveaway updated."))

    # ── /gw template ─────────────────────────────────────────────────────

    template_group = app_commands.Group(
        parent=gw_group, name="template",
        description="Save and reuse giveaway option presets.",
    )

    @template_group.command(name="save", description="Save an option preset (premium).")
    @app_commands.describe(
        name="Template name.",
        prize="Default prize.",
        duration="Default duration (e.g. 1d).",
        winners="Default winners.",
        description="Default description.",
        button_label="Default button label.",
        button_emoji="Default button emoji.",
        image="Default image URL.",
        ping_role="Default ping role.",
        required_roles="Default required roles.",
        blacklisted_roles="Default blacklisted roles.",
        bonus_roles="Default bonus roles.",
        min_account_age="Default min account age (e.g. 7d).",
        min_join_age="Default min server tenure.",
    )
    async def template_save(
        self, interaction: discord.Interaction, name: app_commands.Range[str, 1, 32],
        prize: Optional[str] = None,
        duration: Optional[str] = None,
        winners: Optional[app_commands.Range[int, 1, 50]] = None,
        description: Optional[str] = None,
        button_label: Optional[app_commands.Range[str, 1, 80]] = None,
        button_emoji: Optional[str] = None,
        image: Optional[str] = None,
        ping_role: Optional[discord.Role] = None,
        required_roles: Optional[str] = None,
        blacklisted_roles: Optional[str] = None,
        bonus_roles: Optional[str] = None,
        min_account_age: Optional[str] = None,
        min_join_age: Optional[str] = None,
    ):
        assert interaction.guild
        await interaction.response.defer(ephemeral=False)
        if not await self._is_premium(interaction.guild.id):
            return await interaction.followup.send(embed=self.ef.error(
                "Giveaway templates are a **premium** feature."
            ))
        if await count_gw_templates(interaction.guild.id) >= TEMPLATE_LIMIT_PREM:
            existing = await get_gw_template(interaction.guild.id, name)
            if existing is None:
                return await interaction.followup.send(embed=self.ef.error(
                    f"Template limit ({TEMPLATE_LIMIT_PREM}) reached."
                ))
        data = {
            "prize": prize, "duration": duration,
            "winners": int(winners) if winners else None,
            "description": description,
            "button_label": button_label, "button_emoji": button_emoji,
            "image": image,
            "ping_role_id": ping_role.id if ping_role else None,
            "required_roles_raw": required_roles,
            "blacklisted_roles_raw": blacklisted_roles,
            "bonus_roles_raw": bonus_roles,
            "min_account_age_raw": min_account_age,
            "min_join_age_raw": min_join_age,
        }
        # Drop Nones to keep stored payload tidy.
        data = {k: v for k, v in data.items() if v is not None}
        await save_gw_template(interaction.guild.id, name, data)
        await interaction.followup.send(embed=self.ef.success(
            f"Template `{name.lower()}` saved with {len(data)} field(s)."
        ))

    @template_group.command(name="load", description="Show a saved template.")
    @app_commands.describe(name="Template name.")
    async def template_load(self, interaction: discord.Interaction, name: str):
        assert interaction.guild
        await interaction.response.defer(ephemeral=False)
        data = await get_gw_template(interaction.guild.id, name)
        if not data:
            return await interaction.followup.send(embed=self.ef.error(f"No template `{name}`."))
        e = self.ef.build(title=f"Template: {name.lower()}", color_key="accent")
        for k, v in data.items():
            e.add_field(name=k, value=f"`{v}`", inline=False)
        e.set_footer(text="Use this in /gw start with the same name to apply.")
        await interaction.followup.send(embed=e)

    @template_group.command(name="list", description="List saved templates.")
    async def template_list(self, interaction: discord.Interaction):
        assert interaction.guild
        await interaction.response.defer(ephemeral=False)
        items = await list_gw_templates(interaction.guild.id)
        if not items:
            return await interaction.followup.send(embed=self.ef.info("No saved templates."))
        e = self.ef.build(title=f"Giveaway Templates ({len(items)})", color_key="accent")
        for it in items[:25]:
            e.add_field(name=it.get("name", "?"),
                        value=f"saved <t:{int(it.get('created_at', 0))}:R>", inline=False)
        await interaction.followup.send(embed=e)

    @template_group.command(name="delete", description="Delete a saved template.")
    @app_commands.describe(name="Template name.")
    async def template_delete(self, interaction: discord.Interaction, name: str):
        assert interaction.guild
        await interaction.response.defer(ephemeral=False)
        ok = await delete_gw_template(interaction.guild.id, name)
        if not ok:
            return await interaction.followup.send(embed=self.ef.error(f"No template `{name}`."))
        await interaction.followup.send(embed=self.ef.success(f"Template `{name.lower()}` deleted."))

    # ── error handler ────────────────────────────────────────────────────

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.CommandOnCooldown):
            embed = self.ef.warning(f"Slow down — try again in **{error.retry_after:.1f}s**.")
        elif isinstance(error, app_commands.MissingPermissions):
            embed = self.ef.error("You need **Manage Server** to use this command.")
        else:
            embed = self.ef.error(str(error))
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(embed=embed, ephemeral=False)
            else:
                await interaction.followup.send(embed=embed, ephemeral=False)
        except discord.HTTPException:
            pass


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Giveaways(bot))
