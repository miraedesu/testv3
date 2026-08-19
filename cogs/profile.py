from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands
from pathlib import Path
import re
import random
import string
import typing
import logging


DEFAULT_BANNER = Path(__file__).parent.parent / "assets" / "defaultrie_banner.png"

# Discord CDN URL pattern — only allow these for note images
DISCORD_CDN_PATTERN = re.compile(
    r"^https://(cdn\.discordapp\.com|media\.discordapp\.net|cdn\.discord\.com)/.*"
)

# Strip Discord mention formats from user-controlled text
MENTION_PATTERN = re.compile(r"<@!?[&]?\d+>")

# Maximum notes a single user can have for the same target
MAX_NOTES_PER_USER = 3

# Words that are mild and allowed in this server
ALLOWED_MILD_WORDS = {
    "damn","frick", "freaking", "ass", "fuck","fucking",
}
logger = logging.getLogger(__name__)

try:
    from better_profanity import profanity as _prof
    _prof.load_censor_words(whitelist_words=ALLOWED_MILD_WORDS)

    def check_profanity(text: str) -> bool:
        return _prof.contains_profanity(text)
except ImportError:
    def check_profanity(text: str) -> bool:
        logger.info("[Profile] better_profanity not installed — profanity filtering disabled")
        return False
except TypeError:
    # Older versions of better_profanity don't support whitelist_words
    from better_profanity import profanity as _prof
    _prof.load_censor_words()
    _prof.add_censor_words([])  # no-op
    # Manually allow the mild words by temporarily removing them from internal set
    try:
        _prof.CENSOR_WORDS -= ALLOWED_MILD_WORDS
        # better_profanity also has _WHITE_LIST or uses _CENSOR_WORDS Set
    except AttributeError:
        pass

    def check_profanity(text: str) -> bool:
        return _prof.contains_profanity(text)
# ---- Helpers ----

def validate_discord_url(url: str | None) -> str | None:
    """Returns the URL if it matches Discord CDN, otherwise None."""
    if not url:
        return None
    url = url.strip()
    if DISCORD_CDN_PATTERN.match(url):
        return url
    return None


def sanitize_note_text(text: str) -> str:
    """Remove Discord mention patterns from text to prevent ping injection."""
    return MENTION_PATTERN.sub("`@mention`", text)


# ============================================================
#  ACCESS CHECK
# ============================================================

