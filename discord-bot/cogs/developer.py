"""
Developer cog — bot owner commands for premium management, blacklisting, and diagnostics.
Only users in config["developer_ids"] can use these commands.
"""

import discord
from discord import app_commands
from discord.ext import commands
from typing import Optional

from utils.embeds import EmbedFactory
from utils.helpers import parse_duration, send_dm
from utils.database import (
    get_premium_tier, get_premium_info, set_premium, remove_premium, get_all_premium,
    is_blacklisted, add_blacklist, remove_blacklist, get_blacklist_all,
    get_cache_stats,
)
import time


def _tier_name(config: dict, tier: int) -> str:
    tiers = config.get("premium", {}).get("tiers", {})
    info = tiers.get(str(tier), {})
    return info.get("name", f"Tier {tier}")


def _tier_emoji(config: dict, tier: int) -> str:
    tiers = config.get("premium", {}).get("tiers", {})
    info = tiers.get(str(tier), {})
    return info.get("emoji", "")


async def _send_premium_dm(bot: commands.Bot, user: discord.User, tier: int, expires_at: Optional[int]) -> bool:
    config = bot.config  # type: ignore[attr-defined]
    pe = config.get("premium", {}).get("emojis", {})
    heart = pe.get("heart", "")
    tulip = pe.get("tulip", "")
    support = config.get("premium", {}).get("support_server", "")
    name = _tier_name(config, tier)

    billing = f"<t:{expires_at}:F>" if expires_at else "managed via Patreon"

    embed = discord.Embed(
        description=(
            f"## {heart}   thank you for subscribing !\n"
            f"- you're on the **{name}** tier\n"
            f"- to receive your role, please join the [support server]({support})\n\n"
            f"- your next billing date: {billing}\n"
            f"- you can manage your subscriptions at `/premium`\n"
            f"- thank you for your support {tulip}"
        ),
        color=bot.ef.colors["success"],  # type: ignore[attr-defined]
    )
    try:
        await user.send(embed=embed)
        return True
    except (discord.Forbidden, discord.HTTPException):
        return False


