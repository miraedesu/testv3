"""Custom reactions (/cr ...) and phrase pools (/cr pool ...)."""
from __future__ import annotations

import random

import discord
from discord import app_commands
from discord.ext import commands
from common.settings_store import get_log_channel

from common.constants import MAX_REPLY_LENGTH, PER_PAGE
from common.reaction_helpers import (
    build_image_payload,
    capture_content,
    expand_placeholders,
    resolve_by_name_or_id,
    resolve_images,
)


# -------------------------------
# PAGINATION FOR /cr list
# -------------------------------
class EntryPagination(discord.ui.View):
    def __init__(self, data: list[tuple], author: discord.abc.User):
        super().__init__(timeout=120)
        self.data = data  # list of (id, name, username)
        self.author = author
        self.page = 0
        self.max_page = max((len(data) - 1) // PER_PAGE, 0)
        self.message: discord.Message | None = None

    def create_embed(self) -> discord.Embed:
        start = self.page * PER_PAGE
        end = start + PER_PAGE
        entries = self.data[start:end]

        description = "\n".join(
            f"**{entry_id}** • `{name}`"
            for entry_id, name, username in entries
        ) or "No entries on this page."

        embed = discord.Embed(
            title=f"Entries ({len(self.data)} total)",
            description=description,
            color=discord.Color.green(),
        )
        embed.set_footer(text=f"Page {self.page + 1}/{self.max_page + 1}")

        self.update_buttons()
        return embed

    def update_buttons(self):
        self.first.disabled = self.page == 0
        self.prev.disabled = self.page == 0
        self.next.disabled = self.page == self.max_page
        self.last.disabled = self.page == self.max_page

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user != self.author:
            await interaction.response.send_message("You can't control this menu.", ephemeral=True)
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

    @discord.ui.button(label="➡", style=discord.ButtonStyle.blurple)
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


class PoolPagination(discord.ui.View):
    def __init__(self, pool_name: str, data: list[tuple], author: discord.abc.User):
        super().__init__(timeout=120)
        self.pool_name = pool_name
        self.data = data  # list of (id, phrase)
        self.author = author
        self.page = 0
        self.max_page = max((len(data) - 1) // PER_PAGE, 0)
        self.message: discord.Message | None = None

    def create_embed(self) -> discord.Embed:
        start = self.page * PER_PAGE
        end = start + PER_PAGE
        entries = self.data[start:end]

        description = "\n".join(
            f"**{entry_id}** • {phrase}" for entry_id, phrase in entries
        ) or "No entries on this page."

        embed = discord.Embed(
            title=f"Pool `{self.pool_name}` ({len(self.data)} phrase(s))",
            description=description,
            color=discord.Color.gold(),
        )
        embed.set_footer(text=f"Page {self.page + 1}/{self.max_page + 1}")

        self.update_buttons()
        return embed

    def update_buttons(self):
        self.first.disabled = self.page == 0
        self.prev.disabled = self.page == 0
        self.next.disabled = self.page == self.max_page
        self.last.disabled = self.page == self.max_page

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user != self.author:
            await interaction.response.send_message("You can't control this menu.", ephemeral=True)
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

    @discord.ui.button(label="➡", style=discord.ButtonStyle.blurple)
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


class BulkAddModal(discord.ui.Modal, title="Bulk add reactions"):
    lines: discord.ui.TextInput = discord.ui.TextInput(
        label="One reply per line",
        style=discord.TextStyle.paragraph,
        placeholder="Good morning %user%!\nOhayou gozaimasu\nBuenos dias",
        required=True,
        max_length=4000,
    )

    def __init__(self, bot: commands.Bot, keyword: str):
        super().__init__()
        self.bot = bot
        self.keyword = keyword

    async def on_submit(self, interaction: discord.Interaction):
        keyword_clean = self.keyword.lower().strip()
        replies = [
            line.strip()[:MAX_REPLY_LENGTH]
            for line in self.lines.value.splitlines()
            if line.strip()
        ]

        if not replies:
            await interaction.response.send_message("No valid lines found.", ephemeral=True)
            return

        await self.bot.db.executemany(
            """INSERT INTO custom_reactions
               (guild_id, user_id, username, name,
                reply_text, attachment_path, attachment_name)
               VALUES (?, ?, ?, ?, ?, NULL, NULL)""",
            [
                (interaction.guild_id, interaction.user.id,
                 interaction.user.name, keyword_clean, reply)
                for reply in replies
            ],
        )
        await self.bot.db.commit()

        await interaction.response.send_message(
            f"Added {len(replies)} reaction(s) for **{keyword_clean}**.", ephemeral=True
        )


class PoolBulkAddModal(discord.ui.Modal, title="Bulk add pool phrases"):
    lines: discord.ui.TextInput = discord.ui.TextInput(
        label="One phrase per line",
        style=discord.TextStyle.paragraph,
        placeholder="Good morning!\nOhayo!\nMorning sunshine",
        required=True,
        max_length=4000,
    )

    def __init__(self, bot: commands.Bot, pool_name: str):
        super().__init__()
        self.bot = bot
        self.pool_name = pool_name

    async def on_submit(self, interaction: discord.Interaction):
        pool_clean = self.pool_name.lower().strip()
        phrases = [
            line.strip()[:MAX_REPLY_LENGTH]
            for line in self.lines.value.splitlines()
            if line.strip()
        ]

        if not phrases:
            await interaction.response.send_message("No valid lines found.", ephemeral=True)
            return

        await self.bot.db.executemany(
            "INSERT INTO phrase_pools (guild_id, pool_name, phrase) VALUES (?, ?, ?)",
            [(interaction.guild_id, pool_clean, phrase) for phrase in phrases],
        )
        await self.bot.db.commit()

        await interaction.response.send_message(
            f"Added {len(phrases)} phrase(s) to pool **{pool_clean}**.", ephemeral=True
        )


class Reactions(commands.Cog):
    """Guild-scoped keyword -> text/image auto-responder, plus phrase pools."""

    # default_permissions applies at the whole-group level (Discord doesn't
    # support per-subcommand overrides), so this covers /cr list and /cr show
    # too, not just the mutating commands. It's a default -- a server admin
    # can still re-expose specific subcommands to non-admins via Integrations
    # settings if they want to, this just controls what's visible out of the box.
    cr = app_commands.Group(
        name="cr",
        description="Custom reaction commands",
        default_permissions=discord.Permissions(administrator=True),
    )
    pool = app_commands.Group(
        name="pool",
        description="Manage phrase pools referenced as %name% in replies (e.g. %morning%, %emote%)",
        parent=cr,
    )

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ---------------- cr commands ----------------

    @cr.command(
        name="add",
        description="Add a Custom Reaction.",
    )
    @app_commands.describe(keyword="Keyword to activate Custom Reaction.")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(administrator=True)
    async def cr_add(self, interaction: discord.Interaction, keyword: str):
        text, attachments = await capture_content(self.bot, interaction)
        if text is None and not attachments:
            return

        reply_text, images, skipped = await resolve_images(text, attachments, interaction.guild_id)

        if not reply_text and not images:
            await interaction.followup.send(
                "None of that was usable (no valid text, and no supported images found). Command canceled.",
                ephemeral=True,
            )
            return

        keyword_clean = keyword.lower().strip()

        cursor = await self.bot.db.execute(
            """INSERT INTO custom_reactions (guild_id, user_id, username, name, reply_text)
               VALUES (?, ?, ?, ?, ?)""",
            (interaction.guild_id, interaction.user.id,
             interaction.user.name, keyword_clean, reply_text),
        )
        reaction_id = cursor.lastrowid

        if images:
            await self.bot.db.executemany(
                """INSERT INTO reaction_images (reaction_id, attachment_path, attachment_name, original_url)
                   VALUES (?, ?, ?, ?)""",
                [(reaction_id, img["path"], img["name"], img["url"])
                 for img in images],
            )
        await self.bot.db.commit()

        async with self.bot.db.execute(
            "SELECT COUNT(*) FROM custom_reactions WHERE guild_id = ? AND name = ?",
            (interaction.guild_id, keyword_clean),
        ) as cursor:
            (variant_count,) = await cursor.fetchone()

        note = f" ({len(images)} image(s) attached)" if images else ""
        if skipped > 0:
            note += f" ({skipped} attachment(s) skipped — unsupported type)"
        log_channel = await get_log_channel(self.bot, interaction.guild_id, "server-log")
        if log_channel is not None:
            log_lines = [f"✅ Added reaction **#{reaction_id}** — `{keyword_clean}` | by **{interaction.user.name}** \n",
                         ]
            if reply_text:
                log_lines.append(f"{reply_text}")
            log_content = "\n".join(log_lines)[:2000]

            log_files, _log_skipped = build_image_payload(
                [(img["path"], img["name"], img["url"]) for img in images]
            )
            log_kwargs = {"content": log_content}
            if log_files:
                log_kwargs["files"] = log_files
            try:
                await log_channel.send(**log_kwargs)
            except discord.Forbidden:
                pass
        if variant_count > 1:
            await interaction.followup.send(
                f"Added another reaction for **{keyword_clean}**{note} "
                f"({variant_count} total — one is picked at random each time).",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(f"Registered **#{reaction_id}** — `{keyword_clean}`{note}", ephemeral=True)

    @cr.command(
        name="bulkadd",
        description="Add many text-only reply reactions at once for a keyword, one per line",
    )
    @app_commands.describe(keyword="Trigger keyword")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(administrator=True)
    async def cr_bulkadd(self, interaction: discord.Interaction, keyword: str):
        await interaction.response.send_modal(BulkAddModal(self.bot, keyword))

    @cr.command(name="edit", description="Edit one specific Custom Reaction by ID or by name (if it has only one variant)")
    @app_commands.describe(
        id="ID of the reaction to edit (see /cr list)",
        name="Keyword name to edit -- only works if it has exactly one variant",
    )
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(administrator=True)
    async def cr_edit(self, interaction: discord.Interaction, id: int = None, name: str = None):
        resolved_id, error = await resolve_by_name_or_id(self.bot, interaction.guild_id, id, name)
        if error:
            await interaction.response.send_message(error, ephemeral=True)
            return
        id = resolved_id

        async with self.bot.db.execute(
            "SELECT name FROM custom_reactions WHERE id = ? AND guild_id = ?",
            (id, interaction.guild_id),
        ) as cursor:
            row = await cursor.fetchone()

        if row is None:
            await interaction.response.send_message("ID not found in this server.", ephemeral=True)
            return

        (name_clean,) = row

        text, attachments = await capture_content(
            self.bot, interaction, prefix=f"Editing reaction **#{id}** — `{name_clean}`.\n"
        )
        if text is None and not attachments:
            return

        reply_text, images, skipped = await resolve_images(text, attachments, interaction.guild_id)

        if not reply_text and not images:
            await interaction.followup.send(
                "None of that was usable (no valid text, and no supported images found). Command canceled.",
                ephemeral=True,
            )
            return

        await self.bot.db.execute(
            "UPDATE custom_reactions SET reply_text = ? WHERE id = ? AND guild_id = ?",
            (reply_text, id, interaction.guild_id),
        )
        # Replace the image set for this variant. This only removes the DB rows,
        # not the actual files on disk -- those stay as your local backups.
        await self.bot.db.execute("DELETE FROM reaction_images WHERE reaction_id = ?", (id,))
        if images:
            await self.bot.db.executemany(
                """INSERT INTO reaction_images (reaction_id, attachment_path, attachment_name, original_url)
                   VALUES (?, ?, ?, ?)""",
                [(id, img["path"], img["name"], img["url"]) for img in images],
            )
        await self.bot.db.commit()

        note = f" ({len(images)} image(s))" if images else ""
        if skipped > 0:
            note += f" ({skipped} attachment(s) skipped — unsupported type)"

        await interaction.followup.send(f"Updated reaction **#{id}** of `{name_clean}`{note}.", ephemeral=True)

    @cr.command(name="delete", description="Delete a Custom Reaction by ID")
    @app_commands.describe(id="ID to delete")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(administrator=True)
    async def cr_delete(self, interaction: discord.Interaction, id: int):
        async with self.bot.db.execute(
            "SELECT name, reply_text FROM custom_reactions WHERE id = ? AND guild_id = ?",
            (id, interaction.guild_id),
        ) as cursor:
            row = await cursor.fetchone()

        if row is None:
            await interaction.response.send_message("ID not found in this server.", ephemeral=True)
            return

        name_clean, reply_text = row

        async with self.bot.db.execute(
            "SELECT attachment_path, attachment_name, original_url FROM reaction_images WHERE reaction_id = ?",
            (id,),
        ) as cursor:
            image_rows = await cursor.fetchall()

        # files, _skipped = build_image_payload(image_rows)

        await self.bot.db.execute(
            "DELETE FROM custom_reactions WHERE id = ? AND guild_id = ?",
            (id, interaction.guild_id),
        )
        # Only removes the DB rows -- the actual image files stay on disk as backups.
        await self.bot.db.execute("DELETE FROM reaction_images WHERE reaction_id = ?", (id,))
        await self.bot.db.commit()
        log_channel = await get_log_channel(self.bot, interaction.guild_id, "server-log")
        if log_channel is not None:
            log_lines = [
                f"🗑️ Deleted reaction **#{id}** — `{name_clean}` | by **{interaction.user.name}** \n",
            ]
            if reply_text:
                log_lines.append(f"{reply_text}")
            log_content = "\n".join(log_lines)[:2000]

            log_files, _log_skipped = build_image_payload(
                image_rows)  # fresh File objects, not reused below
            log_kwargs = {"content": log_content}
            if log_files:
                log_kwargs["files"] = log_files
            try:
                await log_channel.send(**log_kwargs)
            except discord.Forbidden:
                pass

        files, _skipped = build_image_payload(image_rows)
        header = f"🗑️ Deleted reaction **{id}** — `{name_clean}`"
        body = f"{header}\n\n{reply_text}" if reply_text else header

        kwargs = {"content": body[:2000]}
        if files:
            kwargs["files"] = files
        await interaction.response.send_message(**kwargs, ephemeral=True)

    @cr.command(name="list", description="View all Custom Reactions.")
    @app_commands.guild_only()
    async def cr_list(self, interaction: discord.Interaction):
        async with self.bot.db.execute(
            "SELECT id, name, username FROM custom_reactions WHERE guild_id = ? ORDER BY id",
            (interaction.guild_id,),
        ) as cursor:
            data = await cursor.fetchall()

        if not data:
            await interaction.response.send_message("No entries found in this server.", ephemeral=True)
            return

        view = EntryPagination(data, interaction.user)
        await interaction.response.send_message(embed=view.create_embed(), view=view, ephemeral=False)
        view.message = await interaction.original_response()

    @cr.command(name="show", description="Preview a Custom Reaction by ID or by name (if it has only one variant)")
    @app_commands.describe(
        id="ID to view (see /cr list)",
        name="Keyword name to view -- only works if it has exactly one variant",
    )
    @app_commands.guild_only()
    async def cr_show(self, interaction: discord.Interaction, id: int = None, name: str = None):
        resolved_id, error = await resolve_by_name_or_id(self.bot, interaction.guild_id, id, name)
        if error:
            await interaction.response.send_message(error, ephemeral=True)
            return
        id = resolved_id

        async with self.bot.db.execute(
            "SELECT name, username, reply_text FROM custom_reactions WHERE id = ? AND guild_id = ?",
            (id, interaction.guild_id),
        ) as cursor:
            row = await cursor.fetchone()

        if row is None:
            await interaction.response.send_message("ID not found in this server.", ephemeral=True)
            return

        name, username, reply_text = row
        if reply_text:
            reply_text = reply_text.replace("%user%", interaction.user.mention)
            reply_text = await expand_placeholders(self.bot, reply_text, interaction.guild_id)

        header = f"Reaction **#{id}** — `{name}` | by {username}"
        body = f"{header}\n\n{reply_text}" if reply_text else header

        async with self.bot.db.execute(
            "SELECT attachment_path, attachment_name, original_url FROM reaction_images WHERE reaction_id = ?",
            (id,),
        ) as cursor:
            image_rows = await cursor.fetchall()

        files, _skipped = build_image_payload(image_rows)

        body = body[:1990]

        kwargs = {"content": body, "ephemeral": True}
        if files:
            kwargs["files"] = files
        await interaction.response.send_message(**kwargs)

    # ---------------- pool commands ----------------

    @pool.command(name="add", description="Add one phrase to a pool, e.g. 'morning' or 'emote'")
    @app_commands.describe(name="Pool name -- used as %name% in reply text", phrase="Phrase or emote to add")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(administrator=True)
    async def pool_add(self, interaction: discord.Interaction, name: str, phrase: str):
        pool_clean = name.lower().strip()
        phrase_clean = phrase.strip()[:MAX_REPLY_LENGTH]

        if not phrase_clean:
            await interaction.response.send_message("Phrase can't be empty.", ephemeral=True)
            return

        await self.bot.db.execute(
            "INSERT INTO phrase_pools (guild_id, pool_name, phrase) VALUES (?, ?, ?)",
            (interaction.guild_id, pool_clean, phrase_clean),
        )
        await self.bot.db.commit()

        await interaction.response.send_message(
            f"Added to pool **{pool_clean}**: {phrase_clean}", ephemeral=True
        )

    @pool.command(name="bulkadd", description="Add many phrases to a pool at once, one per line")
    @app_commands.describe(name="Pool name -- used as %name% in reply text")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(administrator=True)
    async def pool_bulkadd(self, interaction: discord.Interaction, name: str):
        await interaction.response.send_modal(PoolBulkAddModal(self.bot, name))

    @pool.command(name="list", description="View phrases in a pool")
    @app_commands.describe(name="Pool name to view")
    @app_commands.guild_only()
    async def pool_list(self, interaction: discord.Interaction, name: str):
        pool_clean = name.lower().strip()
        async with self.bot.db.execute(
            "SELECT id, phrase FROM phrase_pools WHERE guild_id = ? AND pool_name = ? ORDER BY id",
            (interaction.guild_id, pool_clean),
        ) as cursor:
            data = await cursor.fetchall()

        if not data:
            await interaction.response.send_message(f"Pool **{pool_clean}** is empty or doesn't exist.", ephemeral=True)
            return

        view = PoolPagination(pool_clean, data, interaction.user)
        await interaction.response.send_message(embed=view.create_embed(), view=view, ephemeral=True)
        view.message = await interaction.original_response()

    @pool.command(name="remove", description="Remove one phrase from a pool by ID")
    @app_commands.describe(id="ID to remove (see /cr pool list)")
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(administrator=True)
    async def pool_remove(self, interaction: discord.Interaction, id: int):
        cursor = await self.bot.db.execute(
            "DELETE FROM phrase_pools WHERE id = ? AND guild_id = ?",
            (id, interaction.guild_id),
        )
        await self.bot.db.commit()

        if cursor.rowcount == 0:
            await interaction.response.send_message("ID not found in this server.", ephemeral=True)
            return

        await interaction.response.send_message(f"Removed phrase {id}.", ephemeral=True)

    @pool.command(name="overview", description="List all pools in this server and how many phrases each has")
    @app_commands.guild_only()
    async def pool_overview(self, interaction: discord.Interaction):
        async with self.bot.db.execute(
            "SELECT pool_name, COUNT(*) FROM phrase_pools WHERE guild_id = ? GROUP BY pool_name ORDER BY pool_name",
            (interaction.guild_id,),
        ) as cursor:
            data = await cursor.fetchall()

        if not data:
            await interaction.response.send_message("No pools created in this server yet.", ephemeral=True)
            return

        description = "\n".join(
            f"`%{name}%` — {count} phrase(s)" for name, count in data)
        embed = discord.Embed(
            title="Phrase pools", description=description, color=discord.Color.gold())
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ---------------- public entry point for the moderation cog ----------------

    async def trigger_check(self, message: discord.Message) -> bool:
        """Called by the moderation cog's on_message, *after* its own filters
        have run, so a deleted/moderated message never also fires a reaction.
        Returns True if a reaction was sent."""
        content_clean = message.content.lower().strip()

        async with self.bot.db.execute(
            "SELECT id, reply_text FROM custom_reactions WHERE guild_id = ? AND name = ?",
            (message.guild.id, content_clean),
        ) as cursor:
            variants = await cursor.fetchall()

        if not variants:
            return False

        reaction_id, reply_text = random.choice(variants)
        if reply_text:
            reply_text = reply_text.replace("%user%", message.author.mention)
            reply_text = await expand_placeholders(self.bot, reply_text, message.guild.id)

        async with self.bot.db.execute(
            "SELECT attachment_path, attachment_name, original_url FROM reaction_images WHERE reaction_id = ?",
            (reaction_id,),
        ) as cursor:
            image_rows = await cursor.fetchall()
        files, _skipped = build_image_payload(image_rows)

        content = reply_text[:2000] if reply_text else None

        kwargs = {}
        if content:
            kwargs["content"] = content
        if files:
            kwargs["files"] = files

        if not kwargs:
            return False

        await message.channel.send(**kwargs)
        return True


async def setup(bot: commands.Bot):
    await bot.add_cog(Reactions(bot))
