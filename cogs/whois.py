"""WhoIs command group: 4-button view, paginated history lists, and admin note management."""
from __future__ import annotations

import re
import discord
from discord import app_commands
from discord.ext import commands

MENTION_RE = re.compile(r"<@!?(\d+)>")
ID_RE = re.compile(r"(\d{17,20})")


# def clean_username(name: str) -> str:
#     """Remove old Discord discriminators like #0 or #0000 from usernames."""
#     if not name:
#         return "Unknown"
#     return re.sub(r'#\d{1,4}$', '', name)
def clean_username(name: str) -> str:
    """Preserves old discriminators (#1234) but removes the new system's #0."""
    if not name:
        return "Unknown"
    # This removes '#0' specifically, but leaves '#1234' or '#0000' intact
    return re.sub(r'#0$', '', name)


async def fetch_user_data(bot: commands.Bot, user_id: int) -> dict:
    """Fetch user data safely, handling deleted users."""
    try:
        user_obj = await bot.fetch_user(user_id)
        return {
            "uid": user_id,
            "username": clean_username(user_obj.name),
            "avatar_url": user_obj.display_avatar.url,
            "created_at": user_obj.created_at,
            "is_bot": user_obj.bot,
        }
    except (discord.NotFound, discord.HTTPException):
        return {
            "uid": user_id,
            "username": "Unknown / Deleted User",
            "avatar_url": None,
            "created_at": None,
            "is_bot": False,
        }


def extract_user_id(user_id: str) -> int | None:
    m = MENTION_RE.search(user_id) or ID_RE.search(user_id)
    if not m:
        return None
    return int(m.group(1))


