"""
Moderation cog — jail, mute, lock, unlock, refresh channel, automod, and core mod tools.
Commands: /ban /unban /kick /timeout /untimeout /warn /warnings /clearwarnings /purge
          /jail /unjail /mute /unmute /lock /unlock /refreshchannel /automod
"""

import asyncio
import re
import discord
from discord import app_commands
from discord.ext import commands
from datetime import timezone, datetime
from typing import Optional

EMOJI_PATTERN = re.compile(r"<(a?):([A-Za-z0-9_~]+):(\d+)>")

from utils.embeds import EmbedFactory
from utils.helpers import parse_duration, format_duration, can_act_on, send_dm
from utils.database import (
    add_warning, get_warnings, clear_warnings,
    log_mod_action, get_guild_config, upsert_guild_config,
)

PER_PAGE = 5


# ─────────────────────────── helpers ────────────────────────────

async def _get_or_create_role(
    guild: discord.Guild,
    role_id: Optional[int],
    name: str,
    color: discord.Color,
    permissions: discord.Permissions,
    reason: str,
) -> discord.Role:
    if role_id:
        role = guild.get_role(role_id)
        if role:
            return role
    role = await guild.create_role(name=name, color=color, permissions=permissions, reason=reason)
    return role


async def _apply_channel_overrides(
    guild: discord.Guild,
    role: discord.Role,
    deny: discord.Permissions,
    allow: discord.Permissions,
    skip_channel_id: Optional[int] = None,
) -> None:
    tasks = []
    for ch in guild.channels:
        if ch.id == skip_channel_id:
            continue
        if not isinstance(ch, (discord.TextChannel, discord.ForumChannel, discord.StageChannel)):
            continue
        tasks.append(ch.set_permissions(role, overwrite=discord.PermissionOverwrite.from_pair(allow, deny)))
    if tasks:
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for r in results:
            if isinstance(r, discord.Forbidden):
                pass


# ─────────────────────────── views ──────────────────────────────

class WarningsView(discord.ui.View):
    def __init__(self, user: discord.Member | discord.User, warns: list[dict], ef: EmbedFactory):
        super().__init__(timeout=120)
        self.user  = user
        self.warns = warns
        self.ef    = ef
        self.page  = 0
        self.total = max(1, -(-len(warns) // PER_PAGE))
        self._update_buttons()

    def _update_buttons(self):
        self.prev_btn.disabled = self.page == 0
        self.next_btn.disabled = self.page >= self.total - 1

    def current_embed(self) -> discord.Embed:
        return self.ef.warnings_page(self.user, self.warns, self.page, PER_PAGE)

    @discord.ui.button(label="Prev", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        self.page -= 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self.current_embed(), view=self)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        self.page += 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self.current_embed(), view=self)


class ConfirmView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=30)
        self.confirmed = False

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def yes(self, interaction: discord.Interaction, _: discord.ui.Button):
        self.confirmed = True
        self.stop()
        await interaction.response.defer()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def no(self, interaction: discord.Interaction, _: discord.ui.Button):
        self.stop()
        await interaction.response.send_message("Cancelled.", ephemeral=False)


# ─────────────────────────── cog ────────────────────────────────

