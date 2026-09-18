"""Per-channel auto-translate: translate a user's messages via OpenRouter
(GLM 5.3 Flash) and repost via webhook. Original message is NOT deleted —
the translation is posted alongside it as a webhook message mimicking
the user's profile (name + avatar).
"""
from __future__ import annotations

#--- Imports ---
import json
import logging
import os
import time as _time
from typing import Optional

import aiohttp
import asyncio
import discord
from discord import app_commands
from discord.ext import commands
import base64
from common.safeguard import (
    bot_can_webhook_send,
    check_webhook_message,
    get_managed_webhook,
    watermark_content,
)
from common.settings_store import (
    clear_guild_setting,
    get_guild_setting,
    set_guild_setting,
)

logger = logging.getLogger(__name__)

#--- Constants ---

TRANSLATE_MODEL = "z-ai/glm-5.3-flash"
MAX_MESSAGE_LENGTH = 1000  # chars — skip translation if message exceeds this
WEBHOOK_NAME = "Translator"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")

#--- Shared HTTP session (reused across all API calls) ---
# Avoids DNS+TCP+TLS handshake overhead (~200ms) on every translation call.

_http_session: aiohttp.ClientSession | None = None


_PROVIDER_PREFS = {
    "sort": "latency",
    "max_price": {"prompt": 0.15, "completion": 0.50},
    "preferred_max_latency": 2,
}


async def _get_session() -> aiohttp.ClientSession:
    """Lazily create and reuse a single aiohttp.ClientSession."""
    global _http_session
    if _http_session is None or _http_session.closed:
        _http_session = aiohttp.ClientSession(
            headers={"Content-Type": "application/json"},
            timeout=aiohttp.ClientTimeout(total=30),
        )
    return _http_session


async def _close_session():
    """Called from cog_unload to clean up the shared session."""
    global _http_session
    if _http_session is not None and not _http_session.closed:
        await _http_session.close()
    _http_session = None


#--- OpenRouter API helpers ---

async def _translate_text(
    text: str,
    target_language: str,
    source_language: Optional[str] = None,
    detect_source: bool = False,
) -> tuple[Optional[str], Optional[str]]:
    """Send text to OpenRouter (GLM 5.3 Flash) for translation.

    Args:
        text: The text to translate.
        target_language: Language to translate INTO (e.g. "English").
        source_language: Only translate if text is in this language.
            None or "auto" = translate from any language.
        detect_source: If True (and source is auto), detect + return
            the actual source language. Ignored when source_language
            is specified (we already know it).

    Returns (translation, source_language_detected).
        translation is None if: API failure, or text didn't match
        source_language (returned "SKIP").
        source_language_detected is None unless we know it.
    """
    if not OPENROUTER_API_KEY:
        logger.error("[Translate] OPENROUTER_API_KEY not set — skipping translation.")
        return None, None

    source_is_filtered = source_language and source_language.lower() != "auto"

    #--- Build prompt based on whether we're filtering by source language ---

    if source_is_filtered:
        if detect_source:
            detect_source = False

        system_prompt = (
            f"If the following text is written in {source_language}, "
            f"translate it to {target_language}. "
            f"Include informal language, slang, internet speak, and "
            f"phonetic/creative spellings as valid {source_language}. "
            f"If the text is NOT in {source_language} "
            f"(e.g. it is already in {target_language} or in another language), "
            f'respond with only "SKIP". '
            f"Output ONLY the translation or the word SKIP — no other text. "
            f"Preserve any @mentions, custom emotes (<:name:id>), and emojis exactly as they are."
        )
    elif detect_source:
        system_prompt = (
            f"Translate the following text to {target_language}. "
            "The text may contain slang, informal language, internet speak, "
            "or creative/phonetic spellings — treat these as valid "
            "representations of their respective languages. "
            "Also detect the source language. "
            'Respond with ONLY a JSON object: {"translation": "...", "source_language": "English name of source language"}. '
            "No other text. Preserve @mentions, custom emotes (<:name:id>), and emojis exactly as they are."
        )
    else:
        system_prompt = (
            f"Translate the following text to {target_language}. "
            "The text may contain slang, informal language, internet speak, "
            "or creative/phonetic spellings — treat these as valid "
            "representations of their respective languages and translate "
            "the intended meaning. "
            "Output ONLY the translation — no explanations, no prefixes. "
            "Preserve any @mentions, custom emotes (<:name:id>), and emojis exactly as they are."
        )

    payload = {
        "model": TRANSLATE_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": text},
        ],
        "temperature": 0.1,
        "max_tokens": 1000,
        "provider": _PROVIDER_PREFS,
    }

    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
    }

    try:
        session = await _get_session()
        async with session.post(
            OPENROUTER_URL,
            json=payload,
            headers=headers,
        ) as resp:
            if resp.status != 200:
                error_text = await resp.text()
                logger.warning(
                    "[Translate] OpenRouter returned %d: %s",
                    resp.status, error_text[:200],
                )
                return None, None
            data = await resp.json()
            content = data["choices"][0]["message"]["content"]
            if content is None:
                logger.warning("[Translate] OpenRouter returned null content.")
                return None, None

            if source_is_filtered:
                stripped = content.strip()
                if stripped.upper() == "SKIP":
                    return None, None
                return stripped, source_language

            if detect_source:
                try:
                    parsed = json.loads(content)
                    return (
                        parsed.get("translation", "").strip() or None,
                        parsed.get("source_language", "").strip() or None,
                    )
                except json.JSONDecodeError:
                    logger.warning("[Translate] JSON parse failed, using raw content.")
                    return content.strip(), None

            return content.strip(), None
    except asyncio.TimeoutError:
        logger.warning("[Translate] OpenRouter timed out after 30s (text: %s)", text[:100])
        return None, None
    except Exception:
        logger.exception("[Translate] OpenRouter translation call failed")
        return None, None