# ============================================================
#  PAGINATION VIEW FOR LISTS
# ============================================================
class ListPagination(discord.ui.View):
    def __init__(self, title: str, user_data: dict, entries: list[str], author: discord.abc.User):
        super().__init__(timeout=120)
        self.title = title
        self.user_data = user_data
        self.entries = entries
        self.author = author
        self.page = 0
        self.per_page = 10
        self.max_page = max((len(entries) - 1) // self.per_page, 0)
        self.message: discord.Message | None = None

    def create_embed(self) -> discord.Embed:
        start = self.page * self.per_page
        end = start + self.per_page
        page_entries = self.entries[start:end]

        username = self.user_data["username"]
        avatar_url = self.user_data["avatar_url"]
        uid = self.user_data["uid"]
        created_at = self.user_data["created_at"]

        embed = discord.Embed(title=self.title, color=discord.Color.blue())

        if avatar_url:
            embed.set_author(name=username, icon_url=avatar_url)
            embed.set_thumbnail(url=avatar_url)
        else:
            embed.set_author(name=username)

        embed.add_field(name="User ID", value=f"`{uid}`", inline=True)
        if created_at:
            embed.add_field(name="Account Created", value=f"<t:{int(created_at.timestamp())}:D>", inline=True)

        if not page_entries:
            embed.description = "No entries found."
        else:
            embed.description = "\n\n".join(page_entries)

        embed.set_footer(text=f"Page {self.page + 1}/{self.max_page + 1} • Total: {len(self.entries)} • Requested by {self.author}")
        self.update_buttons()
        return embed

    def update_buttons(self):
        self.first.disabled = self.page == 0
        self.prev.disabled = self.page == 0
        self.next.disabled = self.page == self.max_page
        self.last.disabled = self.page == self.max_page

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user != self.author:
            await interaction.response.send_message("This menu isn't for you.", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    @discord.ui.button(label="⏮", style=discord.ButtonStyle.gray)
    async def first(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = 0
        await interaction.response.edit_message(embed=self.create_embed(), view=self)

    @discord.ui.button(label="⬅", style=discord.ButtonStyle.gray)
    async def prev(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = max(self.page - 1, 0)
        await interaction.response.edit_message(embed=self.create_embed(), view=self)

    @discord.ui.button(label="➡", style=discord.ButtonStyle.gray)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = min(self.page + 1, self.max_page)
        await interaction.response.edit_message(embed=self.create_embed(), view=self)

    @discord.ui.button(label="⏭", style=discord.ButtonStyle.gray)
    async def last(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = self.max_page
        await interaction.response.edit_message(embed=self.create_embed(), view=self)

    @discord.ui.button(label="⏹", style=discord.ButtonStyle.red)
    async def stop_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)


# ============================================================
#  4-BUTTON VIEW
# ============================================================
class WhoIsView(discord.ui.View):
    """4-button view: Who Is / Joins / Notes / Punish."""

    def __init__(self, user_data: dict, author: discord.abc.User, bot: commands.Bot, guild_id: int):
        super().__init__(timeout=120)
        self.user_data = user_data
        self.author = author
        self.bot = bot
        self.guild_id = guild_id
        self.page = 0
        self.message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user != self.author:
            await interaction.response.send_message("This menu isn't for you.", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    def build_base_embed(self) -> discord.Embed:
        """Build the base embed with common fields shared by all 4 pages."""
        uid = self.user_data["uid"]
        username = self.user_data["username"]
        avatar_url = self.user_data["avatar_url"]
        created_at = self.user_data["created_at"]
        is_bot = self.user_data["is_bot"]

        embed = discord.Embed(color=discord.Color.blue())

        if avatar_url:
            embed.set_author(name=username, icon_url=avatar_url)
            embed.set_thumbnail(url=avatar_url)
        else:
            embed.set_author(name=username)

        embed.add_field(name="User ID", value=f"`{uid}`", inline=True)

        if created_at:
            embed.add_field(name="Account Created", value=f"<t:{int(created_at.timestamp())}:D>", inline=True)

        if is_bot:
            embed.add_field(name="Type", value="🤖 Bot", inline=True)

        return embed

    async def build_whois_embed(self) -> discord.Embed:
        embed = self.build_base_embed()
        embed.title = "Who Is"
        uid = self.user_data["uid"]

        async with self.bot.db.execute(
            "SELECT username, timestamp FROM username_history WHERE user_id = ? ORDER BY timestamp DESC LIMIT 15",
            (uid,)
        ) as cursor:
            name_history = await cursor.fetchall()

        async with self.bot.db.execute(
            "SELECT aka_name FROM aka_names WHERE user_id = ? AND guild_id = ? ORDER BY timestamp DESC",
            (uid, self.guild_id)
        ) as cursor:
            aka_names = await cursor.fetchall()

        if name_history:
            names = []
            for name, ts in name_history:
                clean_name = clean_username(name)
                names.append(f"• {clean_name} — <t:{ts}:D>")
            text = "\n".join(names)
            if len(text) > 1024:
                text = text[:1021] + "..."
            embed.add_field(name=f"Previous Names ({len(names)})", value=text, inline=False)
        else:
            embed.add_field(name="Previous Names", value="No history found", inline=False)

        if aka_names:
            aka_list = [clean_username(name[0]) for name in aka_names]
            aka_text = ", ".join(aka_list)
            if len(aka_text) > 1024:
                aka_text = aka_text[:1021] + "..."
            embed.add_field(name=f"AKA ({len(aka_list)})", value=aka_text, inline=False)
        else:
            embed.add_field(name="AKA", value="None", inline=False)

        embed.set_footer(text=f"Requested by {self.author}")
        return embed

    async def build_joins_embed(self) -> discord.Embed:
        embed = self.build_base_embed()
        embed.title = "Join History"
        uid = self.user_data["uid"]

        async with self.bot.db.execute(
            "SELECT joined_at FROM join_history WHERE user_id = ? AND guild_id = ? ORDER BY joined_at DESC LIMIT 15",
            (uid, self.guild_id)
        ) as cursor:
            join_history = await cursor.fetchall()

        if join_history:
            joins = "\n".join(f"• <t:{ts}:F>" for ts, in join_history)
            if len(joins) > 1024:
                joins = joins[:1021] + "..."
            embed.add_field(name=f"Join History ({len(join_history)})", value=joins, inline=False)
        else:
            embed.add_field(name="Join History", value="No previous joins recorded", inline=False)

        embed.set_footer(text=f"Requested by {self.author}")
        return embed

    async def build_notes_embed(self) -> discord.Embed:
        embed = self.build_base_embed()
        embed.title = "Staff Notes"
        uid = self.user_data["uid"]

        async with self.bot.db.execute(
            "SELECT note_text FROM staff_notes WHERE user_id = ? AND guild_id = ? ORDER BY timestamp DESC",
            (uid, self.guild_id)
        ) as cursor:
            notes = await cursor.fetchall()

        if notes:
            notes_text = "\n".join(f"• {note[0]}" for note in notes)
            if len(notes_text) > 1024:
                notes_text = notes_text[:1021] + "..."
            embed.add_field(name=f"Notes ({len(notes)})", value=notes_text, inline=False)
        else:
            embed.add_field(name="Notes", value="No notes recorded", inline=False)

        embed.set_footer(text=f"Requested by {self.author}")
        return embed

    async def build_punish_embed(self) -> discord.Embed:
        embed = self.build_base_embed()
        embed.title = "Punishment History"
        uid = self.user_data["uid"]

        async with self.bot.db.execute(
            "SELECT action, reason, timestamp FROM punishment_history WHERE user_id = ? AND guild_id = ? ORDER BY timestamp DESC LIMIT 10",
            (uid, self.guild_id)
        ) as cursor:
            punishments = await cursor.fetchall()

        if punishments:
            punish_list = []
            for action, reason, ts in punishments:
                reason_text = reason or "No reason provided"
                punish_list.append(f"• **{action.title()}** — <t:{ts}:D>\n   Reason: {reason_text}")
            punish_text = "\n".join(punish_list)
            if len(punish_text) > 1024:
                punish_text = punish_text[:1021] + "..."
            embed.add_field(name=f"Recent Punishments ({len(punishments)})", value=punish_text, inline=False)
        else:
            embed.add_field(name="Recent Punishments", value="No punishments recorded", inline=False)

        embed.set_footer(text=f"Requested by {self.author}")
        return embed

    async def current_embed(self) -> discord.Embed:
        if self.page == 0:
            return await self.build_whois_embed()
        elif self.page == 1:
            return await self.build_joins_embed()
        elif self.page == 2:
            return await self.build_notes_embed()
        else:
            return await self.build_punish_embed()

    @discord.ui.button(label="Who Is", emoji="<:search:1534195860123156582>", style=discord.ButtonStyle.secondary)
    async def whois_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = 0
        embed = await self.current_embed()
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Joins", emoji="<:RiePing:1533761273798328460>", style=discord.ButtonStyle.secondary)
    async def joins_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = 1
        embed = await self.current_embed()
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Notes", emoji="<:note:1534195886501134376>", style=discord.ButtonStyle.secondary)
    async def notes_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = 2
        embed = await self.current_embed()
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Punish", emoji="<:RieThumbsUp:1533761254513184818>", style=discord.ButtonStyle.secondary)
    async def punish_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = 3
        embed = await self.current_embed()
        await interaction.response.edit_message(embed=embed, view=self)


# ============================================================
#  COG
# ============================================================
class WhoIs(commands.Cog):
    whois = app_commands.Group(
        name="whois", 
        description="Look up user history (names, joins, notes, punishments).",
        default_permissions=discord.Permissions(administrator=True)
    )
    whois_add = app_commands.Group(
        name="whois_add", 
        description="Add AKA names or staff notes to a user.",
        default_permissions=discord.Permissions(administrator=True)
    )
    whois_list = app_commands.Group(
        name="whois_list", 
        description="List notes you've added to a user.",
        default_permissions=discord.Permissions(administrator=True)
    )
    whois_del = app_commands.Group(
        name="whois_del", 
        description="Delete a staff note by ID.",
        default_permissions=discord.Permissions(administrator=True)
    )
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @whois.command(name="view", description="View a user's overview (4 buttons: Who Is, Joins, Notes, Punish).")
    @app_commands.describe(user_id="User ID or @mention to look up")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.guild_only()
    async def whois_view(self, interaction: discord.Interaction, user_id: str):
        await interaction.response.defer(ephemeral=True)

        uid = extract_user_id(user_id)
        if not uid:
            await interaction.followup.send("❌ Please provide a valid user ID or @mention.", ephemeral=True)
            return

        user_data = await fetch_user_data(self.bot, uid)
        view = WhoIsView(user_data, interaction.user, self.bot, interaction.guild_id)
        embed = await view.current_embed()
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        view.message = await interaction.original_response()

    @whois.command(name="names", description="View all previous names for a user (paginated).")
    @app_commands.describe(user_id="User ID or @mention to look up")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.guild_only()
    async def whois_names(self, interaction: discord.Interaction, user_id: str):
        await interaction.response.defer(ephemeral=True)

        uid = extract_user_id(user_id)
        if not uid:
            await interaction.followup.send("❌ Please provide a valid user ID or @mention.", ephemeral=True)
            return

        user_data = await fetch_user_data(self.bot, uid)

        async with self.bot.db.execute(
            "SELECT username, timestamp FROM username_history WHERE user_id = ? ORDER BY timestamp ASC",
            (uid,)
        ) as cursor:
            name_history = await cursor.fetchall()

        entries = []
        for name, ts in name_history:
            clean_name = clean_username(name)
            entries.append(f"• {clean_name} — <t:{ts}:D>")

        if not entries:
            await interaction.followup.send("No name history found for this user.", ephemeral=True)
            return

        view = ListPagination("<:note:1534195886501134376> Name History", user_data, entries, interaction.user)
        embed = view.create_embed()
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        view.message = await interaction.original_response()

    @whois.command(name="joins", description="View all join history for a user (paginated).")
    @app_commands.describe(user_id="User ID or @mention to look up")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.guild_only()
    async def whois_joins(self, interaction: discord.Interaction, user_id: str):
        await interaction.response.defer(ephemeral=True)

        uid = extract_user_id(user_id)
        if not uid:
            await interaction.followup.send("❌ Please provide a valid user ID or @mention.", ephemeral=True)
            return

        user_data = await fetch_user_data(self.bot, uid)

        async with self.bot.db.execute(
            "SELECT joined_at FROM join_history WHERE user_id = ? AND guild_id = ? ORDER BY joined_at ASC",
            (uid, interaction.guild_id)
        ) as cursor:
            join_history = await cursor.fetchall()

        entries = [f"• <t:{ts}:F>" for ts, in join_history]

        if not entries:
            await interaction.followup.send("No join history found for this user in this server.", ephemeral=True)
            return

        view = ListPagination("<:RiePing:1533761273798328460> Join History", user_data, entries, interaction.user)
        embed = view.create_embed()
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        view.message = await interaction.original_response()

    @whois.command(name="notes", description="View all staff notes for a user (paginated).")
    @app_commands.describe(user_id="User ID or @mention to look up")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.guild_only()
    async def whois_notes(self, interaction: discord.Interaction, user_id: str):
        await interaction.response.defer(ephemeral=True)

        uid = extract_user_id(user_id)
        if not uid:
            await interaction.followup.send("❌ Please provide a valid user ID or @mention.", ephemeral=True)
            return

        user_data = await fetch_user_data(self.bot, uid)

        async with self.bot.db.execute(
            "SELECT note_text, author_id, id FROM staff_notes WHERE user_id = ? AND guild_id = ? ORDER BY timestamp ASC",
            (uid, interaction.guild_id)
        ) as cursor:
            notes = await cursor.fetchall()

        entries = []
        for note_text, author_id, note_id in notes:
            try:
                mod = await self.bot.fetch_user(author_id)
                mod_name = clean_username(mod.name)
            except (discord.NotFound, discord.HTTPException):
                mod_name = "Unknown Mod"
            entries.append(f"**{mod_name}** (`#{note_id}`):\n{note_text}")

        if not entries:
            await interaction.followup.send("No notes found for this user.", ephemeral=True)
            return

        view = ListPagination("<:note:1534195886501134376> Staff Notes", user_data, entries, interaction.user)
        embed = view.create_embed()
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        view.message = await interaction.original_response()

    @whois.command(name="punish", description="View full punishment history for a user (paginated).")
    @app_commands.describe(user_id="User ID or @mention to look up")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.guild_only()
    async def whois_punish(self, interaction: discord.Interaction, user_id: str):
        await interaction.response.defer(ephemeral=True)

        uid = extract_user_id(user_id)
        if not uid:
            await interaction.followup.send("❌ Please provide a valid user ID or @mention.", ephemeral=True)
            return

        user_data = await fetch_user_data(self.bot, uid)

        async with self.bot.db.execute(
            "SELECT action, reason, timestamp FROM punishment_history WHERE user_id = ? AND guild_id = ? ORDER BY timestamp ASC",
            (uid, interaction.guild_id)
        ) as cursor:
            punishments = await cursor.fetchall()

        entries = []
        for action, reason, ts in punishments:
            reason_text = reason or "No reason provided"
            entries.append(f"• **{action.title()}** — <t:{ts}:F>\n   Reason: {reason_text}")

        if not entries:
            await interaction.followup.send("No punishment history found for this user.", ephemeral=True)
            return

        view = ListPagination("<:RieThumbsUp:1533761254513184818> Punishment History", user_data, entries, interaction.user)
        embed = view.create_embed()
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        view.message = await interaction.original_response()

    # ============================================================
    #  WHOIS ADD / LIST / DEL COMMANDS
    # ============================================================

    @whois_add.command(name="name", description="Add an AKA (alias) name to a user.")
    @app_commands.describe(user="The target user", name="The AKA name to add")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.guild_only()
    async def whois_add_name(self, interaction: discord.Interaction, user: discord.User, name: str):
        name_clean = name.strip()
        if not name_clean:
            await interaction.response.send_message("Name cannot be empty.", ephemeral=True)
            return
        
        async with self.bot.db.execute(
            "SELECT 1 FROM aka_names WHERE user_id = ? AND guild_id = ? AND aka_name = ?",
            (user.id, interaction.guild_id, name_clean)
        ) as cursor:
            if await cursor.fetchone():
                await interaction.response.send_message(f"❌ AKA `{name_clean}` already exists for this user.", ephemeral=True)
                return

        ts = int(discord.utils.utcnow().timestamp())
        await self.bot.db.execute(
            "INSERT INTO aka_names (user_id, guild_id, aka_name, added_by, timestamp) VALUES (?, ?, ?, ?, ?)",
            (user.id, interaction.guild_id, name_clean, interaction.user.id, ts)
        )
        await self.bot.db.commit()
        
        await interaction.response.send_message(f"✅ Added AKA `{name_clean}` for {user.mention}.", ephemeral=True)

    @whois_add.command(name="notes", description="Add a staff note to a user.")
    @app_commands.describe(user="The target user", note="The note text to add")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.guild_only()
    async def whois_add_notes(self, interaction: discord.Interaction, user: discord.User, note: str):
        note_clean = note.strip()
        if not note_clean:
            await interaction.response.send_message("Note cannot be empty.", ephemeral=True)
            return
        
        ts = int(discord.utils.utcnow().timestamp())
        cursor = await self.bot.db.execute(
            "INSERT INTO staff_notes (user_id, guild_id, author_id, note_text, timestamp) VALUES (?, ?, ?, ?, ?)",
            (user.id, interaction.guild_id, interaction.user.id, note_clean, ts)
        )
        await self.bot.db.commit()
        note_id = cursor.lastrowid
        
        await interaction.response.send_message(f"✅ Added note `#{note_id}` for {user.mention}.", ephemeral=True)

    @whois_list.command(name="notes", description="List all notes you've added to a user.")
    @app_commands.describe(user="The target user")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.guild_only()
    async def whois_list_notes(self, interaction: discord.Interaction, user: discord.User):
        async with self.bot.db.execute(
            "SELECT id, note_text, timestamp FROM staff_notes WHERE user_id = ? AND guild_id = ? AND author_id = ? ORDER BY timestamp DESC",
            (user.id, interaction.guild_id, interaction.user.id)
        ) as cursor:
            notes = await cursor.fetchall()
            
        if not notes:
            await interaction.response.send_message("You haven't added any notes to this user.", ephemeral=True)
            return
            
        lines = []
        for note_id, note_text, ts in notes:
            lines.append(f"**#{note_id}** (<t:{ts}:D>): {note_text}")
            
        text = "\n".join(lines)
        if len(text) > 4096:
            text = text[:4093] + "..."
            
        embed = discord.Embed(
            title=f"<:note:1534195886501134376> Your Notes for {user.name}",
            description=text,
            color=discord.Color.blue()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @whois_del.command(name="notes", description="Delete a staff note by its ID.")
    @app_commands.describe(note_id="The ID of the note to delete")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.guild_only()
    async def whois_del_notes(self, interaction: discord.Interaction, note_id: int):
        async with self.bot.db.execute(
            "SELECT author_id FROM staff_notes WHERE id = ? AND guild_id = ?",
            (note_id, interaction.guild_id)
        ) as cursor:
            row = await cursor.fetchone()
            
        if not row:
            await interaction.response.send_message("❌ Note not found in this server.", ephemeral=True)
            return
            
        await self.bot.db.execute(
            "DELETE FROM staff_notes WHERE id = ? AND guild_id = ?",
            (note_id, interaction.guild_id)
        )
        await self.bot.db.commit()
        
        await interaction.response.send_message(f"✅ Deleted note `#{note_id}`.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(WhoIs(bot))