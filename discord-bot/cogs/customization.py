"""
Customization cog — greet/leave/boost systems, named embeds with premium inventory, autorole, modlog, settings.
"""

import json
import discord
from discord import app_commands
from discord.ext import commands
from typing import Optional

from utils.embeds import EmbedFactory
from utils.helpers import (
    format_variables, extract_embed_ref, extract_actions,
    process_actions, VARIABLE_REFERENCE, ordinal,
)
from utils.database import (
    get_guild_config, upsert_guild_config,
    save_embed, get_embed, get_all_embeds, delete_embed, count_server_embeds,
    get_premium_tier, get_server_premium_tier,
    save_inventory_embed, get_inventory_embed, get_all_inventory_embeds,
    delete_inventory_embed, count_inventory_embeds,
)


def _get_embed_limit(config: dict, tier: int) -> int:
    if tier == 0:
        return config.get("premium", {}).get("free_server_embeds", 1)
    tiers = config.get("premium", {}).get("tiers", {})
    info = tiers.get(str(tier), {})
    limit = info.get("server_embeds", 1)
    return limit


def _get_inventory_limit(config: dict, tier: int) -> int:
    if tier == 0:
        return 0
    tiers = config.get("premium", {}).get("tiers", {})
    info = tiers.get(str(tier), {})
    limit = info.get("inventory_embeds", 0)
    return limit


async def _build_event_embed(
    config: dict,
    member: discord.Member,
    ef: EmbedFactory,
    event: str,
    channel: Optional[discord.abc.GuildChannel] = None,
) -> tuple[Optional[str], Optional[discord.Embed], list[dict]]:
    key_msg   = f"{event}_message"
    key_embed = f"{event}_embed"

    defaults = {
        "welcome": "Welcome {user.mention} to **{server}**!",
        "leave":   "{user.tag} left **{server}**. We now have {server.count} members.",
        "boost":   "{user.mention} just boosted **{server}**! We now have {server.boosts} boosts!",
    }
    template = config.get(key_msg) or defaults.get(event, "")

    clean, embed_name = extract_embed_ref(template)
    if not embed_name:
        embed_name = config.get(key_embed)
    clean, actions = extract_actions(clean)

    content = format_variables(clean, member=member, guild=member.guild, channel=channel)
    content = content.strip() or None

    embed: Optional[discord.Embed] = None
    if embed_name:
        embed_data = await get_embed(member.guild.id, embed_name)
        if embed_data:
            embed = _data_to_embed(embed_data, member=member, guild=member.guild, channel=channel)
    else:
        import datetime as _dt
        col = config.get("welcome_color") or ef.colors["success"]
        embed = discord.Embed(description=content or "", color=col)
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.timestamp = _dt.datetime.now(_dt.timezone.utc)
        count = member.guild.member_count or 0
        if event == "welcome":
            embed.set_footer(
                text     = f"You are the {ordinal(count)} member!",
                icon_url = member.guild.icon.url if member.guild.icon else None,
            )
        elif event == "leave":
            embed.set_footer(
                text     = f"{count} members remaining",
                icon_url = member.guild.icon.url if member.guild.icon else None,
            )
        elif event == "boost":
            embed.set_footer(
                text     = f"{member.guild.premium_subscription_count or 0} boosts",
                icon_url = member.guild.icon.url if member.guild.icon else None,
            )
        content = None

    return content, embed, actions


def _apply_vars_to_data(
    data: dict,
    member: Optional[discord.Member] = None,
    guild: Optional[discord.Guild] = None,
    channel: Optional[discord.abc.GuildChannel] = None,
) -> dict:
    """Return a copy of embed data with all text fields variable-substituted."""
    def v(s: str) -> str:
        if not s:
            return s
        return format_variables(s, member=member, guild=guild, channel=channel)

    resolved = dict(data)
    resolved["title"]       = v(data.get("title", ""))
    resolved["description"] = v(data.get("description", ""))
    resolved["author_name"] = v(data.get("author_name", ""))
    resolved["author_url"]  = v(data.get("author_url", ""))
    resolved["author_icon"] = v(data.get("author_icon", ""))
    resolved["footer_text"] = v(data.get("footer_text", ""))
    resolved["footer_icon"] = v(data.get("footer_icon", ""))
    resolved["thumbnail"]   = v(data.get("thumbnail", ""))
    resolved["image"]       = v(data.get("image", ""))
    resolved["url"]         = v(data.get("url", ""))
    resolved["fields"] = [
        {
            "name":   v(field.get("name", "")),
            "value":  v(field.get("value", "")),
            "inline": field.get("inline", True),
        }
        for field in data.get("fields", [])
    ]
    return resolved


def _data_to_embed(
    data: dict,
    member: Optional[discord.Member] = None,
    guild: Optional[discord.Guild] = None,
    channel: Optional[discord.abc.GuildChannel] = None,
) -> discord.Embed:
    if member is not None or guild is not None or channel is not None:
        data = _apply_vars_to_data(data, member=member, guild=guild, channel=channel)
    e = discord.Embed(
        title       = data.get("title") or None,
        description = data.get("description") or None,
        color       = data.get("color", 0xF2EAEA),
        url         = data.get("url") or None,
    )
    if data.get("author_name"):
        e.set_author(
            name=data["author_name"],
            url=data.get("author_url") or None,
            icon_url=data.get("author_icon") or None,
        )
    if data.get("footer_text"):
        e.set_footer(text=data["footer_text"], icon_url=data.get("footer_icon") or None)
    if data.get("thumbnail"):
        e.set_thumbnail(url=data["thumbnail"])
    if data.get("image"):
        e.set_image(url=data["image"])
    for field in data.get("fields", []):
        e.add_field(name=field["name"], value=field["value"], inline=field.get("inline", True))
    return e


