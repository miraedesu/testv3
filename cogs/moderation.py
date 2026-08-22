"""Message moderation pipeline: link filters, spam/scam detection, twitter-link
fixing, pinboard logging, and the guild whitelist. Owns on_message directly
(not just a @commands.Cog.listener) so that ordering is guaranteed: a message
handled/deleted by a filter here never also triggers a custom reaction.
"""
from __future__ import annotations

import asyncio
import datetime
import io
import logging
import os
from collections import defaultdict

import discord
from discord.ext import commands, tasks
from PIL import Image
from common.feature_toggles import is_feature_disabled, get_disabled_features
from common.constants import (
    ALLOWED_ROLE,
    DELETE_EMOJI,
    DMLOG_CHANNEL_ID,
    TRAILING_PUNCT,
    TWT_REGEX,
    WHITELISTED_GUILDS,
    YOUR_USER_IDS,
    masked_link_regex,
    url_regex,
    readimg,
)
from common.settings_store import get_log_channel



anti_spam_cache = defaultdict(list)
spam_handling_in_progress = set()
# user_id -> datetime, blocks re-triggering for 30s
recently_punished: dict[int, datetime.datetime] = {}
pending_deletes: dict[int, int] = {}
_recycle_url = "https://abs.twimg.com/emoji/v2/72x72/267b.png"

logger = logging.getLogger(__name__)

def fix_link(match) -> str:
    link = match.group(0)

    trail = ""
    while link and link[-1] in TRAILING_PUNCT:
        trail = link[-1] + trail
        link = link[:-1]

    if "twitter.com" in link:
        link = link.replace("twitter.com", "vxtwitter.com")
    elif "x.com" in link:
        link = link.replace("x.com", "vxtwitter.com")

    return link + trail


def generate_snapshot_canvas(attachment_bytes_list: list):
    """CPU-bound Pillow compilation function. Keeps the main async loop lag-free."""
    START_X = 50
    START_Y = 40
    GAP = 40
    IMG_WIDTH = 800
    IMG_HEIGHT = 600

    attachment_bytes_list = attachment_bytes_list[:4]
    num_rows = (len(attachment_bytes_list) + 1) // 2
    final_height = START_Y + (num_rows * (IMG_HEIGHT + GAP)) + 40

    img = Image.new("RGBA", (1740, final_height), color=(54, 57, 63, 255))

    for index, attr_bytes in enumerate(attachment_bytes_list):
        try:
            with Image.open(io.BytesIO(attr_bytes)) as attach_img:
                col = index % 2
                row = index // 2
                x_pos = START_X + (col * (IMG_WIDTH + GAP))
                y_pos = START_Y + (row * (IMG_HEIGHT + GAP))

                slot = Image.new("RGB", (IMG_WIDTH, IMG_HEIGHT),
                                 color=(54, 57, 63, 255))
                attach_img.thumbnail((IMG_WIDTH, IMG_HEIGHT))

                offs_x = (IMG_WIDTH - attach_img.width) // 2
                offs_y = (IMG_HEIGHT - attach_img.height) // 2

                attach_rgba = attach_img.convert("RGBA")
                slot.paste(attach_rgba, (offs_x, offs_y), attach_rgba)
                img.paste(slot, (x_pos, y_pos))
        except Exception as e:
            logger.error(
                f"⚠️ Skipping item index {index} in canvas build (Not a verifiable image asset): {e}")

    return img


