"""Per-guild key/value settings backed by the shared sqlite DB (bot.db).
Used for log-channel configuration; any cog can import and call these
directly by passing in the bot instance -- no cross-cog singleton imports.
"""
from __future__ import annotations

import discord
from discord.ext import commands


async def get_guild_setting(bot: commands.Bot, guild_id: int, key: str) -> str | None:
    async with bot.db.execute(
        "SELECT setting_value FROM guild_settings WHERE guild_id = ? AND setting_key = ?",
        (guild_id, key),
    ) as cursor:
        row = await cursor.fetchone()
    return row[0] if row else None


async def set_guild_setting(bot: commands.Bot, guild_id: int, key: str, value: str):
    await bot.db.execute(
        """INSERT INTO guild_settings (guild_id, setting_key, setting_value)
           VALUES (?, ?, ?)
           ON CONFLICT(guild_id, setting_key) DO UPDATE SET setting_value = excluded.setting_value""",
        (guild_id, key, value),
    )
    await bot.db.commit()


async def clear_guild_setting(bot: commands.Bot, guild_id: int, key: str) -> bool:
    cursor = await bot.db.execute(
        "DELETE FROM guild_settings WHERE guild_id = ? AND setting_key = ?",
        (guild_id, key),
    )
    await bot.db.commit()
    return cursor.rowcount > 0


async def get_log_channel(bot: commands.Bot, guild_id: int, log_type: str) -> discord.TextChannel | None:
    """Looks up the configured channel ID for a log type and resolves it to an
    actual TextChannel -- or None if it was never set, or the channel no
    longer exists (deleted, or the bot lost access)."""
    value = await get_guild_setting(bot, guild_id, log_type)
    if value is None:
        return None

    channel = bot.get_channel(int(value))  # cheap cache lookup, no API call
    if channel is None:
        try:
            channel = await bot.fetch_channel(int(value))  # falls back to an API call
        except (discord.NotFound, discord.Forbidden):
            return None

    return channel if isinstance(channel, discord.TextChannel) else None
