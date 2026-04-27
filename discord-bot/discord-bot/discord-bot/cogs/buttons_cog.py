"""
Buttons cog — create Linked (URL) and Functional buttons that can be
attached to autoresponders via {button:name}.  Same inventory system as embeds.
"""

import discord
from discord import app_commands
from discord.ext import commands
import json
from typing import Optional

from utils.embeds import EmbedFactory
from utils.database import (
    save_button, get_button, get_all_buttons, delete_button, count_buttons,
    save_button_inventory, get_button_inventory, get_all_button_inventory,
    delete_button_inventory, get_premium_tier, get_server_premium_tier,
)

FREE_BTN_LIMIT = 0

STYLE_MAP = {
    "primary":   discord.ButtonStyle.primary,
    "secondary": discord.ButtonStyle.secondary,
    "success":   discord.ButtonStyle.success,
    "danger":    discord.ButtonStyle.danger,
}


def _tier_btn_limit(config: dict, tier: int) -> int:
    if tier == 0:
        return FREE_BTN_LIMIT
    tiers = config.get("premium", {}).get("tiers", {})
    return tiers.get(str(tier), {}).get("buttons", 0)


# ─────────────────────────────────────────────────────────────────────────────
# Modals
# ─────────────────────────────────────────────────────────────────────────────

class FunctionalResponseModal(discord.ui.Modal, title="Functional Button Response"):
    response_input = discord.ui.TextInput(
        label="Response / Actions",
        style=discord.TextStyle.paragraph,
        placeholder=(
            "Text, {embed:name}, {addrole:@role}, {dm}, {delete}, {reply}, {react:🎉} …"
        ),
        max_length=2000,
        required=True,
    )

    def __init__(self, *, guild_id: int, name: str, label: str, style: str,
                 emoji: Optional[str], existing_response: Optional[str] = None,
                 created_by: int):
        super().__init__()
        self.guild_id    = guild_id
        self.name        = name
        self.label_text  = label
        self.style       = style
        self.emoji_str   = emoji
        self.created_by  = created_by
        if existing_response:
            self.response_input.default = existing_response

    async def on_submit(self, interaction: discord.Interaction) -> None:
        response = self.response_input.value.strip()
        await save_button(
            guild_id=self.guild_id,
            name=self.name,
            btn_type="functional",
            label=self.label_text,
            style=self.style,
            created_by=self.created_by,
            emoji=self.emoji_str,
            response=response,
        )
        await interaction.response.send_message(
            embed=_ef().success(
                f"Functional button `{self.name}` saved!\n"
                f"Use `{{button:{self.name}}}` in any autoresponder response."
            ),
            ephemeral=False,
        )


def _ef() -> EmbedFactory:
    import json as _j, pathlib as _p
    cfg = _j.loads(_p.Path("config.json").read_text())
    return EmbedFactory(cfg)


# ─────────────────────────────────────────────────────────────────────────────
# Cog
# ─────────────────────────────────────────────────────────────────────────────

