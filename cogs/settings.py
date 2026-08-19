"""Per-guild settings: log channel configuration."""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from common.constants import LOG_CHANNEL_CHOICES_DATA
from common.settings_store import clear_guild_setting, get_guild_setting, set_guild_setting

LOG_CHANNEL_CHOICES = [
    app_commands.Choice(name=name, value=value) for name, value in LOG_CHANNEL_CHOICES_DATA
]


class Settings(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="set_log_channel", description="Set the channel for a given type of log")
    @app_commands.describe(log_type="Which log this is for", channel="Channel to send these logs to")
    @app_commands.choices(log_type=LOG_CHANNEL_CHOICES)
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def set_log_channel(
        self,
        interaction: discord.Interaction,
        log_type: app_commands.Choice[str],
        channel: discord.TextChannel,
    ):
        bot_permissions = channel.permissions_for(interaction.guild.me)
        if not bot_permissions.send_messages or not bot_permissions.embed_links:
            await interaction.response.send_message(
                f"❌ I can't set {channel.mention} as the **{log_type.name}** channel — "
                f"I'm missing `Send Messages` or `Embed Links` there.",
                ephemeral=True,
            )
            return

        await set_guild_setting(self.bot, interaction.guild_id, log_type.value, str(channel.id))

        await interaction.response.send_message(
            f"✅ **{log_type.name}** will now be sent to {channel.mention}.",
            ephemeral=True,
        )

    @app_commands.command(name="clear_log_channel", description="Unset the channel for a given type of log")
    @app_commands.describe(log_type="Which log to clear")
    @app_commands.choices(log_type=LOG_CHANNEL_CHOICES)
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def clear_log_channel(self, interaction: discord.Interaction, log_type: app_commands.Choice[str]):
        cleared = await clear_guild_setting(self.bot, interaction.guild_id, log_type.value)
        if cleared:
            await interaction.response.send_message(f"✅ **{log_type.name}** channel cleared.", ephemeral=True)
        else:
            await interaction.response.send_message(f"**{log_type.name}** wasn't set.", ephemeral=True)

    @app_commands.command(name="log_channels", description="View the currently configured log channels")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def log_channels(self, interaction: discord.Interaction):
        lines = []
        for choice in LOG_CHANNEL_CHOICES:
            value = await get_guild_setting(self.bot, interaction.guild_id, choice.value)
            channel_text = f"<#{value}>" if value else "*not set*"
            lines.append(f"**{choice.name}**: {channel_text}")

        embed = discord.Embed(
            title="Log channels",
            description="\n".join(lines),
            color=discord.Color.blurple(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Settings(bot))
