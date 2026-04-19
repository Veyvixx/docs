"""
Miscellaneous commands cog.
Commands: /ping /avatar /banner /userinfo /serverinfo /roleinfo /variables /help /whois /customize /premium
"""

import base64
import sys
import discord
from discord import app_commands
from discord.ext import commands
import time
from datetime import datetime, timezone
from typing import Optional

from utils.embeds import EmbedFactory
from utils.database import (
    ping_db, get_cache_stats, get_premium_tier, get_premium_info,
    get_guild_palette, set_guild_palette, reset_guild_palette,
    get_guild_emojis, set_guild_emojis, reset_guild_emojis,
)


class AvatarView(discord.ui.View):
    def __init__(self, member: discord.Member | discord.User, ef: EmbedFactory):
        super().__init__(timeout=60)
        self.member = member
        self.ef     = ef

    @discord.ui.button(label="Server Avatar", style=discord.ButtonStyle.secondary)
    async def server_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        e = self.ef.avatar(self.member, "Server Avatar")
        e.set_image(url=self.member.display_avatar.url)
        await interaction.response.edit_message(embed=e, view=self)

    @discord.ui.button(label="Global Avatar", style=discord.ButtonStyle.secondary)
    async def global_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        url = (
            self.member.avatar.url
            if isinstance(self.member, discord.Member) and self.member.avatar
            else self.member.default_avatar.url
        )
        e = self.ef.avatar(self.member, "Global Avatar")
        e.set_image(url=url)
        await interaction.response.edit_message(embed=e, view=self)


class WhoisView(discord.ui.View):
    def __init__(self, member: discord.Member, fetched: Optional[discord.User] = None):
        super().__init__(timeout=60)
        avatar_url = member.display_avatar.replace(format="png", size=4096).url
        self.add_item(discord.ui.Button(label="Avatar", style=discord.ButtonStyle.link, url=avatar_url))
        if fetched and fetched.banner:
            banner_url = fetched.banner.replace(format="png", size=4096).url
            self.add_item(discord.ui.Button(label="Banner", style=discord.ButtonStyle.link, url=banner_url))


