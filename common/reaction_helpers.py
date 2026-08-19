"""Shared helpers for the custom-reactions feature: attachment saving/
validation, link extraction, phrase-pool substitution, and building the
final send payload. Used by cogs/reactions.py, and by cogs/moderation.py
when it triggers a matched reaction from on_message.
"""
from __future__ import annotations

import asyncio
import re
import uuid
from pathlib import Path

import aiohttp
import discord
from discord.ext import commands

from common.constants import (
    ALLOWED_IMAGE_EXTENSIONS,
    ATTACHMENTS_DIR,
    CAPTURE_TIMEOUT,
    DISCORD_CDN_RE,
    EXT_BY_CONTENT_TYPE,
    MAX_FILES_PER_MESSAGE,
    MAX_REPLY_LENGTH,
    PLACEHOLDER_RE,
)


# -------------------------------
# ATTACHMENT HELPERS
# -------------------------------
def is_allowed_image(filename: str, content_type: str | None) -> bool:
    """Prefers Discord/HTTP's reported content-type (based on the real file
    signature, not just the name) when available; falls back to the extension."""
    if content_type is not None:
        return content_type.split(";")[0].strip().lower() in EXT_BY_CONTENT_TYPE
    return Path(filename).suffix.lower() in ALLOWED_IMAGE_EXTENSIONS


async def save_attachment(attachment: discord.Attachment, guild_id: int) -> tuple[str, str] | None:
    """Downloads a Discord attachment to local disk (images only) so it survives
    CDN link expiry. Returns (path_on_disk, original_filename), or None if the
    attachment isn't a recognized image type."""
    if not is_allowed_image(attachment.filename, attachment.content_type):
        return None
    guild_dir = ATTACHMENTS_DIR / str(guild_id)
    await asyncio.to_thread(guild_dir.mkdir, parents=True, exist_ok=True)
    ext = Path(attachment.filename).suffix.lower()
    local_path = guild_dir / f"{uuid.uuid4().hex}{ext}"
    # Attachment.save() would do its own blocking file write internally --
    # read the bytes via discord.py's async HTTP call instead, then do the
    # actual disk write ourselves in a thread so it can't stall the event loop.
    data = await attachment.read()
    await asyncio.to_thread(local_path.write_bytes, data)
    return str(local_path), attachment.filename