def _embed_to_data(state: "EmbedState") -> dict:
    return {
        "title":       state.title,
        "description": state.description,
        "color":       state.color,
        "url":         state.url,
        "author_name": state.author_name,
        "author_url":  state.author_url,
        "author_icon": state.author_icon,
        "footer_text": state.footer_text,
        "footer_icon": state.footer_icon,
        "thumbnail":   state.thumbnail,
        "image":       state.image,
        "fields":      [{"name": n, "value": v, "inline": i} for n, v, i in state.fields],
    }


class EmbedState:
    __slots__ = (
        "title", "description", "color", "url",
        "author_name", "author_url", "author_icon",
        "footer_text", "footer_icon", "thumbnail", "image",
        "fields",
    )
    def __init__(self, data: Optional[dict] = None):
        self.title:       str                       = ""
        self.description: str                       = ""
        self.color:       int                       = 0xF2EAEA
        self.url:         str                       = ""
        self.author_name: str                       = ""
        self.author_url:  str                       = ""
        self.author_icon: str                       = ""
        self.footer_text: str                       = ""
        self.footer_icon: str                       = ""
        self.thumbnail:   str                       = ""
        self.image:       str                       = ""
        self.fields:      list[tuple[str, str, bool]] = []
        if data:
            self.title       = data.get("title", "")
            self.description = data.get("description", "")
            self.color       = data.get("color", 0xF2EAEA)
            self.url         = data.get("url", "")
            self.author_name = data.get("author_name", "")
            self.author_url  = data.get("author_url", "")
            self.author_icon = data.get("author_icon", "")
            self.footer_text = data.get("footer_text", "")
            self.footer_icon = data.get("footer_icon", "")
            self.thumbnail   = data.get("thumbnail", "")
            self.image       = data.get("image", "")
            self.fields      = [
                (f["name"], f["value"], f.get("inline", True))
                for f in data.get("fields", [])
            ]

    def build(self) -> discord.Embed:
        e = discord.Embed(
            title       = self.title       or None,
            description = self.description or None,
            color       = self.color,
            url         = self.url         or None,
        )
        if self.author_name:
            e.set_author(name=self.author_name, url=self.author_url or None, icon_url=self.author_icon or None)
        if self.footer_text:
            e.set_footer(text=self.footer_text, icon_url=self.footer_icon or None)
        if self.thumbnail:
            e.set_thumbnail(url=self.thumbnail)
        if self.image:
            e.set_image(url=self.image)
        for name, value, inline in self.fields:
            e.add_field(name=name, value=value, inline=inline)
        return e


class BasicModal(discord.ui.Modal, title="Edit Basic Info"):
    t_title = discord.ui.TextInput(label="Title",         max_length=256,  required=False)
    t_desc  = discord.ui.TextInput(label="Description",   max_length=4000, required=False, style=discord.TextStyle.paragraph)
    t_color = discord.ui.TextInput(label="Color (#hex)",   max_length=7,   required=False, default="#F2EAEA")
    t_url   = discord.ui.TextInput(label="Title URL",      max_length=512, required=False)

    def __init__(self, s: EmbedState, v: "BuilderView"):
        super().__init__()
        self.s = s; self.v = v
        if s.title:       self.t_title.default = s.title
        if s.description: self.t_desc.default  = s.description
        if s.url:         self.t_url.default   = s.url

    async def on_submit(self, interaction: discord.Interaction):
        self.s.title       = self.t_title.value.strip()
        self.s.description = self.t_desc.value.strip()
        self.s.url         = self.t_url.value.strip()
        raw = self.t_color.value.strip().lstrip("#")
        try:
            self.s.color = int(raw, 16) if raw else 0xF2EAEA
        except ValueError:
            pass
        await self.v.save_and_update(interaction)


class AuthorModal(discord.ui.Modal, title="Edit Author"):
    t_name = discord.ui.TextInput(label="Author Name",     max_length=256, required=False)
    t_url  = discord.ui.TextInput(label="Author URL",      max_length=512, required=False)
    t_icon = discord.ui.TextInput(label="Author Icon URL", max_length=512, required=False)

    def __init__(self, s: EmbedState, v: "BuilderView"):
        super().__init__()
        self.s = s; self.v = v
        if s.author_name: self.t_name.default = s.author_name
        if s.author_url:  self.t_url.default  = s.author_url
        if s.author_icon: self.t_icon.default = s.author_icon

    async def on_submit(self, interaction: discord.Interaction):
        self.s.author_name = self.t_name.value.strip()
        self.s.author_url  = self.t_url.value.strip()
        self.s.author_icon = self.t_icon.value.strip()
        await self.v.save_and_update(interaction)


class FooterModal(discord.ui.Modal, title="Edit Footer"):
    t_text = discord.ui.TextInput(label="Footer Text",     max_length=2048, required=False)
    t_icon = discord.ui.TextInput(label="Footer Icon URL", max_length=512,  required=False)

    def __init__(self, s: EmbedState, v: "BuilderView"):
        super().__init__()
        self.s = s; self.v = v
        if s.footer_text: self.t_text.default = s.footer_text
        if s.footer_icon: self.t_icon.default = s.footer_icon

    async def on_submit(self, interaction: discord.Interaction):
        self.s.footer_text = self.t_text.value.strip()
        self.s.footer_icon = self.t_icon.value.strip()
        await self.v.save_and_update(interaction)


class ImagesModal(discord.ui.Modal, title="Edit Images"):
    t_thumb = discord.ui.TextInput(label="Thumbnail URL",  max_length=512, required=False)
    t_image = discord.ui.TextInput(label="Main Image URL", max_length=512, required=False)

    def __init__(self, s: EmbedState, v: "BuilderView"):
        super().__init__()
        self.s = s; self.v = v
        if s.thumbnail: self.t_thumb.default = s.thumbnail
        if s.image:     self.t_image.default = s.image

    async def on_submit(self, interaction: discord.Interaction):
        self.s.thumbnail = self.t_thumb.value.strip()
        self.s.image     = self.t_image.value.strip()
        await self.v.save_and_update(interaction)


