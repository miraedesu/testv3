"""Perceptual-hash (pHash) tooling for scam image detection.

Provides owner-only commands to:
- Fetch a pHash from a message's images (by message link or ID)
- Save a raw pHash hex string directly (e.g. from an automod-log embed)
- Maintain a persistent blocklist of known-scam image hashes
- List / delete stored pHash entries

The blocklist is the long-lived asset — every scam image you confirm makes
future detection instant, no OCR or heuristics needed.

Only Discord-CDN images are ever fetched by this cog (attachments are
Discord-hosted by definition; inline embed URLs are filtered to
cdn.discordapp.com / media.discordapp.net).
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
from common.constants import DEV_GUILD_ID

logger = logging.getLogger(__name__)

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
# SSRF (the bot will never fetch arbitrary internet hosts via this cog).
_DISCORD_CDN_PREFIXES = (
    "https://cdn.discordapp.com/",
    "https://media.discordapp.net/",
)
MAX_IMAGE_BYTES = 10 * 1024 * 1024  # 10 MB

# ----------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------
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
def _hamming_distance(h1: str, h2: str) -> int:
    """Hamming distance between two 16-char pHash hex strings.
    Both must be valid hex; no validation here (caller ensures)."""
    return bin(int(h1, 16) ^ int(h2, 16)).count("1")

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
        guild_ids=[DEV_GUILD_ID],
    )

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

        await interaction.response.defer(ephemeral=False)

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
            image_atts.extend(att for att in snap.attachments if _is_image(att))
            _harvest_embeds(snap.embeds)

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

        # Attachments — skip oversized ones without downloading.
        for att in image_atts:
            label = f"attachment `{att.filename}`"
            if att.size > MAX_IMAGE_BYTES:
                results.append((
                    label, att.url, "", att.size, "attachment", None,
                    f"too large ({att.size:,} B > {MAX_IMAGE_BYTES:,} B limit)",
                ))
                continue
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

        # Inline URLs (already validated as Discord CDN).
        # Check Content-Length before reading the body; if the header is
        # missing or lies, aiohttp will still cap memory at the actual size.
        for url in inline_urls:
            label = f"embed `{url[:60]}{'…' if len(url) > 60 else ''}`"
            try:
                async with session.get(url) as resp:
                    resp.raise_for_status()
                    cl = resp.headers.get("Content-Length")
                    if cl is not None and int(cl) > MAX_IMAGE_BYTES:
                        results.append((
                            label, url, "", int(cl), "embed", None,
                            f"too large ({int(cl):,} B > {MAX_IMAGE_BYTES:,} B limit)",
                        ))
                        continue
                    image_bytes = await resp.read()
                # Belt-and-suspenders: re-check actual body size.
                if len(image_bytes) > MAX_IMAGE_BYTES:
                    results.append((
                        label, url, "", len(image_bytes), "embed", None,
                        f"too large ({len(image_bytes):,} B > {MAX_IMAGE_BYTES:,} B limit)",
                    ))
                    continue
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
            title="<:search:1534195860123156582> pHash from message",
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

        embed.set_footer(
            text=(
                f"Fetched by {interaction.user} · "
                f"{len(image_atts)} attachment(s), {len(inline_urls)} embed URL(s)"
            )
        )

        await interaction.followup.send(embed=embed, ephemeral=False)

    # ---------------- save (raw hex string) ----------------

    @phash.command(
        name="save",
        description="Save a raw pHash hex string to the blocklist (no image fetch)",
    )
    @app_commands.describe(
        phash="The 16-char pHash hex string (e.g. from an automod-log embed)",
        note="Optional note (e.g. 'Mr Beast giveaway variant 3')",
        source="Optional source URL or message-jump link for traceability",
    )
    @is_bot_owner()
    async def phash_save(
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
            title="✅ Saved pHash to blocklist",
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
    # ---------------- compare ----------------

    @phash.command(
        name="compare",
        description="Compare a pHash hex string against all blocklist entries",
    )
    @app_commands.describe(
        phash="The 16-char pHash hex string to compare",
        threshold="Max Hamming distance to count as a match (default: 10, max: 64)",
    )
    @is_bot_owner()
    async def phash_compare(
        self,
        interaction: discord.Interaction,
        phash: str,
        threshold: int = 10,
    ):
        phash_clean = phash.strip().lower()
        if not _validate_phash_hex(phash_clean):
            await interaction.response.send_message(
                f"❌ `{phash}` is not a valid pHash hex string.\n"
                "Expected 16 hex characters (e.g. `f0e1d2c3b4a59687`).",
                ephemeral=True,
            )
            return

        if threshold < 0 or threshold > 64:
            await interaction.response.send_message(
                "❌ Threshold must be between 0 and 64.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        async with self.bot.db.execute(
            "SELECT id, phash, source_url, note FROM image_phash"
        ) as cursor:
            rows = await cursor.fetchall()

        if not rows:
            await interaction.followup.send(
                "📭 Blocklist is empty. Nothing to compare against.\n"
                "Use `/phash save` to add entries first.",
                ephemeral=True,
            )
            return

        # Compute distances and sort ascending.
        # Each tuple: (distance, id, phash_str, source_url, note)
        distances: list[tuple[int, int, str, str, str | None]] = []
        for entry_id, phash_str, source_url, note in rows:
            dist = _hamming_distance(phash_clean, phash_str)
            distances.append((dist, entry_id, phash_str, source_url, note))
        distances.sort(key=lambda x: x[0])

        matches = [d for d in distances if d[0] <= threshold]
        nearest = distances[:3]  # always shown for context

        embed = discord.Embed(
            title="<:search:1534195860123156582> pHash comparison",
            color=discord.Color.pink(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(
            name="Query",
            value=(
                f"`{phash_clean}`\n"
                f"Threshold: ≤{threshold} bits · DB size: {len(rows)} entries"
            ),
            inline=False,
        )

        if matches:
            lines: list[str] = []
            for dist, entry_id, phash_str, source_url, note in matches:
                note_str = f" — *{note}*" if note else ""
                url_short = (
                    source_url if len(source_url) <= 80 else source_url[:77] + "..."
                )
                lines.append(
                    f"**#{entry_id}** `{phash_str}` (Δ{dist}){note_str}\n"
                    f"  ↳ {url_short}"
                )
            embed.add_field(
                name=f"✅ Matches ({len(matches)})",
                value="\n".join(lines)[:1024],
                inline=False,
            )
        else:
            embed.add_field(
                name="❌ No matches",
                value=f"No entries within threshold ≤{threshold} bits.",
                inline=False,
            )

        # Always show nearest 3 for context (helps spot near-misses even
        # when nothing is within threshold — useful when triaging variants).
        nearest_lines: list[str] = []
        for dist, entry_id, phash_str, source_url, note in nearest:
            note_str = f" — *{note}*" if note else ""
            url_short = (
                source_url if len(source_url) <= 80 else source_url[:77] + "..."
            )
            nearest_lines.append(
                f"**#{entry_id}** `{phash_str}` (Δ{dist}){note_str}\n"
                f"  ↳ {url_short}"
            )
        embed.add_field(
            name="Nearest 3 (for context)",
            value="\n".join(nearest_lines)[:1024],
            inline=False,
        )

        embed.set_footer(text=f"Compared by {interaction.user}")

        await interaction.followup.send(embed=embed, ephemeral=True)
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
                "📭 Blocklist is empty. Use `/phash save` to add an entry.",
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

    # ================================================================
    # Internal helpers
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


async def setup(bot: commands.Bot):
    await bot.add_cog(Phash(bot))