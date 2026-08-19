from __future__ import annotations

import discord
import aiohttp
from discord import app_commands
from discord.ext import commands
import re
import logging

ANILIST_GRAPHQL_URL = "https://graphql.anilist.co"

anilist_img = "https://cdn.discordapp.com/attachments/1509272155546845445/1534221439954063461/anilist-logo.png?ex=6a73568f&is=6a72050f&hm=ab7ea1fde5d9fdad285401d78fe7810def08cb1ed52980d15286951829440702&"
ANILIST_SEARCH_QUERY = """
query ($search: String!, $type: MediaType!, $perPage: Int!, $sort: [MediaSort!]) {
  Page(page: 1, perPage: $perPage) {
    media(search: $search, type: $type, sort: $sort) {
      id
      title { romaji english native }
      format
      episodes
      chapters
      status
      averageScore
      startDate { year }
      coverImage { extraLarge color }
    }
  }
}
"""

ANILIST_DETAILS_QUERY = """
query ($id: Int!) {
  Media(id: $id) {
    id
    title { romaji english native }
    format
    episodes
    chapters
    status
    source
    genres
    averageScore
    description
    siteUrl
    coverImage { extraLarge color }
    nextAiringEpisode {
      episode
      airingAt
      timeUntilAiring
    }
  }
}
"""

_ANILIST_TAG_RE = re.compile(r"(#|__|~~|!|/)[^~!]*(#|__|~~|!|/)|<br\s*/?>|<[^>]+>")

logger = logging.getLogger(__name__)
def clean_anilist_description(desc: str | None, max_length: int = 400) -> str:
    if not desc:
        return "No description available."
    desc = _ANILIST_TAG_RE.sub("", desc)
    desc = re.sub(r"\s+", " ", desc).strip()
    if len(desc) > max_length:
        desc = desc[: max_length - 1].rstrip() + "…"
    return desc


def format_media_format(fmt: str | None) -> str:
    if not fmt:
        return "Unknown"
    return fmt.replace("_", " ").title()


def format_media_status(status: str | None) -> str:
    if not status:
        return "Unknown"
    return status.replace("_", " ").title()


def format_score(score: int | None) -> str:
    if not score:
        return "N/A"
    # Convert 74 -> 7.4/10
    return f"{score / 10:.1f}/10"

def format_time_until(seconds: int | None) -> str:
    """Convert seconds until airing to a short countdown string."""
    if not seconds or seconds <= 0:
        return ""
    days = seconds // 86400
    hours = (seconds % 86400) // 3600
    minutes = (seconds % 3600) // 60
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes and not days:  # only show minutes for sub-day counts
        parts.append(f"{minutes}m")
    return " ".join(parts) if parts else "<1m"

def build_anilist_embed(media: dict, media_type: str, requester: discord.abc.User) -> discord.Embed:
    title_obj = media.get("title", {})
    primary_title = title_obj.get("english") or title_obj.get("romaji") or "Unknown Title"
    site_url = media.get("siteUrl") or "https://anilist.co"

    cover_color = media.get("coverImage", {}).get("color")
    if cover_color:
        try:
            embed_color = discord.Color.from_str(cover_color)
        except ValueError:
            embed_color = discord.Color(0x2E51A2)
    else:
        embed_color = discord.Color(0x2E51A2)

    embed = discord.Embed(
        title=primary_title,
        url=site_url,
        description=clean_anilist_description(media.get("description")),
        color=embed_color,
    )

    fmt = format_media_format(media.get("format"))
    status = format_media_status(media.get("status"))
    source = (media.get("source") or "Unknown").replace("_", " ").title()

    # Limit genres to 3
    genres = media.get("genres") or []
    if len(genres) > 3:
        genres = genres[:3]
    
    score_str = format_score(media.get("averageScore"))
    genres_str = ", ".join(genres) if genres else "N/A"

    if media_type == "ANIME":
        episodes = media.get("episodes")
        episodes_str = str(episodes) if episodes else "Unknown"
        # Airing schedule (if available)
        next_airing = media.get("nextAiringEpisode")
        if next_airing:
            # Currently airing
            next_ep = next_airing.get("episode")
            time_until = next_airing.get("timeUntilAiring")
            countdown = format_time_until(time_until) if time_until else "soon"

            if episodes:
                # Known total → "ep #18/24: 2d 4h"
                episodes_str = f"EP {next_ep}/{episodes}:  {countdown}"
            else:
                # Unknown total → "ep #18 in 2d 4h"
                episodes_str = f"EP {next_ep}: {countdown}"
        else:
            # Not currently airing — show total count (or "Unknown")
            episodes_str = str(episodes) if episodes else "Unknown"

        embed.add_field(name="Format", value=fmt, inline=True)
        embed.add_field(name="Episodes", value=episodes_str, inline=True)
        embed.add_field(name="Status", value=status, inline=True)
        embed.add_field(name="Source", value=source, inline=True)
        embed.add_field(name="Genres", value=genres_str, inline=True)
        embed.add_field(name="Score", value=score_str, inline=True)
    else:
        chapters = media.get("chapters")
        chapters_str = str(chapters) if chapters else "Unknown"
        embed.add_field(name="Format", value=fmt, inline=True)
        embed.add_field(name="Chapters", value=chapters_str, inline=True)
        embed.add_field(name="Status", value=status, inline=True)
        embed.add_field(name="Source", value=source, inline=True)
        embed.add_field(name="Genres", value=genres_str, inline=True)
        embed.add_field(name="Score", value=score_str, inline=True)

    cover_url = media.get("coverImage", {}).get("extraLarge")
    if cover_url:
        embed.set_image(url=cover_url)
    embed.set_footer(text=f"Requested by {requester} • Powered by AniList", icon_url=anilist_img)
    return embed


