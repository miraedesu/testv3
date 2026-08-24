"""Per-channel user uwulock: uwuify a specific user's text messages and repost via webhook."""
from __future__ import annotations

#--- Imports ---
import json
import logging
import random
import re
import time
from collections import deque
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from common.safeguard import bot_can_webhook_send
from common.reaction_helpers import pick_random_phrase
from common.settings_store import (
    clear_guild_setting,
    get_guild_setting,
    set_guild_setting,
)

logger = logging.getLogger(__name__)

#--- Uwuify constants ---

EMOTE_POOL_NAME = "uwuemotes"

#--- Custom Discord emotes: <:name:id> and <a:name:id> ---
CUSTOM_EMOTE_RE = re.compile(r"<a?:\w+:\d+>")

#--- Used inside _uwuify_word to skip emote-shaped tokens ---
CUSTOM_EMOTE_TOKEN_RE = re.compile(r"^<a?:\w+:\d+>$")

#--- Unicode emoji ---
EMOJI_RE = re.compile(
    "["
    "\U0001F600-\U0001F64F"
    "\U0001F300-\U0001F5FF"
    "\U0001F680-\U0001F6FF"
    "\U0001F1E0-\U0001F1FF"
    "\U0001F900-\U0001F9FF"
    "\U0001FA00-\U0001FA6F"
    "\U0001FA70-\U0001FAFF"
    "\U00002600-\U000026FF"
    "\U00002700-\U000027BF"
    "]+",
    flags=re.UNICODE,
)

#--- Per-word probabilities ---
STUTTER_CHANCE = 0.12      # per word in long sentences
ACTION_CHANCE = 0.04       # per word (actions still at word boundaries)

#--- Per-sentence probabilities (faces only at sentence end) ---
SENTENCE_FACE_CHANCE = 0.35   # long sentences get a face at end
SHORT_SENTENCE_MAX_WORDS = 3  # sentences with ≤3 words always get stutter + face

#--- Faces: pure emoticons / kaomoji / short cute interjections (NO actions) ---
FACES = [
    "OwO", "UwU", "uwu", "owo", ">w<", "^w^", ":3", "x3",
    "ÚwÚ", "^-^", ";;w;;", ">~<", ">.<",
    "(・`ω´・)", "(・ω・)", "(*≧▽≦)", "(ꈍᴗꈍ)",
    "(ᗒᗨᗕ)", "^•ﻌ•^", "(◕‿◕)",
    "rawr xD", "nyaa~", "mrrp", "hehe~", "teehee",
    "👉👈", "🥺",
]

#--- Actions: always wrapped in *asterisks* ---
ACTIONS = [
    "*boops your nose*", "*nuzzles*", "*blushes*", "*whispers uwu*",
    "*holds paws*", "*wags tail*", "*glomps you*", "*pounces*",
    "*licks ear*", "*curls up*", "*snuggles closer*", "*gives you headpats*",
    "*rolls over*", "*makes biscuits*", "*does a little spin*", "*wiggles*",
    "*bonks you softly*", "*flops down dramatically*", "*chases own tail*",
    "*knocks your coffee off the desk*", "*giggles*", "*notices bulge*",
    "*twirls hair*", "*tilts head*", "*blinks owishly*",
    "*screams internally*", "*stares cutely*", "*pads over*",
]

WEBHOOK_NAME = "UwuLock"
URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)

#--- Circuit breaker: tracks recent deletions across all channels ---
_deletion_log: deque[float] = deque()
_CIRCUIT_MAX_DELETIONS = 20       # max deletions in the window
_CIRCUIT_WINDOW_SEC = 60          # 60-second rolling window
_CIRCUIT_TRIPPED: bool = False    # latch — stays tripped until cog reload


def _circuit_check() -> bool:
    """Return True if it's safe to delete, False if the circuit is tripped."""
    global _CIRCUIT_TRIPPED
    if _CIRCUIT_TRIPPED:
        return False

    now = time.monotonic()
    #--- Evict timestamps older than the window ---
    while _deletion_log and now - _deletion_log[0] > _CIRCUIT_WINDOW_SEC:
        _deletion_log.popleft()

    if len(_deletion_log) >= _CIRCUIT_MAX_DELETIONS:
        _CIRCUIT_TRIPPED = True
        logger.critical(
            "[UwuLock] CIRCUIT BREAKER TRIPPED — %d deletions in %d seconds. "
            "Cog disabled until reload. Investigate immediately.",
            _CIRCUIT_MAX_DELETIONS, _CIRCUIT_WINDOW_SEC,
        )
        return False

    return True


