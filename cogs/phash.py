"""Perceptual-hash (pHash) tooling for scam image detection.

Provides owner-only commands to:
- Compute a pHash for any image URL (Discord CDN attachments, etc.)
- Save a raw pHash hex string directly (when you only have the hash
  from an automod-log embed, no source image available)
- Maintain a persistent blocklist of known-scam image hashes
- Match a new image's pHash against the blocklist (Hamming distance)
- Compare two arbitrary image URLs side-by-side

The blocklist is the long-lived asset — every scam image you confirm makes
future detection instant, no OCR or heuristics needed.
"""
from __future__ import annotations

import asyncio
import io
import logging
import re

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from PIL import Image, UnidentifiedImageError
import imagehash
from cogs.admin import is_bot_owner

logger = logging.getLogger(__name__)

# <= 10 = visually very similar (variant of the same image / scam template).
# Tunable per-command via /phash match threshold:N.
PHASH_DISTANCE_THRESHOLD = 10

# Placeholder source_url for manually-entered hashes (the column is NOT NULL).
MANUAL_SOURCE_PLACEHOLDER = "(manual entry)"

# Matches Discord jump-message URLs in any of these forms:
#   https://discord.com/channels/{guild}/{channel}/{message}
#   https://ptb.discord.com/channels/...
#   https://canary.discord.com/channels/...
#   https://discordapp.com/channels/...
# Channel ID can also be a DM channel ID (no guild).
MESSAGE_LINK_RE = re.compile(
    r"^https?://(?:ptb\.|canary\.)?discord(?:app)?\.com/channels/"
    r"(?:\d+|@me)/(?P<channel_id>\d+)/(?P<message_id>\d+)/?$"
)

# Only these URL prefixes are accepted for inline-URL hashing, to prevent
# SSRF (the bot will never fetch arbitrary internet hosts via this command).
_DISCORD_CDN_PREFIXES = (
    "https://cdn.discordapp.com/",
    "https://media.discordapp.net/",
)

# ----------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------
async def _fetch_image_bytes(
    url: str, session: aiohttp.ClientSession
) -> bytes:
    """Download image bytes with a sane timeout, reusing a shared session."""
    async with session.get(url) as resp:
        resp.raise_for_status()
        return await resp.read()


def _compute_phash(image_bytes: bytes) -> str:
    """Synchronous, CPU-bound pHash computation.
    Returns the 64-bit hash as a 16-char hex string. Call via
    asyncio.to_thread so the event loop isn't blocked."""
    with Image.open(io.BytesIO(image_bytes)) as img:
        return str(imagehash.phash(img))


def _validate_phash_hex(s: str) -> bool:
    """True if *s* is a valid pHash hex string (parses without raising).
    imagehash uses 16 hex chars for a 64-bit pHash."""
    if not s:
        return False
    try:
        imagehash.hex_to_hash(s)
        return True
    except ValueError:
        return False


def _hamming_distance(hex_a: str, hex_b: str) -> int | None:
    """Hamming distance between two pHash hex strings.
    Returns None if either string is malformed."""
    try:
        return imagehash.hex_to_hash(hex_a) - imagehash.hex_to_hash(hex_b)
    except ValueError:
        return None