HELP_CATEGORIES = [
    ("Misc", "misc", "accent", [
        ("/ping",                  "Bot & database latency"),
        ("/about",                 "Info, stats, and uptime"),
        ("/avatar [user]",         "Avatar viewer with server/global toggle"),
        ("/banner [user]",         "Profile banner"),
        ("/userinfo [user]",       "Detailed member profile"),
        ("/serverinfo",            "Server statistics"),
        ("/roleinfo <role>",       "Role details & permissions"),
        ("/whois <user>",          "Full profile with banner & badges"),
        ("/variables",             "Template variable reference"),
        ("/premium",               "Check premium status & info"),
        ("/help",                  "This help menu"),
    ]),
    ("Moderation", "mod", "error", [
        ("/ban <user>",             "Permanently ban a member"),
        ("/unban <id>",             "Unban by user ID"),
        ("/kick <user>",            "Kick a member"),
        ("/timeout <user> <dur>",   "Timeout (10m, 2h, 1d...)"),
        ("/untimeout <user>",       "Remove timeout"),
        ("/mute <user>",            "Role-based mute (optional duration)"),
        ("/unmute <user>",          "Remove role-based mute"),
        ("/jail <user>",            "Jail — restrict to jail channel"),
        ("/unjail <user>",          "Release from jail"),
        ("/lock [channel]",         "Lock a channel"),
        ("/unlock [channel]",       "Unlock a channel"),
        ("/refreshchannel",         "Recreate channel, clearing messages"),
        ("/warn <user> <reason>",   "Issue a formal warning"),
        ("/warnings <user>",        "Paginated warning history"),
        ("/clearwarnings <user>",   "Clear all warnings"),
        ("/purge <amount>",         "Bulk-delete messages"),
        ("/automod spam",           "Enable/disable spam filter"),
        ("/automod invites",        "Block Discord invite links"),
        ("/automod keywords",       "Block custom keywords"),
        ("/automod mentions",       "Limit mentions per message"),
        ("/automod list",           "View all AutoMod rules"),
        ("/automod disable",        "Disable a rule by ID"),
    ]),
    ("Anti-Nuke", "an", "warning", [
        ("/antinuke status",           "Open the protection dashboard"),
        ("/antinuke toggle",           "Enable / disable"),
        ("/antinuke config",           "Configure thresholds & punishment"),
        ("/antinuke whitelist add",    "Whitelist a trusted user"),
        ("/antinuke whitelist remove", "Remove from whitelist"),
        ("/antinuke whitelist list",   "View whitelisted users"),
        ("/antinuke logs",             "Recent suspicious activity log"),
    ]),
    ("Autoresponders", "ar", "accent", [
        ("/autoresponder create <trigger>",    "Create a trigger + response"),
        ("/autoresponder edit <trigger>",      "Edit an existing autoresponder"),
        ("/autoresponder delete <trigger>",    "Delete an autoresponder"),
        ("/autoresponder list",                "List all server autoresponders"),
        ("/autoresponder enable/disable",      "Toggle without deleting"),
        ("/autoresponder view <trigger>",      "Inspect a single autoresponder"),
        ("/autoresponder inventory save",      "Save to your personal inventory ✨"),
        ("/autoresponder inventory load",      "Import from your inventory"),
        ("/autoresponder inventory list",      "View your inventory ✨"),
        ("/autoresponder inventory delete",    "Remove from your inventory ✨"),
    ]),
    ("Buttons", "btn", "accent", [
        ("/button create linked <name>",       "Button that opens a URL"),
        ("/button create functional <name>",   "Button that runs actions/response"),
        ("/button edit <name>",                "Edit an existing button"),
        ("/button delete <name>",              "Delete a button"),
        ("/button list",                       "List all server buttons"),
        ("/button inventory save",             "Save to your inventory ✨"),
        ("/button inventory load",             "Import from your inventory ✨"),
        ("/button inventory list",             "View your button inventory ✨"),
        ("/button inventory delete",           "Remove from your inventory ✨"),
    ]),
    ("Customization", "custom", "secondary", [
        ("/set greet message/channel",  "Configure greet messages"),
        ("/set leave message/channel",  "Configure leave messages"),
        ("/set boost message/channel",  "Configure boost messages"),
        ("/set log channel",             "Set the server event log channel"),
        ("/set log disable",            "Disable event logging"),
        ("/set jail channel",           "Set the jail channel"),
        ("/test greet/leave/boost",     "Preview event messages"),
        ("/embed create <name>",        "Create a named embed"),
        ("/embed edit <name>",          "Edit an existing embed"),
        ("/embed send <name>",          "Send a saved embed"),
        ("/embed list",                 "List all saved embeds"),
        ("/embed inventory",            "Your personal embed inventory"),
        ("/autorole set/toggle",        "Auto-assign role on join"),
        ("/modlog <channel>",           "Set mod-log channel"),
        ("/settings",                   "Settings dashboard"),
        ("/customize avatar/banner/bio","Customize bot server profile"),
        ("/customize palette set",      "Set a custom embed color palette ✨"),
        ("/customize palette view",     "Preview your saved palette"),
        ("/customize palette reset",    "Restore the default palette"),
        ("/customize emojis set",       "Set custom status emojis ✨"),
        ("/customize emojis view",      "Preview your saved emoji set"),
        ("/customize emojis reset",     "Restore the default emojis"),
    ]),
    ("Premium", "prem", "secondary", [
        ("/premium",                       "View your premium status & tiers"),
        ("/activate premium [server_id]",  "Activate your premium on a server (2 weeks)"),
        ("/activation switch <server_id>", "Move your activation (1-week cooldown)"),
        ("/activation status",             "View your current activation"),
    ]),
]


