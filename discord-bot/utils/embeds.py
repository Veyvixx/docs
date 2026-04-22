"""
Premium embed factory — dolls.gg-inspired dark modern aesthetic.
Icons come from config["emojis"]: success, error, warning, ping, dot, arrow.
"""

import discord
from datetime import datetime, timezone
from typing import Optional


def _ts() -> datetime:
    return datetime.now(timezone.utc)


def _get_badges(member: discord.Member | discord.User) -> list[str]:
    f = member.public_flags
    b = []
    if f.staff:                  b.append("Staff")
    if f.partner:                b.append("Partner")
    if f.bug_hunter:             b.append("Bug Hunter")
    if f.bug_hunter_level_2:     b.append("Bug Hunter L2")
    if f.hypesquad_balance:      b.append("Balance")
    if f.hypesquad_bravery:      b.append("Bravery")
    if f.hypesquad_brilliance:   b.append("Brilliance")
    if f.early_supporter:        b.append("Early Supporter")
    if f.verified_bot_developer: b.append("Bot Dev")
    if f.active_developer:       b.append("Active Dev")
    return b


class EmbedFactory:
    def __init__(self, config: dict, bot: Optional["discord.Client"] = None) -> None:
        self.colors = config["colors"]
        self.e      = config["emojis"]
        self.bot    = bot

    def _apply_footer(self, embed: discord.Embed) -> discord.Embed:
        if self.bot and self.bot.user:
            embed.set_footer(
                text     = self.bot.user.name,
                icon_url = self.bot.user.display_avatar.url,
            )
        return embed

    def build(
        self,
        *,
        title:       str  = "",
        description: str  = "",
        color_key:   str  = "primary",
        color:       Optional[int] = None,
        timestamp:   bool = True,
        url:         str  = "",
        author_name: str  = "",
        author_icon: str  = "",
        thumbnail:   str  = "",
    ) -> discord.Embed:
        embed = discord.Embed(
            title       = title       or None,
            description = description or None,
            color       = color if color is not None else self.colors.get(color_key, self.colors["primary"]),
            url         = url         or None,
        )
        if timestamp:
            embed.timestamp = _ts()
        if author_name:
            embed.set_author(name=author_name, icon_url=author_icon or None)
        if thumbnail:
            embed.set_thumbnail(url=thumbnail)
        return self._apply_footer(embed)

    def base(self, title: str = "", description: str = "", color_key: str = "primary", timestamp: bool = True, url: str = "") -> discord.Embed:
        return self.build(title=title, description=description, color_key=color_key, timestamp=timestamp, url=url)

    def success(self, description: str, title: str = "") -> discord.Embed:
        return self.build(
            title       = title or f"{self.e['success']}  Done",
            description = description,
            color_key   = "success",
        )

    def error(self, description: str, title: str = "") -> discord.Embed:
        return self.build(
            title       = title or f"{self.e['error']}  Error",
            description = description,
            color_key   = "error",
        )

    def warning(self, description: str, title: str = "") -> discord.Embed:
        return self.build(
            title       = title or f"{self.e['warning']}  Warning",
            description = description,
            color_key   = "warning",
        )

    def info(self, description: str, title: str = "") -> discord.Embed:
        return self.build(
            title       = title or "Info",
            description = description,
            color_key   = "secondary",
        )

    def ping(self, gw_ms: float, db_ms: float) -> discord.Embed:
        quality = (
            "Excellent" if gw_ms < 80  else
            "Good"      if gw_ms < 150 else
            "Fair"      if gw_ms < 300 else
            "Poor"
        )
        e = self.build(
            title       = f"{self.e['ping']}  Latency",
            description = f"**{quality}**",
            color_key   = "accent",
        )
        e.add_field(name="Gateway",  value=f"```{gw_ms:.1f} ms```", inline=True)
        e.add_field(name="Database", value=f"```{db_ms:.1f} ms```", inline=True)
        return e

    def user_info(self, member: discord.Member) -> discord.Embed:
        e = self.build(
            author_name = str(member),
            author_icon = member.display_avatar.url,
            thumbnail   = member.display_avatar.url,
            color       = member.color.value or self.colors["primary"],
        )
        e.add_field(name="Display Name", value=member.display_name,    inline=True)
        e.add_field(name="ID",           value=f"`{member.id}`",        inline=True)
        e.add_field(name="Top Role",     value=member.top_role.mention, inline=True)

        created_ts = int(member.created_at.timestamp())
        e.add_field(name="Created", value=f"<t:{created_ts}:D>  {self.e['dot']}  <t:{created_ts}:R>", inline=True)

        if member.joined_at:
            joined_ts = int(member.joined_at.timestamp())
            e.add_field(name="Joined", value=f"<t:{joined_ts}:D>  {self.e['dot']}  <t:{joined_ts}:R>", inline=True)

        e.add_field(name="Status", value=f"`{member.status}`", inline=True)

        roles = [r.mention for r in reversed(member.roles) if r.name != "@everyone"]
        e.add_field(
            name  = f"Roles  ({len(roles)})",
            value = " ".join(roles[:15]) + ("  ..." if len(roles) > 15 else "") if roles else "`-`",
            inline=False,
        )
        badges = _get_badges(member)
        if badges:
            e.add_field(name="Badges", value="  ".join(badges), inline=False)
        return e

    def whois(self, member: discord.Member, fetched_user: Optional[discord.User] = None) -> discord.Embed:
        col = member.color.value or self.colors["primary"]
        e = discord.Embed(color=col)
        e.timestamp = _ts()

        banner_url = None
        if fetched_user and fetched_user.banner:
            banner_url = fetched_user.banner.url
            e.set_image(url=banner_url)

        e.set_thumbnail(url=member.display_avatar.url)

        badges = _get_badges(member)
        badge_str = "  ".join(f"`{b}`" for b in badges) if badges else ""
        name_line = f"**{member.display_name}**"
        if badge_str:
            name_line += f"  {badge_str}"

        desc_lines = [
            name_line,
            f"@{member.name}",
        ]
        e.description = "\n".join(desc_lines)

        created_ts = int(member.created_at.timestamp())
        e.add_field(
            name  = "Created on",
            value = f"<t:{created_ts}:F>  (<t:{created_ts}:R>)",
            inline=False,
        )

        if member.joined_at:
            joined_ts = int(member.joined_at.timestamp())
            e.add_field(
                name  = "Joined on",
                value = f"<t:{joined_ts}:F>  (<t:{joined_ts}:R>)",
                inline=False,
            )

        roles = [r for r in reversed(member.roles) if r.name != "@everyone"]
        if roles:
            role_str = "  ".join(r.mention for r in roles[:20])
            if len(roles) > 20:
                role_str += f"  +{len(roles) - 20} more"
            e.add_field(name=f"Roles  ({len(roles)})", value=role_str, inline=False)

        if member.premium_since:
            boost_ts = int(member.premium_since.timestamp())
            e.add_field(name="Boosting since", value=f"<t:{boost_ts}:R>", inline=True)

        if hasattr(member, "status"):
            e.add_field(name="Status", value=f"`{member.status}`", inline=True)

        if self.bot and self.bot.user:
            e.set_footer(
                text     = self.bot.user.name,
                icon_url = self.bot.user.display_avatar.url,
            )
        return e

    def server_info(self, guild: discord.Guild) -> discord.Embed:
        e = self.build(
            author_name = guild.name,
            author_icon = guild.icon.url if guild.icon else "",
            thumbnail   = guild.icon.url if guild.icon else "",
            color_key   = "primary",
        )
        e.add_field(name="Owner",        value=f"<@{guild.owner_id}>",                     inline=True)
        e.add_field(name="ID",           value=f"`{guild.id}`",                             inline=True)
        e.add_field(name="Created",      value=f"<t:{int(guild.created_at.timestamp())}:R>", inline=True)
        e.add_field(name="Members",      value=f"`{guild.member_count:,}`",                 inline=True)
        e.add_field(name="Channels",     value=f"`{len(guild.channels)}`",                  inline=True)
        e.add_field(name="Roles",        value=f"`{len(guild.roles)}`",                     inline=True)
        e.add_field(name="Emojis",       value=f"`{len(guild.emojis)}`",                    inline=True)
        e.add_field(
            name  = "Boost",
            value = f"`Tier {guild.premium_tier}`  {self.e['dot']}  {guild.premium_subscription_count} boosts",
            inline=True,
        )
        e.add_field(
            name  = "Verification",
            value = f"`{str(guild.verification_level).replace('_', ' ').title()}`",
            inline=True,
        )
        if guild.description:
            e.add_field(name="Description", value=guild.description, inline=False)
        if guild.banner:
            e.set_image(url=guild.banner.url)
        return e

    def avatar(self, target: discord.User | discord.Member, label: str = "Avatar") -> discord.Embed:
        e = self.build(
            title     = f"{target.display_name}  {self.e['dot']}  {label}",
            color_key = "accent",
        )
        e.set_image(url=target.display_avatar.url)
        try:
            links = f"  {self.e['dot']}  ".join(
                f"[{fmt.upper()}]({target.display_avatar.replace(format=fmt, size=4096).url})"
                for fmt in ("png", "jpg", "webp")
            )
            e.add_field(name="Download", value=links, inline=False)
        except Exception:
            pass
        return e

    def banner(self, user: discord.User) -> discord.Embed:
        e = self.build(
            title     = f"{user.display_name}  {self.e['dot']}  Banner",
            color_key = "accent",
        )
        if user.banner:
            e.set_image(url=user.banner.url)
            links = f"  {self.e['dot']}  ".join(
                f"[{fmt.upper()}]({user.banner.replace(format=fmt, size=4096).url})"
                for fmt in ("png", "jpg", "webp")
            )
            e.add_field(name="Download", value=links, inline=False)
        else:
            e.description = "This user has no banner set."
        return e

    def role_info(self, role: discord.Role) -> discord.Embed:
        col = role.color.value if role.color.value else self.colors["primary"]
        e   = self.build(author_name=f"Role  {self.e['dot']}  {role.name}", color=col)

        e.add_field(name="Name",     value=role.mention,            inline=True)
        e.add_field(name="ID",       value=f"`{role.id}`",           inline=True)
        e.add_field(name="Color",    value=f"`{role.color}`",        inline=True)
        e.add_field(name="Members",  value=f"`{len(role.members)}`", inline=True)
        e.add_field(name="Hoisted",  value=f"`{role.hoist}`",        inline=True)
        e.add_field(name="Mention",  value=f"`{role.mentionable}`",  inline=True)
        e.add_field(name="Position", value=f"`{role.position}`",     inline=True)
        e.add_field(name="Created",  value=f"<t:{int(role.created_at.timestamp())}:R>", inline=True)
        e.add_field(name="Managed",  value=f"`{role.managed}`",      inline=True)

        key_perms = [
            p.replace("_", " ").title()
            for p, v in role.permissions
            if v and p in {
                "administrator", "manage_guild", "manage_roles", "manage_channels",
                "kick_members", "ban_members", "manage_messages",
                "mention_everyone", "moderate_members",
            }
        ]
        if key_perms:
            e.add_field(
                name  = "Key Permissions",
                value = f"  {self.e['dot']}  ".join(f"`{p}`" for p in key_perms),
                inline=False,
            )
        return e

    def mod_log(
        self,
        action:    str,
        target:    discord.User | discord.Member,
        moderator: discord.Member,
        reason:    str,
        case_id:   int,
        color_key: str = "error",
        duration:  Optional[str] = None,
    ) -> discord.Embed:
        icon = {
            "Ban":     self.e["error"],
            "Unban":   self.e["success"],
            "Kick":    self.e["dot"],
            "Timeout": self.e["dot"],
            "Warn":    self.e["warning"],
            "Purge":   self.e["dot"],
        }.get(action, self.e["dot"])

        e = self.build(
            author_name = f"{icon}  {action}  {self.e['dot']}  Case #{case_id}",
            author_icon = target.display_avatar.url,
            thumbnail   = target.display_avatar.url,
            color_key   = color_key,
        )
        e.add_field(
            name  = "Target",
            value = f"{target.mention}\n`{target}`  {self.e['dot']}  `{target.id}`",
            inline=True,
        )
        e.add_field(
            name  = "Moderator",
            value = f"{moderator.mention}\n`{moderator}`",
            inline=True,
        )
        if duration:
            e.add_field(name="Duration", value=f"`{duration}`", inline=True)
        e.add_field(name="Reason", value=reason or "No reason provided.", inline=False)
        return e

    def warnings_page(
        self,
        user:     discord.User | discord.Member,
        warns:    list[dict],
        page:     int,
        per_page: int = 5,
    ) -> discord.Embed:
        total_pages = max(1, -(-len(warns) // per_page))
        chunk = warns[page * per_page : (page + 1) * per_page]
        e = self.build(
            author_name = f"Warnings  {self.e['dot']}  {user.display_name}",
            author_icon = user.display_avatar.url,
            color_key   = "warning" if warns else "success",
        )
        if not warns:
            e.description = f"{self.e['success']}  No warnings on record."
            return e
        for w in chunk:
            e.add_field(
                name  = f"Case #{w['id']}  {self.e['dot']}  <t:{w['created_at']}:R>",
                value = f"{self.e['arrow']} {w['reason']}\n- <@{w['moderator_id']}>",
                inline=False,
            )
        e.set_footer(
            text = (
                f"Page {page + 1}/{total_pages}  {self.e['dot']}  "
                f"{len(warns)} warning(s)  {self.e['dot']}  "
                + (self.bot.user.name if self.bot and self.bot.user else "")
            ),
            icon_url = self.bot.user.display_avatar.url if self.bot and self.bot.user else None,
        )
        return e

    def antinuke_alert(
        self,
        action:     str,
        offender:   discord.Member,
        count:      int,
        punishment: str,
    ) -> discord.Embed:
        e = self.build(
            author_name = f"{self.e['warning']}  Anti-Nuke Triggered",
            author_icon = self.bot.user.display_avatar.url if self.bot and self.bot.user else "",
            thumbnail   = offender.display_avatar.url,
            color_key   = "error",
        )
        e.add_field(name="Action",     value=f"`{action.replace('_', ' ').title()}`",  inline=True)
        e.add_field(name="Offender",   value=f"{offender.mention}  (`{offender.id}`)", inline=True)
        e.add_field(name="Count",      value=f"`{count}` actions in window",            inline=True)
        e.add_field(name="Punishment", value=f"`{punishment.upper()}`",                 inline=True)
        return e

    def help_overview(self) -> discord.Embed:
        d = self.e["dot"]
        e = self.build(
            title       = "Commands",
            description = (
                f"Browse commands using the dropdown below.\n\n"
                f"**Misc**  {d}  Utility & info\n"
                f"**Moderation**  {d}  Server management\n"
                f"**Anti-Nuke**  {d}  Raid protection\n"
                f"**Autoresponders**  {d}  Trigger-based auto replies\n"
                f"**Buttons**  {d}  URL & action buttons for ARs\n"
                f"**Customization**  {d}  Greet, leave, boost, embeds & logging\n"
                f"**Premium**  {d}  Activate your premium on a server"
            ),
            color_key   = "accent",
        )
        return e