class Moderation(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.ef: EmbedFactory = bot.ef  # type: ignore[attr-defined]

    async def _mod_log(self, guild: discord.Guild, embed: discord.Embed) -> None:
        config = await get_guild_config(guild.id)
        ch_id  = config.get("mod_log_channel")
        if ch_id:
            ch = guild.get_channel(ch_id)
            if isinstance(ch, discord.TextChannel):
                try:
                    await ch.send(embed=embed)
                except discord.Forbidden:
                    pass

    # ── core mod ──────────────────────────────────────────────────

    @app_commands.command(name="ban", description="Permanently ban a member.")
    @app_commands.default_permissions(ban_members=True)
    @app_commands.describe(user="Member to ban.", reason="Reason.", delete_days="Days of messages to delete (0-7).")
    @app_commands.checks.has_permissions(ban_members=True)
    @app_commands.checks.bot_has_permissions(ban_members=True)
    @app_commands.checks.cooldown(1, 5)
    async def ban(self, interaction: discord.Interaction, user: discord.Member,
                  reason: Optional[str] = "No reason provided.",
                  delete_days: app_commands.Range[int, 0, 7] = 0):
        assert interaction.guild
        if not can_act_on(interaction.user, user):  # type: ignore[arg-type]
            return await interaction.response.send_message(
                embed=self.ef.error("You cannot act on someone with an equal or higher role."), ephemeral=False
            )
        view = ConfirmView()
        await interaction.response.send_message(
            embed=self.ef.warning(f"Ban {user.mention}  (`{user.id}`)\n**Reason:** {reason}",
                                  title=f"{self.ef.e['warning']}  Confirm Ban"),
            view=view, ephemeral=False,
        )
        await view.wait()
        if not view.confirmed:
            return
        await send_dm(user, self.ef.build(title=f"{self.ef.e['error']}  Banned from  {interaction.guild.name}",
                                          description=f"**Reason:** {reason}", color_key="error"))
        await interaction.guild.ban(user, reason=reason, delete_message_days=delete_days)
        case_id = await log_mod_action(interaction.guild.id, "BAN", user.id, interaction.user.id, reason)  # type: ignore[union-attr]
        await self._mod_log(interaction.guild, self.ef.mod_log("Ban", user, interaction.user, reason or "", case_id))  # type: ignore[arg-type]
        await interaction.followup.send(embed=self.ef.success(f"{user.mention} has been **banned**.  Case #{case_id}"), ephemeral=False)

    @app_commands.command(name="unban", description="Unban a user by their ID.")
    @app_commands.default_permissions(ban_members=True)
    @app_commands.describe(user_id="The user's Discord ID.", reason="Reason.")
    @app_commands.checks.has_permissions(ban_members=True)
    @app_commands.checks.bot_has_permissions(ban_members=True)
    @app_commands.checks.cooldown(1, 5)
    async def unban(self, interaction: discord.Interaction, user_id: str, reason: Optional[str] = "No reason provided."):
        assert interaction.guild
        await interaction.response.defer(ephemeral=False)
        try:
            uid   = int(user_id)
            entry = await interaction.guild.fetch_ban(discord.Object(id=uid))
        except (ValueError, discord.NotFound):
            return await interaction.followup.send(embed=self.ef.error("User not found in ban list."))
        await interaction.guild.unban(entry.user, reason=reason)
        case_id = await log_mod_action(interaction.guild.id, "UNBAN", uid, interaction.user.id, reason)  # type: ignore[union-attr]
        await interaction.followup.send(embed=self.ef.success(f"**{entry.user}** unbanned.  Case #{case_id}"))

    @app_commands.command(name="kick", description="Kick a member.")
    @app_commands.default_permissions(kick_members=True)
    @app_commands.describe(user="Member to kick.", reason="Reason.")
    @app_commands.checks.has_permissions(kick_members=True)
    @app_commands.checks.bot_has_permissions(kick_members=True)
    @app_commands.checks.cooldown(1, 5)
    async def kick(self, interaction: discord.Interaction, user: discord.Member, reason: Optional[str] = "No reason provided."):
        assert interaction.guild
        if not can_act_on(interaction.user, user):  # type: ignore[arg-type]
            return await interaction.response.send_message(
                embed=self.ef.error("You cannot act on someone with an equal or higher role."), ephemeral=False
            )
        await interaction.response.defer(ephemeral=False)
        await send_dm(user, self.ef.build(title=f"{self.ef.e['error']}  Kicked from  {interaction.guild.name}",
                                          description=f"**Reason:** {reason}", color_key="error"))
        await interaction.guild.kick(user, reason=reason)
        case_id = await log_mod_action(interaction.guild.id, "KICK", user.id, interaction.user.id, reason)  # type: ignore[union-attr]
        await self._mod_log(interaction.guild, self.ef.mod_log("Kick", user, interaction.user, reason or "", case_id))  # type: ignore[arg-type]
        await interaction.followup.send(embed=self.ef.success(f"{user.mention} kicked.  Case #{case_id}"))

    @app_commands.command(name="timeout", description="Timeout a member.")
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.describe(user="Member.", duration="Duration e.g. 10m, 2h, 1d.", reason="Reason.")
    @app_commands.checks.has_permissions(moderate_members=True)
    @app_commands.checks.bot_has_permissions(moderate_members=True)
    @app_commands.checks.cooldown(1, 5)
    async def timeout(self, interaction: discord.Interaction, user: discord.Member, duration: str,
                      reason: Optional[str] = "No reason provided."):
        assert interaction.guild
        if not can_act_on(interaction.user, user):  # type: ignore[arg-type]
            return await interaction.response.send_message(
                embed=self.ef.error("You cannot act on someone with an equal or higher role."), ephemeral=False
            )
        td = parse_duration(duration)
        if not td:
            return await interaction.response.send_message(
                embed=self.ef.error("Invalid duration. Try `30s`, `10m`, `2h`, `1d`."), ephemeral=False
            )
        await interaction.response.defer(ephemeral=False)
        await user.timeout(datetime.now(timezone.utc) + td, reason=reason)
        friendly = format_duration(td)
        case_id  = await log_mod_action(interaction.guild.id, "TIMEOUT", user.id, interaction.user.id, reason, int(td.total_seconds()))  # type: ignore[union-attr]
        await self._mod_log(interaction.guild, self.ef.mod_log("Timeout", user, interaction.user, reason or "", case_id, duration=friendly))  # type: ignore[arg-type]
        await interaction.followup.send(embed=self.ef.success(f"{user.mention} timed out for **{friendly}**.  Case #{case_id}"))

    @app_commands.command(name="untimeout", description="Remove a member's timeout.")
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.describe(user="Member.")
    @app_commands.checks.has_permissions(moderate_members=True)
    @app_commands.checks.bot_has_permissions(moderate_members=True)
    async def untimeout(self, interaction: discord.Interaction, user: discord.Member):
        await interaction.response.defer(ephemeral=False)
        await user.timeout(None)
        await interaction.followup.send(embed=self.ef.success(f"{user.mention}'s timeout removed."))

    @app_commands.command(name="warn", description="Issue a formal warning.")
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.describe(user="Member.", reason="Reason for the warning.")
    @app_commands.checks.has_permissions(moderate_members=True)
    @app_commands.checks.cooldown(1, 5)
    async def warn(self, interaction: discord.Interaction, user: discord.Member, reason: str):
        assert interaction.guild
        await interaction.response.defer(ephemeral=False)
        warn_id   = await add_warning(interaction.guild.id, user.id, interaction.user.id, reason)  # type: ignore[union-attr]
        all_warns = await get_warnings(interaction.guild.id, user.id)
        await send_dm(user, self.ef.warning(
            f"**Reason:** {reason}\n\nTotal warnings in **{interaction.guild.name}**: **{len(all_warns)}**",
            title=f"{self.ef.e['warning']}  Warning Received",
        ))
        await interaction.followup.send(
            embed=self.ef.success(f"{user.mention} warned — Case #{warn_id}  ·  **{len(all_warns)}** total warning(s).")
        )

    @app_commands.command(name="warnings", description="View paginated warnings for a member.")
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.describe(user="Member to check.")
    @app_commands.checks.has_permissions(moderate_members=True)
    async def warnings(self, interaction: discord.Interaction, user: discord.Member):
        assert interaction.guild
        await interaction.response.defer(ephemeral=False)
        warns = await get_warnings(interaction.guild.id, user.id)
        view  = WarningsView(user, warns, self.ef)
        await interaction.followup.send(embed=view.current_embed(), view=view, ephemeral=False)

    @app_commands.command(name="clearwarnings", description="Clear all warnings for a member.")
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.describe(user="Member.")
    @app_commands.checks.has_permissions(moderate_members=True)
    async def clearwarnings(self, interaction: discord.Interaction, user: discord.Member):
        assert interaction.guild
        view = ConfirmView()
        await interaction.response.send_message(
            embed=self.ef.warning(f"Clear ALL warnings for {user.mention}?"),
            view=view, ephemeral=False,
        )
        await view.wait()
        if not view.confirmed:
            return
        count = await clear_warnings(interaction.guild.id, user.id)
        await interaction.followup.send(
            embed=self.ef.success(f"Cleared **{count}** warning(s) for {user.mention}."), ephemeral=False
        )

    @app_commands.command(name="purge", description="Bulk-delete messages in this channel.")
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.describe(amount="Messages to delete (1-100).", user="Only from this user (optional).")
    @app_commands.checks.has_permissions(manage_messages=True)
    @app_commands.checks.bot_has_permissions(manage_messages=True)
    @app_commands.checks.cooldown(1, 10)
    async def purge(self, interaction: discord.Interaction, amount: app_commands.Range[int, 1, 100],
                    user: Optional[discord.Member] = None):
        if not isinstance(interaction.channel, discord.TextChannel):
            return await interaction.response.send_message(
                embed=self.ef.error("This command can only be used in text channels."), ephemeral=False
            )
        await interaction.response.defer(ephemeral=False)
        check   = (lambda m: m.author == user) if user else None
        deleted = await interaction.channel.purge(limit=amount, check=check, bulk=True)
        await interaction.followup.send(
            embed=self.ef.success(f"Deleted **{len(deleted)}** message(s)."), ephemeral=False
        )

    # ── jail ─────────────────────────────────────────────────────

    @app_commands.command(name="jail", description="Jail a member, restricting them to the jail channel.")
    @app_commands.default_permissions(manage_roles=True)
    @app_commands.describe(user="Member to jail.", reason="Reason.")
    @app_commands.checks.has_permissions(manage_roles=True)
    @app_commands.checks.bot_has_permissions(manage_roles=True, manage_channels=True)
    @app_commands.checks.cooldown(1, 5)
    async def jail(self, interaction: discord.Interaction, user: discord.Member,
                   reason: Optional[str] = "No reason provided."):
        assert interaction.guild
        if not can_act_on(interaction.user, user):  # type: ignore[arg-type]
            return await interaction.response.send_message(
                embed=self.ef.error("You cannot act on someone with an equal or higher role."), ephemeral=False
            )
        await interaction.response.defer(ephemeral=False)
        config = await get_guild_config(interaction.guild.id)
        jail_ch_id = config.get("jail_channel_id")
        if not jail_ch_id:
            return await interaction.followup.send(
                embed=self.ef.error("No jail channel set. Use `/set jail channel` first.")
            )

        role = await _get_or_create_role(
            interaction.guild, config.get("jail_role_id"),
            name="Jailed", color=discord.Color.from_str("#726060"),
            permissions=discord.Permissions(view_channel=True),
            reason="Nana jail role",
        )
        if role.id != config.get("jail_role_id"):
            await upsert_guild_config(interaction.guild.id, jail_role_id=role.id)

        deny = discord.Permissions(send_messages=True, add_reactions=True, create_public_threads=True,
                                   create_private_threads=True, send_messages_in_threads=True, speak=True)
        allow_none = discord.Permissions.none()
        await _apply_channel_overrides(interaction.guild, role, deny, allow_none, skip_channel_id=jail_ch_id)

        jail_ch = interaction.guild.get_channel(jail_ch_id)
        if isinstance(jail_ch, discord.TextChannel):
            await jail_ch.set_permissions(role, send_messages=True, view_channel=True)

        await user.add_roles(role, reason=f"Jailed by {interaction.user}: {reason}")
        case_id = await log_mod_action(interaction.guild.id, "JAIL", user.id, interaction.user.id, reason)  # type: ignore[union-attr]
        await self._mod_log(interaction.guild, self.ef.mod_log("Jail", user, interaction.user, reason or "", case_id))  # type: ignore[arg-type]
        await send_dm(user, self.ef.build(
            title=f"{self.ef.e['warning']}  Jailed in  {interaction.guild.name}",
            description=f"**Reason:** {reason}\nYou can only speak in the jail channel until a moderator releases you.",
            color_key="warning",
        ))
        await interaction.followup.send(
            embed=self.ef.success(f"{user.mention} has been **jailed**.  Case #{case_id}")
        )

    @app_commands.command(name="unjail", description="Release a jailed member.")
    @app_commands.default_permissions(manage_roles=True)
    @app_commands.describe(user="Member to release.")
    @app_commands.checks.has_permissions(manage_roles=True)
    @app_commands.checks.bot_has_permissions(manage_roles=True)
    async def unjail(self, interaction: discord.Interaction, user: discord.Member):
        assert interaction.guild
        await interaction.response.defer(ephemeral=False)
        config  = await get_guild_config(interaction.guild.id)
        role_id = config.get("jail_role_id")
        role    = interaction.guild.get_role(role_id) if role_id else None
        if not role or role not in user.roles:
            return await interaction.followup.send(embed=self.ef.error(f"{user.mention} is not jailed."))
        await user.remove_roles(role, reason=f"Unjailed by {interaction.user}")
        case_id = await log_mod_action(interaction.guild.id, "UNJAIL", user.id, interaction.user.id, "Released from jail")  # type: ignore[union-attr]
        await interaction.followup.send(
            embed=self.ef.success(f"{user.mention} has been **released** from jail.  Case #{case_id}")
        )

    # ── mute ─────────────────────────────────────────────────────

    @app_commands.command(name="mute", description="Mute a member using a role-based mute.")
    @app_commands.default_permissions(manage_roles=True)
    @app_commands.describe(user="Member to mute.", duration="Optional duration (e.g. 10m, 2h, 1d).", reason="Reason.")
    @app_commands.checks.has_permissions(manage_roles=True)
    @app_commands.checks.bot_has_permissions(manage_roles=True, manage_channels=True)
    @app_commands.checks.cooldown(1, 5)
    async def mute(self, interaction: discord.Interaction, user: discord.Member,
                   duration: Optional[str] = None, reason: Optional[str] = "No reason provided."):
        assert interaction.guild
        if not can_act_on(interaction.user, user):  # type: ignore[arg-type]
            return await interaction.response.send_message(
                embed=self.ef.error("You cannot act on someone with an equal or higher role."), ephemeral=False
            )
        td = None
        if duration:
            td = parse_duration(duration)
            if not td:
                return await interaction.response.send_message(
                    embed=self.ef.error("Invalid duration. Try `30s`, `10m`, `2h`, `1d`."), ephemeral=False
                )
        await interaction.response.defer(ephemeral=False)
        config = await get_guild_config(interaction.guild.id)
        role = await _get_or_create_role(
            interaction.guild, config.get("mute_role_id"),
            name="Muted", color=discord.Color.from_str("#B7A3A3"),
            permissions=discord.Permissions.none(),
            reason="Nana mute role",
        )
        if role.id != config.get("mute_role_id"):
            await upsert_guild_config(interaction.guild.id, mute_role_id=role.id)

        deny = discord.Permissions(send_messages=True, add_reactions=True, speak=True,
                                   create_public_threads=True, create_private_threads=True,
                                   send_messages_in_threads=True)
        await _apply_channel_overrides(interaction.guild, role, deny, discord.Permissions.none())

        await user.add_roles(role, reason=f"Muted by {interaction.user}: {reason}")
        friendly = format_duration(td) if td else "indefinite"
        case_id  = await log_mod_action(interaction.guild.id, "MUTE", user.id, interaction.user.id, reason,
                                        int(td.total_seconds()) if td else None)  # type: ignore[union-attr]
        await self._mod_log(interaction.guild, self.ef.mod_log("Mute", user, interaction.user, reason or "", case_id,
                                                                duration=friendly if td else None))  # type: ignore[arg-type]
        await send_dm(user, self.ef.build(
            title=f"{self.ef.e['warning']}  Muted in  {interaction.guild.name}",
            description=f"**Reason:** {reason}" + (f"\n**Duration:** {friendly}" if td else ""),
            color_key="warning",
        ))
        await interaction.followup.send(
            embed=self.ef.success(
                f"{user.mention} has been **muted**" + (f" for **{friendly}**" if td else "") + f".  Case #{case_id}"
            )
        )
        if td:
            await asyncio.sleep(td.total_seconds())
            if role in user.roles:
                await user.remove_roles(role, reason="Mute duration expired")

    @app_commands.command(name="unmute", description="Remove a role-based mute from a member.")
    @app_commands.default_permissions(manage_roles=True)
    @app_commands.describe(user="Member to unmute.")
    @app_commands.checks.has_permissions(manage_roles=True)
    @app_commands.checks.bot_has_permissions(manage_roles=True)
    async def unmute(self, interaction: discord.Interaction, user: discord.Member):
        assert interaction.guild
        await interaction.response.defer(ephemeral=False)
        config  = await get_guild_config(interaction.guild.id)
        role_id = config.get("mute_role_id")
        role    = interaction.guild.get_role(role_id) if role_id else None
        if not role or role not in user.roles:
            return await interaction.followup.send(embed=self.ef.error(f"{user.mention} is not muted."))
        await user.remove_roles(role, reason=f"Unmuted by {interaction.user}")
        case_id = await log_mod_action(interaction.guild.id, "UNMUTE", user.id, interaction.user.id, "Unmuted")  # type: ignore[union-attr]
        await interaction.followup.send(
            embed=self.ef.success(f"{user.mention} has been **unmuted**.  Case #{case_id}")
        )

    # ── lock / unlock ─────────────────────────────────────────────

    @app_commands.command(name="lock", description="Lock a channel so members cannot send messages.")
    @app_commands.describe(channel="Channel to lock (defaults to current).", reason="Reason.")
    @app_commands.default_permissions(manage_roles=True)
    @app_commands.checks.has_permissions(manage_roles=True)
    @app_commands.checks.bot_has_permissions(manage_roles=True)
    @app_commands.checks.cooldown(1, 5)
    async def lock(self, interaction: discord.Interaction,
                   channel: Optional[discord.TextChannel] = None,
                   reason: Optional[str] = "No reason provided."):
        assert interaction.guild
        target = channel or interaction.channel
        if not isinstance(target, discord.TextChannel):
            return await interaction.response.send_message(
                embed=self.ef.error("Can only lock text channels."), ephemeral=False
            )
        await interaction.response.defer(ephemeral=False)
        everyone = interaction.guild.default_role
        await target.set_permissions(everyone, send_messages=False, reason=f"Locked by {interaction.user}: {reason}")
        await interaction.followup.send(
            embed=self.ef.build(
                title=f"{self.ef.e['warning']}  Channel Locked",
                description=f"{target.mention} has been **locked**.\n**Reason:** {reason}",
                color_key="warning",
            )
        )

    @app_commands.command(name="unlock", description="Unlock a previously locked channel.")
    @app_commands.describe(channel="Channel to unlock (defaults to current).", reason="Reason.")
    @app_commands.default_permissions(manage_roles=True)
    @app_commands.checks.has_permissions(manage_roles=True)
    @app_commands.checks.bot_has_permissions(manage_roles=True)
    @app_commands.checks.cooldown(1, 5)
    async def unlock(self, interaction: discord.Interaction,
                     channel: Optional[discord.TextChannel] = None,
                     reason: Optional[str] = "No reason provided."):
        assert interaction.guild
        target = channel or interaction.channel
        if not isinstance(target, discord.TextChannel):
            return await interaction.response.send_message(
                embed=self.ef.error("Can only unlock text channels."), ephemeral=False
            )
        await interaction.response.defer(ephemeral=False)
        everyone = interaction.guild.default_role
        ow = target.overwrites_for(everyone)
        ow.send_messages = None
        await target.set_permissions(everyone, overwrite=ow, reason=f"Unlocked by {interaction.user}: {reason}")
        await interaction.followup.send(
            embed=self.ef.success(f"{target.mention} has been **unlocked**.")
        )

    # ── refresh channel ──────────────────────────────────────────

    @app_commands.command(name="refreshchannel", description="Clone and recreate a channel, clearing all messages.")
    @app_commands.default_permissions(manage_channels=True)
    @app_commands.describe(channel="Channel to refresh (defaults to current).")
    @app_commands.checks.has_permissions(manage_channels=True)
    @app_commands.checks.bot_has_permissions(manage_channels=True)
    @app_commands.checks.cooldown(1, 30)
    async def refreshchannel(self, interaction: discord.Interaction,
                              channel: Optional[discord.TextChannel] = None):
        assert interaction.guild
        target = channel or interaction.channel
        if not isinstance(target, discord.TextChannel):
            return await interaction.response.send_message(
                embed=self.ef.error("Can only refresh text channels."), ephemeral=False
            )
        view = ConfirmView()
        await interaction.response.send_message(
            embed=self.ef.warning(
                f"This will **delete all messages** in {target.mention} by recreating it.\n"
                f"This cannot be undone.",
                title=f"{self.ef.e['warning']}  Confirm Refresh",
            ),
            view=view, ephemeral=False,
        )
        await view.wait()
        if not view.confirmed:
            return
        new_ch = await target.clone(reason=f"Channel refreshed by {interaction.user}")
        await new_ch.edit(position=target.position)
        await target.delete(reason=f"Channel refreshed by {interaction.user}")
        await new_ch.send(
            embed=self.ef.success(f"Channel refreshed by {interaction.user.mention}.")
        )

    # ── automod ───────────────────────────────────────────────────

    automod_group = app_commands.Group(
        name="automod",
        description="Configure Discord's built-in AutoMod.",
        default_permissions=discord.Permissions(manage_guild=True),
    )

    @automod_group.command(name="list", description="List all active AutoMod rules in this server.")
    async def automod_list(self, interaction: discord.Interaction):
        assert interaction.guild
        await interaction.response.defer(ephemeral=False)
        try:
            rules = await interaction.guild.fetch_automod_rules()
        except discord.Forbidden:
            return await interaction.followup.send(embed=self.ef.error("Missing permission to view AutoMod rules."))
        if not rules:
            return await interaction.followup.send(embed=self.ef.info("No AutoMod rules configured."))
        e = self.ef.build(author_name="AutoMod Rules", color_key="accent")
        for rule in rules:
            status = self.ef.e["enabled"] if rule.enabled else self.ef.e["disabled"]
            e.add_field(
                name  = f"`{rule.name}`",
                value = f"{status} · ID: `{rule.id}`\nTrigger: `{rule.trigger.type.name}`",
                inline=False,
            )
        await interaction.followup.send(embed=e)

    @automod_group.command(name="spam", description="Enable or disable Discord's built-in spam detection.")
    @app_commands.describe(enable="True to enable, False to disable.")
    async def automod_spam(self, interaction: discord.Interaction, enable: bool):
        assert interaction.guild
        await interaction.response.defer(ephemeral=False)
        try:
            rules = await interaction.guild.fetch_automod_rules()
            existing = next((r for r in rules if r.trigger.type == discord.AutoModRuleTriggerType.spam), None)
            if existing:
                await existing.edit(enabled=enable)
                action = "enabled" if enable else "disabled"
                return await interaction.followup.send(embed=self.ef.success(f"Spam filter **{action}**."))
            if not enable:
                return await interaction.followup.send(embed=self.ef.info("No spam filter rule exists to disable."))
            await interaction.guild.create_automod_rule(
                name    = "Nana — Spam Filter",
                event_type = discord.AutoModRuleEventType.message_send,
                trigger = discord.AutoModTrigger(type=discord.AutoModRuleTriggerType.spam),
                actions = [discord.AutoModRuleAction(type=discord.AutoModRuleActionType.block_message)],
                enabled = True,
                reason  = f"Spam automod set up by {interaction.user}",
            )
            await interaction.followup.send(embed=self.ef.success("Spam filter **enabled**."))
        except discord.Forbidden:
            await interaction.followup.send(embed=self.ef.error("Missing permission to manage AutoMod rules."))
        except discord.HTTPException as e:
            await interaction.followup.send(embed=self.ef.error(f"Failed: {e}"))

    @automod_group.command(name="invites", description="Block Discord invite links automatically.")
    @app_commands.describe(enable="True to enable, False to disable.")
    async def automod_invites(self, interaction: discord.Interaction, enable: bool):
        assert interaction.guild
        await interaction.response.defer(ephemeral=False)
        try:
            rules    = await interaction.guild.fetch_automod_rules()
            existing = next(
                (r for r in rules
                 if r.trigger.type == discord.AutoModRuleTriggerType.keyword
                 and r.name == "Nana — Block Invites"),
                None,
            )
            if existing:
                await existing.edit(enabled=enable)
                action = "enabled" if enable else "disabled"
                return await interaction.followup.send(embed=self.ef.success(f"Invite blocker **{action}**."))
            if not enable:
                return await interaction.followup.send(embed=self.ef.info("No invite blocker rule exists to disable."))
            await interaction.guild.create_automod_rule(
                name       = "Nana — Block Invites",
                event_type = discord.AutoModRuleEventType.message_send,
                trigger    = discord.AutoModTrigger(
                    type=discord.AutoModRuleTriggerType.keyword,
                    keyword_filter=["discord.gg/*", "discord.com/invite/*", "discordapp.com/invite/*"],
                ),
                actions    = [discord.AutoModRuleAction(type=discord.AutoModRuleActionType.block_message)],
                enabled    = True,
                reason     = f"Invite blocker set up by {interaction.user}",
            )
            await interaction.followup.send(embed=self.ef.success("Invite blocker **enabled**."))
        except discord.Forbidden:
            await interaction.followup.send(embed=self.ef.error("Missing permission to manage AutoMod rules."))
        except discord.HTTPException as e:
            await interaction.followup.send(embed=self.ef.error(f"Failed: {e}"))

    @automod_group.command(name="keywords", description="Block custom keywords automatically.")
    @app_commands.describe(words="Space-separated list of words or phrases to block.", enable="True to enable, False to disable.")
    async def automod_keywords(self, interaction: discord.Interaction, words: str, enable: bool = True):
        assert interaction.guild
        await interaction.response.defer(ephemeral=False)
        keyword_list = [w.strip() for w in words.split() if w.strip()]
        if not keyword_list:
            return await interaction.followup.send(embed=self.ef.error("Please provide at least one keyword."))
        try:
            rules    = await interaction.guild.fetch_automod_rules()
            existing = next(
                (r for r in rules
                 if r.trigger.type == discord.AutoModRuleTriggerType.keyword
                 and r.name == "Nana — Keyword Filter"),
                None,
            )
            if existing:
                current = existing.trigger.keyword_filter or []
                merged  = list(set(current) | set(keyword_list))
                await existing.edit(
                    enabled = enable,
                    trigger = discord.AutoModTrigger(
                        type=discord.AutoModRuleTriggerType.keyword,
                        keyword_filter=merged,
                    ),
                )
                return await interaction.followup.send(
                    embed=self.ef.success(
                        f"Keyword filter updated with `{len(keyword_list)}` new word(s).\n"
                        f"Total blocked: **{len(merged)}** keywords."
                    )
                )
            await interaction.guild.create_automod_rule(
                name       = "Nana — Keyword Filter",
                event_type = discord.AutoModRuleEventType.message_send,
                trigger    = discord.AutoModTrigger(
                    type=discord.AutoModRuleTriggerType.keyword,
                    keyword_filter=keyword_list,
                ),
                actions    = [discord.AutoModRuleAction(type=discord.AutoModRuleActionType.block_message)],
                enabled    = enable,
                reason     = f"Keyword filter set up by {interaction.user}",
            )
            await interaction.followup.send(
                embed=self.ef.success(f"Keyword filter created with **{len(keyword_list)}** word(s).")
            )
        except discord.Forbidden:
            await interaction.followup.send(embed=self.ef.error("Missing permission to manage AutoMod rules."))
        except discord.HTTPException as e:
            await interaction.followup.send(embed=self.ef.error(f"Failed: {e}"))

    @automod_group.command(name="mentions", description="Block messages with too many mentions.")
    @app_commands.describe(limit="Max allowed mentions per message (2-50).")
    async def automod_mentions(self, interaction: discord.Interaction,
                                limit: app_commands.Range[int, 2, 50] = 5):
        assert interaction.guild
        await interaction.response.defer(ephemeral=False)
        try:
            rules    = await interaction.guild.fetch_automod_rules()
            existing = next(
                (r for r in rules if r.trigger.type == discord.AutoModRuleTriggerType.mention_spam),
                None,
            )
            if existing:
                await existing.edit(
                    enabled = True,
                    trigger = discord.AutoModTrigger(
                        type=discord.AutoModRuleTriggerType.mention_spam,
                        mention_total_limit=limit,
                    ),
                )
                return await interaction.followup.send(
                    embed=self.ef.success(f"Mention spam filter updated — max **{limit}** mentions per message.")
                )
            await interaction.guild.create_automod_rule(
                name       = "Nana — Mention Spam",
                event_type = discord.AutoModRuleEventType.message_send,
                trigger    = discord.AutoModTrigger(
                    type=discord.AutoModRuleTriggerType.mention_spam,
                    mention_total_limit=limit,
                ),
                actions    = [discord.AutoModRuleAction(type=discord.AutoModRuleActionType.block_message)],
                enabled    = True,
                reason     = f"Mention spam filter set up by {interaction.user}",
            )
            await interaction.followup.send(
                embed=self.ef.success(f"Mention spam filter enabled — max **{limit}** mentions per message.")
            )
        except discord.Forbidden:
            await interaction.followup.send(embed=self.ef.error("Missing permission to manage AutoMod rules."))
        except discord.HTTPException as e:
            await interaction.followup.send(embed=self.ef.error(f"Failed: {e}"))

    @automod_group.command(name="disable", description="Disable an AutoMod rule by its ID.")
    @app_commands.describe(rule_id="Rule ID from /automod list.")
    async def automod_disable(self, interaction: discord.Interaction, rule_id: str):
        assert interaction.guild
        await interaction.response.defer(ephemeral=False)
        try:
            rule = await interaction.guild.fetch_automod_rule(int(rule_id))
            await rule.edit(enabled=False)
            await interaction.followup.send(embed=self.ef.success(f"Rule `{rule.name}` disabled."))
        except (ValueError, discord.NotFound):
            await interaction.followup.send(embed=self.ef.error("Rule not found. Use `/automod list` to get IDs."))
        except discord.Forbidden:
            await interaction.followup.send(embed=self.ef.error("Missing permission to manage AutoMod rules."))

    # ── steal emojis ──────────────────────────────────────────────

    @app_commands.command(name="steal", description="Add custom emojis from other servers to this one.")
    @app_commands.default_permissions(manage_expressions=True)
    @app_commands.describe(
        emojis="Paste one or more custom emojis (e.g. `<:name:123> <a:foo:456>`).",
        count="How many to add (default: as many as fit). 0 = all that fit.",
    )
    @app_commands.checks.has_permissions(manage_expressions=True)
    @app_commands.checks.bot_has_permissions(manage_expressions=True)
    @app_commands.checks.cooldown(1, 10)
    async def steal(
        self, interaction: discord.Interaction,
        emojis: str,
        count: app_commands.Range[int, 0, 50] = 0,
    ):
        assert interaction.guild
        await interaction.response.defer(ephemeral=False)

        matches = EMOJI_PATTERN.findall(emojis)
        if not matches:
            return await interaction.followup.send(
                embed=self.ef.error(
                    "No custom emojis found in your input. "
                    "Paste them like `<:name:123>` or `<a:name:456>`."
                )
            )

        # Deduplicate by emoji ID, preserving order.
        seen: set[str] = set()
        parsed: list[tuple[bool, str, int]] = []
        for animated, name, eid in matches:
            if eid in seen:
                continue
            seen.add(eid)
            parsed.append((bool(animated), name, int(eid)))

        # Compute remaining capacity, separately for static vs animated.
        guild = interaction.guild
        limit = guild.emoji_limit
        static_used    = sum(1 for e in guild.emojis if not e.animated)
        animated_used  = sum(1 for e in guild.emojis if e.animated)
        static_left    = max(0, limit - static_used)
        animated_left  = max(0, limit - animated_used)

        if static_left == 0 and animated_left == 0:
            return await interaction.followup.send(
                embed=self.ef.error(
                    f"This server is at its emoji limit ({limit} static + {limit} animated)."
                )
            )

        # Cap by user-requested count.
        max_to_add = count if count > 0 else len(parsed)

        added: list[discord.Emoji] = []
        skipped: list[str] = []
        failed:  list[str] = []

        for animated, name, eid in parsed:
            if len(added) >= max_to_add:
                break
            if animated and animated_left <= 0:
                skipped.append(f"`:{name}:` (animated slots full)")
                continue
            if not animated and static_left <= 0:
                skipped.append(f"`:{name}:` (static slots full)")
                continue

            ext = "gif" if animated else "png"
            url = f"https://cdn.discordapp.com/emojis/{eid}.{ext}"
            try:
                img = await self.bot.http.get_from_cdn(url)
            except (discord.NotFound, discord.HTTPException) as e:
                failed.append(f"`:{name}:` (couldn't fetch image: {getattr(e, 'status', '?')})")
                continue
            except Exception as e:
                failed.append(f"`:{name}:` (fetch error: {type(e).__name__})")
                continue

            try:
                new_emoji = await guild.create_custom_emoji(
                    name=name, image=img,
                    reason=f"Stolen by {interaction.user}",
                )
                added.append(new_emoji)
                if animated:
                    animated_left -= 1
                else:
                    static_left -= 1
            except discord.HTTPException as e:
                failed.append(f"`:{name}:` ({e.text or 'API error'})")

            await asyncio.sleep(0.5)  # gentle on the rate limiter

        # Build the result embed.
        parts: list[str] = []
        if added:
            preview = " ".join(str(e) for e in added[:30])
            extra = f" *(+{len(added) - 30} more)*" if len(added) > 30 else ""
            parts.append(f"**Added {len(added)}:** {preview}{extra}")
        if skipped:
            parts.append(f"**Skipped {len(skipped)}:**\n" + "\n".join(skipped[:10]))
        if failed:
            parts.append(f"**Failed {len(failed)}:**\n" + "\n".join(failed[:10]))
        if not parts:
            parts.append("Nothing happened.")

        color = "success" if added and not failed else ("warning" if added else "error")
        e = self.ef.build(
            title=f"{self.ef.e['enabled']}  Emoji Steal",
            description="\n\n".join(parts),
            color_key=color,
        )
        e.set_footer(text=f"Slots left  ·  {static_left} static  ·  {animated_left} animated")
        await interaction.followup.send(embed=e)

    # ── error handler ─────────────────────────────────────────────

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.MissingPermissions):
            embed = self.ef.error("You don't have permission for this command.")
        elif isinstance(error, app_commands.BotMissingPermissions):
            embed = self.ef.error("I'm missing required permissions.")
        elif isinstance(error, app_commands.CommandOnCooldown):
            embed = self.ef.warning(f"Slow down — try again in **{error.retry_after:.1f}s**.")
        else:
            embed = self.ef.error(str(error))
        if not interaction.response.is_done():
            await interaction.response.send_message(embed=embed, ephemeral=False)
        else:
            await interaction.followup.send(embed=embed, ephemeral=False)


async def setup(bot: commands.Bot):
    await bot.add_cog(Moderation(bot))