def _circuit_record() -> None:
    """Record a deletion in the circuit breaker log."""
    _deletion_log.append(time.monotonic())


#--- Uwuify core ---

async def replace_emotes(bot, text: str, guild_id: int) -> str:
    """Replace every emote (custom Discord + unicode emoji) in `text`
    with a random emote from the `uwuemotes` pool.

    Pool entries are expected to be Discord custom emote strings
    (e.g. ``<:catjam:123>``). If the pool is empty or missing the
    original text is returned unchanged — emotes are NOT silently
    stripped, so the admin notices when the pool needs filling.
    """
    if not text:
        return text

    #--- Collect all emote spans ---
    spans: list[tuple[int, int]] = []
    spans.extend((m.start(), m.end()) for m in CUSTOM_EMOTE_RE.finditer(text))
    spans.extend((m.start(), m.end()) for m in EMOJI_RE.finditer(text))

    if not spans:
        return text

    #--- Sort + dedupe overlapping spans ---
    spans.sort()
    dedup: list[tuple[int, int]] = []
    last_end = -1
    for s, e in spans:
        if s >= last_end:
            dedup.append((s, e))
            last_end = e
    spans = dedup

    #--- Fetch one pool emote per emote found ---
    #--- If pool is empty on the very first lookup, bail out entirely
    #--- so original emotes are preserved rather than vanishing.
    phrases: list[str] = []
    for _ in spans:
        phrase = await pick_random_phrase(bot, EMOTE_POOL_NAME, guild_id)
        if phrase is None:
            return text  #--- Pool empty/missing — preserve original emotes ---
        phrases.append(phrase)

    #--- Apply replacements right-to-left ---
    out = text
    for (start, end), phrase in zip(reversed(spans), reversed(phrases)):
        out = out[:start] + phrase + out[end:]
    return out


def _uwuify_word(word: str) -> str:
    """Apply uwu substitutions to a single word.
    Skips tokens that are Discord custom emotes (<:name:id> / <a:name:id>).
    """
    #--- Don't mangle emote strings ---
    if CUSTOM_EMOTE_TOKEN_RE.match(word):
        return word

    w = word

    #--- Letter swaps ---
    w = w.replace("r", "w").replace("R", "W")
    w = w.replace("l", "w").replace("L", "W")

    #--- "th" → "d" ---
    w = w.replace("th", "d").replace("TH", "D").replace("Th", "D")

    #--- "n" followed by vowel → "ny" ---
    w = re.sub(r"n([aeiouAEIOU])", r"ny\1", w)
    w = re.sub(r"N([aeiouAEIOU])", r"Ny\1", w)

    #--- "ove" → "uv" ---
    w = w.replace("ove", "uv").replace("Ove", "Uv")

    #--- "st" → "sd" (softening) ---
    w = w.replace("st", "sd").replace("St", "Sd")

    return w


async def uwuify_message(bot, text: str, guild_id: int) -> str:
    """Full uwuify pipeline: strip URLs → replace emotes → uwuify text.

    Emotes are replaced BEFORE uwuify so pool emote strings pass through
    _uwuify_word untouched (it skips <a?:\\w+:\\d+> tokens).
    """
    #--- Strip URLs ---
    text = URL_RE.sub("", text).strip()
    if not text:
        return ""

    #--- Replace emotes with pool emotes (async — hits DB) ---
    text = await replace_emotes(bot, text, guild_id)
    if not text:
        return ""

    #--- Uwuify per-sentence (sync, no DB) ---
    return _uwuify_sentences(text)


def _uwuify_sentences(text: str) -> str:
    """Split into sentences, apply per-sentence stutter / face / action.
    Emote tokens (<:name:id>) pass through untouched.
    """
    #--- Split into sentences (delimiter stays attached) ---
    raw_sentences = re.split(r"(?<=[.!?])\s+", text)

    result_sentences = []

    for sentence in raw_sentences:
        sentence = sentence.strip()
        if not sentence:
            continue

        words = sentence.split()
        if not words:
            continue

        word_count = len(words)
        is_short = word_count <= SHORT_SENTENCE_MAX_WORDS

        uwu_words = []

        for i, word in enumerate(words):
            uwu_word = _uwuify_word(word)

            #--- Stutter decision ---
            should_stutter = False
            if is_short and i == 0:
                should_stutter = True            #--- guaranteed on first word ---
            elif random.random() < STUTTER_CHANCE:
                should_stutter = True

            #--- Don't stutter on emote tokens ---
            if should_stutter and uwu_word and not CUSTOM_EMOTE_TOKEN_RE.match(uwu_word):
                if uwu_word[0].isalpha():
                    uwu_word = f"{uwu_word[0]}-{uwu_word}"

            uwu_words.append(uwu_word)

            #--- Action at word boundary ---
            if random.random() < ACTION_CHANCE:
                uwu_words.append(random.choice(ACTIONS))

        #--- Face at end of sentence ---
        if is_short:
            uwu_words.append(random.choice(FACES))
        elif random.random() < SENTENCE_FACE_CHANCE:
            uwu_words.append(random.choice(FACES))

        result_sentences.append(" ".join(uwu_words))

    return "  ".join(result_sentences)


