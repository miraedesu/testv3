"""Entry point: bot instance, DB connection, cog loading, global error handler."""
from __future__ import annotations

import os

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
import logging

from common.constants import ATTACHMENTS_DIR, DB_PATH, DEV_GUILD_ID
from common.command_toggles import is_command_disabled

load_dotenv()

# -------------------------------
# INTENTS
# -------------------------------
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.presences = True
intents.guilds = True
intents.dm_messages = True


# Configure logging to write to a file instantly (unbuffered)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(name)s] %(levelname)s: %(message)s',
    handlers=[
        logging.FileHandler("bot.log", encoding='utf-8'),
        logging.StreamHandler() # Also keep printing to console
    ]
)
logger = logging.getLogger(__name__) 
# Force discord.py to use the same logger
discord_logger = logging.getLogger('discord')
discord_logger.setLevel(logging.INFO)

class OwnerToggleableCommandTree(app_commands.CommandTree):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.type != discord.InteractionType.application_command:
            return True
        if interaction.guild_id is None or interaction.command is None:
            return True

        qualified_name = interaction.command.qualified_name
        if await is_command_disabled(self.client, interaction.guild_id, qualified_name, interaction.channel_id):
            await interaction.response.send_message(
                f"`/{qualified_name}` is disabled in this server or channel.",
                ephemeral=True,
            )
            return False
        return True

class MyBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents,
                         tree_cls=OwnerToggleableCommandTree)
        self.db = None

    async def setup_hook(self):
        ATTACHMENTS_DIR.mkdir(exist_ok=True)
        self.db = await aiosqlite.connect(DB_PATH)
        
        # Load the DB cog first so the enabled_cogs table exists
        await self.load_extension("cogs.db")
        
        # Fetch enabled cogs from the database
        async with self.db.execute("SELECT cog_name FROM enabled_cogs") as cursor:
            rows = await cursor.fetchall()
        
        cogs_to_load = [row[0] for row in rows]
        
        # If it's a fresh install (DB is empty), load a default list
        if not cogs_to_load:
            cogs_to_load = [
                "cogs.reactions", "cogs.settings", "cogs.welcome", "cogs.emotes",
                "cogs.moderation", "cogs.member_events", "cogs.permcheck", "cogs.admin",
                "cogs.anime", "cogs.profile", "cogs.whois", "cogs.misc", "cogs.guild_settings", "cogs.bot_log",
            ]
            # Save defaults to DB
            await self.db.executemany("INSERT OR IGNORE INTO enabled_cogs (cog_name) VALUES (?)", [(c,) for c in cogs_to_load])
            await self.db.commit()

        for ext in cogs_to_load:
            try:
                await self.load_extension(ext)
            except Exception as e:
                logger.error(f"Failed to load {ext}: {e}")

        await self.tree.sync()
        dev_guild = discord.Object(id=DEV_GUILD_ID)
        await self.tree.sync(guild=dev_guild)
        print(f"Synced slash commands for {self.user}")
    async def close(self):
        if self.db:
            logger.info("Closing aiosqlite connection...")
            try:
                await self.db.close()
            except Exception as e:
                logger.error(f"Error closing database: {e}")
        logger.info("Closing Discord bot client...")
        await super().close()


bot = MyBot()


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CommandOnCooldown):
        msg = f"<:alarm:1534195779810365530> Slow down! Try again in {error.retry_after:.1f}s"
    elif isinstance(error, app_commands.MissingPermissions):
        msg = "You need Administrator permission to use this command."
    elif isinstance(error, app_commands.TransformerError):
        msg=  "Could not find that member. They may have left the server or typed their name incorrectly."
    elif isinstance(error, app_commands.NoPrivateMessage):
        msg = "This command must be used in a server."
    elif isinstance(error, app_commands.CheckFailure):
        msg = "You don't have permission to use this command."
    else:
        raise error
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)


@bot.event
async def on_ready():
    logger.info(f"Logged in as {bot.user.name} ({bot.user.id})")


if __name__ == "__main__":
    bot.run(os.getenv("DISCORD_TOKEN"))