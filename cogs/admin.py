"""Bot-owner-only utilities: per-guild command disable/enable."""
from __future__ import annotations

import importlib
import sys

import discord
from discord import app_commands
from discord.ext import commands
from common.constants import FEATURE_CHOICES_DATA, DEV_GUILD_ID
from common.command_toggles import disable_command, enable_command, list_disabled_commands
from common.feature_toggles import disable_feature, enable_feature, list_disabled_features
import logging


FEATURE_CHOICES = [app_commands.Choice(
    name=name, value=value) for name, value in FEATURE_CHOICES_DATA]

logger = logging.getLogger(__name__)

def _collect_all_command_names(tree: app_commands.CommandTree) -> list[str]:
    """Every registered command AND group, as its full qualified path
    (e.g. "cr", "cr add", "cr pool add") -- disabling a group name disables
    everything under it"""
    names: list[str] = []

    def walk(cmd, prefix: str = ""):
        full_name = f"{prefix}{cmd.name}".strip()
        names.append(full_name)
        if isinstance(cmd, app_commands.Group):
            for sub in cmd.commands:
                walk(sub, prefix=f"{full_name} ")

    for cmd in tree.get_commands():
        walk(cmd)
    return names


def resolve_guild(bot: commands.Bot, server: str) -> discord.Guild | None:
    """Resolve a guild by ID (as string) or by name (case-insensitive)."""
    if server.isdigit():
        return bot.get_guild(int(server))
    lowered = server.lower()
    exact = [g for g in bot.guilds if g.name.lower() == lowered]
    if exact:
        return exact[0]
    partial = [g for g in bot.guilds if lowered in g.name.lower()]
    return partial[0] if len(partial) == 1 else None

async def guild_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    current_lower = current.lower()
    matches = [
        g for g in interaction.client.guilds
        if current_lower in g.name.lower()
    ]
    return [
        app_commands.Choice(name=f"{g.name} ({g.id})", value=str(g.id))
        for g in matches[:25]
    ]


async def command_name_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    all_names = _collect_all_command_names(interaction.client.tree)
    current_lower = current.lower()
    matches = sorted(
        name for name in all_names if current_lower in name.lower())
    return [app_commands.Choice(name=name, value=name) for name in matches[:25]]


# def is_bot_owner():
#     async def predicate(interaction: discord.Interaction) -> bool:
#         if interaction.guild_id != DEV_GUILD_ID:
#             return False
#         return await interaction.client.is_owner(interaction.user)
#     return app_commands.check(predicate)
def is_bot_owner():
    async def predicate(interaction: discord.Interaction) -> bool:
        # Only check if the user is the bot owner, regardless of which server it is
        return await interaction.client.is_owner(interaction.user)
    return app_commands.check(predicate)

async def cog_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    current_lower = current.lower()
    names = ["all", "common"] + sorted(interaction.client.extensions.keys())
    matches = [name for name in names if current_lower in name.lower()]
    return [app_commands.Choice(name=name, value=name) for name in matches[:25]]