async def _detect_language(text: str) -> Optional[str]:
    """Send text to OpenRouter (GLM 5.3 Flash) for language detection.

    Returns the detected language name (e.g. "Japanese"), or None on failure.
    """
    if not OPENROUTER_API_KEY:
        logger.error("[Translate] OPENROUTER_API_KEY not set — skipping detection.")
        return None

    system_prompt = (
        "Detect the language of the following text. "
        "Respond with ONLY the language name in English "
        "(e.g. 'English', 'Japanese', 'Spanish'). "
        "If the text is too short or ambiguous, respond with 'Unknown'."
    )

    payload = {
        "model": TRANSLATE_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": text},
        ],
        "temperature": 0,
        "max_tokens": 50,
        "provider": _PROVIDER_PREFS,
    }

    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
    }

    try:
        session = await _get_session()
        async with session.post(
            OPENROUTER_URL,
            json=payload,
            headers=headers,
        ) as resp:
            if resp.status != 200:
                error_text = await resp.text()
                logger.warning(
                    "[Translate] OpenRouter returned %d: %s",
                    resp.status, error_text[:200],
                )
                return None
            data = await resp.json()
            content = data["choices"][0]["message"]["content"]
            if content is None:
                logger.warning("[Translate] OpenRouter returned null content for detection.")
                return None
            return content.strip()
    except asyncio.TimeoutError:
        logger.warning("[Translate] OpenRouter detection timed out (text: %s)", text[:100])
        return None
    except Exception:
        logger.exception("[Translate] OpenRouter detection call failed")
        return None


