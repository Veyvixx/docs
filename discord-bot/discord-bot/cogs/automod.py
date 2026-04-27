"""
Discord-native AutoMod cog.

Manages real ``discord.AutoModRule`` resources via the Discord API and persists
preset metadata (rule IDs, custom keyword lists, exempt roles/channels, action
configuration) to the ``nana_automod`` DynamoDB table.

Slash group: ``/automod``
    enable <preset>             Create/enable the rule for a preset.
    disable <preset>            Disable but keep the rule (call again to delete).
    list                        Show every preset and its current state.
    whitelist add role/channel  Mark a role/channel exempt from a preset.
    whitelist remove role/channel
    action set                  Block / timeout duration / log channel.
    keyword add/remove/clear    Manage the custom keyword list (preset=keyword).
    mentions <limit>            Set the mention-spam limit (preset=mention_spam).

Presets:
    keyword       — block a custom word list
    spam          — Discord's built-in spam filter
    invite        — regex match on discord.gg / discord.com/invite links
    link          — regex match on any http(s) link
    caps          — regex match on long all-caps runs ([A-Z]{10,})
    mention_spam  — built-in mention-total-limit trigger
"""

from __future__ import annotations

import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from utils.embeds import EmbedFactory
from utils.database import (
    get_automod_config,
    get_all_automod_configs,
    upsert_automod_config,
    delete_automod_config,
)

logger = logging.getLogger(__name__)


PRESETS = ["keyword", "spam", "invite", "link", "caps", "mention_spam"]

PRESET_LABELS = {
    "keyword":      "Custom Keywords",
    "spam":         "Anti-Spam (built-in)",
    "invite":       "Invite Links",
    "link":         "All Links",
    "caps":         "Excessive Caps",
    "mention_spam": "Mention Spam",
}

# Regex / preset definitions per preset
INVITE_REGEX = r"(?:https?:\/\/)?(?:www\.)?(?:discord(?:app)?\.com\/invite|discord\.gg)\/[A-Za-z0-9-]+"
LINK_REGEX   = r"https?:\/\/[^\s]+"
CAPS_REGEX   = r"[A-Z]{10,}"

DEFAULT_MENTION_LIMIT = 5
DEFAULT_TIMEOUT_S     = 300  # 5 minutes


# ──────────────────────────── helpers ────────────────────────────────────

def _build_trigger(preset: str, *, keywords: list[str] | None = None,
                   mention_limit: int = DEFAULT_MENTION_LIMIT) -> Optional[discord.AutoModTrigger]:
    """Compose the ``AutoModTrigger`` for a given preset."""
    if preset == "keyword":
        if not keywords:
            return None
        return discord.AutoModTrigger(
            type=discord.AutoModRuleTriggerType.keyword,
            keyword_filter=keywords[:1000],
        )
    if preset == "spam":
        return discord.AutoModTrigger(type=discord.AutoModRuleTriggerType.spam)
    if preset == "invite":
        return discord.AutoModTrigger(
            type=discord.AutoModRuleTriggerType.keyword,
            regex_patterns=[INVITE_REGEX],
        )
    if preset == "link":
        return discord.AutoModTrigger(
            type=discord.AutoModRuleTriggerType.keyword,
            regex_patterns=[LINK_REGEX],
        )
    if preset == "caps":
        return discord.AutoModTrigger(
            type=discord.AutoModRuleTriggerType.keyword,
            regex_patterns=[CAPS_REGEX],
        )
    if preset == "mention_spam":
        return discord.AutoModTrigger(
            type=discord.AutoModRuleTriggerType.mention_spam,
            mention_limit=mention_limit,
        )
    return None


