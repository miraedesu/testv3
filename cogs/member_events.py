"""Member/guild lifecycle logging: leave/kick/ban, role changes, boosts."""
from __future__ import annotations

import json
import asyncio
import time
import os 
import discord
from discord.ext import commands
import logging
import re

from common.constants import bannedimg, friendly_permission_name, kickimg, leaveimg
from common.settings_store import get_log_channel
from common.feature_toggles import is_feature_disabled



# Tracks the last boost-count change already logged per guild, so a single
# boost/unboost event doesn't get logged twice by two different handlers
# that both react to the same underlying change.
logger = logging.getLogger(__name__)

def _parse_booster_id(text: str, guild: discord.Guild) -> int | None:
    """Resolve a user ID from a mention, raw ID, or guild nickname/username."""
    text = text.strip()
    m = re.match(r'^<@!?(\d+)>$', text)
    if m:
        return int(m.group(1))
    if text.isdigit():
        return int(text)
    member = guild.get_member_named(text)
    return member.id if member else None


async def apply_boost_attribution(
    bot, message: discord.Message, user_id: int, admin: discord.User, guild: discord.Guild,
) -> bool:
    """Transform a 'Boost Count Changed' embed into a 'Boost Removed' embed,
    remove the attribution button, and decrement the user's boost_list entry
    if they're in it. Returns True on success."""
    member = guild.get_member(user_id)

    embed = discord.Embed(
        title="<:boostremove:1534198057036419214> Boost Removed",
        description=(
            f"{member.mention} is no longer "
            f"boosting the server."
        ),
        color=discord.Color.red(),
        timestamp=discord.utils.utcnow(),
    )
    if member:
        embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(
        name="User",
        value=f"{member} (`{user_id}`)" if member else f"`{user_id}` (not in server)",
        inline=False,
    )
    embed.add_field(name="Edited by", value=admin.mention, inline=False)
    embed.set_footer(
        text=f"Total boosts: {guild.premium_subscription_count} • Level {guild.premium_tier}"
    )

    try:
        await message.edit(embed=embed, view=None)
    except (discord.NotFound, discord.Forbidden):
        return False

    # Decrement boost_list entry if the user is tracked
    try:
        from cogs.boost_list import decrement_boost_count
        await decrement_boost_count(bot, guild.id, user_id)
    except ImportError:
        pass
    return True


class BoostAttributionModal(discord.ui.Modal, title="Attribute Boost Removal"):
    booster_name = discord.ui.TextInput(
        label="Who unboosted? (user ID, @mention, or name)",
        placeholder="e.g. @john, john, or 123456789012345678",
        required=True,
        max_length=100,
    )

    def __init__(self, embed_message: discord.Message, admin: discord.Member):
        super().__init__()
        self.embed_message = embed_message
        self.admin = admin

    async def on_submit(self, interaction: discord.Interaction):
        user_id = _parse_booster_id(self.booster_name.value, interaction.guild)
        if user_id is None:
            await interaction.response.send_message(
                f"❌ Couldn't resolve `{self.booster_name.value}` to a user. "
                f"Try a user ID, @mention, or exact username.",
                ephemeral=True,
            )
            return

        ok = await apply_boost_attribution(
            interaction.client, self.embed_message, user_id,
            self.admin, interaction.guild,
        )
        if ok:
            await interaction.response.send_message(
                f"✅ Boost removal attributed to <@{user_id}>. "
                f"Embed updated and boost_list adjusted.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                "❌ Could not edit the original message — it may be deleted or I lack permissions.",
                ephemeral=True,
            )
class BoostAttributionView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)  # Persistent — no expiry

    @discord.ui.button(
        label="I know who it was",
        style=discord.ButtonStyle.secondary,
        custom_id="boost_attribution:click",
    )
    async def attribute_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message(
                "❌ Only server admins can attribute boost removals.",
                ephemeral=True,
            )
            return

        modal = BoostAttributionModal(
            embed_message=interaction.message,
            admin=interaction.user,
        )
        await interaction.response.send_modal(modal)
def diff_permissions(before: discord.Permissions, after: discord.Permissions) -> tuple[list[str], list[str]]:
    """Returns (granted, revoked) permission names between two Permissions."""
    before_perms = dict(before)
    after_perms = dict(after)
    granted = [name for name, value in after_perms.items() if value and not before_perms.get(name)]
    revoked = [name for name, value in before_perms.items() if value and not after_perms.get(name)]
    return granted, revoked


def describe_overwrite_value(value: bool | None) -> str:
    """Renders a PermissionOverwrite's tri-state value (True/False/None) as
    Allow/Deny/Neutral, matching Discord's own UI language for overwrites."""
    if value is True:
        return "Allow"
    elif value is False:
        return "Deny"
    return "Neutral"




