"""Centralized permission safeguards and webhook security for cogs.

Provides:
- bot_can_webhook_send(): permission check for webhook operations
- get_managed_webhook(): centralized webhook lookup/creation with creator verification
- watermark_content(): prepend invisible zero-width watermark to webhook messages
- check_webhook_message(): detect unauthorized webhook usage and auto-kill
- rotate_all_webhooks(): periodic token invalidation (call from a tasks.loop)

Webhook security model:
  Every webhook message the bot sends is prefixed with an invisible
  zero-width watermark. If a webhook message from one of our webhooks
  lacks this watermark, it was sent by someone who obtained the URL —
  the webhook is instantly deleted and recreated on next use.
"""
from __future__ import annotations

import logging
from typing import Optional

import discord
from discord.ext import commands
import json

logger = logging.getLogger(__name__)


#--- Permission check ---

def bot_can_webhook_send(
    channel: discord.abc.GuildChannel,
    *,
    also_manage_messages: bool = False,
) -> bool:
    """Return True if the bot can send via webhook in this channel.

    Requires:
      - view_channel  (otherwise we can't even see it)
      - send_messages (without this Discord will 403 the webhook send)
      - manage_webhooks (needed to create/fetch the webhook)

    If `also_manage_messages=True` (e.g. uwu.py needs to delete the user's
    original message), additionally requires manage_messages.

    Runs on the cached member, so this is cheap (no API call).
    """
    me = channel.guild.me
    perms = channel.permissions_for(me)

    if not (perms.view_channel and perms.send_messages and perms.manage_webhooks):
        return False

    if also_manage_messages and not perms.manage_messages:
        return False

    return True


#--- Webhook security ---

# Invisible watermark prepended to every webhook message the bot sends.
# If a webhook message from our webhook lacks this, it was sent by
# someone else who obtained the URL — we kill the webhook instantly.
_WEBHOOK_WATERMARK = "\u200b\u200c\u200d\u200b\u200c"
_WATERMARK_LEN = len(_WEBHOOK_WATERMARK)

# Cache: (channel_id, webhook_name) -> Webhook
_webhook_cache: dict[tuple[int, str], discord.Webhook] = {}

# Registry of webhook IDs we created/trust (for fast on_message lookup)
_our_webhook_ids: set[int] = set()

async def _persist_webhook_ids(bot: commands.Bot) -> None:
    """Save current _our_webhook_ids to DB as a JSON list."""
    try:
        ids_json = json.dumps(list(_our_webhook_ids))
        await bot.db.execute(
            """INSERT INTO guild_settings (guild_id, setting_key, setting_value)
               VALUES (0, '_safeguard_webhook_ids', ?)
               ON CONFLICT(guild_id, setting_key) DO UPDATE SET setting_value = excluded.setting_value""",
            (ids_json,),
        )
        await bot.db.commit()
    except Exception:
        logger.exception("[Safeguard] Failed to persist webhook IDs to DB")

async def _load_persisted_webhook_ids(bot: commands.Bot) -> None:
    """Load saved webhook IDs from DB into _our_webhook_ids."""
    try:
        async with bot.db.execute(
            "SELECT setting_value FROM guild_settings WHERE guild_id = 0 AND setting_key = '_safeguard_webhook_ids'"
        ) as cursor:
            row = await cursor.fetchone()
        if row:
            ids = json.loads(row[0])
            _our_webhook_ids.update(ids)
            logger.info("[Safeguard] Loaded %d persisted webhook IDs from DB", len(ids))
    except Exception:
        logger.exception("[Safeguard] Failed to load persisted webhook IDs")
async def init_safeguard(bot: commands.Bot) -> None:
    """Call this on bot startup to load persisted webhook IDs.

    Should be called from a cog's cog_load() method after bot.db is ready.
    """
    await _load_persisted_webhook_ids(bot)
def watermark_content(content: str) -> str:
    """Prepend invisible watermark to webhook message content.

    Truncates content to fit Discord's 2000-char limit including the watermark.
    """
    return _WEBHOOK_WATERMARK + content[:2000 - _WATERMARK_LEN]


def is_watermarked(content: str | None) -> bool:
    """Check if content starts with our invisible watermark."""
    if not content:
        return False
    return content.startswith(_WEBHOOK_WATERMARK)


