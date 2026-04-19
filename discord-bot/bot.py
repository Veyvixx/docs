"""
Main entry point — loads config, initialises DB, registers all cogs,
syncs slash commands, and passes shared state to every cog.
"""

import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import discord
from discord.ext import commands

from utils.database import init_db, is_blacklisted
from utils.embeds import EmbedFactory
from utils.webhook import start_webhook_server

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("bot.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("bot")


def load_config() -> dict:
    config_path = Path(__file__).parent / "config.json"
    with open(config_path, encoding="utf-8") as f:
        return json.load(f)


class DiscordBot(commands.Bot):
    def __init__(self, config: dict) -> None:
        self.config = config
        # EmbedFactory created before login; bot ref is set in on_ready
        self.ef = EmbedFactory(config)

        intents = discord.Intents.default()
        intents.members = True
        intents.presences = True
        intents.message_content = True

        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            help_command=None,
            case_insensitive=True,
        )

    DEV_GUILD = discord.Object(id=1492674952447393913)

    async def setup_hook(self) -> None:
        await init_db()

        cogs = [
            "cogs.misc", "cogs.moderation", "cogs.antinuke",
            "cogs.customization", "cogs.developer", "cogs.events",
            "cogs.autoresponder", "cogs.buttons_cog",
            "cogs.premium_activate",
        ]
        for cog in cogs:
            try:
                await self.load_extension(cog)
                logger.info("Loaded cog: %s", cog)
            except Exception as exc:
                logger.error("Failed to load cog %s: %s", cog, exc, exc_info=True)

        global_synced = await self.tree.sync()
        logger.info("Synced %d global slash command(s).", len(global_synced))

        guild_synced = await self.tree.sync(guild=self.DEV_GUILD)
        logger.info("Synced %d guild slash command(s) to dev guild.", len(guild_synced))

        asyncio.create_task(start_webhook_server(self))

    async def on_ready(self) -> None:
        if not hasattr(self, "start_time"):
            self.start_time: datetime = datetime.now(timezone.utc)
        # Give EmbedFactory access to the bot user for footers
        self.ef.bot = self
        logger.info(
            "Bot ready! Logged in as %s (ID: %s) | Guilds: %d",
            self.user,
            self.user.id if self.user else "unknown",
            len(self.guilds),
        )
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name=f"{len(self.guilds)} servers | /help",
            )
        )

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        if not self.user:
            return

        # Respond to bare pings (mention with no command)
        if self.user in message.mentions:
            content = message.content.strip()
            bare = content in (
                f"<@{self.user.id}>",
                f"<@!{self.user.id}>",
            )
            if bare:
                pe = self.config.get("premium", {}).get("emojis", {})
                butterflies = pe.get("butterflies", "🦋")
                small_heart  = pe.get("heart",       "🤍")
                tulip        = pe.get("tulip",        "🌷")
                pinky        = pe.get("pinkyHeart",   "💗")

                embed = discord.Embed(
                    title       = f"Nana in your hands! {butterflies}",
                    description = (
                        f"We use mainly `/` slash commands. Here is everything you need to know.\n"
                        f"\n"
                        f"**Quick Commands** {small_heart}\n"
                        f"- `/help` — Find all available commands\n"
                        f"- `/about` — Learn more about Nana\n"
                        f"\n"
                        f"**Premium** {tulip}\n"
                        f"Make your server advanced with anti-nuke, enhanced moderation, real time "
                        f"moderating, gorgeous embed builder with no limit and a __lot more__! "
                        f"— only with **Nana Premium**"
                    ),
                    color = 0xF2EAEA,
                )
                embed.set_footer(
                    text     = f"Developed with love by veyvixx",
                    icon_url = self.user.display_avatar.url if self.user else None,
                )

                view = discord.ui.View()
                view.add_item(discord.ui.Button(
                    label="Support Server",
                    url="https://discord.gg/BQcm3ptaxX",
                    style=discord.ButtonStyle.link,
                ))
                view.add_item(discord.ui.Button(
                    label="Patreon",
                    url="https://patreon.com/NanaBotDis",
                    style=discord.ButtonStyle.link,
                ))

                await message.reply(embed=embed, view=view, mention_author=False)
                return

        await self.process_commands(message)

    async def on_guild_join(self, guild: discord.Guild) -> None:
        if await is_blacklisted(guild.id):
            logger.info("Leaving blacklisted guild: %s (ID: %s)", guild.name, guild.id)
            await guild.leave()
            return
        logger.info("Joined guild: %s (ID: %s)", guild.name, guild.id)

    async def on_guild_remove(self, guild: discord.Guild) -> None:
        logger.info("Removed from guild: %s (ID: %s)", guild.name, guild.id)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if await is_blacklisted(interaction.user.id):
            return False
        if interaction.guild and await is_blacklisted(interaction.guild.id):
            return False
        return True

    async def on_app_command_error(
        self,
        interaction: discord.Interaction,
        error: discord.app_commands.AppCommandError,
    ) -> None:
        embed = self.ef.error(str(error))
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(embed=embed, ephemeral=False)
            else:
                await interaction.followup.send(embed=embed, ephemeral=False)
        except Exception:
            pass


async def main() -> None:
    config = load_config()
    token = os.environ.get("DISCORD_TOKEN") or config.get("token", "")
    if not token:
        logger.error("No bot token found. Set DISCORD_TOKEN env var.")
        sys.exit(1)

    bot = DiscordBot(config)
    async with bot:
        await bot.start(token)


if __name__ == "__main__":
    asyncio.run(main())
