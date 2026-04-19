"""Comprehensive server event logging cog."""

import discord
from discord import app_commands
from discord.ext import commands
from typing import Optional

from utils.database import get_guild_config, upsert_guild_config

# ─────────────────────────────────────────────────────────────────────────────
# Color palette per event category
# ─────────────────────────────────────────────────────────────────────────────
C = {
    "join":    0x57F287,
    "leave":   0xED4245,
    "ban":     0xED4245,
    "unban":   0x57F287,
    "edit":    0xFEE75C,
    "delete":  0xEB459E,
    "update":  0x5865F2,
    "create":  0x57F287,
    "remove":  0xED4245,
    "voice":   0x00B0F4,
    "timeout": 0xFF8C00,
    "thread":  0x9B59B6,
    "invite":  0x1ABC9C,
    "emoji":   0xFF6B9D,
    "server":  0x5865F2,
    "cmd":     0xA8D8EA,
    "boost":   0xFF73FA,
}


class Events(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── Helpers ───────────────────────────────────────────────────────────────

    async def _ch(self, guild: discord.Guild) -> Optional[discord.TextChannel]:
        """Return the configured log channel, or None."""
        config = await get_guild_config(guild.id)
        ch_id  = config.get("log_channel_id")
        if not ch_id:
            return None
        ch = guild.get_channel(ch_id)
        return ch if isinstance(ch, discord.TextChannel) else None

    async def _send(self, guild: discord.Guild, embed: discord.Embed) -> None:
        ch = await self._ch(guild)
        if ch:
            try:
                await ch.send(embed=embed)
            except (discord.Forbidden, discord.HTTPException):
                pass

    @staticmethod
    def _e(color: int, title: str) -> discord.Embed:
        e = discord.Embed(title=title, color=color)
        e.timestamp = discord.utils.utcnow()
        return e

    # ── Message Events ────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message) -> None:
        if not message.guild or message.author.bot:
            return
        e = self._e(C["delete"], "🗑️  Message Deleted")
        e.set_author(name=str(message.author), icon_url=message.author.display_avatar.url)
        e.add_field(name="Author",  value=f"{message.author.mention} `{message.author.id}`", inline=True)
        e.add_field(name="Channel", value=message.channel.mention, inline=True)
        if message.content:
            snip = message.content[:1020]
            if len(message.content) > 1020:
                snip += "…"
            e.add_field(name="Content", value=f"```{snip}```", inline=False)
        if message.attachments:
            e.add_field(name="Attachments", value="\n".join(a.filename for a in message.attachments), inline=False)
        e.set_footer(text=f"Message ID: {message.id}")
        await self._send(message.guild, e)

    @commands.Cog.listener()
    async def on_bulk_message_delete(self, messages: list[discord.Message]) -> None:
        if not messages or not messages[0].guild:
            return
        guild   = messages[0].guild
        channel = messages[0].channel
        authors = {m.author for m in messages if hasattr(m, "author") and not m.author.bot}
        e = self._e(C["delete"], f"🗑️  Bulk Delete  ·  {len(messages)} messages")
        e.add_field(name="Channel",       value=channel.mention, inline=True)
        e.add_field(name="Total Deleted", value=str(len(messages)), inline=True)
        if authors:
            preview = list(authors)[:10]
            suffix  = f"  + {len(authors) - 10} more" if len(authors) > 10 else ""
            e.add_field(name="Authors Affected",
                        value=" ".join(a.mention for a in preview) + suffix,
                        inline=False)
        await self._send(guild, e)

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message) -> None:
        if not after.guild or after.author.bot:
            return
        if before.content == after.content:
            return
        e = self._e(C["edit"], "✏️  Message Edited")
        e.set_author(name=str(after.author), icon_url=after.author.display_avatar.url)
        e.add_field(name="Author",  value=f"{after.author.mention} `{after.author.id}`", inline=True)
        e.add_field(name="Channel", value=after.channel.mention, inline=True)
        e.add_field(name="Jump",    value=f"[View Message]({after.jump_url})", inline=True)
        def _clip(t: str, n: int = 500) -> str:
            return (t[:n] + "…") if len(t) > n else t
        e.add_field(name="Before", value=f"```{_clip(before.content) or '(empty)'}```", inline=False)
        e.add_field(name="After",  value=f"```{_clip(after.content)  or '(empty)'}```", inline=False)
        e.set_footer(text=f"Message ID: {after.id}")
        await self._send(after.guild, e)

    # ── Member Events ─────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        e = self._e(C["join"], "✅  Member Joined")
        e.set_author(name=str(member), icon_url=member.display_avatar.url)
        created_ts = int(member.created_at.timestamp())
        e.add_field(name="User",           value=f"{member.mention} `{member.id}`", inline=False)
        e.add_field(name="Account Created", value=f"<t:{created_ts}:F>  (<t:{created_ts}:R>)", inline=False)
        e.add_field(name="Member #",       value=str(member.guild.member_count), inline=True)
        e.set_thumbnail(url=member.display_avatar.url)
        e.set_footer(text=f"User ID: {member.id}")
        await self._send(member.guild, e)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        e = self._e(C["leave"], "📤  Member Left")
        e.set_author(name=str(member), icon_url=member.display_avatar.url)
        joined_str = (f"<t:{int(member.joined_at.timestamp())}:R>" if member.joined_at else "Unknown")
        roles = [r.mention for r in member.roles if r.name != "@everyone"]
        e.add_field(name="User",   value=f"{member.mention} `{member.id}`", inline=False)
        e.add_field(name="Joined", value=joined_str, inline=True)
        e.add_field(name="Roles",  value=(" ".join(roles[:15]) + ("…" if len(roles) > 15 else "")) if roles else "None", inline=False)
        e.set_thumbnail(url=member.display_avatar.url)
        e.set_footer(text=f"User ID: {member.id}")
        await self._send(member.guild, e)

    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user: discord.User) -> None:
        e = self._e(C["ban"], "🔨  Member Banned")
        e.set_author(name=str(user), icon_url=user.display_avatar.url)
        e.add_field(name="User", value=f"{user.mention} `{user.id}`", inline=False)
        e.set_thumbnail(url=user.display_avatar.url)
        e.set_footer(text=f"User ID: {user.id}")
        await self._send(guild, e)

    @commands.Cog.listener()
    async def on_member_unban(self, guild: discord.Guild, user: discord.User) -> None:
        e = self._e(C["unban"], "🔓  Member Unbanned")
        e.set_author(name=str(user), icon_url=user.display_avatar.url)
        e.add_field(name="User", value=f"{user.mention} `{user.id}`", inline=False)
        e.set_thumbnail(url=user.display_avatar.url)
        e.set_footer(text=f"User ID: {user.id}")
        await self._send(guild, e)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        fields: list[tuple[str, str, str]] = []
        added_roles   = [r for r in after.roles  if r not in before.roles  and r.name != "@everyone"]
        removed_roles = [r for r in before.roles if r not in after.roles   and r.name != "@everyone"]

        if before.nick != after.nick:
            fields.append(("Nickname", before.nick or "*None*", after.nick or "*None*"))

        if before.premium_since != after.premium_since:
            if after.premium_since:
                fields.append(("Server Boost", "—", "Started boosting 🚀"))
            else:
                fields.append(("Server Boost", "Boosting", "Stopped boosting"))

        if before.timed_out_until != after.timed_out_until:
            if after.timed_out_until:
                ts = int(after.timed_out_until.timestamp())
                fields.append(("Timeout Applied", "—", f"Expires <t:{ts}:R>"))
            else:
                fields.append(("Timeout Removed", "Active", "—"))

        if not fields and not added_roles and not removed_roles:
            return

        e = self._e(C["update"], "🔄  Member Updated")
        e.set_author(name=str(after), icon_url=after.display_avatar.url)
        e.add_field(name="User", value=f"{after.mention} `{after.id}`", inline=False)

        for label, old, new in fields:
            e.add_field(name=label, value=f"{old} → {new}", inline=False)

        if added_roles:
            e.add_field(name="✅ Roles Added",    value=" ".join(r.mention for r in added_roles),   inline=False)
        if removed_roles:
            e.add_field(name="❌ Roles Removed",  value=" ".join(r.mention for r in removed_roles), inline=False)

        e.set_thumbnail(url=after.display_avatar.url)
        e.set_footer(text=f"User ID: {after.id}")
        await self._send(after.guild, e)

    # ── Voice Events ──────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member,
                                    before: discord.VoiceState,
                                    after: discord.VoiceState) -> None:
        if member.bot:
            return

        # Join
        if before.channel is None and after.channel is not None:
            e = self._e(C["voice"], "🔊  Joined Voice")
            e.add_field(name="User",    value=f"{member.mention} `{member.id}`", inline=True)
            e.add_field(name="Channel", value=after.channel.mention, inline=True)
            e.set_footer(text=f"User ID: {member.id}")
            return await self._send(member.guild, e)

        # Leave
        if before.channel is not None and after.channel is None:
            e = self._e(C["voice"], "🔇  Left Voice")
            e.add_field(name="User",    value=f"{member.mention} `{member.id}`", inline=True)
            e.add_field(name="Channel", value=before.channel.mention, inline=True)
            e.set_footer(text=f"User ID: {member.id}")
            return await self._send(member.guild, e)

        # Move
        if before.channel and after.channel and before.channel != after.channel:
            e = self._e(C["voice"], "🔀  Moved Voice Channel")
            e.add_field(name="User", value=f"{member.mention} `{member.id}`", inline=False)
            e.add_field(name="From", value=before.channel.mention, inline=True)
            e.add_field(name="To",   value=after.channel.mention,  inline=True)
            e.set_footer(text=f"User ID: {member.id}")
            return await self._send(member.guild, e)

        # Mute / deafen state changes
        changes: list[str] = []
        if before.mute != after.mute:
            changes.append(f"Server Mute → **{'On' if after.mute else 'Off'}**")
        if before.deaf != after.deaf:
            changes.append(f"Server Deafen → **{'On' if after.deaf else 'Off'}**")
        if before.self_stream != after.self_stream:
            changes.append(f"Streaming → **{'On' if after.self_stream else 'Off'}**")
        if before.self_video != after.self_video:
            changes.append(f"Camera → **{'On' if after.self_video else 'Off'}**")

        if changes:
            e = self._e(C["voice"], "🎙️  Voice State Changed")
            e.add_field(name="User",    value=f"{member.mention} `{member.id}`", inline=True)
            e.add_field(name="Channel", value=after.channel.mention if after.channel else "—", inline=True)
            e.add_field(name="Changes", value="\n".join(changes), inline=False)
            e.set_footer(text=f"User ID: {member.id}")
            await self._send(member.guild, e)

    # ── Channel Events ────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel) -> None:
        e = self._e(C["create"], "📁  Channel Created")
        e.add_field(name="Name",     value=getattr(channel, "mention", f"`#{channel.name}`"), inline=True)
        e.add_field(name="Type",     value=str(channel.type).replace("_", " ").title(), inline=True)
        e.add_field(name="Category", value=channel.category.name if channel.category else "None", inline=True)
        e.set_footer(text=f"Channel ID: {channel.id}")
        await self._send(channel.guild, e)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        e = self._e(C["remove"], "🗑️  Channel Deleted")
        e.add_field(name="Name",     value=f"`#{channel.name}`", inline=True)
        e.add_field(name="Type",     value=str(channel.type).replace("_", " ").title(), inline=True)
        e.add_field(name="Category", value=channel.category.name if channel.category else "None", inline=True)
        e.set_footer(text=f"Channel ID: {channel.id}")
        await self._send(channel.guild, e)

    @commands.Cog.listener()
    async def on_guild_channel_update(self,
                                      before: discord.abc.GuildChannel,
                                      after: discord.abc.GuildChannel) -> None:
        fields: list[tuple[str, str, str]] = []

        if before.name != after.name:
            fields.append(("Name", f"`{before.name}`", f"`{after.name}`"))
        if getattr(before, "topic", None) != getattr(after, "topic", None):
            fields.append(("Topic",
                           getattr(before, "topic", None) or "*None*",
                           getattr(after,  "topic", None) or "*None*"))
        if getattr(before, "slowmode_delay", None) != getattr(after, "slowmode_delay", None):
            fields.append(("Slowmode",
                           f"{getattr(before, 'slowmode_delay', 0)}s",
                           f"{getattr(after,  'slowmode_delay', 0)}s"))
        if getattr(before, "nsfw", None) != getattr(after, "nsfw", None):
            fields.append(("NSFW",
                           str(getattr(before, "nsfw", False)),
                           str(getattr(after,  "nsfw", False))))
        if before.category != after.category:
            fields.append(("Category",
                           before.category.name if before.category else "None",
                           after.category.name  if after.category  else "None"))
        if getattr(before, "bitrate", None) != getattr(after, "bitrate", None):
            fields.append(("Bitrate",
                           f"{getattr(before, 'bitrate', 0)//1000}kbps",
                           f"{getattr(after,  'bitrate', 0)//1000}kbps"))
        if getattr(before, "user_limit", None) != getattr(after, "user_limit", None):
            fields.append(("User Limit",
                           str(getattr(before, "user_limit", 0) or "∞"),
                           str(getattr(after,  "user_limit", 0) or "∞")))
        if before.overwrites != after.overwrites:
            fields.append(("Permissions", "Updated", "—"))

        if not fields:
            return

        e = self._e(C["update"], "✏️  Channel Updated")
        e.add_field(name="Channel", value=getattr(after, "mention", f"`#{after.name}`"), inline=False)
        for label, old, new in fields:
            e.add_field(name=label, value=f"{old} → {new}", inline=True)
        e.set_footer(text=f"Channel ID: {after.id}")
        await self._send(after.guild, e)

    # ── Role Events ───────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_guild_role_create(self, role: discord.Role) -> None:
        e = self._e(C["create"], "🏷️  Role Created")
        e.add_field(name="Name",        value=role.mention, inline=True)
        e.add_field(name="Color",       value=str(role.color), inline=True)
        e.add_field(name="Hoisted",     value=str(role.hoist), inline=True)
        e.add_field(name="Mentionable", value=str(role.mentionable), inline=True)
        e.set_footer(text=f"Role ID: {role.id}")
        await self._send(role.guild, e)

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role) -> None:
        e = self._e(C["remove"], "🏷️  Role Deleted")
        e.add_field(name="Name",  value=f"`@{role.name}`", inline=True)
        e.add_field(name="Color", value=str(role.color), inline=True)
        e.set_footer(text=f"Role ID: {role.id}")
        await self._send(role.guild, e)

    @commands.Cog.listener()
    async def on_guild_role_update(self, before: discord.Role, after: discord.Role) -> None:
        fields: list[tuple[str, str, str]] = []

        if before.name != after.name:
            fields.append(("Name", f"`{before.name}`", f"`{after.name}`"))
        if before.color != after.color:
            fields.append(("Color", str(before.color), str(after.color)))
        if before.hoist != after.hoist:
            fields.append(("Hoisted", str(before.hoist), str(after.hoist)))
        if before.mentionable != after.mentionable:
            fields.append(("Mentionable", str(before.mentionable), str(after.mentionable)))
        if before.position != after.position:
            fields.append(("Position", str(before.position), str(after.position)))
        if before.permissions != after.permissions:
            fields.append(("Permissions", "Updated", "—"))

        if not fields:
            return

        e = self._e(C["update"], "🏷️  Role Updated")
        e.add_field(name="Role", value=after.mention, inline=False)
        for label, old, new in fields:
            e.add_field(name=label, value=f"{old} → {new}", inline=True)
        e.set_footer(text=f"Role ID: {after.id}")
        await self._send(after.guild, e)

    # ── Server / Guild Events ─────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_guild_update(self, before: discord.Guild, after: discord.Guild) -> None:
        fields: list[tuple[str, str, str]] = []

        if before.name != after.name:
            fields.append(("Name", before.name, after.name))
        if before.icon != after.icon:
            fields.append(("Icon", "Changed", "—"))
        if before.banner != after.banner:
            fields.append(("Banner", "Changed", "—"))
        if before.splash != after.splash:
            fields.append(("Invite Splash", "Changed", "—"))
        if before.description != after.description:
            fields.append(("Description",
                           before.description or "*None*",
                           after.description  or "*None*"))
        if before.verification_level != after.verification_level:
            fields.append(("Verification Level",
                           str(before.verification_level),
                           str(after.verification_level)))
        if before.explicit_content_filter != after.explicit_content_filter:
            fields.append(("Content Filter",
                           str(before.explicit_content_filter),
                           str(after.explicit_content_filter)))
        if before.afk_channel != after.afk_channel:
            fields.append(("AFK Channel",
                           before.afk_channel.name if before.afk_channel else "None",
                           after.afk_channel.name  if after.afk_channel  else "None"))
        if before.system_channel != after.system_channel:
            fields.append(("System Channel",
                           before.system_channel.mention if before.system_channel else "None",
                           after.system_channel.mention  if after.system_channel  else "None"))

        if not fields:
            return

        e = self._e(C["server"], "⚙️  Server Updated")
        for label, old, new in fields:
            e.add_field(name=label, value=f"`{old}` → `{new}`", inline=False)
        if after.icon:
            e.set_thumbnail(url=after.icon.url)
        e.set_footer(text=f"Guild ID: {after.id}")
        await self._send(after, e)

    # ── Emoji & Sticker Events ────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_guild_emojis_update(self,
                                     guild: discord.Guild,
                                     before: list[discord.Emoji],
                                     after: list[discord.Emoji]) -> None:
        before_ids = {em.id for em in before}
        after_ids  = {em.id for em in after}
        added   = [em for em in after  if em.id not in before_ids]
        removed = [em for em in before if em.id not in after_ids]

        if not added and not removed:
            return

        e = self._e(C["emoji"], "😀  Emojis Updated")
        if added:
            e.add_field(name=f"✅ Added ({len(added)})",
                        value=" ".join(str(em) for em in added[:20]) or "—",
                        inline=False)
        if removed:
            e.add_field(name=f"❌ Removed ({len(removed)})",
                        value=" ".join(f"`:{em.name}:`" for em in removed[:20]) or "—",
                        inline=False)
        await self._send(guild, e)

    @commands.Cog.listener()
    async def on_guild_stickers_update(self,
                                       guild: discord.Guild,
                                       before: list[discord.GuildSticker],
                                       after: list[discord.GuildSticker]) -> None:
        before_ids = {s.id for s in before}
        after_ids  = {s.id for s in after}
        added   = [s for s in after  if s.id not in before_ids]
        removed = [s for s in before if s.id not in after_ids]

        if not added and not removed:
            return

        e = self._e(C["emoji"], "🎨  Stickers Updated")
        if added:
            e.add_field(name=f"✅ Added ({len(added)})",
                        value=", ".join(f"`{s.name}`" for s in added) or "—",
                        inline=False)
        if removed:
            e.add_field(name=f"❌ Removed ({len(removed)})",
                        value=", ".join(f"`{s.name}`" for s in removed) or "—",
                        inline=False)
        await self._send(guild, e)

    # ── Thread Events ─────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_thread_create(self, thread: discord.Thread) -> None:
        e = self._e(C["thread"], "🧵  Thread Created")
        e.add_field(name="Thread",     value=thread.mention, inline=True)
        e.add_field(name="In Channel", value=thread.parent.mention if thread.parent else "Unknown", inline=True)
        if thread.owner:
            e.add_field(name="Created By", value=thread.owner.mention, inline=True)
        e.set_footer(text=f"Thread ID: {thread.id}")
        await self._send(thread.guild, e)

    @commands.Cog.listener()
    async def on_thread_delete(self, thread: discord.Thread) -> None:
        e = self._e(C["remove"], "🧵  Thread Deleted")
        e.add_field(name="Thread",     value=f"`{thread.name}`", inline=True)
        e.add_field(name="In Channel", value=thread.parent.mention if thread.parent else "Unknown", inline=True)
        e.set_footer(text=f"Thread ID: {thread.id}")
        await self._send(thread.guild, e)

    @commands.Cog.listener()
    async def on_thread_update(self, before: discord.Thread, after: discord.Thread) -> None:
        fields: list[tuple[str, str, str]] = []

        if before.name != after.name:
            fields.append(("Name", before.name, after.name))
        if before.archived != after.archived:
            fields.append(("Archived", str(before.archived), str(after.archived)))
        if before.locked != after.locked:
            fields.append(("Locked", str(before.locked), str(after.locked)))
        if before.slowmode_delay != after.slowmode_delay:
            fields.append(("Slowmode", f"{before.slowmode_delay}s", f"{after.slowmode_delay}s"))

        if not fields:
            return

        e = self._e(C["update"], "🧵  Thread Updated")
        e.add_field(name="Thread", value=after.mention, inline=False)
        for label, old, new in fields:
            e.add_field(name=label, value=f"`{old}` → `{new}`", inline=True)
        e.set_footer(text=f"Thread ID: {after.id}")
        await self._send(after.guild, e)

    # ── Invite Events ─────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_invite_create(self, invite: discord.Invite) -> None:
        if not invite.guild:
            return
        e = self._e(C["invite"], "🔗  Invite Created")
        e.add_field(name="Code",       value=f"`discord.gg/{invite.code}`", inline=True)
        e.add_field(name="Channel",    value=invite.channel.mention if invite.channel else "—", inline=True)
        e.add_field(name="Created By", value=invite.inviter.mention if invite.inviter else "Unknown", inline=True)
        e.add_field(name="Max Uses",   value=str(invite.max_uses) if invite.max_uses else "∞", inline=True)
        e.add_field(name="Expires",
                    value=f"<t:{int(invite.expires_at.timestamp())}:R>" if invite.expires_at else "Never",
                    inline=True)
        e.add_field(name="Temporary",  value=str(invite.temporary), inline=True)
        e.set_footer(text=f"Invite code: {invite.code}")
        await self._send(invite.guild, e)  # type: ignore[arg-type]

    @commands.Cog.listener()
    async def on_invite_delete(self, invite: discord.Invite) -> None:
        if not invite.guild:
            return
        e = self._e(C["remove"], "🔗  Invite Deleted / Expired")
        e.add_field(name="Code",    value=f"`discord.gg/{invite.code}`", inline=True)
        e.add_field(name="Channel", value=invite.channel.mention if invite.channel else "—", inline=True)
        e.set_footer(text=f"Invite code: {invite.code}")
        await self._send(invite.guild, e)  # type: ignore[arg-type]

    # ── Slash Command Usage ───────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_app_command_completion(self,
                                        interaction: discord.Interaction,
                                        command: app_commands.Command | app_commands.ContextMenu) -> None:
        if not interaction.guild:
            return
        e = self._e(C["cmd"], "⌨️  Command Used")
        e.set_author(name=str(interaction.user), icon_url=interaction.user.display_avatar.url)
        e.add_field(name="Command", value=f"`/{command.qualified_name}`", inline=True)
        e.add_field(name="User",    value=f"{interaction.user.mention} `{interaction.user.id}`", inline=True)
        e.add_field(name="Channel", value=interaction.channel.mention if interaction.channel else "—", inline=True)
        e.set_footer(text=f"User ID: {interaction.user.id}")
        await self._send(interaction.guild, e)

    # ── Setup command (/set log channel) is in customization.py ──────────────


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Events(bot))