async def can_add_notes(db, member: discord.Member) -> tuple[bool, str]:
    """
    Check if a member can add notes.
    Priority: user-specific entry > role-specific entry > default (allowed).
    Returns (allowed, reason).
    """
    guild_id = member.guild.id

    # 1. Check user-specific entry (highest priority)
    async with db.execute(
        "SELECT status FROM note_access WHERE guild_id = ? AND entity_type = 'user' AND entity_id = ?",
        (guild_id, member.id),
    ) as cursor:
        row = await cursor.fetchone()
        if row:
            if row[0] == "disabled":
                return (False, "You have been individually blocked from adding notes.")
            else:
                return (True, "")

    # 2. Check role-specific entries (highest priority role wins)
    for role in reversed(member.roles):  # highest → lowest
        async with db.execute(
            "SELECT status FROM note_access WHERE guild_id = ? AND entity_type = 'role' AND entity_id = ?",
            (guild_id, role.id),
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                if row[0] == "disabled":
                    return (False, f"The role {role.mention} is blocked from adding notes.")
                else:
                    return (True, "")

    # 3. Default: allowed
    return (True, "")


# ============================================================
#  MODALS
# ============================================================

class NoteAddModal(discord.ui.Modal):
    def __init__(self, target: discord.Member, db, cog):
        super().__init__(title=f"Add note for {target.display_name}")
        self.target = target
        self.db = db
        self.cog = cog

        self.note_input = discord.ui.TextInput(
            label="Note",
            style=discord.TextStyle.paragraph,
            placeholder="Enter your note here...",
            required=True,
            max_length=500,
        )
        self.add_item(self.note_input)

        self.image_input = discord.ui.TextInput(
            label="Image URL (Discord links only, optional)",
            placeholder="https://cdn.discordapp.com/attachments/...",
            required=False,
            max_length=500,
        )
        self.add_item(self.image_input)

    async def on_submit(self, interaction: discord.Interaction):
        # Re-check access in case it changed since modal opened
        allowed, reason = await can_add_notes(self.db, interaction.user)
        if not allowed:
            await interaction.response.send_message(f"❌ {reason}", ephemeral=True)
            return

        # Check max notes per user per target
        async with self.db.execute(
            "SELECT COUNT(*) FROM user_notes WHERE guild_id = ? AND author_id = ? AND target_id = ?",
            (interaction.guild.id, interaction.user.id, self.target.id),
        ) as cursor:
            count = (await cursor.fetchone())[0]

        if count >= MAX_NOTES_PER_USER:
            await interaction.response.send_message(
                f"❌ You already have {MAX_NOTES_PER_USER} notes for {self.target.mention}. "
                f"Delete one before adding a new one (`/note delete`).",
                ephemeral=True,
            )
            return

        note_text = self.note_input.value

        # Server-side length validation
        if len(note_text) > 500:
            await interaction.response.send_message(
                "❌ Note too long (max 500 characters).", ephemeral=True
            )
            return

        # Strip malicious mention patterns
        note_text = sanitize_note_text(note_text)

        if check_profanity(note_text):
            await interaction.response.send_message(
                "❌ Your note contains inappropriate language. Please revise.",
                ephemeral=True,
            )
            return

        image_url = validate_discord_url(self.image_input.value)
        if self.image_input.value and not image_url:
            await interaction.response.send_message(
                "❌ Image URL must be a Discord CDN link "
                "(`cdn.discordapp.com`, `media.discordapp.net`, or `cdn.discord.com`).",
                ephemeral=True,
            )
            return

        note_id = await self.cog.generate_unique_note_id()

        await self.db.execute(
            "INSERT INTO user_notes (note_id, guild_id, author_id, target_id, "
            "note_text, note_image_url) VALUES (?, ?, ?, ?, ?, ?)",
            (note_id, interaction.guild.id, interaction.user.id, self.target.id, note_text, image_url),
        )
        await self.db.commit()

        await interaction.response.send_message(
            f"✅ Note added for {self.target.mention}. Note ID: `{note_id}`",
            ephemeral=True,
        )


class NoteEditModal(discord.ui.Modal):
    def __init__(self, note_id: str, existing_text: str, existing_image: str | None, db, target: discord.Member):
        super().__init__(title=f"Edit note for {target.display_name}")
        self.note_id = note_id
        self.db = db
        self.target = target

        self.note_input = discord.ui.TextInput(
            label="Note",
            style=discord.TextStyle.paragraph,
            default=existing_text,
            required=True,
            max_length=500,
        )
        self.add_item(self.note_input)

        self.image_input = discord.ui.TextInput(
            label="Image URL (Discord links only, optional)",
            default=existing_image or "",
            required=False,
            max_length=500,
        )
        self.add_item(self.image_input)

    async def on_submit(self, interaction: discord.Interaction):
        note_text = self.note_input.value

        # Server-side length validation
        if len(note_text) > 500:
            await interaction.response.send_message(
                "❌ Note too long (max 500 characters).", ephemeral=True
            )
            return

        # Strip malicious mention patterns
        note_text = sanitize_note_text(note_text)

        if check_profanity(note_text):
            await interaction.response.send_message(
                "❌ Your note contains inappropriate language. Please revise.",
                ephemeral=True,
            )
            return

        image_url = validate_discord_url(self.image_input.value)
        if self.image_input.value and not image_url:
            await interaction.response.send_message(
                "❌ Image URL must be a Discord CDN link.",
                ephemeral=True,
            )
            return

        await self.db.execute(
            "UPDATE user_notes SET note_text = ?, note_image_url = ? WHERE note_id = ?",
            (note_text, image_url, self.note_id),
        )
        await self.db.commit()

        await interaction.response.send_message(
            f"✅ Note `{self.note_id}` updated.", ephemeral=True
        )


# ============================================================
#  VIEWS
# ============================================================

class NoteActionView(discord.ui.View):
    def __init__(self, notes: list, author_id: int, target: discord.Member, db, mode: str):
        super().__init__(timeout=60)
        self.message = None
        self.db = db
        self.target = target
        self.mode = mode
        self.author_id = author_id

        options = []
        for row in notes[:25]:
            note_id, note_text = row[0], row[3]
            label = f"{note_id}: {note_text}"
            if len(label) > 100:
                label = label[:97] + "..."
            options.append(discord.SelectOption(label=label, value=note_id, description=f"ID: {note_id}"))

        self.select = discord.ui.Select(
            placeholder=f"Select a note to {mode}...",
            options=options,
            min_values=1,
            max_values=1,
        )
        self.select.callback = self.on_select
        self.add_item(self.select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("These aren't your notes.", ephemeral=True)
            return False
        return True

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True

    async def on_select(self, interaction: discord.Interaction):
        note_id = self.select.values[0]

        if self.mode == "delete":
            await self.db.execute(
                "DELETE FROM user_notes WHERE note_id = ? AND guild_id = ?",
                (note_id, interaction.guild.id),
            )
            await self.db.commit()
            await interaction.response.send_message(f"✅ Note `{note_id}` deleted.", ephemeral=True)
            for item in self.children:
                item.disabled = True
            try:
                await interaction.message.edit(view=self)
            except discord.HTTPException:
                pass

        elif self.mode == "edit":
            async with self.db.execute(
                "SELECT note_text, note_image_url FROM user_notes WHERE note_id = ?", (note_id,)
            ) as cursor:
                row = await cursor.fetchone()

            if row:
                modal = NoteEditModal(note_id, row[0], row[1], self.db, self.target)
                await interaction.response.send_modal(modal)
            else:
                await interaction.response.send_message("❌ Note not found.", ephemeral=True)
class NoteShowPagination(discord.ui.View):
    """Paginate through all notes on a target — interaction user's notes first."""

    def __init__(self, notes: list, target: discord.Member, guild: discord.Guild, author: discord.abc.User):
        super().__init__(timeout=120)
        self.notes = notes  # (note_id, author_id, note_text, note_image_url)
        self.target = target
        self.guild = guild
        self.author = author
        self.page = 0
        self.max_page = len(notes) - 1
        self.message: discord.Message | None = None

    def create_embed(self) -> discord.Embed:
        note_id, author_id, note_text, note_image_url = self.notes[self.page]

        author_member = self.guild.get_member(author_id)
        author_name = author_member.display_name if author_member else f"User `{author_id}`"

        embed = discord.Embed(
            title=f"Notes for {self.target.display_name} by {author_name}",
            description=note_text,
            color=discord.Color.pink(),
        )
        embed.set_thumbnail(url=self.target.display_avatar.url)

        if note_image_url:
            embed.set_image(url=note_image_url)

        embed.set_footer(text=f"Note ID: {note_id} • Page {self.page + 1}/{self.max_page + 1}")
        embed.timestamp = discord.utils.utcnow()
        self.update_buttons()
        return embed

    def update_buttons(self):
        self.prev.disabled = self.page == 0
        self.next.disabled = self.page == self.max_page

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

    @discord.ui.button(label="⬅", style=discord.ButtonStyle.gray)
    async def prev(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = max(self.page - 1, 0)
        await interaction.response.edit_message(embed=self.create_embed(), view=self)

    @discord.ui.button(label="➡", style=discord.ButtonStyle.gray)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = min(self.page + 1, self.max_page)
        await interaction.response.edit_message(embed=self.create_embed(), view=self)


class NoteListPagination(discord.ui.View):
    """Paginate through all notes the interaction user has authored."""

    def __init__(self, notes: list, guild: discord.Guild, author: discord.abc.User):
        super().__init__(timeout=120)
        self.notes = notes  # (note_id, target_id, note_text, note_image_url)
        self.guild = guild
        self.author = author
        self.page = 0
        self.per_page = 5
        self.max_page = max((len(notes) - 1) // self.per_page, 0)
        self.message: discord.Message | None = None

    def create_embed(self) -> discord.Embed:
        start = self.page * self.per_page
        end = start + self.per_page
        entries = self.notes[start:end]

        embed = discord.Embed(
            title="<:RieCaroling:1533761233327493241> Your Notes",
            color=discord.Color.pink(),
            timestamp=discord.utils.utcnow(),
        )

        if not entries:
            embed.description = "No notes on this page."
        else:
            lines: list[str] = []
            for note_id, target_id, note_text, note_image_url in entries:
                member = self.guild.get_member(target_id)
                target_name = member.display_name if member else f"User `{target_id}` (left)"
                preview = note_text if len(note_text) <= 80 else note_text[:77] + "..."
                icon = "🖼️ " if note_image_url else ""
                lines.append(f"**{target_name}** — `{note_id}`\n{icon}{preview}")
            embed.description = "\n\n".join(lines)

        embed.set_footer(text=f"Page {self.page + 1}/{self.max_page + 1} • Total: {len(self.notes)}")
        self.update_buttons()
        return embed

    def update_buttons(self):
        self.prev.disabled = self.page == 0
        self.next.disabled = self.page == self.max_page

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

    @discord.ui.button(label="⬅", style=discord.ButtonStyle.gray)
    async def prev(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = max(self.page - 1, 0)
        await interaction.response.edit_message(embed=self.create_embed(), view=self)

    @discord.ui.button(label="➡", style=discord.ButtonStyle.gray)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = min(self.page + 1, self.max_page)
        await interaction.response.edit_message(embed=self.create_embed(), view=self)

class UserinfoView(discord.ui.View):
    def __init__(self, member: discord.Member, author: discord.abc.User, banner_url: str | None, db):
        super().__init__(timeout=120)
        self.member = member
        self.author = author
        self.page = 0
        self.banner_url = banner_url
        self.use_default_banner = banner_url == "attachment://defaultrie_banner.png"
        self.db = db

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author.id:
            await interaction.response.send_message("This menu isn't for you.", ephemeral=True)
            return False
        return True

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        if hasattr(self, "message"):
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    async def update_page(self, interaction: discord.Interaction):
        embed = await self.current_embed()
        if self.page == 0 and self.use_default_banner:
            file = discord.File(DEFAULT_BANNER, filename="defaultrie_banner.png")
            await interaction.response.edit_message(
                embed=embed, view=self, attachments=[file],  # Pass the file inside attachments=
                allowed_mentions=discord.AllowedMentions.none()
            )
        else:
            await interaction.response.edit_message(
                embed=embed, view=self, attachments=[],  # Empty list clears the file so it doesn't show below the embed
                allowed_mentions=discord.AllowedMentions.none()
            )

    def build_profile_embed(self) -> discord.Embed:
        m = self.member
        created = discord.utils.format_dt(m.created_at, "F")
        joined = discord.utils.format_dt(m.joined_at, "F") if m.joined_at else "Unknown"

        if m.premium_since:
            boost_date = discord.utils.format_dt(m.premium_since, "F")
            boost = f"Since {boost_date}"
        else:
            boost = "Not boosting"

        embed = discord.Embed(
            # title=f"{m.name}'s Profile",
            color=discord.Color.pink(),
        )
        embed.set_author(name=m.name, icon_url=m.display_avatar.url)
        embed.set_thumbnail(url=m.display_avatar.url)
        embed.add_field(name="User ID", value=str(m.id), inline=False)
        embed.add_field(name="Username", value=m.name, inline=True)
        embed.add_field(name="Display Name", value=m.display_name, inline=True)
        if m.nick:
            embed.add_field(name="Server Nickname", value=m.nick, inline=True)
        embed.add_field(name="Account Created", value=created, inline=False)
        embed.add_field(name="Joined Server", value=joined, inline=False)
        embed.add_field(name="Server Boost", value=boost, inline=False)

        if self.banner_url:
            embed.set_image(url=self.banner_url)

        embed.set_footer(text=f"User ID: {m.id}")
        embed.timestamp = discord.utils.utcnow()
        return embed

    def build_roles_embed(self) -> discord.Embed:
        m = self.member
        roles = [r for r in reversed(m.roles) if not r.is_default()]
        if roles:
            roles_text = ", ".join(r.mention for r in roles)
            if len(roles_text) > 4096:
                roles_text = roles_text[:4093] + "..."
        else:
            roles_text = "No roles."

        embed = discord.Embed(
            title=f"{m.name}'s Roles",
            description=roles_text,
            color=discord.Color.pink(),
        )
        embed.set_thumbnail(url=m.display_avatar.url)
        embed.set_footer(text=f"User ID: {m.id}")
        embed.timestamp = discord.utils.utcnow()
        return embed

    async def build_notes_embed(self) -> discord.Embed:
        m = self.member
        notes = []
        if self.db:
            async with self.db.execute(
                "SELECT note_id, author_id, target_id, note_text, note_image_url "
                "FROM user_notes WHERE guild_id = ? AND target_id = ? AND author_id = ? "
                "ORDER BY created_at ASC",
                (m.guild.id, m.id, self.author.id),
            ) as cursor:
                notes = await cursor.fetchall()

        embed = discord.Embed(
            title=f"Notes for {m.name}",
            color=discord.Color.pink(),
        )
        embed.set_thumbnail(url=m.display_avatar.url)

        if not notes:
            embed.description = "You haven't added any notes for this user."
            embed.set_footer(text=f"User ID: {m.id}")
        else:
            parts = []
            image_url = None
            for row in notes:
                note_id, author_id, _, note_text, note_image_url = row
                parts.append(note_text)
                if note_image_url:
                    image_url = note_image_url

            description = "\n\n".join(parts)
            if len(description) > 4096:
                description = description[:4093] + "..."
            embed.description = description

            if image_url:
                embed.set_image(url=image_url)

            note_ids = ", ".join(n[0] for n in notes)
            if len(note_ids) > 800:
                note_ids = note_ids[:797] + "..."
            embed.set_footer(text=f"User ID: {m.id} • Note ID: {note_ids}")

        embed.timestamp = discord.utils.utcnow()
        return embed

    async def current_embed(self) -> discord.Embed:
        if self.page == 0:
            return self.build_profile_embed()
        elif self.page == 1:
            return self.build_roles_embed()
        return await self.build_notes_embed()

    @discord.ui.button(label="User Profile", emoji="<:RiePing:1533761273798328460>", style=discord.ButtonStyle.secondary)
    async def profile_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = 0
        await self.update_page(interaction)

    @discord.ui.button(label="Roles", emoji="<:RieThumbsUp:1533761254513184818>", style=discord.ButtonStyle.secondary)
    async def roles_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = 1
        await self.update_page(interaction)

    @discord.ui.button(label="Notes", emoji="<:RieCaroling:1533761233327493241>", style=discord.ButtonStyle.secondary)
    async def notes_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = 2
        await self.update_page(interaction)


# ============================================================
#  COG
# ============================================================

class Profile(commands.Cog):
    note = app_commands.Group(name="note", description="Manage user notes")
    note_admin = app_commands.Group(
        name="note_admin",
        description="Admin tools for managing user notes",
        default_permissions=discord.Permissions(administrator=True)
    )
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def generate_unique_note_id(self) -> str:
        """Generate a unique 6-character note ID."""
        while True:
            note_id = "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
            async with self.bot.db.execute(
                "SELECT 1 FROM user_notes WHERE note_id = ?", (note_id,)
            ) as cursor:
                if not await cursor.fetchone():
                    return note_id

    # ---- /userinfo ----

    @app_commands.command(name="userinfo", description="View a member's profile (3 pages).")
    @app_commands.describe(member="Member to view; defaults to yourself")
    @app_commands.checks.cooldown(1, 5.0, key=lambda i: i.user.id)
    # async def userinfo(self, interaction: discord.Interaction, member: discord.Member | None):
    async def userinfo(self, interaction: discord.Interaction, member: discord.Member | None = None):
        member = member or interaction.user
        if not isinstance(member, discord.Member):
            member = interaction.user

        try:
            user = await self.bot.fetch_user(member.id)
        except discord.HTTPException:
            user = member

        banner_url = user.banner.url if user.banner else None
        view = UserinfoView(member, interaction.user, banner_url, self.bot.db)

        if banner_url:
            embed = await view.current_embed()
            await interaction.response.send_message(
                embed=embed, view=view, allowed_mentions=discord.AllowedMentions.none()
            )
        else:
            view.use_default_banner = True
            view.banner_url = "attachment://defaultrie_banner.png"
            file = discord.File(DEFAULT_BANNER, filename="defaultrie_banner.png")
            embed = await view.current_embed()
            await interaction.response.send_message(
                embed=embed, view=view, file=file, allowed_mentions=discord.AllowedMentions.none()
            )

        view.message = await interaction.original_response()

    # ---- /serverinfo ----

    @app_commands.command(name="serverinfo", description="View server info.")
    async def serverinfo(self, interaction: discord.Interaction):
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return

        total = guild.member_count or len(guild.members) or 0
        humans = sum(1 for m in guild.members if not m.bot) if guild.members else "?"
        bots = sum(1 for m in guild.members if m.bot) if guild.members else "?"

        text_count = 0
        text_locked = 0
        voice_count = 0
        voice_locked = 0

        for channel in guild.channels:
            ow = channel.overwrites_for(guild.default_role)
            is_locked = ow.view_channel is False
            if isinstance(channel, discord.TextChannel):
                text_count += 1
                if is_locked:
                    text_locked += 1
            elif isinstance(channel, discord.VoiceChannel):
                voice_count += 1
                if is_locked:
                    voice_locked += 1

        boost_level = guild.premium_tier
        boost_count = guild.premium_subscription_count or 0
        owner = guild.owner
        owner_str = owner.name if owner else f"Unknown ({guild.owner_id})"
        created = guild.created_at.strftime("%m/%d/%Y %H:%M")

        embed = discord.Embed(title=f"Server Info — {guild.name}", color=discord.Color.pink())
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)

        embed.add_field(name="Owner", value=owner_str, inline=False)
        embed.add_field(name="Members", value=f"Total: {total}\nHumans: {humans}\nBots: {bots}", inline=True)
        text_locked_str = f" ({text_locked} locked)" if text_locked > 0 else ""
        voice_locked_str = f" ({voice_locked} locked)" if voice_locked > 0 else ""
        embed.add_field(
            name="Channels",
            value=f"<:text_channel:1534196045616124054> {text_count}{text_locked_str}\n"
                  f"<:voice_chat:1534195973058859249> {voice_count}{voice_locked_str}",
            inline=True
        )
        embed.add_field(name="Roles", value=f"{len(guild.roles)} roles", inline=True)
        embed.add_field(name="Boost Level", value=f"Level {boost_level} ({boost_count} boosts)", inline=True)
        embed.set_footer(text=f"ID: {guild.id} • Created {created}")

        if guild.banner:
            embed.set_image(url=guild.banner.url)

        await interaction.response.send_message(embed=embed)
    @app_commands.command(name="roleinfo", description="View information about a role")
    @app_commands.describe(role="Role to inspect")
    @app_commands.guild_only()
    async def roleinfo(self, interaction: discord.Interaction, role: discord.Role):
        member_count = len(role.members)
        if role.color == discord.Color.default():
            color_str = "Default"
            embed_color = discord.Color.pink()
        else:
            color_str = f"#{role.color.value:06X}"
            embed_color = role.color

        created = discord.utils.format_dt(role.created_at, "F")

        description = (
            f"**Role Name:** {role.name}\n"
            f"**Members in Role:** {member_count}\n"
            f"**Color:** {color_str}\n"
            f"**Created:** {created}"
        )

        embed = discord.Embed(
            title="Role Info",
            description=description,
            color=embed_color,
        )
        embed.set_footer(text=f"Role ID: {role.id}")

        await interaction.response.send_message(embed=embed)
    # ---- /note add ----

    @note.command(name="add", description="Add a note for a member (1 note per user per target).")
    @app_commands.describe(member="Member to add a note for")
    @app_commands.guild_only()
    @app_commands.checks.cooldown(1, 10.0, key=lambda i: i.user.id)
    async def note_add(self, interaction: discord.Interaction, member: discord.Member):
        allowed, reason = await can_add_notes(self.bot.db, interaction.user)
        if not allowed:
            await interaction.response.send_message(f"❌ {reason}", ephemeral=True)
            return

        async with self.bot.db.execute(
            "SELECT COUNT(*) FROM user_notes WHERE guild_id = ? AND author_id = ? AND target_id = ?",
            (interaction.guild.id, interaction.user.id, member.id),
        ) as cursor:
            count = (await cursor.fetchone())[0]

        if count >= MAX_NOTES_PER_USER:
            await interaction.response.send_message(
                f"❌ You already have {MAX_NOTES_PER_USER} notes for {member.mention}. "
                f"Delete one before adding a new one (`/note delete`).",
                ephemeral=True,
            )
            return

        modal = NoteAddModal(member, self.bot.db, self)
        await interaction.response.send_modal(modal)

    # ---- /note show ----

    @note.command(name="show", description="Show all notes on a member (yours first, then others').")
    @app_commands.describe(member="Member whose notes you want to see")
    @app_commands.guild_only()
    async def note_show(self, interaction: discord.Interaction, member: discord.Member):
        # Fetch ALL notes on this target, interaction user's notes first
        async with self.bot.db.execute(
            "SELECT note_id, author_id, note_text, note_image_url "
            "FROM user_notes WHERE guild_id = ? AND target_id = ? "
            "ORDER BY (author_id != ?), created_at ASC",
            (interaction.guild.id, member.id, interaction.user.id),
        ) as cursor:
            notes = await cursor.fetchall()

        if not notes:
            await interaction.response.send_message(
                f"There are no notes for {member.mention} yet.", ephemeral=True
            )
            return

        view = NoteShowPagination(notes, member, interaction.guild, interaction.user)
        embed = view.create_embed()
        await interaction.response.send_message(embed=embed, view=view, ephemeral=False)
        view.message = await interaction.original_response()
    # ---- /note list ----

    @note.command(name="list", description="View all notes you've added (paginated).")
    @app_commands.guild_only()
    async def note_list(self, interaction: discord.Interaction):
        async with self.bot.db.execute(
            "SELECT note_id, target_id, note_text, note_image_url "
            "FROM user_notes WHERE guild_id = ? AND author_id = ? "
            "ORDER BY created_at ASC",
            (interaction.guild.id, interaction.user.id),
        ) as cursor:
            notes = await cursor.fetchall()

        if not notes:
            await interaction.response.send_message(
                "You haven't added any notes yet.", ephemeral=True
            )
            return

        view = NoteListPagination(notes, interaction.guild, interaction.user)
        embed = view.create_embed()
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        view.message = await interaction.original_response()
    # ---- /note delete ----

    @note.command(name="delete", description="Delete a note you added for a member.")
    @app_commands.describe(member="Member whose note you want to delete")
    @app_commands.guild_only()
    async def note_delete(self, interaction: discord.Interaction, member: discord.Member):
        async with self.bot.db.execute(
            "SELECT note_id, author_id, target_id, note_text, note_image_url "
            "FROM user_notes WHERE guild_id = ? AND target_id = ? AND author_id = ? "
            "ORDER BY created_at ASC",
            (interaction.guild.id, member.id, interaction.user.id),
        ) as cursor:
            notes = await cursor.fetchall()

        if not notes:
            await interaction.response.send_message(
                f"You have no notes on {member.mention}.", ephemeral=True
            )
            return

        if len(notes) == 1:
            note_id, _, _, note_text, _ = notes[0]
            await self.bot.db.execute(
                "DELETE FROM user_notes WHERE note_id = ? AND guild_id = ?",
                (note_id, interaction.guild.id),
            )
            await self.bot.db.commit()

            embed = discord.Embed(
                title="✅ Note Deleted",
                description=f"Your note for {member.mention} has been removed.",
                color=discord.Color.red(),
                timestamp=discord.utils.utcnow(),
            )
            embed.add_field(name="Note ID", value=f"`{note_id}`", inline=True)
            deleted_preview = note_text if len(note_text) <= 200 else note_text[:197] + "..."
            embed.add_field(name="Deleted Content", value=deleted_preview, inline=False)
            embed.set_footer(text=f"User ID: {member.id}")
            await interaction.response.send_message(embed=embed, ephemeral=True)
        else:
            view = NoteActionView(notes, interaction.user.id, member, self.bot.db, "delete")
            await interaction.response.send_message(
                f"You have {len(notes)} notes for {member.mention}. Select one to delete:",
                view=view,
                ephemeral=True,
            )
            view.message = await interaction.original_response()
    # ---- /note edit ----

    @note.command(name="edit", description="Edit a note you added for a member.")
    @app_commands.describe(member="Member whose note you want to edit")
    @app_commands.guild_only()
    async def note_edit(self, interaction: discord.Interaction, member: discord.Member):
        async with self.bot.db.execute(
            "SELECT note_id, author_id, target_id, note_text, note_image_url "
            "FROM user_notes WHERE guild_id = ? AND target_id = ? AND author_id = ? "
            "ORDER BY created_at ASC",
            (interaction.guild.id, member.id, interaction.user.id),
        ) as cursor:
            notes = await cursor.fetchall()

        if not notes:
            await interaction.response.send_message(
                f"You have no notes on {member.mention}.", ephemeral=True
            )
            return

        if len(notes) == 1:
            note_id, _, _, note_text, note_image_url = notes[0]
            modal = NoteEditModal(note_id, note_text, note_image_url, self.bot.db, member)
            await interaction.response.send_modal(modal)
        else:
            view = NoteActionView(notes, interaction.user.id, member, self.bot.db, "edit")
            await interaction.response.send_message(
                f"You have {len(notes)} notes for {member.mention}. Select one to edit:",
                view=view,
                ephemeral=True,
            )
            view.message = await interaction.original_response()
    # ---- /note admin_delete ----

    @note_admin.command(name="delete", description="Delete any note by its ID (admin only).")
    @app_commands.describe(note_id="The 6-character note ID to delete")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.guild_only()
    async def note_admin_delete(self, interaction: discord.Interaction, note_id: str):
        note_id = note_id.strip().upper()

        cursor = await self.bot.db.execute(
            "DELETE FROM user_notes WHERE note_id = ? AND guild_id = ?",
            (note_id, interaction.guild.id),
        )
        await self.bot.db.commit()

        if cursor.rowcount > 0:
            await interaction.response.send_message(f"✅ Note `{note_id}` has been deleted.", ephemeral=True)
        else:
            await interaction.response.send_message(f"❌ No note found with ID `{note_id}` in this server.", ephemeral=True)

    # ---- /note admin_enable ----

    @note_admin.command(name="enable", description="Enable note-adding for a user or role (admin only).")
    @app_commands.describe(
        user="User to enable (leave empty if enabling a role)",
        role="Role to enable (leave empty if enabling a user)",
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.guild_only()
    async def note_admin_enable(
        self,
        interaction: discord.Interaction,
        user: discord.Member | None = None,
        role: discord.Role | None = None,

    ):
        if not user and not role:
            await interaction.response.send_message("❌ Provide a user or role to enable.", ephemeral=True)
            return
        if user and role:
            await interaction.response.send_message("❌ Provide only one — either a user or a role, not both.", ephemeral=True)
            return

        if user:
            entity_type, entity_id, entity_name = "user", user.id, user.mention
        else:
            entity_type, entity_id, entity_name = "role", role.id, role.mention

        await self.bot.db.execute(
            "INSERT INTO note_access (guild_id, entity_type, entity_id, status) "
            "VALUES (?, ?, ?, 'enabled') "
            "ON CONFLICT(guild_id, entity_type, entity_id) DO UPDATE SET status = 'enabled'",
            (interaction.guild.id, entity_type, entity_id),
        )
        await self.bot.db.commit()

        await interaction.response.send_message(
            f"✅ {entity_type.capitalize()} {entity_name} can now add notes.",
            ephemeral=True,
        )

    # ---- /note admin_disable ----

    @note_admin.command(name="disable", description="Disable note-adding for a user or role (admin only).")
    @app_commands.describe(
        user="User to disable (leave empty if disabling a role)",
        role="Role to disable (leave empty if disabling a user)",
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.guild_only()
    async def note_admin_disable(
        self,
        interaction: discord.Interaction,
        user: discord.Member | None = None,
        role: discord.Role | None = None,
    ):
        if not user and not role:
            await interaction.response.send_message("❌ Provide a user or role to disable.", ephemeral=True)
            return
        if user and role:
            await interaction.response.send_message("❌ Provide only one — either a user or a role, not both.", ephemeral=True)
            return

        if user:
            entity_type, entity_id, entity_name = "user", user.id, user.mention
        else:
            entity_type, entity_id, entity_name = "role", role.id, role.mention

        await self.bot.db.execute(
            "INSERT INTO note_access (guild_id, entity_type, entity_id, status) "
            "VALUES (?, ?, ?, 'disabled') "
            "ON CONFLICT(guild_id, entity_type, entity_id) DO UPDATE SET status = 'disabled'",
            (interaction.guild.id, entity_type, entity_id),
        )
        await self.bot.db.commit()

        await interaction.response.send_message(
            f"✅ {entity_type.capitalize()} {entity_name} can no longer add notes.",
            ephemeral=True,
        )

    # ---- /note admin_list ----

    @note_admin.command(name="list", description="View all enabled/disabled note access entries (admin only).")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.guild_only()
    async def note_admin_list(self, interaction: discord.Interaction):
        guild = interaction.guild

        async with self.bot.db.execute(
            "SELECT entity_type, entity_id, status FROM note_access WHERE guild_id = ? "
            "ORDER BY status ASC, entity_type ASC",
            (guild.id,),
        ) as cursor:
            rows = await cursor.fetchall()

        if not rows:
            await interaction.response.send_message(
                "No access entries configured. Everyone can add notes by default.",
                ephemeral=True,
            )
            return

        enabled_entries: list[str] = []
        disabled_entries: list[str] = []

        for entity_type, entity_id, status in rows:
            if entity_type == "user":
                member = guild.get_member(entity_id)
                name = member.mention if member else f"User `{entity_id}` (left server)"
            else:
                role = guild.get_role(entity_id)
                name = role.mention if role else f"Role `{entity_id}` (deleted)"

            if status == "enabled":
                enabled_entries.append(name)
            else:
                disabled_entries.append(name)

        embed = discord.Embed(
            title="<:note:1534195886501134376> Note Access List",
            color=discord.Color.pink(),
            timestamp=discord.utils.utcnow(),
        )

        if disabled_entries:
            disabled_text = "\n".join(disabled_entries)
            if len(disabled_text) > 1024:
                disabled_text = disabled_text[:1021] + "..."
            embed.add_field(
                name=f"❌ Disabled ({len(disabled_entries)})",
                value=disabled_text,
                inline=False,
            )

        if enabled_entries:
            enabled_text = "\n".join(enabled_entries)
            if len(enabled_text) > 1024:
                enabled_text = enabled_text[:1021] + "..."
            embed.add_field(
                name=f"✅ Enabled ({len(enabled_entries)})",
                value=enabled_text,
                inline=False,
            )

        embed.set_footer(text=f"Total entries: {len(rows)} • Server ID: {guild.id}")

        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ---- /avatar ----
    @app_commands.command(name="avatar", description="Display user avatar")
    @app_commands.describe(target="The member whose avatar you want to view")
    async def avatar(self, interaction: discord.Interaction, target: discord.Member = None):
        target_user = target or interaction.user
        avatar_url = target_user.display_avatar.with_size(4096).url
        profile_link = f"https://discord.com/users/{target_user.id}"

        embed = discord.Embed(
            description=f"**Avatar for [{target_user.display_name}]({profile_link})**",
            color=0xffb6c1
        )
        embed.set_image(url=avatar_url)

        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="banner", description="Display user banner")
    @app_commands.describe(target="Whos banner you wanna see")
    async def banner(self, interaction: discord.Interaction, target: discord.Member = None):
        target_user = target or interaction.user

        try:
            full_user = await self.bot.fetch_user(target_user.id)
        except discord.NotFound:
            await interaction.response.send_message("Could not find user", ephemeral=True)
            return

        if full_user.banner:
            banner_url = full_user.banner.with_size(4096).url
            profile_link = f"https://discord.com/users/{target_user.id}"
            embed = discord.Embed(
                description=f"**Banner for [{target_user.display_name}]({profile_link})**",
                color=0xffb6c1
            )
            embed.set_image(url=banner_url)
            await interaction.response.send_message(embed=embed)
        else:
            await interaction.response.send_message(f"{target_user.display_name} doesnt have banner", ephemeral=True)

    @app_commands.command(name="server", description="Displays the server icon or server banner")
    @app_commands.describe(target="Choose whether you want to see the server icon or banner")
    @app_commands.guild_only()
    async def server_target(self, interaction: discord.Interaction, target: typing.Literal["Icon", "Banner"]):
        guild = interaction.guild

        if target == "Icon":
            if guild.icon:
                icon_url = guild.icon.with_size(4096).url
                await interaction.response.send_message(icon_url)
            else:
                await interaction.response.send_message("This server does not have an icon set!", ephemeral=True)
        elif target == "Banner":
            if guild.banner:
                banner_url = guild.banner.with_size(4096).url
                await interaction.response.send_message(banner_url)
            else:
                await interaction.response.send_message(
                    "This server does not have a banner set! (It may need more server boosts).",
                    ephemeral=True
                )

async def setup(bot: commands.Bot):
    await bot.add_cog(Profile(bot))