class MemberEvents(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._channel_snapshots = {}
        self._snapshot_timers = {}
        self._last_layout = {}

    async def cog_load(self) -> None:
        """On startup: sync DB boost state and register persistent views."""
        self.bot.add_view(BoostAttributionView())
        asyncio.create_task(self._sync_all_boosts())
    def cog_unload(self):
        """Cancel any pending snapshot debounce timers."""
        for task in self._snapshot_timers.values():
            task.cancel()
        self._snapshot_timers.clear()
    async def _sync_all_boosts(self) -> None:
        """Background sync — waits for bot readiness, then reconciles each guild."""
        await self.bot.wait_until_ready()
        for guild in self.bot.guilds:
            try:
                await self._sync_boosts(guild)
            except Exception as e:
                logger.error(
                    f"[MemberEvents] Boost sync error for guild {guild.id} ({guild.name}): {e}"
                )

    async def _sync_boosts(self, guild: discord.Guild) -> None:
        """Compare DB-tracked boosters against guild.premium_subscribers.
        - Stale rows (in DB but member no longer boosting) → DELETE + log
        - Missing rows (boosting but not in DB) → INSERT silently (no log,
          because we can't reliably tell when it started while the bot was offline)
        """
        actual_boosters: dict[int, discord.Member] = {
            m.id: m for m in guild.premium_subscribers
        }

        async with self.bot.db.execute(
            "SELECT user_id FROM guild_boosts WHERE guild_id = ?",
            (guild.id,),
        ) as cursor:
            db_rows = await cursor.fetchall()

        db_user_ids = {row[0] for row in db_rows}
        actual_ids = set(actual_boosters.keys())

        # 1. Clean up stale entries — members who were boosting when the bot
        #    went down but have since stopped (without on_member_update firing).
        stale = db_user_ids - actual_ids
        if stale:
            for user_id in stale:
                await self.bot.db.execute(
                    "DELETE FROM guild_boosts WHERE guild_id = ? AND user_id = ?",
                    (guild.id, user_id),
                )
            await self.bot.db.commit()
            logger.info(
                f"[MemberEvents] Cleaned {len(stale)} stale boost entries "
                f"for {guild.name} ({guild.id})"
            )

        # 2. Insert missing boosters silently (bot was offline when they started).
        missing = actual_ids - db_user_ids
        for user_id in missing:
            member = actual_boosters[user_id]
            started_ts = (
                int(member.premium_since.timestamp())
                if member.premium_since
                else int(discord.utils.utcnow().timestamp())
            )
            await self.bot.db.execute(
                "INSERT OR IGNORE INTO guild_boosts "
                "(guild_id, user_id, started_at) VALUES (?, ?, ?)",
                (guild.id, user_id, started_ts),
            )
        if missing:
            await self.bot.db.commit()
            logger.info(
                f"[MemberEvents] Added {len(missing)} missing boost entries "
                f"for {guild.name} ({guild.id}) — silent sync"
            )
    # def _take_snapshot(self, guild: discord.Guild) -> dict[int, tuple[int, int | None]]:
    def _take_snapshot(self, guild: discord.Guild):
        """Capture {channel_id: (position, category_id)} for all channels in a guild."""
        return {c.id: (c.position, c.category_id) for c in guild.channels}
    
    def _capture_layout(self, guild: discord.Guild) -> dict:
        """Capture the current channel layout as a JSON-serializable dict."""
        layout = {"categories": [], "uncategorized": []}
        
        for cat in sorted(guild.categories, key=lambda c: c.position):
            cat_data = {"name": cat.name, "channels": []}
            for ch in sorted(cat.channels, key=lambda c: c.position):
                cat_data["channels"].append({
                    "name": ch.name,
                    "type": "voice" if isinstance(ch, discord.VoiceChannel) else "text"
                })
            layout["categories"].append(cat_data)
            
        for ch in guild.channels:
            if ch.category is None and not isinstance(ch, discord.CategoryChannel):
                layout["uncategorized"].append({
                    "name": ch.name,
                    "type": "voice" if isinstance(ch, discord.VoiceChannel) else "text"
                })
                
        return layout
    def _trigger_snapshot_buffer(self, guild: discord.Guild):
        """Starts/resets a 10-minute timer. If no more moves happen for 10 mins, it saves a snapshot."""
        if guild.id in self._snapshot_timers:
            self._snapshot_timers[guild.id].cancel()
            
        # 10 minute buffer (600 seconds)
        self._snapshot_timers[guild.id] = asyncio.create_task(self._save_snapshot(guild))

    async def _save_snapshot(self, guild: discord.Guild):
        try:
            await asyncio.sleep(600)
        except asyncio.CancelledError:
            return

        if await is_feature_disabled(self.bot, guild.id, "channel_layout_screenshot"):
            return

        layout = self._capture_layout(guild)
        layout_json = json.dumps(layout)

        if self._last_layout.get(guild.id) == layout_json:
            logger.info(f"[History] Channel layout in {guild.name} reverted to original state. Skipping snapshot.")
            return

        now_ts = int(discord.utils.utcnow().timestamp())

        async with self.bot.db.execute(
            "SELECT COALESCE(MAX(snapshot_num), 0) + 1 FROM channel_snapshots WHERE guild_id = ?",
            (guild.id,),
        ) as cursor:
            snap_num = (await cursor.fetchone())[0]

        await self.bot.db.execute(
            "INSERT INTO channel_snapshots (guild_id, snapshot_num, snapshot_data, created_at) "
            "VALUES (?, ?, ?, ?)",
            (guild.id, snap_num, layout_json, now_ts),
        )
        await self.bot.db.commit()

        self._last_layout[guild.id] = layout_json

        log_channel = await get_log_channel(self.bot, guild.id, "server-log")
        if log_channel:
            embed = discord.Embed(
                title="<:category:1534195833430474982> Channel Layout Changed",
                description=(
                    f"Some channels were moved.\n"
                    f"Use `/snapshot view snapshot_num:{snap_num}` to see current layout."
                ),
                color=discord.Color.blue(),
                timestamp=discord.utils.utcnow(),
            )
            try:
                await log_channel.send(embed=embed)
            except discord.Forbidden:
                pass
    @commands.Cog.listener()
    async def on_ready(self):
        """Initialize in-memory tracking for all guilds on startup.
        Baseline DB snapshots are handled by the Misc cog."""
        for guild in self.bot.guilds:
            # 1. Save in-memory snapshot for position tracking
            self._channel_snapshots[guild.id] = self._take_snapshot(guild)

            # 2. Initialize the last layout memory (used by _save_snapshot
            #    to skip saving if the layout reverted to its prior state)
            if guild.id not in self._last_layout:
                self._last_layout[guild.id] = json.dumps(self._capture_layout(guild))

            # 3. Save an initial baseline snapshot to the database (gated)
            if await is_feature_disabled(self.bot, guild.id, "channel_layout_screenshot"):
                continue

            layout = self._capture_layout(guild)
            now_ts = int(discord.utils.utcnow().timestamp())

            async with self.bot.db.execute(
                "SELECT 1 FROM channel_snapshots WHERE guild_id = ? AND created_at > ?",
                (guild.id, now_ts - 3600),
            ) as cursor:
                recent = await cursor.fetchone()

            if not recent:
                async with self.bot.db.execute(
                    "SELECT COALESCE(MAX(snapshot_num), 0) + 1 FROM channel_snapshots WHERE guild_id = ?",
                    (guild.id,),
                ) as cursor:
                    snap_num = (await cursor.fetchone())[0]

                await self.bot.db.execute(
                    "INSERT INTO channel_snapshots (guild_id, snapshot_num, snapshot_data, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (guild.id, snap_num, json.dumps(layout), now_ts),
                )
                await self.bot.db.commit()
                logger.info(f"[MemberEvents] Saved baseline snapshot #{snap_num} for {guild.name}")
    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        self._channel_snapshots[guild.id] = self._take_snapshot(guild)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        guild = member.guild
        if member.bot:
            return  # or log separately
        # Wait 1 second to allow Discord's Audit Log to process the kick/ban
        await asyncio.sleep(1)

        now = discord.utils.utcnow()
        action_title = "Member Left"
        embed_color = discord.Color.blue()
        reason = "User left the server."
        moderator = None

        chosen_img = leaveimg
        log_checks = [
            (discord.AuditLogAction.ban, "Member Banned", discord.Color.red(), bannedimg),
            (discord.AuditLogAction.kick, "Member Kicked", discord.Color.gold(), kickimg),
        ]

        try:
            for action_type, title, color, cornerimg in log_checks:
                async for entry in guild.audit_logs(action=action_type, limit=3):
                    if entry.target.id == member.id and (now - entry.created_at).total_seconds() < 15:
                        action_title = title
                        embed_color = color
                        reason = entry.reason or "No reason provided."
                        moderator = entry.user
                        chosen_img = cornerimg
                        break
                if moderator:
                    break
        except discord.Forbidden:
            logger.info("Bot lacks 'View Audit Log' permission to check logs.")

        # Kicks/bans go to punishment-log; a plain departure goes to leave-log.
        if not await is_feature_disabled(self.bot, guild.id, "member_leave_log"):
            log_type = "punishment-log" if moderator else "leave-log"
            log_channel = await get_log_channel(self.bot, guild.id, log_type)

            if log_channel is not None:
                roles = [role.mention for role in member.roles if not role.is_default()]
                roles_string = " ".join(roles) if roles else "No roles assigned."

                embed = discord.Embed(
                    title=action_title,
                    description=f"{member.mention} | {member.name}",
                    color=embed_color,
                    timestamp=discord.utils.utcnow(),
                )
                embed.set_thumbnail(url=f"attachment://{os.path.basename(chosen_img)}")

                if moderator:
                    embed.add_field(name="Action by:", value=moderator.mention, inline=True)
                    embed.add_field(name="Reason", value=reason, inline=True)
                    embed.add_field(name="\u200b", value="\u200b", inline=True)
                
                embed.add_field(name="Account Created", value=f"<t:{int(member.created_at.timestamp())}:D>", inline=True)
                embed.add_field(
                    name="Joined Server",
                    value=f"<t:{int(member.joined_at.timestamp())}:D>" if member.joined_at else "Unknown",
                    inline=True,
                )
                embed.add_field(name=f"Roles Had ({len(roles)})", value=roles_string, inline=False)
                embed.set_footer(text=f"{member.id}", icon_url=member.display_avatar.url)

                try:
                    await log_channel.send(file=discord.File(chosen_img), embed=embed)
                except discord.Forbidden:
                    logger.info(f"Permission Denied: Cannot send logs to #{log_channel.name}")
        # --- Track kicks/bans in the DB for WhoIs ---
        if moderator:
            action_type = "ban" if action_title == "Member Banned" else "kick"
            ts = int(discord.utils.utcnow().timestamp())
            try:
                await self.bot.db.execute(
                    "INSERT INTO punishment_history (user_id, guild_id, action, reason, moderator_id, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
                    (member.id, guild.id, action_type, reason, moderator.id, ts)
                )
                await self.bot.db.commit()
            except Exception as e:
                logger.error(f"[MemberEvents] Error tracking punishment: {e}")

        # Independent concern: a boosting member leaving loses their boost.
        if member.premium_since is None:
            return

        # DB-backed dedup: only log if the row actually existed.
        try:
            cursor = await self.bot.db.execute(
                "DELETE FROM guild_boosts WHERE guild_id = ? AND user_id = ?",
                (guild.id, member.id),
            )
            await self.bot.db.commit()
            if cursor.rowcount == 0:
                return  # Already logged/handled — prevent double log
        except Exception as e:
            logger.error(f"[MemberEvents] Error removing boost row on leave: {e}")
            return

        boost_log_channel = await get_log_channel(self.bot, guild.id, "server-log")
        if boost_log_channel is None:
            return

        boost_embed = discord.Embed(
            title="<:boostremove:1534198057036419214> Boost Removed (member left)",
            description=f"{member} left the server and their boost is gone with them.",
            color=discord.Color.dark_grey(),
            timestamp=discord.utils.utcnow(),
        )
        boost_embed.set_thumbnail(url=member.display_avatar.url)
        boost_embed.add_field(name="User", value=f"{member} (`{member.id}`)", inline=False)
        boost_embed.set_footer(
            text=f"Total boosts: {guild.premium_subscription_count} • Level {guild.premium_tier}"
        )

        try:
            await boost_log_channel.send(embed=boost_embed)
        except discord.Forbidden:
            logger.info(f"Permission Denied: Cannot send logs to #{boost_log_channel.name}")      
    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        # --- Username change logging & DB tracking ---
        if before.name != after.name:
            ts = int(discord.utils.utcnow().timestamp())

            # Log to user-log channel (gated by username_update_log feature)
            if not await is_feature_disabled(self.bot, after.guild.id, "username_update_log"):
                user_log_channel = await get_log_channel(self.bot, after.guild.id, "user-log")
                if user_log_channel:
                    embed = discord.Embed(
                        title="ℹ️ Username Updated",
                        description=f"{after.mention} changed their username.",
                        color=discord.Color.blue(),
                        timestamp=discord.utils.utcnow()
                    )
                    embed.set_author(name=f"{after} ({after.id})", icon_url=after.display_avatar.url)
                    embed.add_field(name="Old Name", value=before.name, inline=True)
                    embed.add_field(name="New Name", value=after.name, inline=True)
                    embed.add_field(name="User ID", value=str(after.id), inline=False)
                    try:
                        await user_log_channel.send(embed=embed)
                    except discord.Forbidden:
                        pass

            # Save to DB
            try:
                # 1. Only insert Old Name if it doesn't exist for this user at all
                async with self.bot.db.execute(
                    "SELECT 1 FROM username_history WHERE user_id = ? AND username = ? LIMIT 1",
                    (before.id, before.name)
                ) as cursor:
                    if not await cursor.fetchone():
                        await self.bot.db.execute(
                            "INSERT OR IGNORE INTO username_history (user_id, username, timestamp) VALUES (?, ?, ?)",
                            (before.id, before.name, ts - 1)
                        )
                
                # 2. Always insert New Name
                await self.bot.db.execute(
                    "INSERT OR IGNORE INTO username_history (user_id, username, timestamp) VALUES (?, ?, ?)",
                    (after.id, after.name, ts)
                )
                await self.bot.db.commit()
            except Exception as e:
                logger.error(f"[MemberEvents] Error tracking username change: {e}")

        # --- Role-change logging (independent of the boost check below) ---
        if before.roles != after.roles:
            if await is_feature_disabled(self.bot, after.guild.id, "role_change_log"):
                pass  # Skip role-change logging
            else:
                user_log_channel = await get_log_channel(self.bot, after.guild.id, "user-log")
                if user_log_channel is not None:
                    await asyncio.sleep(1)
                    now = discord.utils.utcnow()
                    async for entry in after.guild.audit_logs(action=discord.AuditLogAction.member_role_update, limit=5):
                        if entry.target.id != after.id:
                            continue
                        if (now - entry.created_at).total_seconds() >= 15:
                            continue
                        if entry.user.bot:
                            break
                        if entry.user.id == after.id:
                            break

                        added_roles = [role for role in after.roles if role not in before.roles]
                        removed_roles = [role for role in before.roles if role not in after.roles]

                        if added_roles:
                            role_title = "Added Role"
                            role_color = discord.Color.green()
                        elif removed_roles:
                            role_title = "Removed Role"
                            role_color = discord.Color.red()
                        else:
                            break

                        embed = discord.Embed(
                            title=role_title,
                            timestamp=discord.utils.utcnow(),
                            description=f"Roles for {after.mention} were updated by {entry.user.mention}.",
                            color=role_color,
                        )
                        if added_roles:
                            embed.add_field(name="Added", value=", ".join(role.mention for role in added_roles), inline=False)
                        if removed_roles:
                            embed.add_field(name="Removed", value=", ".join(role.mention for role in removed_roles), inline=False)

                        embed.add_field(name="Account Created", value=f"<t:{int(after.created_at.timestamp())}:D>", inline=True)
                        embed.add_field(
                            name="Joined Server",
                            value=f"<t:{int(after.joined_at.timestamp())}:D>" if after.joined_at else "Unknown",
                            inline=True,
                        )
                        embed.set_footer(text=f"User ID: {after.id}", icon_url=after.display_avatar.url)

                        await user_log_channel.send(embed=embed)
                        break

        # --- Boost start/stop logging (DB-backed dedup) ---
        started_boosting = before.premium_since is None and after.premium_since is not None
        stopped_boosting = before.premium_since is not None and after.premium_since is None

        if not (started_boosting or stopped_boosting):
            return
        if started_boosting:
            # INSERT OR IGNORE → rowcount == 0 means already tracked (double event)
            try:
                cursor = await self.bot.db.execute(
                    "INSERT OR IGNORE INTO guild_boosts "
                    "(guild_id, user_id, started_at) VALUES (?, ?, ?)",
                    (after.guild.id, after.id, int(after.premium_since.timestamp())),
                )
                await self.bot.db.commit()
                if cursor.rowcount == 0:
                    return  # Prevent double log
            except Exception as e:
                logger.error(f"[MemberEvents] Error tracking boost start: {e}")
                return
        else:  # stopped_boosting
            try:
                cursor = await self.bot.db.execute(
                    "DELETE FROM guild_boosts WHERE guild_id = ? AND user_id = ?",
                    (after.guild.id, after.id),
                )
                await self.bot.db.commit()
                if cursor.rowcount == 0:
                    return  # Prevent double log
            except Exception as e:
                logger.error(f"[MemberEvents] Error tracking boost stop: {e}")
                return

        server_log_channel = await get_log_channel(self.bot, after.guild.id, "server-log")
        if server_log_channel is None:
            return

        if started_boosting:
            embed = discord.Embed(
                title="<:newboost:1534195815671660685> New Server Boost",
                description=f"{after.mention} just boosted the server!",
                color=discord.Color.blue(),
                timestamp=discord.utils.utcnow(),
            )
        else:
            embed = discord.Embed(
                title="<:boostremove:1534198057036419214> Boost Removed",
                description=f"{after.mention} is no longer boosting the server.",
                color=discord.Color.red(),
                timestamp=discord.utils.utcnow(),
            )

        embed.set_thumbnail(url=after.display_avatar.url)
        embed.add_field(name="User", value=f"{after} (`{after.id}`)", inline=False)
        embed.set_footer(
            text=f"Total boosts: {after.guild.premium_subscription_count} • Level {after.guild.premium_tier}"
        )

        try:
            await server_log_channel.send(embed=embed)
        except discord.Forbidden:
            logger.info(f"Permission Denied: Cannot send logs to #{server_log_channel.name}")
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        """Track joins for the whois command."""
        ts = int(member.joined_at.timestamp()) if member.joined_at else int(discord.utils.utcnow().timestamp())
        try:
            await self.bot.db.execute(
                "INSERT OR IGNORE INTO join_history (user_id, guild_id, joined_at) VALUES (?, ?, ?)",
                (member.id, member.guild.id, ts)
            )
            await self.bot.db.commit()
        except Exception as e:
            logger.error(f"[MemberEvents] Error tracking join: {e}")
    @commands.Cog.listener()
    async def on_guild_role_update(self, before: discord.Role, after: discord.Role):
        if await is_feature_disabled(self.bot, after.guild.id, "role_change_log"):
            return
        if before.permissions == after.permissions:
            return

        granted, revoked = diff_permissions(before.permissions, after.permissions)
        if not granted and not revoked:
            return

        log_channel = await get_log_channel(self.bot, after.guild.id, "server-log")
        if log_channel is None:
            return
        await asyncio.sleep(1.5)
        now = discord.utils.utcnow()
        moderator = None
        try:
            async for entry in after.guild.audit_logs(action=discord.AuditLogAction.role_update, limit=5):
                if entry.target.id == after.id and (now - entry.created_at).total_seconds() < 15:
                    moderator = entry.user
                    break
        except discord.Forbidden:
            logger.info("Bot lacks 'View Audit Log' permission to check logs.")

        if granted and not revoked:
            color = discord.Color.green()
        elif revoked and not granted:
            color = discord.Color.red()
        elif revoked and granted:
            color = discord.Color.gold()
        else:
            color = discord.Color.blue()

        embed = discord.Embed(
            title="Role Permissions Changed",
            description=f"Permissions for {after.mention} were updated" + (f" by {moderator.mention}" if moderator else "") + ".",
            color=color,
            timestamp=now,
        )
        if granted:
            embed.add_field(name="Granted", value=", ".join(friendly_permission_name(p) for p in granted)[:1024], inline=False)
        if revoked:
            embed.add_field(name="Revoked", value=", ".join(friendly_permission_name(p) for p in revoked)[:1024], inline=False)
        embed.set_footer(text=f"Role ID: {after.id}")

        try:
            await log_channel.send(embed=embed)
        except discord.Forbidden:
            logger.info(f"Permission Denied: Cannot send logs to #{log_channel.name}")
    @commands.Cog.listener()
    async def on_guild_role_create(self, role: discord.Role):
        if await is_feature_disabled(self.bot, role.guild.id, "role_change_log"):
            return
        log_channel = await get_log_channel(self.bot, role.guild.id, "server-log")
        if log_channel is None:
            return
        await asyncio.sleep(1.5)
        now = discord.utils.utcnow()
        moderator = None
        try:
            async for entry in role.guild.audit_logs(action=discord.AuditLogAction.role_create, limit=5):
                if entry.target.id == role.id and (now - entry.created_at).total_seconds() < 15:
                    moderator = entry.user
                    break
        except discord.Forbidden:
            logger.info("Bot lacks 'View Audit Log' permission.")

        embed = discord.Embed(
            title="Role Created",
            description=f"New role {role.mention} was created"
                        + (f" by {moderator.mention}" if moderator else "") + ".",
            color=discord.Color.green(),
            timestamp=now,
        )
        embed.add_field(
            name="Permissions",
            value=", ".join(
                friendly_permission_name(p)
                for p, v in dict(role.permissions).items() if v
            )[:1024] or "*(no permissions)*",
            inline=False,
        )
        embed.set_footer(text=f"Role ID: {role.id}")
        try:
            await log_channel.send(embed=embed)
        except discord.Forbidden:
            logger.info(f"Permission Denied: Cannot send logs to #{log_channel.name}")
    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role):
        if await is_feature_disabled(self.bot, role.guild.id, "role_change_log"):
            return
        log_channel = await get_log_channel(self.bot, role.guild.id, "server-log")
        if log_channel is None:
            return
        await asyncio.sleep(1.5)
        now = discord.utils.utcnow()
        moderator = None
        try:
            async for entry in role.guild.audit_logs(action=discord.AuditLogAction.role_delete, limit=5):
                if entry.target.id == role.id and (now - entry.created_at).total_seconds() < 15:
                    moderator = entry.user
                    break
        except discord.Forbidden:
            logger.info("Bot lacks 'View Audit Log' permission to check logs.")

        embed = discord.Embed(
            title="Role Deleted",
            description=f"Role **@{role.name}** (`{role.id}`) was deleted"
                        + (f" by {moderator.mention}" if moderator else "")
                        + ".",
            color=discord.Color.red(),
            timestamp=now,
        )
        embed.add_field(name="Deleted Role Name", value=role.name, inline=True)
        embed.add_field(name="Color", value=str(role.color), inline=True)
        embed.add_field(
            name="Members Affected",
            value=str(len(role.members)),
            inline=True,
        )
        embed.set_footer(text=f"Role ID: {role.id}")
        try:
            await log_channel.send(embed=embed)
        except discord.Forbidden:
            logger.info(f"Permission Denied: Cannot send logs to #{log_channel.name}")
    @commands.Cog.listener()
    async def on_guild_channel_update(self, before: discord.abc.GuildChannel, after: discord.abc.GuildChannel):
        # 1. Handle Position/Category changes (Snapshot Buffer)
        if before.position != after.position or before.category_id != after.category_id:
            if not await is_feature_disabled(self.bot, after.guild.id, "channel_layout_screenshot"):
                self._trigger_snapshot_buffer(after.guild)

        # 2. Handle Permission Overwrite changes
        if before.overwrites == after.overwrites:
            return

        if await is_feature_disabled(self.bot, after.guild.id, "channel_permission_log"):
            return
        
        log_channel = await get_log_channel(self.bot, after.guild.id, "server-log")
        if log_channel is None:
            return
        await asyncio.sleep(1.5)
        now = discord.utils.utcnow()
        moderator = None
        audit_actions = (
            discord.AuditLogAction.overwrite_create,
            discord.AuditLogAction.overwrite_update,
            discord.AuditLogAction.overwrite_delete,
        )
        try:
            for action in audit_actions:
                async for entry in after.guild.audit_logs(action=action, limit=5):
                    if entry.target.id == after.id and (now - entry.created_at).total_seconds() < 15:
                        moderator = entry.user
                        break
                if moderator:
                    break
        except discord.Forbidden:
            logger.info("Bot lacks 'View Audit Log' permission to check logs.")

        changes = []
        any_granted = False
        any_revoked = False
        for target in set(before.overwrites) | set(after.overwrites):
            before_ow = before.overwrites.get(target)
            after_ow = after.overwrites.get(target)
            if before_ow == after_ow:
                continue

            before_pairs = dict(before_ow) if before_ow else {}
            after_pairs = dict(after_ow) if after_ow else {}
            if any(v and not before_pairs.get(p) for p, v in after_pairs.items()):
                any_granted = True
            if any(v and not after_pairs.get(p) for p, v in before_pairs.items()):
                any_revoked = True

            if before_ow is None:
                set_perms = [f"{friendly_permission_name(p)}: {describe_overwrite_value(v)}" for p, v in after_pairs.items() if v is not None]
                detail = ", ".join(set_perms) if set_perms else "no permissions set"
                changes.append(f"Added overwrite for {target.mention} ({detail})")
            elif after_ow is None:
                set_perms = [f"{friendly_permission_name(p)}: {describe_overwrite_value(v)}" for p, v in before_pairs.items() if v is not None]
                detail = ", ".join(set_perms) if set_perms else "no permissions were set"
                changes.append(f"Removed overwrite for {target.mention} (had {detail})")
            else:
                changed_perms = [
                    f"{friendly_permission_name(perm)} ({describe_overwrite_value(before_pairs[perm])} → {describe_overwrite_value(after_pairs[perm])})"
                    for perm in before_pairs if before_pairs[perm] != after_pairs[perm]
                ]
                changes.append(f"Updated overwrite for {target.mention}: {', '.join(changed_perms)}")

        if not changes:
            return
        if any_granted and not any_revoked:
            color = discord.Color.green()
        elif any_revoked and not any_granted:
            color = discord.Color.red()
        elif any_revoked and any_granted:
            color = discord.Color.gold()
        else:
            color = discord.Color.blue()

        embed = discord.Embed(
            title="Channel Permissions Changed",
            description=f"Permission overwrites for {after.mention} were updated" + (f" by {moderator.mention}" if moderator else "") + ".",
            color=color,
            timestamp=now,
        )
        embed.add_field(name="Changes", value="\n".join(changes)[:1024], inline=False)
        embed.set_footer(text=f"Channel ID: {after.id}")

        try:
            await log_channel.send(embed=embed)
        except discord.Forbidden:
            logger.info(f"Permission Denied: Cannot send logs to #{log_channel.name}")
    @commands.Cog.listener()
    async def on_guild_update(self, before: discord.Guild, after: discord.Guild):
        """
        Safety net: if boost count drifts away from what's tracked in the DB
        (e.g., a member event was missed), reconcile the DB and log properly.

        FIX for the "double log" issue: Discord fires on_member_update when
        premium_since becomes None AND on_guild_update when the actual count
        drops (after the 3-day grace period). These can fire in either order,
        which previously caused two "X is no longer boosting" messages.
        We now wait briefly for on_member_update to fire first; if it does,
        db_count == live_count and we skip. If it doesn't, we identify stale
        rows (members in DB but no longer premium_subscribers) and log them
        ourselves — and delete the rows so on_member_update (if it fires
        later) sees rowcount == 0 and doesn't re-log.
        """
        if before.premium_subscription_count == after.premium_subscription_count:
            return

        # Wait briefly for on_member_update to handle it (and delete the row).
        # 3s is plenty: when both fire on the same Day-3 grace-period expiry,
        # they fire within milliseconds of each other.
        await asyncio.sleep(3)

        async with self.bot.db.execute(
            "SELECT COUNT(*) FROM guild_boosts WHERE guild_id = ?",
            (after.id,),
        ) as cursor:
            db_count = (await cursor.fetchone())[0]

        if db_count == after.premium_subscription_count:
            # on_member_update already handled it — no drift, no log.
            return

        # Drift detected — try to identify stale/missing users.
        actual_ids = {m.id for m in after.premium_subscribers}
        async with self.bot.db.execute(
            "SELECT user_id FROM guild_boosts WHERE guild_id = ?",
            (after.id,),
        ) as cursor:
            db_ids = {row[0] for row in await cursor.fetchall()}

        log_channel = await get_log_channel(self.bot, after.id, "server-log")
        diff = after.premium_subscription_count - before.premium_subscription_count
        tier_changed = before.premium_tier != after.premium_tier

        if diff < 0:
            # Count dropped — find stale users (in DB but not actually boosting)
            stale_ids = db_ids - actual_ids
            if stale_ids:
                # Delete the stale rows so on_member_update (if it fires
                # later) sees rowcount == 0 and doesn't double-log.
                for uid in stale_ids:
                    await self.bot.db.execute(
                        "DELETE FROM guild_boosts WHERE guild_id = ? AND user_id = ?",
                        (after.id, uid),
                    )
                await self.bot.db.commit()

                # Also decrement any boost_list entries for these users
                try:
                    from cogs.boost_list import decrement_boost_count
                    for uid in stale_ids:
                        await decrement_boost_count(self.bot, after.id, uid)
                except ImportError:
                    pass

                if log_channel is not None:
                    for uid in stale_ids:
                        member = after.get_member(uid)
                        boost_embed = discord.Embed(
                            title="<:boostremove:1534198057036419214> Boost Removed",
                            description=(
                                f"{member.mention if member else f'`{uid}`'} is no "
                                f"longer boosting the server."
                            ),
                            color=discord.Color.red(),
                            timestamp=discord.utils.utcnow(),
                        )
                        if member:
                            boost_embed.set_thumbnail(url=member.display_avatar.url)
                            boost_embed.add_field(
                                name="User",
                                value=f"{member} (`{member.id}`)",
                                inline=False,
                            )
                        boost_embed.set_footer(
                            text=(
                                f"Total boosts: {after.premium_subscription_count} "
                                f"• Level {after.premium_tier}"
                            )
                        )
                        try:
                            await log_channel.send(embed=boost_embed)
                        except discord.Forbidden:
                            logger.info(
                                f"Permission Denied: Cannot send logs to "
                                f"#{log_channel.name}"
                            )
                return

        # Either increase drift (couldn't identify new boosters) or
        # decrease drift with no stale rows identified — fall back to the
        # generic count-changed message with the attribution button.
        await self._post_boost_drift_embed(
            after,
            before.premium_subscription_count,
            after.premium_subscription_count,
            db_count,
            tier_changed=tier_changed,
            before_tier=before.premium_tier,
        )

    async def _post_boost_drift_embed(
        self,
        guild: discord.Guild,
        before_count: int,
        after_count: int,
        db_count: int,
        tier_changed: bool = False,
        before_tier: int = 0,
    ) -> None:
        """Post the 'Boost Count Changed' drift embed with the attribution
        button. Used by on_guild_update and the simulate_boost_drift command."""
        log_channel = await get_log_channel(self.bot, guild.id, "server-log")
        if log_channel is None:
            return

        diff = after_count - before_count
        embed = discord.Embed(
            title="<:boost:1534195799892955176> Boost Count Changed",
            description=(
                f"Boost count went from **{before_count}** "
                f"to **{after_count}** "
                f"({'+' if diff > 0 else ''}{diff}).\n"
                f"⚠️ DB-tracked boosters ({db_count}) don't match the live "
                f"count — this will be reconciled on next sync."
            ),
            color=discord.Color.blurple(),
            timestamp=discord.utils.utcnow(),
        )
        if tier_changed:
            embed.add_field(
                name="Tier Changed",
                value=f"Level {before_tier} → Level {guild.premium_tier}",
                inline=False,
            )
        embed.set_footer(text=f"Level {guild.premium_tier}")
        try:
            await log_channel.send(embed=embed, view=BoostAttributionView())
        except discord.Forbidden:
            logger.info(f"Permission Denied: Cannot send logs to #{log_channel.name}")
async def setup(bot: commands.Bot):
    await bot.add_cog(MemberEvents(bot))