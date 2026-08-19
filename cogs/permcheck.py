"""/permcheck: quickly see which roles (and channel overwrites) grant a given
permission, instead of manually clicking through every role in Server Settings.
"""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from common.constants import PER_PAGE, PERMISSION_DISPLAY_NAMES, friendly_permission_name
from cogs.member_events import describe_overwrite_value


async def permission_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    current_lower = current.lower()
    matches = [
        (snake_name, display_name)
        for snake_name, display_name in PERMISSION_DISPLAY_NAMES.items()
        if current_lower in display_name.lower() or current_lower in snake_name
    ]
    matches.sort(key=lambda pair: pair[1])
    return [
        app_commands.Choice(name=display_name, value=snake_name)
        # Discord's hard cap per autocomplete response
        for snake_name, display_name in matches[:25]
    ]


def get_channel_overrides_split(
    channel: discord.abc.GuildChannel, member: discord.Member
) -> tuple[list[str], list[str], list[str], list[str], bool]:
    # ── Member-specific (USER) overwrites ──────────────────────────
    member_ow = channel.overwrites_for(member)
    user_allowed: list[str] = []
    user_denied: list[str] = []
    member_ow_dict = dict(member_ow)
    for name, value in member_ow:
        if value is True:
            user_allowed.append(name)
        elif value is False:
            user_denied.append(name)

    # ── Role overwrites (aggregate, low → high role priority) ──────
    role_effective: dict[str, bool] = {}
    for role in member.roles:
        role_ow = channel.overwrites_for(role)
        for name, value in role_ow:
            if value is not None:
                role_effective[name] = value

    role_allowed = [n for n, v in role_effective.items() if v is True]
    role_denied = [n for n, v in role_effective.items() if v is False]

    # ── Net view_channel (user overwrite wins over role) ───────────
    member_view = member_ow_dict.get("view_channel")
    role_view = role_effective.get("view_channel")
    if member_view is not None:
        cannot_view = member_view is False
    elif role_view is not None:
        cannot_view = role_view is False
    else:
        cannot_view = False

    return user_allowed, user_denied, role_allowed, role_denied, cannot_view


