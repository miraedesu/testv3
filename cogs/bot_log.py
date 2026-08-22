"""Bot self-monitoring: logs slash command usage and bot message deletions
to a designated channel in the dev guild. Used to detect admin/user abuse."""
from __future__ import annotations

import asyncio
import discord
from discord.ext import commands
import logging

from common.constants import BOTMSG_CHANNEL_ID

logger = logging.getLogger(__name__)


# ApplicationCommandOptionType values
_OPT_USER = 6
_OPT_CHANNEL = 7
_OPT_ROLE = 8
_OPT_MENTIONABLE = 9
_OPT_ATTACHMENT = 11
_OPT_BOOLEAN = 5
_OPT_SUBCOMMAND = 1
_OPT_SUBCOMMAND_GROUP = 2


def _format_value(value, opt_type: int, resolved: dict) -> str:
    """Format a single option value based on its type and resolved data."""
    if opt_type == _OPT_USER:
        user = resolved.get("users", {}).get(str(value))
        if user:
            username = user.get("username", "?")
            return f"<@{value}> ({username})"
        return f"<@{value}>"
    elif opt_type == _OPT_CHANNEL:
        ch = resolved.get("channels", {}).get(str(value))
        if ch:
            name = ch.get("name", "?")
            return f"<#{value}> ({name})"
        return f"<#{value}>"
    elif opt_type == _OPT_ROLE:
        role = resolved.get("roles", {}).get(str(value))
        if role:
            name = role.get("name", "?")
            return f"<@&{value}> ({name})"
        return f"<@&{value}>"
    elif opt_type == _OPT_MENTIONABLE:
        return f"<@{value}>"
    elif opt_type == _OPT_ATTACHMENT:
        att = resolved.get("attachments", {}).get(str(value))
        if att:
            filename = att.get("filename", "attachment")
            url = att.get("url", "")
            return f"[{filename}]({url})" if url else f"attachment:{filename}"
        return f"attachment:{value}"
    elif opt_type == _OPT_BOOLEAN:
        return "True" if value else "False"
    else:
        s = str(value)
        return s if len(s) <= 200 else s[:197] + "..."


def _walk_options(options: list[dict], resolved: dict, prefix: str = "") -> list[tuple[str, str]]:
    """Recursively walk options, returning list of (name, formatted_value)."""
    pairs = []
    for opt in options or []:
        name = opt.get("name", "?")
        opt_type = opt.get("type", 0)
        full_name = f"{prefix} {name}".strip() if prefix else name

        if opt_type in (_OPT_SUBCOMMAND, _OPT_SUBCOMMAND_GROUP):
            pairs.extend(_walk_options(opt.get("options", []), resolved, full_name))
        elif "value" in opt:
            pairs.append((full_name, _format_value(opt["value"], opt_type, resolved)))
    return pairs


def _build_command_path(interaction: discord.Interaction) -> str:
    """Build full command path like 'cr pool add' from interaction data."""
    data = interaction.data or {}
    parts = [data.get("name", "?")]

    def walk(opts):
        for opt in opts or []:
            if opt.get("type") in (_OPT_SUBCOMMAND, _OPT_SUBCOMMAND_GROUP):
                parts.append(opt.get("name", "?"))
                walk(opt.get("options"))
                return

    walk(data.get("options"))
    return " ".join(parts)