class Moderation(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.cleanup_caches.start()
        self._logged_pins: set[int] = set()

    def cog_unload(self):
        self.cleanup_caches.cancel()

    @commands.Cog.listener()
    async def on_ready(self):
        global _recycle_url
        custom_emoji = discord.utils.get(self.bot.emojis, name="recycle")
        _recycle_url = custom_emoji.url if custom_emoji else "https://abs.twimg.com/emoji/v2/72x72/267b.png"

    @tasks.loop(minutes=5)
    async def cleanup_caches(self):
        now = discord.utils.utcnow()

        expired_punished = [
            uid for uid, ts in recently_punished.items()
            if (now - ts).total_seconds() >= 30
        ]
        for uid in expired_punished:
            del recently_punished[uid]

        stale_window = datetime.timedelta(seconds=15)
        for uid in list(anti_spam_cache.keys()):
            anti_spam_cache[uid] = [
                msg for msg in anti_spam_cache[uid]
                if now - msg['timestamp'] < stale_window
            ]
            if not anti_spam_cache[uid]:
                del anti_spam_cache[uid]

    @cleanup_caches.before_loop
    async def before_cleanup_caches(self):
        await self.bot.wait_until_ready()

    # ---------------- guild whitelist ----------------

    async def check_guild_whitelist(self, guild: discord.Guild):
        """Checks if a guild is whitelisted. If not, logs it and leaves."""
        if guild.id not in WHITELISTED_GUILDS:
            logger.info(
                f"Joined unauthorized guild: {guild.name} ({guild.id}). Leaving immediately...")
            if guild.system_channel and guild.system_channel.permissions_for(guild.me).send_messages:
                try:
                    await guild.system_channel.send("This bot is private and not whitelisted for this server. Leaving...")
                except discord.Forbidden:
                    pass
            await guild.leave()

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        await self.check_guild_whitelist(guild)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        # Enforce whitelist immediately upon entrance
        if member.guild.id not in WHITELISTED_GUILDS:
            await self.check_guild_whitelist(member.guild)
            return
        if await is_feature_disabled(self.bot, member.guild.id, "welcome_card"):
            return
        welcome_cog = self.bot.get_cog("Welcome")
        if welcome_cog:
            await welcome_cog.queue.put(member)

    # ---------------- on_message pipeline ----------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.author == self.bot.user:
            return

        # --- DM Processing (must be first, before any guild access) ---
        if message.guild is None:
            log_channel = self.bot.get_channel(DMLOG_CHANNEL_ID)
            if log_channel:
                embed = discord.Embed(title="You got new DM", color=0x3498db)
                embed.set_author(name=f"{message.author}", icon_url=message.author.display_avatar.url)
                msg_text = message.content if message.content else "(No text content)"
                embed.add_field(name="Message:", value=msg_text, inline=False)
                if message.attachments:
                    links = "\n".join([att.url for att in message.attachments])
                    embed.add_field(name="Attachments:", value=links, inline=False)
                    if message.attachments[0].content_type and message.attachments[0].content_type.startswith("image"):
                        embed.set_image(url=message.attachments[0].url)
                embed.add_field(name="User ID:", value=f"`{message.author.id}`", inline=False)
                embed.set_footer(text="Reply using: !reply [ID] [message]")
                await log_channel.send(embed=embed)
            return

        # Outbound Reply Handler System
        if message.channel.id == DMLOG_CHANNEL_ID and message.content.startswith("!reply"):
            is_auth = message.author.id in YOUR_USER_IDS
            if not is_auth:
                return

            parts = message.content.split(" ", 2)
            if len(parts) < 2:
                await message.channel.send("❌ Syntax: `!reply [User_ID] [Message]` (or attach an image)")
                return

            try:
                target_user_id = int(parts[1])
            except ValueError:
                await message.channel.send("❌ Invalid User ID layout. Must be numeric.")
                return

            reply_content = parts[2] if len(parts) == 3 else ""
            if not reply_content and not message.attachments:
                await message.channel.send("❌ Syntax: Cannot send an empty reply.")
                return

            try:
                target_user = await self.bot.fetch_user(target_user_id)
                files_to_send = []
                for attachment in message.attachments:
                    files_to_send.append(await attachment.to_file())

                await target_user.send(content=reply_content, files=files_to_send)
                await message.add_reaction("✅")
                return
            except Exception as e:
                await message.channel.send(f"❌ Failed to send DM: {e}")
                return

        # Server Content Moderation Layer
        recycle_url = _recycle_url
        log_channel = await get_log_channel(self.bot, message.guild.id, "automod-log")
        disabled_features = await get_disabled_features(self.bot, message.guild.id, message.channel.id)

        # Hidden link filter — catches [text](url) markdown for everyone
        if "automod_masked_links" not in disabled_features and masked_link_regex.search(message.content):
            if log_channel:
                try:
                    embed = discord.Embed(
                        color=0xffb6c1, timestamp=discord.utils.utcnow())
                    embed.set_author(
                        name="Deleted Hidden link / Possibly scam", icon_url=recycle_url)
                    embed.add_field(
                        name="User", value=message.author.mention, inline=True)
                    embed.add_field(
                        name="User ID", value=f"{message.author.id}", inline=True)
                    embed.add_field(
                        name="Channel", value=message.channel.mention, inline=True)
                    if message.content:
                        embed.add_field(
                            name="Message Content:", value=f"`{message.content[:500]}`", inline=False)
                    embed.add_field(
                        name="Account Created", value=f"<t:{int(message.author.created_at.timestamp())}:D>", inline=True)
                    embed.add_field(
                        name="Joined Server",
                        value=f"<t:{int(message.author.joined_at.timestamp())}:D>" if message.author.joined_at else "Unknown",
                        inline=True,
                    )
                    await log_channel.send(embed=embed)
                except Exception as e:
                    logger.error(f"Error handling hidden link filter: {e}")
            else:
                logger.info(
                    f"⚠️ [Security Warning] Spam in '{message.guild.name}' but automod-log is unconfigured.")
            try:
                await message.delete()
            except discord.Forbidden:
                logger.info(
                    f"❌ Cannot delete scam message in <#{message.channel.id}> - Missing 'Manage Messages' permission.")
            try:
                await message.author.timeout(
                    datetime.timedelta(minutes=1),
                    reason="Posted a hidden link / Possible Scam",
                )
            except discord.Forbidden:
                logger.info(
                    f"❌ Could not timeout {message.author} (Role hierarchy restriction).")
            return

        # Non-members mandatory remove URLs
        elif "automod_non_member_links" not in disabled_features and url_regex.search(message.content):
            from cogs.guild_settings import resolve_allowed_role
            allowed_role = await resolve_allowed_role(self.bot, message.guild)
            if not allowed_role:
                logger.info(
                    f"⚠️ [Configuration Warning] A URL was found, but no allowed "
                    f"role is configured in '{message.guild.name}'. "
                    f"Use /guild_settings allowed_role to set one.")
            if allowed_role and allowed_role not in message.author.roles:
                if log_channel:
                    try:
                        embed = discord.Embed(
                            color=0xffb6c1, timestamp=discord.utils.utcnow())
                        embed.set_author(
                            name=f"Deleted message / User without role @{ALLOWED_ROLE}", icon_url=recycle_url)
                        embed.add_field(
                            name="User", value=message.author.mention, inline=True)
                        embed.add_field(
                            name="User ID", value=f"`{message.author.id}`", inline=True)
                        embed.add_field(
                            name="Channel", value=message.channel.mention, inline=True)
                        if message.content:
                            embed.add_field(
                                name="Message Content:", value=f"`{message.content[:500]}`", inline=False)
                            embed.add_field(
                                name="Account Created", value=f"<t:{int(message.author.created_at.timestamp())}:D>", inline=True)
                            embed.add_field(
                                name="Joined Server",
                                value=f"<t:{int(message.author.joined_at.timestamp())}:D>" if message.author.joined_at else "Unknown",
                                inline=True,
                            )
                        await log_channel.send(embed=embed)
                    except Exception as e:
                        logger.error(f"Error handling non-member link filter: {e}")
                else:
                    logger.info(
                        f"⚠️ [Security Warning] Spam in '{message.guild.name}' but automod-log is unconfigured.")
                try:
                    await message.delete()
                except discord.Forbidden:
                    logger.info(
                        f"❌ Cannot delete scam message in <#{message.channel.id}> - Missing 'Manage Messages' permission.")
                return

        # Normalize text content to catch identical copy-pastes
        content = None
        if "automod_spam_detection" not in disabled_features:
            content = message.content.strip().lower()
            if not content and message.attachments:
                content = f"[file_spam]:{message.attachments[0].filename}_{message.attachments[0].size}"
        if content:
            now = discord.utils.utcnow()
            user_id = message.author.id
            channel_id = message.channel.id

            if user_id in recently_punished:
                if (now - recently_punished[user_id]).total_seconds() < 30:
                    try:
                        await message.delete()
                    except Exception:
                        pass
                    return
                else:
                    del recently_punished[user_id]

            time_window = datetime.timedelta(seconds=15)
            anti_spam_cache[user_id] = [
                msg for msg in anti_spam_cache[user_id]
                if now - msg['timestamp'] < time_window
            ]

            anti_spam_cache[user_id].append({
                'content': content,
                'channel_id': channel_id,
                'timestamp': now,
                'msg_obj': message,
            })

            matching_channels = set()
            for msg in anti_spam_cache[user_id]:
                if msg['content'] == content:
                    matching_channels.add(msg['channel_id'])

            if len(matching_channels) >= 3:
                if user_id in spam_handling_in_progress:
                    return
                spam_handling_in_progress.add(user_id)

                recently_punished[user_id] = now

                await asyncio.sleep(0.5)

                messages_to_purge = [
                    msg['msg_obj'] for msg in anti_spam_cache[user_id]
                    if msg['content'] == content
                ]

                try:
                    files_to_send_as_logs = []

                    if log_channel:
                        image_attachments = []
                        other_attachments = []

                        seen_attachments = set()
                        for msg_obj in messages_to_purge:
                            for att in msg_obj.attachments:
                                key = (att.filename, att.size)
                                if key in seen_attachments:
                                    continue
                                seen_attachments.add(key)
                                if att.content_type and att.content_type.startswith("image"):
                                    image_attachments.append(att)
                                else:
                                    other_attachments.append(att)

                        if image_attachments:
                            try:
                                attachment_bytes_list = []
                                for att in image_attachments[:4]:
                                    attachment_bytes_list.append(await att.read())

                                pil_img = await asyncio.to_thread(generate_snapshot_canvas, attachment_bytes_list)
                                img_bin = io.BytesIO()
                                pil_img.save(img_bin, format="PNG")
                                img_bin.seek(0)
                                files_to_send_as_logs.append(discord.File(
                                    fp=img_bin, filename="snapshot.png"))
                            except Exception as e:
                                logger.error(
                                    f"❌ Cross-channel snapshot processing failed: {e}")

                        try:
                            channels_mentions = ", ".join(
                                [f"<#{ch_id}>" for ch_id in matching_channels])
                            embed = discord.Embed(
                                color=0xffb6c1, timestamp=discord.utils.utcnow())
                            embed.set_author(
                                name="Deleted Possible Scam/Spam", icon_url=recycle_url)
                            embed.add_field(
                                name="User:", value=message.author.mention, inline=True)
                            embed.add_field(
                                name="User ID", value=f"{message.author.id}", inline=True)
                            embed.add_field(
                                name="Channels:", value=channels_mentions, inline=False)
                            embed.add_field(
                                name="Action taken:", value="`1 hour timeout`", inline=True)
                            if message.content:
                                clean_content = message.content[:1024]
                                embed.add_field(
                                    name="Message:", value=f"`{clean_content}`", inline=False)
                            embed.add_field(
                                name="Account Created", value=f"<t:{int(message.author.created_at.timestamp())}:D>", inline=True)
                            embed.add_field(
                                name="Joined Server",
                                value=f"<t:{int(message.author.joined_at.timestamp())}:D>" if message.author.joined_at else "Unknown",
                                inline=True,
                            )
                            if other_attachments:
                                other_files_text = "\n".join(
                                    [f"`{att.filename}` — {round(att.size / 1024, 1)} KB" for att in other_attachments]
                                )
                                embed.add_field(
                                    name="Other Attachments:", value=other_files_text[:1024], inline=False)
                            if image_attachments:
                                embed.set_image(
                                    url="attachment://snapshot.png")

                            await log_channel.send(embed=embed, files=files_to_send_as_logs)
                        except discord.Forbidden:
                            logger.info(
                                f"❌ Missing write permissions for automod-log channel: {log_channel.mention}")
                    else:
                        logger.info(
                            f"⚠️ [Security Warning] Spam in '{message.guild.name}' but automod-log is unconfigured.")

                    try:
                        await message.author.timeout(datetime.timedelta(minutes=60), reason="Possible Scam/Spam")
                        for msg_obj in messages_to_purge:
                            try:
                                await msg_obj.delete()
                            except discord.DiscordException:
                                pass
                    except discord.Forbidden:
                        logger.info(
                            f"❌ Hierarchy blocked: Cannot modify {message.author}.")

                finally:
                    anti_spam_cache[user_id].clear()
                    spam_handling_in_progress.discard(user_id)
                return

        if "twitter_fix" not in disabled_features and TWT_REGEX.search(message.content):
            fixed_content = TWT_REGEX.sub(fix_link, message.content)
            deleted = False
            try:
                await message.delete()
                deleted = True
            except discord.Forbidden:
                pass
            except discord.HTTPException:
                pass

            if not deleted:
                try:
                    await message.edit(suppress=True)
                except (discord.Forbidden, discord.HTTPException):
                    pass
            custom_mentions = [
                user for user in message.mentions if user.id != message.author.id]
            sent = await message.channel.send(
                f"**From** {message.author.mention}:\n{fixed_content}:\n*If you are the author you can delete this message by reacting with ❌*",
                allowed_mentions=discord.AllowedMentions(
                    users=custom_mentions, roles=False, everyone=False),
            )
            await sent.add_reaction(DELETE_EMOJI)
            pending_deletes[sent.id] = message.author.id

        # Custom reactions auto-responder -- runs last, so a message already
        # handled/deleted above never also fires a keyword reaction.
        if "custom_reactions" not in disabled_features:
            reactions_cog = self.bot.get_cog("Reactions")
            if reactions_cog:
                await reactions_cog.trigger_check(message)

        await self.bot.process_commands(message)

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if payload.message_id not in pending_deletes:
            return
        if str(payload.emoji) != DELETE_EMOJI:
            return
        if payload.user_id != pending_deletes[payload.message_id]:
            return

        channel = self.bot.get_channel(payload.channel_id)
        if channel is None:
            return

        try:
            msg = await channel.fetch_message(payload.message_id)
            if hasattr(self.bot, "_self_deleted_messages"):
                self.bot._self_deleted_messages.add(payload.message_id)
            await msg.delete()
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass
        finally:
            pending_deletes.pop(payload.message_id, None)

    # Pinboard
    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent):
        if "pinned" not in payload.data:
            return
        if not payload.data["pinned"]:
            return
        if await is_feature_disabled(self.bot, payload.guild_id, "pinboard"):
            return
        if payload.message_id in self._logged_pins:  # Already logged
            return
        self._logged_pins.add(payload.message_id)
        channel = self.bot.get_channel(payload.channel_id)
        if channel is None:
            return

        log_channel = await get_log_channel(self.bot, payload.guild_id, "pinboard")
        if log_channel is None:
            return

        try:
            message = await channel.fetch_message(payload.message_id)
        except discord.NotFound:
            return
        except discord.Forbidden:
            logger.info(
                f"Permission Denied: Cannot fetch messages from channel #{channel.name} ({channel.id})")
            return

        embed = discord.Embed(color=0xffb6c1, timestamp=discord.utils.utcnow())
        embed.set_author(name=f"{message.author}",
                         icon_url=message.author.display_avatar.url)

        msg_text = message.content if message.content else ""
        embed.description = f"[Jump to message]({message.jump_url})\n{msg_text}"

        if message.attachments:
            first_image = next(
                (att for att in message.attachments if att.content_type and att.content_type.startswith(
                    "image")),
                None,
            )
            if first_image:
                embed.set_image(url=first_image.url)
        embed.set_footer(text=f"{message.author.id}")
        try:
            await log_channel.send(embed=embed)
        except discord.Forbidden:
            logger.info(
                f"Permission Denied: Cannot send log embeds to #{log_channel.name} ({log_channel.id})")
            return