class FieldModal(discord.ui.Modal, title="Add Field"):
    t_name   = discord.ui.TextInput(label="Field Name",  max_length=256)
    t_value  = discord.ui.TextInput(label="Field Value", max_length=1024, style=discord.TextStyle.paragraph)
    t_inline = discord.ui.TextInput(label="Inline? (yes/no)", max_length=3, default="yes")

    def __init__(self, s: EmbedState, v: "BuilderView"):
        super().__init__()
        self.s = s; self.v = v

    async def on_submit(self, interaction: discord.Interaction):
        if len(self.s.fields) >= 25:
            return await interaction.response.send_message("Max 25 fields reached.", ephemeral=False)
        inline = self.t_inline.value.strip().lower() not in ("no", "false", "n", "0")
        self.s.fields.append((self.t_name.value, self.t_value.value, inline))
        await self.v.save_and_update(interaction)


class BuilderView(discord.ui.View):
    def __init__(self, s: EmbedState, guild_id: int, embed_name: str, owner_id: int, show_inventory: bool = False):
        super().__init__(timeout=600)
        self.s          = s
        self.guild_id   = guild_id
        self.embed_name = embed_name
        self.owner_id   = owner_id
        if show_inventory:
            self.add_item(InventoryButton(s, owner_id, embed_name))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Only the embed creator can use these buttons.", ephemeral=True)
            return False
        return True

    async def save_and_update(self, interaction: discord.Interaction):
        data = _embed_to_data(self.s)
        await save_embed(self.guild_id, self.embed_name, data)
        member  = interaction.user if isinstance(interaction.user, discord.Member) else None
        guild   = interaction.guild
        channel = interaction.channel if isinstance(interaction.channel, discord.abc.GuildChannel) else None
        preview = _data_to_embed(data, member=member, guild=guild, channel=channel)
        if not preview.title and not preview.description:
            preview.description = "*Empty embed — use the buttons below to add content.*"
        await interaction.response.edit_message(embed=preview, view=self)

    @discord.ui.button(label="Edit Basic Info", style=discord.ButtonStyle.secondary, row=0)
    async def basic(self, i: discord.Interaction, _: discord.ui.Button):
        await i.response.send_modal(BasicModal(self.s, self))

    @discord.ui.button(label="Edit Author", style=discord.ButtonStyle.secondary, row=1)
    async def author(self, i: discord.Interaction, _: discord.ui.Button):
        await i.response.send_modal(AuthorModal(self.s, self))

    @discord.ui.button(label="Edit Footer", style=discord.ButtonStyle.secondary, row=1)
    async def footer(self, i: discord.Interaction, _: discord.ui.Button):
        await i.response.send_modal(FooterModal(self.s, self))

    @discord.ui.button(label="Edit Images", style=discord.ButtonStyle.secondary, row=1)
    async def images(self, i: discord.Interaction, _: discord.ui.Button):
        await i.response.send_modal(ImagesModal(self.s, self))

    @discord.ui.button(label="Add Field", style=discord.ButtonStyle.primary, row=2)
    async def add_field(self, i: discord.Interaction, _: discord.ui.Button):
        await i.response.send_modal(FieldModal(self.s, self))

    @discord.ui.button(label="Remove Field", style=discord.ButtonStyle.secondary, row=2)
    async def rm_field(self, i: discord.Interaction, _: discord.ui.Button):
        if self.s.fields:
            self.s.fields.pop()
            await self.save_and_update(i)
        else:
            await i.response.send_message("No fields to remove.", ephemeral=False)

    @discord.ui.button(label="Variables", style=discord.ButtonStyle.secondary, row=2)
    async def vars_btn(self, i: discord.Interaction, _: discord.ui.Button):
        e = discord.Embed(title="Variables", description=VARIABLE_REFERENCE, color=0xF2EAEA)
        await i.response.send_message(embed=e, ephemeral=False)


class InventoryButton(discord.ui.Button):
    def __init__(self, s: EmbedState, owner_id: int, embed_name: str):
        super().__init__(label="Add to Inventory", style=discord.ButtonStyle.success, row=3)
        self.s = s
        self.owner_id = owner_id
        self.embed_name = embed_name

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.owner_id:
            return await interaction.response.send_message("Only the embed creator can use this.", ephemeral=True)
        await save_inventory_embed(interaction.user.id, self.embed_name, _embed_to_data(self.s))
        await interaction.response.send_message(
            f"Saved `{self.embed_name}` to your personal embed inventory!", ephemeral=False
        )


class ImportView(discord.ui.View):
    def __init__(self, guild_id: int, embed_name: str, inv_data: dict, owner_id: int, config: dict, ef: EmbedFactory):
        super().__init__(timeout=60)
        self.guild_id   = guild_id
        self.embed_name = embed_name
        self.inv_data   = inv_data
        self.owner_id   = owner_id
        self.config     = config
        self.ef         = ef

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.owner_id

    @discord.ui.button(label="Use from Inventory", style=discord.ButtonStyle.success)
    async def use_inv(self, interaction: discord.Interaction, _: discord.ui.Button):
        state = EmbedState(self.inv_data)
        await save_embed(self.guild_id, self.embed_name, self.inv_data)
        tier = await get_premium_tier(self.owner_id)
        show_inv = tier >= 1
        view = BuilderView(state, self.guild_id, self.embed_name, self.owner_id, show_inventory=show_inv)
        preview = state.build()
        await interaction.response.edit_message(
            content=f"Imported `{self.embed_name}` from your inventory!",
            embed=preview, view=view,
        )

    @discord.ui.button(label="Start Fresh", style=discord.ButtonStyle.secondary)
    async def start_fresh(self, interaction: discord.Interaction, _: discord.ui.Button):
        state = EmbedState()
        await save_embed(self.guild_id, self.embed_name, _embed_to_data(state))
        tier = await get_premium_tier(self.owner_id)
        show_inv = tier >= 1
        view = BuilderView(state, self.guild_id, self.embed_name, self.owner_id, show_inventory=show_inv)
        await interaction.response.edit_message(
            content=f"Starting fresh embed `{self.embed_name}`!",
            embed=discord.Embed(description="*Empty embed — use the buttons below to add content.*", color=0xF2EAEA),
            view=view,
        )