async def _translate_image(
    image_bytes: bytes | None,
    content_type: str | None,
    image_url: str | None,
    target_language: str,
    accompanying_text: str | None = None,
) -> tuple[Optional[str], Optional[str]]:
    """Send an image to GLM-5.3-flash for OCR + translation.

    Either `image_bytes` (uploaded attachment, sent as base64) or
    `image_url` (Discord CDN link, URL passed directly to OpenRouter)
    must be provided. If `accompanying_text` is given, it is also
    translated alongside the image text.

    Returns (translation, source_language_detected).
        translation is None on failure or no text found.
        source_language_detected is the detected language name or None.
    """
    if not OPENROUTER_API_KEY:
        logger.error("[Translate] OPENROUTER_API_KEY not set — skipping image translation.")
        return None, None

    #--- Build the image content part ---
    if image_bytes is not None:
        media_type = content_type or "image/png"
        if media_type not in ("image/png", "image/jpeg", "image/webp", "image/gif"):
            media_type = "image/png"
        b64 = base64.b64encode(image_bytes).decode("utf-8")
        image_part: dict = {
            "type": "image_url",
            "image_url": {"url": f"data:{media_type};base64,{b64}"},
        }
    elif image_url is not None:
        image_part = {
            "type": "image_url",
            "image_url": {"url": image_url},
        }
    else:
        return None, None

    #--- Build prompt — always detect source language ---
    if accompanying_text:
        system_prompt = (
            f"Translate all text visible in the image AND the user-provided text "
            f"to {target_language}. Also detect the source language of the text. "
            f'Respond with ONLY a JSON object: {{"translation": "...", "source_language": "English name of source language"}}. '
            f"No other text. If the image has no readable text, just translate the "
            f"user text and detect its source language. "
            f"Preserve @mentions, custom emotes (<:name:id>), and emojis."
        )
        user_content = [
            {"type": "text", "text": accompanying_text},
            image_part,
        ]
    else:
        system_prompt = (
            f"Extract all text visible in the image and translate it to {target_language}. "
            f"Also detect the source language of the text in the image. "
            f'Respond with ONLY a JSON object: {{"translation": "...", "source_language": "English name of source language"}}. '
            f"No other text. If there is no readable text in the image, respond with: "
            f'{{"translation": "", "source_language": "Unknown"}}. '
            f"Preserve @mentions, custom emotes (<:name:id>), and emojis."
        )
        user_content = [image_part]

    payload = {
        "model": TRANSLATE_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0.1,
        "max_tokens": 2000,
        "provider": _PROVIDER_PREFS,
    }

    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
    }

    try:
        session = await _get_session()
        async with session.post(
            OPENROUTER_URL,
            json=payload,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=60),  # override session default (30s) — image OCR is slower
        ) as resp:
            if resp.status != 200:
                error_text = await resp.text()
                logger.warning(
                    "[Translate] OpenRouter image returned %d: %s",
                    resp.status, error_text[:200],
                )
                return None, None
            data = await resp.json()
            content = data["choices"][0]["message"]["content"]
            if content is None:
                return None, None

            #--- Parse JSON response ---
            try:
                parsed = json.loads(content)
                translation = parsed.get("translation", "").strip() or None
                source_lang = parsed.get("source_language", "").strip() or None
                #--- Empty translation = no text found ---
                if not translation:
                    return None, source_lang
                return translation, source_lang
            except json.JSONDecodeError:
                #--- Fallback: treat raw content as translation ---
                logger.warning("[Translate] Image JSON parse failed, using raw content.")
                stripped = content.strip()
                if not stripped or stripped.upper() == "NO_TEXT":
                    return None, None
                return stripped, None
    except asyncio.TimeoutError:
        logger.warning("[Translate] OpenRouter image translation timed out after 60s")
        return None, None
    except Exception:
        logger.exception("[Translate] OpenRouter image translation call failed")
        return None, None


#--- Cog: Translate ---

