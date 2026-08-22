"""Per-guild configuration: ALLOWED_ROLE for automod, plus per-channel/guild
disable toggles for features and commands."""
from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from common.constants import FEATURE_CHOICES_DATA, OPT_IN_FEATURES, OPT_IN_COMMANDS
from common.feature_toggles import (
    disable_feature as db_disable_feature,
    enable_feature as db_enable_feature,
    list_disabled_features,
)
from common.command_toggles import (
    disable_command as db_disable_command,
    enable_command as db_enable_command,
    list_disabled_commands,
)

logger = logging.getLogger(__name__)

async def _is_bot_owner_disabled(bot, guild_id: int, name: str, is_feature: bool) -> bool:
    """Check if a command/feature is disabled by the bot owner (global disable)."""
    table = "disabled_features" if is_feature else "disabled_commands"
    name_col = "feature_name" if is_feature else "command_name"
    async with bot.db.execute(
        f"SELECT 1 FROM {table} WHERE guild_id = ? AND {name_col} = ? "
        f"AND channel_id IS NULL AND disabled_by = 'bot_owner' LIMIT 1",
        (guild_id, name),
    ) as cursor:
        return await cursor.fetchone() is not None

def _is_bot_owner_command(cmd) -> bool:
    """Detect commands guarded by the is_bot_owner() check from cogs.admin."""
    for check in getattr(cmd, "checks", []):
        qualname = getattr(check, "__qualname__", "")
        if "is_bot_owner" in qualname:
            return True
    return False