#--- Webhook helper ---

_webhook_cache: dict[int, discord.Webhook] = {}


async def _get_webhook(
    channel: discord.TextChannel | discord.ForumChannel,
) -> Optional[discord.Webhook]:
    """Find or create the UwuLock webhook for a channel. Caches by channel ID."""
    #--- Check cache first ---
    cached = _webhook_cache.get(channel.id)
    if cached is not None:
        try:
            await cached.fetch()  #--- Verify it still exists ---
            return cached
        except (discord.NotFound, discord.Forbidden):
            _webhook_cache.pop(channel.id, None)

    #--- Lookup or create ---
    try:
        webhooks = await channel.webhooks()
        for wh in webhooks:
            #--- Only trust webhooks created by the bot itself ---
            if wh.name == WEBHOOK_NAME and wh.user is not None and wh.user.id == channel.guild.me.id:
                _webhook_cache[channel.id] = wh
                return wh
        wh = await channel.create_webhook(name=WEBHOOK_NAME)
        _webhook_cache[channel.id] = wh
        return wh
    except discord.Forbidden:
        logger.warning("[UwuLock] Missing Manage Webhooks permission in #%s", channel)
        return None
    except Exception:
        logger.exception("[UwuLock] Failed to get/create webhook in #%s", channel)
        return None


#--- Cog: UwuLock ---

