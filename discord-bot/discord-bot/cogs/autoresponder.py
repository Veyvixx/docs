"""
Autoresponder cog — mimu-style trigger/response system with variables,
embeds, buttons, DM, roles, reactions, cooldowns, and a personal inventory.
"""

import discord
from discord import app_commands
from discord.ext import commands
import json
import logging
import random
import re
import traceback
from typing import Optional

log = logging.getLogger("bot")

from utils.embeds import EmbedFactory
from utils.helpers import format_variables, extract_all_flags, _resolve_role
from utils.database import (
    create_autoresponder, get_autoresponder, get_autoresponder_by_id,
    get_all_autoresponders, update_autoresponder, delete_autoresponder,
    count_autoresponders, increment_ar_use_count, check_ar_cooldown,
    save_ar_inventory, get_ar_inventory, get_all_ar_inventory,
    delete_ar_inventory, count_ar_inventory,
    get_embed, get_button, get_premium_tier, get_server_premium_tier,
)

FREE_AR_LIMIT = 3


def _tier_ar_limit(config: dict, tier: int) -> int:
    if tier == 0:
        return FREE_AR_LIMIT
    tiers = config.get("premium", {}).get("tiers", {})
    limit = tiers.get(str(tier), {}).get("autoresponders", FREE_AR_LIMIT)
    return limit


def _tier_ar_inv_limit(config: dict, tier: int) -> int:
    if tier == 0:
        return 0
    tiers = config.get("premium", {}).get("tiers", {})
    return tiers.get(str(tier), {}).get("ar_inventory", 0)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers for building and sending a response
# ─────────────────────────────────────────────────────────────────────────────

async def _build_ar_view(
    guild: discord.Guild, button_names: list[str]
) -> Optional[discord.ui.View]:
    """Build a View from named buttons stored in the guild's button table."""
    if not button_names:
        return None
    view = discord.ui.View(timeout=None)
    for name in button_names:
        btn_data = await get_button(guild.id, name)
        if not btn_data:
            continue
        style_map = {
            "primary":   discord.ButtonStyle.primary,
            "secondary": discord.ButtonStyle.secondary,
            "success":   discord.ButtonStyle.success,
            "danger":    discord.ButtonStyle.danger,
        }
        if btn_data["btn_type"] == "linked":
            btn = discord.ui.Button(
                label=btn_data["label"],
                url=btn_data["url"] or "https://discord.com",
                emoji=btn_data.get("emoji") or None,
            )
        else:
            style = style_map.get(btn_data.get("style", "primary"), discord.ButtonStyle.primary)
            btn = FunctionalButton(
                label=btn_data["label"],
                style=style,
                emoji=btn_data.get("emoji") or None,
                response=btn_data.get("response") or "",
                guild=guild,
            )
        view.add_item(btn)
    return view if view.children else None


