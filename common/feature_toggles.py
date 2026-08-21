"""Per-guild feature toggles for passive behaviors (event listeners).

Supports both guild-wide (channel_id IS NULL) and per-channel
(channel_id = <int>) disables. Opt-in features must be in enabled_features
(guild-wide only) to be active."""
from __future__ import annotations

from discord.ext import commands
from common.constants import OPT_IN_FEATURES


async def is_feature_disabled(
    bot: commands.Bot,
    guild_id: int,
    feature_name: str,
    channel_id: int | None = None,
) -> bool:
    """True if the feature is disabled in *channel_id* (or guild-wide
    when *channel_id* is None).  Also returns True for opt-in features
    that haven't been explicitly enabled."""
    # 1. Explicit disable — channel-specific OR guild-wide
    async with bot.db.execute(
        "SELECT 1 FROM disabled_features "
        "WHERE guild_id = ? AND feature_name = ? "
        "AND (channel_id IS NULL OR channel_id IS ?) LIMIT 1",
        (guild_id, feature_name, channel_id),
    ) as cursor:
        if await cursor.fetchone():
            return True

    # 2. Opt-in feature not yet enabled (guild-wide only)
    if feature_name in OPT_IN_FEATURES:
        async with bot.db.execute(
            "SELECT 1 FROM enabled_features "
            "WHERE guild_id = ? AND feature_name = ?",
            (guild_id, feature_name),
        ) as cursor:
            if not await cursor.fetchone():
                return True
    return False


async def get_disabled_features(
    bot: commands.Bot,
    guild_id: int,
    channel_id: int | None = None,
) -> set[str]:
    """All feature names disabled for *channel_id* (union of channel-specific
    and guild-wide).  When *channel_id* is None, returns guild-wide only."""
    if channel_id is None:
        async with bot.db.execute(
            "SELECT feature_name FROM disabled_features "
            "WHERE guild_id = ? AND channel_id IS NULL",
            (guild_id,),
        ) as cursor:
            rows = await cursor.fetchall()
    else:
        async with bot.db.execute(
            "SELECT feature_name FROM disabled_features "
            "WHERE guild_id = ? AND (channel_id IS NULL OR channel_id IS ?)",
            (guild_id, channel_id),
        ) as cursor:
            rows = await cursor.fetchall()
    return {name for (name,) in rows}

async def _ensure_disabled_by_column(bot):
    """Add disabled_by column to disabled_features if it doesn't exist."""
    try:
        await bot.db.execute(
            "ALTER TABLE disabled_features ADD COLUMN disabled_by TEXT DEFAULT 'guild_admin'"
        )
        await bot.db.commit()
    except Exception:
        pass  # Column already exists
async def _ensure_enabled_by_column(bot):
    """Add enabled_by column to enabled_features if it doesn't exist."""
    try:
        await bot.db.execute(
            "ALTER TABLE enabled_features ADD COLUMN enabled_by TEXT"
        )
        await bot.db.commit()
    except Exception:
        pass  # Column already exists
async def disable_feature(
    bot: commands.Bot,
    guild_id: int,
    feature_name: str,
    channel_id: int | None = None,
    disabled_by: str = "guild_admin",
) -> None:
    await _ensure_disabled_by_column(bot)
    await bot.db.execute(
        "INSERT OR IGNORE INTO disabled_features "
        "(guild_id, feature_name, channel_id, disabled_by) VALUES (?, ?, ?, ?)",
        (guild_id, feature_name, channel_id, disabled_by),
    )
    await bot.db.commit()


async def enable_feature(
    bot: commands.Bot,
    guild_id: int,
    feature_name: str,
    channel_id: int | None = None,
    disabled_by: str = "guild_admin",
    enabled_by: str = "guild_admin",
) -> bool:
    """Removes the disable row (only rows set by the same source).
    Returns True if a disable row was actually removed."""
    await _ensure_disabled_by_column(bot)
    await _ensure_enabled_by_column(bot)
    cursor = await bot.db.execute(
        "DELETE FROM disabled_features "
        "WHERE guild_id = ? AND feature_name = ? AND channel_id IS ? AND disabled_by = ?",
        (guild_id, feature_name, channel_id, disabled_by),
    )
    await bot.db.execute(
        "INSERT INTO enabled_features (guild_id, feature_name, enabled_by) "
        "VALUES (?, ?, ?) "
        "ON CONFLICT(guild_id, feature_name) DO UPDATE SET enabled_by = excluded.enabled_by",
        (guild_id, feature_name, enabled_by),
    )
    await bot.db.commit()
    return cursor.rowcount > 0

async def list_disabled_features(
    bot: commands.Bot,
    guild_id: int,
) -> list[tuple[str, int | None, str]]:
    """Returns ``[(feature_name, channel_id, disabled_by), …]``."""
    await _ensure_disabled_by_column(bot)
    async with bot.db.execute(
        "SELECT feature_name, channel_id, disabled_by FROM disabled_features "
        "WHERE guild_id = ? ORDER BY channel_id, feature_name",
        (guild_id,),
    ) as cursor:
        rows = await cursor.fetchall()
    return [(name, ch_id, source) for name, ch_id, source in rows]

async def enforce_opt_in_lockdown(bot: commands.Bot) -> None:
    """On startup, ensure opt-in features obey the code config.
    1. Wipe enabled_features rows for opt-in features that weren't bot-owner-enabled
       (cleans up falsely-enabled rows from before OPT_IN enforcement existed).
    2. Write a bot_owner-locked disabled_features row for any opt-in feature that
       is not currently enabled — blocks guild admins from enabling it."""
    await _ensure_disabled_by_column(bot)
    await _ensure_enabled_by_column(bot)

    # 1. Wipe falsely-enabled opt-in rows (enabled_by is NULL or != 'bot_owner')
    if OPT_IN_FEATURES:
        placeholders = ",".join("?" for _ in OPT_IN_FEATURES)
        await bot.db.execute(
            f"DELETE FROM enabled_features "
            f"WHERE feature_name IN ({placeholders}) "
            f"AND (enabled_by IS NULL OR enabled_by != 'bot_owner')",
            tuple(OPT_IN_FEATURES),
        )

    # 2. Lock down opt-in features not in enabled_features for every guild
    for guild in bot.guilds:
        async with bot.db.execute(
            "SELECT feature_name FROM enabled_features WHERE guild_id = ?",
            (guild.id,),
        ) as cursor:
            enabled = {row[0] for row in await cursor.fetchall()}

        for feature in OPT_IN_FEATURES:
            if feature not in enabled:
                await bot.db.execute(
                    "INSERT OR IGNORE INTO disabled_features "
                    "(guild_id, feature_name, channel_id, disabled_by) VALUES (?, ?, NULL, 'bot_owner')",
                    (guild.id, feature),
                )

    await bot.db.commit()