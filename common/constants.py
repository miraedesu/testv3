"""Shared constants used across multiple cogs."""
from __future__ import annotations

import re
from pathlib import Path
import os
from dotenv import load_dotenv

load_dotenv()

DB_PATH = "evilrie.db"
ATTACHMENTS_DIR = Path("attachments")

# ---- Custom reactions ----
PER_PAGE = 10
MAX_REPLY_LENGTH = 1900   # stay under Discord's 2000 char message limit
CAPTURE_TIMEOUT = 60      # seconds to wait for the follow-up text/attachment message
MAX_FILES_PER_MESSAGE = 10  # Discord's max attachments per message
DISCORD_CDN_RE = re.compile(
    r"https?://(?:cdn\.discordapp\.com|media\.discordapp\.net)/\S+"
)
ALLOWED_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
EXT_BY_CONTENT_TYPE = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
}
PLACEHOLDER_RE = re.compile(r"%(\w+)%")

# Features that require explicit enabling per server
OPT_IN_FEATURES = {
    "welcome_card",
    "automod_masked_links",
    "automod_non_member_links",
    "automod_spam_detection",
    "twitter_fix",
    "message_log",
    "custom_reactions",
}
OPT_IN_COMMANDS = {
    "test",
    "whois",
    "cr",
}

# ---- Moderation ----


def _require_int_env(name: str) -> int:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return int(value)


def _int_list_env(name: str) -> list[int]:
    raw = os.environ.get(name, "")
    return [int(part) for part in raw.split(",") if part.strip()]

def _optional_int_env(name: str) -> int | None:
    """Reads an optional integer env var. Returns None if unset/empty.
    Raises RuntimeError if the value is set but not a valid integer."""
    value = os.environ.get(name)
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        raise RuntimeError(f"Environment variable {name} must be an integer, got: {value}")
      
# Channel in the dev guild where bot-internal logs (command usage, bot
# message deletions) are posted for safety/abuse monitoring.
BOTMSG_CHANNEL_ID: int | None = _optional_int_env("BOTMSG_CHANNEL")
DMLOG_CHANNEL_ID: int = _require_int_env("DMLOG_CHANNEL_ID")
YOUR_USER_IDS: set[int] = set(_int_list_env("YOUR_USER_IDS"))
DEV_GUILD_ID: int = _require_int_env("DEV_GUILD_ID")
WHITELISTED_GUILDS: list[int] = _int_list_env("WHITELISTED_GUILDS")

# End of IDs
url_regex = re.compile(r'(https?://\S+|www\.\S+)', re.IGNORECASE)
masked_link_regex = re.compile(
    r'\[.+?\]\((https?://\S+?|www\.\S+?)\)', re.IGNORECASE)
ALLOWED_ROLE = "Members"
TWT_REGEX = re.compile(r"https?://(?:www\.)?(?:twitter\.com|x\.com)/\S+")
TRAILING_PUNCT = ".,!?)]}>\"'"

leaveimg = "assets/leaveimg.png"
kickimg = "assets/kickimg.png"
bannedimg = "assets/bannedimg.png"
readimg = "assets/readimg.png"
DELETE_EMOJI = "❌"

# ---- Log channel types ----
LOG_CHANNEL_CHOICES_DATA = [
    ("Automod Log", "automod-log"),
    ("Message Log", "message-log"),
    ("Punishment Log", "punishment-log"),
    ("Leave Log", "leave-log"),
    ("Server Log", "server-log"),
    ("User Log", "user-log"),
    ("Pinboard", "pinboard"),
]

# ---- AI welcome card ----
AI_MODEL = "google/gemini-3.1-flash-lite"

PERMISSION_DISPLAY_NAMES: dict[str, str] = {
    "add_reactions": "Add Reactions",
    "administrator": "Administrator",
    "attach_files": "Attach Files",
    "ban_members": "Ban Members",
    "bypass_slowmode": "Bypass Slowmode",
    "change_nickname": "Change Nickname",
    "connect": "Connect",
    "create_events": "Create Events",
    "create_expressions": "Create Expressions",
    "create_instant_invite": "Create Invite",
    "create_private_threads": "Create Private Threads",
    "create_public_threads": "Create Public Threads",
    "deafen_members": "Deafen Members",
    "embed_links": "Embed Links",
    "external_emojis": "Use External Emojis",
    "external_stickers": "Use External Stickers",
    "kick_members": "Kick Members",
    "manage_channels": "Manage Channels",
    "manage_events": "Manage Events",
    "manage_expressions": "Manage Expressions",
    "manage_guild": "Manage Server",
    "manage_messages": "Manage Messages",
    "manage_nicknames": "Manage Nicknames",
    "manage_roles": "Manage Roles",
    "manage_threads": "Manage Threads",
    "manage_webhooks": "Manage Webhooks",
    "mention_everyone": "Mention @everyone, @here, and All Roles",
    "moderate_members": "Timeout Members",
    "move_members": "Move Members",
    "mute_members": "Mute Members",
    "pin_messages": "Pin Messages",
    "priority_speaker": "Priority Speaker",
    "read_message_history": "Read Message History",
    "read_messages": "View Channels",
    "request_to_speak": "Request to Speak",
    "send_messages": "Send Messages",
    "send_messages_in_threads": "Send Messages in Threads",
    "send_polls": "Create Polls",
    "send_tts_messages": "Send Text-to-Speech Messages",
    "send_voice_messages": "Send Voice Messages",
    "set_voice_channel_status": "Set Voice Channel Status",
    "speak": "Speak",
    "stream": "Video",
    "use_application_commands": "Use Application Commands",
    "use_embedded_activities": "Use Activities",
    "use_external_apps": "Use External Apps",
    "use_external_sounds": "Use External Sounds",
    "use_soundboard": "Use Soundboard",
    "use_voice_activation": "Use Voice Activity",
    "view_audit_log": "View Audit Log",
    "view_creator_monetization_analytics": "View Creator Monetization Insights",
    "view_guild_insights": "View Server Insights",
}
FEATURE_CHOICES_DATA = [
    ("Welcome Card", "welcome_card"),
    ("Automod: Masked Links in []", "automod_masked_links"),
    ("Automod: Non-Member Links", "automod_non_member_links"),
    ("Automod: Spam Detection", "automod_spam_detection"),
    ("Twitter/X Link Fixing", "twitter_fix"),
    ("Message Log (delete/edits)", "message_log"),
    ("Custom Reactions", "custom_reactions"),
    ("Username Update Log", "username_update_log"),
]


def friendly_permission_name(name: str) -> str:
    """Looks up a discord.py permission flag's current Discord UI label.
    Falls back to a title-cased guess for anything not yet in the table
    above (e.g. a permission Discord added after this was last updated)."""
    return PERMISSION_DISPLAY_NAMES.get(name, name.replace("_", " ").title())