class ButtonsCog(commands.Cog, name="Buttons"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.ef: EmbedFactory = bot.ef  # type: ignore[attr-defined]
        self.config: dict = bot.config  # type: ignore[attr-defined]

    btn_group = app_commands.Group(
        name="button",
        description="Manage server buttons for autoresponders.",
        default_permissions=discord.Permissions(manage_guild=True),
    )

    # ── create sub-group ──────────────────────────────────────────────────────

    create_group = app_commands.Group(
        name="create",
        description="Create a new button.",
        parent=btn_group,
    )

    @create_group.command(name="linked", description="Create a button that opens a URL.")
    @app_commands.describe(
        name="Internal name (used in {button:name}).",
        label="Button label shown to users.",
        url="The URL to open when clicked.",
        emoji="Optional emoji for the button.",
    )
    async def create_linked(
        self, interaction: discord.Interaction,
        name: str, label: str, url: str,
        emoji: Optional[str] = None,
    ) -> None:
        assert interaction.guild and isinstance(interaction.user, discord.Member)
        tier  = await get_server_premium_tier(interaction.guild.id)
        count = await count_buttons(interaction.guild.id)
        limit = _tier_btn_limit(self.config, tier)
        if limit != -1 and count >= limit:
            return await interaction.response.send_message(
                embed=self.ef.error(
                    f"Button limit reached (`{count}/{limit}`).\n"
                    f"A premium member can activate higher limits with `/activate premium`."
                ),
                ephemeral=False,
            )
        await save_button(
            guild_id=interaction.guild.id,
            name=name.lower(),
            btn_type="linked",
            label=label,
            style="primary",
            created_by=interaction.user.id,
            emoji=emoji,
            url=url,
        )
        await interaction.response.send_message(
            embed=self.ef.success(
                f"Linked button `{name}` saved!\n"
                f"Use `{{button:{name.lower()}}}` in any autoresponder response."
            ),
            ephemeral=False,
        )

    @create_group.command(name="functional", description="Create a button that runs actions when clicked.")
    @app_commands.describe(
        name="Internal name (used in {button:name}).",
        label="Button label shown to users.",
        style="Button colour style.",
        emoji="Optional emoji for the button.",
    )
    @app_commands.choices(style=[
        app_commands.Choice(name="Primary (blurple)", value="primary"),
        app_commands.Choice(name="Secondary (grey)",  value="secondary"),
        app_commands.Choice(name="Success (green)",   value="success"),
        app_commands.Choice(name="Danger (red)",      value="danger"),
    ])
    async def create_functional(
        self, interaction: discord.Interaction,
        name: str, label: str,
        style: str = "primary",
        emoji: Optional[str] = None,
    ) -> None:
        assert interaction.guild and isinstance(interaction.user, discord.Member)
        tier  = await get_server_premium_tier(interaction.guild.id)
        count = await count_buttons(interaction.guild.id)
        limit = _tier_btn_limit(self.config, tier)
        if limit != -1 and count >= limit:
            return await interaction.response.send_message(
                embed=self.ef.error(
                    f"Button limit reached (`{count}/{limit}`).\n"
                    f"A premium member can activate higher limits with `/activate premium`."
                ),
                ephemeral=False,
            )
        modal = FunctionalResponseModal(
            guild_id=interaction.guild.id,
            name=name.lower(),
            label=label,
            style=style,
            emoji=emoji,
            created_by=interaction.user.id,
        )
        await interaction.response.send_modal(modal)

    # ── /button edit ──────────────────────────────────────────────────────────

    @btn_group.command(name="edit", description="Edit an existing button.")
    @app_commands.describe(name="Button name to edit.", label="New label (leave blank to keep).",
                           url="New URL (linked buttons only).", emoji="New emoji.")
    async def btn_edit(
        self, interaction: discord.Interaction,
        name: str,
        label: Optional[str] = None,
        url: Optional[str] = None,
        emoji: Optional[str] = None,
    ) -> None:
        assert interaction.guild and isinstance(interaction.user, discord.Member)
        btn = await get_button(interaction.guild.id, name)
        if not btn:
            return await interaction.response.send_message(
                embed=self.ef.error(f"No button named `{name}`."), ephemeral=False
            )
        if btn["btn_type"] == "functional":
            modal = FunctionalResponseModal(
                guild_id=interaction.guild.id,
                name=name.lower(),
                label=label or btn["label"],
                style=btn.get("style", "primary"),
                emoji=emoji or btn.get("emoji"),
                existing_response=btn.get("response"),
                created_by=interaction.user.id,
            )
            return await interaction.response.send_modal(modal)
        # Linked — update directly
        await save_button(
            guild_id=interaction.guild.id,
            name=name.lower(),
            btn_type="linked",
            label=label or btn["label"],
            style=btn.get("style", "primary"),
            created_by=interaction.user.id,
            emoji=emoji or btn.get("emoji"),
            url=url or btn.get("url"),
        )
        await interaction.response.send_message(
            embed=self.ef.success(f"Button `{name}` updated."), ephemeral=False
        )

    # ── /button delete ────────────────────────────────────────────────────────

    @btn_group.command(name="delete", description="Delete a button.")
    @app_commands.describe(name="Button name to delete.")
    async def btn_delete(self, interaction: discord.Interaction, name: str) -> None:
        assert interaction.guild
        deleted = await delete_button(interaction.guild.id, name)
        if deleted:
            await interaction.response.send_message(
                embed=self.ef.success(f"Button `{name}` deleted."), ephemeral=False
            )
        else:
            await interaction.response.send_message(
                embed=self.ef.error(f"No button named `{name}`."), ephemeral=False
            )

    # ── /button list ──────────────────────────────────────────────────────────

    @btn_group.command(name="list", description="List all buttons in this server.")
    async def btn_list(self, interaction: discord.Interaction) -> None:
        assert interaction.guild
        btns  = await get_all_buttons(interaction.guild.id)
        tier  = await get_server_premium_tier(interaction.guild.id)
        limit = _tier_btn_limit(self.config, tier)
        lbl   = "unlimited" if limit == -1 else str(limit)
        if not btns:
            return await interaction.response.send_message(
                embed=self.ef.info(f"No buttons created yet (`0/{lbl}`)."), ephemeral=False
            )
        e = self.ef.build(author_name=f"Server Buttons  ({len(btns)}/{lbl})", color_key="accent")
        for b in btns[:25]:
            kind = "🔗 Linked" if b["btn_type"] == "linked" else "⚡ Functional"
            detail = b.get("url") or "(action response)"
            e.add_field(name=f"`{b['name']}`  ·  {kind}", value=f"Label: **{b['label']}**\n{detail}", inline=False)
        await interaction.response.send_message(embed=e, ephemeral=False)

    # ── Inventory sub-group ───────────────────────────────────────────────────

    inv_group = app_commands.Group(
        name="inventory",
        description="Your personal button inventory.",
        parent=btn_group,
    )

    @inv_group.command(name="save", description="Save a button to your personal inventory.")
    @app_commands.describe(name="Button name to save.", inventory_name="Name for this inventory entry.")
    async def inv_save(self, interaction: discord.Interaction, name: str, inventory_name: str) -> None:
        assert interaction.guild
        tier = await get_premium_tier(interaction.user.id)
        if tier < 1:
            return await interaction.response.send_message(
                embed=self.ef.error("Button inventory is a premium feature."), ephemeral=False
            )
        btn = await get_button(interaction.guild.id, name)
        if not btn:
            return await interaction.response.send_message(
                embed=self.ef.error(f"No button named `{name}`."), ephemeral=False
            )
        data = dict(btn)
        await save_button_inventory(interaction.user.id, inventory_name, data)
        await interaction.response.send_message(
            embed=self.ef.success(f"Button `{name}` saved to inventory as `{inventory_name}`."), ephemeral=False
        )

    @inv_group.command(name="load", description="Load a button from your inventory into this server.")
    @app_commands.describe(inventory_name="Inventory entry name.", new_name="Override the button name.")
    async def inv_load(self, interaction: discord.Interaction,
                       inventory_name: str, new_name: Optional[str] = None) -> None:
        assert interaction.guild and isinstance(interaction.user, discord.Member)
        tier = await get_premium_tier(interaction.user.id)
        data = await get_button_inventory(interaction.user.id, inventory_name)
        if not data:
            return await interaction.response.send_message(
                embed=self.ef.error(f"No inventory entry named `{inventory_name}`."), ephemeral=False
            )
        count = await count_buttons(interaction.guild.id)
        limit = _tier_btn_limit(self.config, tier)
        if limit != -1 and count >= limit:
            return await interaction.response.send_message(
                embed=self.ef.error("Server button limit reached."), ephemeral=False
            )
        target_name = (new_name or data.get("name") or inventory_name).lower()
        await save_button(
            guild_id=interaction.guild.id,
            name=target_name,
            btn_type=data.get("btn_type", "linked"),
            label=data.get("label", target_name),
            style=data.get("style", "primary"),
            created_by=interaction.user.id,
            emoji=data.get("emoji"),
            url=data.get("url"),
            response=data.get("response"),
        )
        await interaction.response.send_message(
            embed=self.ef.success(f"Loaded `{inventory_name}` as button `{target_name}`."), ephemeral=False
        )

    @inv_group.command(name="list", description="View your personal button inventory.")
    async def inv_list(self, interaction: discord.Interaction) -> None:
        tier = await get_premium_tier(interaction.user.id)
        if tier < 1:
            return await interaction.response.send_message(
                embed=self.ef.error("Button inventory is a premium feature."), ephemeral=False
            )
        entries = await get_all_button_inventory(interaction.user.id)
        if not entries:
            return await interaction.response.send_message(
                embed=self.ef.info("Your button inventory is empty."), ephemeral=False
            )
        e = self.ef.build(author_name=f"Your Button Inventory  ({len(entries)})", color_key="accent")
        for row in entries[:25]:
            e.add_field(name=f"`{row['name']}`", value=f"<t:{row['created_at']}:R>", inline=True)
        await interaction.response.send_message(embed=e, ephemeral=False)

    @inv_group.command(name="delete", description="Delete a button from your inventory.")
    @app_commands.describe(inventory_name="Inventory entry to delete.")
    async def inv_delete(self, interaction: discord.Interaction, inventory_name: str) -> None:
        tier = await get_premium_tier(interaction.user.id)
        if tier < 1:
            return await interaction.response.send_message(
                embed=self.ef.error("Button inventory is a premium feature."), ephemeral=False
            )
        deleted = await delete_button_inventory(interaction.user.id, inventory_name)
        if deleted:
            await interaction.response.send_message(
                embed=self.ef.success(f"Removed `{inventory_name}` from your inventory."), ephemeral=False
            )
        else:
            await interaction.response.send_message(
                embed=self.ef.error(f"No entry named `{inventory_name}` in your inventory."), ephemeral=False
            )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ButtonsCog(bot))
