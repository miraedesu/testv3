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
from typing import Optional

import aiohttp
import asyncio
import discord
from discord import app_commands
from discord.ext import commands

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
MAX_MESSAGE_LENGTH = 1000   # chars — skip translation if message exceeds this
WEBHOOK_NAME = "Translator"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")

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
        #--- Only translate if text is in the specified source language ---
        if detect_source:
            #--- Source is known, no need to detect — just translate ---
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
        #--- Auto source + we want to know the source language ---
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
        #--- Auto source, no detection needed ---
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
    }

    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                OPENROUTER_URL,
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=30),
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

                #--- Source filtering: check for SKIP ---
                if source_is_filtered:
                    stripped = content.strip()
                    if stripped.upper() == "SKIP":
                        return None, None  #--- Text wasn't in source language ---
                    return stripped, source_language

                #--- Auto + detect_source: parse JSON ---
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

                #--- Auto, no detection: plain translation ---
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
    }

    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                OPENROUTER_URL,
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=30),
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
        logger.warning("[Translate] OpenRouter detection timed out after 30s (text: %s)", text[:100])
        return None
    except Exception:
        logger.exception("[Translate] OpenRouter detection call failed")
        return None

#--- Cog: Translate ---

class Translate(commands.Cog):
    """Auto-translate a user's messages in a channel via OpenRouter and
    repost the translation via webhook. Original message stays intact.
    """
    translate = app_commands.Group(
        name="translate",
        description="Toggle auto-translation for specific users in this channel",
        default_permissions=discord.Permissions(administrator=True),
    )
    def __init__(self, bot):
        self.bot = bot
        
    async def cog_load(self):
        from common.safeguard import init_safeguard
        await init_safeguard(self.bot)

    #--- Storage helpers ---
    # Format: JSON dict { "user_id": "source_language", ... }
    # Key: translate:{channel_id}

    async def _get_translated_users(
        self, guild_id: int, channel_id: int
    ) -> dict[int, str]:
        """Read the JSON dict of {user_id: source_language} for this channel."""
        raw = await get_guild_setting(self.bot, guild_id, f"translate:{channel_id}")
        if not raw:
            return {}
        try:
            data = json.loads(raw)
            #--- JSON keys are strings; convert to int ---
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
    @app_commands.describe(
        language="Default language (e.g. English, Japanese, Spanish)",
    )
    async def translate_default(
        self,
        interaction: discord.Interaction,
        language: str,
    ):
        """Set the default translation language for this channel."""
        await set_guild_setting(
            self.bot, interaction.guild_id,
            f"translate_default:{interaction.channel_id}", language
        )
        await interaction.response.send_message(
            f"Default translation language for this channel set to **{language}**.",
            ephemeral=True,
        )
    #--- /translate user ---

    @translate.command(name="user", description="Auto-translate a user's messages in this channel")
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
        """Add a user to this channel's auto-translate list.

        The `language` parameter is the SOURCE language — only messages
        in that language will be translated. If omitted, all of the
        user's messages are translated (auto-detect source).

        Target language comes from /translate default (or English).
        """
        if not isinstance(interaction.channel, (discord.TextChannel, discord.Thread)):
            await interaction.response.send_message(
                "Translate only works in text channels and threads.", ephemeral=True
            )
            return

        #--- Resolve target language from channel default ---
        target_language = await get_guild_setting(
            self.bot, interaction.guild_id,
            f"translate_default:{interaction.channel_id}"
        ) or "English"

        #--- Store source language ("auto" if not specified) ---
        source_language = language or "auto"

        translated = await self._get_translated_users(
            interaction.guild_id, interaction.channel_id
        )
        translated[user.id] = source_language
        await self._set_translated_users(
            interaction.guild_id, interaction.channel_id, translated
        )

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
    @app_commands.describe(user="The user to remove from auto-translate")
    async def translate_remove(
        self, interaction: discord.Interaction, user: discord.Member
    ):
        """Remove a user from this channel's auto-translate list."""
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
        await interaction.response.send_message(
            f"Stopped auto-translating {user.mention}.", ephemeral=True
        )

    #--- /translate list ---

    @translate.command(name="list", description="Show who's being auto-translated in this channel")
    async def translate_list(self, interaction: discord.Interaction):
        """List all auto-translated users and their source languages in this channel."""
        translated = await self._get_translated_users(
            interaction.guild_id, interaction.channel_id
        )
        if not translated:
            await interaction.response.send_message(
                "No one is being auto-translated in this channel.", ephemeral=True
            )
            return

        #--- Get target language for display ---
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
    async def translate_clear(self, interaction: discord.Interaction):
        """Remove every user from this channel's auto-translate list."""
        await self._set_translated_users(
            interaction.guild_id, interaction.channel_id, {}
        )
        await interaction.response.send_message(
            "Cleared all auto-translations in this channel.", ephemeral=True
        )

    #--- /detect (standalone command, not part of the translate group) ---

    @app_commands.command(name="detect", description="Detect the language of a specific message")
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        message_id="ID of the message to analyze (right-click → Copy ID)",
    )
    async def detect(
        self,
        interaction: discord.Interaction,
        message_id: str,
    ):
        """Detect the language of a message using OpenRouter (GLM 5.3 Flash).

        Fetches the specified message by ID and detects its language.
        """
        if not isinstance(interaction.channel, (discord.TextChannel, discord.Thread)):
            await interaction.response.send_message(
                "Detect only works in text channels and threads.", ephemeral=True
            )
            return

        #--- Defer — API call may take a second or two ---
        await interaction.response.defer(ephemeral=True)

        #--- Fetch the message by ID ---
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

        #--- Validate text content ---
        if not target_text or not target_text.strip():
            await interaction.followup.send(
                "That message has no text content to analyze.", ephemeral=True
            )
            return

        #--- Hard cap: skip if too long ---
        if len(target_text) > MAX_MESSAGE_LENGTH:
            await interaction.followup.send(
                f"Message is too long ({len(target_text)} chars, "
                f"max {MAX_MESSAGE_LENGTH}).",
                ephemeral=True,
            )
            return

        #--- Call OpenRouter for detection ---
        detected = await _detect_language(target_text)
        if detected is None:
            await interaction.followup.send(
                "Failed to detect language. Try again later.", ephemeral=True
            )
            return

        #--- Show result with a snippet of the analyzed text ---
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
        """If author is in the auto-translate list for this channel,
        translate their message via OpenRouter and repost via webhook.

        The original message is NOT deleted — the translation appears
        alongside it as a webhook message with the user's name and avatar.
        """
        #--- Webhook security: intercept all webhook messages ---
        if message.webhook_id is not None:
            await check_webhook_message(message, self.bot)
            return

        #--- Bail on DMs, bots, system messages ---
        if message.guild is None or message.author.bot:
            return
        if message.type not in (
            discord.MessageType.default,
            discord.MessageType.reply,
        ):
            return

        #--- Only text channels and threads ---
        channel = message.channel
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return

        #--- Is this user being auto-translated here? (cheap DB lookup) ---
        translated = await self._get_translated_users(message.guild.id, channel.id)
        if message.author.id not in translated:
            return
        #--- Conflict guard: skip if user is also uwulocked in this channel ---
        #--- (uwu.py deletes the original — we don't want to race with it)
        uwu_raw = await get_guild_setting(
            self.bot, message.guild.id, f"uwulock:{channel.id}"
        )
        if uwu_raw:
            try:
                uwu_users = json.loads(uwu_raw)
                if message.author.id in uwu_users:
                    return  #--- UwuLock owns this user here ---
            except json.JSONDecodeError:
                pass

        #--- Check for text content ---
        text = message.content.strip()
        if not text:
            return

        #--- Hard cap: skip if message is too long (cost protection) ---
        if len(text) > MAX_MESSAGE_LENGTH:
            return

        #--- Permission check (webhook send only — no deletion needed) ---
        if isinstance(channel, discord.Thread):
            perm_target = channel.parent or channel
        else:
            perm_target = channel
        if not bot_can_webhook_send(perm_target):
            return

        #--- Resolve webhook BEFORE calling the API ---
        if isinstance(channel, discord.Thread):
            webhook_channel = channel.parent
            thread_kwarg = {"thread": channel}
        else:
            webhook_channel = channel
            thread_kwarg = {}

        if webhook_channel is None:
            return  #--- Parent channel deleted ---

        webhook = await get_managed_webhook(webhook_channel, WEBHOOK_NAME, self.bot)
        if webhook is None:
            return  #--- No webhook available — skip silently ---

        #--- Get target language from channel default ---
        target_language = await get_guild_setting(
            self.bot, message.guild.id,
            f"translate_default:{channel.id}"
        ) or "English"

        #--- source_language is what's stored per-user (the FROM language) ---
        source_language = translated[message.author.id]

        #--- Call OpenRouter for translation ---
        translated_text, _ = await _translate_text(
            text,
            target_language,
            source_language=source_language,
            detect_source=False,
        )
        if translated_text is None or not translated_text.strip():
            return  #--- Translation failed, empty, or not in source language ---

        #--- Don't repost if translation is identical to original ---
        if translated_text.strip().lower() == text.strip().lower():
            return

        #--- Build content: translated text + link to original ---
        msg_link = (
            f"https://discord.com/channels/"
            f"{message.guild.id}/{channel.id}/{message.id}"
        )
        content = f"{translated_text[:1900]}\n\n[Original]({msg_link})"

        #--- Apply invisible security watermark ---
        content = watermark_content(content)

        #--- Send translated text via webhook ---
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