async def fire_response(
    response_template: str,
    message: discord.Message,
    member: discord.Member,
    guild: discord.Guild,
    ar_id: Optional[int] = None,
) -> None:
    """
    Parse and execute a full autoresponder/functional-button response.
    Handles all flag variables, guards, role actions, reactions, DMs, etc.
    """
    clean, flags = extract_all_flags(response_template)

    # ── Guards ────────────────────────────────────────────────────────────────

    # {chance:N}
    if flags["chance"] < 100 and random.randint(1, 100) > flags["chance"]:
        return

    # {require:@role}
    for role_ref in flags["require"]:
        role = _resolve_role(guild, role_ref)
        if role and role not in member.roles:
            return

    # {cooldown:N}
    if ar_id is not None and flags["cooldown"] > 0:
        on_cd = await check_ar_cooldown(guild.id, ar_id, member.id, flags["cooldown"])
        if on_cd:
            return

    # ── Resolve embed ─────────────────────────────────────────────────────────
    from cogs.customization import _data_to_embed  # avoid circular at module level
    embed: Optional[discord.Embed] = None
    if flags["embed"]:
        data = await get_embed(guild.id, flags["embed"])
        if data:
            embed = _data_to_embed(data, member=member, guild=guild)

    # ── Resolve content ───────────────────────────────────────────────────────
    content = format_variables(clean, member=member, guild=guild,
                               channel=message.channel if isinstance(message.channel, discord.abc.GuildChannel) else None)
    content = content.strip() or None

    # ── Build view ────────────────────────────────────────────────────────────
    view = await _build_ar_view(guild, flags["buttons"])

    # ── Resolve target channel ────────────────────────────────────────────────
    target_ch: discord.TextChannel = message.channel  # type: ignore[assignment]
    if flags["channel"]:
        raw = flags["channel"].strip("<#>")
        try:
            ch = guild.get_channel(int(raw))
            if isinstance(ch, discord.TextChannel):
                target_ch = ch
        except ValueError:
            pass

    # ── Send ──────────────────────────────────────────────────────────────────
    sent: Optional[discord.Message] = None
    kwargs: dict = {}
    if content:
        kwargs["content"] = content
    if embed:
        kwargs["embed"] = embed
    if view:
        kwargs["view"] = view

    if not kwargs:
        return

    try:
        if flags["dm"]:
            try:
                await member.send(**kwargs)
            except (discord.Forbidden, discord.HTTPException):
                pass
        elif flags["reply"]:
            sent = await message.reply(**kwargs)
        else:
            sent = await target_ch.send(**kwargs)
    except (discord.Forbidden, discord.HTTPException):
        return

    # ── Post-send actions ─────────────────────────────────────────────────────
    if sent and flags["pin"]:
        try:
            await sent.pin()
        except (discord.Forbidden, discord.HTTPException):
            pass

    for emoji in flags["react"]:
        try:
            await message.add_reaction(emoji)
        except (discord.Forbidden, discord.HTTPException):
            pass

    for role_ref in flags["addrole"]:
        role = _resolve_role(guild, role_ref)
        if role:
            try:
                await member.add_roles(role, reason="Autoresponder {addrole}")
            except (discord.Forbidden, discord.HTTPException):
                pass

    for role_ref in flags["removerole"]:
        role = _resolve_role(guild, role_ref)
        if role:
            try:
                await member.remove_roles(role, reason="Autoresponder {removerole}")
            except (discord.Forbidden, discord.HTTPException):
                pass

    if flags["delete"]:
        try:
            await message.delete()
        except (discord.Forbidden, discord.HTTPException):
            pass

    if ar_id is not None:
        await increment_ar_use_count(ar_id)


# ─────────────────────────────────────────────────────────────────────────────
# Functional button  (persists only while bot is up; interaction-based)
# ─────────────────────────────────────────────────────────────────────────────