_HEX_RE = __import__("re").compile(r"^#?[0-9A-Fa-f]{6}$")


def _to_hex(v) -> str:
    """Coerce a config color value (int or '#RRGGBB' string) to '#RRGGBB'."""
    if isinstance(v, int):
        return f"#{v:06X}"
    if isinstance(v, str):
        return v if v.startswith("#") else f"#{v}"
    return "#F2EAEA"


class PaletteModal(discord.ui.Modal, title="Custom Embed Palette"):
    """Discord modals allow up to 5 components, so warning shares the error color."""

    def __init__(self, ef: EmbedFactory, defaults: dict, current: dict):
        super().__init__()
        self.ef = ef
        self._fields: dict[str, discord.ui.TextInput] = {}
        # Limited to 5 inputs (Discord modal hard cap).
        for key in ("primary", "secondary", "accent", "success", "error"):
            placeholder = _to_hex(current.get(key) or defaults.get(key))
            ti = discord.ui.TextInput(
                label=key.capitalize(),
                placeholder=placeholder,
                default=str(current.get(key, "")),
                required=False,
                max_length=7,
            )
            self.add_item(ti)
            self._fields[key] = ti

    async def on_submit(self, interaction: discord.Interaction):
        assert interaction.guild
        palette = {}
        for key, ti in self._fields.items():
            v = (ti.value or "").strip()
            if not v:
                continue
            if not _HEX_RE.match(v):
                return await interaction.response.send_message(
                    embed=self.ef.error(f"`{key}`: `{v}` is not a valid hex color (e.g. `#F2EAEA`)."),
                    ephemeral=False,
                )
            palette[key] = v if v.startswith("#") else f"#{v}"
        if not palette:
            return await interaction.response.send_message(
                embed=self.ef.error("No colors provided."), ephemeral=False
            )
        # warning shares error since modals max out at 5 inputs.
        if "error" in palette and "warning" not in palette:
            palette["warning"] = palette["error"]
        await set_guild_palette(interaction.guild.id, palette)
        e = self.ef.success("Custom palette saved.")
        for k, v in palette.items():
            e.add_field(name=k.capitalize(), value=f"`{v}`", inline=True)
        await interaction.response.send_message(embed=e, ephemeral=False)


class EmojisModal(discord.ui.Modal, title="Custom Status Emojis"):
    def __init__(self, ef: EmbedFactory, defaults: dict, current: dict):
        super().__init__()
        self.ef = ef
        self._fields: dict[str, discord.ui.TextInput] = {}
        # Limited to 5 inputs (Discord modal hard cap).
        for key in ("success", "error", "warning", "ping", "dot"):
            ph = str(current.get(key) or defaults.get(key, ""))[:100]
            ti = discord.ui.TextInput(
                label=key.capitalize(),
                placeholder=ph,
                default=str(current.get(key, "")),
                required=False,
                max_length=100,
            )
            self.add_item(ti)
            self._fields[key] = ti

    async def on_submit(self, interaction: discord.Interaction):
        assert interaction.guild
        emojis = {k: ti.value.strip() for k, ti in self._fields.items() if (ti.value or "").strip()}
        if not emojis:
            return await interaction.response.send_message(
                embed=self.ef.error("No emojis provided."), ephemeral=False
            )
        await set_guild_emojis(interaction.guild.id, emojis)
        e = self.ef.success("Custom emojis saved.")
        for k, v in emojis.items():
            e.add_field(name=k.capitalize(), value=v, inline=True)
        await interaction.response.send_message(embed=e, ephemeral=False)


