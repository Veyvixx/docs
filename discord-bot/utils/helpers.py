"""
Shared utility helpers — duration parsing, role hierarchy checks, DM sending,
and a comprehensive template variable formatter with 70+ variables.
"""

import discord
import re
import random
from datetime import timedelta, datetime, timezone
from typing import Optional


def parse_duration(raw: str) -> Optional[timedelta]:
    pattern = r"(\d+)\s*([smhdw])"
    matches = re.findall(pattern, raw.lower())
    if not matches:
        return None
    unit_map = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
    total = sum(int(amt) * unit_map[u] for amt, u in matches)
    return timedelta(seconds=total)


def format_duration(td: timedelta) -> str:
    total = int(td.total_seconds())
    parts = []
    for label, secs in [("d", 86400), ("h", 3600), ("m", 60), ("s", 1)]:
        count, total = divmod(total, secs)
        if count:
            parts.append(f"{count}{label}")
    return " ".join(parts) or "0s"


def can_act_on(actor: discord.Member, target: discord.Member) -> bool:
    if target == target.guild.owner:
        return False
    return actor.top_role > target.top_role


async def send_dm(user: discord.User | discord.Member, embed: discord.Embed) -> bool:
    try:
        await user.send(embed=embed)
        return True
    except (discord.Forbidden, discord.HTTPException):
        return False


def chunk_list(lst: list, size: int) -> list[list]:
    return [lst[i : i + size] for i in range(0, len(lst), size)]


def ordinal(n: int) -> str:
    suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    if 11 <= n % 100 <= 13:
        suffix = "th"
    return f"{n}{suffix}"


def _get_badge_names(member: discord.Member | discord.User) -> list[str]:
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


_BOOST_TIERS = {0: "None", 1: "Tier 1", 2: "Tier 2", 3: "Tier 3"}
_BOOST_GOALS = {0: 2, 1: 7, 2: 14, 3: 14}


