"""Centralized permission safeguards for cogs that need elevated perms.

Only cogs that actually require these permissions should import this —
other cogs keep using channel.send and stay simple.

The point of this module is ONE place to audit/extend if the bot ever
needs to add new permission-gated features (webhook sends, bulk deletes,
channel reordering, etc.).
"""
from __future__ import annotations

import discord


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