class HelpSelect(discord.ui.Select):
    def __init__(self, ef: EmbedFactory):
        self.ef = ef
        opts = [
            discord.SelectOption(label=label, value=key)
            for label, key, _, _ in HELP_CATEGORIES
        ]
        super().__init__(placeholder="Choose a category...", options=opts)

    async def callback(self, interaction: discord.Interaction):
        row = next(r for r in HELP_CATEGORIES if r[1] == self.values[0])
        label, _, color_key, commands = row
        e = self.ef.build(title=label, color_key=color_key)
        for name, desc in commands:
            e.add_field(name=f"`{name}`", value=f"{self.ef.e['arrow']} {desc}", inline=False)
        await interaction.response.edit_message(embed=e)


class HelpView(discord.ui.View):
    def __init__(self, ef: EmbedFactory):
        super().__init__(timeout=120)
        self.add_item(HelpSelect(ef))


class Misc(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.ef: EmbedFactory = bot.ef  # type: ignore[attr-defined]
        self.config: dict = bot.config  # type: ignore[attr-defined]

    @app_commands.command(name="ping", description="Check bot latency and database response time.")
    @app_commands.checks.cooldown(1, 5)
    async def ping(self, interaction: discord.Interaction):
        gw_ms = self.bot.latency * 1000
        t0    = time.monotonic()
        await ping_db()
        db_ms = (time.monotonic() - t0) * 1000

        e     = self.ef.ping(gw_ms, db_ms)
        stats = get_cache_stats()
        e.add_field(
            name  = "Cache",
            value = f"`{stats['keys']}` keys  {self.ef.e['dot']}  `{stats['hit_rate']}` hit rate",
            inline=True,
        )
        await interaction.response.send_message(embed=e)

    @app_commands.command(name="about", description="Info, stats, and uptime for Nana.")
    @app_commands.checks.cooldown(1, 10)
    async def about(self, interaction: discord.Interaction):
        bot = self.bot
        start_time: datetime = getattr(bot, "start_time", datetime.now(timezone.utc))
        now = datetime.now(timezone.utc)

        # Uptime
        delta       = now - start_time
        total_secs  = int(delta.total_seconds())
        d, rem      = divmod(total_secs, 86400)
        h, rem      = divmod(rem, 3600)
        m, s        = divmod(rem, 60)
        parts = []
        if d: parts.append(f"{d}d")
        if h: parts.append(f"{h}h")
        if m: parts.append(f"{m}m")
        parts.append(f"{s}s")
        uptime_str = " ".join(parts)

        # Versions
        py_ver  = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        dpy_ver = discord.__version__

        # Stats
        guilds = len(bot.guilds)
        users  = sum(g.member_count or 0 for g in bot.guilds)

        # Timestamps
        restart_ts = int(start_time.timestamp())

        e = self.ef.build(
            description=(
                "*powerful features. soft aesthetic. "
                "__made with love for your server__ ♡*"
            ),
            color_key="primary",
        )
        if bot.user:
            e.set_thumbnail(url=bot.user.display_avatar.url)

        e.add_field(
            name  = "Discord Stats",
            value = (
                f"**Servers** `{guilds:,}`\n"
                f"**Users** `{users:,}`"
            ),
            inline=True,
        )
        e.add_field(
            name  = "System Stats",
            value = (
                f"**Python** `{py_ver}`\n"
                f"**discord.py** `{dpy_ver}`"
            ),
            inline=True,
        )
        e.add_field(
            name  = "Bot Uptime",
            value = (
                f"**Online** `{uptime_str}`\n"
                f"**Last Restarted** <t:{restart_ts}:R>"
            ),
            inline=False,
        )

        await interaction.response.send_message(embed=e)

    @app_commands.command(name="avatar", description="Display a user's avatar.")
    @app_commands.describe(user="Member to look up (defaults to you).")
    @app_commands.checks.cooldown(1, 5)
    async def avatar(self, interaction: discord.Interaction, user: Optional[discord.Member] = None):
        target = user or interaction.user  # type: ignore[assignment]
        await interaction.response.send_message(
            embed=self.ef.avatar(target, "Avatar"),  # type: ignore[arg-type]
            view=AvatarView(target, self.ef),         # type: ignore[arg-type]
        )

    @app_commands.command(name="banner", description="Display a user's profile banner.")
    @app_commands.describe(user="User to look up (defaults to you).")
    @app_commands.checks.cooldown(1, 5)
    async def banner(self, interaction: discord.Interaction, user: Optional[discord.User] = None):
        target  = user or interaction.user
        fetched = await self.bot.fetch_user(target.id)
        await interaction.response.send_message(embed=self.ef.banner(fetched))

    @app_commands.command(name="userinfo", description="Show a detailed profile for a server member.")
    @app_commands.describe(user="Member to look up (defaults to you).")
    @app_commands.checks.cooldown(1, 5)
    async def userinfo(self, interaction: discord.Interaction, user: Optional[discord.Member] = None):
        target = user or interaction.user  # type: ignore[assignment]
        await interaction.response.send_message(embed=self.ef.user_info(target))  # type: ignore[arg-type]

    @app_commands.command(name="whois", description="Full profile with banner, badges, and detailed info.")
    @app_commands.describe(user="Member to look up (defaults to you).")
    @app_commands.checks.cooldown(1, 5)
    async def whois(self, interaction: discord.Interaction, user: Optional[discord.Member] = None):
        target = user or interaction.user  # type: ignore[assignment]
        await interaction.response.defer()
        try:
            fetched = await self.bot.fetch_user(target.id)
        except discord.HTTPException:
            fetched = None
        embed = self.ef.whois(target, fetched)  # type: ignore[arg-type]
        view = WhoisView(target, fetched)  # type: ignore[arg-type]
        await interaction.followup.send(embed=embed, view=view)

    @app_commands.command(name="serverinfo", description="Display detailed server statistics.")
    @app_commands.checks.cooldown(1, 10)
    async def serverinfo(self, interaction: discord.Interaction):
        if not interaction.guild:
            return await interaction.response.send_message(
                embed=self.ef.error("This command can only be used inside a server."), ephemeral=False
            )
        await interaction.response.send_message(embed=self.ef.server_info(interaction.guild))

    @app_commands.command(name="roleinfo", description="Show details about a role.")
    @app_commands.describe(role="The role to inspect.")
    @app_commands.checks.cooldown(1, 5)
    async def roleinfo(self, interaction: discord.Interaction, role: discord.Role):
        await interaction.response.send_message(embed=self.ef.role_info(role))

    @app_commands.command(name="variables", description="Show all available template variables.")
    async def variables(self, interaction: discord.Interaction):
        from utils.helpers import VARIABLE_REFERENCE
        e = discord.Embed(
            title       = "Template Variables",
            description = "Use these in greet, leave, boost messages and embed templates.\n\n" + VARIABLE_REFERENCE,
            color       = self.ef.colors["accent"],
        )
        if self.bot.user:
            e.set_footer(
                text     = "Example: Welcome {user.mention} — you are member #{server.count}!",
                icon_url = self.bot.user.display_avatar.url,
            )
        await interaction.response.send_message(embed=e, ephemeral=False)

    @app_commands.command(name="help", description="Browse all available commands.")
    async def help(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            embed=self.ef.help_overview(),
            view=HelpView(self.ef),
            ephemeral=False,
        )

    @app_commands.command(name="premium", description="View your premium status and tier information.")
    async def premium(self, interaction: discord.Interaction):
        tier = await get_premium_tier(interaction.user.id)
        tiers_config = self.config.get("premium", {}).get("tiers", {})
        arrow_emoji = self.ef.e["arrow"]

        if tier > 0:
            info = await get_premium_info(interaction.user.id)
            tier_data = tiers_config.get(str(tier), {})
            name = tier_data.get("name", f"Tier {tier}")
            emoji = tier_data.get("emoji", "")
            exp = f"<t:{info['expires_at']}:F>" if info and info.get("expires_at") else "Permanent"

            e = self.ef.build(
                author_name=f"Premium  {self.ef.e['dot']}  {interaction.user.display_name}",
                color_key="success",
            )
            e.add_field(name="Your Tier", value=f"{emoji} **{name}**", inline=True)
            e.add_field(name="Expires",   value=exp,                    inline=True)
            e.add_field(name="\u200b", value="\u200b", inline=True)

            def _lbl(v: int) -> str:
                return "unlimited" if v == -1 else str(v)

            se  = tier_data.get("server_embeds", 1)
            ie  = tier_data.get("inventory_embeds", 0)
            ars = tier_data.get("autoresponders", 0)
            ari = tier_data.get("ar_inventory", 0)
            btns = tier_data.get("buttons", 0)
            e.add_field(
                name="Your Perks",
                value=(
                    f"{arrow_emoji} {_lbl(se)} server embeds  ·  {_lbl(ie)} inventory embeds\n"
                    f"{arrow_emoji} {_lbl(ars)} autoresponders  ·  {_lbl(ari)} AR inventory\n"
                    f"{arrow_emoji} {_lbl(btns)} buttons\n"
                    f"{arrow_emoji} `/customize` access\n"
                    + (f"{arrow_emoji} direct developer support\n" if tier >= 2 else "")
                    + (f"{arrow_emoji} custom Nana bot\n" if tier >= 3 else "")
                ),
                inline=False,
            )
            e.set_thumbnail(url=interaction.user.display_avatar.url)
        else:
            e = self.ef.build(
                author_name=f"Premium  {self.ef.e['dot']}  Nana",
                color_key="accent",
            )
            e.description = f"Support Nana and unlock exclusive features!\n\n"

            for tid, td in tiers_config.items():
                te = td.get("emoji", "")
                tn = td.get("name", "")
                tp = td.get("price", 0)
                se = td.get("server_embeds", 1)
                ie = td.get("inventory_embeds", 0)
                perks = f"{'unlimited' if se == -1 else se} server embeds, {'unlimited' if ie == -1 else ie} inventory embeds"
                if int(tid) == 1:
                    perks += ", `/customize` access"
                elif int(tid) == 2:
                    perks += ", dev support, giveaways"
                elif int(tid) == 3:
                    perks += ", custom bot, everything in Tulip"
                e.add_field(
                    name=f"{te} {tn}  —  ${tp}/mo",
                    value=f"{arrow_emoji} {perks}",
                    inline=False,
                )

            premium_cfg = self.config.get("premium", {})
            support  = premium_cfg.get("support_server", "")
            patreon  = premium_cfg.get("patreon_url", "")

            view = discord.ui.View()
            if patreon:
                view.add_item(discord.ui.Button(label="Get Premium on Patreon",
                                                 style=discord.ButtonStyle.link, url=patreon))
            if support:
                view.add_item(discord.ui.Button(label="Support Server",
                                                 style=discord.ButtonStyle.link, url=support))

            import os
            tiers_path = os.path.join(os.path.dirname(__file__), "..", "assets", "nana_tiers.png")
            if os.path.isfile(tiers_path):
                e.set_image(url="attachment://nana_tiers.png")
                file = discord.File(tiers_path, filename="nana_tiers.png")
                return await interaction.response.send_message(
                    embed=e, file=file,
                    view=view if len(view.children) else discord.utils.MISSING,
                    ephemeral=False,
                )
            return await interaction.response.send_message(
                embed=e,
                view=view if len(view.children) else discord.utils.MISSING,
                ephemeral=False,
            )

        await interaction.response.send_message(embed=e, ephemeral=False)

    customize_group = app_commands.Group(
        name="customize",
        description="Customize the bot's server profile.",
        default_permissions=discord.Permissions(manage_guild=True),
    )

    async def _check_premium(self, interaction: discord.Interaction) -> bool:
        tier = await get_premium_tier(interaction.user.id)
        if tier < 1:
            await interaction.response.send_message(
                embed=self.ef.error("The `/customize` command requires premium. Use `/premium` to learn more."),
                ephemeral=False,
            )
            return False
        return True

    @customize_group.command(name="avatar", description="Set the bot's server avatar.")
    @app_commands.describe(image="Upload an image for the bot's server avatar.")
    async def customize_avatar(self, interaction: discord.Interaction, image: discord.Attachment):
        assert interaction.guild
        if not await self._check_premium(interaction):
            return
        if not image.content_type or not image.content_type.startswith("image/"):
            return await interaction.response.send_message(
                embed=self.ef.error("Please upload a valid image file."), ephemeral=False
            )
        await interaction.response.defer(ephemeral=False)
        try:
            image_bytes = await image.read()
            b64 = base64.b64encode(image_bytes).decode("utf-8")
            data_uri = f"data:{image.content_type};base64,{b64}"
            route = discord.http.Route(
                "PATCH", "/guilds/{guild_id}/members/@me",
                guild_id=interaction.guild.id,
            )
            await self.bot.http.request(route, json={"avatar": data_uri})
            await interaction.followup.send(embed=self.ef.success("Server avatar updated!"))
        except discord.HTTPException as exc:
            await interaction.followup.send(embed=self.ef.error(f"Failed to update avatar: {exc}"))

    @customize_group.command(name="banner", description="Set the bot's server banner.")
    @app_commands.describe(image="Upload an image for the bot's server banner.")
    async def customize_banner(self, interaction: discord.Interaction, image: discord.Attachment):
        assert interaction.guild
        if not await self._check_premium(interaction):
            return
        if not image.content_type or not image.content_type.startswith("image/"):
            return await interaction.response.send_message(
                embed=self.ef.error("Please upload a valid image file."), ephemeral=False
            )
        await interaction.response.defer(ephemeral=False)
        try:
            image_bytes = await image.read()
            b64 = base64.b64encode(image_bytes).decode("utf-8")
            data_uri = f"data:{image.content_type};base64,{b64}"
            route = discord.http.Route(
                "PATCH", "/guilds/{guild_id}/members/@me",
                guild_id=interaction.guild.id,
            )
            await self.bot.http.request(route, json={"banner": data_uri})
            await interaction.followup.send(embed=self.ef.success("Server banner updated!"))
        except discord.HTTPException as exc:
            await interaction.followup.send(embed=self.ef.error(f"Failed to update banner: {exc}"))

    @customize_group.command(name="bio", description="Add text above the bot's current server bio.")
    @app_commands.describe(text="Text to place above the existing bio (max 100 chars).", reset="Set True to replace the bio entirely instead of prepending.")
    async def customize_bio(self, interaction: discord.Interaction,
                            text: app_commands.Range[str, 1, 100],
                            reset: bool = False):
        assert interaction.guild
        if not await self._check_premium(interaction):
            return
        await interaction.response.defer(ephemeral=False)
        from utils.database import get_guild_config, upsert_guild_config
        config      = await get_guild_config(interaction.guild.id)
        stored_bio  = config.get("bot_bio") or ""
        if reset or not stored_bio:
            new_bio = text
        else:
            new_bio = f"{text}\n{stored_bio}"
        if len(new_bio) > 190:
            new_bio = new_bio[:190]
        try:
            route = discord.http.Route(
                "PATCH", "/guilds/{guild_id}/members/@me",
                guild_id=interaction.guild.id,
            )
            await self.bot.http.request(route, json={"bio": new_bio})
            await upsert_guild_config(interaction.guild.id, bot_bio=new_bio)
            action = "reset to" if reset or not stored_bio else "updated — added above existing bio:"
            await interaction.followup.send(embed=self.ef.success(f"Server bio {action}\n> {new_bio}"))
        except discord.HTTPException as exc:
            await interaction.followup.send(embed=self.ef.error(f"Failed to update bio: {exc}"))

    # ── /customize palette ───────────────────────────────────────────────────

    palette_group = app_commands.Group(
        parent=customize_group, name="palette",
        description="Customize the embed color palette for this server.",
    )

    @palette_group.command(name="set", description="Open a modal to set custom embed colors.")
    async def palette_set(self, interaction: discord.Interaction):
        assert interaction.guild
        if not await self._check_premium(interaction):
            return
        current = await get_guild_palette(interaction.guild.id) or {}
        defaults = self.config.get("colors", {})
        modal = PaletteModal(self.ef, defaults=defaults, current=current)
        await interaction.response.send_modal(modal)

    @palette_group.command(name="view", description="Preview your saved color palette.")
    async def palette_view(self, interaction: discord.Interaction):
        assert interaction.guild
        palette = await get_guild_palette(interaction.guild.id)
        if not palette:
            return await interaction.response.send_message(
                embed=self.ef.info("No custom palette set. Use `/customize palette set` to create one."),
                ephemeral=False,
            )
        e = self.ef.build(title="Your Custom Palette", color_key="accent")
        for k in ("primary", "secondary", "accent", "success", "error", "warning"):
            v = palette.get(k, "—")
            e.add_field(name=k.capitalize(), value=f"`{v}`", inline=True)
        await interaction.response.send_message(embed=e, ephemeral=False)

    @palette_group.command(name="reset", description="Restore the default embed color palette.")
    async def palette_reset(self, interaction: discord.Interaction):
        assert interaction.guild
        if not await self._check_premium(interaction):
            return
        await reset_guild_palette(interaction.guild.id)
        await interaction.response.send_message(
            embed=self.ef.success("Palette reset to defaults."), ephemeral=False
        )

    # ── /customize emojis ────────────────────────────────────────────────────

    emojis_group = app_commands.Group(
        parent=customize_group, name="emojis",
        description="Customize the status emojis used in embed responses.",
    )

    @emojis_group.command(name="set", description="Open a modal to set custom status emojis.")
    async def emojis_set(self, interaction: discord.Interaction):
        assert interaction.guild
        if not await self._check_premium(interaction):
            return
        current = await get_guild_emojis(interaction.guild.id) or {}
        defaults = self.config.get("emojis", {})
        modal = EmojisModal(self.ef, defaults=defaults, current=current)
        await interaction.response.send_modal(modal)

    @emojis_group.command(name="view", description="Preview your saved emoji set.")
    async def emojis_view(self, interaction: discord.Interaction):
        assert interaction.guild
        emojis = await get_guild_emojis(interaction.guild.id)
        if not emojis:
            return await interaction.response.send_message(
                embed=self.ef.info("No custom emojis set. Use `/customize emojis set` to create them."),
                ephemeral=False,
            )
        e = self.ef.build(title="Your Custom Emojis", color_key="accent")
        for k in ("success", "error", "warning", "ping", "dot", "arrow"):
            v = emojis.get(k, "—")
            e.add_field(name=k.capitalize(), value=v, inline=True)
        await interaction.response.send_message(embed=e, ephemeral=False)

    @emojis_group.command(name="reset", description="Restore the default emoji set.")
    async def emojis_reset(self, interaction: discord.Interaction):
        assert interaction.guild
        if not await self._check_premium(interaction):
            return
        await reset_guild_emojis(interaction.guild.id)
        await interaction.response.send_message(
            embed=self.ef.success("Emojis reset to defaults."), ephemeral=False
        )

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.CommandOnCooldown):
            embed = self.ef.warning(f"Slow down — try again in **{error.retry_after:.1f}s**.")
        else:
            embed = self.ef.error(str(error))
        if not interaction.response.is_done():
            await interaction.response.send_message(embed=embed, ephemeral=False)
        else:
            await interaction.followup.send(embed=embed, ephemeral=False)


async def setup(bot: commands.Bot):
    await bot.add_cog(Misc(bot))