class UwuLock(commands.Cog):
    """Uwulock a user in a channel: their text messages are uwuified and reposted via webhook."""

    uwulock = app_commands.Group(
        name="uwulock",
        description="Toggle uwuification for specific users in this channel",
        default_permissions=discord.Permissions(administrator=True),
    )

    def __init__(self, bot):
        self.bot = bot

    #--- Storage helpers ---

    async def _get_locked_users(self, guild_id: int, channel_id: int) -> list[int]:
        """Read the JSON list of uwulocked user IDs for this channel."""
        raw = await get_guild_setting(self.bot, guild_id, f"uwulock:{channel_id}")
        if not raw:
            return []
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return []

    async def _set_locked_users(
        self, guild_id: int, channel_id: int, users: list[int]
    ) -> None:
        """Write the JSON list of uwulocked user IDs. Clears the key if list is empty."""
        if users:
            await set_guild_setting(
                self.bot, guild_id, f"uwulock:{channel_id}", json.dumps(users)
            )
        else:
            await clear_guild_setting(self.bot, guild_id, f"uwulock:{channel_id}")

    #--- /uwulock add ---

    @uwulock.command(name="add", description="Uwuify a user's text messages in this channel")
    @app_commands.describe(user="The user to uwulock")
    async def uwulock_add(self, interaction: discord.Interaction, user: discord.Member):
        """Add a user to this channel's uwulock list."""
        if not isinstance(interaction.channel, (discord.TextChannel, discord.Thread)):
            await interaction.response.send_message(
                "UwuLock only works in text channels and threads.", ephemeral=True
            )
            return

        locked = await self._get_locked_users(interaction.guild_id, interaction.channel_id)
        if user.id in locked:
            await interaction.response.send_message(
                f"{user.display_name} is already uwulocked here.", ephemeral=True
            )
            return

        locked.append(user.id)
        await self._set_locked_users(interaction.guild_id, interaction.channel_id, locked)
        await interaction.response.send_message(
            f"Uwulocked {user.mention} in this channel. OwO", ephemeral=True
        )

    #--- /uwulock remove ---

    @uwulock.command(name="remove", description="Stop uwuifying a user's messages in this channel")
    @app_commands.describe(user="The user to remove from uwulock")
    async def uwulock_remove(self, interaction: discord.Interaction, user: discord.Member):
        """Remove a user from this channel's uwulock list."""
        locked = await self._get_locked_users(interaction.guild_id, interaction.channel_id)
        if user.id not in locked:
            await interaction.response.send_message(
                f"{user.display_name} isn't uwulocked here.", ephemeral=True
            )
            return

        locked.remove(user.id)
        await self._set_locked_users(interaction.guild_id, interaction.channel_id, locked)
        await interaction.response.send_message(
            f"Un-uwulocked {user.mention}. Back to normal. UwU", ephemeral=True
        )

    #--- /uwulock list ---

    @uwulock.command(name="list", description="Show who's uwulocked in this channel")
    async def uwulock_list(self, interaction: discord.Interaction):
        """List all uwulocked users in the current channel."""
        locked = await self._get_locked_users(interaction.guild_id, interaction.channel_id)
        if not locked:
            await interaction.response.send_message(
                "No one is uwulocked in this channel.", ephemeral=True
            )
            return

        lines = []
        for uid in locked:
            member = interaction.guild.get_member(uid)
            name = member.display_name if member else f"Unknown ({uid})"
            lines.append(f"• {name}")

        embed = discord.Embed(
            title="UwuLock — this channel",
            description="\n".join(lines),
            color=0xFFC0CB,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    #--- /uwulock clear ---

    @uwulock.command(name="clear", description="Remove all uwulocks in this channel")
    async def uwulock_clear(self, interaction: discord.Interaction):
        """Remove every user from this channel's uwulock list."""
        await self._set_locked_users(interaction.guild_id, interaction.channel_id, [])
        await interaction.response.send_message(
            "Cleared all uwulocks in this channel. :3", ephemeral=True
        )

    #--- on_message: uwuify + repost ---

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """If author is uwulocked in this channel, uwuify and repost via webhook.

        Delete-first for snappy UX. Webhook is resolved BEFORE deletion so the
        common failure case (no webhook available) doesn't lose the message.
        Circuit breaker prevents runaway deletion if something goes wrong.
        """
        #--- Circuit breaker latch ---
        if _CIRCUIT_TRIPPED:
            return

        #--- Bail on DMs, bots, webhooks, system messages ---
        if message.guild is None or message.author.bot or message.webhook_id is not None:
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

        #--- Is this user uwulocked here? (cheap DB lookup, run BEFORE perms) ---
        locked = await self._get_locked_users(message.guild.id, channel.id)
        if message.author.id not in locked:
            return

        #--- Per-user-triggered safety check (only runs when needed) ---
        # For threads, webhook perms come from the parent channel.
        if isinstance(channel, discord.Thread):
            perm_target = channel.parent or channel
        else:
            perm_target = channel
        if not bot_can_webhook_send(perm_target, also_manage_messages=True):
            #--- No perms to delete+webhook here. Leave the original message. ---
            return

        #--- Strip URLs, check if any text remains ---
        text = message.content
        text_stripped = URL_RE.sub("", text).strip()
        if not text_stripped:
            return

        #--- Uwuify (async — includes emote replacement) ---
        uwu_text = await uwuify_message(self.bot, text, message.guild.id)
        if not uwu_text:
            return

        #--- Circuit breaker check BEFORE any deletion ---
        if not _circuit_check():
            return

        #--- Resolve webhook BEFORE deleting (catch common failure early) ---
        if isinstance(channel, discord.Thread):
            webhook_channel = channel.parent
            thread_kwarg = {"thread": channel}
        else:
            webhook_channel = channel
            thread_kwarg = {}

        if webhook_channel is None:
            return  #--- Parent channel deleted — can't create webhook ---

        webhook = await _get_webhook(webhook_channel)
        if webhook is None:
            return  #--- No webhook available — don't delete, original preserved ---

        #--- Delete original FIRST (snappy UX) ---
        try:
            await message.delete()
            _circuit_record()
        except discord.NotFound:
            pass  #--- Already deleted ---
        except Exception:
            logger.exception("[UwuLock] Failed to delete message %s", message.id)
            return

        #--- Send uwuified text via webhook ---
        try:
            await webhook.send(
                content=uwu_text,
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
            #--- Webhook failed after deletion — message lost, log loudly ---
            logger.error(
                "[UwuLock] Webhook send FAILED after deleting original "
                "(message id %s, channel %s, author %s). Original message is lost.",
                message.id, channel, message.author,
            )


#--- Cog entry point ---

async def setup(bot):
    await bot.add_cog(UwuLock(bot))