async def save_from_url(url: str, guild_id: int) -> tuple[str, str] | None:
    """Downloads an image from a direct URL (including Discord CDN links) to
    local disk, so the copy doesn't depend on that link staying valid. Returns
    (path, filename), or None if the download failed or isn't a supported image."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return None
                content_type = resp.content_type
                data = await resp.read()
    except aiohttp.ClientError:
        return None

    original_name = url.split("?")[0].rsplit("/", 1)[-1] or "image"
    if not is_allowed_image(original_name, content_type):
        return None

    guild_dir = ATTACHMENTS_DIR / str(guild_id)
    await asyncio.to_thread(guild_dir.mkdir, parents=True, exist_ok=True)
    ext = Path(original_name).suffix.lower(
    ) or EXT_BY_CONTENT_TYPE.get(content_type, "")
    local_path = guild_dir / f"{uuid.uuid4().hex}{ext}"
    await asyncio.to_thread(local_path.write_bytes, data)
    return str(local_path), original_name


async def capture_content(
    bot: commands.Bot, interaction: discord.Interaction, prefix: str = ""
) -> tuple[str | None, list[discord.Attachment]]:
    """Prompts the user and waits for their follow-up text/attachment(s) message.
    `prefix` is shown before the prompt (e.g. to confirm which entry is being
    edited). Returns (None, []) if the capture failed/timed out/was empty
    (error already sent)."""
    await interaction.response.send_message(
        f"{prefix}What should the reaction say?"
        "(use `%user%` in the text to ping whoever triggers it)"
        "Type cancel to stop command.",
        ephemeral=True,
    )

    def check(m: discord.Message):
        return m.author == interaction.user and m.channel == interaction.channel

    try:
        msg = await bot.wait_for("message", check=check, timeout=CAPTURE_TIMEOUT)
    except asyncio.TimeoutError:
        await interaction.followup.send("You took too long. Command canceled.", ephemeral=True)
        return None, []
    if msg.content.strip().lower() == "cancel":
        await interaction.followup.send("Command canceled.", ephemeral=True)
        return None, []

    text = msg.content.strip()[:MAX_REPLY_LENGTH] or None
    attachments = msg.attachments

    if not text and not attachments:
        await interaction.followup.send("Error: you must provide text or an attachment!", ephemeral=True)
        return None, []

    return text, attachments


async def resolve_images(
    text: str | None, attachments: list[discord.Attachment], guild_id: int
) -> tuple[str | None, list[dict], int]:
    """Saves every attachment and every Discord CDN link found in `text`.
    Returns (remaining_caption_text, [{"path", "name", "url"}, ...], skipped_count)
    for whichever attachments were valid images; invalid ones are skipped and counted."""
    images = []
    skipped = 0

    for attachment in attachments:
        result = await save_attachment(attachment, guild_id)
        if result:
            path, name = result
            images.append({"path": path, "name": name, "url": attachment.url})
        else:
            skipped += 1

    remaining = text
    if remaining:
        removed_any = False
        # Walk matches right-to-left so removing text doesn't shift earlier indices.
        for match in reversed(list(DISCORD_CDN_RE.finditer(remaining))):
            result = await save_from_url(match.group(0), guild_id)
            if result:
                path, name = result
                images.insert(
                    0, {"path": path, "name": name, "url": match.group(0)})
                remaining = remaining[:match.start()] + remaining[match.end():]
                removed_any = True
        if removed_any:
            # Only clean up runs of spaces/tabs left behind at the exact spot
            # a link was cut out -- newlines (blank lines, paragraph breaks,
            # etc. the user actually typed) are left completely untouched.
            remaining = re.sub(r"[ \t]{2,}", " ", remaining)
        remaining = remaining.strip() or None

    return remaining, images, skipped


def build_image_payload(images: list[tuple]) -> tuple[list[discord.File], list[str]]:
    """Always sends images as real file attachments (never as a raw URL in
    message content), since a plain-text link makes Discord show a link
    chip above the image preview -- a genuine attachment renders clean with
    no chip, just text + image. Returns (files, skipped_originals) where
    skipped_originals lists any original_url that couldn't be attached
    (e.g. local file missing) so the caller can decide whether to fall back.
    """
    files: list[discord.File] = []
    skipped: list[str] = []

    for attachment_path, attachment_name, original_url in images[:MAX_FILES_PER_MESSAGE]:
        if attachment_path:
            try:
                files.append(discord.File(
                    attachment_path, filename=attachment_name))
                continue
            except FileNotFoundError:
                pass  # local copy missing; try original_url next
        if original_url:
            skipped.append(original_url)

    return files, skipped


async def resolve_by_name_or_id(
    bot: commands.Bot, guild_id: int, id: int | None, name: str | None
) -> tuple[int | None, str | None]:
    """Resolves an /cr edit or /cr show call that can take either `id` or
    `name`. Returns (resolved_id, error_message):
      - if `id` was given, it's returned as-is (no DB lookup needed here --
        the caller still needs to verify it actually exists).
      - if `name` was given and matches exactly one variant, that variant's
        id is returned.
      - if `name` matches zero or multiple variants, resolved_id is None and
        error_message explains what to do (including a pick-list for the
        multiple-variant case, since a keyword can have several)."""
    if id is not None:
        return id, None
    if name is None:
        return None, "Please provide either `id` or `name`."

    name_clean = name.lower().strip()
    async with bot.db.execute(
        "SELECT id, reply_text FROM custom_reactions WHERE guild_id = ? AND name = ? ORDER BY id",
        (guild_id, name_clean),
    ) as cursor:
        rows = await cursor.fetchall()

    if not rows:
        return None, f"No entries found for **{name_clean}**."

    if len(rows) == 1:
        return rows[0][0], None

    lines = [
        f"**{rid}** — {(reply_text or '(image only)')[:60]}" for rid, reply_text in rows]
    listing = "\n".join(lines)
    return None, f"**{name_clean}** has {len(rows)} variants -- specify which one with `id`:\n{listing}"


# -------------------------------
# PHRASE POOL SUBSTITUTION (%morning%, %emote%, ...)
# -------------------------------
async def pick_random_phrase(bot: commands.Bot, pool_name: str, guild_id: int) -> str | None:
    async with bot.db.execute(
        "SELECT phrase FROM phrase_pools WHERE guild_id = ? AND pool_name = ? ORDER BY RANDOM() LIMIT 1",
        (guild_id, pool_name),
    ) as cursor:
        row = await cursor.fetchone()
    return row[0] if row else None


async def expand_placeholders(bot: commands.Bot, text: str | None, guild_id: int) -> str | None:
    """Replaces each %pool_name% placeholder (other than the reserved %user%)
    with an independently random phrase from that pool. A placeholder with no
    matching pool (or an empty one) is left as literal text, so a typo or an
    empty pool is visible instead of silently disappearing."""
    if not text:
        return text

    pieces = []
    last_end = 0
    for match in PLACEHOLDER_RE.finditer(text):
        pool_name = match.group(1).lower()
        if pool_name == "user":
            continue  # handled separately via message.author.mention
        phrase = await pick_random_phrase(bot, pool_name, guild_id)
        if phrase is None:
            continue
        pieces.append(text[last_end:match.start()])
        pieces.append(phrase)
        last_end = match.end()
    pieces.append(text[last_end:])
    return "".join(pieces)
