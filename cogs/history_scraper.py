"""Historical data scraper: fetches old welcome messages and username changes."""
from __future__ import annotations

import asyncio
import re
import discord
from typing import Optional
from discord import app_commands
from discord.ext import commands
from common.settings_store import get_guild_setting, set_guild_setting
from cogs.admin import is_bot_owner
import logging

logger = logging.getLogger("history_scraper")

MENTION_RE = re.compile(r"<@!?(\d+)>")
ID_RE = re.compile(r"(\d{17,20})")
MAX_SNOWFLAKE = (1 << 63) - 1

# Add the IDs of users whose welcome messages you want to completely ignore here.
SKIP_USER_IDS = [
    #123456789123456789,
    #123456789123456789,
]


def parse_welcome_content(msg: discord.Message) -> Optional[int]:
    """Extract user ID from welcome message content or embeds."""
    # 1. Skip if the author is one of the specific users we want to ignore
    if msg.author.id in SKIP_USER_IDS:
        return None
        
    # 2. Handle default Discord system welcome messages
    if msg.type == discord.MessageType.new_member:
        uid = msg.author.id
        if 1000 < uid <= MAX_SNOWFLAKE:
            # Check if it's a deleted account placeholder
            if msg.author.name == "Deleted User" and msg.author.discriminator == "0000":
                return None
            return uid
        return None

    # 3. Gather all text from content AND embeds for bot messages
    text_to_check = msg.content
    
    for embed in msg.embeds:
        if embed.title:
            text_to_check += " " + embed.title
        if embed.description:
            text_to_check += " " + embed.description
        for field in embed.fields:
            text_to_check += " " + field.name + " " + field.value
            
    # 4. Skip boost messages
    if "boost" in text_to_check.lower():
        return None
        
    # 5. Skip unknown/deleted user placeholders
    lower_text = text_to_check.lower()
    if "@unknown" in lower_text or "unknown-user" in lower_text:
        return None
        
    # 6. Extract user ID from mention
    if not text_to_check.strip():
        return None
        
    matches = MENTION_RE.findall(text_to_check)
    if matches:
        uid = int(matches[0])
        if 1000 < uid <= MAX_SNOWFLAKE:
            return uid
    return None


def parse_username_embed(embed: discord.Embed) -> Optional[tuple[int, str, str]]:
    """Extract (user_id, old_name, new_name) from a username change embed."""
    title = embed.title or ""
    if "Username Updated" not in title:
        return None

    old_name = None
    new_name = None
    user_id = None

    for field in embed.fields:
        fname = field.name.lower()
        if "old" in fname:
            old_name = field.value.strip().strip("`")
        elif "new" in fname:
            new_name = field.value.strip().strip("`")
        elif "user id" in fname or fname.strip() == "id":
            m = ID_RE.search(field.value)
            if m:
                parsed_id = int(m.group(1))
                if 0 < parsed_id <= MAX_SNOWFLAKE:
                    user_id = parsed_id

    if user_id is None and embed.author:
        m = ID_RE.search(embed.author.name or "")
        if m:
            parsed_id = int(m.group(1))
            if 0 < parsed_id <= MAX_SNOWFLAKE:
                user_id = parsed_id

    if user_id and old_name and new_name:
        return (user_id, old_name, new_name)
    return None