class Admin(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
    def _reload_common_modules(self) -> tuple[list[str], list[str]]:
        """Reloads all common.* modules. Returns (succeeded, failed)."""
        succeeded = []
        failed = []
        common_modules = sorted([name for name in sys.modules if name.startswith("common.")])
        for mod_name in common_modules:
            try:
                importlib.reload(sys.modules[mod_name])
                succeeded.append(mod_name)
            except Exception as e:
                failed.append(f"`{mod_name}`: {e}")
        return succeeded, failed
    admin_group = app_commands.Group(
        name="admin",
        description="Bot owner utilities",
        default_permissions=discord.Permissions(administrator=True),
        guild_ids=[DEV_GUILD_ID],
    )

    @admin_group.command(name="disable_feature", description="Disable a passive feature/filter in a target server (bot owner only)")
    @app_commands.describe(server="Server ID or name to apply this to", feature="Feature to disable")
    @app_commands.choices(feature=FEATURE_CHOICES)
    @app_commands.autocomplete(server=guild_autocomplete)
    @is_bot_owner()
    async def disable_feature_cmd(self, interaction: discord.Interaction, server: str, feature: app_commands.Choice[str]):
        guild = resolve_guild(self.bot, server)
        if guild is None:
            await interaction.response.send_message(f"Couldn't resolve a server matching `{server}`.", ephemeral=True)
            return
        await disable_feature(self.bot, guild.id, feature.value, disabled_by="bot_owner")
        await interaction.response.send_message(f"Disabled **{feature.name}** in **{guild.name}** (`{guild.id}`).", ephemeral=True)

    @admin_group.command(name="list_disabled_features", description="List features disabled in a target server (bot owner only)")
    @app_commands.describe(server="Server ID or name to check")
    @app_commands.autocomplete(server=guild_autocomplete)
    @is_bot_owner()
    async def list_disabled_features_cmd(self, interaction: discord.Interaction, server: str):
        guild = resolve_guild(self.bot, server)
        if guild is None:
            await interaction.response.send_message(f"Couldn't resolve a server matching `{server}`.", ephemeral=True)
            return

        rows = await list_disabled_features(self.bot, guild.id)
        if not rows:
            await interaction.response.send_message(f"No features are disabled in **{guild.name}**.", ephemeral=True)
            return

        name_lookup = {value: name for name, value in FEATURE_CHOICES_DATA}
        lines: list[str] = []
        for value, channel_id, disabled_by in rows:
            label = name_lookup.get(value, value)
            if channel_id is None:
                lines.append(f"**{label}** *(server-wide)* — by `{disabled_by}`")
            else:
                ch = guild.get_channel(channel_id)
                scope = ch.mention if ch else f"`{channel_id}` (deleted)"
                lines.append(f"**{label}** — in {scope} — by `{disabled_by}`")

        embed = discord.Embed(
            title=f"Disabled Features — {guild.name}",
            description="\n".join(lines)[:4096],
            color=discord.Color.red(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
    @admin_group.command(name="disable_command", description="Disable a command in a target server (bot owner only)")
    @app_commands.describe(
        server="Server ID or name to apply this to",
        command="Command to disable (disabling a group disables all its subcommands)",
    )
    @app_commands.autocomplete(server=guild_autocomplete, command=command_name_autocomplete)
    @is_bot_owner()
    async def disable_command_cmd(self, interaction: discord.Interaction, server: str, command: str):
        guild = resolve_guild(self.bot, server)
        if guild is None:
            await interaction.response.send_message(f"Couldn't resolve a server matching `{server}`.", ephemeral=True)
            return
        await disable_command(self.bot, guild.id, command, disabled_by="bot_owner")
        await interaction.response.send_message(f"Disabled `/{command}` in **{guild.name}** (`{guild.id}`).", ephemeral=True)

    @admin_group.command(name="enable_command", description="Re-enable a previously disabled command in a target server (bot owner only)")
    @app_commands.describe(server="Server ID or name to apply this to", command="Command to re-enable")
    @app_commands.autocomplete(server=guild_autocomplete, command=command_name_autocomplete)
    @is_bot_owner()
    async def enable_command_cmd(self, interaction: discord.Interaction, server: str, command: str):
        guild = resolve_guild(self.bot, server)
        if guild is None:
            await interaction.response.send_message(f"Couldn't resolve a server matching `{server}`.", ephemeral=True)
            return
        was_disabled = await enable_command(self.bot, guild.id, command, disabled_by="bot_owner")
        if was_disabled:
            await interaction.response.send_message(f"Re-enabled `/{command}` in **{guild.name}** (`{guild.id}`).", ephemeral=True)
        else:
            await interaction.response.send_message(f"`/{command}` wasn't disabled in **{guild.name}**.", ephemeral=True)

    @admin_group.command(name="list_disabled", description="List commands disabled in a target server (bot owner only)")
    @app_commands.describe(server="Server ID or name to check")
    @app_commands.autocomplete(server=guild_autocomplete)
    @is_bot_owner()
    async def list_disabled_cmd(self, interaction: discord.Interaction, server: str):
        guild = resolve_guild(self.bot, server)
        if guild is None:
            await interaction.response.send_message(f"Couldn't resolve a server matching `{server}`.", ephemeral=True)
            return

        rows = await list_disabled_commands(self.bot, guild.id)
        if not rows:
            await interaction.response.send_message(f"No commands are disabled in **{guild.name}**.", ephemeral=True)
            return

        lines: list[str] = []
        for name, channel_id, disabled_by in rows:
            scope_label = "*(server-wide)*" if channel_id is None else None
            if channel_id is not None:
                ch = guild.get_channel(channel_id)
                scope_label = ch.mention if ch else f"`{channel_id}` (deleted)"
            scope_str = f" — in {scope_label}" if scope_label and scope_label != "*(server-wide)*" else ""
            lines.append(f"`/{name}` {scope_label or ''}{scope_str} — by `{disabled_by}`")

        embed = discord.Embed(
            title=f"Disabled Commands — {guild.name}",
            description="\n".join(lines)[:4096],
            color=discord.Color.red(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
    @admin_group.command(name="reload", description="Reload a cog, all cogs, or common modules")
    @app_commands.describe(
        cog="Which cog to reload (or 'all' or 'common')",
        sync="Also re-sync slash commands after reloading",
    )
    @app_commands.autocomplete(cog=cog_autocomplete)
    @app_commands.guild_only()
    @is_bot_owner()
    async def reload(self, interaction: discord.Interaction, cog: str, sync: bool = False):
        await interaction.response.defer(ephemeral=True)

        lines = []

        # --- Reload common modules ---
        if cog == "common":
            common_ok, common_fail = self._reload_common_modules()
            if common_ok:
                lines.append("✅ Reloaded common: " + ", ".join(f"`{s}`" for s in common_ok))
            if common_fail:
                lines.append("❌ Common failed:\n" + "\n".join(common_fail))

            # After reloading common, reload ALL cogs so they pick up new references
            targets = list(self.bot.extensions.keys())
            succeeded = []
            failed = []
            for ext in targets:
                try:
                    await self.bot.reload_extension(ext)
                    succeeded.append(ext)
                except Exception as e:
                    failed.append(f"`{ext}`: {e}")
            if succeeded:
                lines.append("✅ Reloaded cogs: " + ", ".join(f"`{s}`" for s in succeeded))
            if failed:
                lines.append("❌ Cog failed:\n" + "\n".join(failed))

            if sync:
                try:
                    # Sync global
                    synced_global = await self.bot.tree.sync()
                    # Sync dev guild
                    dev_guild = discord.Object(id=DEV_GUILD_ID)
                    synced_guild = await self.bot.tree.sync(guild=dev_guild)
                    lines.append(f"🔄 Re-synced {len(synced_global)} global and {len(synced_guild)} dev guild commands.")
                except discord.HTTPException as e:
                    lines.append(f"⚠️ Reload succeeded but sync failed: {e}")

            await interaction.followup.send("\n".join(lines)[:2000], ephemeral=True)
            return

        # --- Reload single or all cogs ---
        if cog == "all":
            targets = list(self.bot.extensions.keys())
        elif cog in self.bot.extensions:
            targets = [cog]
        else:
            await interaction.followup.send(f"`{cog}` isn't currently loaded.", ephemeral=True)
            return

        succeeded = []
        failed = []
        for ext in targets:
            try:
                await self.bot.reload_extension(ext)
                succeeded.append(ext)
            except Exception as e:
                failed.append(f"`{ext}`: {e}")

        if succeeded:
            lines.append("✅ Reloaded: " + ", ".join(f"`{s}`" for s in succeeded))
        if failed:
            lines.append("❌ Failed:\n" + "\n".join(failed))

        if sync:
            try:
                # Sync global
                synced_global = await self.bot.tree.sync()
                # Sync dev guild
                dev_guild = discord.Object(id=DEV_GUILD_ID)
                synced_guild = await self.bot.tree.sync(guild=dev_guild)
                lines.append(f"🔄 Re-synced {len(synced_global)} global and {len(synced_guild)} dev guild commands.")
            except discord.HTTPException as e:
                lines.append(f"⚠️ Reload succeeded but sync failed: {e}")

        await interaction.followup.send("\n".join(lines)[:2000], ephemeral=True)

    def _get_available_cogs(self) -> list[str]:
        """Scan the cogs/ directory for available .py files."""
        import os
        cogs = []
        if os.path.exists("cogs"):
            for filename in os.listdir("cogs"):
                if filename.endswith(".py") and not filename.startswith("_"):
                    cogs.append(f"cogs.{filename[:-3]}")
        return sorted(cogs)

    async def available_cogs_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        all_cogs = self._get_available_cogs()
        current_lower = current.lower()
        matches = [cog for cog in all_cogs if current_lower in cog.lower()]
        return [app_commands.Choice(name=cog, value=cog) for cog in matches[:25]]

    @admin_group.command(name="cog_enable", description="Load a cog dynamically and save it to startup")
    @app_commands.describe(cog="Which cog to enable")
    @app_commands.autocomplete(cog=available_cogs_autocomplete)
    @is_bot_owner()
    async def cog_enable(self, interaction: discord.Interaction, cog: str):
        if cog in self.bot.extensions:
            await interaction.response.send_message(f"✅ `{cog}` is already loaded.", ephemeral=True)
            return
            
        try:
            await self.bot.load_extension(cog)
            await self.bot.db.execute("INSERT OR IGNORE INTO enabled_cogs (cog_name) VALUES (?)", (cog,))
            await self.bot.db.commit()
            await interaction.response.send_message(f"✅ Enabled and loaded `{cog}`. It will load on next restart.", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"❌ Failed to load `{cog}`: {e}", ephemeral=True)

    @admin_group.command(name="cog_disable", description="Unload a cog dynamically and remove it from startup")
    @app_commands.describe(cog="Which cog to disable")
    @app_commands.autocomplete(cog=available_cogs_autocomplete)
    @is_bot_owner()
    async def cog_disable(self, interaction: discord.Interaction, cog: str):
        if cog in ("cogs.admin", "cogs.db"):
            await interaction.response.send_message("❌ You cannot disable core cogs!", ephemeral=True)
            return
            
        if cog not in self.bot.extensions:
            await interaction.response.send_message(f"❌ `{cog}` is not currently loaded.", ephemeral=True)
            return
            
        try:
            await self.bot.unload_extension(cog)
            await self.bot.db.execute("DELETE FROM enabled_cogs WHERE cog_name = ?", (cog,))
            await self.bot.db.commit()
            await interaction.response.send_message(f"✅ Disabled and unloaded `{cog}`. It will not load on next restart.", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"❌ Failed to unload `{cog}`: {e}", ephemeral=True)

    @admin_group.command(name="cog_list", description="List all available and running cogs")
    @is_bot_owner()
    async def cog_list(self, interaction: discord.Interaction):
        all_cogs = self._get_available_cogs()
        loaded_cogs = list(self.bot.extensions.keys())
        
        loaded_text = "\n".join(f"🟢 `{c}`" for c in sorted(loaded_cogs)) or "None loaded"
        disabled_cogs = [c for c in all_cogs if c not in loaded_cogs]
        disabled_text = "\n".join(f"🔴 `{c}`" for c in disabled_cogs) or "None disabled"
        
        embed = discord.Embed(title="⚙️ Cog Status", color=discord.Color.blue())
        embed.add_field(name="Loaded", value=loaded_text[:1024], inline=False)
        embed.add_field(name="Disabled", value=disabled_text[:1024], inline=False)
        
        await interaction.response.send_message(embed=embed, ephemeral=True)
    @admin_group.command(name="enable_feature", description="Enable an opt-in feature for a server")
    @app_commands.describe(server="Server ID or name", feature="Feature to enable")
    @app_commands.choices(feature=FEATURE_CHOICES)
    @app_commands.autocomplete(server=guild_autocomplete)
    @is_bot_owner()
    async def enable_feature_cmd(self, interaction: discord.Interaction, server: str, feature: app_commands.Choice[str]):
        guild = resolve_guild(self.bot, server)
        if not guild:
            await interaction.response.send_message("Server not found.", ephemeral=True)
            return
        await enable_feature(self.bot, guild.id, feature.value, disabled_by="bot_owner")
        await interaction.response.send_message(f"✅ Enabled {feature.name} in {guild.name}.", ephemeral=True)
        
    @admin_group.command(name="delete_name", description="Remove a specific name from a user's WhoIs history (bot owner only)")
    @app_commands.describe(
        user_id="The ID of the user whose name you want to delete",
        name="The exact name to remove (e.g., 'OldName#1234' or 'NewName')"
    )
    @is_bot_owner()
    async def delete_name_cmd(self, interaction: discord.Interaction, user_id: str, name: str):
        if not user_id.isdigit():
            await interaction.response.send_message("❌ Invalid user ID. Must be numbers only.", ephemeral=True)
            return
            
        uid = int(user_id)
        name_clean = name.strip()
        
        cursor = await self.bot.db.execute(
            "DELETE FROM username_history WHERE user_id = ? AND username = ?",
            (uid, name_clean)
        )
        await self.bot.db.commit()
        
        if cursor.rowcount > 0:
            # Log the deletion for safety/audit purposes
            logger.warning(
                f"Admin {interaction.user} ({interaction.user.id}) deleted {cursor.rowcount} instance(s) of name '{name_clean}' from user ID {uid} in guild {interaction.guild_id}."
            )
            await interaction.response.send_message(
                f"✅ Removed {cursor.rowcount} instance(s) of `{name_clean}` from the username history of `{uid}`.", 
                ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"❌ No entries found for `{name_clean}` under user ID `{uid}`.\n"
                f"Make sure you typed the name exactly as it appears in `/whois names`.",
                ephemeral=True
            )

async def setup(bot: commands.Bot):
    await bot.add_cog(Admin(bot))