class SettingsSelect(discord.ui.Select):
    def __init__(self):
        opts = [
            discord.SelectOption(label="Greet",     value="greet",    description="Welcome message config"),
            discord.SelectOption(label="Leave",     value="leave",    description="Leave message config"),
            discord.SelectOption(label="Boost",     value="boost",    description="Boost message config"),
            discord.SelectOption(label="Auto-Role", value="autorole", description="Auto-assign on join"),
            discord.SelectOption(label="Anti-Nuke", value="antinuke", description="Raid protection"),
            discord.SelectOption(label="Mod Log",   value="modlog",   description="Moderation log channel"),
        ]
        super().__init__(placeholder="Select a category...", options=opts)

    async def callback(self, interaction: discord.Interaction):
        v: SettingsView = self.view  # type: ignore[assignment]
        await interaction.response.edit_message(embed=v.page(self.values[0]))


class SettingsView(discord.ui.View):
    def __init__(self, config: dict, ef: EmbedFactory):
        super().__init__(timeout=180)
        self.config = config
        self.ef     = ef
        self.add_item(SettingsSelect())

    def _event_page(self, key: str, label: str) -> discord.Embed:
        c = self.config
        db_key = "welcome" if key == "greet" else key
        enabled = bool(c.get(f"{db_key}_enabled", 0))
        ch_id   = c.get(f"{db_key}_channel")
        e = self.ef.build(author_name=f"{label} Settings", color_key="success" if enabled else "secondary")
        e.add_field(name="Status",  value=f"{self.ef.e['enabled']} Enabled" if enabled else f"{self.ef.e['disabled']} Disabled", inline=True)
        e.add_field(name="Channel", value=f"<#{ch_id}>" if ch_id else "`-`",     inline=True)
        template = c.get(f"{db_key}_message") or "Default"
        e.add_field(name="Message Template", value=f"```{template[:200]}```", inline=False)
        embed_name = c.get(f"{db_key}_embed")
        if embed_name:
            e.add_field(name="Linked Embed", value=f"`{{embed:{embed_name}}}`", inline=False)
        return e

    def page(self, key: str) -> discord.Embed:
        c = self.config

        if key in ("greet", "leave", "boost"):
            label = key.title()
            return self._event_page(key, label)

        elif key == "autorole":
            enabled = bool(c.get("autorole_enabled", 0))
            role_id = c.get("autorole_id")
            e = self.ef.build(author_name="Auto-Role Settings", color_key="success" if enabled else "secondary")
            e.add_field(name="Status", value=f"{self.ef.e['enabled']} Enabled" if enabled else f"{self.ef.e['disabled']} Disabled", inline=True)
            e.add_field(name="Role",   value=f"<@&{role_id}>" if role_id else "`-`", inline=True)

        elif key == "antinuke":
            enabled    = bool(c.get("antinuke_enabled", 1))
            punishment = c.get("antinuke_punishment", "ban")
            e = self.ef.build(author_name="Anti-Nuke Settings", color_key="success" if enabled else "error")
            e.add_field(name="Status",     value=f"{self.ef.e['enabled']} Enabled" if enabled else f"{self.ef.e['disabled']} Disabled",       inline=True)
            e.add_field(name="Punishment", value=f"`{punishment.upper()}`",                   inline=True)
            e.add_field(name="Window",     value=f"`{c.get('antinuke_window', 10)}s`",        inline=True)
            e.add_field(name="Ban",        value=f"`{c.get('antinuke_ban_thresh',  3)}`",     inline=True)
            e.add_field(name="Kick",       value=f"`{c.get('antinuke_kick_thresh', 3)}`",     inline=True)
            e.add_field(name="Chan Del",   value=f"`{c.get('antinuke_chan_thresh',  3)}`",     inline=True)

        else:
            ch_id = c.get("mod_log_channel")
            e = self.ef.build(author_name="Mod Log Settings", color_key="primary")
            e.add_field(name="Channel", value=f"<#{ch_id}>" if ch_id else "`-`", inline=True)

        return e