class HistoryScraper(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._active_scrapes: set[str] = set()

    history = app_commands.Group(
        name="history",
        description="Historical data scraping tools (bot owner only)",
        default_permissions=discord.Permissions(administrator=True),
    )

    @history.command(name="welcome_channel", description="Set the channel to scrape for old welcome messages")
    @app_commands.describe(channel="The welcome channel")
    @is_bot_owner()
    @app_commands.guild_only()
    async def set_welcome_channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        await set_guild_setting(self.bot, interaction.guild_id, "history_welcome_channel", str(channel.id))
        await interaction.response.send_message(f"✅ Welcome channel set to {channel.mention}", ephemeral=True)

    @history.command(name="username_channel", description="Set the channel to scrape for old username changes")
    @app_commands.describe(channel="The username change log channel")
    @is_bot_owner()
    @app_commands.guild_only()
    async def set_username_channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        await set_guild_setting(self.bot, interaction.guild_id, "history_username_channel", str(channel.id))
        await interaction.response.send_message(f"✅ Username change channel set to {channel.mention}", ephemeral=True)

    @history.command(name="clear_welcome", description="Wipe all scraped join history for this server so you can re-scrape")
    @is_bot_owner()
    @app_commands.guild_only()
    async def clear_welcome(self, interaction: discord.Interaction):
        await self.bot.db.execute("DELETE FROM join_history WHERE guild_id = ?", (interaction.guild_id,))
        await self.bot.db.execute("DELETE FROM scrape_progress WHERE guild_id = ? AND scrape_type = 'welcome'", (interaction.guild_id,))
        await self.bot.db.commit()
        await interaction.response.send_message("✅ Wiped all join history for this server. You can now re-run `/history scrape_welcome`.", ephemeral=True)

    @history.command(name="add_join", description="Manually add a user's join date using a specific message ID")
    @app_commands.describe(
        message_id="The ID of the message to use as the join date",
        user_id="The ID of the user who joined"
    )
    @is_bot_owner()
    @app_commands.guild_only()
    async def add_join(self, interaction: discord.Interaction, message_id: str, user_id: str):
        channel_id_str = await get_guild_setting(self.bot, interaction.guild_id, "history_welcome_channel")
        if not channel_id_str:
            await interaction.response.send_message("❌ Welcome channel not set. Use `/history welcome_channel` first.", ephemeral=True)
            return
            
        channel = self.bot.get_channel(int(channel_id_str))
        if not channel:
            await interaction.response.send_message("❌ Welcome channel not found.", ephemeral=True)
            return
            
        try:
            msg = await channel.fetch_message(int(message_id))
        except discord.NotFound:
            await interaction.response.send_message("❌ Message not found in the welcome channel.", ephemeral=True)
            return
        except ValueError:
            await interaction.response.send_message("❌ Invalid message ID.", ephemeral=True)
            return
            
        if not user_id.isdigit():
            await interaction.response.send_message("❌ Invalid user ID.", ephemeral=True)
            return
            
        uid = int(user_id)
        join_ts = int(msg.created_at.timestamp())
        
        await self.bot.db.execute(
            "INSERT OR IGNORE INTO join_history (user_id, guild_id, joined_at) VALUES (?, ?, ?)",
            (uid, interaction.guild_id, join_ts)
        )
        await self.bot.db.commit()
        
        await interaction.response.send_message(
            f"✅ Added join date <t:{join_ts}:F> for user `{uid}` based on message `{message_id}`.",
            ephemeral=True
        )

    @history.command(name="scrape_welcome", description="Start scraping old welcome messages (background task)")
    @is_bot_owner()
    @app_commands.guild_only()
    async def scrape_welcome(self, interaction: discord.Interaction):
        channel_id_str = await get_guild_setting(self.bot, interaction.guild_id, "history_welcome_channel")
        if not channel_id_str:
            await interaction.response.send_message("❌ Welcome channel not set. Use `/history welcome_channel` first.", ephemeral=True)
            return
        channel = self.bot.get_channel(int(channel_id_str))
        if not channel:
            await interaction.response.send_message("❌ Channel not found.", ephemeral=True)
            return
        key = f"{interaction.guild_id}:welcome"
        if key in self._active_scrapes:
            await interaction.response.send_message("❌ Already in progress.", ephemeral=True)
            return
        self._active_scrapes.add(key)
        await interaction.response.send_message("✅ Started scraping welcome messages in the background. Use `/history status` to check progress.", ephemeral=True)
        asyncio.create_task(self._scrape_welcome_task(interaction.guild_id, channel))

    @history.command(name="scrape_usernames", description="Start scraping old username changes (background task)")
    @is_bot_owner()
    @app_commands.guild_only()
    async def scrape_usernames(self, interaction: discord.Interaction):
        channel_id_str = await get_guild_setting(self.bot, interaction.guild_id, "history_username_channel")
        if not channel_id_str:
            await interaction.response.send_message("❌ Username channel not set. Use `/history username_channel` first.", ephemeral=True)
            return
        channel = self.bot.get_channel(int(channel_id_str))
        if not channel:
            await interaction.response.send_message("❌ Channel not found.", ephemeral=True)
            return
        key = f"{interaction.guild_id}:usernames"
        if key in self._active_scrapes:
            await interaction.response.send_message("❌ Already in progress.", ephemeral=True)
            return
        self._active_scrapes.add(key)
        await interaction.response.send_message("✅ Started scraping username changes in the background. Use `/history status` to check progress.", ephemeral=True)
        asyncio.create_task(self._scrape_usernames_task(interaction.guild_id, channel))

    @history.command(name="status", description="Check scraping progress")
    @is_bot_owner()
    @app_commands.guild_only()
    async def status(self, interaction: discord.Interaction):
        async with self.bot.db.execute(
            "SELECT scrape_type, is_complete, total_processed, total_saved FROM scrape_progress WHERE guild_id = ?",
            (interaction.guild_id,)
        ) as cursor:
            rows = await cursor.fetchall()
        if not rows:
            await interaction.response.send_message("No scraping has been done yet.", ephemeral=True)
            return
        embed = discord.Embed(title="📊 Scraping Status", color=discord.Color.blue())
        for stype, done, proc, saved in rows:
            key = f"{interaction.guild_id}:{stype}"
            status = "✅ Complete" if done else ("🔄 In Progress" if key in self._active_scrapes else "⏸ Paused")
            embed.add_field(name=stype.title(), value=f"Status: {status}\nProcessed: {proc:,}\nSaved: {saved:,}", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @history.command(name="stop", description="Stop any active scraping")
    @is_bot_owner()
    @app_commands.guild_only()
    async def stop(self, interaction: discord.Interaction):
        prefix = f"{interaction.guild_id}:"
        active = [k for k in self._active_scrapes if k.startswith(prefix)]
        if not active:
            await interaction.response.send_message("No active scraping to stop.", ephemeral=True)
            return
        for k in active:
            self._active_scrapes.discard(k)
        await interaction.response.send_message(f"✅ Stopped {len(active)} scraping task(s). Progress has been saved.", ephemeral=True)

    @history.command(name="reset", description="Reset scraping progress for a specific type")
    @app_commands.describe(scrape_type="Which to reset")
    @app_commands.choices(scrape_type=[
        app_commands.Choice(name="Welcome Messages", value="welcome"),
        app_commands.Choice(name="Username Changes", value="usernames"),
    ])
    @is_bot_owner()
    @app_commands.guild_only()
    async def reset(self, interaction: discord.Interaction, scrape_type: app_commands.Choice[str]):
        key = f"{interaction.guild_id}:{scrape_type.value}"
        if key in self._active_scrapes:
            await interaction.response.send_message("❌ Stop it first with `/history stop`.", ephemeral=True)
            return
        await self.bot.db.execute("DELETE FROM scrape_progress WHERE guild_id = ? AND scrape_type = ?", (interaction.guild_id, scrape_type.value))
        await self.bot.db.commit()
        await interaction.response.send_message(f"✅ Reset progress for {scrape_type.name}.", ephemeral=True)

    async def _scrape_welcome_task(self, guild_id: int, channel: discord.TextChannel):
        key = f"{guild_id}:welcome"
        logger.info(f"[History] Starting welcome scrape task for guild {guild_id}")
        try:
            # --- Auto-insert Server Owner's join date as server creation date ---
            guild = self.bot.get_guild(guild_id)
            if guild and guild.owner_id:
                owner_ts = int(guild.created_at.timestamp())
                await self.bot.db.execute(
                    "INSERT OR IGNORE INTO join_history (user_id, guild_id, joined_at) VALUES (?, ?, ?)",
                    (guild.owner_id, guild_id, owner_ts)
                )
                await self.bot.db.commit()
                logger.info(f"[History] Inserted server owner ({guild.owner_id}) join date as {owner_ts}")

            async with self.bot.db.execute(
                "SELECT last_message_id, total_processed, total_saved FROM scrape_progress WHERE guild_id = ? AND scrape_type = ?",
                (guild_id, "welcome")
            ) as cursor:
                row = await cursor.fetchone()
            last_id = row[0] if row else None
            total_proc = row[1] or 0 if row else 0
            total_saved = row[2] or 0 if row else 0
            
            logger.info(f"[History] Resuming from message ID: {last_id}, Processed: {total_proc}")

            if not row:
                await self.bot.db.execute(
                    "INSERT INTO scrape_progress (guild_id, scrape_type, last_message_id, is_complete, total_processed, total_saved) VALUES (?, ?, NULL, 0, 0, 0)",
                    (guild_id, "welcome")
                )
                await self.bot.db.commit()

            while key in self._active_scrapes:
                logger.info(f"[History] Fetching 100 welcome messages before {last_id}...")
                if last_id:
                    messages = [m async for m in channel.history(limit=100, before=discord.Object(id=last_id))]
                else:
                    messages = [m async for m in channel.history(limit=100)]
                if not messages:
                    logger.info("[History] No more welcome messages found. Scrape complete.")
                    break
                
                batch = 0
                for msg in messages:
                    uid = parse_welcome_content(msg)
                    if uid:
                        msg_ts = int(msg.created_at.timestamp())
                        
                        # 5-minute deduplication check
                        async with self.bot.db.execute(
                            "SELECT 1 FROM join_history WHERE user_id = ? AND guild_id = ? AND joined_at >= ?",
                            (uid, guild_id, msg_ts - 300) # 300 seconds = 5 minutes
                        ) as cursor:
                            if await cursor.fetchone():
                                continue # Skip, already joined in the last 5 mins
                                
                        await self.bot.db.execute(
                            "INSERT OR IGNORE INTO join_history (user_id, guild_id, joined_at) VALUES (?, ?, ?)",
                            (uid, guild_id, msg_ts)
                        )
                        batch += 1
                await self.bot.db.commit()

                last_id = messages[-1].id
                total_proc += len(messages)
                total_saved += batch
                await self.bot.db.execute(
                    "UPDATE scrape_progress SET last_message_id = ?, total_processed = ?, total_saved = ? WHERE guild_id = ? AND scrape_type = ?",
                    (last_id, total_proc, total_saved, guild_id, "welcome")
                )
                await self.bot.db.commit()
                logger.info(f"[History] Welcome: {total_proc:,} processed, {total_saved:,} saved (id: {last_id})")
                await asyncio.sleep(5.0)

            if key in self._active_scrapes:
                await self.bot.db.execute("UPDATE scrape_progress SET is_complete = 1 WHERE guild_id = ? AND scrape_type = ?", (guild_id, "welcome"))
                await self.bot.db.commit()
                logger.info(f"[History] Welcome scrape complete: {total_proc:,} processed, {total_saved:,} saved")
        except Exception as e:
            logger.error(f"[History] Error in welcome scrape: {e}")
        finally:
            self._active_scrapes.discard(key)
            logger.info("[History] Welcome scrape task ended.")

    async def _scrape_usernames_task(self, guild_id: int, channel: discord.TextChannel):
        key = f"{guild_id}:usernames"
        logger.info(f"[History] Starting username scrape task for guild {guild_id}")
        try:
            async with self.bot.db.execute(
                "SELECT last_message_id, total_processed, total_saved FROM scrape_progress WHERE guild_id = ? AND scrape_type = ?",
                (guild_id, "usernames")
            ) as cursor:
                row = await cursor.fetchone()
            
            last_id = row[0] if row else None
            total_proc = row[1] or 0 if row else 0
            total_saved = row[2] or 0 if row else 0
            
            logger.info(f"[History] Resuming from message ID: {last_id}, Processed: {total_proc}")
            
            if not row:
                await self.bot.db.execute(
                    "INSERT INTO scrape_progress (guild_id, scrape_type, last_message_id, is_complete, total_processed, total_saved) VALUES (?, ?, NULL, 0, 0, 0)",
                    (guild_id, "usernames")
                )
                await self.bot.db.commit()

            while key in self._active_scrapes:
                logger.info(f"[History] Fetching 100 messages before {last_id}...")
                if last_id:
                    messages = [m async for m in channel.history(limit=100, before=discord.Object(id=last_id))]
                else:
                    messages = [m async for m in channel.history(limit=100)]
                    
                if not messages:
                    logger.info("[History] No more messages found. Scrape complete.")
                    break

                batch = 0
                for msg in messages:
                    for embed in msg.embeds:
                        result = parse_username_embed(embed)
                        if result:
                            uid, old_n, new_n = result
                            ts = int(msg.created_at.timestamp())
                            
                            async with self.bot.db.execute(
                                "SELECT 1 FROM username_history WHERE user_id = ? AND username = ? LIMIT 1",
                                (uid, old_n)
                            ) as cursor:
                                if not await cursor.fetchone():
                                    await self.bot.db.execute(
                                        "INSERT OR IGNORE INTO username_history (user_id, username, timestamp) VALUES (?, ?, ?)",
                                        (uid, old_n, ts - 1)
                                    )
                            
                            await self.bot.db.execute(
                                "INSERT OR IGNORE INTO username_history (user_id, username, timestamp) VALUES (?, ?, ?)",
                                (uid, new_n, ts)
                            )
                            batch += 1
                await self.bot.db.commit()

                last_id = messages[-1].id
                total_proc += len(messages)
                total_saved += batch
                await self.bot.db.execute(
                    "UPDATE scrape_progress SET last_message_id = ?, total_processed = ?, total_saved = ? WHERE guild_id = ? AND scrape_type = ?",
                    (last_id, total_proc, total_saved, guild_id, "usernames")
                )
                await self.bot.db.commit()
                logger.info(f"[History] Usernames: {total_proc:,} processed, {total_saved:,} saved (id: {last_id})")
                await asyncio.sleep(5.0)

            if key in self._active_scrapes:
                await self.bot.db.execute("UPDATE scrape_progress SET is_complete = 1 WHERE guild_id = ? AND scrape_type = ?", (guild_id, "usernames"))
                await self.bot.db.commit()
                logger.info(f"[History] Username scrape complete: {total_proc:,} processed, {total_saved:,} saved")
        except Exception as e:
            logger.error(f"[History] Error in username scrape: {e}")
        finally:
            self._active_scrapes.discard(key)
            logger.info("[History] Username scrape task ended.")


async def setup(bot: commands.Bot):
    await bot.add_cog(HistoryScraper(bot))