def format_variables(
    template: str,
    member: Optional[discord.Member | discord.User] = None,
    guild: Optional[discord.Guild] = None,
    channel: Optional[discord.abc.GuildChannel] = None,
) -> str:
    now = datetime.now(timezone.utc)
    guild = guild or (member.guild if isinstance(member, discord.Member) else None)

    replacements: dict[str, str] = {}

    if member is not None:
        avatar_url = member.display_avatar.url if hasattr(member, "display_avatar") else ""
        created_ts = int(member.created_at.timestamp())

        joined_str = ""
        joined_ts_str = ""
        nickname = ""
        top_role = ""
        top_role_mention = ""
        top_role_color = ""
        role_count = "0"
        roles_str = ""
        status_str = "offline"
        color_str = "#000000"
        join_pos = "0"
        banner_url = ""
        boost_since = "N/A"
        boosting = "False"
        activity_name = "None"
        custom_status = "None"
        is_owner = "False"
        is_admin = "False"
        is_mod = "False"
        is_pending = "False"
        timeout_until = "None"

        if isinstance(member, discord.Member):
            if member.joined_at:
                joined_str = f"<t:{int(member.joined_at.timestamp())}:R>"
                joined_ts_str = f"<t:{int(member.joined_at.timestamp())}:F>"
            nickname = member.nick or member.display_name
            top_role = member.top_role.name if member.top_role.name != "@everyone" else "None"
            top_role_mention = member.top_role.mention if member.top_role.name != "@everyone" else "`None`"
            top_role_color = str(member.top_role.color)
            real_roles = [r for r in member.roles if r.name != "@everyone"]
            role_count = str(len(real_roles))
            roles_str = ", ".join(r.name for r in reversed(real_roles)) if real_roles else "None"
            status_str = str(member.status) if hasattr(member, "status") else "offline"
            color_str = str(member.color)
            if member.premium_since:
                boost_since = f"<t:{int(member.premium_since.timestamp())}:R>"
                boosting = "True"
            if member.activities:
                for act in member.activities:
                    if isinstance(act, discord.CustomActivity):
                        custom_status = act.name or "None"
                    elif act.name:
                        activity_name = act.name
            is_owner = str(member.id == member.guild.owner_id)
            is_admin = str(member.guild_permissions.administrator)
            is_mod = str(member.guild_permissions.moderate_members)
            is_pending = str(member.pending) if hasattr(member, "pending") else "False"
            if member.timed_out_until:
                timeout_until = f"<t:{int(member.timed_out_until.timestamp())}:R>"
            if guild and guild.members:
                sorted_members = sorted(
                    [m for m in guild.members if m.joined_at],
                    key=lambda m: m.joined_at  # type: ignore[arg-type, return-value]
                )
                try:
                    join_pos = str(sorted_members.index(member) + 1)
                except ValueError:
                    join_pos = "?"
        else:
            nickname = member.display_name

        if hasattr(member, "banner") and member.banner:
            banner_url = member.banner.url

        badges = _get_badge_names(member)

        replacements.update({
            "user":                 member.display_name,
            "user.mention":         member.mention,
            "user.name":            member.name,
            "user.id":              str(member.id),
            "user.tag":             str(member),
            "user.avatar":          avatar_url,
            "user.created":         f"<t:{created_ts}:R>",
            "user.joined":          joined_str or "N/A",
            "user.nickname":        nickname,
            "user.discriminator":   member.discriminator or "0",
            "user.displayname":     member.display_name,
            "user.displayavatar":   avatar_url,
            "user.banner":          banner_url or "None",
            "user.toprole":         top_role,
            "user.toprole.mention": top_role_mention,
            "user.toprole.color":   top_role_color,
            "user.rolecount":       role_count,
            "user.roles":           roles_str,
            "user.status":          status_str,
            "user.bot":             str(member.bot),
            "user.color":           color_str,
            "user.badges":          ", ".join(badges) if badges else "None",
            "user.createdago":      f"<t:{created_ts}:R>",
            "user.createdfull":     f"<t:{created_ts}:F>",
            "user.joinedfull":      joined_ts_str or "N/A",
            "user.joinedago":       joined_str or "N/A",
            "user.joinposition":    join_pos,
            "user.boostsince":      boost_since,
            "user.boosting":        boosting,
            "user.activity":        activity_name,
            "user.customstatus":    custom_status,
            "user.isowner":         is_owner,
            "user.isadmin":         is_admin,
            "user.ismod":           is_mod,
            "user.pending":         is_pending,
            "user.timeout":         timeout_until,
        })

    if guild is not None:
        icon_url = guild.icon.url if guild.icon else ""
        guild_banner_url = guild.banner.url if guild.banner else ""
        splash_url = guild.splash.url if guild.splash else ""
        count = guild.member_count or 0
        created_ts = int(guild.created_at.timestamp())

        humans = sum(1 for m in guild.members if not m.bot) if guild.members else 0
        bots = sum(1 for m in guild.members if m.bot) if guild.members else 0

        online = idle = dnd = offline = 0
        if guild.members:
            for m in guild.members:
                s = str(m.status) if hasattr(m, "status") else "offline"
                if s == "online":    online += 1
                elif s == "idle":    idle += 1
                elif s == "dnd":     dnd += 1
                else:                offline += 1

        boost_count = guild.premium_subscription_count or 0
        boost_tier = guild.premium_tier
        boost_goal = _BOOST_GOALS.get(boost_tier, 14)
        boost_progress = f"{boost_count}/{boost_goal}"
        filled = min(10, int((boost_count / max(boost_goal, 1)) * 10))
        boost_bar = "\u2588" * filled + "\u2591" * (10 - filled)

        afk_ch = guild.afk_channel.name if guild.afk_channel else "None"
        afk_timeout = str(guild.afk_timeout // 60) if guild.afk_timeout else "0"
        rules_ch = guild.rules_channel.mention if guild.rules_channel else "None"
        system_ch = guild.system_channel.mention if guild.system_channel else "None"

        replacements.update({
            "server":               guild.name,
            "server.name":          guild.name,
            "server.id":            str(guild.id),
            "server.count":         str(count),
            "server.icon":          icon_url,
            "server.owner":         f"<@{guild.owner_id}>",
            "server.level":         str(boost_tier),
            "server.boosts":        str(boost_count),
            "server.boostcount":    str(boost_count),
            "server.boosttier":     _BOOST_TIERS.get(boost_tier, "None"),
            "server.boostgoal":     str(boost_goal),
            "server.boostprogress": boost_progress,
            "server.boostbar":      boost_bar,
            "server.banner":        guild_banner_url or "None",
            "server.splash":        splash_url or "None",
            "server.description":   guild.description or "None",
            "server.channels":      str(len(guild.channels)),
            "server.textchannels":  str(len(guild.text_channels)),
            "server.voicechannels": str(len(guild.voice_channels)),
            "server.categories":    str(len(guild.categories)),
            "server.roles":         str(len(guild.roles)),
            "server.emojis":        str(len(guild.emojis)),
            "server.stickers":      str(len(guild.stickers)),
            "server.created":       f"<t:{created_ts}:R>",
            "server.createdfull":   f"<t:{created_ts}:F>",
            "server.vanity":        guild.vanity_url_code or "None",
            "server.humans":        str(humans),
            "server.bots":          str(bots),
            "server.online":        str(online),
            "server.idle":          str(idle),
            "server.dnd":           str(dnd),
            "server.offline":       str(offline),
            "server.verification":  str(guild.verification_level).replace("_", " ").title(),
            "server.tier":          str(boost_tier),
            "server.afkchannel":    afk_ch,
            "server.afktimeout":    f"{afk_timeout}m",
            "server.ruleschannel":  rules_ch,
            "server.systemchannel": system_ch,
            "server.nsfw":          str(guild.nsfw_level).replace("_", " ").title(),
            "server.locale":        str(guild.preferred_locale),
            "ordinal":              ordinal(count),
        })

    if channel is not None:
        ch_created = int(channel.created_at.timestamp()) if channel.created_at else 0
        topic = ""
        if isinstance(channel, discord.TextChannel) and channel.topic:
            topic = channel.topic
        replacements.update({
            "channel":         channel.name,
            "channel.mention": channel.mention if hasattr(channel, "mention") else f"#{channel.name}",
            "channel.name":    channel.name,
            "channel.id":      str(channel.id),
            "channel.topic":   topic,
            "channel.created": f"<t:{ch_created}:R>" if ch_created else "N/A",
        })

    unix_ts = int(now.timestamp())
    replacements.update({
        "date":        now.strftime("%Y-%m-%d"),
        "time":        now.strftime("%H:%M UTC"),
        "timestamp":   str(unix_ts),
        "unix":        str(unix_ts),
        "date.day":    str(now.day),
        "date.month":  str(now.month),
        "date.year":   str(now.year),
        "time.hour":   str(now.hour),
        "time.minute": str(now.minute),
        "newline":     "\n",
        "nl":          "\n",
        "tab":         "\t",
        "space":       " ",
        "empty":       "\u200b",
    })

    def handle_special(match: re.Match) -> Optional[str]:
        inner = match.group(1).strip()
        low = inner.lower()
        if low.startswith("random:"):
            parts = low[7:].split("-", 1)
            try:
                lo, hi = int(parts[0]), int(parts[1])
                return str(random.randint(lo, hi))
            except (ValueError, IndexError):
                return None
        if low == "randomcolor":
            return f"#{random.randint(0, 0xFFFFFF):06x}"
        if low.startswith("choice:"):
            options = inner[7:].split("|")
            return random.choice(options).strip() if options else None
        if low.startswith("#") and guild:
            try:
                ch_id = int(low[1:])
                return f"<#{ch_id}>"
            except ValueError:
                pass
        if low.startswith("&") and guild:
            try:
                r_id = int(low[1:])
                return f"<@&{r_id}>"
            except ValueError:
                pass
        if low.startswith("random.") and guild:
            what = low[7:]
            if what == "user" and guild.members:
                return random.choice(guild.members).mention
            if what == "channel" and guild.text_channels:
                return random.choice(guild.text_channels).mention
            if what == "role" and guild.roles:
                r = random.choice([r for r in guild.roles if r.name != "@everyone"] or guild.roles)
                return r.mention
        return None

    def replacer(match: re.Match) -> str:
        spec = handle_special(match)
        if spec is not None:
            return spec
        key = match.group(1).strip().lower()
        return replacements.get(key, match.group(0))

    return re.sub(r"\{([^}]+)\}", replacer, template)


def extract_embed_ref(template: str) -> tuple[str, Optional[str]]:
    match = re.search(r"\{embed:([^}]+)\}", template, re.IGNORECASE)
    if match:
        name = match.group(1).strip().lower()
        clean = template[:match.start()] + template[match.end():]
        return clean.strip(), name
    return template, None


def extract_actions(template: str) -> tuple[str, list[dict]]:
    actions: list[dict] = []
    clean = template

    for match in re.finditer(r"\{addrole:([^}]+)\}", template, re.IGNORECASE):
        role_ref = match.group(1).strip()
        actions.append({"type": "addrole", "role": role_ref})
    clean = re.sub(r"\{addrole:[^}]+\}", "", clean, flags=re.IGNORECASE)

    for match in re.finditer(r"\{removerole:([^}]+)\}", template, re.IGNORECASE):
        role_ref = match.group(1).strip()
        actions.append({"type": "removerole", "role": role_ref})
    clean = re.sub(r"\{removerole:[^}]+\}", "", clean, flags=re.IGNORECASE)

    if re.search(r"\{dm\}", template, re.IGNORECASE):
        actions.append({"type": "dm"})
    clean = re.sub(r"\{dm\}", "", clean, flags=re.IGNORECASE)

    for match in re.finditer(r"\{delete:(\d+)\}", template, re.IGNORECASE):
        actions.append({"type": "delete", "seconds": int(match.group(1))})
    clean = re.sub(r"\{delete:\d+\}", "", clean, flags=re.IGNORECASE)

    return clean.strip(), actions


async def process_actions(
    actions: list[dict],
    member: discord.Member,
    guild: discord.Guild,
) -> None:
    for action in actions:
        try:
            if action["type"] == "addrole":
                role = _resolve_role(guild, action["role"])
                if role:
                    await member.add_roles(role, reason="Template action {addrole}")
            elif action["type"] == "removerole":
                role = _resolve_role(guild, action["role"])
                if role:
                    await member.remove_roles(role, reason="Template action {removerole}")
        except (discord.Forbidden, discord.HTTPException):
            pass


def _resolve_role(guild: discord.Guild, ref: str) -> Optional[discord.Role]:
    ref = ref.strip().lstrip("<@&").rstrip(">")
    try:
        role_id = int(ref)
        return guild.get_role(role_id)
    except ValueError:
        pass
    for role in guild.roles:
        if role.name.lower() == ref.lower():
            return role
    return None


def extract_all_flags(template: str) -> tuple[str, dict]:
    """
    Extract every action/flag variable from a response template for
    autoresponders and functional buttons.  Returns (cleaned_text, flags).

    flags keys:
        dm            bool         – send response as DM instead of channel
        delete        bool         – delete the trigger message
        reply         bool         – reply to the trigger message
        pin           bool         – pin the bot's response
        addrole       list[str]    – role refs to add to the member
        removerole    list[str]    – role refs to remove from the member
        react         list[str]    – emoji to react on the trigger message
        require       list[str]    – role refs required (guard, not executed)
        cooldown      int          – per-user cooldown in seconds (guard)
        chance        int          – 0-100 percent chance to fire (guard)
        channel       str|None     – target channel id/mention override
        embed         str|None     – named embed to attach
        buttons       list[str]    – named buttons to attach
    """
    clean = template
    flags: dict = {
        "dm": False, "delete": False, "reply": False, "pin": False,
        "addrole": [], "removerole": [], "react": [],
        "require": [], "cooldown": 0, "chance": 100,
        "channel": None, "embed": None, "buttons": [],
    }

    if re.search(r"\{dm\}", clean, re.IGNORECASE):
        flags["dm"] = True
        clean = re.sub(r"\{dm\}", "", clean, flags=re.IGNORECASE)

    if re.search(r"\{delete\}", clean, re.IGNORECASE):
        flags["delete"] = True
        clean = re.sub(r"\{delete\}", "", clean, flags=re.IGNORECASE)

    if re.search(r"\{reply\}", clean, re.IGNORECASE):
        flags["reply"] = True
        clean = re.sub(r"\{reply\}", "", clean, flags=re.IGNORECASE)

    if re.search(r"\{pin\}", clean, re.IGNORECASE):
        flags["pin"] = True
        clean = re.sub(r"\{pin\}", "", clean, flags=re.IGNORECASE)

    for m in re.finditer(r"\{addrole:([^}]+)\}", clean, re.IGNORECASE):
        flags["addrole"].append(m.group(1).strip())
    clean = re.sub(r"\{addrole:[^}]+\}", "", clean, flags=re.IGNORECASE)

    for m in re.finditer(r"\{removerole:([^}]+)\}", clean, re.IGNORECASE):
        flags["removerole"].append(m.group(1).strip())
    clean = re.sub(r"\{removerole:[^}]+\}", "", clean, flags=re.IGNORECASE)

    for m in re.finditer(r"\{react:([^}]+)\}", clean, re.IGNORECASE):
        flags["react"].append(m.group(1).strip())
    clean = re.sub(r"\{react:[^}]+\}", "", clean, flags=re.IGNORECASE)

    for m in re.finditer(r"\{require:([^}]+)\}", clean, re.IGNORECASE):
        flags["require"].append(m.group(1).strip())
    clean = re.sub(r"\{require:[^}]+\}", "", clean, flags=re.IGNORECASE)

    m = re.search(r"\{cooldown:(\d+)\}", clean, re.IGNORECASE)
    if m:
        flags["cooldown"] = int(m.group(1))
        clean = re.sub(r"\{cooldown:\d+\}", "", clean, flags=re.IGNORECASE)

    m = re.search(r"\{chance:(\d+)\}", clean, re.IGNORECASE)
    if m:
        flags["chance"] = max(0, min(100, int(m.group(1))))
        clean = re.sub(r"\{chance:\d+\}", "", clean, flags=re.IGNORECASE)

    m = re.search(r"\{channel:([^}]+)\}", clean, re.IGNORECASE)
    if m:
        flags["channel"] = m.group(1).strip()
        clean = re.sub(r"\{channel:[^}]+\}", "", clean, flags=re.IGNORECASE)

    m = re.search(r"\{embed:([^}]+)\}", clean, re.IGNORECASE)
    if m:
        flags["embed"] = m.group(1).strip().lower()
        clean = re.sub(r"\{embed:[^}]+\}", "", clean, flags=re.IGNORECASE)

    for m in re.finditer(r"\{button:([^}]+)\}", clean, re.IGNORECASE):
        flags["buttons"].append(m.group(1).strip().lower())
    clean = re.sub(r"\{button:[^}]+\}", "", clean, flags=re.IGNORECASE)

    return clean.strip(), flags


VARIABLE_REFERENCE = (
    "**User Variables**\n"
    "`{user}` · `{user.mention}` · `{user.name}` · `{user.id}`\n"
    "`{user.tag}` · `{user.created}` · `{user.joined}`\n"
    "`{user.nickname}` · `{user.displayname}`\n"
    "`{user.toprole}` · `{user.toprole.mention}` · `{user.toprole.color}`\n"
    "`{user.rolecount}` · `{user.roles}` · `{user.status}` · `{user.color}`\n"
    "`{user.badges}` · `{user.createdago}` · `{user.createdfull}`\n"
    "`{user.joinedago}` · `{user.joinedfull}` · `{user.joinposition}`\n"
    "`{user.boostsince}` · `{user.boosting}` · `{user.activity}`\n"
    "`{user.customstatus}` · `{user.timeout}`\n\n"
    "**Server Variables**\n"
    "`{server}` · `{server.name}` · `{server.id}` · `{server.count}`\n"
    "`{server.owner}` · `{server.level}` · `{server.boosts}`\n"
    "`{server.boosttier}` · `{server.boostgoal}`\n"
    "`{server.boostprogress}` · `{server.boostbar}`\n"
    "`{server.description}`\n"
    "`{server.channels}` · `{server.textchannels}` · `{server.voicechannels}`\n"
    "`{server.categories}` · `{server.roles}` · `{server.emojis}`\n"
    "`{server.stickers}` · `{server.created}` · `{server.createdfull}`\n"
    "`{server.vanity}` · `{server.humans}` · `{server.bots}`\n"
    "`{server.online}` · `{server.idle}` · `{server.dnd}` · `{server.offline}`\n"
    "`{server.verification}` · `{server.tier}`\n"
    "`{server.afkchannel}` · `{server.afktimeout}` · `{server.ruleschannel}`\n"
    "`{server.systemchannel}` · `{server.nsfw}` · `{server.locale}`\n\n"
    "**Channel Variables**\n"
    "`{channel}` · `{channel.mention}` · `{channel.name}`\n"
    "`{channel.id}` · `{channel.topic}` · `{channel.created}`\n\n"
    "**Date & Time**\n"
    "`{date}` · `{time}` · `{ordinal}` · `{timestamp}`\n"
    "`{date.day}` · `{date.month}` · `{date.year}`\n"
    "`{time.hour}` · `{time.minute}`\n\n"
    "**Formatting**\n"
    "`{newline}` / `{nl}` — line break\n\n"
    "**Random**\n"
    "`{random:1-100}` · `{randomcolor}` · `{choice:a|b|c}`\n"
    "`{random.user}` · `{random.channel}` · `{random.role}`\n\n"
    "**Mentions by ID**\n"
    "`{#channelid}` · `{&roleid}`\n\n"
    "**Autoresponder & Button Actions**\n"
    "`{embed:name}` — attach a saved embed\n"
    "`{button:name}` — attach a saved button\n"
    "`{addrole:@role}` · `{removerole:@role}` — add/remove role\n"
    "`{dm}` — send response as DM instead of channel\n"
    "`{delete}` — delete the trigger message\n"
    "`{reply}` — reply to the trigger message\n"
    "`{pin}` — pin the bot's response\n"
    "`{react:emoji}` — react on the trigger message\n\n"
    "**Autoresponder Guards** *(prevent firing)*\n"
    "`{require:@role}` — only fire if member has role\n"
    "`{cooldown:30}` — per-user cooldown in seconds\n"
    "`{chance:50}` — 50% random chance to respond\n"
    "`{channel:#id}` — send to a different channel\n\n"
    "**Greet / Leave / Boost Templates**\n"
    "`{addrole:@role}` · `{removerole:@role}` · `{dm}` · `{delete:5}`\n"
    "`{embed:name}` — attach a saved embed"
)