class Customization(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.ef: EmbedFactory = bot.ef  # type: ignore[attr-defined]
        self.config: dict = bot.config  # type: ignore[attr-defined]

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        config = await get_guild_config(member.guild.id)

        if config.get("autorole_enabled") and config.get("autorole_id"):
            role = member.guild.get_role(config["autorole_id"])
            if role:
                try:
                    await member.add_roles(role, reason="Auto-role")
                except discord.Forbidden:
                    pass

        if config.get("welcome_enabled") and config.get("welcome_channel"):
            ch = member.guild.get_channel(config["welcome_channel"])
            if not isinstance(ch, discord.TextChannel):
                return
            content, embed, actions = await _build_event_embed(config, member, self.ef, "welcome", ch)
            try:
                await ch.send(content=content, embed=embed)
            except discord.Forbidden:
                pass
            if actions:
                await process_actions(actions, member, member.guild)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        config = await get_guild_config(member.guild.id)
        if not config.get("leave_enabled") or not config.get("leave_channel"):
            return
        ch = member.guild.get_channel(config["leave_channel"])
        if not isinstance(ch, discord.TextChannel):
            return
        content, embed, actions = await _build_event_embed(config, member, self.ef, "leave", ch)
        try:
            await ch.send(content=content, embed=embed)
        except discord.Forbidden:
            pass
        if actions:
            try:
                await process_actions(actions, member, member.guild)
            except Exception:
                pass

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        if before.premium_since is not None or after.premium_since is None:
            return
        config = await get_guild_config(after.guild.id)
        if not config.get("boost_enabled") or not config.get("boost_channel"):
            return
        ch = after.guild.get_channel(config["boost_channel"])
        if not isinstance(ch, discord.TextChannel):
            return
        content, embed, actions = await _build_event_embed(config, after, self.ef, "boost", ch)
        try:
            await ch.send(content=content, embed=embed)
        except discord.Forbidden:
            pass
        if actions:
            await process_actions(actions, after, after.guild)

    set_group = app_commands.Group(
        name="set",
        description="Configure server settings.",
        default_permissions=discord.Permissions(manage_guild=True),
    )

    greet_group = app_commands.Group(name="greet", description="Greet message settings.", parent=set_group)

    @greet_group.command(name="message", description="Set the greet message template.")
    @app_commands.describe(message="Template with {variables}.")
    async def greet_message(self, interaction: discord.Interaction, message: str):
        assert interaction.guild
        await upsert_guild_config(interaction.guild.id, welcome_message=message)
        await interaction.response.send_message(
            embed=self.ef.success(f"Greet message updated.\n```{message[:200]}```"), ephemeral=False
        )

    @greet_group.command(name="channel", description="Set the channel for greet messages.")
    @app_commands.describe(channel="Channel to send greet messages to.")
    async def greet_channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        assert interaction.guild
        await upsert_guild_config(interaction.guild.id, welcome_channel=channel.id, welcome_enabled=1)
        await interaction.response.send_message(
            embed=self.ef.success(f"Greet messages will be sent to {channel.mention}."), ephemeral=False
        )

    @greet_group.command(name="toggle", description="Enable or disable greet messages.")
    async def greet_toggle(self, interaction: discord.Interaction):
        assert interaction.guild
        config = await get_guild_config(interaction.guild.id)
        new = 0 if config.get("welcome_enabled") else 1
        await upsert_guild_config(interaction.guild.id, welcome_enabled=new)
        await interaction.response.send_message(
            embed=self.ef.success(f"Greet messages {self.ef.e['enabled'] if new else self.ef.e['disabled']} **{'enabled' if new else 'disabled'}**."), ephemeral=False
        )

    @greet_group.command(name="embed", description="Link a named embed to greet messages.")
    @app_commands.describe(name="Name of the saved embed to attach.")
    async def greet_embed(self, interaction: discord.Interaction, name: str):
        assert interaction.guild
        existing = await get_embed(interaction.guild.id, name)
        if not existing:
            return await interaction.response.send_message(
                embed=self.ef.error(f"No embed named `{name}` found. Create one with `/embed create`."), ephemeral=False
            )
        await upsert_guild_config(interaction.guild.id, welcome_embed=name.lower())
        await interaction.response.send_message(
            embed=self.ef.success(f"Greet messages will now use `{{embed:{name}}}`."), ephemeral=False
        )

    leave_group = app_commands.Group(name="leave", description="Leave message settings.", parent=set_group)

    @leave_group.command(name="message", description="Set the leave message template.")
    @app_commands.describe(message="Template with {variables}.")
    async def leave_message(self, interaction: discord.Interaction, message: str):
        assert interaction.guild
        await upsert_guild_config(interaction.guild.id, leave_message=message)
        await interaction.response.send_message(
            embed=self.ef.success(f"Leave message updated.\n```{message[:200]}```"), ephemeral=False
        )

    @leave_group.command(name="channel", description="Set the channel for leave messages.")
    @app_commands.describe(channel="Channel to send leave messages to.")
    async def leave_channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        assert interaction.guild
        await upsert_guild_config(interaction.guild.id, leave_channel=channel.id, leave_enabled=1)
        await interaction.response.send_message(
            embed=self.ef.success(f"Leave messages will be sent to {channel.mention}."), ephemeral=False
        )

    @leave_group.command(name="toggle", description="Enable or disable leave messages.")
    async def leave_toggle(self, interaction: discord.Interaction):
        assert interaction.guild
        config = await get_guild_config(interaction.guild.id)
        new = 0 if config.get("leave_enabled") else 1
        await upsert_guild_config(interaction.guild.id, leave_enabled=new)
        await interaction.response.send_message(
            embed=self.ef.success(f"Leave messages {self.ef.e['enabled'] if new else self.ef.e['disabled']} **{'enabled' if new else 'disabled'}**."), ephemeral=False
        )

    @leave_group.command(name="embed", description="Link a named embed to leave messages.")
    @app_commands.describe(name="Name of the saved embed to attach.")
    async def leave_embed(self, interaction: discord.Interaction, name: str):
        assert interaction.guild
        existing = await get_embed(interaction.guild.id, name)
        if not existing:
            return await interaction.response.send_message(
                embed=self.ef.error(f"No embed named `{name}` found. Create one with `/embed create`."), ephemeral=False
            )
        await upsert_guild_config(interaction.guild.id, leave_embed=name.lower())
        await interaction.response.send_message(
            embed=self.ef.success(f"Leave messages will now use `{{embed:{name}}}`."), ephemeral=False
        )

    boost_group = app_commands.Group(name="boost", description="Boost message settings.", parent=set_group)

    @boost_group.command(name="message", description="Set the boost message template.")
    @app_commands.describe(message="Template with {variables}.")
    async def boost_message(self, interaction: discord.Interaction, message: str):
        assert interaction.guild
        await upsert_guild_config(interaction.guild.id, boost_message=message)
        await interaction.response.send_message(
            embed=self.ef.success(f"Boost message updated.\n```{message[:200]}```"), ephemeral=False
        )

    @boost_group.command(name="channel", description="Set the channel for boost messages.")
    @app_commands.describe(channel="Channel to send boost messages to.")
    async def boost_channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        assert interaction.guild
        await upsert_guild_config(interaction.guild.id, boost_channel=channel.id, boost_enabled=1)
        await interaction.response.send_message(
            embed=self.ef.success(f"Boost messages will be sent to {channel.mention}."), ephemeral=False
        )

    @boost_group.command(name="toggle", description="Enable or disable boost messages.")
    async def boost_toggle(self, interaction: discord.Interaction):
        assert interaction.guild
        config = await get_guild_config(interaction.guild.id)
        new = 0 if config.get("boost_enabled") else 1
        await upsert_guild_config(interaction.guild.id, boost_enabled=new)
        await interaction.response.send_message(
            embed=self.ef.success(f"Boost messages {self.ef.e['enabled'] if new else self.ef.e['disabled']} **{'enabled' if new else 'disabled'}**."), ephemeral=False
        )

    @boost_group.command(name="embed", description="Link a named embed to boost messages.")
    @app_commands.describe(name="Name of the saved embed to attach.")
    async def boost_embed(self, interaction: discord.Interaction, name: str):
        assert interaction.guild
        existing = await get_embed(interaction.guild.id, name)
        if not existing:
            return await interaction.response.send_message(
                embed=self.ef.error(f"No embed named `{name}` found. Create one with `/embed create`."), ephemeral=False
            )
        await upsert_guild_config(interaction.guild.id, boost_embed=name.lower())
        await interaction.response.send_message(
            embed=self.ef.success(f"Boost messages will now use `{{embed:{name}}}`."), ephemeral=False
        )

    log_group = app_commands.Group(name="log", description="Logging settings.", parent=set_group)

    @log_group.command(name="channel", description="Set the channel where all server events are logged.")
    @app_commands.describe(channel="The log channel.")
    async def log_channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        assert interaction.guild
        await upsert_guild_config(interaction.guild.id, log_channel_id=channel.id)
        await interaction.response.send_message(
            embed=self.ef.success(f"Log channel set to {channel.mention}. All server events will be posted there."),
            ephemeral=False,
        )

    @log_group.command(name="disable", description="Disable server event logging.")
    async def log_disable(self, interaction: discord.Interaction):
        assert interaction.guild
        await upsert_guild_config(interaction.guild.id, log_channel_id=None)
        await interaction.response.send_message(
            embed=self.ef.success("Server event logging has been disabled."), ephemeral=False
        )

    jail_group = app_commands.Group(name="jail", description="Jail system settings.", parent=set_group)

    @jail_group.command(name="channel", description="Set the channel where jailed members can talk.")
    @app_commands.describe(channel="The jail channel.")
    async def jail_channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        assert interaction.guild
        await upsert_guild_config(interaction.guild.id, jail_channel_id=channel.id)
        await interaction.response.send_message(
            embed=self.ef.success(f"Jail channel set to {channel.mention}."), ephemeral=False
        )

    test_group = app_commands.Group(
        name="test",
        description="Test server features.",
        default_permissions=discord.Permissions(manage_guild=True),
    )

    @test_group.command(name="greet", description="Sends a test greet message to the configured greet channel.")
    async def test_greet(self, interaction: discord.Interaction):
        assert interaction.guild and isinstance(interaction.user, discord.Member)
        await interaction.response.defer(ephemeral=False)
        config = await get_guild_config(interaction.guild.id)
        ch_id = config.get("welcome_channel")
        if not ch_id:
            return await interaction.followup.send(
                embed=self.ef.error("No greet channel set. Use `/set greet channel` first."), ephemeral=False
            )
        ch = interaction.guild.get_channel(ch_id)
        if not isinstance(ch, discord.TextChannel):
            return await interaction.followup.send(
                embed=self.ef.error("Greet channel not found or is not a text channel."), ephemeral=False
            )
        content, embed, _ = await _build_event_embed(config, interaction.user, self.ef, "welcome", ch)
        try:
            await ch.send(content=content, embed=embed)
            await interaction.followup.send(
                embed=self.ef.success(f"Test greet sent to {ch.mention}."), ephemeral=False
            )
        except discord.Forbidden:
            await interaction.followup.send(
                embed=self.ef.error(f"No permission to send in {ch.mention}."), ephemeral=False
            )

    @test_group.command(name="leave", description="Sends a test leave message to the configured leave channel.")
    async def test_leave(self, interaction: discord.Interaction):
        assert interaction.guild and isinstance(interaction.user, discord.Member)
        await interaction.response.defer(ephemeral=False)
        config = await get_guild_config(interaction.guild.id)
        ch_id = config.get("leave_channel")
        if not ch_id:
            return await interaction.followup.send(
                embed=self.ef.error("No leave channel set. Use `/set leave channel` first."), ephemeral=False
            )
        ch = interaction.guild.get_channel(ch_id)
        if not isinstance(ch, discord.TextChannel):
            return await interaction.followup.send(
                embed=self.ef.error("Leave channel not found or is not a text channel."), ephemeral=False
            )
        content, embed, _ = await _build_event_embed(config, interaction.user, self.ef, "leave", ch)
        try:
            await ch.send(content=content, embed=embed)
            await interaction.followup.send(
                embed=self.ef.success(f"Test leave sent to {ch.mention}."), ephemeral=False
            )
        except discord.Forbidden:
            await interaction.followup.send(
                embed=self.ef.error(f"No permission to send in {ch.mention}."), ephemeral=False
            )

    @test_group.command(name="boost", description="Sends a test boost message to the configured boost channel.")
    async def test_boost(self, interaction: discord.Interaction):
        assert interaction.guild and isinstance(interaction.user, discord.Member)
        await interaction.response.defer(ephemeral=False)
        config = await get_guild_config(interaction.guild.id)
        ch_id = config.get("boost_channel")
        if not ch_id:
            return await interaction.followup.send(
                embed=self.ef.error("No boost channel set. Use `/set boost channel` first."), ephemeral=False
            )
        ch = interaction.guild.get_channel(ch_id)
        if not isinstance(ch, discord.TextChannel):
            return await interaction.followup.send(
                embed=self.ef.error("Boost channel not found or is not a text channel."), ephemeral=False
            )
        content, embed, _ = await _build_event_embed(config, interaction.user, self.ef, "boost", ch)
        try:
            await ch.send(content=content, embed=embed)
            await interaction.followup.send(
                embed=self.ef.success(f"Test boost sent to {ch.mention}."), ephemeral=False
            )
        except discord.Forbidden:
            await interaction.followup.send(
                embed=self.ef.error(f"No permission to send in {ch.mention}."), ephemeral=False
            )

    embed_group = app_commands.Group(
        name="embed",
        description="Named embed system.",
        default_permissions=discord.Permissions(manage_messages=True),
    )

    @embed_group.command(name="create", description="Create a new named embed and open the builder.")
    @app_commands.describe(name="Name of the embed.")
    async def embed_create(self, interaction: discord.Interaction, name: app_commands.Range[str, 1, 32]):
        assert interaction.guild
        name_clean = name.lower().strip().replace(" ", "-")

        existing = await get_embed(interaction.guild.id, name_clean)
        if existing:
            return await interaction.response.send_message(
                embed=self.ef.error(f"An embed named `{name_clean}` already exists. Use `/embed edit` to modify it."),
                ephemeral=False,
            )

        server_tier = await get_server_premium_tier(interaction.guild.id)
        limit = _get_embed_limit(self.config, server_tier)
        if limit != -1:
            current = await count_server_embeds(interaction.guild.id)
            if current >= limit:
                tier_info = ""
                if server_tier == 0:
                    tier_info = "\nA premium member can activate higher limits with `/activate premium`."
                return await interaction.response.send_message(
                    embed=self.ef.error(f"Server embed limit reached (`{current}/{limit}`).{tier_info}"),
                    ephemeral=False,
                )
        tier = await get_premium_tier(interaction.user.id)

        inv_data = await get_inventory_embed(interaction.user.id, name_clean)
        if inv_data and tier >= 1:
            view = ImportView(interaction.guild.id, name_clean, inv_data, interaction.user.id, self.config, self.ef)
            return await interaction.response.send_message(
                embed=self.ef.info(
                    f"You have `{name_clean}` in your embed inventory.\n"
                    f"Would you like to use it, or start fresh?"
                ),
                view=view,
            )

        state = EmbedState()
        await save_embed(interaction.guild.id, name_clean, _embed_to_data(state))
        show_inv = tier >= 1
        info = self.ef.build(
            description=(
                f"Created an embed with name `{name_clean}`.\n"
                f"  You can now refer to the embed with `{{embed:{name_clean}}}` "
                f"in your greet/leave/boost message.\n"
                f"  For example, to attach to a greet message, use\n"
                f"  `/set greet message message: Welcome {{user}} {{embed:{name_clean}}}`\n\n"
                f"Select from the buttons below to edit!"
            ),
            color_key="success",
        )
        view = BuilderView(state, interaction.guild.id, name_clean, interaction.user.id, show_inventory=show_inv)
        await interaction.response.send_message(embed=info, view=view)

    @embed_group.command(name="edit", description="Open the builder for an existing embed.")
    @app_commands.describe(name="Name of the embed to edit.")
    async def embed_edit(self, interaction: discord.Interaction, name: str):
        assert interaction.guild
        name_clean = name.lower().strip()
        data = await get_embed(interaction.guild.id, name_clean)
        if not data:
            return await interaction.response.send_message(
                embed=self.ef.error(f"No embed named `{name_clean}` found."), ephemeral=False
            )
        state = EmbedState(data)
        preview = state.build()
        if not preview.title and not preview.description:
            preview.description = "*Empty embed — use the buttons below to add content.*"
        tier = await get_premium_tier(interaction.user.id)
        show_inv = tier >= 1
        view = BuilderView(state, interaction.guild.id, name_clean, interaction.user.id, show_inventory=show_inv)
        await interaction.response.send_message(
            content=f"Editing embed: `{name_clean}`", embed=preview, view=view
        )

    @embed_group.command(name="send", description="Send a saved embed to a channel.")
    @app_commands.describe(name="Name of the embed.", channel="Target channel (defaults to current).")
    async def embed_send(self, interaction: discord.Interaction, name: str, channel: Optional[discord.TextChannel] = None):
        assert interaction.guild
        target = channel or interaction.channel
        if not isinstance(target, discord.TextChannel):
            return await interaction.response.send_message(
                embed=self.ef.error("Invalid target channel."), ephemeral=False
            )
        data = await get_embed(interaction.guild.id, name.lower().strip())
        if not data:
            return await interaction.response.send_message(
                embed=self.ef.error(f"No embed named `{name}` found."), ephemeral=False
            )
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        embed = _data_to_embed(data, member=member, guild=interaction.guild, channel=target)
        try:
            await target.send(embed=embed)
            await interaction.response.send_message(
                embed=self.ef.success(f"Embed `{name}` sent to {target.mention}."), ephemeral=False
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                embed=self.ef.error(f"No permission to send in {target.mention}."), ephemeral=False
            )

    @embed_group.command(name="list", description="List all saved embeds.")
    async def embed_list(self, interaction: discord.Interaction):
        assert interaction.guild
        embeds = await get_all_embeds(interaction.guild.id)
        if not embeds:
            return await interaction.response.send_message(
                embed=self.ef.info("No saved embeds. Use `/embed create` to make one."), ephemeral=False
            )
        tier = await get_premium_tier(interaction.user.id)
        limit = _get_embed_limit(self.config, tier)
        limit_str = "unlimited" if limit == -1 else str(limit)
        e = self.ef.build(author_name=f"Saved Embeds  ({len(embeds)}/{limit_str})", color_key="accent")
        for row in embeds:
            e.add_field(
                name  = f"`{row['name']}`",
                value = f"Use: `{{embed:{row['name']}}}`  {self.ef.e['dot']}  <t:{row['updated_at']}:R>",
                inline=False,
            )
        await interaction.response.send_message(embed=e, ephemeral=False)

    @embed_group.command(name="delete", description="Delete a saved embed.")
    @app_commands.describe(name="Name of the embed to delete.")
    async def embed_delete(self, interaction: discord.Interaction, name: str):
        assert interaction.guild
        deleted = await delete_embed(interaction.guild.id, name.lower().strip())
        if deleted:
            await interaction.response.send_message(
                embed=self.ef.success(f"Embed `{name}` deleted."), ephemeral=False
            )
        else:
            await interaction.response.send_message(
                embed=self.ef.error(f"No embed named `{name}` found."), ephemeral=False
            )

    @embed_group.command(name="inventory", description="View your personal embed inventory.")
    async def embed_inventory(self, interaction: discord.Interaction):
        tier = await get_premium_tier(interaction.user.id)
        if tier < 1:
            return await interaction.response.send_message(
                embed=self.ef.error("Embed inventory is a premium feature. Check `/premium` for details."),
                ephemeral=False,
            )
        embeds = await get_all_inventory_embeds(interaction.user.id)
        limit = _get_inventory_limit(self.config, tier)
        limit_str = "unlimited" if limit == -1 else str(limit)
        if not embeds:
            return await interaction.response.send_message(
                embed=self.ef.info(
                    f"Your inventory is empty (`0/{limit_str}`).\n"
                    f"Use the green **Add to Inventory** button when editing an embed to save it."
                ),
                ephemeral=False,
            )
        e = self.ef.build(author_name=f"Your Embed Inventory  ({len(embeds)}/{limit_str})", color_key="accent")
        for row in embeds:
            e.add_field(
                name  = f"`{row['name']}`",
                value = f"<t:{row['updated_at']}:R>",
                inline=True,
            )
        await interaction.response.send_message(embed=e, ephemeral=False)

    @embed_group.command(name="invdelete", description="Delete an embed from your personal inventory.")
    @app_commands.describe(name="Name of the embed to remove from your inventory.")
    async def embed_invdelete(self, interaction: discord.Interaction, name: str):
        tier = await get_premium_tier(interaction.user.id)
        if tier < 1:
            return await interaction.response.send_message(
                embed=self.ef.error("Embed inventory is a premium feature."), ephemeral=False
            )
        deleted = await delete_inventory_embed(interaction.user.id, name.lower().strip())
        if deleted:
            await interaction.response.send_message(
                embed=self.ef.success(f"Removed `{name}` from your inventory."), ephemeral=False
            )
        else:
            await interaction.response.send_message(
                embed=self.ef.error(f"No embed named `{name}` found in your inventory."), ephemeral=False
            )

    autorole_group = app_commands.Group(
        name="autorole",
        description="Configure auto-role on join.",
        default_permissions=discord.Permissions(manage_guild=True),
    )

    @autorole_group.command(name="set", description="Set the role auto-assigned to new members.")
    @app_commands.describe(role="Role to auto-assign.")
    async def autorole_set(self, interaction: discord.Interaction, role: discord.Role):
        assert interaction.guild
        await upsert_guild_config(interaction.guild.id, autorole_id=role.id, autorole_enabled=1)
        await interaction.response.send_message(
            embed=self.ef.success(f"Auto-role set to {role.mention}."), ephemeral=False
        )

    @autorole_group.command(name="toggle", description="Enable or disable auto-role.")
    async def autorole_toggle(self, interaction: discord.Interaction):
        assert interaction.guild
        config = await get_guild_config(interaction.guild.id)
        new = 0 if config.get("autorole_enabled") else 1
        await upsert_guild_config(interaction.guild.id, autorole_enabled=new)
        await interaction.response.send_message(
            embed=self.ef.success(f"Auto-role {self.ef.e['enabled'] if new else self.ef.e['disabled']} **{'enabled' if new else 'disabled'}**."), ephemeral=False
        )

    @app_commands.command(name="modlog", description="Set the channel for moderation logs.")
    @app_commands.describe(channel="Channel for mod logs.")
    @app_commands.default_permissions(manage_guild=True)
    async def modlog(self, interaction: discord.Interaction, channel: discord.TextChannel):
        assert interaction.guild
        await upsert_guild_config(interaction.guild.id, mod_log_channel=channel.id)
        await interaction.response.send_message(
            embed=self.ef.success(f"Mod-log channel set to {channel.mention}."), ephemeral=False
        )

    @app_commands.command(name="settings", description="Interactive settings dashboard.")
    @app_commands.default_permissions(manage_guild=True)
    async def settings(self, interaction: discord.Interaction):
        assert interaction.guild
        await interaction.response.defer(ephemeral=False)
        config = await get_guild_config(interaction.guild.id)
        view = SettingsView(config, self.ef)
        await interaction.followup.send(embed=view.page("greet"), view=view, ephemeral=False)

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        embed = self.ef.error(str(error))
        if not interaction.response.is_done():
            await interaction.response.send_message(embed=embed, ephemeral=False)
        else:
            await interaction.followup.send(embed=embed, ephemeral=False)


async def setup(bot: commands.Bot):
    await bot.add_cog(Customization(bot))
