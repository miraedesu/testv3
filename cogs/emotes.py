"""Application emoji management: /emote upload / steal / url / list."""
from __future__ import annotations

import re
import asyncio

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from cogs.admin import is_bot_owner

EMOJI_RE = re.compile(r"<(a?):(\w+):(\d+)>")
EMOJI_PER_PAGE = 8
# Only allow image content types
ALLOWED_CONTENT_TYPES = {
    "image/png",
    "image/jpeg",
    "image/jpg",
    "image/gif",
    "image/webp",
}
MAX_EMOJI_SIZE = 256 * 1024  # 256 KB


class EmojiPagination(discord.ui.View):
    def __init__(self, data: list[tuple], author: discord.abc.User):
        super().__init__(timeout=120)
        self.data = data
        self.author = author
        self.page = 0
        self.max_page = max((len(data) - 1) // EMOJI_PER_PAGE, 0)
        self.message: discord.Message | None = None

    def create_embed(self) -> discord.Embed:
        start = self.page * EMOJI_PER_PAGE
        end = start + EMOJI_PER_PAGE
        entries = self.data[start:end]

        lines = [f"{emoji_str} **{name}** — `{emoji_str}`" for _id,
                 name, emoji_str, _animated in entries]
        description = "\n\n".join(lines) or "No entries on this page."

        embed = discord.Embed(
            title=f"Application Emojis ({len(self.data)} total)",
            description=description,
            color=discord.Color.green(),
        )
        embed.set_footer(text=f"Page {self.page + 1}/{self.max_page + 1}")

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
        # If the author clicks stop, stop the view and disable all buttons
        if interaction.user == self.author:
            self.stop()
            for child in self.children:
                child.disabled = True
            await interaction.response.edit_message(view=self)
        else:
            # Non-author gets told they can't use this
            await interaction.response.send_message(
                "You can't control this menu.", ephemeral=True
            )


async def download_image_from_url(url: str) -> tuple[bytes | None, str | None]:
    """
    Downloads an image from a URL.
    Returns (image_bytes, error_message).
    On success: (bytes, None)
    On failure: (None, error_string)
    """
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return (None, f"Download failed (HTTP {resp.status}).")

                content_type = resp.content_type
                if content_type not in ALLOWED_CONTENT_TYPES:
                    return (None, f"URL must point to an image (got `{content_type}`). "
                                   "Allowed: PNG, JPG, GIF, WEBP.")

                image_bytes = await resp.read()

                if len(image_bytes) > MAX_EMOJI_SIZE:
                    return (None, f"Image is too large ({len(image_bytes) // 1024} KB). "
                                   f"Maximum is {MAX_EMOJI_SIZE // 1024} KB.")

                return (image_bytes, None)
    except aiohttp.ClientConnectorError:
        return (None, "Couldn't connect to that URL. Check if it's accessible.")
    except asyncio.TimeoutError:
        return (None, "Download timed out. The URL took too long to respond.")
    except Exception as e:
        return (None, f"Unexpected error: {e}")


class Emotes(commands.Cog):
    """All subcommands here are admin-only, so the group-level
    default_permissions (unlike /cr's) is appropriate -- there's no open
    subcommand it would incorrectly hide."""

    emote = app_commands.Group(
        name="emote",
        description="Manage application emojis.",
        default_permissions=discord.Permissions(administrator=True),
    )

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ---- /emote upload ----

    @emote.command(name="upload", description="Upload an application emoji from a file.")
    @app_commands.describe(
        name="Name for the emoji (2-32 chars, alphanumeric/underscores)",
        image="The image file to upload (PNG/JPG/GIF, max 256KB)",
    )
    @is_bot_owner()
    async def emote_upload(self, interaction: discord.Interaction, name: str, image: discord.Attachment):
        await interaction.response.defer(ephemeral=True)

        if image.size > MAX_EMOJI_SIZE:
            await interaction.followup.send(
                f"That file is too large ({image.size // 1024} KB). "
                f"Emoji images must be {MAX_EMOJI_SIZE // 1024} KB or smaller.",
                ephemeral=True,
            )
            return

        if image.content_type not in ALLOWED_CONTENT_TYPES:
            await interaction.followup.send(
                f"File must be an image (PNG/JPG/GIF/WEBP). Got: `{image.content_type}`",
                ephemeral=True,
            )
            return

        try:
            image_bytes = await image.read()
            new_emoji = await self.bot.create_application_emoji(name=name, image=image_bytes)
        except discord.HTTPException as e:
            await interaction.followup.send(f"Failed to upload emoji: {e}", ephemeral=True)
            return

        await interaction.followup.send(
            f"Uploaded {new_emoji} as `:{new_emoji.name}:` (ID: {new_emoji.id})\n"
            f"Copy: `{new_emoji}`",
            ephemeral=True,
        )

    # ---- /emote url ----

    @emote.command(name="url", description="Upload an application emoji from a direct image URL.")
    @app_commands.describe(
        name="Name for the emoji (2-32 chars, alphanumeric/underscores)",
        url="Direct image URL (must end in .png/.jpg/.gif/.webp)",
    )
    @is_bot_owner()
    async def emote_url(self, interaction: discord.Interaction, name: str, url: str):
        await interaction.response.defer(ephemeral=True)

        # Basic URL validation
        if not url.startswith(("http://", "https://")):
            await interaction.followup.send(
                "URL must start with `http://` or `https://`.",
                ephemeral=True,
            )
            return

        image_bytes, error = await download_image_from_url(url)

        if error:
            await interaction.followup.send(f"❌ {error}", ephemeral=True)
            return

        try:
            new_emoji = await self.bot.create_application_emoji(name=name, image=image_bytes)
        except discord.HTTPException as e:
            await interaction.followup.send(f"Failed to upload emoji: {e}", ephemeral=True)
            return

        await interaction.followup.send(
            f"Uploaded {new_emoji} as `:{new_emoji.name}:` (ID: {new_emoji.id})\n"
            f"Copy: `{new_emoji}`",
            ephemeral=True,
        )

    # ---- /emote steal ----

    @emote.command(
        name="steal",
        description="Copy an existing custom emoji from any server into app's emojis.",
    )
    @app_commands.describe(
        name="Name for the new emoji (2-32 chars)",
        emoji="Paste the existing custom emoji",
    )
    @is_bot_owner()
    async def emote_steal(self, interaction: discord.Interaction, name: str, emoji: str):
        await interaction.response.defer(ephemeral=True)

        match = EMOJI_RE.search(emoji)
        if not match:
            await interaction.followup.send(
                "That doesn't look like a custom emoji. Make sure you paste the "
                "actual emoji (it should look like `<:name:123456789012345678>`).",
                ephemeral=True,
            )
            return

        animated, _src_name, emoji_id = match.groups()
        ext = "gif" if animated else "png"
        url = f"https://cdn.discordapp.com/emojis/{emoji_id}.{ext}"

        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    await interaction.followup.send(
                        f"Couldn't download that emoji from Discord's CDN (status {resp.status}).",
                        ephemeral=True,
                    )
                    return
                image_bytes = await resp.read()

        if len(image_bytes) > MAX_EMOJI_SIZE:
            await interaction.followup.send(
                f"That emoji is too large ({len(image_bytes) // 1024} KB). "
                f"Discord won't accept it as an application emoji.",
                ephemeral=True,
            )
            return

        try:
            new_emoji = await self.bot.create_application_emoji(name=name, image=image_bytes)
        except discord.HTTPException as e:
            await interaction.followup.send(f"Failed to upload emoji: {e}", ephemeral=True)
            return

        await interaction.followup.send(
            f"Stole {new_emoji} and saved it as `:{new_emoji.name}:` (ID: {new_emoji.id})\n"
            f"Copy: `{new_emoji}`",
            ephemeral=True,
        )

    # ---- /emote list ----

    @emote.command(name="list", description="List all application emojis.")
    @is_bot_owner()
    async def emote_list(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        try:
            emojis = await self.bot.fetch_application_emojis()
        except discord.HTTPException as e:
            await interaction.followup.send(f"Failed to fetch emojis: {e}", ephemeral=True)
            return

        if not emojis:
            await interaction.followup.send("No application emojis found.", ephemeral=True)
            return

        data = [(e.id, e.name, str(e), e.animated) for e in emojis]

        view = EmojiPagination(data, interaction.user)
        embed = view.create_embed()
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        view.message = await interaction.original_response()


async def setup(bot: commands.Bot):
    await bot.add_cog(Emotes(bot))