# ----------------------------------------------------------------
# Cog
# ----------------------------------------------------------------
class Phash(commands.Cog):
    """pHash tooling and blocklist management (bot owner only)."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._session: aiohttp.ClientSession | None = None

    async def cog_unload(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()

    async def _get_session(self) -> aiohttp.ClientSession:
        """Lazily-created, reusable client session for image fetches."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15),
                headers={"User-Agent": "testv3-phash/1.0"},
            )
        return self._session

    phash = app_commands.Group(
        name="phash",
        description="Perceptual hash tools for scam image detection (owner only)",
        default_permissions=discord.Permissions(administrator=True),
    )

    # ---------------- get ----------------

    @phash.command(name="get", description="Compute the pHash of an image URL")
    @app_commands.describe(
        url="Direct image URL (e.g. a Discord attachment link)",
        save="Also save this hash to the blocklist (default: false)",
        note="Optional note (e.g. 'Mr Beast giveaway variant 3')",
    )
    @is_bot_owner()
    async def phash_get(
        self,
        interaction: discord.Interaction,
        url: str,
        save: bool = False,
        note: str | None = None,
    ):
        await interaction.response.defer(ephemeral=True)
        session = await self._get_session()

        try:
            image_bytes = await _fetch_image_bytes(url, session)
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            await interaction.followup.send(
                f"❌ Failed to download image: `{e}`", ephemeral=True
            )
            return

        try:
            phash_str = await asyncio.to_thread(_compute_phash, image_bytes)
        except (UnidentifiedImageError, OSError) as e:
            await interaction.followup.send(
                f"❌ Could not parse image: `{e}`", ephemeral=True
            )
            return

        embed = discord.Embed(
            title="🧷 Image pHash",
            color=discord.Color.pink(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="URL", value=f"`{url[:300]}`", inline=False)
        embed.add_field(name="pHash", value=f"`{phash_str}`", inline=False)
        embed.add_field(
            name="Size",
            value=f"{len(image_bytes):,} bytes ({len(image_bytes) / 1024:.1f} KB)",
            inline=True,
        )
        embed.set_thumbnail(url=url)

        if save:
            saved_id = await self._insert_phash(
                phash_str, url, interaction.user.id, note
            )
            if saved_id is not None:
                embed.add_field(
                    name="Saved",
                    value=f"✅ Stored in blocklist (ID `{saved_id}`) — future matches will find it.",
                    inline=False,
                )
            else:
                embed.add_field(
                    name="Save failed",
                    value=f"❌ pHash `{phash_str}` may already be in the blocklist.",
                    inline=False,
                )

        await interaction.followup.send(embed=embed, ephemeral=True)

    # ---------------- fetch (from message ID or link) ----------------

    @phash.command(
        name="fetch",
        description="Compute pHash from a message's images (by message link or ID)",
    )
    @app_commands.describe(
        message="Message link, or just the message ID if in this channel",
        save="Also save every image pHash to the blocklist (default: false)",
        note="Optional note when saving (e.g. 'Mr Beast giveaway variant 3')",
    )
    @is_bot_owner()
    async def phash_fetch(
        self,
        interaction: discord.Interaction,
        message: str,
        save: bool = False,
        note: str | None = None,
    ):
        # --- Parse the input: link, bare message ID, or junk ---
        raw = message.strip()
        m = MESSAGE_LINK_RE.match(raw)
        if m:
            channel_id = int(m.group("channel_id"))
            message_id = int(m.group("message_id"))
        elif raw.isdigit():
            if interaction.channel is None or not isinstance(
                interaction.channel,
                (discord.TextChannel, discord.Thread, discord.VoiceChannel,
                 discord.StageChannel, discord.ForumChannel),
            ):
                await interaction.response.send_message(
                    "❌ Bare message IDs only work inside a text channel — "
                    "use a full message link instead.",
                    ephemeral=True,
                )
                return
            channel_id = interaction.channel.id
            message_id = int(raw)
        else:
            await interaction.response.send_message(
                "❌ Input must be a Discord message link (`https://discord.com/channels/...`) "
                "or a numeric message ID.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        # --- Resolve the channel (cache first, then fetch) ---
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except discord.NotFound:
                await interaction.followup.send(
                    f"❌ Channel `{channel_id}` not found.", ephemeral=True
                )
                return
            except discord.Forbidden:
                await interaction.followup.send(
                    f"❌ No permission to view channel `{channel_id}`.",
                    ephemeral=True,
                )
                return

        # Only fetch from text-like channels (security: no fetching from
        # arbitrary guild channels we can't reasonably read messages in).
        if not isinstance(
            channel,
            (discord.TextChannel, discord.Thread, discord.VoiceChannel,
             discord.StageChannel, discord.ForumChannel, discord.DMChannel,
             discord.GroupChannel),
        ):
            await interaction.followup.send(
                "❌ That channel isn't a text/voice/DM channel.",
                ephemeral=True,
            )
            return

        # --- Fetch the message ---
        try:
            target_msg = await channel.fetch_message(message_id)
        except discord.NotFound:
            await interaction.followup.send(
                f"❌ Message `{message_id}` not found in that channel.",
                ephemeral=True,
            )
            return
        except discord.Forbidden:
            await interaction.followup.send(
                "❌ No permission to read messages in that channel.",
                ephemeral=True,
            )
            return
        except discord.HTTPException as e:
            await interaction.followup.send(
                f"❌ Discord error fetching message: `{e}`", ephemeral=True
            )
            return

        # --- Collect images (attachments + inline Discord-CDN URLs) ---
        # Attachments are inherently Discord-CDN-hosted, so always safe.
        image_atts: list[discord.Attachment] = []
        inline_urls: list[str] = []

        def _is_image(att: discord.Attachment) -> bool:
            if att.content_type and att.content_type.startswith("image"):
                return True
            # Fallback: many scam PNGs/JPGs lack content_type but have extensions
            return att.filename.lower().endswith(
                (".png", ".jpg", ".jpeg", ".webp", ".gif")
            )

        def _harvest_embeds(embeds: list[discord.Embed]) -> None:
            for emb in embeds:
                for cand in (emb.image, emb.thumbnail):
                    if cand is None or not cand.url:
                        continue
                    if cand.url.startswith(_DISCORD_CDN_PREFIXES):
                        inline_urls.append(cand.url)

        # (a) the message itself
        image_atts.extend(att for att in target_msg.attachments if _is_image(att))
        _harvest_embeds(target_msg.embeds)

        # (b) forwarded messages — discord.py exposes them via message_snapshots
        snapshots = getattr(target_msg, "message_snapshots", None) or []
        for snap in snapshots:
            snap_msg = snap.message
            image_atts.extend(att for att in snap_msg.attachments if _is_image(att))
            _harvest_embeds(snap_msg.embeds)

        # Dedupe inline URLs (preserve order)
        inline_urls = list(dict.fromkeys(inline_urls))

        if not image_atts and not inline_urls:
            await interaction.followup.send(
                "❌ That message has no Discord-CDN image attachments or embeds.\n"
                "Only `cdn.discordapp.com` / `media.discordapp.net` URLs are "
                "accepted for safety.",
                ephemeral=True,
            )
            return

        # --- Compute pHash for every source ---
        session = await self._get_session()

        # Each entry: (source_label, source_url, phash_str, size_bytes,
        #              kind, saved_id, error_str)
        results: list[tuple[str, str, str, int, str, int | None, str | None]] = []
        saved_count = 0

        # Attachments
        for att in image_atts:
            label = f"attachment `{att.filename}`"
            try:
                att_bytes = await att.read()
                p_hash = await asyncio.to_thread(_compute_phash, att_bytes)
            except (UnidentifiedImageError, OSError) as e:
                results.append((label, att.url, "", 0, "attachment", None, str(e)))
                continue
            except Exception as e:
                results.append((label, att.url, "", 0, "attachment", None, str(e)))
                continue

            saved_id = None
            if save:
                saved_id = await self._insert_phash(
                    p_hash, att.url, interaction.user.id, note
                )
                if saved_id is not None:
                    saved_count += 1
            results.append((label, att.url, p_hash, len(att_bytes), "attachment", saved_id, None))

        # Inline URLs (already validated as Discord CDN)
        for url in inline_urls:
            label = f"embed `{url[:60]}{'…' if len(url) > 60 else ''}`"
            try:
                image_bytes = await _fetch_image_bytes(url, session)
                p_hash = await asyncio.to_thread(_compute_phash, image_bytes)
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                results.append((label, url, "", 0, "embed", None, f"download: {e}"))
                continue
            except (UnidentifiedImageError, OSError) as e:
                results.append((label, url, "", 0, "embed", None, str(e)))
                continue
            except Exception as e:
                results.append((label, url, "", 0, "embed", None, str(e)))
                continue

            saved_id = None
            if save:
                saved_id = await self._insert_phash(
                    p_hash, url, interaction.user.id, note
                )
                if saved_id is not None:
                    saved_count += 1
            results.append((label, url, p_hash, len(image_bytes), "embed", saved_id, None))

        # --- Build the response embed ---
        embed = discord.Embed(
            title="🧷 pHash from message",
            color=discord.Color.pink(),
            timestamp=discord.utils.utcnow(),
            url=target_msg.jump_url,
        )
        embed.add_field(
            name="Source",
            value=f"[Jump to message]({target_msg.jump_url})\n"
                  f"Author: {target_msg.author.mention} (`{target_msg.author.id}`)",
            inline=False,
        )

        ok_lines: list[str] = []
        fail_lines: list[str] = []
        for label, _url, p_hash, size, kind, saved_id, err in results:
            if err is not None:
                fail_lines.append(f"❌ {label}: {err}")
            else:
                line = f"`{p_hash}` — {kind}, {size:,} B"
                if saved_id is not None:
                    line += f"  ✅ saved #{saved_id}"
                elif save:
                    line += "  ⚠️ already in blocklist"
                ok_lines.append(line)

        if ok_lines:
            embed.add_field(
                name=f"Computed ({len(ok_lines)})",
                value="\n".join(ok_lines)[:1024],
                inline=False,
            )
        if fail_lines:
            embed.add_field(
                name=f"Failed ({len(fail_lines)})",
                value="\n".join(fail_lines)[:1024],
                inline=False,
            )

        if save:
            if saved_count:
                embed.add_field(
                    name="Blocklist",
                    value=(
                        f"✅ {saved_count} new entr"
                        f"{'y' if saved_count == 1 else 'ies'} added."
                    ),
                    inline=False,
                )
            else:
                embed.add_field(
                    name="Blocklist",
                    value="ℹ️ No new entries (all hashes were already present).",
                    inline=False,
                )

        # Thumbnail = first computed image (nice for quick visual confirm)
        for _label, src_url, p_hash, _size, _kind, _sid, _err in results:
            if _err is None and p_hash:
                embed.set_thumbnail(url=src_url)
                break

        embed.set_footer(
            text=(
                f"Fetched by {interaction.user} · "
                f"{len(image_atts)} attachment(s), {len(inline_urls)} embed URL(s)"
            )
        )

        await interaction.followup.send(embed=embed, ephemeral=True)
    # ---------------- save (from URL) ----------------

    @phash.command(name="save", description="Compute pHash from URL and add to blocklist")
    @app_commands.describe(
        url="Direct image URL (e.g. a Discord attachment link)",
        note="Optional note (e.g. 'Mr Beast giveaway variant 3')",
    )
    @is_bot_owner()
    async def phash_save(
        self,
        interaction: discord.Interaction,
        url: str,
        note: str | None = None,
    ):
        await interaction.response.defer(ephemeral=True)
        session = await self._get_session()

        try:
            image_bytes = await _fetch_image_bytes(url, session)
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            await interaction.followup.send(
                f"❌ Failed to download image: `{e}`", ephemeral=True
            )
            return

        try:
            phash_str = await asyncio.to_thread(_compute_phash, image_bytes)
        except (UnidentifiedImageError, OSError) as e:
            await interaction.followup.send(
                f"❌ Could not parse image: `{e}`", ephemeral=True
            )
            return

        saved_id = await self._insert_phash(
            phash_str, url, interaction.user.id, note
        )
        if saved_id is None:
            await interaction.followup.send(
                f"❌ Save failed (pHash `{phash_str}` may already be in the blocklist)",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title="✅ Saved to blocklist",
            color=discord.Color.green(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="ID", value=f"`{saved_id}`", inline=True)
        embed.add_field(name="pHash", value=f"`{phash_str}`", inline=True)
        embed.add_field(name="URL", value=f"`{url[:300]}`", inline=False)
        if note:
            embed.add_field(name="Note", value=note, inline=False)
        embed.set_thumbnail(url=url)
        embed.set_footer(text=f"Added by {interaction.user}")

        await interaction.followup.send(embed=embed, ephemeral=True)

    # ---------------- save_raw (from hex string) ----------------

    @phash.command(
        name="save_raw",
        description="Save a raw pHash hex string to the blocklist (no image fetch)",
    )
    @app_commands.describe(
        phash="The 16-char pHash hex string (e.g. from an automod-log embed)",
        note="Optional note (e.g. 'Mr Beast giveaway variant 3')",
        source="Optional source URL or message-jump link for traceability",
    )
    @is_bot_owner()
    async def phash_save_raw(
        self,
        interaction: discord.Interaction,
        phash: str,
        note: str | None = None,
        source: str | None = None,
    ):
        # Normalise: strip whitespace, lowercase so duplicate detection
        # is case-insensitive (imagehash emits lowercase, but a user-typed
        # uppercase hash should still match).
        phash_clean = phash.strip().lower()

        if not _validate_phash_hex(phash_clean):
            await interaction.response.send_message(
                f"❌ `{phash}` is not a valid pHash hex string.\n"
                "Expected 16 hex characters (e.g. `f0e1d2c3b4a59687`).",
                ephemeral=True,
            )
            return

        source_url = source.strip() if source and source.strip() else MANUAL_SOURCE_PLACEHOLDER

        saved_id = await self._insert_phash(
            phash_clean, source_url, interaction.user.id, note
        )
        if saved_id is None:
            await interaction.response.send_message(
                f"❌ Save failed — pHash `{phash_clean}` is already in the blocklist.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title="✅ Saved raw pHash to blocklist",
            color=discord.Color.green(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="ID", value=f"`{saved_id}`", inline=True)
        embed.add_field(name="pHash", value=f"`{phash_clean}`", inline=True)
        embed.add_field(
            name="Source", value=f"`{source_url[:300]}`", inline=False
        )
        if note:
            embed.add_field(name="Note", value=note, inline=False)
        embed.set_footer(text=f"Added by {interaction.user}")

        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ---------------- list ----------------

    @phash.command(name="list", description="List all stored pHashes")
    @is_bot_owner()
    async def phash_list(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        async with self.bot.db.execute(
            "SELECT id, phash, source_url, added_by, added_at, note "
            "FROM image_phash ORDER BY id DESC"
        ) as cursor:
            rows = await cursor.fetchall()

        if not rows:
            await interaction.followup.send(
                "📭 Blocklist is empty. Use `/phash save` or `/phash save_raw` to add an entry.",
                ephemeral=True,
            )
            return

        lines: list[str] = []
        for entry_id, phash_str, source_url, added_by, added_at, note in rows:
            ts = f"<t:{added_at}:d>"
            note_str = f" — *{note}*" if note else ""
            url_short = (
                source_url if len(source_url) <= 80 else source_url[:77] + "..."
            )
            lines.append(
                f"**#{entry_id}** `{phash_str}`{note_str}\n"
                f"  ↳ {url_short} (added {ts} by <@{added_by}>)"
            )

        # Chunk to stay under embed description limit (4096 chars).
        # Fixed: only flush when current is non-empty (avoids empty first chunk
        # when a single line is itself longer than 4000 chars).
        chunks: list[str] = []
        current = ""
        for line in lines:
            candidate = (current + line + "\n") if current else (line + "\n")
            if len(candidate) > 4000 and current:
                chunks.append(current)
                current = line + "\n"
            else:
                current = candidate
        if current:
            chunks.append(current)

        for i, chunk in enumerate(chunks):
            title = f"🧷 Image pHash Blocklist ({len(rows)} entries)"
            if len(chunks) > 1:
                title += f" — p.{i + 1}/{len(chunks)}"
            embed = discord.Embed(
                title=title, description=chunk, color=discord.Color.pink()
            )
            await interaction.followup.send(embed=embed, ephemeral=True)

    # ---------------- delete ----------------

    @phash.command(name="delete", description="Delete a stored pHash by ID")
    @app_commands.describe(entry_id="The ID shown by /phash list")
    @is_bot_owner()
    async def phash_delete(self, interaction: discord.Interaction, entry_id: int):
        async with self.bot.db.execute(
            "SELECT phash, source_url FROM image_phash WHERE id = ?",
            (entry_id,),
        ) as cursor:
            row = await cursor.fetchone()

        if not row:
            await interaction.response.send_message(
                f"❌ No blocklist entry with ID `{entry_id}`.",
                ephemeral=True,
            )
            return

        phash_str, source_url = row
        await self.bot.db.execute("DELETE FROM image_phash WHERE id = ?", (entry_id,))
        await self.bot.db.commit()
        mod_cog = self.bot.get_cog("Moderation")
        if mod_cog is not None:
            mod_cog.invalidate_blocklist_cache()
        embed = discord.Embed(
            title="🗑️ Deleted",
            color=discord.Color.red(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="ID", value=f"`{entry_id}`", inline=True)
        embed.add_field(name="pHash", value=f"`{phash_str}`", inline=True)
        embed.add_field(name="URL", value=f"`{source_url[:300]}`", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ---------------- match ----------------

    @phash.command(
        name="match",
        description="Check if an image URL matches any stored pHash",
    )
    @app_commands.describe(
        url="Image URL to check",
        threshold="Max Hamming distance to count as match (default: 10)",
    )
    @is_bot_owner()
    async def phash_match(
        self,
        interaction: discord.Interaction,
        url: str,
        threshold: int = PHASH_DISTANCE_THRESHOLD,
    ):
        await interaction.response.defer(ephemeral=True)
        session = await self._get_session()

        try:
            image_bytes = await _fetch_image_bytes(url, session)
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            await interaction.followup.send(
                f"❌ Failed to download image: `{e}`", ephemeral=True
            )
            return

        try:
            phash_str = await asyncio.to_thread(_compute_phash, image_bytes)
        except (UnidentifiedImageError, OSError) as e:
            await interaction.followup.send(
                f"❌ Could not parse image: `{e}`", ephemeral=True
            )
            return

        await self._render_match_result(
            interaction, phash_str, threshold, thumbnail_url=url
        )

    # ---------------- match_raw (from hex string) ----------------

    @phash.command(
        name="match_raw",
        description="Check if a raw pHash hex string matches any stored pHash",
    )
    @app_commands.describe(
        phash="The 16-char pHash hex string to look up",
        threshold="Max Hamming distance to count as match (default: 10)",
    )
    @is_bot_owner()
    async def phash_match_raw(
        self,
        interaction: discord.Interaction,
        phash: str,
        threshold: int = PHASH_DISTANCE_THRESHOLD,
    ):
        phash_clean = phash.strip().lower()
        if not _validate_phash_hex(phash_clean):
            await interaction.response.send_message(
                f"❌ `{phash}` is not a valid pHash hex string.",
                ephemeral=True,
            )
            return

        # Defer only after validation passes (instant response on bad input).
        await interaction.response.defer(ephemeral=True)
        await self._render_match_result(
            interaction, phash_clean, threshold, thumbnail_url=None
        )

    # ---------------- compare ----------------

    @phash.command(
        name="compare",
        description="Compare two image URLs by pHash distance",
    )
    @app_commands.describe(url1="First image URL", url2="Second image URL")
    @is_bot_owner()
    async def phash_compare(
        self,
        interaction: discord.Interaction,
        url1: str,
        url2: str,
    ):
        await interaction.response.defer(ephemeral=True)
        session = await self._get_session()

        try:
            bytes1, bytes2 = await asyncio.gather(
                _fetch_image_bytes(url1, session),
                _fetch_image_bytes(url2, session),
            )
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            await interaction.followup.send(
                f"❌ Failed to download one of the images: `{e}`", ephemeral=True
            )
            return

        try:
            phash1, phash2 = await asyncio.gather(
                asyncio.to_thread(_compute_phash, bytes1),
                asyncio.to_thread(_compute_phash, bytes2),
            )
        except (UnidentifiedImageError, OSError) as e:
            await interaction.followup.send(
                f"❌ Could not parse one of the images: `{e}`", ephemeral=True
            )
            return

        await self._render_compare_result(interaction, phash1, phash2)

    # ---------------- compare_raw ----------------

    @phash.command(
        name="compare_raw",
        description="Compare two raw pHash hex strings by Hamming distance",
    )
    @app_commands.describe(
        phash1="First 16-char pHash hex string",
        phash2="Second 16-char pHash hex string",
    )
    @is_bot_owner()
    async def phash_compare_raw(
        self,
        interaction: discord.Interaction,
        phash1: str,
        phash2: str,
    ):
        h1 = phash1.strip().lower()
        h2 = phash2.strip().lower()
        if not _validate_phash_hex(h1) or not _validate_phash_hex(h2):
            await interaction.response.send_message(
                "❌ One or both pHash strings are not valid 16-char hex.",
                ephemeral=True,
            )
            return
        await self._render_compare_result(interaction, h1, h2)

    # ================================================================
    # Internal helpers (shared by URL and raw commands)
    # ================================================================

    async def _insert_phash(
        self,
        phash_str: str,
        source_url: str,
        added_by: int,
        note: str | None,
    ) -> int | None:
        """Insert a pHash row. Returns the new row id, or None if the hash
        was already present (UNIQUE constraint). Reads lastrowid BEFORE
        commit (safe pattern across aiosqlite versions)."""
        now_ts = int(discord.utils.utcnow().timestamp())
        try:
            cursor = await self.bot.db.execute(
                "INSERT INTO image_phash "
                "(phash, source_url, added_by, added_at, note) "
                "VALUES (?, ?, ?, ?, ?)",
                (phash_str, source_url, added_by, now_ts, note),
            )
            saved_id = cursor.lastrowid
            await self.bot.db.commit()
            mod_cog = self.bot.get_cog("Moderation")
            if mod_cog is not None:
                mod_cog.invalidate_blocklist_cache()
            return saved_id
        except Exception as e:
            logger.warning(f"pHash insert failed for `{phash_str}`: {e}")
            return None

    async def _render_match_result(
        self,
        interaction: discord.Interaction,
        phash_str: str,
        threshold: int,
        thumbnail_url: str | None,
    ) -> None:
        """Shared match-render logic for /phash match and /phash match_raw."""
        async with self.bot.db.execute(
            "SELECT id, phash, source_url, note FROM image_phash"
        ) as cursor:
            stored = await cursor.fetchall()

        if not stored:
            await interaction.followup.send(
                f"🧷 Your pHash: `{phash_str}`\n"
                "📭 Blocklist is empty — nothing to match against.",
                ephemeral=True,
            )
            return

        scored: list[tuple[int, int, str, str, str | None]] = []
        # (distance, id, phash, source_url, note)
        for entry_id, stored_phash, stored_url, note in stored:
            dist = _hamming_distance(phash_str, stored_phash)
            if dist is None:
                continue
            scored.append((dist, entry_id, stored_phash, stored_url, note))
        scored.sort(key=lambda x: x[0])

        # Guard against empty `scored` (all stored hashes malformed).
        if not scored:
            await interaction.followup.send(
                f"🧷 Your pHash: `{phash_str}`\n"
                "❌ All stored hashes are malformed — blocklist needs repair.",
                ephemeral=True,
            )
            return

        best_dist, best_id, best_phash, best_url, best_note = scored[0]
        is_match = best_dist <= threshold

        embed = discord.Embed(
            title="🔍 Match result",
            color=discord.Color.green() if is_match else discord.Color.light_grey(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Your pHash", value=f"`{phash_str}`", inline=False)
        embed.add_field(name="Threshold", value=f"`≤ {threshold}`", inline=True)
        embed.add_field(
            name="Best match", value=f"`{best_dist}` (entry #{best_id})", inline=True
        )
        embed.add_field(
            name="Verdict",
            value=(
                "✅ **Match** — likely same image or a variant"
                if is_match
                else "❎ No match within threshold"
            ),
            inline=False,
        )
        embed.add_field(name="Matched pHash", value=f"`{best_phash}`", inline=True)
        embed.add_field(name="Source URL", value=f"`{best_url[:200]}`", inline=False)
        if best_note:
            embed.add_field(name="Note", value=best_note, inline=False)

        # Show top-3 closest matches for blocklist curation.
        top_matches = scored[:3]
        matches_text = "\n".join(
            f"• `dist={d}` — #{i} `{h}`" for d, i, h, _, _ in top_matches
        )
        embed.add_field(name="Top 3 closest", value=matches_text, inline=False)

        if thumbnail_url:
            embed.set_thumbnail(url=thumbnail_url)

        await interaction.followup.send(embed=embed, ephemeral=True)

    async def _render_compare_result(
        self,
        interaction: discord.Interaction,
        phash1: str,
        phash2: str,
    ) -> None:
        """Shared compare-render logic for /phash compare and /phash compare_raw."""
        dist = _hamming_distance(phash1, phash2)
        if dist is None:
            await interaction.followup.send(
                "❌ Could not compare hashes (one is malformed).", ephemeral=True
            )
            return

        if dist == 0:
            verdict = "🟢 Identical (same image)"
        elif dist <= 5:
            verdict = "🟢 Near-identical (minor edits: re-encode, resize, crop)"
        elif dist <= 10:
            verdict = "🟡 Visually very similar (likely a variant)"
        elif dist <= 20:
            verdict = "🟠 Related (same template / same subject, different content)"
        else:
            verdict = "🔴 Different images"

        embed = discord.Embed(
            title="🧷 pHash comparison",
            color=discord.Color.pink(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="pHash 1", value=f"`{phash1}`", inline=False)
        embed.add_field(name="pHash 2", value=f"`{phash2}`", inline=False)
        embed.add_field(name="Hamming distance", value=f"`{dist}`", inline=True)
        embed.add_field(name="Verdict", value=verdict, inline=False)

        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Phash(bot))