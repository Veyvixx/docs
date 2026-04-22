"""
Anti-Nuke cog — per-guild configurable thresholds, suspicious activity log,
in-memory rolling window detection (no DB on hot path).
Commands: /antinuke status  toggle  config  whitelist  logs
"""

import asyncio
import discord
from discord import app_commands
from discord.ext import commands
from collections import defaultdict, deque
import time
import logging
from typing import Deque

from utils.embeds import EmbedFactory
from utils.database import (
    get_guild_config, upsert_guild_config,
    get_whitelist, add_to_whitelist, remove_from_whitelist,
    log_antinuke, get_antinuke_logs,
)
from utils.cache import cache, whitelist_key

logger = logging.getLogger(__name__)


class AntiNukeConfigModal(discord.ui.Modal, title="Anti-Nuke Configuration"):
    ban_thresh     = discord.ui.TextInput(label="Ban threshold",            placeholder="3", max_length=3, default="3")
    kick_thresh    = discord.ui.TextInput(label="Kick threshold",           placeholder="3", max_length=3, default="3")
    chan_thresh    = discord.ui.TextInput(label="Channel delete threshold", placeholder="3", max_length=3, default="3")
    role_thresh    = discord.ui.TextInput(label="Role delete threshold",    placeholder="3", max_length=3, default="3")
    window         = discord.ui.TextInput(label="Detection window (seconds)", placeholder="10", max_length=4, default="10")

    def __init__(self, current: dict, view: "AntiNukeView"):
        super().__init__()
        self.parent_view = view
        # Pre-fill with current settings
        self.ban_thresh.default  = str(current.get("antinuke_ban_thresh",  3))
        self.kick_thresh.default = str(current.get("antinuke_kick_thresh", 3))
        self.chan_thresh.default = str(current.get("antinuke_chan_thresh",  3))
        self.role_thresh.default = str(current.get("antinuke_role_thresh", 3))
        self.window.default      = str(current.get("antinuke_window",     10))

    async def on_submit(self, interaction: discord.Interaction):
        assert interaction.guild
        try:
            await upsert_guild_config(
                interaction.guild.id,
                antinuke_ban_thresh  = max(1, int(self.ban_thresh.value  or 3)),
                antinuke_kick_thresh = max(1, int(self.kick_thresh.value or 3)),
                antinuke_chan_thresh  = max(1, int(self.chan_thresh.value  or 3)),
                antinuke_role_thresh = max(1, int(self.role_thresh.value or 3)),
                antinuke_window      = max(5, int(self.window.value      or 10)),
            )
        except ValueError:
            await interaction.response.send_message(
                "Invalid values — all fields must be numbers.", ephemeral=False
            )
            return

        config = await get_guild_config(interaction.guild.id)
        embed  = self.parent_view._status_embed(config)
        await interaction.response.edit_message(embed=embed, view=self.parent_view)


class PunishSelect(discord.ui.Select):
    def __init__(self, current: str, view: "AntiNukeView"):
        self.parent_view = view
        opts = [
            discord.SelectOption(label="Ban offender",  value="ban",  default=current == "ban"),
            discord.SelectOption(label="Kick offender", value="kick", default=current == "kick"),
        ]
        super().__init__(placeholder="Choose punishment…", options=opts)

    async def callback(self, interaction: discord.Interaction):
        assert interaction.guild
        await upsert_guild_config(interaction.guild.id, antinuke_punishment=self.values[0])
        config = await get_guild_config(interaction.guild.id)
        await interaction.response.edit_message(
            embed=self.parent_view._status_embed(config), view=self.parent_view
        )


class AntiNukeView(discord.ui.View):
    def __init__(self, config: dict, ef: EmbedFactory, guild: discord.Guild):
        super().__init__(timeout=180)
        self.config = config
        self.ef     = ef
        self.guild  = guild
        self.add_item(PunishSelect(config.get("antinuke_punishment", "ban"), self))

    def _status_embed(self, config: dict) -> discord.Embed:
        enabled    = bool(config.get("antinuke_enabled", 1))
        punishment = config.get("antinuke_punishment", "ban")
        window     = config.get("antinuke_window", 10)

        e = self.ef.build(
            author_name = "Anti-Nuke Dashboard",
            color_key   = "success" if enabled else "error",
        )
        e.add_field(name="Status",     value=f"{self.ef.e['enabled']} Enabled" if enabled else f"{self.ef.e['disabled']} Disabled", inline=True)
        e.add_field(name="Punishment", value=f"`{punishment.upper()}`",               inline=True)
        e.add_field(name="Window",     value=f"`{window}s`",                          inline=True)
        e.add_field(name="Ban",        value=f"`{config.get('antinuke_ban_thresh',  3)}` actions", inline=True)
        e.add_field(name="Kick",       value=f"`{config.get('antinuke_kick_thresh', 3)}` actions", inline=True)
        e.add_field(name="Chan Del",   value=f"`{config.get('antinuke_chan_thresh',  3)}` actions", inline=True)
        e.add_field(name="Role Del",   value=f"`{config.get('antinuke_role_thresh', 3)}` actions", inline=True)
        return e

    @discord.ui.button(label="Toggle", style=discord.ButtonStyle.secondary, row=1)
    async def toggle(self, interaction: discord.Interaction, _: discord.ui.Button):
        assert interaction.guild
        cfg     = await get_guild_config(interaction.guild.id)
        new_val = 0 if cfg.get("antinuke_enabled", 1) else 1
        await upsert_guild_config(interaction.guild.id, antinuke_enabled=new_val)
        cfg     = await get_guild_config(interaction.guild.id)
        await interaction.response.edit_message(embed=self._status_embed(cfg), view=self)

    @discord.ui.button(label="Edit Thresholds", style=discord.ButtonStyle.primary, row=1)
    async def edit_thresh(self, interaction: discord.Interaction, _: discord.ui.Button):
        assert interaction.guild
        cfg = await get_guild_config(interaction.guild.id)
        await interaction.response.send_modal(AntiNukeConfigModal(cfg, self))


