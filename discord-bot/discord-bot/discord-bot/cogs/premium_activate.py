"""
Server premium activation system — premium members can activate their tier
benefits on a single server at a time. Like Nitro server boost slots:

  • /activate premium <server_id>  → activates for 14 days
  • /activation switch <new_id>    → moves the activation (1-week cooldown)
  • /activation status             → shows current activation
"""

import discord
from discord import app_commands
from discord.ext import commands
import time

from utils.embeds import EmbedFactory
from utils.database import (
    get_premium_tier, get_user_activation, get_server_premium,
    get_server_activations, activate_server_premium, switch_server_premium,
    remove_server_premium, ACTIVATION_DURATION_S, SWITCH_COOLDOWN_S,
)


def _fmt_dur(secs: int) -> str:
    if secs <= 0:
        return "0s"
    days, rem = divmod(secs, 86400)
    h,    rem = divmod(rem,  3600)
    m,    _   = divmod(rem,    60)
    parts = []
    if days: parts.append(f"{days}d")
    if h:    parts.append(f"{h}h")
    if m and not days: parts.append(f"{m}m")
    return " ".join(parts) or f"{secs}s"


class PremiumActivate(commands.Cog, name="Premium Activation"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.ef: EmbedFactory = bot.ef  # type: ignore[attr-defined]
        self.config: dict = bot.config  # type: ignore[attr-defined]

    # ── /activate premium <server_id> ────────────────────────────────────────

    activate_group = app_commands.Group(
        name="activate",
        description="Activate your premium benefits on a server.",
    )

    @activate_group.command(name="premium",
                            description="Activate your premium tier on this or another server (2 weeks).")
    @app_commands.describe(
        server_id="The server ID to activate premium on. Defaults to this server.",
    )
    async def activate_premium(self, interaction: discord.Interaction,
                               server_id: str | None = None) -> None:
        await interaction.response.defer(ephemeral=False)

        tier = await get_premium_tier(interaction.user.id)
        if tier < 1:
            return await interaction.followup.send(
                embed=self.ef.error(
                    "You don't have a premium membership.\n"
                    "Use `/premium` to learn how to become a premium member."
                ),
            )

        # Resolve target guild
        if server_id is None:
            if not interaction.guild:
                return await interaction.followup.send(
                    embed=self.ef.error("Please provide a `server_id` when running this in DMs."),
                )
            target_id = interaction.guild.id
        else:
            try:
                target_id = int(server_id.strip())
            except ValueError:
                return await interaction.followup.send(
                    embed=self.ef.error("Server ID must be a number."),
                )

        target_guild = self.bot.get_guild(target_id)
        if target_guild is None:
            return await interaction.followup.send(
                embed=self.ef.error(
                    f"I'm not in a server with ID `{target_id}`.\n"
                    "Invite me there first, then run this command again."
                ),
            )
        if not target_guild.get_member(interaction.user.id):
            return await interaction.followup.send(
                embed=self.ef.error(f"You aren't a member of **{target_guild.name}**."),
            )

        # Check existing activation
        existing = await get_user_activation(interaction.user.id)
        if existing and existing["expires_at"] > int(time.time()):
            if existing["guild_id"] == target_id:
                left = existing["expires_at"] - int(time.time())
                return await interaction.followup.send(
                    embed=self.ef.info(
                        f"Premium is already active on **{target_guild.name}**.\n"
                        f"Time remaining: **{_fmt_dur(left)}**"
                    ),
                )
            # Different guild — must use /activation switch
            current_g = self.bot.get_guild(existing["guild_id"])
            return await interaction.followup.send(
                embed=self.ef.error(
                    f"Your premium is already active on **"
                    f"{current_g.name if current_g else existing['guild_id']}**.\n"
                    f"Use `/activation switch` to move it to **{target_guild.name}**."
                ),
            )

        # Activate
        info = await activate_server_premium(interaction.user.id, target_id, tier)
        e = self.ef.success(
            f"**You activated premium**\n"
            f"{self.ef.e['arrow']} you successfully activated premium for `{target_id}`\n"
            f"{self.ef.e['arrow']} you can switch your activation in **1 week**\n"
            f"{self.ef.e['arrow']} this activation expires <t:{info['expires_at']}:R>\n"
            f"{self.ef.e['arrow']} to switch your activation use `/activation switch`"
        )
        await interaction.followup.send(embed=e)

    # ── /activation group ────────────────────────────────────────────────────

    activation_group = app_commands.Group(
        name="activation",
        description="Manage your premium activation slot.",
    )

    @activation_group.command(name="switch",
                              description="Move your premium activation to another server (1-week cooldown).")
    @app_commands.describe(server_id="The server ID to move your activation to.")
    async def activation_switch(self, interaction: discord.Interaction,
                                server_id: str) -> None:
        await interaction.response.defer(ephemeral=False)

        tier = await get_premium_tier(interaction.user.id)
        if tier < 1:
            return await interaction.followup.send(
                embed=self.ef.error("You don't have a premium membership."),
            )

        existing = await get_user_activation(interaction.user.id)
        now = int(time.time())
        if not existing or existing["expires_at"] <= now:
            return await interaction.followup.send(
                embed=self.ef.error(
                    "You don't have an active activation to switch.\n"
                    "Use `/activate premium` to activate first."
                ),
            )

        try:
            new_id = int(server_id.strip())
        except ValueError:
            return await interaction.followup.send(
                embed=self.ef.error("Server ID must be a number."),
            )

        if existing["guild_id"] == new_id:
            return await interaction.followup.send(
                embed=self.ef.error("Premium is already active on that server."),
            )

        new_guild = self.bot.get_guild(new_id)
        if new_guild is None:
            return await interaction.followup.send(
                embed=self.ef.error(
                    f"I'm not in a server with ID `{new_id}`.\nInvite me first."
                ),
            )
        if not new_guild.get_member(interaction.user.id):
            return await interaction.followup.send(
                embed=self.ef.error(f"You aren't a member of **{new_guild.name}**."),
            )

        # Cooldown check
        elapsed = now - existing["last_switched_at"]
        if elapsed < SWITCH_COOLDOWN_S:
            wait = SWITCH_COOLDOWN_S - elapsed
            ready_ts = now + wait
            return await interaction.followup.send(
                embed=self.ef.error(
                    f"You can switch your activation in **{_fmt_dur(wait)}** "
                    f"(<t:{ready_ts}:R>)."
                ),
            )

        info = await switch_server_premium(interaction.user.id, new_id, tier)
        old_guild = self.bot.get_guild(existing["guild_id"])
        e = self.ef.success(
            f"**Activation switched**\n"
            f"{self.ef.e['arrow']} moved from **"
            f"{old_guild.name if old_guild else existing['guild_id']}** "
            f"to **{new_guild.name}**\n"
            f"{self.ef.e['arrow']} new activation expires <t:{info['expires_at']}:R>\n"
            f"{self.ef.e['arrow']} you can switch again in **1 week**"
        )
        await interaction.followup.send(embed=e)

    @activation_group.command(name="status",
                              description="Check where your premium is currently activated.")
    async def activation_status(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=False)
        info = await get_user_activation(interaction.user.id)
        if not info or info["expires_at"] <= int(time.time()):
            return await interaction.followup.send(
                embed=self.ef.info(
                    "You don't have an active premium activation.\n"
                    "Use `/activate premium <server_id>` to activate it."
                ),
            )
        guild = self.bot.get_guild(info["guild_id"])
        switch_ready = info["last_switched_at"] + SWITCH_COOLDOWN_S
        now = int(time.time())
        switch_line = (
            f"can switch **now**" if switch_ready <= now
            else f"can switch <t:{switch_ready}:R>"
        )
        tier_data = self.config.get("premium", {}).get("tiers", {}).get(str(info["tier"]), {})
        tier_name = tier_data.get("name", f"Tier {info['tier']}")
        tier_emoji = tier_data.get("emoji", "")
        e = self.ef.build(
            author_name=f"Activation Status  {self.ef.e['dot']}  {interaction.user.display_name}",
            color_key="success",
        )
        e.add_field(name="Tier",       value=f"{tier_emoji} **{tier_name}**", inline=True)
        e.add_field(name="Server",
                    value=f"**{guild.name if guild else info['guild_id']}**\n`{info['guild_id']}`",
                    inline=True)
        e.add_field(name="Expires",    value=f"<t:{info['expires_at']}:R>", inline=True)
        e.add_field(name="Switching",  value=switch_line,                     inline=False)
        await interaction.followup.send(embed=e)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(PremiumActivate(bot))