# ── ALLOWED_ROLE helpers ────────────────────────────────────────────────
async def get_allowed_role_id(bot, guild_id: int) -> int | None:
    async with bot.db.execute(
        "SELECT setting_value FROM guild_settings "
        "WHERE guild_id = ? AND setting_key = 'allowed_role_id'",
        (guild_id,),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        return None
    try:
        return int(row[0])
    except (ValueError, TypeError):
        return None


async def set_allowed_role_id(bot, guild_id: int, role_id: int | None) -> None:
    if role_id is None:
        await bot.db.execute(
            "DELETE FROM guild_settings "
            "WHERE guild_id = ? AND setting_key = 'allowed_role_id'",
            (guild_id,),
        )
    else:
        await bot.db.execute(
            "INSERT INTO guild_settings (guild_id, setting_key, setting_value) "
            "VALUES (?, 'allowed_role_id', ?) "
            "ON CONFLICT(guild_id, setting_key) "
            "DO UPDATE SET setting_value = excluded.setting_value",
            (guild_id, str(role_id)),
        )
    await bot.db.commit()
# ── BOOST_LIST_MENTION helpers ──────────────────────────────────────────
async def get_boost_list_mention_id(bot, guild_id: int) -> int | None:
    async with bot.db.execute(
        "SELECT setting_value FROM guild_settings "
        "WHERE guild_id = ? AND setting_key = 'boost_list_mention_user_id'",
        (guild_id,),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        return None
    try:
        return int(row[0])
    except (ValueError, TypeError):
        return None


async def set_boost_list_mention_id(bot, guild_id: int, user_id: int | None) -> None:
    if user_id is None:
        await bot.db.execute(
            "DELETE FROM guild_settings "
            "WHERE guild_id = ? AND setting_key = 'boost_list_mention_user_id'",
            (guild_id,),
        )
    else:
        await bot.db.execute(
            "INSERT INTO guild_settings (guild_id, setting_key, setting_value) "
            "VALUES (?, 'boost_list_mention_user_id', ?) "
            "ON CONFLICT(guild_id, setting_key) "
            "DO UPDATE SET setting_value = excluded.setting_value",
            (guild_id, str(user_id)),
        )
    await bot.db.commit()

async def resolve_allowed_role(bot, guild: discord.Guild) -> discord.Role | None:
    """Per-guild configured role, falling back to the default-name lookup."""
    role_id = await get_allowed_role_id(bot, guild.id)
    if role_id is not None:
        return guild.get_role(role_id)
    from common.constants import ALLOWED_ROLE
    return discord.utils.get(guild.roles, name=ALLOWED_ROLE)


# ── Autocomplete ───────────────────────────────────────────────────────
async def feature_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    cl = current.lower()
    return [
        app_commands.Choice(name=label, value=value)
        for label, value in FEATURE_CHOICES_DATA
        if cl in label.lower() or cl in value
    ][:25]


async def command_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    seen: set[str] = set()

    # Walk BOTH global commands and guild-specific commands —
    # tree.get_commands(guild=...) only returns one set, not the merge
    stack: list[app_commands.Command | app_commands.Group] = []
    stack.extend(interaction.client.tree.get_commands(guild=None))  # global
    if interaction.guild:
        stack.extend(interaction.client.tree.get_commands(guild=discord.Object(id=interaction.guild.id)))

    while stack:
        cmd = stack.pop()
        if _is_bot_owner_command(cmd):
            continue
        seen.add(cmd.qualified_name)
        if isinstance(cmd, app_commands.Group):
            stack.extend(cmd.commands)

    cl = current.lower()
    return [
        app_commands.Choice(name=name, value=name)
        for name in sorted(seen)
        if cl in name.lower()
    ][:25]


async def role_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    """Lists every role in the guild including @everyone."""
    cl = current.lower()
    choices: list[app_commands.Choice[str]] = []
    for role in interaction.guild.roles:
        if cl in role.name.lower() or cl in str(role.id):
            label = role.name if not role.is_default() else "@everyone"
            choices.append(app_commands.Choice(name=label, value=str(role.id)))
        if len(choices) >= 25:
            break
    return choices


# ── Cog ────────────────────────────────────────────────────────────────
class GuildSettings(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    group = app_commands.Group(
        name="guild_settings",
        description="Configure per-guild settings (allowed role, feature/command toggles)",
        default_permissions=discord.Permissions(administrator=True),
    )

    # ── allowed_role ────────────────────────────────────────────────
    @group.command(name="allowed_role", description="Set the role that bypasses the non-member link filter")
    @app_commands.describe(role_id="Role to allow (type to search — @everyone is valid)")
    @app_commands.autocomplete(role_id=role_autocomplete)
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.guild_only()
    async def allowed_role(self, interaction: discord.Interaction, role_id: str):
        try:
            rid = int(role_id)
        except ValueError:
            await interaction.response.send_message("❌ Invalid role.", ephemeral=True)
            return

        role = interaction.guild.get_role(rid)
        if role is None:
            await interaction.response.send_message("❌ Role not found in this server.", ephemeral=True)
            return

        await set_allowed_role_id(self.bot, interaction.guild.id, rid)
        label = "@everyone" if role.is_default() else role.mention
        await interaction.response.send_message(
            f"✅ Allowed role set to {label}.", ephemeral=True
        )

    @group.command(name="allowed_role_view", description="Show the currently configured allowed role")
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.guild_only()
    async def allowed_role_view(self, interaction: discord.Interaction):
        current_id = await get_allowed_role_id(self.bot, interaction.guild.id)
        if current_id is None:
            await interaction.response.send_message(
                "No allowed role configured. Falling back to the default "
                "(role named 'Members' if present).",
                ephemeral=True,
            )
            return
        current = interaction.guild.get_role(current_id)
        label = (
            current.mention if current and not current.is_default()
            else "@everyone" if current and current.is_default()
            else f"`{current_id}` (deleted)"
        )
        await interaction.response.send_message(
            f"Current allowed role: {label}", ephemeral=True
        )

    @group.command(name="allowed_role_clear", description="Clear guild allowed role")
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.guild_only()
    async def allowed_role_clear(self, interaction: discord.Interaction):
        await set_allowed_role_id(self.bot, interaction.guild.id, None)
        await interaction.response.send_message(
            "✅ Per-guild allowed role cleared. Default fallback will be used.",
            ephemeral=True,
        )
    # ── boost_list_mention ───────────────────────────────────────────
    @group.command(name="boost_list_mention", description="Set who gets pinged for boost deadline reminders")
    @app_commands.describe(user="User to ping when boost deadlines approach")
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.guild_only()
    async def boost_list_mention_set(self, interaction: discord.Interaction, user: discord.User):
        await set_boost_list_mention_id(self.bot, interaction.guild.id, user.id)
        await interaction.response.send_message(
            f"✅ Boost deadline reminders will ping {user.mention}.",
            ephemeral=True,
        )

    @group.command(name="boost_list_mention_view", description="Show who gets pinged for boost deadline reminders")
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.guild_only()
    async def boost_list_mention_view(self, interaction: discord.Interaction):
        uid = await get_boost_list_mention_id(self.bot, interaction.guild.id)
        if uid is None:
            await interaction.response.send_message(
                "No boost mention user set. Deadline reminders will be skipped until you set one.",
                ephemeral=True,
            )
            return
        user = interaction.guild.get_member(uid) or self.bot.get_user(uid)
        label = user.mention if user else f"`{uid}` (not found)"
        await interaction.response.send_message(
            f"Boost deadline reminders ping: {label}", ephemeral=True
        )

    @group.command(name="boost_list_mention_clear", description="Clear the boost deadline reminder ping user")
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.guild_only()
    async def boost_list_mention_clear(self, interaction: discord.Interaction):
        await set_boost_list_mention_id(self.bot, interaction.guild.id, None)
        await interaction.response.send_message(
            "✅ Boost mention user cleared. Reminders will be skipped until set.",
            ephemeral=True,
        )
    @group.command(name="boost_edit", description="Attribute a 'Boost Count Changed' embed to a user (alternative to the button)")
    @app_commands.describe(
        message_id="Message ID of the Boost Count Changed embed in server-log",
        user="User who removed their boost",
    )
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.guild_only()
    async def boost_edit(
        self, interaction: discord.Interaction, message_id: str, user: discord.User,
    ):
        from common.settings_store import get_log_channel
        from cogs.member_events import apply_boost_attribution

        log_channel = await get_log_channel(self.bot, interaction.guild.id, "server-log")
        if log_channel is None:
            await interaction.response.send_message(
                "❌ No server-log channel set for this guild.", ephemeral=True
            )
            return

        try:
            msg = await log_channel.fetch_message(int(message_id))
        except (ValueError, discord.NotFound, discord.HTTPException):
            await interaction.response.send_message(
                "❌ Message not found in server-log.", ephemeral=True
            )
            return

        if not msg.embeds or "Boost Count Changed" not in (msg.embeds[0].title or ""):
            await interaction.response.send_message(
                "❌ That message doesn't look like a boost drift embed.",
                ephemeral=True,
            )
            return

        ok = await apply_boost_attribution(
            self.bot, msg, user.id, interaction.user, interaction.guild,
        )
        if ok:
            await interaction.response.send_message(
                f"✅ Attributed boost removal to {user.mention}. "
                f"Embed updated and boost_list adjusted (if they were tracked).",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                "❌ Could not edit the message — it may be deleted or I lack permissions.",
                ephemeral=True,
            )
    # ── disable_feature / enable_feature ────────────────────────────
    @group.command(name="disable_feature", description="Disable a feature for #channel or the whole server")
    @app_commands.describe(
        feature="Feature to disable",
        channel="Channel to disable in (omit for server-wide)",
    )
    @app_commands.autocomplete(feature=feature_autocomplete)
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.guild_only()
    async def disable_feature(
        self,
        interaction: discord.Interaction,
        feature: str,
        channel: discord.TextChannel | discord.VoiceChannel | discord.ForumChannel | None = None,
    ):
        if feature not in {v for _, v in FEATURE_CHOICES_DATA}:
            await interaction.response.send_message(
                "❌ Unknown feature — pick one from the autocomplete list.",
                ephemeral=True,
            )
            return
        await db_disable_feature(self.bot, interaction.guild.id, feature, channel.id if channel else None)
        scope = "server-wide" if channel is None else f"in {channel.mention}"
        await interaction.response.send_message(
            f"✅ `{feature}` disabled {scope}.", ephemeral=True
        )

    @group.command(name="enable_feature", description="Re-enable a previously disabled feature")
    @app_commands.describe(
        feature="Feature to enable",
        channel="Channel scope to clear (omit for server-wide)",
    )
    @app_commands.autocomplete(feature=feature_autocomplete)
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.guild_only()
    async def enable_feature(
        self,
        interaction: discord.Interaction,
        feature: str,
        channel: discord.TextChannel | discord.VoiceChannel | discord.ForumChannel | None = None,
    ):
        # Block if bot owner globally disabled this feature
        if await _is_bot_owner_disabled(self.bot, interaction.guild.id, feature, is_feature=True):
            await interaction.response.send_message(
                f"🔒 `{feature}` was globally disabled by the bot owner and cannot be re-enabled here.",
                ephemeral=True,
            )
            return
        # Opt-in features can only be initially enabled by the bot owner.
        # Guild admins may still clear their own channel-specific disables
        # of an already-enabled opt-in feature.
        if feature in OPT_IN_FEATURES:
            async with self.bot.db.execute(
                "SELECT 1 FROM enabled_features WHERE guild_id = ? AND feature_name = ?",
                (interaction.guild.id, feature),
            ) as cursor:
                if not await cursor.fetchone():
                    await interaction.response.send_message(
                        f"🔒 `{feature}` is an opt-in feature — only the bot owner can enable it "
                        f"(use `/admin enable_feature`).",
                        ephemeral=True,
                    )
                    return
        removed = await db_enable_feature(self.bot, interaction.guild.id, feature, channel.id if channel else None)
        scope = "server-wide" if channel is None else f"in {channel.mention}"
        await interaction.response.send_message(
            f"✅ `{feature}` enabled {scope}."
            if removed
            else f"ℹ️ `{feature}` was not disabled {scope}.",
            ephemeral=True,
        )
    # ── disable_command / enable_command ───────────────────────────
    @group.command(name="disable_command", description="Disable a command in #channel or server-wide")
    @app_commands.describe(
        command="Command to disable (use autocomplete)",
        channel="Channel to disable in (omit for server-wide)",
    )
    @app_commands.autocomplete(command=command_autocomplete)
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.guild_only()
    async def disable_command(
        self,
        interaction: discord.Interaction,
        command: str,
        channel: discord.TextChannel | discord.VoiceChannel | discord.ForumChannel | None = None,
    ):
        await db_disable_command(self.bot, interaction.guild.id, command, channel.id if channel else None)
        scope = "server-wide" if channel is None else f"in {channel.mention}"
        await interaction.response.send_message(
            f"✅ `/{command}` disabled {scope}.", ephemeral=True
        )

    @group.command(name="enable_command", description="Re-enable a previously disabled command")
    @app_commands.describe(
        command="Command to enable (use autocomplete)",
        channel="Channel scope to clear (omit for server-wide)",
    )
    @app_commands.autocomplete(command=command_autocomplete)
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.guild_only()
    async def enable_command(
        self,
        interaction: discord.Interaction,
        command: str,
        channel: discord.TextChannel | discord.VoiceChannel | discord.ForumChannel | None = None,
    ):
        # Block if bot owner globally disabled this command
        if await _is_bot_owner_disabled(self.bot, interaction.guild.id, command, is_feature=False):
            await interaction.response.send_message(
                f"🔒 `/{command}` was globally disabled by the bot owner and cannot be re-enabled here.",
                ephemeral=True,
            )
            return
        # Opt-in commands can only be initially enabled by the bot owner.
        base_cmd = command.split()[0] if command else command
        if base_cmd in OPT_IN_COMMANDS:
            async with self.bot.db.execute(
                "SELECT 1 FROM enabled_commands WHERE guild_id = ? AND command_name = ?",
                (interaction.guild.id, base_cmd),
            ) as cursor:
                if not await cursor.fetchone():
                    await interaction.response.send_message(
                        f"🔒 `/{command}` is an opt-in command — only the bot owner can enable it "
                        f"(use `/admin enable_command`).",
                        ephemeral=True,
                    )
                    return
        removed = await db_enable_command(self.bot, interaction.guild.id, command, channel.id if channel else None)
        scope = "server-wide" if channel is None else f"in {channel.mention}"
        await interaction.response.send_message(
            f"✅ `/{command}` enabled {scope}."
            if removed
            else f"ℹ️ `/{command}` was not disabled {scope}.",
            ephemeral=True,
        )
    # ── list ─────────────────────────────────────────────────────────
    @group.command(name="list", description="Show all current guild settings")
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.guild_only()
    async def list_settings(self, interaction: discord.Interaction):
        guild = interaction.guild

        allowed_id = await get_allowed_role_id(self.bot, guild.id)
        if allowed_id is not None:
            ar = guild.get_role(allowed_id)
            allowed_str = (
                "@everyone" if ar and ar.is_default()
                else ar.mention if ar
                else f"`{allowed_id}` (deleted)"
            )
        else:
            allowed_str = "*(unset — using default fallback)*"

        feature_rows = await list_disabled_features(self.bot, guild.id)
        command_rows = await list_disabled_commands(self.bot, guild.id)

        def _group(rows):
            by_chan: dict[str, list[str]] = {}
            for name, ch_id, source in rows:
                if ch_id is None:
                    key = "Server-wide"
                else:
                    ch = guild.get_channel(ch_id)
                    key = ch.mention if ch else f"`{ch_id}` (deleted)"
                tag = " 🔒" if source == "bot_owner" else ""
                by_chan.setdefault(key, []).append(f"`{name}`{tag}")
            # Server-wide first, then channels alphabetically
            return dict(
                sorted(by_chan.items(), key=lambda kv: (kv[0] != "Server-wide", kv[0]))
            )

        def _render(by_chan):
            if not by_chan:
                return "*(none)*"
            lines = []
            for scope, items in by_chan.items():
                lines.append(f"**{scope}** ({len(items)}): " + ", ".join(items))
            return "\n".join(lines)[:1024]

        embed = discord.Embed(
            title="Guild Settings",
            color=discord.Color.blurple(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Allowed Role", value=allowed_str, inline=False)
        embed.add_field(name="Disabled Features", value=_render(_group(feature_rows)), inline=False)
        embed.add_field(name="Disabled Commands", value=_render(_group(command_rows)), inline=False)
        embed.set_footer(text=f"Guild ID: {guild.id} • 🔒 = bot-owner locked")
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(GuildSettings(bot))