async def search_anilist(query: str, media_type: str, per_page: int = 15, sort: str = "SEARCH_MATCH") -> list[dict] | str:
    payload = {
        "query": ANILIST_SEARCH_QUERY,
        "variables": {
            "search": query,
            "type": media_type,
            "perPage": per_page,
            "sort": [sort]
        },
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                ANILIST_GRAPHQL_URL, json=payload, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status == 403:
                    logger.info("[AniList] API is temporarily disabled (403)")
                    return "API_DISABLED"
                if resp.status != 200:
                    return []
                data = await resp.json()
                return data.get("data", {}).get("Page", {}).get("media", []) or []
    except Exception as e:
        logger.error(f"[AniList] Search error: {e}")
        return []

async def fetch_anilist_details(media_id: int) -> dict | None:
    payload = {
        "query": ANILIST_DETAILS_QUERY,
        "variables": {"id": media_id},
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(
            ANILIST_GRAPHQL_URL, json=payload, timeout=aiohttp.ClientTimeout(total=10)
        ) as resp:
            logger.debug(f"[AniList] Details status: {resp.status}")  # Debug log
            if resp.status != 200:
                error_text = await resp.text()
                logger.error(f"[AniList] Details error: {error_text}")
                return None
            data = await resp.json()
            return data.get("data", {}).get("Media")


class AniListSelectView(discord.ui.View):
    """Select dropdown view for choosing from search results."""

    def __init__(self, results: list[dict], media_type: str, author: discord.abc.User):
        super().__init__(timeout=60)
        self.results = results
        self.media_type = media_type
        self.author = author

        options = []
        for media in results[:25]:  # Discord hard limit: 25 options
            title_obj = media.get("title", {})
            title = title_obj.get("english") or title_obj.get("romaji") or "Unknown"
            media_id = media.get("id")
            fmt = format_media_format(media.get("format"))
            year = media.get("startDate", {}).get("year")
            year_str = f" ({year})" if year else ""

            status = format_media_status(media.get("status"))
            score_str = format_score(media.get("averageScore"))

            if media_type == "ANIME":
                eps = media.get("episodes")
                eps_str = f"{eps} ep" if eps else "? ep"
                desc = f"{fmt} • {eps_str} • {status} • {score_str}{year_str}"
            else:
                ch = media.get("chapters")
                ch_str = f"{ch} ch" if ch else "? ch"
                desc = f"{fmt} • {ch_str} • {status} • {score_str}{year_str}"

            options.append(
                discord.SelectOption(
                    label=title[:100],
                    value=str(media_id),
                    description=desc[:100],
                )
            )

        self.select = discord.ui.Select(
            placeholder="Select a title to view details...",
            options=options,
            min_values=1,
            max_values=1,
        )
        self.select.callback = self.on_select
        self.add_item(self.select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user != self.author:
            await interaction.response.send_message(
                "This search menu isn't for you.", ephemeral=True
            )
            return False
        return True

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True
        if hasattr(self, "message"):
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    async def on_select(self, interaction: discord.Interaction):
        media_id = int(self.select.values[0])
        await interaction.response.defer(ephemeral=True)

        selected_preview = next((m for m in self.results if m.get("id") == media_id), None)

        media = await fetch_anilist_details(media_id)
        if media is None:
            if selected_preview:
                media = selected_preview
                media.setdefault("siteUrl", f"https://anilist.co/anime/{media_id}/" if self.media_type == "ANIME" else f"https://anilist.co/manga/{media_id}/")
            else:
                await interaction.followup.send("❌ Failed to fetch details.", ephemeral=True)
                return

        embed = build_anilist_embed(media, self.media_type, self.author)

        # Send the final result as a NEW public message in the channel
        try:
            await interaction.channel.send(embed=embed)
        except discord.HTTPException:
            await interaction.followup.send("❌ Failed to send result to channel.", ephemeral=True)
            return

        # Delete the ephemeral search message entirely
        self.stop()
        await interaction.delete_original_response()


class Anime(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="anime", description="Search for an anime on AniList.")
    @app_commands.describe(
        query="Anime title to search for",
        sort_by="How to sort the search results"
    )
    @app_commands.choices(sort_by=[
        app_commands.Choice(name="Best Match", value="SEARCH_MATCH"),
        app_commands.Choice(name="Popularity", value="POPULARITY_DESC"),
        app_commands.Choice(name="Highest Score", value="SCORE_DESC"),
        app_commands.Choice(name="Newest", value="START_DATE_DESC"),
    ])
    @app_commands.checks.cooldown(1, 10.0, key=lambda i: i.user.id)
    async def anime(
        self, 
        interaction: discord.Interaction, 
        query: str, 
        sort_by: app_commands.Choice[str] | None = None
    ):
        await interaction.response.defer(ephemeral=True)
        sort_val = sort_by.value if sort_by else "SEARCH_MATCH"
        
        results = await search_anilist(query, "ANIME", per_page=15, sort=sort_val)

        if results == "API_DISABLED":
            await interaction.followup.send(
                "❌ The AniList API is temporarily disabled! Check back later.",
                ephemeral=True,
            )
            return

        if not results:
            await interaction.followup.send(
                f"❌ Couldn't find any anime matching `{query}`.",
                ephemeral=True,
            )
            return

        if len(results) == 1:
            media = await fetch_anilist_details(results[0].get("id"))
            if media is None:
                media = results[0]
                media.setdefault("siteUrl", f"https://anilist.co/anime/{results[0].get('id')}/")
            embed = build_anilist_embed(media, "ANIME", interaction.user)
            # Send the final result as a NEW public message in the channel
            await interaction.channel.send(embed=embed)
            # Delete the ephemeral "Thinking..." interaction
            await interaction.delete_original_response()
            return

        view = AniListSelectView(results, "ANIME", interaction.user)
        search_embed = discord.Embed(
            title="<:search:1534195860123156582> Anime Search Results",
            description=f"Found **{len(results)}** results for `{query}`.\n"
                        f"Select the title you want to view from the dropdown below.",
            color=discord.Color(0x2E51A2),
        )
        search_embed.set_footer(text=f"Requested by {interaction.user} • Powered by AniList", icon_url=anilist_img)
        await interaction.followup.send(embed=search_embed, view=view, ephemeral=True)
        view.message = await interaction.original_response()

    @app_commands.command(name="manga", description="Search for a manga on AniList.")
    @app_commands.describe(
        query="Manga title to search for",
        sort_by="How to sort the search results"
    )
    @app_commands.choices(sort_by=[
        app_commands.Choice(name="Best Match", value="SEARCH_MATCH"),
        app_commands.Choice(name="Popularity", value="POPULARITY_DESC"),
        app_commands.Choice(name="Highest Score", value="SCORE_DESC"),
        app_commands.Choice(name="Newest", value="START_DATE_DESC"),
    ])
    @app_commands.checks.cooldown(1, 10.0, key=lambda i: i.user.id)
    async def manga(
        self, 
        interaction: discord.Interaction, 
        query: str,
        sort_by: app_commands.Choice[str] | None = None
    ):
        await interaction.response.defer(ephemeral=True)
        sort_val = sort_by.value if sort_by else "SEARCH_MATCH"
        
        results = await search_anilist(query, "MANGA", per_page=15, sort=sort_val)

        if results == "API_DISABLED":
            await interaction.followup.send(
                "❌ The AniList API is temporarily disabled! Check back later.",
                ephemeral=True,
            )
            return
        if not results:
            await interaction.followup.send(
                f"❌ Couldn't find any manga matching `{query}`.",
                ephemeral=True,
            )
            return

        if len(results) == 1:
            media = await fetch_anilist_details(results[0].get("id"))
            if media is None:
                media = results[0]
                media.setdefault("siteUrl", f"https://anilist.co/manga/{results[0].get('id')}/")
            embed = build_anilist_embed(media, "MANGA", interaction.user)
            # Send the final result as a NEW public message in the channel
            await interaction.channel.send(embed=embed)
            # Delete the ephemeral "Thinking..." interaction
            await interaction.delete_original_response()
            return

        view = AniListSelectView(results, "MANGA", interaction.user)
        search_embed = discord.Embed(
            title="<:search:1534195860123156582> Manga Search Results",
            description=f"Found **{len(results)}** results for `{query}`.\n"
                        f"Select the title you want to view from the dropdown below.",
            color=discord.Color(0x2E51A2),
        )
        search_embed.set_footer(text=f"Requested by {interaction.user} • Powered by AniList", icon_url=anilist_img)
        await interaction.followup.send(embed=search_embed, view=view, ephemeral=True)
        view.message = await interaction.original_response()


async def setup(bot: commands.Bot):
    await bot.add_cog(Anime(bot))