async def get_managed_webhook(
    channel: discord.TextChannel | discord.ForumChannel,
    webhook_name: str,
    bot: commands.Bot,  # <-- NEW param
) -> Optional[discord.Webhook]:
    """Find or create a webhook for a channel. Caches by (channel_id, name)."""
    cache_key = (channel.id, webhook_name)

    #--- Check cache first ---
    cached = _webhook_cache.get(cache_key)
    if cached is not None:
        try:
            await cached.fetch()
            _our_webhook_ids.add(cached.id)
            return cached
        except (discord.NotFound, discord.Forbidden):
            _webhook_cache.pop(cache_key, None)
            _our_webhook_ids.discard(cached.id)

    #--- Lookup or create ---
    try:
        webhooks = await channel.webhooks()
        for wh in webhooks:
            if wh.name == webhook_name and wh.user is not None and wh.user.id == channel.guild.me.id:
                _webhook_cache[cache_key] = wh
                _our_webhook_ids.add(wh.id)
                await _persist_webhook_ids(bot)  # <-- NEW
                return wh
        wh = await channel.create_webhook(name=webhook_name)
        _webhook_cache[cache_key] = wh
        _our_webhook_ids.add(wh.id)
        await _persist_webhook_ids(bot)  # <-- NEW
        return wh
    except discord.Forbidden:
        logger.warning("[Safeguard] Missing Manage Webhooks permission in #%s", channel)
        return None
    except Exception:
        logger.exception("[Safeguard] Failed to get/create webhook in #%s", channel)
        return None
    
async def kill_webhook(
    webhook_id: int,
    guild_id: int,
    channel_id: int,
    bot: commands.Bot,
) -> None:
    """Immediately delete a compromised webhook and purge all references."""
    found_in_cache = False

    #--- Find and delete from cache ---
    for (ch_id, name), wh in list(_webhook_cache.items()):
        if wh.id == webhook_id:
            try:
                await wh.delete(reason="Unauthorized webhook usage detected — auto-kill")
            except (discord.NotFound, discord.Forbidden) as e:
                if isinstance(e, discord.Forbidden):
                    logger.critical(
                        "[Safeguard] Cannot delete compromised webhook %d — "
                        "bot lacks permission.", webhook_id,
                    )
            _webhook_cache.pop((ch_id, name), None)
            found_in_cache = True
            break

    #--- If not in cache (e.g. after restart), try via channel webhook list ---
    if not found_in_cache:
        channel = bot.get_channel(channel_id)
        #--- Handle threads: webhook lives in the parent channel ---
        if isinstance(channel, discord.Thread):
            channel = channel.parent
        if channel and hasattr(channel, 'webhooks'):
            try:
                for wh in await channel.webhooks():
                    if wh.id == webhook_id:
                        await wh.delete(reason="Unauthorized webhook usage detected — auto-kill")
                        found_in_cache = True
                        break
            except (discord.Forbidden, discord.HTTPException):
                pass

    _our_webhook_ids.discard(webhook_id)
    await _persist_webhook_ids(bot)

    logger.critical(
        "[Safeguard] ⚠️ WEBHOOK KILLED — unauthorized usage detected "
        "(webhook_id=%d, guild=%d, channel=%d). "
        "Webhook will be recreated on next message.",
        webhook_id, guild_id, channel_id,
    )

    #--- Best-effort channel alert ---
    try:
        channel = bot.get_channel(channel_id)
        if channel and isinstance(channel, (discord.TextChannel, discord.Thread)):
            await channel.send(
                "⚠️ **Security Alert:** An unauthorized message was detected from "
                "a bot webhook. The webhook has been automatically terminated and "
                "will be recreated safely on the next message.",
                delete_after=30,
            )
    except Exception:
        pass

async def check_webhook_message(message: discord.Message, bot: commands.Bot) -> None:
    """Check if a webhook message is authorized. Called from cog on_message listeners.

    If the message is from one of our webhooks but lacks the watermark,
    the webhook is instantly killed via kill_webhook().

    This function should be called at the top of on_message, before any
    other processing, whenever message.webhook_id is not None.
    """
    if message.webhook_id is None:
        return

    if message.webhook_id not in _our_webhook_ids:
        return  # Not our webhook — ignore

    #--- Our webhook — verify watermark ---
    if is_watermarked(message.content):
        return  # Legitimate message from us

    #--- UNAUTHORIZED: no watermark = not sent by us ---
    logger.warning(
        "[Safeguard] Unauthorized webhook message detected: %s",
        message.content[:100],
    )
    await kill_webhook(
        message.webhook_id,
        message.guild.id,
        message.channel.id,
        bot,
    )


async def rotate_all_webhooks(bot: commands.Bot) -> int:
    """Delete all cached webhooks to invalidate any leaked tokens.

    Returns the number of webhooks deleted. Webhooks are recreated
    automatically on next use via get_managed_webhook().

    Call this periodically (e.g., every 24h via a tasks.loop in any cog)
    for defense-in-depth against token leakage.
    """
    count = 0
    for key, webhook in list(_webhook_cache.items()):
        try:
            await webhook.delete(reason="Scheduled token rotation")
            count += 1
        except (discord.NotFound, discord.Forbidden):
            pass
        _webhook_cache.pop(key, None)
        _our_webhook_ids.discard(webhook.id)

    if count:
        logger.info("[Safeguard] Rotated %d webhooks (scheduled token rotation).", count)
    await _persist_webhook_ids(bot)
    return count