# ---------------- message log: deletes / edits ----------------

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        if payload.guild_id is None:
            return

        cached = payload.cached_message
        if cached is not None and cached.author.bot:
            return

        if await is_feature_disabled(self.bot, payload.guild_id, "message_log", payload.channel_id):
            return

        log_channel = await get_log_channel(self.bot, payload.guild_id, "message-log")
        if log_channel is None:
            return

        channel = self.bot.get_channel(payload.channel_id)
        now = discord.utils.utcnow()

        moderator = None
        guild = self.bot.get_guild(payload.guild_id)
        if guild is not None:
            try:
                async for entry in guild.audit_logs(action=discord.AuditLogAction.message_delete, limit=5):
                    if (now - entry.created_at).total_seconds() >= 15:
                        continue
                    if entry.extra.channel.id != payload.channel_id:
                        continue
                    if cached is not None and entry.target.id != cached.author.id:
                        continue
                    moderator = entry.user
                    break
            except discord.Forbidden:
                logger.info("Bot lacks 'View Audit Log' permission to check logs.")

        embed = discord.Embed(title="🗑️ Message Deleted",
                              color=discord.Color.red(), timestamp=now)
        embed.set_thumbnail(url=f"attachment://{os.path.basename(readimg)}")

        if cached is not None:
            embed.set_author(name=f"{cached.author} ({cached.author.id})",
                             icon_url=cached.author.display_avatar.url)

        embed.add_field(name="Message ID:",
                        value=payload.message_id, inline=True)
        if channel is not None:
            embed.add_field(name="Channel:",
                            value=channel.mention, inline=True)
        if moderator is not None:
            embed.add_field(name="Deleted by:",
                            value=moderator.mention, inline=True)

        if cached is not None:
            content_display = f"`{cached.content}`" if cached.content else "*(no text content)*"
            embed.add_field(
                name="Content", value=content_display[:1024], inline=False)
            if cached.attachments:
                attach_list = "\n".join(
                    f"{a.filename}" for a in cached.attachments)
                embed.add_field(name="Attachments:",
                                value=attach_list[:1024], inline=False)
        else:
            embed.add_field(
                name="Content", value="*(message wasn't cached -- content unavailable)*", inline=False)

        try:
            await log_channel.send(embed=embed, file=discord.File(readimg))
        except discord.Forbidden:
            logger.info(
                f"Permission Denied: Cannot send logs to #{log_channel.name}")

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent):
        if payload.guild_id is None:
            return

        if await is_feature_disabled(self.bot, payload.guild_id, "message_log", payload.channel_id):
            return

        log_channel = await get_log_channel(self.bot, payload.guild_id, "message-log")
        if log_channel is None:
            return

        channel = self.bot.get_channel(payload.channel_id)
        non_bot_cached = [
            m for m in payload.cached_messages if not m.author.bot]

        embed = discord.Embed(
            title="🗑️ Bulk Message Delete",
            description=f"{len(payload.message_ids)} message(s) were deleted"
            + (f" in {channel.mention}" if channel is not None else "") + ".",
            color=discord.Color.red(),
            timestamp=discord.utils.utcnow(),
        )
        if non_bot_cached:
            authors = sorted({str(m.author) for m in non_bot_cached})
            embed.add_field(name="Known authors", value=", ".join(
                authors)[:1024], inline=False)

        try:
            await log_channel.send(embed=embed)
        except discord.Forbidden:
            logger.info(
                f"Permission Denied: Cannot send logs to #{log_channel.name}")

    @commands.Cog.listener("on_raw_message_edit")
    async def on_message_log_edit(self, payload: discord.RawMessageUpdateEvent):
        if payload.guild_id is None:
            return

        cached = payload.cached_message
        if cached is not None and cached.author.bot:
            return

        new_content = payload.data.get("content")
        if new_content is None:
            return
        if cached is not None and cached.content == new_content:
            return

        if await is_feature_disabled(self.bot, payload.guild_id, "message_log", payload.channel_id):
            return

        log_channel = await get_log_channel(self.bot, payload.guild_id, "message-log")
        if log_channel is None:
            return

        channel = self.bot.get_channel(payload.channel_id)
        jump_url = f"https://discord.com/channels/{payload.guild_id}/{payload.channel_id}/{payload.message_id}"
        embed = discord.Embed(title="ℹ️ Message Edited",
                              description=f"[Jump to Message]({jump_url})",
                              color=discord.Color.purple(),
                              timestamp=discord.utils.utcnow())
        embed.set_thumbnail(url=f"attachment://{os.path.basename(readimg)}")

        if cached is not None:
            embed.set_author(name=f"{cached.author} ({cached.author.id})",
                             icon_url=cached.author.display_avatar.url)
        else:
            author_id = payload.data.get("author", {}).get("id", "Unknown")
            embed.set_author(name=f"User ID: {author_id}")

        embed.add_field(name="Message ID:",
                        value=payload.message_id, inline=True)
        if channel is not None:
            embed.add_field(name="Channel:",
                            value=channel.mention, inline=True)

        if cached is not None:
            old_display = f"`{cached.content}`" if cached.content else "*(no text content)*"
            embed.add_field(
                name="Old Message:", value=old_display[:1024], inline=False)
        else:
            embed.add_field(
                name="Old Message:", value="*(original content wasn't cached)*", inline=False)

        new_display = f"`{new_content}`" if new_content else "*(no text content)*"
        embed.add_field(name="New Message:",
                        value=new_display[:1024], inline=False)

        try:
            await log_channel.send(embed=embed, file=discord.File(readimg))
        except discord.Forbidden:
            logger.info(
                f"Permission Denied: Cannot send logs to #{log_channel.name}")

async def setup(bot: commands.Bot):
    await bot.add_cog(Moderation(bot))
