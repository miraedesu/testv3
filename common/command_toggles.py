"""Per-guild command enable/disable toggles.

Supports guild-wide (channel_id IS NULL) and per-channel disables.
Opt-in commands must be in enabled_commands (guild-wide) to be active."""
from __future__ import annotations

from discord.ext import commands
from common.constants import OPT_IN_COMMANDS


async def is_command_disabled(
    bot: commands.Bot,
    guild_id: int,
    qualified_name: str,
    channel_id: int | None = None,
) -> bool:
    """True if the command (or any parent group) is disabled in *channel_id*
    (or guild-wide when None).  Also True for opt-in commands not yet enabled."""
    parts = qualified_name.split(" ")
    prefixes = [" ".join(parts[:i]) for i in range(len(parts), 0, -1)]
    placeholders = ",".join("?" for _ in prefixes)

    # 1. Explicit disable — channel-specific OR guild-wide, any prefix match
    async with bot.db.execute(
        f"SELECT 1 FROM disabled_commands "
        f"WHERE guild_id = ? AND command_name IN ({placeholders}) "
        f"AND (channel_id IS NULL OR channel_id IS ?) LIMIT 1",
        (guild_id, *prefixes, channel_id),
    ) as cursor:
        if await cursor.fetchone():
            return True

    # 2. Opt-in command not yet enabled (guild-wide only)
    base_cmd_name = parts[0]
    if base_cmd_name in OPT_IN_COMMANDS:
        async with bot.db.execute(
            "SELECT 1 FROM enabled_commands "
            "WHERE guild_id = ? AND command_name = ?",
            (guild_id, base_cmd_name),
        ) as cursor:
            if not await cursor.fetchone():
                return True
    return False

async def _ensure_disabled_by_column(bot):
    """Add disabled_by column to disabled_commands if it doesn't exist."""
    try:
        await bot.db.execute(
            "ALTER TABLE disabled_commands ADD COLUMN disabled_by TEXT DEFAULT 'guild_admin'"
        )
        await bot.db.commit()
    except Exception:
        pass  # Column already exists
async def _ensure_enabled_by_column(bot):
    """Add enabled_by column to enabled_commands if it doesn't exist."""
    try:
        await bot.db.execute(
            "ALTER TABLE enabled_commands ADD COLUMN enabled_by TEXT"
        )
        await bot.db.commit()
    except Exception:
        pass  # Column already exists
async def disable_command(
    bot: commands.Bot,
    guild_id: int,
    command_name: str,
    channel_id: int | None = None,
    disabled_by: str = "guild_admin",
) -> None:
    await _ensure_disabled_by_column(bot)
    await bot.db.execute(
        "INSERT OR IGNORE INTO disabled_commands "
        "(guild_id, command_name, channel_id, disabled_by) VALUES (?, ?, ?, ?)",
        (guild_id, command_name, channel_id, disabled_by),
    )
    await bot.db.commit()

async def enable_command(
    bot: commands.Bot,
    guild_id: int,
    command_name: str,
    channel_id: int | None = None,
    disabled_by: str = "guild_admin",
    enabled_by: str = "guild_admin",
) -> bool:
    """Removes the matching disable row (only rows set by the same source)
    and writes an enabled_commands entry.
    Returns True if a disable row was actually removed."""
    await _ensure_disabled_by_column(bot)
    await _ensure_enabled_by_column(bot)
    cursor = await bot.db.execute(
        "DELETE FROM disabled_commands "
        "WHERE guild_id = ? AND command_name = ? AND channel_id IS ? AND disabled_by = ?",
        (guild_id, command_name, channel_id, disabled_by),
    )
    await bot.db.execute(
        "INSERT INTO enabled_commands (guild_id, command_name, enabled_by) "
        "VALUES (?, ?, ?) "
        "ON CONFLICT(guild_id, command_name) DO UPDATE SET enabled_by = excluded.enabled_by",
        (guild_id, command_name, enabled_by),
    )
    await bot.db.commit()
    return cursor.rowcount > 0
async def list_disabled_commands(
    bot: commands.Bot,
    guild_id: int,
) -> list[tuple[str, int | None, str]]:
    """Returns ``[(command_name, channel_id, disabled_by), …]``."""
    await _ensure_disabled_by_column(bot)
    async with bot.db.execute(
        "SELECT command_name, channel_id, disabled_by FROM disabled_commands "
        "WHERE guild_id = ? ORDER BY command_name",
        (guild_id,),
    ) as cursor:
        rows = await cursor.fetchall()
    return [(name, ch_id, source) for name, ch_id, source in rows]

async def enforce_opt_in_lockdown(bot: commands.Bot) -> None:
    """On startup, ensure opt-in commands obey the code config.

    1. De-duplicate NULL-channel_id rows (SQLite treats NULL as distinct in PK).
    2. Wipe enabled_commands rows for opt-in commands that weren't bot-owner-enabled.
    3. Write a bot_owner-locked disabled_commands row for any opt-in command
       that is not currently enabled — uses NOT EXISTS instead of INSERT OR IGNORE
       because the latter doesn't dedupe NULL channel_id."""
    await _ensure_disabled_by_column(bot)
    await _ensure_enabled_by_column(bot)

    # 1. De-duplicate NULL-channel_id rows
    await bot.db.execute(
        "DELETE FROM disabled_commands WHERE rowid NOT IN ("
        "  SELECT MIN(rowid) FROM disabled_commands "
        "  GROUP BY guild_id, command_name, COALESCE(channel_id, -1)"
        ")"
    )

    # 2. Wipe falsely-enabled opt-in rows
    if OPT_IN_COMMANDS:
        placeholders = ",".join("?" for _ in OPT_IN_COMMANDS)
        await bot.db.execute(
            f"DELETE FROM enabled_commands "
            f"WHERE command_name IN ({placeholders}) "
            f"AND (enabled_by IS NULL OR enabled_by != 'bot_owner')",
            tuple(OPT_IN_COMMANDS),
        )

    # 3. Lock down opt-in commands not in enabled_commands for every guild
    for guild in bot.guilds:
        async with bot.db.execute(
            "SELECT command_name FROM enabled_commands WHERE guild_id = ?",
            (guild.id,),
        ) as cursor:
            enabled = {row[0] for row in await cursor.fetchall()}

        for command in OPT_IN_COMMANDS:
            if command not in enabled:
                await bot.db.execute(
                    "INSERT INTO disabled_commands "
                    "(guild_id, command_name, channel_id, disabled_by) "
                    "SELECT ?, ?, NULL, 'bot_owner' "
                    "WHERE NOT EXISTS ("
                    "  SELECT 1 FROM disabled_commands "
                    "  WHERE guild_id = ? AND command_name = ? AND channel_id IS NULL"
                    ")",
                    (guild.id, command, guild.id, command),
                )

    await bot.db.commit()

    await bot.db.commit()