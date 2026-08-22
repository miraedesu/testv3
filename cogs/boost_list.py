"""Manually-tracked server boosters with deadline reminders.

Tracks sponsored nitro boosters: their boost count, since-when date, and an
annual deadline (next (month, day) anniversary of when they started boosting).
Two reminders ping the responsible admin 1 week and 3 days before each
booster's deadline, posted to the guild's "server-log" channel.

Auto-tracking: when a `on_member_update` boost event fires, the boost_count
of an existing entry is incremented/decremented automatically. If a member
starts boosting but isn't in the list yet, an attribution embed is posted
with an "I know who it was" button so the admin can add them with defaults.
If a decrement would drop a user's count to 0, the entry is auto-deleted.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

from common.settings_store import get_log_channel

logger = logging.getLogger(__name__)


# ── Deadline helpers ────────────────────────────────────────────────────

def calculate_next_deadline(boost_since_ts: int, now_ts: int | None = None) -> int:
    """Next annual deadline = next (month, day) anniversary of boost_since.

    If this year's anniversary has already passed (strict less-than, by date),
    returns next year's anniversary. Feb 29 falls back to Feb 28 in non-leap
    years.
    """
    if now_ts is None:
        now_ts = int(discord.utils.utcnow().timestamp())
    boost_since = datetime.fromtimestamp(boost_since_ts, tz=timezone.utc)
    now = datetime.fromtimestamp(now_ts, tz=timezone.utc)
    boost_since_date = boost_since.replace(hour=0, minute=0, second=0, microsecond=0)
    now_date = now.replace(hour=0, minute=0, second=0, microsecond=0)

    try:
        this_year = boost_since_date.replace(year=now_date.year)
    except ValueError:  # Feb 29 in non-leap year
        this_year = boost_since_date.replace(year=now_date.year, day=28)

    if this_year < now_date:
        try:
            return int(this_year.replace(year=now_date.year + 1).timestamp())
        except ValueError:
            return int(this_year.replace(year=now_date.year + 1, day=28).timestamp())
    return int(this_year.timestamp())


# ── Reminder dispatch (posts to the guild's server-log channel) ──────────

async def _post_reminder(
    bot, guild_id: int, admin_id: int, booster_id: int, deadline_ts: int,
    days_label: str,
) -> bool:
    """Post a deadline reminder to the guild's server-log channel.
    Returns True if posted, False if the channel is unset/unreachable."""
    log_channel = await get_log_channel(bot, guild_id, "server-log")
    if log_channel is None:
        logger.info(
            f"[BoostList] No server-log channel set for guild {guild_id}; "
            f"skipping {days_label} reminder for booster {booster_id}."
        )
        return False

    embed = discord.Embed(
        title=f"<:boost:1534195799892955176> Boost Deadline — {days_label}",
        description=(
            f"<@{admin_id}> — Booster <@{booster_id}> has their boost "
            f"deadline in **{days_label}** (<t:{deadline_ts}:F>)."
        ),
        color=discord.Color.gold(),
        timestamp=discord.utils.utcnow(),
    )
    embed.set_footer(text=f"Booster ID: {booster_id} • Admin ID: {admin_id}")
    try:
        await log_channel.send(
            content=f"<@{admin_id}>", embed=embed
        )
        return True
    except (discord.Forbidden, discord.HTTPException) as e:
        logger.warning(
            f"[BoostList] Failed to post {days_label} reminder to "
            f"server-log in guild {guild_id}: {e}"
        )
        return False


# ── Boost list mutation helpers (used by both this cog and member_events) ──

async def add_to_boost_list(
    bot, guild_id: int, user_id: int, admin_id: int,
    boost_since_ts: int | None = None, boost_count: int = 1,
    deadline_ts: int | None = None,
) -> int | None:
    """Add a user to boost_list. Returns entry ID, or None if already present."""
    if boost_since_ts is None:
        boost_since_ts = int(discord.utils.utcnow().timestamp())
    if deadline_ts is None:
        deadline_ts = calculate_next_deadline(boost_since_ts)
    if boost_count < 1:
        boost_count = 1

    try:
        cursor = await bot.db.execute(
            "INSERT INTO boost_list (guild_id, user_id, admin_id, boost_count, "
            "boost_since, deadline, week_notified, three_day_notified) "
            "VALUES (?, ?, ?, ?, ?, ?, 0, 0)",
            (guild_id, user_id, admin_id, boost_count, boost_since_ts, deadline_ts),
        )
        await bot.db.commit()
    except Exception:
        return None  # UNIQUE constraint — already in list
    return cursor.lastrowid


async def increment_boost_count(bot, guild_id: int, user_id: int) -> bool:
    """Increment boost_count for an existing entry. Returns True if found."""
    cursor = await bot.db.execute(
        "UPDATE boost_list SET boost_count = boost_count + 1 "
        "WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id),
    )
    await bot.db.commit()
    return cursor.rowcount > 0


async def decrement_boost_count(bot, guild_id: int, user_id: int) -> bool:
    """Decrement boost_count. If it would drop to 0, delete the entry.

    Returns True if an entry was found (and either decremented or removed)."""
    async with bot.db.execute(
        "SELECT id, boost_count FROM boost_list WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id),
    ) as cursor:
        row = await cursor.fetchone()

    if row is None:
        return False

    entry_id, count = row
    if count <= 1:
        await bot.db.execute("DELETE FROM boost_list WHERE id = ?", (entry_id,))
        await bot.db.commit()
    else:
        await bot.db.execute(
            "UPDATE boost_list SET boost_count = boost_count - 1 WHERE id = ?",
            (entry_id,),
        )
        await bot.db.commit()
    return True


# ── Persistent View: "I know who it was" (boost start attribution) ──────
# Used by cogs.member_events when a member boosts but isn't in the list.
# The target user_id is stored in the message embed's footer so the same
# persistent view (registered once at startup) works for every message.

class BoostListAddView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)  # Persistent

    @discord.ui.button(
        label="I know who it was",
        style=discord.ButtonStyle.secondary,
        custom_id="boost_list_add:click",
    )
    async def add_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message(
                "❌ Only server admins can do this.", ephemeral=True
            )
            return

        # Parse target user_id from the embed footer ("User ID: <id>")
        try:
            embed = interaction.message.embeds[0]
            footer_text = embed.footer.text or ""
            target_user_id = int(footer_text.split("User ID: ")[1])
        except (IndexError, ValueError):
            await interaction.response.send_message(
                "❌ Couldn't determine which user to add (embed footer missing).",
                ephemeral=True,
            )
            return

        target_member = interaction.guild.get_member(target_user_id)
        if target_member is None:
            await interaction.response.send_message(
                "❌ That user is no longer in this server — can't add to boost_list.",
                ephemeral=True,
            )
            return

        now_ts = int(discord.utils.utcnow().timestamp())
        boost_since = (
            int(target_member.premium_since.timestamp())
            if target_member.premium_since
            else now_ts
        )
        deadline = calculate_next_deadline(boost_since, now_ts)

        entry_id = await add_to_boost_list(
            interaction.client, interaction.guild.id, target_user_id,
            interaction.user.id, boost_since_ts=boost_since,
            boost_count=1, deadline_ts=deadline,
        )
        if entry_id is None:
            await interaction.response.send_message(
                f"❌ {target_member.mention} is already in the boost_list.",
                ephemeral=True,
            )
            return

        # Update the original embed to reflect the addition
        try:
            embed.color = discord.Color.green()
            embed.add_field(
                name="Added to boost_list",
                value=(
                    f"✅ {target_member.mention} added by {interaction.user.mention}\n"
                    f"Boosts: 1 • Deadline: <t:{deadline}:F>"
                ),
                inline=False,
            )
            await interaction.message.edit(embed=embed)
        except (discord.NotFound, discord.Forbidden):
            pass

        await interaction.response.send_message(
            f"✅ Added {target_member.mention} to the boost_list "
            f"(1 boost, deadline <t:{deadline}:F>).",
            ephemeral=True,
        )


# ── Pagination for /boost_list list ─────────────────────────────────────

class BoostListPagination(discord.ui.View):
    def __init__(self, entries, guild, author):
        super().__init__(timeout=120)
        self.entries = entries  # list of (id, user_id, count, since_ts, deadline_ts)
        self.guild = guild
        self.author = author
        self.page = 0
        self.per_page = 5
        self.max_page = max((len(entries) - 1) // self.per_page, 0)
        self.message: discord.Message | None = None

    def create_embed(self) -> discord.Embed:
        start = self.page * self.per_page
        end = start + self.per_page
        chunk = self.entries[start:end]

        embed = discord.Embed(
            title="<:boost:1534195799892955176> Boost List",
            color=discord.Color.pink(),
            timestamp=discord.utils.utcnow(),
        )

        if not chunk:
            embed.description = "No entries on this page."
        else:
            lines = []
            for entry_id, user_id, count, since_ts, deadline_ts in chunk:
                member = self.guild.get_member(user_id)
                user_str = member.mention if member else f"`{user_id}`"
                lines.append(
                    f"**#{entry_id}** • {user_str}\n"
                    f"Boosts: **{count}** • Since: <t:{since_ts}:D>\n"
                    f"Deadline: <t:{deadline_ts}:F> (<t:{deadline_ts}:R>)"
                )
            embed.description = "\n\n".join(lines)

        embed.set_footer(
            text=f"Page {self.page + 1}/{self.max_page + 1} • Total: {len(self.entries)}"
        )
        self.update_buttons()
        return embed

    def update_buttons(self):
        self.prev.disabled = self.page == 0
        self.next.disabled = self.page == self.max_page

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user != self.author:
            await interaction.response.send_message(
                "This menu isn't for you.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="⬅", style=discord.ButtonStyle.gray)
    async def prev(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = max(self.page - 1, 0)
        await interaction.response.edit_message(embed=self.create_embed(), view=self)

    @discord.ui.button(label="➡", style=discord.ButtonStyle.gray)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = min(self.page + 1, self.max_page)
        await interaction.response.edit_message(embed=self.create_embed(), view=self)


# ── Cog ──────────────────────────────────────────────────────────────────

class BoostList(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.refresh_deadlines.start()
        self.fire_reminders.start()

    async def cog_load(self) -> None:
        # Register the persistent view so the "I know who it was" button
        # keeps working after a restart.
        self.bot.add_view(BoostListAddView())

    def cog_unload(self):
        self.refresh_deadlines.cancel()
        self.fire_reminders.cancel()

    @tasks.loop(minutes=1)
    async def fire_reminders(self):
        """Every minute, check boost_list entries for upcoming deadlines.

        - 1-week reminder fires when (deadline - now) <= 7 days and not yet sent.
        - 3-day reminder fires when (deadline - now) <= 3 days and not yet sent.
        - Both post to the guild's server-log channel and ping the admin.

        Each reminder is marked as sent via the week_notified / three_day_notified
        flags so it fires exactly once per annual cycle.
        """
        now_ts = int(discord.utils.utcnow().timestamp())
        week_cutoff = now_ts + 7 * 86400
        three_day_cutoff = now_ts + 3 * 86400

        # 1-week reminders
        async with self.bot.db.execute(
            "SELECT id, guild_id, user_id, admin_id, deadline "
            "FROM boost_list "
            "WHERE deadline <= ? AND deadline > ? AND week_notified = 0",
            (week_cutoff, now_ts),
        ) as cursor:
            due_week = await cursor.fetchall()

        for entry_id, guild_id, user_id, admin_id, deadline_ts in due_week:
            posted = await _post_reminder(
                self.bot, guild_id, admin_id, user_id, deadline_ts, "1 week"
            )
            if posted:
                await self.bot.db.execute(
                    "UPDATE boost_list SET week_notified = 1 WHERE id = ?",
                    (entry_id,),
                )
                await self.bot.db.commit()

        # 3-day reminders
        async with self.bot.db.execute(
            "SELECT id, guild_id, user_id, admin_id, deadline "
            "FROM boost_list "
            "WHERE deadline <= ? AND deadline > ? AND three_day_notified = 0",
            (three_day_cutoff, now_ts),
        ) as cursor:
            due_3day = await cursor.fetchall()

        for entry_id, guild_id, user_id, admin_id, deadline_ts in due_3day:
            posted = await _post_reminder(
                self.bot, guild_id, admin_id, user_id, deadline_ts, "3 days"
            )
            if posted:
                await self.bot.db.execute(
                    "UPDATE boost_list SET three_day_notified = 1 WHERE id = ?",
                    (entry_id,),
                )
                await self.bot.db.commit()

    @tasks.loop(hours=1)
    async def refresh_deadlines(self):
        """For entries whose deadline has passed, advance to next year and
        reset the notification flags so reminders fire again next cycle."""
        now_ts = int(discord.utils.utcnow().timestamp())
        async with self.bot.db.execute(
            "SELECT id, guild_id, user_id, admin_id, boost_since "
            "FROM boost_list WHERE deadline < ?",
            (now_ts,),
        ) as cursor:
            expired = await cursor.fetchall()

        for entry_id, guild_id, user_id, admin_id, boost_since in expired:
            new_deadline = calculate_next_deadline(boost_since, now_ts)
            await self.bot.db.execute(
                "UPDATE boost_list SET deadline = ?, "
                "week_notified = 0, three_day_notified = 0 WHERE id = ?",
                (new_deadline, entry_id),
            )
        if expired:
            await self.bot.db.commit()

    @fire_reminders.before_loop
    @refresh_deadlines.before_loop
    async def _wait_ready(self):
        await self.bot.wait_until_ready()

    # ── Group ──────────────────────────────────────────────────────────
    boost_list = app_commands.Group(
        name="boost_list",
        description="Track manually-reviewed server boosters and their deadlines",
        default_permissions=discord.Permissions(administrator=True),
    )

    async def _entry_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        if not interaction.guild:
            return []
        async with self.bot.db.execute(
            "SELECT id, user_id, boost_count, deadline FROM boost_list "
            "WHERE guild_id = ? ORDER BY deadline ASC",
            (interaction.guild.id,),
        ) as cursor:
            rows = await cursor.fetchall()

        cl = current.lower()
        results = []
        for entry_id, user_id, count, deadline_ts in rows:
            member = interaction.guild.get_member(user_id)
            label_name = member.display_name if member else f"User {user_id}"
            label = f"#{entry_id} — {label_name} ({count} boost, <t:{deadline_ts}:R>)"
            if cl in label.lower() or cl in str(user_id) or cl in str(entry_id):
                results.append(app_commands.Choice(name=label[:100], value=str(entry_id)))
            if len(results) >= 25:
                break
        return results

    async def _booster_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        if not interaction.guild:
            return []
        async with self.bot.db.execute(
            "SELECT user_id FROM boost_list WHERE guild_id = ?",
            (interaction.guild.id,),
        ) as cursor:
            existing_ids = {row[0] for row in await cursor.fetchall()}

        cl = current.lower()
        results = []
        for m in interaction.guild.premium_subscribers:
            if m.id in existing_ids:
                continue
            label = f"{m.display_name} ({m.id})"
            if cl in label.lower() or cl in str(m.id):
                results.append(app_commands.Choice(name=label[:100], value=str(m.id)))
            if len(results) >= 25:
                break
        return results

    @boost_list.command(name="list", description="Show all tracked boosters and their deadlines")
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.guild_only()
    async def list_cmd(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        async with self.bot.db.execute(
            "SELECT id, user_id, boost_count, boost_since, deadline "
            "FROM boost_list WHERE guild_id = ? ORDER BY deadline ASC",
            (interaction.guild.id,),
        ) as cursor:
            entries = await cursor.fetchall()

        if not entries:
            await interaction.followup.send(
                "📋 The boost list is empty. Use `/boost_list add` to start tracking.",
                ephemeral=True,
            )
            return

        view = BoostListPagination(entries, interaction.guild, interaction.user)
        embed = view.create_embed()
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        view.message = await interaction.original_response()

    @boost_list.command(name="add", description="Add a currently-boosting user to the boost list")
    @app_commands.describe(
        user="Pick a current booster (autocomplete shows non-tracked boosters)",
        boost_count="Number of boosts to record (default 1, min 1)",
    )
    @app_commands.autocomplete(user=_booster_autocomplete)
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.guild_only()
    async def add_cmd(
        self, interaction: discord.Interaction, user: str, boost_count: int = 1
    ):
        try:
            user_id = int(user)
        except ValueError:
            await interaction.response.send_message("❌ Invalid user.", ephemeral=True)
            return

        if boost_count < 1:
            await interaction.response.send_message(
                "❌ Boost count must be at least 1.", ephemeral=True
            )
            return

        member = interaction.guild.get_member(user_id)
        if member is None:
            await interaction.response.send_message(
                "❌ That user isn't in this server.", ephemeral=True
            )
            return

        now_ts = int(discord.utils.utcnow().timestamp())
        boost_since = (
            int(member.premium_since.timestamp())
            if member.premium_since
            else now_ts
        )
        deadline = calculate_next_deadline(boost_since, now_ts)

        entry_id = await add_to_boost_list(
            self.bot, interaction.guild.id, user_id, interaction.user.id,
            boost_since_ts=boost_since, boost_count=boost_count, deadline_ts=deadline,
        )
        if entry_id is None:
            await interaction.response.send_message(
                f"❌ {member.mention} is already in the boost_list. "
                f"Use `/boost_list edit` to change their boost count.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            f"✅ Added {member.mention} to the boost_list:\n"
            f"• Boosts: **{boost_count}**\n"
            f"• Since: <t:{boost_since}:D>\n"
            f"• Deadline: <t:{deadline}:F> (<t:{deadline}:R>)\n"
            f"• You'll be pinged in the server-log channel "
            f"1 week and 3 days before the deadline.",
            ephemeral=True,
        )

    @boost_list.command(name="edit", description="Edit an existing boost list entry")
    @app_commands.describe(
        entry_id="Entry ID from /boost_list list",
        boost_count="New boost count (min 1; leave empty to keep current)",
        deadline="New deadline (e.g. '2027-05-05' or 'May 5 2027'); leave empty to keep",
    )
    @app_commands.autocomplete(entry_id=_entry_autocomplete)
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.guild_only()
    async def edit_cmd(
        self, interaction: discord.Interaction, entry_id: int,
        boost_count: int | None = None, deadline: str | None = None,
    ):
        if boost_count is None and (deadline is None or not deadline.strip()):
            await interaction.response.send_message(
                "❌ Provide at least one of `boost_count` or `deadline` to edit.",
                ephemeral=True,
            )
            return

        if boost_count is not None and boost_count < 1:
            await interaction.response.send_message(
                "❌ Boost count must be at least 1.", ephemeral=True
            )
            return

        async with self.bot.db.execute(
            "SELECT id, guild_id, user_id, boost_count, boost_since, deadline "
            "FROM boost_list WHERE id = ?",
            (entry_id,),
        ) as cursor:
            row = await cursor.fetchone()

        if row is None or row[1] != interaction.guild.id:
            await interaction.response.send_message(
                "❌ Entry not found in this server.", ephemeral=True
            )
            return

        (
            _id, guild_id, user_id, current_count,
            boost_since, current_deadline,
        ) = row

        # Update boost_count
        if boost_count is not None:
            await self.bot.db.execute(
                "UPDATE boost_list SET boost_count = ? WHERE id = ?",
                (boost_count, entry_id),
            )
            await self.bot.db.commit()

        # Update deadline + reset notification flags so reminders re-fire
        new_deadline_ts = current_deadline
        deadline_changed = False
        if deadline is not None and deadline.strip():
            import dateparser
            parsed = dateparser.parse(
                deadline,
                settings={"PREFER_DATES_FROM": "future", "RETURN_AS_TIMEZONE_AWARE": True},
            )
            if parsed is None:
                await interaction.response.send_message(
                    f"❌ Couldn't understand the deadline `{deadline}`. "
                    f"Try a format like `2027-05-05` or `May 5 2027`.",
                    ephemeral=True,
                )
                return
            new_deadline_ts = int(parsed.timestamp())
            await self.bot.db.execute(
                "UPDATE boost_list SET deadline = ?, "
                "week_notified = 0, three_day_notified = 0 WHERE id = ?",
                (new_deadline_ts, entry_id),
            )
            await self.bot.db.commit()
            deadline_changed = True

        member = interaction.guild.get_member(user_id)
        user_str = member.mention if member else f"`{user_id}`"
        final_count = boost_count if boost_count is not None else current_count
        await interaction.response.send_message(
            f"✅ Updated entry #{entry_id} ({user_str}):\n"
            f"• Boosts: **{final_count}**\n"
            f"• Deadline: <t:{new_deadline_ts}:F> (<t:{new_deadline_ts}:R>)"
            + ("\n• Reminder flags reset (reminders will re-fire)." if deadline_changed else ""),
            ephemeral=True,
        )

    @boost_list.command(name="delete", description="Remove a user from the boost list")
    @app_commands.describe(entry_id="Entry ID from /boost_list list")
    @app_commands.autocomplete(entry_id=_entry_autocomplete)
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.guild_only()
    async def delete_cmd(self, interaction: discord.Interaction, entry_id: int):
        async with self.bot.db.execute(
            "SELECT guild_id, user_id FROM boost_list WHERE id = ?",
            (entry_id,),
        ) as cursor:
            row = await cursor.fetchone()

        if row is None or row[0] != interaction.guild.id:
            await interaction.response.send_message(
                "❌ Entry not found in this server.", ephemeral=True
            )
            return

        guild_id, user_id = row
        await self.bot.db.execute("DELETE FROM boost_list WHERE id = ?", (entry_id,))
        await self.bot.db.commit()

        member = interaction.guild.get_member(user_id)
        user_str = member.mention if member else f"`{user_id}`"
        await interaction.response.send_message(
            f"✅ Removed {user_str} from the boost list (entry #{entry_id}).",
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(BoostList(bot))