class PermCheckPagination(discord.ui.View):
    def __init__(self, display_name: str, entries: list[str], author: discord.abc.User):
        super().__init__(timeout=120)
        self.display_name = display_name
        self.entries = entries  # pre-formatted lines, roles and channel hits mixed together
        self.author = author
        self.page = 0
        self.max_page = max((len(entries) - 1) // PER_PAGE, 0)
        self.message: discord.Message | None = None

    def create_embed(self) -> discord.Embed:
        start = self.page * PER_PAGE
        end = start + PER_PAGE
        page_entries = self.entries[start:end]

        description = "\n".join(page_entries) or "No matches found."

        embed = discord.Embed(
            title=f"Permission Check: {self.display_name}",
            description=description,
            color=discord.Color.blurple(),
        )
        embed.set_footer(
            text=f"Page {self.page + 1}/{self.max_page + 1} • {len(self.entries)} total")

        self.update_buttons()
        return embed

    def update_buttons(self):
        self.first.disabled = self.page == 0
        self.prev.disabled = self.page == 0
        self.next.disabled = self.page == self.max_page
        self.last.disabled = self.page == self.max_page

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user != self.author:
            await interaction.response.send_message("You can't control this menu.", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    @discord.ui.button(label="⏮", style=discord.ButtonStyle.gray)
    async def first(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = 0
        await interaction.response.edit_message(embed=self.create_embed(), view=self)

    @discord.ui.button(label="⬅", style=discord.ButtonStyle.gray)
    async def prev(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = max(self.page - 1, 0)
        await interaction.response.edit_message(embed=self.create_embed(), view=self)

    @discord.ui.button(label="➡", style=discord.ButtonStyle.blurple)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = min(self.page + 1, self.max_page)
        await interaction.response.edit_message(embed=self.create_embed(), view=self)

    @discord.ui.button(label="⏭", style=discord.ButtonStyle.gray)
    async def last(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = self.max_page
        await interaction.response.edit_message(embed=self.create_embed(), view=self)

    @discord.ui.button(label="⏹", style=discord.ButtonStyle.red)
    async def stop_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)


class PermCheck(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="permcheck",
        description="See which roles (and channel overwrites) grant a given permission",
    )
    @app_commands.describe(permission="Permission to check for")
    @app_commands.autocomplete(permission=permission_autocomplete)
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.default_permissions(administrator=True)
    async def permcheck(self, interaction: discord.Interaction, permission: str):
        if permission not in PERMISSION_DISPLAY_NAMES:
            await interaction.response.send_message(
                "Unrecognized permission -- pick one from the autocomplete list rather than typing it.",
                ephemeral=True,
            )
            return

        guild = interaction.guild
        display_name = friendly_permission_name(permission)

        entries: list[str] = []

        # --- Roles ---
        for role in guild.roles:
            has_directly = getattr(role.permissions, permission, False)
            has_via_admin = role.permissions.administrator and not has_directly
            if has_directly or has_via_admin:
                tag = " *(via Administrator)*" if has_via_admin else ""
                entries.append(f"Role: {role.mention}{tag}")

        # --- Channel overwrites (pure cache lookup -- no API calls) ---
        for channel in guild.channels:
            notes = []
            for target, overwrite in channel.overwrites.items():
                value = dict(overwrite).get(permission)
                if value is not None:
                    notes.append(
                        f"{target.mention}: {describe_overwrite_value(value)}")
            if notes:
                entries.append(
                    f"Channel: {channel.mention} — {', '.join(notes)}")

        if not entries:
            await interaction.response.send_message(
                f"No roles or channel overwrites reference **{display_name}**.", ephemeral=True
            )
            return

        view = PermCheckPagination(display_name, entries, interaction.user)
        await interaction.response.send_message(embed=view.create_embed(), view=view, ephemeral=True)
        view.message = await interaction.original_response()

    @app_commands.command(
        name="permcheck_user",
        description="See a member's effective permissions, and which channels override them",
    )
    @app_commands.describe(member="Member to check")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.default_permissions(administrator=True)
    async def permcheck_user(self, interaction: discord.Interaction, member: discord.Member):
        guild = interaction.guild
        base_perms = member.guild_permissions

        entries: list[str] = []

        # ── Server-wide summary (unchanged) ───────────────────────────
        if base_perms.administrator:
            entries.append(
                "This member has **Administrator** — all permissions everywhere.")
        else:
            granted_names = sorted(
                friendly_permission_name(name) for name, value in dict(base_perms).items() if value
            )
            entries.append(
                "**Server-wide permissions:** " +
                (", ".join(granted_names) if granted_names else "none")
            )

        # spacer between the summary and per-channel results
        entries.append("")

        # ── Per-channel overrides: USER first, then ROLES ─────────────
        found_channels = False

        for channel in guild.channels:
            try:
                user_allowed, user_denied, role_allowed, role_denied, cannot_view = (
                    get_channel_overrides_split(channel, member)
                )
            except (discord.ClientException, AttributeError):
                continue

            has_user = bool(user_allowed or user_denied)
            has_role = bool(role_allowed or role_denied)

            if not has_user and not has_role:
                continue  # no explicit overrides on this channel

            found_channels = True

            # Build sub-lines: USER section first, then ROLE section
            sub_lines: list[str] = []

            if cannot_view:
                sub_lines.append("🚫 **Cannot view this channel**")

            # --- USER overwrites ---
            if has_user:
                user_parts: list[str] = []
                if user_allowed:
                    user_parts.append(
                        "✅ " + ", ".join(friendly_permission_name(p) for p in user_allowed)
                    )
                if user_denied:
                    user_parts.append(
                        "❌ " + ", ".join(friendly_permission_name(p) for p in user_denied)
                    )
                sub_lines.append("👤 **User** — " + " | ".join(user_parts))

            # --- ROLE overwrites ---
            if has_role:
                role_parts: list[str] = []
                if role_allowed:
                    role_parts.append(
                        "✅ " + ", ".join(friendly_permission_name(p) for p in role_allowed)
                    )
                if role_denied:
                    role_parts.append(
                        "❌ " + ", ".join(friendly_permission_name(p) for p in role_denied)
                    )
                sub_lines.append("🎭 **Roles** — " + " | ".join(role_parts))

            # Channel header + indented sub-lines
            entries.append(f"**Channel: {channel.mention}**")
            for line in sub_lines:
                entries.append(f"  └ {line}")

        if not found_channels:
            entries.append("No channel-specific overrides affect this member.")

        view = PermCheckPagination(
            member.display_name, entries, interaction.user)
        await interaction.response.send_message(embed=view.create_embed(), view=view, ephemeral=True)
        view.message = await interaction.original_response()


async def setup(bot: commands.Bot):
    await bot.add_cog(PermCheck(bot))