class Translate(commands.Cog):
    """Auto-translate a user's messages in a channel via OpenRouter and
    repost the translation via webhook. Original message stays intact.
    """

    # NOTE: No default_permissions on the Group itself.
    # Admin-only restriction is set per-command via @app_commands.default_permissions.
    # /translate manual is left open — access is controlled by role in code.
    translate = app_commands.Group(
        name="translate",
        description="Translation settings and manual translation",
    )

    #--- Cache TTL: 60 seconds ---
    _CACHE_TTL = 60

    def __init__(self, bot):
        self.bot = bot
        #--- In-memory cache: (guild_id, channel_id, key) → (value, timestamp) ---
        self._settings_cache: dict[tuple[int, int, str], tuple[str | None, float]] = {}
        #--- Translated users cache: (guild_id, channel_id) → (dict, timestamp) ---
        self._users_cache: dict[tuple[int, int], tuple[dict[int, str], float]] = {}

    async def cog_load(self):
        from common.safeguard import init_safeguard
        await init_safeguard(self.bot)

    async def cog_unload(self):
        await _close_session()

    #--- Cached DB lookups ---

    async def _cached_get_setting(
        self, guild_id: int, channel_id: int, key: str
    ) -> str | None:
        """Guild setting lookup with 60s in-memory cache."""
        cache_key = (guild_id, channel_id, key)
        cached = self._settings_cache.get(cache_key)
        if cached is not None:
            value, ts = cached
            if _time.monotonic() - ts < self._CACHE_TTL:
                return value
        value = await get_guild_setting(self.bot, guild_id, key)
        self._settings_cache[cache_key] = (value, _time.monotonic())
        return value

    async def _cached_get_translated_users(
        self, guild_id: int, channel_id: int
    ) -> dict[int, str]:
        """Translated users dict with 60s in-memory cache."""
        cache_key = (guild_id, channel_id)
        cached = self._users_cache.get(cache_key)
        if cached is not None:
            users, ts = cached
            if _time.monotonic() - ts < self._CACHE_TTL:
                return users
        raw = await get_guild_setting(self.bot, guild_id, f"translate:{channel_id}")
        if not raw:
            users = {}
        else:
            try:
                data = json.loads(raw)
                users = {int(k): v for k, v in data.items()}
            except json.JSONDecodeError:
                users = {}
        self._users_cache[cache_key] = (users, _time.monotonic())
        return users

    def _invalidate_cache(self, guild_id: int, channel_id: int):
        """Clear all cached settings + users for a channel.
        Called after any settings change."""
        to_delete = [
            k for k in self._settings_cache
            if k[0] == guild_id and k[1] == channel_id
        ]
        for k in to_delete:
            del self._settings_cache[k]
        self._users_cache.pop((guild_id, channel_id), None)

    #--- Storage helpers (used by commands, not on_message hot path) ---

    async def _get_translated_users(
        self, guild_id: int, channel_id: int
    ) -> dict[int, str]:
        """Read the JSON dict of {user_id: source_language} for this channel."""
        raw = await get_guild_setting(self.bot, guild_id, f"translate:{channel_id}")
        if not raw:
            return {}
        try:
            data = json.loads(raw)
            return {int(k): v for k, v in data.items()}
        except json.JSONDecodeError:
            return {}

    async def _set_translated_users(
        self, guild_id: int, channel_id: int, users: dict[int, str]
    ) -> None:
        """Write the JSON dict. Clears the key if dict is empty."""
        if users:
            await set_guild_setting(
                self.bot, guild_id, f"translate:{channel_id}", json.dumps(users)
            )
        else:
            await clear_guild_setting(self.bot, guild_id, f"translate:{channel_id}")

    #--- /translate default ---

    @translate.command(name="default", description="Set the default target language for this channel")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(
        language="Default language (e.g. English, Japanese, Spanish)",
    )
    async def translate_default(
        self,
        interaction: discord.Interaction,
        language: str,
    ):
        await set_guild_setting(
            self.bot, interaction.guild_id,
            f"translate_default:{interaction.channel_id}", language
        )
        self._invalidate_cache(interaction.guild_id, interaction.channel_id)
        await interaction.response.send_message(
            f"Default translation language for this channel set to **{language}**.",
            ephemeral=True,
        )

    #--- /translate show_original ---

    @translate.command(name="show_original", description="Toggle whether translations include a link to the original message (off by default)")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(
        show="True = show [Original](url) link. False = hide it (default).",
    )
    async def translate_show_original(
        self,
        interaction: discord.Interaction,
        show: bool,
    ):
        await set_guild_setting(
            self.bot, interaction.guild_id,
            f"translate_show_original:{interaction.channel_id}",
            "true" if show else "false",
        )
        self._invalidate_cache(interaction.guild_id, interaction.channel_id)
        status = "shown" if show else "hidden"
        await interaction.response.send_message(
            f"Original message link is now **{status}** for translations in this channel.",
            ephemeral=True,
        )

    #--- /translate user ---

    @translate.command(name="user", description="Auto-translate a user's messages in this channel")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(
        user="The user to auto-translate",
        language="Source language to translate FROM (e.g. Spanish, Japanese). Omit for auto-detect.",
    )
    async def translate_user(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        language: Optional[str] = None,
    ):
        if not isinstance(interaction.channel, (discord.TextChannel, discord.Thread)):
            await interaction.response.send_message(
                "Translate only works in text channels and threads.", ephemeral=True
            )
            return

        target_language = await get_guild_setting(
            self.bot, interaction.guild_id,
            f"translate_default:{interaction.channel_id}"
        ) or "English"

        source_language = language or "auto"

        translated = await self._get_translated_users(
            interaction.guild_id, interaction.channel_id
        )
        translated[user.id] = source_language
        await self._set_translated_users(
            interaction.guild_id, interaction.channel_id, translated
        )
        self._invalidate_cache(interaction.guild_id, interaction.channel_id)

        if language:
            await interaction.response.send_message(
                f"Now translating {user.mention}'s **{language}** messages "
                f"to **{target_language}** in this channel.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                f"Now translating {user.mention}'s messages (any language) "
                f"to **{target_language}** in this channel.",
                ephemeral=True,
            )

    #--- /translate remove ---

    @translate.command(name="remove", description="Stop auto-translating a user's messages")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(user="The user to remove from auto-translate")
    async def translate_remove(
        self, interaction: discord.Interaction, user: discord.Member
    ):
        translated = await self._get_translated_users(
            interaction.guild_id, interaction.channel_id
        )
        if user.id not in translated:
            await interaction.response.send_message(
                f"{user.display_name} isn't being auto-translated here.",
                ephemeral=True,
            )
            return

        del translated[user.id]
        await self._set_translated_users(
            interaction.guild_id, interaction.channel_id, translated
        )
        self._invalidate_cache(interaction.guild_id, interaction.channel_id)
        await interaction.response.send_message(
            f"Stopped auto-translating {user.mention}.", ephemeral=True
        )

    #--- /translate list ---

    @translate.command(name="list", description="Show who's being auto-translated in this channel")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def translate_list(self, interaction: discord.Interaction):
        translated = await self._get_translated_users(
            interaction.guild_id, interaction.channel_id
        )
        if not translated:
            await interaction.response.send_message(
                "No one is being auto-translated in this channel.", ephemeral=True
            )
            return

        target_language = await get_guild_setting(
            self.bot, interaction.guild_id,
            f"translate_default:{interaction.channel_id}"
        ) or "English"

        lines = []
        for uid, src_lang in translated.items():
            member = interaction.guild.get_member(uid)
            name = member.display_name if member else f"Unknown ({uid})"
            if src_lang.lower() == "auto":
                lines.append(f"• {name} — any language → **{target_language}**")
            else:
                lines.append(f"• {name} — **{src_lang}** → **{target_language}**")

        embed = discord.Embed(
            title="Auto-Translate — this channel",
            description="\n".join(lines),
            color=0x3498DB,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    #--- /translate clear ---

    @translate.command(name="clear", description="Remove all auto-translations in this channel")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def translate_clear(self, interaction: discord.Interaction):
        await self._set_translated_users(
            interaction.guild_id, interaction.channel_id, {}
        )
        self._invalidate_cache(interaction.guild_id, interaction.channel_id)
        await interaction.response.send_message(
            "Cleared all auto-translations in this channel.", ephemeral=True
        )

    #--- /translate role_set ---

    @translate.command(name="role_set", description="Set the role required to use /translate manual")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(role="Role that can use manual translation")
    async def translate_role_set(self, interaction: discord.Interaction, role: discord.Role):
        await set_guild_setting(
            self.bot, interaction.guild_id, "translate_role", str(role.id)
        )
        await interaction.response.send_message(
            f"✅ Manual translation now requires the {role.mention} role.",
            ephemeral=True,
        )

    #--- /translate role_clear ---

    @translate.command(name="role_clear", description="Remove the manual translation role requirement")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def translate_role_clear(self, interaction: discord.Interaction):
        await clear_guild_setting(self.bot, interaction.guild_id, "translate_role")
        await interaction.response.send_message(
            "✅ Manual translation role requirement cleared. Anyone can use /translate manual.",
            ephemeral=True,
        )

    #--- /translate role_view ---

    @translate.command(name="role_view", description="Show the current manual translation role")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def translate_role_view(self, interaction: discord.Interaction):
        raw = await get_guild_setting(self.bot, interaction.guild_id, "translate_role")
        if not raw:
            await interaction.response.send_message(
                "No role set — /translate manual is open to everyone.",
                ephemeral=True,
            )
            return
        try:
            role_id = int(raw)
        except ValueError:
            await interaction.response.send_message(
                "⚠️ Stored role ID is invalid. Use /translate role_clear and re-set.",
                ephemeral=True,
            )
            return
        role = interaction.guild.get_role(role_id)
        if role:
            await interaction.response.send_message(
                f"Manual translation requires: {role.mention}",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                f"⚠️ Role `{role_id}` no longer exists. Use /translate role_clear and re-set.",
                ephemeral=True,
            )

    #--- /translate manual ---

    MAX_IMAGE_BYTES = 10 * 1024 * 1024  # 10 MB

    @translate.command(name="manual", description="Manually translate text or an image (role-gated)")
    @app_commands.describe(
        text="Text to translate",
        image="Image attachment — text in the image will be translated",
    )
    async def translate_manual(
        self,
        interaction: discord.Interaction,
        text: Optional[str] = None,
        image: Optional[discord.Attachment] = None,
    ):
        """Manually translate text and/or an image. Output is an embed
        showing both the original and the translation. Uses the channel's
        default target language (set via /translate default, or English).
        """
        if interaction.guild is None:
            await interaction.response.send_message(
                "This command only works in servers.", ephemeral=True
            )
            return

        #--- Role check ---
        role_raw = await get_guild_setting(self.bot, interaction.guild_id, "translate_role")
        if role_raw:
            try:
                required_role = interaction.guild.get_role(int(role_raw))
            except ValueError:
                required_role = None
            if required_role is None:
                await interaction.response.send_message(
                    "⚠️ The configured translate role no longer exists. "
                    "Ask an admin to re-set it with /translate role_set.",
                    ephemeral=True,
                )
                return
            if required_role not in interaction.user.roles:
                await interaction.response.send_message(
                    f"❌ You need the {required_role.mention} role to use this command.",
                    ephemeral=True,
                )
                return

        #--- Validate: at least one input ---
        if not text and not image:
            await interaction.response.send_message(
                "❌ Provide `text` and/or `image`.",
                ephemeral=True,
            )
            return

        #--- Validate image attachment type ---
        if image and (not image.content_type or not image.content_type.startswith("image/")):
            await interaction.response.send_message(
                "❌ The attached file is not an image.",
                ephemeral=True,
            )
            return

        #--- Validate image size ---
        if image and image.size > self.MAX_IMAGE_BYTES:
            await interaction.response.send_message(
                f"❌ Image is too large ({image.size // 1024 // 1024} MB, "
                f"max {self.MAX_IMAGE_BYTES // 1024 // 1024} MB).",
                ephemeral=True,
            )
            return

        #--- Defer — API calls may take several seconds ---
        await interaction.response.defer()

        #--- Resolve target language from channel default ---
        lang = await get_guild_setting(
            self.bot, interaction.guild_id,
            f"translate_default:{interaction.channel_id}"
        ) or "English"

        #--- Text-only translation ---
        if text and not image:
            translated, source_lang = await _translate_text(
                text, lang, detect_source=True
            )
            if not translated:
                await interaction.followup.send(
                    "❌ Translation failed. Try again later.",
                    ephemeral=True,
                )
                return

            #--- Truncate to fit embed field limit (1024) ---
            original_display = text[:1000] + ("…" if len(text) > 1000 else "")
            translation_display = translated[:1000] + ("…" if len(translated) > 1000 else "")

            #--- Build title with source language ---
            if source_lang and source_lang.lower() != "unknown":
                title = f"Translation from {source_lang}"
            else:
                title = "Translation"

            embed = discord.Embed(
                title=title,
                color=0x3498DB,
            )
            embed.add_field(
                name="Original",
                value=f"```\n{original_display}\n```",
                inline=False,
            )
            embed.add_field(
                name="Translation",
                value=f"```\n{translation_display}\n```",
                inline=False,
            )
            embed.set_footer(text=f"Requested by {interaction.user.display_name}")
            await interaction.followup.send(embed=embed)
            return

        #--- Image translation (with optional accompanying text) ---
        image_bytes = None
        content_type = None
        image_url_for_embed = None
        if image:
            try:
                image_bytes = await image.read()
                content_type = image.content_type
                image_url_for_embed = image.url  # Discord CDN URL — safe to display
            except Exception:
                logger.exception("[Translate] Failed to read image attachment")
                await interaction.followup.send(
                    "❌ Failed to download the image attachment.",
                    ephemeral=True,
                )
                return

        translated, source_lang = await _translate_image(
            image_bytes=image_bytes,
            content_type=content_type,
            image_url=None,
            target_language=lang,
            accompanying_text=text,
        )

        if not translated:
            if text:
                #--- Image failed but we have text — try text-only as fallback ---
                translated, source_lang = await _translate_text(
                    text, lang, detect_source=True
                )
                if translated:
                    original_display = text[:1000] + ("…" if len(text) > 1000 else "")
                    translation_display = translated[:1000] + ("…" if len(translated) > 1000 else "")

                    if source_lang and source_lang.lower() != "unknown":
                        title = f"Translation from {source_lang}"
                    else:
                        title = "Translation"

                    embed = discord.Embed(
                        title=title,
                        color=0x3498DB,
                    )
                    embed.add_field(
                        name="Original",
                        value=f"```\n{original_display}\n```",
                        inline=False,
                    )
                    embed.add_field(
                        name="Translation",
                        value=f"```\n{translation_display}\n```",
                        inline=False,
                    )
                    embed.set_footer(
                        text=f"Requested by {interaction.user.display_name} • "
                             f"Image text could not be extracted"
                    )
                    await interaction.followup.send(embed=embed)
                    return
            await interaction.followup.send(
                "❌ Translation failed or no readable text was found in the image.",
                ephemeral=True,
            )
            return

        #--- Build embed for image translation ---
        translation_display = translated[:1000] + ("…" if len(translated) > 1000 else "")

        #--- Build title with source language ---
        if source_lang and source_lang.lower() != "unknown":
            title = f"Translation from {source_lang}"
        else:
            title = "Translation"

        embed = discord.Embed(
            title=title,
            color=0x3498DB,
        )

        #--- If user also provided text, show it as "Original (text)" field ---
        if text:
            original_display = text[:1000] + ("…" if len(text) > 1000 else "")
            embed.add_field(
                name="Original (text)",
                value=f"```\n{original_display}\n```",
                inline=False,
            )

        embed.add_field(
            name="Translation",
            value=f"```\n{translation_display}\n```",
            inline=False,
        )

        #--- "Original Image" label field — appears right above the image ---
        if image_url_for_embed:
            embed.add_field(
                name="Original Image",
                value="\u200b",  # zero-width space — field name is the label
                inline=False,
            )
            embed.set_image(url=image_url_for_embed)

        embed.set_footer(text=f"Requested by {interaction.user.display_name}")
        await interaction.followup.send(embed=embed)

    #--- /detect (standalone command, not part of the translate group) ---

    @app_commands.command(name="detect", description="Detect the language of a specific message")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(
        message_id="ID of the message to analyze (right-click → Copy ID)",
    )
    async def detect(
        self,
        interaction: discord.Interaction,
        message_id: str,
    ):
        if not isinstance(interaction.channel, (discord.TextChannel, discord.Thread)):
            await interaction.response.send_message(
                "Detect only works in text channels and threads.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        try:
            msg = await interaction.channel.fetch_message(int(message_id))
        except (discord.NotFound, discord.Forbidden):
            await interaction.followup.send(
                "Could not find that message in this channel.", ephemeral=True
            )
            return
        except ValueError:
            await interaction.followup.send(
                "Invalid message ID. Right-click a message → Copy ID.",
                ephemeral=True,
            )
            return

        target_text = msg.content
        author_display = msg.author.display_name

        if not target_text or not target_text.strip():
            await interaction.followup.send(
                "That message has no text content to analyze.", ephemeral=True
            )
            return

        if len(target_text) > MAX_MESSAGE_LENGTH:
            await interaction.followup.send(
                f"Message is too long ({len(target_text)} chars, "
                f"max {MAX_MESSAGE_LENGTH}).",
                ephemeral=True,
            )
            return

        detected = await _detect_language(target_text)
        if detected is None:
            await interaction.followup.send(
                "Failed to detect language. Try again later.", ephemeral=True
            )
            return

        snippet = target_text[:200] + ("..." if len(target_text) > 200 else "")
        embed = discord.Embed(
            title="Language Detection",
            description=f"**{author_display}**'s message appears to be in "
            f"**{detected}**.",
            color=0x3498DB,
        )
        embed.add_field(name="Message", value=f"```\n{snippet}\n```", inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)

    #--- on_message: translate + repost via webhook ---

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.webhook_id is not None:
            await check_webhook_message(message, self.bot)
            return

        if message.guild is None or message.author.bot:
            return
        if message.type not in (
            discord.MessageType.default,
            discord.MessageType.reply,
        ):
            return

        channel = message.channel
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return

        #--- Cached: translated users (0ms on cache hit) ---
        translated = await self._cached_get_translated_users(
            message.guild.id, channel.id
        )
        if message.author.id not in translated:
            return

        #--- Cached: uwulock conflict check (0ms on cache hit) ---
        uwu_raw = await self._cached_get_setting(
            message.guild.id, channel.id, f"uwulock:{channel.id}"
        )
        if uwu_raw:
            try:
                uwu_users = json.loads(uwu_raw)
                if message.author.id in uwu_users:
                    return
            except json.JSONDecodeError:
                pass

        text = message.content.strip()
        if not text:
            return

        if len(text) > MAX_MESSAGE_LENGTH:
            return

        if isinstance(channel, discord.Thread):
            perm_target = channel.parent or channel
        else:
            perm_target = channel
        if not bot_can_webhook_send(perm_target):
            return

        if isinstance(channel, discord.Thread):
            webhook_channel = channel.parent
            thread_kwarg = {"thread": channel}
        else:
            webhook_channel = channel
            thread_kwarg = {}

        if webhook_channel is None:
            return

        #--- Cached: target language + show_original (0ms on cache hit) ---
        target_language_raw = await self._cached_get_setting(
            message.guild.id, channel.id,
            f"translate_default:{channel.id}"
        )
        target_language = target_language_raw or "English"

        show_original_raw = await self._cached_get_setting(
            message.guild.id, channel.id,
            f"translate_show_original:{channel.id}"
        )

        source_language = translated[message.author.id]

        #--- CONCURRENT: webhook fetch + translation API call ---
        # These are the two slow operations. Running them in parallel
        # saves ~100ms (webhook fetch no longer blocks translation start).
        # get_managed_webhook has its own in-memory cache in safeguard.py,
        # so on cache hit this is nearly instant.
        webhook_task = asyncio.ensure_future(
            get_managed_webhook(webhook_channel, WEBHOOK_NAME, self.bot)
        )
        translate_task = asyncio.ensure_future(
            _translate_text(
                text,
                target_language,
                source_language=source_language,
                detect_source=False,
            )
        )

        webhook, translated_text = await asyncio.gather(
            webhook_task,
            translate_task,
        )

        if webhook is None:
            return

        if translated_text is None or not translated_text.strip():
            return

        if translated_text.strip().lower() == text.strip().lower():
            return

        #--- Build content: translated text (+ optional link to original) ---
        show_original = show_original_raw == "true"

        if show_original:
            msg_link = (
                f"https://discord.com/channels/"
                f"{message.guild.id}/{channel.id}/{message.id}"
            )
            content = f"{translated_text[:1900]}\n\n[Original]({msg_link})"
        else:
            content = translated_text[:1990]

        content = watermark_content(content)

        try:
            await webhook.send(
                content=content,
                username=message.author.display_name,
                avatar_url=message.author.display_avatar.url,
                allowed_mentions=discord.AllowedMentions(
                    everyone=False,
                    roles=False,
                    users=True,
                ),
                **thread_kwarg,
            )
        except Exception:
            logger.exception(
                "[Translate] Webhook send failed "
                "(message id %s, channel %s, author %s).",
                message.id, channel, message.author,
            )


#--- Cog entry point ---

async def setup(bot):
    if not OPENROUTER_API_KEY:
        logger.warning(
            "[Translate] OPENROUTER_API_KEY is not set — "
            "translation and detection will fail at runtime."
        )
    await bot.add_cog(Translate(bot))