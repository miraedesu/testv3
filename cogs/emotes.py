"""Emote usage stats + guild emote management.

Tracking:  every custom emote used in a message or reaction is written to
           the `emote_usage` table (schema in cogs/db.py). Gated by the
           "emote_stats" feature toggle, like every other feature.
Commands:  /emote top                     — leaderboard (7d / 30d / all time)
           /emote upload name image|url   — add a guild emote (admin-only)
           /emote list all|animated|static — mirrors Discord's server-UI split (admin-only)

The old application-emoji cog this file replaces now lives at cogs/app_emotes.py
(group /appemote). Its URL downloader was replaced by the hardened helpers below
(see _fetch_image_bytes for the list of fixes).
"""
from __future__ import annotations

import asyncio
import io
import ipaddress
import logging
import re
import socket
from urllib.parse import urljoin, urlparse

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from PIL import Image

from common.feature_toggles import is_feature_disabled

logger = logging.getLogger(__name__)

EMOTE_USE_RE = re.compile(r"<(a?):([A-Za-z0-9_]{2,32}):(\d{15,25})>")
EMOTE_NAME_RE = re.compile(r"^[A-Za-z0-9_]{2,32}$")

MAX_EMOTE_BYTES = 256 * 1024          # Discord's emote size limit
MAX_REDIRECTS = 3
DOWNLOAD_TIMEOUT = aiohttp.ClientTimeout(total=10)

PERIOD_CHOICES = [
    app_commands.Choice(name="Last 7 days", value=7),
    app_commands.Choice(name="Last 30 days", value=30),
    app_commands.Choice(name="All time", value=0),
]

# ---------------- hardened download / validation helpers ----------------
# Fixes vs. cogs/app_emotes.py's download_image_from_url:
#   * SSRF: https-only, DNS resolved and checked against private/reserved
#     ranges BEFORE connecting, redirects followed manually and re-validated.
#   * Memory: body is streamed and cut off at MAX_EMOTE_BYTES — never a full
#     resp.read() of an arbitrary file; every request has a timeout.
#   * Content: bytes are verified as a real image with Pillow and re-encoded —
#     the Content-Type header alone is never trusted.