def _build_actions(*, log_channel_id: Optional[int],
                   timeout_seconds: Optional[int]) -> list[discord.AutoModRuleAction]:
    actions: list[discord.AutoModRuleAction] = [
        discord.AutoModRuleAction(type=discord.AutoModRuleActionType.block_message),
    ]
    if log_channel_id:
        actions.append(discord.AutoModRuleAction(
            type=discord.AutoModRuleActionType.send_alert_message,
            channel_id=int(log_channel_id),
        ))
    if timeout_seconds and timeout_seconds > 0:
        from datetime import timedelta
        actions.append(discord.AutoModRuleAction(
            type=discord.AutoModRuleActionType.timeout,
            duration=timedelta(seconds=int(timeout_seconds)),
        ))
    return actions


async def _fetch_existing_rule(guild: discord.Guild, rule_id: int) -> Optional[discord.AutoModRule]:
    try:
        return await guild.fetch_automod_rule(rule_id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return None


# ──────────────────────────── cog ────────────────────────────────────────


class AutoMod(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.ef: EmbedFactory = bot.ef  # type: ignore[attr-defined]

    automod_group = app_commands.Group(
        name="automod",
        description="Manage Discord-native AutoMod rules (block, log, timeout).",
        default_permissions=discord.Permissions(manage_guild=True),
    )

    # ── helpers ──────────────────────────────────────────────────────────

    async def _ensure_perms(self, interaction: discord.Interaction) -> bool:
        assert interaction.guild
        me = interaction.guild.me
        if me is None or not me.guild_permissions.manage_guild:
            await interaction.response.send_message(
                embed=self.ef.error("I need the **Manage Server** permission to manage AutoMod rules."),
                ephemeral=False,
            )
            return False
        return True

    async def _sync_rule(
        self,
        guild: discord.Guild,
        preset: str,
        *,
        cfg: Optional[dict] = None,
    ) -> tuple[Optional[discord.AutoModRule], Optional[str]]:
        """Create or update the AutoMod rule for ``preset`` based on the saved cfg.

        Returns (rule, error_message). On success error_message is None.
        """
        cfg = cfg or await get_automod_config(guild.id, preset) or {}
        keywords      = list(cfg.get("keywords") or [])
        mention_limit = int(cfg.get("mention_limit") or DEFAULT_MENTION_LIMIT)
        timeout_secs  = int(cfg.get("timeout_seconds") or 0)
        log_ch_id     = cfg.get("log_channel_id")
        exempt_roles  = [int(r) for r in (cfg.get("exempt_roles") or [])]
        exempt_chans  = [int(c) for c in (cfg.get("exempt_channels") or [])]

        if preset == "keyword" and not keywords:
            return None, "Add at least one keyword first with `/automod keyword add`."

        trigger = _build_trigger(preset, keywords=keywords, mention_limit=mention_limit)
        if trigger is None:
            return None, f"Unknown preset `{preset}`."
        actions = _build_actions(
            log_channel_id=log_ch_id,
            timeout_seconds=timeout_secs,
        )

        existing_id = cfg.get("rule_id")
        rule = None
        if existing_id:
            rule = await _fetch_existing_rule(guild, int(existing_id))

        kwargs: dict = dict(
            name=f"Nana - {PRESET_LABELS[preset]}",
            event_type=discord.AutoModRuleEventType.message_send,
            trigger=trigger,
            actions=actions,
            enabled=True,
            exempt_roles=[discord.Object(id=r) for r in exempt_roles],
            exempt_channels=[discord.Object(id=c) for c in exempt_chans],
            reason="Nana /automod",
        )

        try:
            if rule is None:
                rule = await guild.create_automod_rule(**kwargs)
            else:
                rule = await rule.edit(**kwargs)
        except discord.Forbidden:
            return None, "I don't have permission to manage AutoMod rules."
        except discord.HTTPException as exc:
            return None, f"Discord API error: {exc}"

        await upsert_automod_config(
            guild.id, preset,
            rule_id=rule.id,
            enabled=True,
        )
        return rule, None

    # ── /automod enable ──────────────────────────────────────────────────

    @automod_group.command(name="enable", description="Enable an AutoMod preset.")
    @app_commands.describe(preset="Which preset to enable.")
    @app_commands.choices(preset=[app_commands.Choice(name=PRESET_LABELS[p], value=p) for p in PRESETS])
    async def enable(self, interaction: discord.Interaction, preset: app_commands.Choice[str]):
        if not await self._ensure_perms(interaction):
            return
        await interaction.response.defer(ephemeral=False)
        rule, err = await self._sync_rule(interaction.guild, preset.value)  # type: ignore[arg-type]
        if err:
            return await interaction.followup.send(embed=self.ef.error(err))
        await interaction.followup.send(
            embed=self.ef.success(f"AutoMod **{PRESET_LABELS[preset.value]}** is now active. (rule `{rule.id}`)")
        )

    # ── /automod disable ─────────────────────────────────────────────────

    @automod_group.command(name="disable", description="Disable a preset (call again to delete the rule).")
    @app_commands.describe(preset="Which preset to disable.")
    @app_commands.choices(preset=[app_commands.Choice(name=PRESET_LABELS[p], value=p) for p in PRESETS])
    async def disable(self, interaction: discord.Interaction, preset: app_commands.Choice[str]):
        if not await self._ensure_perms(interaction):
            return
        await interaction.response.defer(ephemeral=False)
        cfg = await get_automod_config(interaction.guild.id, preset.value) or {}  # type: ignore[union-attr]
        rule_id = cfg.get("rule_id")
        enabled = bool(cfg.get("enabled"))
        if not rule_id:
            return await interaction.followup.send(
                embed=self.ef.info(f"AutoMod **{PRESET_LABELS[preset.value]}** is not configured.")
            )
        rule = await _fetch_existing_rule(interaction.guild, int(rule_id))  # type: ignore[arg-type]
        if not enabled:
            # Already disabled — second call deletes.
            if rule:
                try:
                    await rule.delete(reason="Nana /automod disable (second call)")
                except (discord.Forbidden, discord.HTTPException):
                    pass
            await delete_automod_config(interaction.guild.id, preset.value)  # type: ignore[union-attr]
            return await interaction.followup.send(
                embed=self.ef.success(f"AutoMod **{PRESET_LABELS[preset.value]}** rule deleted.")
            )
        if rule:
            try:
                await rule.edit(enabled=False, reason="Nana /automod disable")
            except (discord.Forbidden, discord.HTTPException) as exc:
                return await interaction.followup.send(embed=self.ef.error(f"Failed: {exc}"))
        await upsert_automod_config(interaction.guild.id, preset.value, enabled=False)  # type: ignore[union-attr]
        await interaction.followup.send(
            embed=self.ef.success(
                f"AutoMod **{PRESET_LABELS[preset.value]}** disabled. "
                f"Run `/automod disable` again to delete the rule entirely."
            )
        )

    # ── /automod list ────────────────────────────────────────────────────

    @automod_group.command(name="list", description="List every Nana AutoMod preset and its state.")
    async def list_(self, interaction: discord.Interaction):
        assert interaction.guild
        await interaction.response.defer(ephemeral=False)
        configs = {c["preset"]: c for c in await get_all_automod_configs(interaction.guild.id)}
        e = self.ef.build(title="AutoMod Rules", color_key="accent")
        for p in PRESETS:
            cfg = configs.get(p)
            if not cfg:
                state = "`not configured`"
            elif int(cfg.get("enabled", 0)):
                state = "🟢 enabled"
            else:
                state = "🟡 disabled"
            extra: list[str] = []
            if cfg:
                if p == "keyword":
                    n = len(cfg.get("keywords") or [])
                    extra.append(f"{n} keyword(s)")
                if p == "mention_spam":
                    extra.append(f"limit {int(cfg.get('mention_limit') or DEFAULT_MENTION_LIMIT)}")
                if cfg.get("timeout_seconds"):
                    extra.append(f"timeout {int(cfg['timeout_seconds'])}s")
                if cfg.get("log_channel_id"):
                    extra.append(f"log <#{int(cfg['log_channel_id'])}>")
                exr = cfg.get("exempt_roles") or []
                exc = cfg.get("exempt_channels") or []
                if exr or exc:
                    extra.append(f"exempt: {len(exr)} role(s), {len(exc)} channel(s)")
            tail = f"  ·  {' · '.join(extra)}" if extra else ""
            e.add_field(name=PRESET_LABELS[p], value=f"{state}{tail}", inline=False)
        await interaction.followup.send(embed=e)

    # ── /automod whitelist add/remove ───────────────────────────────────

    whitelist_group = app_commands.Group(
        parent=automod_group, name="whitelist",
        description="Exempt roles/channels from an AutoMod preset.",
    )

    async def _modify_exempt(
        self,
        interaction: discord.Interaction,
        preset: str,
        kind: str,            # "role" | "channel"
        target_id: int,
        add: bool,
    ) -> None:
        assert interaction.guild
        cfg = await get_automod_config(interaction.guild.id, preset) or {}
        if not cfg.get("rule_id"):
            return await interaction.followup.send(
                embed=self.ef.error(f"`{PRESET_LABELS[preset]}` is not enabled. Run `/automod enable` first.")
            )
        key = "exempt_roles" if kind == "role" else "exempt_channels"
        current = [int(x) for x in (cfg.get(key) or [])]
        if add:
            if target_id in current:
                return await interaction.followup.send(
                    embed=self.ef.info(f"Already exempt.")
                )
            current.append(target_id)
        else:
            if target_id not in current:
                return await interaction.followup.send(
                    embed=self.ef.info(f"Not in the exempt list.")
                )
            current = [x for x in current if x != target_id]
        await upsert_automod_config(interaction.guild.id, preset, **{key: current})
        _, err = await self._sync_rule(interaction.guild, preset)
        if err:
            return await interaction.followup.send(embed=self.ef.error(err))
        verb = "added to" if add else "removed from"
        await interaction.followup.send(
            embed=self.ef.success(f"{kind.title()} `{target_id}` {verb} the exempt list for **{PRESET_LABELS[preset]}**.")
        )

    @whitelist_group.command(name="add_role", description="Exempt a role from a preset.")
    @app_commands.choices(preset=[app_commands.Choice(name=PRESET_LABELS[p], value=p) for p in PRESETS])
    async def whitelist_add_role(
        self, interaction: discord.Interaction,
        preset: app_commands.Choice[str], role: discord.Role,
    ):
        if not await self._ensure_perms(interaction):
            return
        await interaction.response.defer(ephemeral=False)
        await self._modify_exempt(interaction, preset.value, "role", role.id, add=True)

    @whitelist_group.command(name="remove_role", description="Stop exempting a role.")
    @app_commands.choices(preset=[app_commands.Choice(name=PRESET_LABELS[p], value=p) for p in PRESETS])
    async def whitelist_remove_role(
        self, interaction: discord.Interaction,
        preset: app_commands.Choice[str], role: discord.Role,
    ):
        if not await self._ensure_perms(interaction):
            return
        await interaction.response.defer(ephemeral=False)
        await self._modify_exempt(interaction, preset.value, "role", role.id, add=False)

    @whitelist_group.command(name="add_channel", description="Exempt a channel from a preset.")
    @app_commands.choices(preset=[app_commands.Choice(name=PRESET_LABELS[p], value=p) for p in PRESETS])
    async def whitelist_add_channel(
        self, interaction: discord.Interaction,
        preset: app_commands.Choice[str], channel: discord.TextChannel,
    ):
        if not await self._ensure_perms(interaction):
            return
        await interaction.response.defer(ephemeral=False)
        await self._modify_exempt(interaction, preset.value, "channel", channel.id, add=True)

    @whitelist_group.command(name="remove_channel", description="Stop exempting a channel.")
    @app_commands.choices(preset=[app_commands.Choice(name=PRESET_LABELS[p], value=p) for p in PRESETS])
    async def whitelist_remove_channel(
        self, interaction: discord.Interaction,
        preset: app_commands.Choice[str], channel: discord.TextChannel,
    ):
        if not await self._ensure_perms(interaction):
            return
        await interaction.response.defer(ephemeral=False)
        await self._modify_exempt(interaction, preset.value, "channel", channel.id, add=False)

    # ── /automod action set ──────────────────────────────────────────────

    action_group = app_commands.Group(
        parent=automod_group, name="action",
        description="Configure block / timeout / log-channel actions.",
    )

    @action_group.command(name="set", description="Configure actions taken when a preset triggers.")
    @app_commands.describe(
        preset="Which preset to update.",
        timeout_seconds="Auto-timeout offender for N seconds (0 = off).",
        log_channel="Channel to alert when a rule trips (None = clear).",
    )
    @app_commands.choices(preset=[app_commands.Choice(name=PRESET_LABELS[p], value=p) for p in PRESETS])
    async def action_set(
        self,
        interaction: discord.Interaction,
        preset: app_commands.Choice[str],
        timeout_seconds: app_commands.Range[int, 0, 2419200] = 0,
        log_channel: Optional[discord.TextChannel] = None,
    ):
        if not await self._ensure_perms(interaction):
            return
        await interaction.response.defer(ephemeral=False)
        await upsert_automod_config(
            interaction.guild.id, preset.value,  # type: ignore[union-attr]
            timeout_seconds=int(timeout_seconds),
            log_channel_id=int(log_channel.id) if log_channel else 0,
        )
        cfg = await get_automod_config(interaction.guild.id, preset.value) or {}  # type: ignore[union-attr]
        if cfg.get("rule_id"):
            _, err = await self._sync_rule(interaction.guild, preset.value)  # type: ignore[union-attr]
            if err:
                return await interaction.followup.send(embed=self.ef.error(err))
        bits = [f"timeout = `{timeout_seconds}s`"]
        bits.append(f"log channel = {log_channel.mention if log_channel else '`None`'}")
        await interaction.followup.send(
            embed=self.ef.success(f"AutoMod **{PRESET_LABELS[preset.value]}** action updated.\n" + " · ".join(bits))
        )

    # ── /automod keyword add/remove/clear ───────────────────────────────

    keyword_group = app_commands.Group(
        parent=automod_group, name="keyword",
        description="Manage the custom keyword list (preset = keyword).",
    )

    @keyword_group.command(name="add", description="Add a word/phrase to the keyword filter.")
    @app_commands.describe(word="Word or phrase to block. * is a wildcard.")
    async def keyword_add(self, interaction: discord.Interaction, word: app_commands.Range[str, 1, 60]):
        if not await self._ensure_perms(interaction):
            return
        await interaction.response.defer(ephemeral=False)
        cfg = await get_automod_config(interaction.guild.id, "keyword") or {}  # type: ignore[union-attr]
        kws = list(cfg.get("keywords") or [])
        word_l = word.lower().strip()
        if word_l in kws:
            return await interaction.followup.send(embed=self.ef.info("Already in the list."))
        if len(kws) >= 1000:
            return await interaction.followup.send(
                embed=self.ef.error("Discord caps keyword filters at 1000 entries.")
            )
        kws.append(word_l)
        await upsert_automod_config(interaction.guild.id, "keyword", keywords=kws)  # type: ignore[union-attr]
        if cfg.get("rule_id"):
            _, err = await self._sync_rule(interaction.guild, "keyword")
            if err:
                return await interaction.followup.send(embed=self.ef.error(err))
        await interaction.followup.send(
            embed=self.ef.success(f"Added `{word_l}` to the keyword filter ({len(kws)} total).")
        )

    @keyword_group.command(name="remove", description="Remove a word from the keyword filter.")
    @app_commands.describe(word="Word/phrase to remove.")
    async def keyword_remove(self, interaction: discord.Interaction, word: str):
        if not await self._ensure_perms(interaction):
            return
        await interaction.response.defer(ephemeral=False)
        cfg = await get_automod_config(interaction.guild.id, "keyword") or {}  # type: ignore[union-attr]
        kws = list(cfg.get("keywords") or [])
        word_l = word.lower().strip()
        if word_l not in kws:
            return await interaction.followup.send(embed=self.ef.info("Not in the list."))
        kws = [k for k in kws if k != word_l]
        await upsert_automod_config(interaction.guild.id, "keyword", keywords=kws)  # type: ignore[union-attr]
        if cfg.get("rule_id"):
            _, err = await self._sync_rule(interaction.guild, "keyword")
            if err:
                return await interaction.followup.send(embed=self.ef.error(err))
        await interaction.followup.send(
            embed=self.ef.success(f"Removed `{word_l}` ({len(kws)} remaining).")
        )

    @keyword_group.command(name="list", description="Show the keyword filter list.")
    async def keyword_list(self, interaction: discord.Interaction):
        assert interaction.guild
        await interaction.response.defer(ephemeral=False)
        cfg = await get_automod_config(interaction.guild.id, "keyword") or {}
        kws = list(cfg.get("keywords") or [])
        if not kws:
            return await interaction.followup.send(
                embed=self.ef.info("No keywords configured. Use `/automod keyword add`.")
            )
        chunks: list[str] = []
        cur = ""
        for k in kws:
            piece = f"`{k}` "
            if len(cur) + len(piece) > 1000:
                chunks.append(cur)
                cur = piece
            else:
                cur += piece
        if cur:
            chunks.append(cur)
        e = self.ef.build(title=f"Keyword Filter ({len(kws)})", color_key="accent")
        for i, c in enumerate(chunks, 1):
            e.add_field(name=f"Page {i}", value=c, inline=False)
        await interaction.followup.send(embed=e)

    @keyword_group.command(name="clear", description="Wipe every keyword from the filter.")
    async def keyword_clear(self, interaction: discord.Interaction):
        if not await self._ensure_perms(interaction):
            return
        await interaction.response.defer(ephemeral=False)
        cfg = await get_automod_config(interaction.guild.id, "keyword") or {}  # type: ignore[union-attr]
        await upsert_automod_config(interaction.guild.id, "keyword", keywords=[])  # type: ignore[union-attr]
        if cfg.get("rule_id"):
            rule = await _fetch_existing_rule(interaction.guild, int(cfg["rule_id"]))  # type: ignore[arg-type]
            if rule:
                try:
                    await rule.edit(enabled=False, reason="Nana keyword list cleared")
                except (discord.Forbidden, discord.HTTPException):
                    pass
        await interaction.followup.send(embed=self.ef.success("Keyword filter cleared."))

    # ── /automod mentions ────────────────────────────────────────────────

    @automod_group.command(name="mentions", description="Set the mention-spam limit (preset = mention_spam).")
    @app_commands.describe(limit="Max mentions per message before triggering (1-50).")
    async def mentions(self, interaction: discord.Interaction, limit: app_commands.Range[int, 1, 50]):
        if not await self._ensure_perms(interaction):
            return
        await interaction.response.defer(ephemeral=False)
        await upsert_automod_config(
            interaction.guild.id, "mention_spam",  # type: ignore[union-attr]
            mention_limit=int(limit),
        )
        cfg = await get_automod_config(interaction.guild.id, "mention_spam") or {}  # type: ignore[union-attr]
        if cfg.get("rule_id"):
            _, err = await self._sync_rule(interaction.guild, "mention_spam")
            if err:
                return await interaction.followup.send(embed=self.ef.error(err))
        await interaction.followup.send(
            embed=self.ef.success(f"Mention-spam limit set to **{limit}**. Run `/automod enable mention_spam` if not already on.")
        )

    # ── error handler ────────────────────────────────────────────────────

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.CommandOnCooldown):
            embed = self.ef.warning(f"Slow down — try again in **{error.retry_after:.1f}s**.")
        elif isinstance(error, app_commands.MissingPermissions):
            embed = self.ef.error("You need **Manage Server** to use this command.")
        else:
            embed = self.ef.error(str(error))
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(embed=embed, ephemeral=False)
            else:
                await interaction.followup.send(embed=embed, ephemeral=False)
        except discord.HTTPException:
            pass


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AutoMod(bot))
