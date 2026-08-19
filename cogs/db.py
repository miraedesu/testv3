from __future__ import annotations

import discord
from discord.ext import commands
from common.constants import ATTACHMENTS_DIR
import logging
logger = logging.getLogger(__name__)

class Database(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self) -> None:
        """Runs when the cog is added/reloaded. Creates all DB tables."""
        logger.info("[DB] Initializing database schema...")
        db = self.bot.db
        
        # Ensure attachments directory exists
        ATTACHMENTS_DIR.mkdir(exist_ok=True)
        
        await db.execute("PRAGMA journal_mode=WAL")
        
        # --- Enabled Cogs ---
        await db.execute("""
            CREATE TABLE IF NOT EXISTS enabled_cogs (
                cog_name TEXT PRIMARY KEY
            )
        """)
        # --- Custom Reactions ---
        await db.execute("""
            CREATE TABLE IF NOT EXISTS custom_reactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                username TEXT NOT NULL,
                name TEXT NOT NULL,
                reply_text TEXT,
                attachment_path TEXT,
                attachment_name TEXT,
                original_url TEXT
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_custom_reactions_guild_name ON custom_reactions(guild_id, name)")

        await db.execute("""
            CREATE TABLE IF NOT EXISTS reaction_images (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                reaction_id INTEGER NOT NULL,
                attachment_path TEXT,
                attachment_name TEXT,
                original_url TEXT
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_reaction_images_reaction_id ON reaction_images(reaction_id)")

        # --- Disabled Commands & Features (channel-aware) ---
        await db.execute("""
            CREATE TABLE IF NOT EXISTS disabled_commands (
                guild_id INTEGER NOT NULL,
                command_name TEXT NOT NULL,
                channel_id INTEGER,
                PRIMARY KEY (guild_id, command_name, channel_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS disabled_features (
                guild_id INTEGER NOT NULL,
                feature_name TEXT NOT NULL,
                channel_id INTEGER,
                PRIMARY KEY (guild_id, feature_name, channel_id)
            )
        """)
                # --- Migration: add channel_id to disabled_commands / disabled_features
        async def _has_column(table: str, column: str) -> bool:
            async with db.execute(f"PRAGMA table_info({table})") as cur:
                cols = await cur.fetchall()
            return any(c[1] == column for c in cols)

        if not await _has_column("disabled_commands", "channel_id"):
            await db.execute(
                "CREATE TABLE disabled_commands_new ("
                "  guild_id INTEGER NOT NULL,"
                "  command_name TEXT NOT NULL,"
                "  channel_id INTEGER,"
                "  PRIMARY KEY (guild_id, command_name, channel_id)"
                ")"
            )
            await db.execute(
                "INSERT OR IGNORE INTO disabled_commands_new "
                "(guild_id, command_name, channel_id) "
                "SELECT guild_id, command_name, NULL FROM disabled_commands"
            )
            await db.execute("DROP TABLE disabled_commands")
            await db.execute(
                "ALTER TABLE disabled_commands_new RENAME TO disabled_commands"
            )

        if not await _has_column("disabled_features", "channel_id"):
            await db.execute(
                "CREATE TABLE disabled_features_new ("
                "  guild_id INTEGER NOT NULL,"
                "  feature_name TEXT NOT NULL,"
                "  channel_id INTEGER,"
                "  PRIMARY KEY (guild_id, feature_name, channel_id)"
                ")"
            )
            await db.execute(
                "INSERT OR IGNORE INTO disabled_features_new "
                "(guild_id, feature_name, channel_id) "
                "SELECT guild_id, feature_name, NULL FROM disabled_features"
            )
            await db.execute("DROP TABLE disabled_features")
            await db.execute(
                "ALTER TABLE disabled_features_new RENAME TO disabled_features"
            )
        # --- Phrase Pools ---
        await db.execute("""
            CREATE TABLE IF NOT EXISTS phrase_pools (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                pool_name TEXT NOT NULL,
                phrase TEXT NOT NULL
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_phrase_pools_guild_pool ON phrase_pools(guild_id, pool_name)")

        # --- Guild Settings ---
        await db.execute("""
            CREATE TABLE IF NOT EXISTS guild_settings (
                guild_id INTEGER NOT NULL,
                setting_key TEXT NOT NULL,
                setting_value TEXT,
                PRIMARY KEY (guild_id, setting_key)
            )
        """)

        # --- User Notes ---
        await db.execute("""
            CREATE TABLE IF NOT EXISTS user_notes (
                note_id TEXT PRIMARY KEY,
                guild_id INTEGER NOT NULL,
                author_id INTEGER NOT NULL,
                target_id INTEGER NOT NULL,
                note_text TEXT NOT NULL,
                note_image_url TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (guild_id, author_id, target_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS note_access (
                guild_id INTEGER NOT NULL,
                entity_type TEXT NOT NULL,
                entity_id INTEGER NOT NULL,
                status TEXT NOT NULL,
                PRIMARY KEY (guild_id, entity_type, entity_id)
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_notes_target ON user_notes (guild_id, target_id)")

        # --- Reminders ---
        await db.execute("""
            CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                channel_id INTEGER,
                guild_id INTEGER,
                trigger_time INTEGER NOT NULL,
                message TEXT NOT NULL,
                repeat_interval INTEGER,
                created_at INTEGER NOT NULL
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_reminders_user ON reminders (user_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_reminders_trigger ON reminders (trigger_time)")

        # --- User Timezones ---
        await db.execute("""
            CREATE TABLE IF NOT EXISTS user_timezones (
                user_id INTEGER PRIMARY KEY,
                timezone TEXT NOT NULL
            )
        """)

        # --- Channel Snapshots ---
        await db.execute("""
            CREATE TABLE IF NOT EXISTS channel_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                snapshot_data TEXT NOT NULL,
                created_at INTEGER NOT NULL
            )
        """)

        # --- One-time Migration (Custom Reactions) ---
        await db.execute("""
            INSERT INTO reaction_images (reaction_id, attachment_path, attachment_name, original_url)
            SELECT id, attachment_path, attachment_name, original_url
            FROM custom_reactions
            WHERE attachment_path IS NOT NULL
              AND id NOT IN (SELECT reaction_id FROM reaction_images)
        """)
        # --- WhoIs History Tables ---
        await db.execute("""
            CREATE TABLE IF NOT EXISTS username_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                username TEXT NOT NULL,
                timestamp INTEGER NOT NULL,
                UNIQUE (user_id, username, timestamp)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS join_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                guild_id INTEGER NOT NULL,
                joined_at INTEGER NOT NULL,
                UNIQUE (user_id, guild_id, joined_at)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS punishment_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                guild_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                reason TEXT,
                moderator_id INTEGER,
                timestamp INTEGER NOT NULL
            )
        """)
        # --- Staff Notes (allows multiple entries) ---
        await db.execute("""
            CREATE TABLE IF NOT EXISTS staff_notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                guild_id INTEGER NOT NULL,
                author_id INTEGER NOT NULL,
                note_text TEXT NOT NULL,
                timestamp INTEGER NOT NULL
            )
        """)
                # --- AKA Names (admin-added nicknames) ---
        await db.execute("""
            CREATE TABLE IF NOT EXISTS aka_names (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                guild_id INTEGER NOT NULL,
                aka_name TEXT NOT NULL,
                added_by INTEGER NOT NULL,
                timestamp INTEGER NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS enabled_features (
                guild_id INTEGER NOT NULL,
                feature_name TEXT NOT NULL,
                PRIMARY KEY (guild_id, feature_name)
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS enabled_commands (
                guild_id INTEGER NOT NULL,
                command_name TEXT NOT NULL,
                PRIMARY KEY (guild_id, command_name)
            )
        """)
        # --- Guild Boosts (dedup tracking across restarts) ---
        await db.execute("""
            CREATE TABLE IF NOT EXISTS guild_boosts (
                guild_id  INTEGER NOT NULL,
                user_id   INTEGER NOT NULL,
                started_at INTEGER NOT NULL,
                PRIMARY KEY (guild_id, user_id)
            )
        """)
        await db.commit()
        logger.info("[DB] Schema initialization complete.")


async def setup(bot: commands.Bot):
    await bot.add_cog(Database(bot))