def _is_public_ip(ip_str: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    return not (
        addr.is_private or addr.is_loopback or addr.is_link_local
        or addr.is_multicast or addr.is_reserved or addr.is_unspecified
    )


def _host_resolves_public(hostname: str) -> bool:
    """Sync DNS check — call via asyncio.to_thread."""
    try:
        infos = socket.getaddrinfo(hostname, 443, proto=socket.IPPROTO_TCP)
    except OSError:
        return False
    return bool(infos) and all(_is_public_ip(info[4][0]) for info in infos)


async def _fetch_image_bytes(url: str) -> tuple[bytes, str | None]:
    """Download an image with SSRF + size guards. Returns (data, error)."""
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        parsed = urlparse(current)
        if parsed.scheme != "https" or not parsed.hostname:
            return b"", "Only plain `https://` links are supported."
        if parsed.username or parsed.password or len(current) > 2048:
            return b"", "That doesn't look like a plain image link."
        #--- SSRF guard: resolve DNS ourselves, refuse private ranges ---
        if not await asyncio.to_thread(_host_resolves_public, parsed.hostname):
            return b"", "That host doesn't resolve to a public IP."
        try:
            async with aiohttp.ClientSession(timeout=DOWNLOAD_TIMEOUT) as session:
                async with session.get(current, allow_redirects=False) as resp:
                    if resp.status in (301, 302, 303, 307, 308):
                        loc = resp.headers.get("Location")
                        if not loc:
                            return b"", "Host redirected nowhere."
                        current = urljoin(current, loc)   # re-validated next loop
                        continue
                    if resp.status != 200:
                        return b"", f"Host answered HTTP {resp.status}."
                    ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                    if ctype and not ctype.startswith("image/"):
                        return b"", f"That's not an image (Content-Type: `{ctype}`)."
                    #--- Stream with a hard cap ---
                    buf = bytearray()
                    async for chunk in resp.content.iter_chunked(8192):
                        buf.extend(chunk)
                        if len(buf) > MAX_EMOTE_BYTES:
                            return b"", "Image exceeds Discord's 256 KB emote limit."
                    return bytes(buf), None
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
            return b"", "Download failed (connection error or timeout)."
    return b"", f"More than {MAX_REDIRECTS} redirects."


def _prepare_emote_image(data: bytes) -> tuple[bytes, bool, str | None]:
    """Pillow-validate the bytes, normalize, detect animation.
    Returns (bytes, animated, error)."""
    try:
        with Image.open(io.BytesIO(data)) as img:
            img.load()
            fmt = (img.format or "").upper()
            animated = bool(getattr(img, "is_animated", False))

            if fmt == "GIF":
                return data, animated, None          # GIF, static or animated
            if animated:                             # APNG etc.
                if fmt == "PNG":
                    return data, True, None          # APNG — Discord supports it
                return b"", False, "Animated images must be GIF or APNG."

            #--- Static non-GIF (JPG/WEBP/AVIF/…) → clean PNG. Re-encoding
            #--- also strips metadata/EXIF and defeats polyglot files.
            if max(img.size) > 128:
                img.thumbnail((128, 128), Image.LANCZOS)
            buf = io.BytesIO()
            img.convert("RGBA").save(buf, "PNG")
            normalized = buf.getvalue()
    except Exception:
        return b"", False, "That file is not a valid image."

    if len(normalized) > MAX_EMOTE_BYTES:
        return b"", False, "Image exceeds the 256 KB emote limit (after conversion)."
    return normalized, False, None

def _chunk_plain(parts: list[str], sep: str, cap: int = 1900) -> list[str]:
    """Join parts into strings under Discord's 2000-char limit."""
    chunks: list[str] = []
    current = ""
    for part in parts:
        piece = part if not current else sep + part
        if current and len(current) + len(piece) > cap:
            chunks.append(current)
            current = part
        else:
            current += piece
    if current:
        chunks.append(current)
    return chunks
# ---------------- /emote list (subgroup) ----------------

class EmoteList(app_commands.Group):
    """/emote list — mirrors Discord's server-UI split (static vs animated)."""

    def __init__(self):
        super().__init__(
            name="list",
            description="List this server's emotes",
            guild_only=True,
            default_permissions=discord.Permissions(administrator=True),
        )

    @app_commands.command(name="all", description="List every emote in this server")
    async def list_all(self, interaction: discord.Interaction):
        await self._send_listing(interaction, animated=None)

    @app_commands.command(name="animated", description="List animated emotes only")
    async def list_animated(self, interaction: discord.Interaction):
        await self._send_listing(interaction, animated=True)

    @app_commands.command(name="static", description="List static (non-animated) emotes only")
    async def list_static(self, interaction: discord.Interaction):
        await self._send_listing(interaction, animated=False)

    async def _send_listing(self, interaction: discord.Interaction, animated: bool | None):
        guild = interaction.guild
        if guild is None:
            return
        emojis = sorted(guild.emojis, key=lambda e: e.name.lower())
        if animated is not None:
            emojis = [e for e in emojis if e.animated == animated]

        label = {None: "all", True: "animated", False: "static"}[animated]
        if not emojis:
            await interaction.response.send_message(
                f"No {label} emotes in this server.", ephemeral=True
            )
            return

        chunks = _chunk_plain([str(e) for e in emojis], sep=" ")
        chunks[0] = f"**Emotes — {label} ({len(emojis)})**\n{chunks[0]}"
        await interaction.response.send_message(content=chunks[0])
        for extra in chunks[1:]:
            await interaction.followup.send(content=extra)


# ---------------- Cog ----------------

class Emotes(commands.Cog):
    """Usage stats are readable by everyone; upload/list are admin-only
    (group default_permissions + a runtime permission re-check, since a
    server can override integration defaults)."""

    emote = app_commands.Group(
        name="emote",
        description="Emote stats and management.",
        guild_only=True,
        default_permissions=discord.Permissions(administrator=True),
    )

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        if self.emote.get_command("list") is None:   # reload-safe
            self.emote.add_command(EmoteList())

    # ---------------- tracking ----------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Record every custom emote used in message content."""
        if message.guild is None or message.author.bot or message.webhook_id is not None:
            return
        if await is_feature_disabled(self.bot, message.guild.id, "emote_stats"):
            return
        matches = EMOTE_USE_RE.findall(message.content)
        if not matches:
            return
        now_ts = int(discord.utils.utcnow().timestamp())
        rows = [
            (message.guild.id, int(eid), name, 1 if prefix == "a" else 0, now_ts)
            for prefix, name, eid in matches
        ]
        try:
            await self.bot.db.executemany(
                "INSERT INTO emote_usage (guild_id, emote_id, emote_name, animated, used_at) "
                "VALUES (?, ?, ?, ?, ?)",
                rows,
            )
            await self.bot.db.commit()
        except Exception:
            logger.exception("[Emotes] Failed to record emote usage")

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        """Record custom-emoji reactions too — delete this listener if you
        want message-content-only stats."""
        if payload.guild_id is None or payload.emoji.id is None:
            return  # unicode emoji — not tracked
        if payload.member and payload.member.bot:
            return
        if await is_feature_disabled(self.bot, payload.guild_id, "emote_stats"):
            return
        try:
            await self.bot.db.execute(
                "INSERT INTO emote_usage (guild_id, emote_id, emote_name, animated, used_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    payload.guild_id,
                    payload.emoji.id,
                    payload.emoji.name or "unknown",
                    1 if payload.emoji.animated else 0,
                    int(discord.utils.utcnow().timestamp()),
                ),
            )
            await self.bot.db.commit()
        except Exception:
            logger.exception("[Emotes] Failed to record emote reaction")

    # ---------------- /emote top ----------------

    @emote.command(name="top", description="Most-used emotes in this server")
    @app_commands.choices(period=PERIOD_CHOICES)
    @app_commands.describe(period="Time window (default: last 7 days)")
    async def emote_top(
        self,
        interaction: discord.Interaction,
        period: app_commands.Choice[int] | None = None,
    ):
        guild = interaction.guild
        if guild is None:
            return
        if not guild.emojis:
            await interaction.response.send_message("This server has no emotes.", ephemeral=True)
            return

        days = period.value if period is not None else 7
        now_ts = int(discord.utils.utcnow().timestamp())
        time_filter = "AND used_at >= ?" if days > 0 else ""
        params: tuple = (guild.id, now_ts - days * 86400) if days > 0 else (guild.id,)
        sql = ("SELECT emote_id, COUNT(*) AS uses FROM emote_usage "
               f"WHERE guild_id = ? {time_filter} GROUP BY emote_id")
        async with self.bot.db.execute(sql, params) as cursor:
            usage = {row[0]: row[1] for row in await cursor.fetchall()}

        ordered = sorted(guild.emojis, key=lambda e: (-usage.get(e.id, 0), e.name.lower()))
        parts = [f"{str(e)} `{usage.get(e.id, 0)}`" for e in ordered]
        label = f"last {days} days" if days > 0 else "all time"

        chunks = _chunk_plain(parts, sep=", ")
        chunks[0] = f"**🏆 Top emotes — {label}**\n{chunks[0]}"
        await interaction.response.send_message(content=chunks[0])
        for extra in chunks[1:]:
            await interaction.followup.send(content=extra)
    # ---------------- /emote upload ----------------

    @emote.command(name="upload", description="Add a new emote from a file or URL")
    @app_commands.describe(
        name="Name?",
        image="Attach an image file (PNG/GIF/APNG)",
        url="…or a https:// image URL",
    )
    async def emote_upload(
        self,
        interaction: discord.Interaction,
        name: str,
        image: discord.Attachment | None = None,
        url: str | None = None,
    ):
        guild = interaction.guild
        if guild is None:
            return

        #--- Runtime re-check: default_permissions only hides the command,
        #--- a server can override integration permissions ---
        if not interaction.user.guild_permissions.manage_guild_expressions:
            await interaction.response.send_message("Admins only.", ephemeral=True)
            return
        if not guild.me.guild_permissions.manage_guild_expressions:
            await interaction.response.send_message(
                "I need the **Manage Expressions** permission to create emotes.",
                ephemeral=True,
            )
            return
        if not EMOTE_NAME_RE.match(name):
            await interaction.response.send_message(
                "Invalid name — 2-32 characters, letters/numbers/underscore only.",
                ephemeral=True,
            )
            return
        if (image is None) == (url is None):   # both or neither
            await interaction.response.send_message(
                "Provide **either** a file (`image`) **or** a `url` — not both.",
                ephemeral=True,
            )
            return
        if len(guild.emojis) >= guild.emoji_limit:
            await interaction.response.send_message(
                f"Emote slots are full ({len(guild.emojis)}/{guild.emoji_limit}). "
                "Delete one first.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        #--- Acquire bytes ---
        if image is not None:
            if image.size > MAX_EMOTE_BYTES:
                await interaction.followup.send(
                    f"File is {image.size // 1024} KB — the Discord emote limit is "
                    f"{MAX_EMOTE_BYTES // 1024} KB.",
                    ephemeral=True,
                )
                return
            if image.content_type and not image.content_type.startswith("image/"):
                await interaction.followup.send(
                    f"`{image.content_type}` is not an image.", ephemeral=True
                )
                return
            data = await image.read()   # size already capped by the check above
        else:
            data, err = await _fetch_image_bytes(url)
            if err:
                await interaction.followup.send(f"❌ {err}", ephemeral=True)
                return

        #--- Validate + normalize (Pillow, off the event loop) ---
        data, animated, err = await asyncio.to_thread(_prepare_emote_image, data)
        if err:
            await interaction.followup.send(f"❌ {err}", ephemeral=True)
            return

        #--- Create. This IS the "pass through Discord CDN" step: Discord
        #--- re-hosts the image and only its CDN URL ever exists afterwards;
        #--- the source URL is never echoed or hotlinked anywhere.
        try:
            emoji = await guild.create_custom_emoji(
                name=name,
                image=data,
                reason=f"/emote upload by {interaction.user} ({interaction.user.id})",
            )
        except discord.Forbidden:
            await interaction.followup.send(
                "❌ Discord refused — I'm missing **Manage Expressions**.",
                ephemeral=True,
            )
            return
        except discord.HTTPException as e:
            await interaction.followup.send(
                f"❌ Discord rejected the emote (HTTP {e.status}): `{e.text}`",
                ephemeral=True,
            )
            return

        kind = "animated" if emoji.animated else "static"
        logger.info(
            "[Emotes] Uploaded %s emote :%s: (%s) by %s in %s",
            kind, name, emoji.id, interaction.user, guild.name,
        )
        await interaction.followup.send(
            f"✅ Added {emoji} as `{name}` ({kind}) — "
            f"{len(guild.emojis)}/{guild.emoji_limit} slots used.",
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Emotes(bot))