class BotLog(commands.Cog):
    """Logs slash command usage and bot message deletions to a dev-guild
    channel for safety/auditing purposes."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._log_channel: discord.TextChannel | None = None
        # ── Rate limiting ──
        self._log_window_start: int = 0
        self._log_count: int = 0
        self._rate_limit_dropped: int = 0
        if BOTMSG_CHANNEL_ID is None:
            logger.warning("[BotLog] BOTMSG_CHANNEL not set in .env — cog will be inert.")
        else:
            self._resolve_task = asyncio.create_task(self._resolve_log_channel())
            self.bot._self_deleted_messages: set[int] = set()

    async def _send_log(self, embed: discord.Embed) -> None:
        if self._log_channel is None:
            return

        now_ts = int(discord.utils.utcnow().timestamp())

        # Reset 60-second window
        if now_ts - self._log_window_start >= 60:
            # If we dropped logs in the previous window, send a summary
            if self._rate_limit_dropped > 0:
                summary = discord.Embed(
                    title=f"🔇 Rate-limited — {self._rate_limit_dropped} log(s) dropped in the last minute",
                    color=discord.Color.dark_grey(),
                    timestamp=discord.utils.utcnow(),
                )
                try:
                    await self._log_channel.send(embed=summary)
                except (discord.Forbidden, discord.HTTPException):
                    pass
            self._log_window_start = now_ts
            self._log_count = 0
            self._rate_limit_dropped = 0

        # Hard cap: 20 logs per 60 seconds
        if self._log_count >= 20:
            self._rate_limit_dropped += 1
            return

        self._log_count += 1
        try:
            await self._log_channel.send(embed=embed)
        except discord.Forbidden:
            logger.warning("[BotLog] Cannot send to log channel — check permissions.")
        except discord.HTTPException as e:
            logger.warning(f"[BotLog] HTTP error sending log: {e}")
    async def _resolve_log_channel(self) -> None:
        await self.bot.wait_until_ready()
        try:
            ch = self.bot.get_channel(BOTMSG_CHANNEL_ID) or await self.bot.fetch_channel(BOTMSG_CHANNEL_ID)
            if isinstance(ch, discord.TextChannel):
                self._log_channel = ch
                logger.info(f"[BotLog] Logging to #{ch.name} ({ch.id}).")
            else:
                logger.error(f"[BotLog] BOTMSG_CHANNEL_ID ({BOTMSG_CHANNEL_ID}) is not a text channel.")
        except discord.NotFound:
            logger.error(f"[BotLog] BOTMSG_CHANNEL_ID ({BOTMSG_CHANNEL_ID}) not found.")
        except discord.Forbidden:
            logger.error(f"[BotLog] Bot lacks access to BOTMSG_CHANNEL_ID ({BOTMSG_CHANNEL_ID}).")
        except Exception as e:
            logger.error(f"[BotLog] Error resolving log channel: {e}")



    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        """Log all slash command invocations (success or failure)."""
        if interaction.type != discord.InteractionType.application_command:
            return
        if self._log_channel is None:
            return

        data = interaction.data or {}
        resolved = data.get("resolved", {})
        command_path = _build_command_path(interaction)
        options = _walk_options(data.get("options", []), resolved)

        if interaction.guild:
            location = f"{interaction.guild.name} (`{interaction.guild.id}`)"
            if interaction.channel:
                location += f"\n📍 {interaction.channel.mention}"
        else:
            location = "DM"

        embed = discord.Embed(
            title=f"🔧 Command Used: `/{command_path}`",
            color=discord.Color.blurple(),
            timestamp=discord.utils.utcnow(),
        )
        embed.set_author(
            name=f"{interaction.user} ({interaction.user.id})",
            icon_url=interaction.user.display_avatar.url,
        )
        embed.add_field(name="Location", value=location, inline=False)

        if options:
            lines = [f"**{name}:** {value}" for name, value in options]
            embed.add_field(
                name="Inputs",
                value="\n".join(lines)[:1024],
                inline=False,
            )
        else:
            embed.add_field(name="Inputs", value="*(none)*", inline=False)

        await self._send_log(embed)

    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message):
        """Log when the bot's own messages (incl. embeds) are deleted.
        Skips messages the bot deleted itself (auto-cleanup).
        Preserves the original embed structure; metadata in proper fields
        so mentions and timestamps actually render."""
        if message.author.id != self.bot.user.id:
            return
        if self._log_channel is None:
            return
        # Skip deletions in our own log channel (would be recursive noise)
        if message.channel.id == self._log_channel.id:
            return
        # Skip empty bot messages (no content, no embeds, no attachments — nothing to audit)
        if not message.content and not message.embeds and not message.attachments:
            return        
        # Skip messages the bot is deleting itself (twitter-fix cleanup, etc.)
        if message.id in getattr(self.bot, "_self_deleted_messages", set()):
            self.bot._self_deleted_messages.discard(message.id)
            return
        # Check audit log FIRST — before building anything
        deleted_by = None
        if message.guild:
            await asyncio.sleep(1.5)  # let audit log populate
            try:
                now = discord.utils.utcnow()
                async for entry in message.guild.audit_logs(
                    action=discord.AuditLogAction.message_delete,
                    limit=5,
                ):
                    if (entry.target.id == self.bot.user.id
                            and (now - entry.created_at).total_seconds() < 15):
                        deleted_by = entry.user
                        break
            except discord.Forbidden:
                pass
            except Exception as e:
                logger.debug(f"[BotLog] Audit log check failed: {e}")

        # Skip if the bot itself deleted the message (auto-delete / cleanup)
        if deleted_by is not None and deleted_by.id == self.bot.user.id:
            return

        sent_ts = int(message.created_at.timestamp())

        # Build the log embed with proper fields (renders mentions + timestamps)
        log_embed = discord.Embed(
            title="🗑️ Bot Message Deleted",
            color=discord.Color.red(),
            timestamp=discord.utils.utcnow(),
        )

        # Channel as a clickable mention (only renders in fields, not footer)
        if message.guild:
            channel_value = f"{message.channel.mention} (`{message.channel.id}`)"
            log_embed.add_field(
                name="📍 Channel",
                value=channel_value,
                inline=False,
            )
        else:
            log_embed.add_field(name="📍 Channel", value="DM channel", inline=False)

        # Sent timestamp — renders as a live Discord timestamp in fields
        log_embed.add_field(
            name="🕒 Sent",
            value=f"<t:{sent_ts}:F> (<t:{sent_ts}:R>)",
            inline=False,
        )

        # Deleted by (only if known from audit log)
        if deleted_by is not None:
            log_embed.add_field(
                name="👤 Deleted By",
                value=f"{deleted_by.mention} (`{deleted_by.id}`)",
                inline=False,
            )

        # Original text content in description (4096 char limit)
        if message.content:
            content = message.content if len(message.content) <= 4096 else message.content[:4093] + "..."
            log_embed.description = content

        # Footer — just the message ID (the only thing that renders cleanly here)
        log_embed.set_footer(text=f"Message ID: {message.id}")

        # Send log embed + original embeds preserved as-is (up to 9, 10 total max)
        embeds_to_send = [log_embed] + list(message.embeds[:9])

        try:
            await self._log_channel.send(embeds=embeds_to_send)
        except discord.Forbidden:
            logger.warning("[BotLog] Cannot send to log channel — check permissions.")
        except discord.HTTPException as e:
            logger.warning(f"[BotLog] HTTP error sending log: {e}")

async def setup(bot: commands.Bot):
    await bot.add_cog(BotLog(bot))