# ── Main cog ──────────────────────────────────────────────────────────────────

class AntiNuke(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot    = bot
        self.ef: EmbedFactory = bot.ef  # type: ignore[attr-defined]
        self.config: dict = bot.config  # type: ignore[attr-defined]

        # Rolling action windows: {guild_id: {action_key: deque[timestamp]}}
        self._actions:  dict[int, dict[str, Deque[float]]] = defaultdict(lambda: defaultdict(deque))
        # Avoid double-punishing the same user in the same window
        self._punished: dict[int, set[int]] = defaultdict(set)

    # ── Whitelist cache helper ─────────────────────────────────────────────────

    async def _whitelist(self, guild_id: int) -> set[int]:
        """Return whitelist, preferring the shared TTL cache."""
        cached = cache.get(whitelist_key(guild_id))
        if cached is not None:
            return cached
        return await get_whitelist(guild_id)

    # ── Core detection  (no DB on hot path — everything in memory) ────────────

    async def _check(self, guild: discord.Guild, user_id: int, action: str) -> None:
        config = await get_guild_config(guild.id)   # served from cache ~99% of the time
        if not config.get("antinuke_enabled", 1):
            return

        # Whitelist check
        if user_id in await self._whitelist(guild.id):
            return
        if user_id in self._punished[guild.id]:
            return
        # Bot itself
        if self.bot.user and user_id == self.bot.user.id:
            return

        # Threshold from per-guild config or global config
        thresh_key = {
            "ban":            "antinuke_ban_thresh",
            "kick":           "antinuke_kick_thresh",
            "channel_delete": "antinuke_chan_thresh",
            "role_delete":    "antinuke_role_thresh",
            "webhook_create": "antinuke_webhook_thresh",
        }.get(action)
        default = self.config.get("antinuke", {}).get("thresholds", {}).get(action, 3)
        threshold = config.get(thresh_key, default) if thresh_key else default
        window    = config.get("antinuke_window", self.config.get("antinuke", {}).get("window_seconds", 10))

        now    = time.monotonic()
        bucket = self._actions[guild.id][action]
        bucket.append(now)
        while bucket and now - bucket[0] > window:
            bucket.popleft()

        if len(bucket) < threshold:
            return

        # Threshold crossed — act
        self._punished[guild.id].add(user_id)
        bucket.clear()
        punishment = config.get("antinuke_punishment", "ban")

        logger.warning("Anti-nuke: guild=%s action=%s user=%s punishment=%s", guild.id, action, user_id, punishment)
        await self._punish(guild, user_id, action, threshold, punishment)

    async def _punish(
        self,
        guild:      discord.Guild,
        user_id:    int,
        action:     str,
        count:      int,
        punishment: str,
    ) -> None:
        reason = f"[Anti-Nuke] Mass {action} detected — {count}+ actions in window"
        try:
            if punishment == "ban":
                await guild.ban(discord.Object(id=user_id), reason=reason)
            elif punishment == "kick":
                member = guild.get_member(user_id)
                if member:
                    await guild.kick(member, reason=reason)
        except (discord.Forbidden, discord.HTTPException) as exc:
            logger.error("Anti-nuke punish failed: %s", exc)
            return

        # Log to DB (batched — non-blocking)
        await log_antinuke(guild.id, action, user_id, count, punishment)

        # Alert in mod-log channel
        config  = await get_guild_config(guild.id)
        ch_id   = config.get("mod_log_channel")
        if ch_id:
            ch = guild.get_channel(ch_id)
            if isinstance(ch, discord.TextChannel):
                member = guild.get_member(user_id)
                if member:
                    try:
                        await ch.send(embed=self.ef.antinuke_alert(action, member, count, punishment))
                    except discord.Forbidden:
                        pass

    # ── Audit log listeners ───────────────────────────────────────────────────

    async def _audit(self, guild: discord.Guild, action_type: discord.AuditLogAction, target_id: int, key: str):
        await asyncio.sleep(0.4)
        try:
            async for entry in guild.audit_logs(limit=5, action=action_type):
                if entry.target and entry.target.id == target_id:
                    await self._check(guild, entry.user.id, key)
                    return
        except discord.Forbidden:
            pass

    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user: discord.User | discord.Member):
        await self._audit(guild, discord.AuditLogAction.ban, user.id, "ban")

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        await self._audit(member.guild, discord.AuditLogAction.kick, member.id, "kick")

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel):
        await self._audit(channel.guild, discord.AuditLogAction.channel_delete, channel.id, "channel_delete")

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role):
        await self._audit(role.guild, discord.AuditLogAction.role_delete, role.id, "role_delete")

    @commands.Cog.listener()
    async def on_webhooks_update(self, channel: discord.abc.GuildChannel):
        await asyncio.sleep(0.4)
        try:
            async for entry in channel.guild.audit_logs(limit=3, action=discord.AuditLogAction.webhook_create):
                await self._check(channel.guild, entry.user.id, "webhook_create")
                return
        except discord.Forbidden:
            pass

    # ── Slash commands ────────────────────────────────────────────────────────

    an_group = app_commands.Group(
        name="antinuke",
        description="Anti-nuke protection system.",
        default_permissions=discord.Permissions(administrator=True),
    )

    @an_group.command(name="status", description="Open the anti-nuke dashboard.")
    async def status(self, interaction: discord.Interaction):
        assert interaction.guild
        await interaction.response.defer(ephemeral=False)
        config = await get_guild_config(interaction.guild.id)
        view   = AntiNukeView(config, self.ef, interaction.guild)
        await interaction.followup.send(embed=view._status_embed(config), view=view, ephemeral=False)

    @an_group.command(name="toggle", description="Quickly enable or disable anti-nuke.")
    async def toggle(self, interaction: discord.Interaction):
        assert interaction.guild
        config  = await get_guild_config(interaction.guild.id)
        new_val = 0 if config.get("antinuke_enabled", 1) else 1
        await upsert_guild_config(interaction.guild.id, antinuke_enabled=new_val)
        icon = self.ef.e['enabled'] if new_val else self.ef.e['disabled']
        state = "enabled" if new_val else "disabled"
        await interaction.response.send_message(
            embed=self.ef.success(f"Anti-nuke {icon} **{state}**."), ephemeral=False
        )

    @an_group.command(name="config", description="Configure thresholds and punishment interactively.")
    async def config_cmd(self, interaction: discord.Interaction):
        assert interaction.guild
        await interaction.response.defer(ephemeral=False)
        config = await get_guild_config(interaction.guild.id)
        view   = AntiNukeView(config, self.ef, interaction.guild)
        await interaction.followup.send(embed=view._status_embed(config), view=view, ephemeral=False)

    @an_group.command(name="logs", description="Show recent suspicious activity log.")
    async def logs_cmd(self, interaction: discord.Interaction):
        assert interaction.guild
        await interaction.response.defer(ephemeral=False)
        rows = await get_antinuke_logs(interaction.guild.id, limit=10)
        e    = self.ef.build(
            author_name = "Anti-Nuke  ·  Activity Log",
            color_key   = "warning",
        )
        if not rows:
            e.description = "No suspicious activity logged."
        else:
            for r in rows:
                e.add_field(
                    name  = f"`{r['action'].replace('_',' ').title()}`  ·  <t:{r['created_at']}:R>",
                    value = f"> <@{r['offender_id']}> triggered **{r['count']}** actions  ·  punished: `{r['punishment'].upper()}`",
                    inline=False,
                )
        await interaction.followup.send(embed=e, ephemeral=False)

    # Whitelist sub-group
    wl_group = app_commands.Group(name="whitelist", description="Manage antinuke whitelist.", parent=an_group)

    @wl_group.command(name="add", description="Add a user to the antinuke whitelist.")
    @app_commands.describe(user="User to whitelist.")
    async def wl_add(self, interaction: discord.Interaction, user: discord.Member):
        assert interaction.guild
        await add_to_whitelist(interaction.guild.id, user.id, interaction.user.id)
        await interaction.response.send_message(
            embed=self.ef.success(f"{user.mention} added to whitelist."), ephemeral=False
        )

    @wl_group.command(name="remove", description="Remove a user from the antinuke whitelist.")
    @app_commands.describe(user="User to remove.")
    async def wl_remove(self, interaction: discord.Interaction, user: discord.Member):
        assert interaction.guild
        await remove_from_whitelist(interaction.guild.id, user.id)
        await interaction.response.send_message(
            embed=self.ef.success(f"{user.mention} removed from whitelist."), ephemeral=False
        )

    @wl_group.command(name="list", description="List all whitelisted users.")
    async def wl_list(self, interaction: discord.Interaction):
        assert interaction.guild
        wl = await get_whitelist(interaction.guild.id)
        e  = self.ef.build(
            author_name = "Antinuke Whitelist",
            color_key   = "secondary",
        )
        e.description = " ".join(f"<@{uid}>" for uid in wl) if wl else "No users whitelisted."
        await interaction.response.send_message(embed=e, ephemeral=False)

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        embed = self.ef.error(str(error))
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(embed=embed, ephemeral=True)
            else:
                await interaction.followup.send(embed=embed, ephemeral=True)
        except (discord.NotFound, discord.HTTPException):
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(AntiNuke(bot))