class Developer(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.ef: EmbedFactory = bot.ef  # type: ignore[attr-defined]
        self.config: dict = bot.config  # type: ignore[attr-defined]

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        dev_ids = self.config.get("developer_ids", [])
        if interaction.user.id not in dev_ids:
            await interaction.response.send_message(
                embed=self.ef.error("This command is restricted to bot developers."),
                ephemeral=True,
            )
            return False
        return True

    DEV_GUILD_ID = 1492674952447393913

    dev_group = app_commands.Group(
        name="dev",
        description="Developer-only commands.",
        guild_ids=[DEV_GUILD_ID],
        default_permissions=discord.Permissions(administrator=True),
    )

    prem_group = app_commands.Group(name="premium", description="Premium management.", parent=dev_group)

    @prem_group.command(name="add", description="Grant premium to a user.")
    @app_commands.describe(user="User to grant premium.", tier="Tier level (1-3).", duration="Duration (e.g. 30d, 1w). Omit for permanent.")
    async def prem_add(
        self,
        interaction: discord.Interaction,
        user: discord.User,
        tier: app_commands.Range[int, 1, 3],
        duration: Optional[str] = None,
    ):
        expires_at = None
        if duration:
            td = parse_duration(duration)
            if not td:
                return await interaction.response.send_message(
                    embed=self.ef.error("Invalid duration format."), ephemeral=True
                )
            expires_at = int(time.time() + td.total_seconds())

        await set_premium(user.id, tier, expires_at=expires_at, added_by=interaction.user.id)
        name = _tier_name(self.config, tier)
        emoji = _tier_emoji(self.config, tier)

        dm_sent = await _send_premium_dm(self.bot, user, tier, expires_at)
        dm_note = "DM sent" if dm_sent else "DM failed (user has DMs closed)"

        exp_str = f"<t:{expires_at}:F>" if expires_at else "Permanent"
        await interaction.response.send_message(
            embed=self.ef.success(
                f"Granted **{emoji} {name}** to {user.mention}.\n"
                f"Expires: {exp_str}\n{dm_note}"
            ),
            ephemeral=True,
        )

    @prem_group.command(name="remove", description="Remove premium from a user.")
    @app_commands.describe(user="User to remove premium from.")
    async def prem_remove(self, interaction: discord.Interaction, user: discord.User):
        removed = await remove_premium(user.id)
        if removed:
            await interaction.response.send_message(
                embed=self.ef.success(f"Premium removed from {user.mention}."), ephemeral=True
            )
        else:
            await interaction.response.send_message(
                embed=self.ef.error(f"{user.mention} doesn't have premium."), ephemeral=True
            )

    @prem_group.command(name="check", description="Check a user's premium status.")
    @app_commands.describe(user="User to check.")
    async def prem_check(self, interaction: discord.Interaction, user: discord.User):
        info = await get_premium_info(user.id)
        if not info:
            return await interaction.response.send_message(
                embed=self.ef.info(f"{user.mention} has no premium."), ephemeral=True
            )
        tier = info["tier"]
        name = _tier_name(self.config, tier)
        emoji = _tier_emoji(self.config, tier)
        exp = f"<t:{info['expires_at']}:F>" if info.get("expires_at") else "Permanent"
        added = f"<@{info['added_by']}>" if info.get("added_by") else "Patreon"

        e = self.ef.build(author_name=f"Premium  {self.ef.e['dot']}  {user.display_name}", color_key="accent")
        e.add_field(name="Tier",    value=f"{emoji} {name}", inline=True)
        e.add_field(name="Expires", value=exp,                inline=True)
        e.add_field(name="Added By", value=added,             inline=True)
        e.add_field(name="Since",   value=f"<t:{info['created_at']}:R>", inline=True)
        await interaction.response.send_message(embed=e, ephemeral=True)

    @prem_group.command(name="list", description="List all premium users.")
    async def prem_list(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        users = await get_all_premium()
        if not users:
            return await interaction.followup.send(
                embed=self.ef.info("No premium users."), ephemeral=True
            )
        e = self.ef.build(author_name="Premium Users", color_key="accent")
        for u in users[:25]:
            name = _tier_name(self.config, u["tier"])
            emoji = _tier_emoji(self.config, u["tier"])
            exp = f"<t:{u['expires_at']}:R>" if u.get("expires_at") else "Perm"
            e.add_field(
                name=f"{emoji} <@{u['user_id']}>",
                value=f"{name}  {self.ef.e['dot']}  {exp}",
                inline=False,
            )
        if len(users) > 25:
            e.set_footer(text=f"Showing 25/{len(users)} users")
        await interaction.followup.send(embed=e, ephemeral=True)

    bl_group = app_commands.Group(name="blacklist", description="Blacklist management.", parent=dev_group)

    @bl_group.command(name="add", description="Blacklist a user or guild.")
    @app_commands.describe(target_id="User or guild ID.", target_type="'user' or 'guild'.", reason="Reason.")
    async def bl_add(
        self,
        interaction: discord.Interaction,
        target_id: str,
        target_type: Optional[str] = "user",
        reason: Optional[str] = "No reason provided.",
    ):
        try:
            tid = int(target_id)
        except ValueError:
            return await interaction.response.send_message(
                embed=self.ef.error("Invalid ID."), ephemeral=True
            )
        t_type = "guild" if target_type and target_type.lower() == "guild" else "user"
        await add_blacklist(tid, t_type, reason or "", interaction.user.id)
        await interaction.response.send_message(
            embed=self.ef.success(f"Blacklisted {t_type} `{tid}`.  Reason: {reason}"), ephemeral=True
        )

        if t_type == "guild":
            g = self.bot.get_guild(tid)
            if g:
                try:
                    await g.leave()
                except Exception:
                    pass

    @bl_group.command(name="remove", description="Remove from blacklist.")
    @app_commands.describe(target_id="User or guild ID.")
    async def bl_remove(self, interaction: discord.Interaction, target_id: str):
        try:
            tid = int(target_id)
        except ValueError:
            return await interaction.response.send_message(
                embed=self.ef.error("Invalid ID."), ephemeral=True
            )
        removed = await remove_blacklist(tid)
        if removed:
            await interaction.response.send_message(
                embed=self.ef.success(f"Removed `{tid}` from blacklist."), ephemeral=True
            )
        else:
            await interaction.response.send_message(
                embed=self.ef.error(f"`{tid}` is not blacklisted."), ephemeral=True
            )

    @bl_group.command(name="list", description="View all blacklisted entries.")
    async def bl_list(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        entries = await get_blacklist_all()
        if not entries:
            return await interaction.followup.send(
                embed=self.ef.info("Blacklist is empty."), ephemeral=True
            )
        e = self.ef.build(author_name="Blacklist", color_key="error")
        for entry in entries[:25]:
            e.add_field(
                name=f"`{entry['target_id']}`  ({entry['target_type']})",
                value=f"{entry.get('reason', 'No reason')}  {self.ef.e['dot']}  <t:{entry['added_at']}:R>",
                inline=False,
            )
        await interaction.followup.send(embed=e, ephemeral=True)

    @dev_group.command(name="stats", description="Bot diagnostics and cache stats.")
    async def stats(self, interaction: discord.Interaction):
        stats = get_cache_stats()
        e = self.ef.build(author_name="Bot Diagnostics", color_key="accent")
        e.add_field(name="Guilds",     value=f"`{len(self.bot.guilds)}`",       inline=True)
        e.add_field(name="Users",      value=f"`{len(self.bot.users)}`",        inline=True)
        e.add_field(name="Latency",    value=f"`{self.bot.latency*1000:.1f}ms`", inline=True)
        e.add_field(name="Cache Keys", value=f"`{stats['keys']}`",              inline=True)
        e.add_field(name="Hit Rate",   value=f"`{stats['hit_rate']}`",          inline=True)
        e.add_field(name="Cogs",       value=f"`{len(self.bot.cogs)}`",         inline=True)
        await interaction.response.send_message(embed=e, ephemeral=True)

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        embed = self.ef.error(str(error))
        if not interaction.response.is_done():
            await interaction.response.send_message(embed=embed, ephemeral=True)
        else:
            await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Developer(bot))