class FunctionalButton(discord.ui.Button):
    def __init__(self, *, label: str, style: discord.ButtonStyle,
                 emoji: Optional[str], response: str, guild: discord.Guild):
        super().__init__(label=label, style=style, emoji=emoji)
        self.response_template = response
        self.guild = guild

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        if not isinstance(interaction.user, discord.Member):
            return
        # For button clicks we pass a fake "message" from the interaction's message
        msg = interaction.message
        if msg is None:
            return
        await fire_response(
            self.response_template, msg,
            interaction.user, self.guild,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Modals
# ─────────────────────────────────────────────────────────────────────────────

class ResponseModal(discord.ui.Modal, title="Autoresponder Response"):
    response_input = discord.ui.TextInput(
        label="Response (text, or use action flags)",
        style=discord.TextStyle.paragraph,
        placeholder="hi {user}! welcome ♡   |   flags: {embed:name} {dm} {reply} {react:🎉} {cooldown:30}",
        max_length=2000,
        required=True,
    )

    def __init__(self, *, trigger: str, match_type: str, case_sensitive: bool,
                 cooldown: int, existing: Optional[str] = None,
                 ar_id: Optional[str] = None):
        super().__init__()
        self.trigger        = trigger
        self.match_type     = match_type
        self.case_sensitive = case_sensitive
        self.cooldown       = cooldown
        self.ar_id          = ar_id
        if existing:
            self.response_input.default = existing

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            assert interaction.guild and isinstance(interaction.user, discord.Member)
            response = self.response_input.value.strip()

            if self.ar_id is not None:
                await update_autoresponder(self.ar_id, response=response,
                                           match_type=self.match_type,
                                           case_sensitive=int(self.case_sensitive),
                                           cooldown=self.cooldown)
                await interaction.response.send_message(
                    embed=_ef().success(f"Autoresponder for `{self.trigger}` updated."),
                    ephemeral=False,
                )
            else:
                await create_autoresponder(
                    guild_id=interaction.guild.id,
                    trigger=self.trigger,
                    match_type=self.match_type,
                    case_sensitive=self.case_sensitive,
                    response=response,
                    cooldown=self.cooldown,
                    created_by=interaction.user.id,
                )
                await interaction.response.send_message(
                    embed=_ef().success(
                        f"Autoresponder created!\n"
                        f"**Trigger:** `{self.trigger}`  **Match:** `{self.match_type}`\n"
                        f"**Response:** {response[:200]}{'…' if len(response) > 200 else ''}"
                    ),
                    ephemeral=False,
                )
        except Exception as exc:
            log.exception("ResponseModal.on_submit failed for trigger=%r ar_id=%r",
                          self.trigger, self.ar_id)
            err_text = f"Failed to save autoresponder: `{type(exc).__name__}: {exc}`"
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(
                        embed=_ef().error(err_text), ephemeral=True
                    )
                else:
                    await interaction.response.send_message(
                        embed=_ef().error(err_text), ephemeral=True
                    )
            except Exception:
                log.exception("Could not even send the error message to the user")

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.error("ResponseModal on_error: %s\n%s", error, traceback.format_exc())
        try:
            msg = f"Modal error: `{type(error).__name__}: {error}`"
            if interaction.response.is_done():
                await interaction.followup.send(embed=_ef().error(msg), ephemeral=True)
            else:
                await interaction.response.send_message(
                    embed=_ef().error(msg), ephemeral=True
                )
        except Exception:
            log.exception("Could not surface modal error to user")


def _ef() -> EmbedFactory:
    import json as _json, pathlib as _p
    cfg = _json.loads(_p.Path("config.json").read_text())
    return EmbedFactory(cfg)


# ─────────────────────────────────────────────────────────────────────────────
# Cog
# ─────────────────────────────────────────────────────────────────────────────

class Autoresponder(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.ef: EmbedFactory = bot.ef  # type: ignore[attr-defined]
        self.config: dict = bot.config  # type: ignore[attr-defined]

    # ── on_message listener ───────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or not message.guild:
            return
        if not isinstance(message.author, discord.Member):
            return

        ars = await get_all_autoresponders(message.guild.id)
        content = message.content

        for ar in ars:
            if not ar["enabled"]:
                continue

            trigger    = ar["trigger"]
            match_type = ar["match_type"]
            cs         = bool(ar["case_sensitive"])

            cmp_content = content if cs else content.lower()
            cmp_trigger = trigger if cs else trigger.lower()

            matched = False
            if match_type == "exact":
                matched = cmp_content == cmp_trigger
            elif match_type == "contains":
                matched = cmp_trigger in cmp_content
            elif match_type == "starts":
                matched = cmp_content.startswith(cmp_trigger)
            elif match_type == "ends":
                matched = cmp_content.endswith(cmp_trigger)
            elif match_type == "regex":
                try:
                    matched = bool(re.search(trigger, content, 0 if cs else re.IGNORECASE))
                except re.error:
                    matched = False

            if matched:
                await fire_response(
                    ar["response"], message,
                    message.author, message.guild,
                    ar_id=ar["id"],
                )
                break  # first match wins

    # ── Slash command group ───────────────────────────────────────────────────

    ar_group = app_commands.Group(
        name="autoresponder",
        description="Autoresponder management.",
        default_permissions=discord.Permissions(manage_guild=True),
    )

    # ── /autoresponder create ─────────────────────────────────────────────────

    @ar_group.command(name="create", description="Create a new autoresponder.")
    @app_commands.describe(
        trigger="The trigger phrase or pattern.",
        match="How the trigger is matched (default: exact).",
        case_sensitive="Match case-sensitively (default: off).",
        cooldown="Per-user cooldown in seconds (0 = none).",
    )
    @app_commands.choices(match=[
        app_commands.Choice(name="exact",    value="exact"),
        app_commands.Choice(name="contains", value="contains"),
        app_commands.Choice(name="starts with", value="starts"),
        app_commands.Choice(name="ends with",   value="ends"),
        app_commands.Choice(name="regex",    value="regex"),
    ])
    async def ar_create(
        self, interaction: discord.Interaction,
        trigger: str,
        match: str = "exact",
        case_sensitive: bool = False,
        cooldown: int = 0,
    ) -> None:
        assert interaction.guild and isinstance(interaction.user, discord.Member)
        tier  = await get_server_premium_tier(interaction.guild.id)
        count = await count_autoresponders(interaction.guild.id)
        limit = _tier_ar_limit(self.config, tier)
        if limit != -1 and count >= limit:
            lbl = "unlimited" if limit == -1 else str(limit)
            return await interaction.response.send_message(
                embed=self.ef.error(
                    f"This server has reached its autoresponder limit (`{count}/{lbl}`).\n"
                    f"A premium member can activate higher limits with `/activate premium`."
                ),
                ephemeral=False,
            )
        existing = await get_autoresponder(interaction.guild.id, trigger)
        if existing:
            return await interaction.response.send_message(
                embed=self.ef.error(
                    f"An autoresponder for `{trigger}` already exists. "
                    f"Use `/autoresponder edit` to update it."
                ),
                ephemeral=False,
            )
        modal = ResponseModal(trigger=trigger, match_type=match,
                              case_sensitive=case_sensitive, cooldown=cooldown)
        await interaction.response.send_modal(modal)

    # ── /autoresponder edit ───────────────────────────────────────────────────

    @ar_group.command(name="edit", description="Edit an existing autoresponder's response.")
    @app_commands.describe(
        trigger="The trigger of the autoresponder to edit.",
        match="Change the match type.",
        case_sensitive="Change case sensitivity.",
        cooldown="Change per-user cooldown (seconds).",
    )
    @app_commands.choices(match=[
        app_commands.Choice(name="exact",       value="exact"),
        app_commands.Choice(name="contains",    value="contains"),
        app_commands.Choice(name="starts with", value="starts"),
        app_commands.Choice(name="ends with",   value="ends"),
        app_commands.Choice(name="regex",       value="regex"),
    ])
    async def ar_edit(
        self, interaction: discord.Interaction,
        trigger: str,
        match: Optional[str] = None,
        case_sensitive: Optional[bool] = None,
        cooldown: Optional[int] = None,
    ) -> None:
        assert interaction.guild
        ar = await get_autoresponder(interaction.guild.id, trigger)
        if not ar:
            return await interaction.response.send_message(
                embed=self.ef.error(f"No autoresponder found for trigger `{trigger}`."),
                ephemeral=False,
            )
        modal = ResponseModal(
            trigger=trigger,
            match_type=match or ar["match_type"],
            case_sensitive=case_sensitive if case_sensitive is not None else bool(ar["case_sensitive"]),
            cooldown=cooldown if cooldown is not None else ar["cooldown"],
            existing=ar["response"],
            ar_id=ar["id"],
        )
        await interaction.response.send_modal(modal)

    # ── /autoresponder delete ─────────────────────────────────────────────────

    @ar_group.command(name="delete", description="Delete an autoresponder.")
    @app_commands.describe(trigger="The trigger of the autoresponder to delete.")
    async def ar_delete(self, interaction: discord.Interaction, trigger: str) -> None:
        assert interaction.guild
        deleted = await delete_autoresponder(interaction.guild.id, trigger)
        if deleted:
            await interaction.response.send_message(
                embed=self.ef.success(f"Autoresponder for `{trigger}` deleted."),
                ephemeral=False,
            )
        else:
            await interaction.response.send_message(
                embed=self.ef.error(f"No autoresponder found for `{trigger}`."),
                ephemeral=False,
            )

    # ── /autoresponder list ───────────────────────────────────────────────────

    @ar_group.command(name="list", description="List all autoresponders in this server.")
    async def ar_list(self, interaction: discord.Interaction) -> None:
        assert interaction.guild
        ars = await get_all_autoresponders(interaction.guild.id)
        tier  = await get_server_premium_tier(interaction.guild.id)
        limit = _tier_ar_limit(self.config, tier)
        lbl   = "unlimited" if limit == -1 else str(limit)
        if not ars:
            return await interaction.response.send_message(
                embed=self.ef.info(f"No autoresponders set up yet (`0/{lbl}`)."),
                ephemeral=False,
            )
        e = self.ef.build(
            author_name=f"{interaction.guild.name}  ·  Autoresponders  ({len(ars)}/{lbl})",
            color_key="accent",
        )
        for ar in ars[:25]:
            status = "✅" if ar["enabled"] else "❌"
            cd     = f"  `cd:{ar['cooldown']}s`" if ar["cooldown"] else ""
            e.add_field(
                name  = f"{status} `{ar['trigger']}`",
                value = f"Match: `{ar['match_type']}`{cd}  ·  used {ar['use_count']}×",
                inline = False,
            )
        await interaction.response.send_message(embed=e, ephemeral=False)

    # ── /autoresponder enable / disable ───────────────────────────────────────

    @ar_group.command(name="enable", description="Enable an autoresponder.")
    @app_commands.describe(trigger="Trigger of the autoresponder to enable.")
    async def ar_enable(self, interaction: discord.Interaction, trigger: str) -> None:
        assert interaction.guild
        ar = await get_autoresponder(interaction.guild.id, trigger)
        if not ar:
            return await interaction.response.send_message(
                embed=self.ef.error(f"No autoresponder found for `{trigger}`."), ephemeral=False
            )
        await update_autoresponder(ar["id"], enabled=1)
        await interaction.response.send_message(
            embed=self.ef.success(f"Autoresponder `{trigger}` enabled."), ephemeral=False
        )

    @ar_group.command(name="disable", description="Disable an autoresponder without deleting it.")
    @app_commands.describe(trigger="Trigger of the autoresponder to disable.")
    async def ar_disable(self, interaction: discord.Interaction, trigger: str) -> None:
        assert interaction.guild
        ar = await get_autoresponder(interaction.guild.id, trigger)
        if not ar:
            return await interaction.response.send_message(
                embed=self.ef.error(f"No autoresponder found for `{trigger}`."), ephemeral=False
            )
        await update_autoresponder(ar["id"], enabled=0)
        await interaction.response.send_message(
            embed=self.ef.success(f"Autoresponder `{trigger}` disabled."), ephemeral=False
        )

    # ── /autoresponder view ───────────────────────────────────────────────────

    @ar_group.command(name="view", description="View a single autoresponder's details.")
    @app_commands.describe(trigger="Trigger to inspect.")
    async def ar_view(self, interaction: discord.Interaction, trigger: str) -> None:
        assert interaction.guild
        ar = await get_autoresponder(interaction.guild.id, trigger)
        if not ar:
            return await interaction.response.send_message(
                embed=self.ef.error(f"No autoresponder found for `{trigger}`."), ephemeral=False
            )
        e = self.ef.build(author_name=f"Autoresponder · {ar['trigger']}", color_key="accent")
        e.add_field(name="Match Type",      value=f"`{ar['match_type']}`",         inline=True)
        e.add_field(name="Case Sensitive",  value=str(bool(ar["case_sensitive"])), inline=True)
        e.add_field(name="Cooldown",        value=f"{ar['cooldown']}s",            inline=True)
        e.add_field(name="Status",          value="Enabled" if ar["enabled"] else "Disabled", inline=True)
        e.add_field(name="Use Count",       value=str(ar["use_count"]),            inline=True)
        resp = ar["response"]
        e.add_field(name="Response", value=f"```{resp[:500]}{'...' if len(resp)>500 else ''}```", inline=False)
        await interaction.response.send_message(embed=e, ephemeral=False)

    # ── Inventory sub-group ───────────────────────────────────────────────────

    inv_group = app_commands.Group(
        name="inventory",
        description="Your personal autoresponder inventory.",
        parent=ar_group,
    )

    @inv_group.command(name="save", description="Save an autoresponder to your personal inventory.")
    @app_commands.describe(
        trigger="Trigger of the autoresponder to save.",
        inventory_name="Name for this inventory entry.",
    )
    async def inv_save(self, interaction: discord.Interaction,
                       trigger: str, inventory_name: str) -> None:
        assert interaction.guild
        tier = await get_premium_tier(interaction.user.id)
        if tier < 1:
            return await interaction.response.send_message(
                embed=self.ef.error("Autoresponder inventory is a premium feature. Check `/premium`."),
                ephemeral=False,
            )
        ar = await get_autoresponder(interaction.guild.id, trigger)
        if not ar:
            return await interaction.response.send_message(
                embed=self.ef.error(f"No autoresponder found for `{trigger}`."), ephemeral=False
            )
        inv_limit = _tier_ar_inv_limit(self.config, tier)
        inv_count = await count_ar_inventory(interaction.user.id)
        if inv_limit != -1 and inv_count >= inv_limit:
            return await interaction.response.send_message(
                embed=self.ef.error(f"Inventory full (`{inv_count}/{inv_limit}`)."), ephemeral=False
            )
        await save_ar_inventory(
            interaction.user.id, inventory_name,
            ar["trigger"], ar["match_type"], ar["response"],
        )
        await interaction.response.send_message(
            embed=self.ef.success(f"Saved to inventory as `{inventory_name}`."), ephemeral=False
        )

    @inv_group.command(name="load", description="Load an autoresponder from your inventory into this server.")
    @app_commands.describe(
        inventory_name="Name of the inventory entry to load.",
        new_trigger="Override the trigger (leave blank to use original).",
    )
    async def inv_load(self, interaction: discord.Interaction,
                       inventory_name: str, new_trigger: Optional[str] = None) -> None:
        assert interaction.guild and isinstance(interaction.user, discord.Member)
        tier = await get_premium_tier(interaction.user.id)
        if tier < 1:
            return await interaction.response.send_message(
                embed=self.ef.error("Inventory loading is a premium-member feature."),
                ephemeral=False,
            )
        entry = await get_ar_inventory(interaction.user.id, inventory_name)
        if not entry:
            return await interaction.response.send_message(
                embed=self.ef.error(f"No inventory entry named `{inventory_name}`."), ephemeral=False
            )
        trigger     = new_trigger or entry["trigger"]
        server_tier = await get_server_premium_tier(interaction.guild.id)
        count       = await count_autoresponders(interaction.guild.id)
        limit       = _tier_ar_limit(self.config, server_tier)
        if limit != -1 and count >= limit:
            return await interaction.response.send_message(
                embed=self.ef.error("Server autoresponder limit reached."), ephemeral=False
            )
        existing = await get_autoresponder(interaction.guild.id, trigger)
        if existing:
            return await interaction.response.send_message(
                embed=self.ef.error(f"Trigger `{trigger}` already exists in this server."), ephemeral=False
            )
        await create_autoresponder(
            guild_id=interaction.guild.id,
            trigger=trigger,
            match_type=entry["match_type"],
            case_sensitive=False,
            response=entry["response"],
            cooldown=0,
            created_by=interaction.user.id,
        )
        await interaction.response.send_message(
            embed=self.ef.success(f"Loaded `{inventory_name}` → trigger `{trigger}`."), ephemeral=False
        )

    @inv_group.command(name="list", description="View your personal autoresponder inventory.")
    async def inv_list(self, interaction: discord.Interaction) -> None:
        tier = await get_premium_tier(interaction.user.id)
        if tier < 1:
            return await interaction.response.send_message(
                embed=self.ef.error("Autoresponder inventory is a premium feature. Check `/premium`."),
                ephemeral=False,
            )
        entries = await get_all_ar_inventory(interaction.user.id)
        limit   = _tier_ar_inv_limit(self.config, tier)
        lbl     = "unlimited" if limit == -1 else str(limit)
        if not entries:
            return await interaction.response.send_message(
                embed=self.ef.info(f"Your AR inventory is empty (`0/{lbl}`)."),
                ephemeral=False,
            )
        e = self.ef.build(author_name=f"Your AR Inventory  ({len(entries)}/{lbl})", color_key="accent")
        for row in entries[:25]:
            e.add_field(name=f"`{row['name']}`", value=f"trigger: `{row['trigger']}`  match: `{row['match_type']}`", inline=False)
        await interaction.response.send_message(embed=e, ephemeral=False)

    @inv_group.command(name="delete", description="Delete an entry from your autoresponder inventory.")
    @app_commands.describe(inventory_name="Name of the inventory entry to delete.")
    async def inv_delete(self, interaction: discord.Interaction, inventory_name: str) -> None:
        tier = await get_premium_tier(interaction.user.id)
        if tier < 1:
            return await interaction.response.send_message(
                embed=self.ef.error("Autoresponder inventory is a premium feature."), ephemeral=False
            )
        deleted = await delete_ar_inventory(interaction.user.id, inventory_name)
        if deleted:
            await interaction.response.send_message(
                embed=self.ef.success(f"Removed `{inventory_name}` from your inventory."), ephemeral=False
            )
        else:
            await interaction.response.send_message(
                embed=self.ef.error(f"No inventory entry named `{inventory_name}`."), ephemeral=False
            )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Autoresponder(bot))
