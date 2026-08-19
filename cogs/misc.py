from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands, tasks
from datetime import datetime, timedelta
import dateparser
import zoneinfo
import random
import re
import json
import logging
import asyncio
import aiohttp


# Default settings if user hasn't set a timezone
DEFAULT_DATEPARSER_SETTINGS = {
    'PREFER_DATES_FROM': 'future',
    'RETURN_AS_TIMEZONE_AWARE': True,
    'TO_TIMEZONE': 'UTC' # Always store the trigger time as UTC
}

MAX_REMINDERS = 15

# Discord timestamp format styles: (label, style_char)
# Empty style_char → raw Unix timestamp (no formatting)
TIMESTAMP_FORMATS: list[tuple[str, str]] = [
    ("Short Time",        "t"),  # 8:47 PM
    ("Long Time",         "T"),  # 8:47:00 PM
    ("Short Date",        "d"),  # 08/05/2026
    ("Long Date",         "D"),  # August 5th, 2026
    ("Short Date/Time",   "f"),  # August 5th, 2026 8:47 PM
    ("Long Date/Time",    "F"),  # Wednesday, August 5th, 2026 8:47 PM
    ("Relative Time",     "R"),  # 52 seconds ago
    ("Unix Timestamp",    ""),   # 1785955620 (raw, no formatting)
]
# ---- /convert data ----

WEIGHT_UNITS: dict[str, float] = {
    "mg": 0.001, "g": 1.0, "kg": 1000.0, "t": 1_000_000.0,
    "oz": 28.349523125, "lb": 453.59237, "st": 6350.29318,
}

LENGTH_UNITS: dict[str, float] = {
    "mm": 0.001, "cm": 0.01, "m": 1.0, "km": 1000.0,
    "in": 0.0254, "ft": 0.3048, "yd": 0.9144, "mi": 1609.344,
}

COMMON_CURRENCIES = [
    "USD", "EUR", "GBP", "JPY", "AUD", "CAD", "CHF", "CNY", "HKD", "NZD",
    "SEK", "KRW", "SGD", "NOK", "MXN", "INR", "BRL", "ZAR", "TRY", "PLN",
    "THB", "IDR", "HUF", "CZK", "ILS", "PHP", "MYR", "RON", "DKK", "AED",
    "RUB", "CNH", "TWD", "PKR", "BDT", "VND", "EGP", "SAR", "QAR", "KWD",
]

CURRENCY_ALIASES: dict[str, str] = {
    "YEN": "JPY", "YENS": "JPY", "JPYEN": "JPY",
    "YUAN": "CNY", "RMB": "CNY", "KUAI": "CNY",
    "QUID": "GBP", "POUND": "GBP", "POUNDS": "GBP", "STERLING": "GBP",
    "BUCK": "USD", "BUCKS": "USD", "DOLLAR": "USD", "DOLLARS": "USD", "GREENBACK": "USD",
    "EURO": "EUR", "EUROS": "EUR",
    "FRANC": "CHF", "FRANCS": "CHF",
    "KRONA": "SEK", "KRONOR": "SEK",
    "KRONE": "DKK", "KRONER": "DKK",
    "RAND": "ZAR", "RANDS": "ZAR",
    "RUBLE": "RUB", "ROUBLE": "RUB", "RUBLES": "RUB",
    "RUPEE": "INR", "RUPEES": "INR",
    "REAL": "BRL", "REAIS": "BRL",
    "PESO": "MXN", "PESOS": "MXN",
    "LIRA": "TRY", "LIRAS": "TRY",
    "FORINT": "HUF",
    "ZLOTY": "PLN",
    "BAHT": "THB",
    "RINGGIT": "MYR",
    "RUPIAH": "IDR",
    "DIRHAM": "AED",
    "RIYAL": "SAR",
    "WON": "KRW",
}

CURRENCY_NAMES: dict[str, str] = {
    "USD": "US Dollar", "EUR": "Euro", "GBP": "British Pound",
    "JPY": "Japanese Yen", "AUD": "Australian Dollar", "CAD": "Canadian Dollar",
    "CHF": "Swiss Franc", "CNY": "Chinese Yuan", "HKD": "Hong Kong Dollar",
    "NZD": "New Zealand Dollar", "SEK": "Swedish Krona", "KRW": "South Korean Won",
    "SGD": "Singapore Dollar", "NOK": "Norwegian Krone", "MXN": "Mexican Peso",
    "INR": "Indian Rupee", "BRL": "Brazilian Real", "ZAR": "South African Rand",
    "TRY": "Turkish Lira", "PLN": "Polish Zloty", "THB": "Thai Baht",
    "IDR": "Indonesian Rupiah", "HUF": "Hungarian Forint", "CZK": "Czech Koruna",
    "ILS": "Israeli Shekel", "PHP": "Philippine Peso", "MYR": "Malaysian Ringgit",
    "RON": "Romanian Leu", "DKK": "Danish Krone", "AED": "UAE Dirham",
    "RUB": "Russian Ruble", "CNH": "Chinese Yuan (Offshore)", "TWD": "Taiwan Dollar",
    "PKR": "Pakistani Rupee", "BDT": "Bangladeshi Taka", "VND": "Vietnamese Dong",
    "EGP": "Egyptian Pound", "SAR": "Saudi Riyal", "QAR": "Qatari Riyal",
    "KWD": "Kuwaiti Dinar",
}
CURRENCY_SYMBOLS: dict[str, str] = {
    "USD": "$", "EUR": "€", "GBP": "£", "JPY": "¥", "CNY": "¥",
    "AUD": "A$", "CAD": "C$", "HKD": "HK$", "NZD": "NZ$", "SGD": "S$",
    "CHF": "Fr", "SEK": "kr", "NOK": "kr", "DKK": "kr", "RON": "lei",
    "KRW": "₩", "INR": "₹", "BRL": "R$", "ZAR": "R", "TRY": "₺",
    "PLN": "zł", "THB": "฿", "IDR": "Rp", "HUF": "Ft", "CZK": "Kč",
    "ILS": "₪", "PHP": "₱", "MYR": "RM", "MXN": "$", "RUB": "₽",
    "TWD": "NT$", "PKR": "₨", "BDT": "৳", "VND": "₫", "EGP": "E£",
    "SAR": "﷼", "QAR": "﷼", "KWD": "د.ك", "AED": "د.إ",
}

WISE_QUOTE_URL = "https://api.wise.com/v2/quotes"

logger = logging.getLogger(__name__)


async def get_user_timezone(db, user_id: int) -> str | None:
    """Fetch a user's saved timezone from the DB."""
    async with db.execute("SELECT timezone FROM user_timezones WHERE user_id = ?", (user_id,)) as cursor:
        row = await cursor.fetchone()
        return row[0] if row else None

def get_dateparser_tz_string(tz_str: str) -> str:
    """
    Converts a stored timezone string to one dateparser understands.
    For UTC offsets like 'UTC-04:00', dateparser uses standard convention
    (NOT POSIX), so we pass it directly without inverting the sign.
    For IANA zones (e.g., 'America/New_York'), pass as-is.
    """
    # Convert 'UTC-04:00' to 'UTC-4' format which dateparser prefers
    match = re.match(r'^UTC([+-])(\d{2}):(\d{2})$', tz_str)
    if match:
        sign, hours, minutes = match.groups()
        hours = int(hours)
        minutes = int(minutes)
        if minutes > 0:
            return f"UTC{sign}{hours}:{minutes:02d}"
        return f"UTC{sign}{hours}"
    return tz_str

# ============================================================
#  PAGINATION VIEW
# ============================================================
class LayoutPagination(discord.ui.View):
    def __init__(self, pages: list[str], author: discord.abc.User, snapshot_id: int, created_dt: datetime):
        super().__init__(timeout=120)
        self.pages = pages
        self.author = author
        self.snapshot_id = snapshot_id
        self.created_dt = created_dt
        self.page = 0
        self.max_page = len(pages) - 1
        self.message: discord.Message | None = None

    def create_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title=f"Channel Snapshot #{self.snapshot_id}",
            description=self.pages[self.page],
            color=discord.Color.pink(),
            timestamp=self.created_dt
        )
        embed.set_footer(text=f"Page {self.page + 1}/{self.max_page + 1} • Captured on")
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


class ComparePagination(discord.ui.View):
    def __init__(self, embeds: list[discord.Embed], author: discord.abc.User):
        super().__init__(timeout=120)
        self.embeds = embeds
        self.author = author
        self.page = 0
        self.max_page = len(embeds) - 1
        self.message: discord.Message | None = None

    def create_embed(self) -> discord.Embed:
        embed = self.embeds[self.page]
        embed.set_footer(text=f"Page {self.page + 1}/{self.max_page + 1} • Comparing Snapshots")
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
class SnapshotPagination(discord.ui.View):
    def __init__(self, snapshots: list[tuple[int, int]], author: discord.abc.User):
        super().__init__(timeout=120)
        self.snapshots = snapshots  # list of (snapshot_id, created_at_ts)
        self.author = author
        self.page = 0
        self.per_page = 10
        self.max_page = max((len(snapshots) - 1) // self.per_page, 0)
        self.message: discord.Message | None = None

    def create_embed(self) -> discord.Embed:
        start = self.page * self.per_page
        end = start + self.per_page
        entries = self.snapshots[start:end]

        embed = discord.Embed(
            title="<:note:1534195886501134376> Channel Snapshots",
            color=discord.Color.pink(),
            timestamp=discord.utils.utcnow()
        )

        if not entries:
            embed.description = "No snapshots on this page."
        else:
            description_lines = []
            for snap_id, created_ts in entries:
                description_lines.append(f"**#{snap_id}** • <t:{created_ts}:F> (<t:{created_ts}:R>)")
            
            embed.description = "\n".join(description_lines)

        embed.set_footer(text=f"Page {self.page + 1}/{self.max_page + 1} • Total: {len(self.snapshots)}")
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

class ReminderPagination(discord.ui.View):
    def __init__(self, reminders: list, author: discord.abc.User):
        super().__init__(timeout=120)
        self.reminders = reminders
        self.author = author
        self.page = 0
        self.per_page = 5
        self.max_page = max((len(reminders) - 1) // self.per_page, 0)
        self.message: discord.Message | None = None

    def create_embed(self) -> discord.Embed:
        start = self.page * self.per_page
        end = start + self.per_page
        entries = self.reminders[start:end]

        embed = discord.Embed(
            title="⏰ Your Reminders",
            color=discord.Color.pink(),
            timestamp=discord.utils.utcnow()
        )

        if not entries:
            embed.description = "No reminders on this page."
        else:
            description_lines = []
            for rem_id, trigger_ts, msg, repeat in entries:
                repeat_str = f" 🔁 (every {timedelta(seconds=repeat)})" if repeat else ""
                short_msg = msg if len(msg) <= 50 else msg[:47] + "..."
                description_lines.append(f"**#{rem_id}** • <t:{trigger_ts}:F>{repeat_str}\n> {short_msg}")
            
            embed.description = "\n\n".join(description_lines)

        embed.set_footer(text=f"Page {self.page + 1}/{self.max_page + 1} • Total: {len(self.reminders)}")
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


# ============================================================
#  COG
# ============================================================

class Misc(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.check_reminders.start()
        self._startup_snapshot_task: asyncio.Task | None = None

    async def cog_load(self) -> None:
        """On startup, auto-capture snapshots for all guilds if layout changed."""
        self._startup_snapshot_task = asyncio.create_task(self._capture_startup_snapshots())

    def cog_unload(self):
        self.check_reminders.cancel()
        if self._startup_snapshot_task and not self._startup_snapshot_task.done():
            self._startup_snapshot_task.cancel()

    async def _capture_startup_snapshots(self) -> None:
        """One-shot: capture a snapshot for each guild on bot start,
        but only if no snapshot exists or the layout differs from the latest.
        Runtime layout changes are handled by MemberEvents._trigger_snapshot_buffer."""
        await self.bot.wait_until_ready()
        for guild in self.bot.guilds:
            try:
                await self._capture_if_changed(guild)
            except Exception as e:
                logger.error(f"[Misc] Startup snapshot error for guild {guild.id} ({guild.name}): {e}")

    def _capture_guild_layout(self, guild: discord.Guild) -> dict:
        """Capture the current channel layout of a guild as a JSON-serializable dict."""
        categories = []
        for category in guild.categories:
            channels = []
            for ch in category.channels:
                ch_type = "voice" if isinstance(ch, (discord.VoiceChannel, discord.StageChannel)) else "text"
                channels.append({"name": ch.name, "type": ch_type})
            categories.append({"name": category.name, "channels": channels})

        uncategorized = []
        for ch in guild.channels:
            if isinstance(ch, discord.CategoryChannel):
                continue
            if ch.category is None:
                ch_type = "voice" if isinstance(ch, (discord.VoiceChannel, discord.StageChannel)) else "text"
                uncategorized.append({"name": ch.name, "type": ch_type})

        return {"categories": categories, "uncategorized": uncategorized}

    async def _capture_if_changed(self, guild: discord.Guild) -> None:
        """Save a new snapshot only if no snapshot exists or the layout differs."""
        current_data = self._capture_guild_layout(guild)
        current_json = json.dumps(current_data, sort_keys=True)

        async with self.bot.db.execute(
            "SELECT snapshot_data FROM channel_snapshots "
            "WHERE guild_id = ? ORDER BY created_at DESC LIMIT 1",
            (guild.id,),
        ) as cursor:
            row = await cursor.fetchone()

        if row is not None:
            existing_json = json.dumps(json.loads(row[0]), sort_keys=True)
            if current_json == existing_json:
                logger.info(f"[Misc] Guild {guild.id} ({guild.name}) layout unchanged — skipping snapshot.")
                return

        now_ts = int(discord.utils.utcnow().timestamp())
        await self.bot.db.execute(
            "INSERT INTO channel_snapshots (guild_id, snapshot_data, created_at) VALUES (?, ?, ?)",
            (guild.id, current_json, now_ts),
        )
        await self.bot.db.commit()
        logger.info(f"[Misc] Captured new snapshot for guild {guild.id} ({guild.name}).")

    @tasks.loop(seconds=15.0)
    async def check_reminders(self):
        """Background task to fetch and dispatch due reminders."""
        now_ts = int(discord.utils.utcnow().timestamp())

        try:
            async with self.bot.db.execute(
                "SELECT id, user_id, channel_id, message, repeat_interval "
                "FROM reminders WHERE trigger_time <= ?",
                (now_ts,),
            ) as cursor:
                due_reminders = await cursor.fetchall()
        except Exception as e:
            logger.error(f"[Misc] Error fetching reminders: {e}")
            return

        for row in due_reminders:
            rem_id, user_id, channel_id, message, repeat_interval = row

            if repeat_interval and repeat_interval > 0:
                next_trigger = now_ts + repeat_interval
                await self.bot.db.execute(
                    "UPDATE reminders SET trigger_time = ? WHERE id = ?",
                    (next_trigger, rem_id),
                )
                await self.bot.db.commit()
            else:
                await self.bot.db.execute("DELETE FROM reminders WHERE id = ?", (rem_id,))
                await self.bot.db.commit()

            embed = discord.Embed(
                title="Reminder",
                description=message,
                color=discord.Color.pink(),
                timestamp=discord.utils.utcnow()
            )
            embed.set_thumbnail(url="https://cdn.discordapp.com/emojis/1534195779810365530.webp")
            sent = False
            try:
                user = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
                if user:
                    await user.send(embed=embed)
                    sent = True
            except (discord.Forbidden, discord.HTTPException):
                pass

            if not sent and channel_id:
                try:
                    channel = self.bot.get_channel(channel_id)
                    if channel:
                        await channel.send(content=f"<@{user_id}>", embed=embed)
                        sent = True
                except (discord.Forbidden, discord.HTTPException, discord.NotFound):
                    pass

    @check_reminders.before_loop
    async def before_check_reminders(self):
        await self.bot.wait_until_ready()

    # ============================================================
    #  TIMEZONE & TIME COMMANDS
    # ============================================================

    # A comprehensive map of countries to their primary IANA timezones.
    COUNTRY_TIMEZONE_MAP = {
        "Afghanistan": "Asia/Kabul", "Albania": "Europe/Tirane", "Algeria": "Africa/Algiers",
        "Andorra": "Europe/Andorra", "Angola": "Africa/Luanda", "Argentina": "America/Argentina/Buenos_Aires",
        "Armenia": "Asia/Yerevan", "Australia (Sydney)": "Australia/Sydney", "Australia (Perth)": "Australia/Perth",
        "Austria": "Europe/Vienna", "Azerbaijan": "Asia/Baku", "Bahamas": "America/Nassau",
        "Bahrain": "Asia/Bahrain", "Bangladesh": "Asia/Dhaka", "Belarus": "Europe/Minsk",
        "Belgium": "Europe/Brussels", "Belize": "America/Belize", "Benin": "Africa/Porto-Novo",
        "Bhutan": "Asia/Thimphu", "Bolivia": "America/La_Paz", "Bosnia and Herzegovina": "Europe/Sarajevo",
        "Botswana": "Africa/Gaborone", "Brazil (Brasilia)": "America/Sao_Paulo", "Brazil (Manaus)": "America/Manaus",
        "Brunei": "Asia/Brunei", "Bulgaria": "Europe/Sofia", "Burkina Faso": "Africa/Ouagadougou",
        "Burundi": "Africa/Bujumbura", "Cambodia": "Asia/Phnom_Penh", "Cameroon": "Africa/Douala",
        "Canada (Eastern)": "America/Toronto", "Canada (Central)": "America/Winnipeg", "Canada (Mountain)": "America/Edmonton",
        "Canada (Pacific)": "America/Vancouver", "Canada (Newfoundland)": "America/St_Johns", "Cape Verde": "Atlantic/Cape_Verde",
        "Central African Republic": "Africa/Bangui", "Chad": "Africa/Ndjamena", "Chile": "America/Santiago",
        "China": "Asia/Shanghai", "Colombia": "America/Bogota", "Comoros": "Indian/Comoro",
        "Congo (Brazzaville)": "Africa/Brazzaville", "Congo (Kinshasa)": "Africa/Kinshasa", "Costa Rica": "America/Costa_Rica",
        "Croatia": "Europe/Zagreb", "Cuba": "America/Havana", "Cyprus": "Asia/Nicosia",
        "Czech Republic": "Europe/Prague", "Denmark": "Europe/Copenhagen", "Djibouti": "Africa/Djibouti",
        "Dominican Republic": "America/Santo_Domingo", "Ecuador": "America/Guayaquil", "Egypt": "Africa/Cairo",
        "El Salvador": "America/El_Salvador", "Equatorial Guinea": "Africa/Malabo", "Eritrea": "Africa/Asmara",
        "Estonia": "Europe/Tallinn", "Eswatini": "Africa/Mbabane", "Ethiopia": "Africa/Addis_Ababa",
        "Fiji": "Pacific/Fiji", "Finland": "Europe/Helsinki", "France": "Europe/Paris",
        "Gabon": "Africa/Libreville", "Gambia": "Africa/Banjul", "Georgia": "Asia/Tbilisi",
        "Germany": "Europe/Berlin", "Ghana": "Africa/Accra", "Greece": "Europe/Athens",
        "Guatemala": "America/Guatemala", "Guinea": "Africa/Conakry", "Guyana": "America/Guyana",
        "Haiti": "America/Port-au-Prince", "Honduras": "America/Tegucigalpa", "Hong Kong": "Asia/Hong_Kong",
        "Hungary": "Europe/Budapest", "Iceland": "Atlantic/Reykjavik", "India": "Asia/Kolkata",
        "Indonesia (Jakarta)": "Asia/Jakarta", "Indonesia (Makassar)": "Asia/Makassar", "Iran": "Asia/Tehran",
        "Iraq": "Asia/Baghdad", "Ireland": "Europe/Dublin", "Israel": "Asia/Jerusalem",
        "Italy": "Europe/Rome", "Ivory Coast": "Africa/Abidjan", "Jamaica": "America/Jamaica",
        "Japan": "Asia/Tokyo", "Jordan": "Asia/Amman", "Kazakhstan": "Asia/Almaty",
        "Kenya": "Africa/Nairobi", "Kuwait": "Asia/Kuwait", "Kyrgyzstan": "Asia/Bishkek",
        "Laos": "Asia/Vientiane", "Latvia": "Europe/Riga", "Lebanon": "Asia/Beirut",
        "Lesotho": "Africa/Maseru", "Liberia": "Africa/Monrovia", "Libya": "Africa/Tripoli",
        "Liechtenstein": "Europe/Vaduz", "Lithuania": "Europe/Vilnius", "Luxembourg": "Europe/Luxembourg",
        "Macau": "Asia/Macau", "Madagascar": "Indian/Antananarivo", "Malawi": "Africa/Blantyre",
        "Malaysia": "Asia/Kuala_Lumpur", "Maldives": "Indian/Maldives", "Mali": "Africa/Bamako",
        "Malta": "Europe/Malta", "Mauritania": "Africa/Nouakchott", "Mauritius": "Indian/Mauritius",
        "Mexico (City)": "America/Mexico_City", "Mexico (Tijuana)": "America/Tijuana", "Moldova": "Europe/Chisinau",
        "Monaco": "Europe/Monaco", "Mongolia": "Asia/Ulaanbaatar", "Morocco": "Africa/Casablanca",
        "Mozambique": "Africa/Maputo", "Myanmar": "Asia/Yangon", "Namibia": "Africa/Windhoek",
        "Nepal": "Asia/Kathmandu", "Netherlands": "Europe/Amsterdam", "New Zealand": "Pacific/Auckland",
        "Nicaragua": "America/Managua", "Niger": "Africa/Niamey", "Nigeria": "Africa/Lagos",
        "North Korea": "Asia/Pyongyang", "North Macedonia": "Europe/Skopje", "Norway": "Europe/Oslo",
        "Oman": "Asia/Muscat", "Pakistan": "Asia/Karachi", "Palestine": "Asia/Gaza",
        "Panama": "America/Panama", "Papua New Guinea": "Pacific/Port_Moresby", "Paraguay": "America/Asuncion",
        "Peru": "America/Lima", "Philippines": "Asia/Manila", "Poland": "Europe/Warsaw",
        "Portugal": "Europe/Lisbon", "Qatar": "Asia/Qatar", "Romania": "Europe/Bucharest",
        "Russia (Moscow)": "Europe/Moscow", "Russia (Vladivostok)": "Asia/Vladivostok", "Rwanda": "Africa/Kigali",
        "Saudi Arabia": "Asia/Riyadh", "Senegal": "Africa/Dakar", "Serbia": "Europe/Belgrade",
        "Sierra Leone": "Africa/Freetown", "Singapore": "Asia/Singapore", "Slovakia": "Europe/Bratislava",
        "Slovenia": "Europe/Ljubljana", "Somalia": "Africa/Mogadishu", "South Africa": "Africa/Johannesburg",
        "South Korea": "Asia/Seoul", "South Sudan": "Africa/Juba", "Spain": "Europe/Madrid",
        "Sri Lanka": "Asia/Colombo", "Sudan": "Africa/Khartoum", "Suriname": "America/Paramaribo",
        "Sweden": "Europe/Stockholm", "Switzerland": "Europe/Zurich", "Syria": "Asia/Damascus",
        "Taiwan": "Asia/Taipei", "Tajikistan": "Asia/Dushanbe", "Tanzania": "Africa/Dar_es_Salaam",
        "Thailand": "Asia/Bangkok", "Togo": "Africa/Lome", "Trinidad and Tobago": "America/Port_of_Spain",
        "Tunisia": "Africa/Tunis", "Turkey": "Europe/Istanbul", "Turkmenistan": "Asia/Ashgabat",
        "Uganda": "Africa/Kampala", "Ukraine": "Europe/Kyiv", "United Arab Emirates": "Asia/Dubai",
        "United Kingdom": "Europe/London", "United States (Eastern)": "America/New_York", "United States (Central)": "America/Chicago",
        "United States (Mountain)": "America/Denver", "United States (Pacific)": "America/Los_Angeles", "United States (Alaska)": "America/Anchorage",
        "United States (Hawaii)": "Pacific/Honolulu", "Uruguay": "America/Montevideo", "Uzbekistan": "Asia/Tashkent",
        "Venezuela": "America/Caracas", "Vietnam": "Asia/Ho_Chi_Minh", "Yemen": "Asia/Aden",
        "Zambia": "Africa/Lusaka", "Zimbabwe": "Africa/Harare"
    }

    async def timezone_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """Autocomplete function for /setmytime."""
        # 1. Search the country map first (case-insensitive)
        country_matches = [
            app_commands.Choice(name=f"{name} ({tz})", value=tz)
            for name, tz in self.COUNTRY_TIMEZONE_MAP.items()
            if current.lower() in name.lower() or current.lower() in tz.lower()
        ]

        # If we have 25 matches, return them (Discord limit)
        if len(country_matches) >= 25:
            return country_matches[:25]

        # 2. Search the full IANA database for any remaining matches (e.g., specific cities)
        all_tz = zoneinfo.available_timezones()
        iana_matches = [
            app_commands.Choice(name=tz, value=tz)
            for tz in all_tz
            if current.lower() in tz.lower() and not tz.startswith("Etc/GMT")
        ]
        # Combine and return up to 25 unique results
        seen = {choice.value for choice in country_matches}
        for choice in iana_matches:
            if choice.value not in seen:
                country_matches.append(choice)
                seen.add(choice.value)
            if len(country_matches) >= 25:
                break

        return country_matches[:25]

    @app_commands.command(name="setmytime", description="Set your timezone for reminder parsing. Use 'none' to remove.")
    @app_commands.describe(timezone="Your timezone (start typing your country/city to see options)")
    @app_commands.autocomplete(timezone=timezone_autocomplete)
    async def setmytime(self, interaction: discord.Interaction, timezone: str):
        # Handle "none" to remove timezone
        if timezone.lower() == "none":
            await self.bot.db.execute("DELETE FROM user_timezones WHERE user_id = ?", (interaction.user.id,))
            await self.bot.db.commit()
            await interaction.response.send_message(
                "✅ Your timezone has been removed. Times will now be parsed using the bot's local time.",
                ephemeral=True
            )
            return
            
        # Normalize manual UTC/GMT offsets to standard UTC-05:00 format.
        # Also catch Etc/GMT zones which use inverted POSIX signs.
        match = re.match(r'^(?:Etc/GMT|UTC|GMT)\s*([+-])\s*(\d{1,2})(?::?(\d{2}))?$', timezone, re.IGNORECASE)
        if match:
            sign, hours, minutes = match.groups()
            hours = int(hours)
            minutes = int(minutes) if minutes else 0
            
            # Etc/GMT uses POSIX convention (inverted signs), so flip it
            if timezone.upper().startswith("ETC/GMT"):
                sign = '-' if sign == '+' else '+'
            
            tz_clean = f"UTC{sign}{hours:02d}:{minutes:02d}"
        else:
            tz_clean = timezone.split(" ")[0] if " " in timezone else timezone
        
        # Use the inverted string for dateparser validation
        test_settings = DEFAULT_DATEPARSER_SETTINGS.copy()
        test_settings['TIMEZONE'] = get_dateparser_tz_string(tz_clean)
        
        try:
            test_dt = dateparser.parse('12:00pm', settings=test_settings)
            if not test_dt:
                raise ValueError("Invalid timezone")
        except Exception:
            await interaction.response.send_message(
                f"❌ Could not understand timezone `{timezone}`.\n"
                "Try formats like `EST`, `PST`, `America/Chicago`, or `UTC+2`.",
                ephemeral=True
            )
            return

        # Save to DB
        await self.bot.db.execute(
            "INSERT INTO user_timezones (user_id, timezone) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET timezone = ?",
            (interaction.user.id, tz_clean, tz_clean)
        )
        await self.bot.db.commit()

        await interaction.response.send_message(
            f"✅ Your timezone has been set to `{tz_clean}`.\n"
            "Times you enter for reminders will now be interpreted in this timezone.",
            ephemeral=True
        )

    @app_commands.command(name="timenow", description="Check the current time for a user based on their set timezone.")
    @app_commands.describe(user="Whos time you wanna check")
    async def timenow(self, interaction: discord.Interaction, user: discord.User):
        target_tz = await get_user_timezone(self.bot.db, user.id)
        
        if not target_tz:
            await interaction.response.send_message(
                f"❌ {user.mention} hasn't set up their timezone yet using `/setmytime`.",
                ephemeral=True
            )
            return
            
        try:
            # Attempt to load via standard zoneinfo (handles IANA like America/New_York)
            try:
                tz_info = zoneinfo.ZoneInfo(target_tz)
            except Exception:
                # Fallback for UTC offset formats like UTC-05:00
                from dateutil import tz as dateutil_tz
                tz_info = dateutil_tz.gettz(target_tz)
                if not tz_info:
                    raise ValueError("Invalid timezone")
                
            target_current_time = datetime.now(tz_info)
            formatted_time = target_current_time.strftime("%I:%M %p %Z (%B %d, %Y)")
            
            description = f"It is currently **{formatted_time}** for {user.mention}."
            
            # Check author's timezone to calculate the difference
            author_tz = await get_user_timezone(self.bot.db, interaction.user.id)
            
            if author_tz:
                if author_tz == target_tz:
                    description += "\n\nThey are in the **same timezone** as you."
                else:
                    # Load author's timezone with fallback too
                    try:
                        author_tz_info = zoneinfo.ZoneInfo(author_tz)
                    except Exception:
                        from dateutil import tz as dateutil_tz
                        author_tz_info = dateutil_tz.gettz(author_tz)
                        
                    author_offset = datetime.now(author_tz_info).utcoffset()
                    target_offset = target_current_time.utcoffset()
                    
                    if author_offset is not None and target_offset is not None:
                        diff = target_offset - author_offset
                        diff_hours = diff.total_seconds() / 3600
                        
                        if diff_hours > 0:
                            hours_str = f"{diff_hours:g}" # removes trailing .0 for whole numbers
                            description += f"\n\nThey are **{hours_str} hours ahead** of you."
                        elif diff_hours < 0:
                            hours_str = f"{abs(diff_hours):g}"
                            description += f"\n\nThey are **{hours_str} hours behind** you."
                        else:
                            description += "\n\nThey are in the **same timezone** as you."
            else:
                description += "\n\n💡 *Set your own timezone with `/setmytime` to see the time difference.*"
                
            embed = discord.Embed(
                title=f"<:alarm:1534195779810365530> Time for {user.name}",
                description=description,
                color=discord.Color.pink()
            )
            await interaction.response.send_message(embed=embed)
            
        except Exception as e:
            logger.error(f"[Misc] Error in timenow: {e}")
            await interaction.response.send_message(
                f"❌ An error occurred while fetching the time for {user.mention}.",
                ephemeral=True
            )
    @app_commands.command(name="time", description="Generate Discord timestamp syntax (like hammertime.cyou) for any date/time.")
    @app_commands.describe(
        when="When? (e.g., 'tomorrow 3pm', 'Dec 25 14:30', 'in 2 hours', '2025-12-25 18:00')",
        timezone="Timezone to interpret the time in (defaults to your saved timezone)",
    )
    @app_commands.autocomplete(timezone=timezone_autocomplete)
    async def time_cmd(
        self,
        interaction: discord.Interaction,
        when: str,
        timezone: str | None = None,
    ):
        # Resolve timezone: explicit parameter > user's saved > none
        user_tz = None
        if timezone is None:
            user_tz = await get_user_timezone(self.bot.db, interaction.user.id)
        else:
            # Normalize manual UTC/GMT offsets (same logic as /setmytime)
            match = re.match(
                r'^(?:Etc/GMT|UTC|GMT)\s*([+-])\s*(\d{1,2})(?::?(\d{2}))?$',
                timezone, re.IGNORECASE,
            )
            if match:
                sign, hours, minutes = match.groups()
                hours = int(hours)
                minutes = int(minutes) if minutes else 0
                if timezone.upper().startswith("ETC/GMT"):
                    sign = '-' if sign == '+' else '+'
                user_tz = f"UTC{sign}{hours:02d}:{minutes:02d}"
            else:
                user_tz = timezone.split(" ")[0] if " " in timezone else timezone

        # Parse the date/time with dateparser, applying the resolved timezone
        parse_settings = DEFAULT_DATEPARSER_SETTINGS.copy()
        if user_tz:
            parse_settings['TIMEZONE'] = get_dateparser_tz_string(user_tz)

        parsed_dt = dateparser.parse(when, settings=parse_settings)
        if not parsed_dt:
            hint = (
                f" (timezone: `{user_tz}`)"
                if user_tz
                else "\n💡 *Tip: Use `/setmytime` to set your timezone for better parsing.*"
            )
            await interaction.response.send_message(
                f"❌ Could not understand `{when}`.{hint}\n"
                "Try formats like `tomorrow at 3pm`, `Dec 25 2:30pm`, `in 2 hours`, or `2025-12-25 18:00`.",
                ephemeral=True,
            )
            return

        ts = int(parsed_dt.timestamp())

        # Build embed with all chat-syntax variants for the user to copy
        tz_label = user_tz if user_tz else "UTC (default)"
        embed = discord.Embed(
            title="<:alarm:1534195779810365530> Timestamp Generated",
            description=(
                f"**Parsed as:** <t:{ts}:F> (<t:{ts}:R>)\n"
                f"**Timezone:** `{tz_label}`\n\n"
                f"Copy any format below and paste it into a message — Discord will render it for everyone in their own local time."
            ),
            color=discord.Color.pink(),
            timestamp=discord.utils.utcnow(),
        )

        for name, fmt in TIMESTAMP_FORMATS:
            if fmt:
                syntax = f"`<t:{ts}:{fmt}>`"
                preview = f"<t:{ts}:{fmt}>"
            else:
                # Raw Unix timestamp
                syntax = f"`{ts}`"
                preview = f"`{ts}` (raw unix — paste without backticks)"
            embed.add_field(name=name, value=f"{syntax}  →  {preview}", inline=False)

        embed.set_footer(text=f"Unix: {ts}")
        await interaction.response.send_message(embed=embed, ephemeral=True)
    # ============================================================
    #  REMINDER COMMANDS
    # ============================================================

    reminder = app_commands.Group(name="reminder", description="Manage your reminders")

    @reminder.command(name="set", description="Set a new reminder.")
    @app_commands.describe(
        about="About what ?",
        when="When to remind you (e.g., 'in 2 hours', 'tomorrow at 3pm', 'soon')",
        repeat="Optional: Repeat interval (e.g., 'every day', 'every 2 hours')"
    )
    async def reminder_set(
        self,
        interaction: discord.Interaction,
        about: str,
        when: str,
        repeat: str | None = None
    ):
        await interaction.response.defer(ephemeral=True)

        # Spam prevention
        async with self.bot.db.execute(
            "SELECT COUNT(*) FROM reminders WHERE user_id = ?", 
            (interaction.user.id,)
        ) as cursor:
            count = (await cursor.fetchone())[0]
            
        if count >= MAX_REMINDERS:
            await interaction.followup.send(
                f"❌ You already have {MAX_REMINDERS} active reminders. Please cancel some before adding new ones.",
                ephemeral=True
            )
            return

        now = discord.utils.utcnow()
        trigger_dt = None

        # Special case for "soon"
        if when.lower() == "soon":
            minutes = random.randint(5, 60)
            trigger_dt = now + timedelta(minutes=minutes)
        else:
            # Get user's timezone settings
            user_tz = await get_user_timezone(self.bot.db, interaction.user.id)
            parse_settings = DEFAULT_DATEPARSER_SETTINGS.copy()
            if user_tz:
                parse_settings['TIMEZONE'] = get_dateparser_tz_string(user_tz)

            # Parse the trigger time
            trigger_dt = dateparser.parse(when, settings=parse_settings)
            if not trigger_dt:
                tz_hint = f" (Make sure it matches your timezone: `{user_tz}`)" if user_tz else "\n💡 *Tip: Use `/setmytime` to set your timezone for better accuracy.*"
                await interaction.followup.send(
                    f"❌ Could not understand the time `{when}`.{tz_hint}\n"
                    "Try formats like `in 2 hours`, `tomorrow at 3pm`, or `soon`.",
                    ephemeral=True,
                )
                return

        if trigger_dt <= now:
            await interaction.followup.send(
                "❌ That time is in the past. Please provide a future time.",
                ephemeral=True,
            )
            return

        trigger_ts = int(trigger_dt.timestamp())

        # Parse repeat interval if provided
        repeat_seconds = None
        if repeat:
            # Use user's timezone for repeat parsing too
            user_tz = await get_user_timezone(self.bot.db, interaction.user.id)
            parse_settings = DEFAULT_DATEPARSER_SETTINGS.copy()
            if user_tz:
                parse_settings['TIMEZONE'] = get_dateparser_tz_string(user_tz)

            parsed_repeat = dateparser.parse(repeat, settings=parse_settings)
            if not parsed_repeat:
                await interaction.followup.send(
                    f"❌ Could not understand the repeat interval `{repeat}`.",
                    ephemeral=True,
                )
                return
            
            repeat_seconds = int((parsed_repeat - now).total_seconds())
            if repeat_seconds <= 0:
                await interaction.followup.send(
                    "❌ Repeat interval must be a positive duration (e.g., 'every 1 day').",
                    ephemeral=True,
                )
                return

        # Store in DB
        channel_id = interaction.channel_id if interaction.guild else None
        guild_id = interaction.guild_id

        cursor = await self.bot.db.execute(
            "INSERT INTO reminders (user_id, channel_id, guild_id, trigger_time, message, repeat_interval, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (interaction.user.id, channel_id, guild_id, trigger_ts, about, repeat_seconds, int(now.timestamp())),
        )
        await self.bot.db.commit()
        rem_id = cursor.lastrowid

        # Build confirmation embed
        confirm_embed = discord.Embed(
            title=f"Reminder Set #{rem_id}",
            description=f"Ok **{interaction.user.name}**, I will remind you in <t:{trigger_ts}:R> about:\n\n `{about}`",
            color=discord.Color.green(),
            timestamp=discord.utils.utcnow()
        )
        confirm_embed.set_thumbnail(url="https://cdn.discordapp.com/emojis/1534195779810365530.webp")
        if repeat_seconds:
            confirm_embed.add_field(name="Repeating", value=f"Every {timedelta(seconds=repeat_seconds)}", inline=False)
        
        footer_text = f"Reminder ID: {rem_id}"
        # Add hint to footer if they haven't set their timezone
        if when.lower() != "soon":
            user_tz_check = await get_user_timezone(self.bot.db, interaction.user.id)
            if not user_tz_check:
                footer_text += " • ℹ️ Use /setmytime for better accuracy"
        confirm_embed.set_footer(text=footer_text)

        # Try to DM the user to confirm DMs work
        dm_sent = False
        try:
            await interaction.user.send(embed=confirm_embed)
            dm_sent = True
        except (discord.Forbidden, discord.HTTPException):
            pass

        if dm_sent:
            response_msg = "Reminder set! I've sent a confirmation to your DMs."
            if when.lower() != "soon":
                user_tz_check = await get_user_timezone(self.bot.db, interaction.user.id)
                if not user_tz_check:
                    response_msg += "\n\nℹ️ **Tip:** I noticed you haven't set your timezone yet. Use `/setmytime` to ensure times like `3pm` are parsed correctly in the future."
            await interaction.followup.send(response_msg, ephemeral=True)
        else:
            if interaction.guild:
                await interaction.channel.send(embed=confirm_embed)
                warning_msg = "⚠️ I couldn't DM you, so I posted the confirmation here. Please enable DMs from server members to receive your reminder privately."
                if when.lower() != "soon":
                    user_tz_check = await get_user_timezone(self.bot.db, interaction.user.id)
                    if not user_tz_check:
                        warning_msg += "\n\nℹ️ **Tip:** Use `/setmytime` to set your timezone for better time parsing accuracy."
                await interaction.followup.send(warning_msg, ephemeral=True)
            else:
                await interaction.followup.send("❌ I couldn't send you a DM. Please check your privacy settings.", ephemeral=True)
    @reminder.command(name="list", description="View your active reminders.")
    async def reminder_list(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        
        async with self.bot.db.execute(
            "SELECT id, trigger_time, message, repeat_interval FROM reminders "
            "WHERE user_id = ? ORDER BY trigger_time ASC",
            (interaction.user.id,),
        ) as cursor:
            reminders = await cursor.fetchall()

        if not reminders:
            await interaction.followup.send("You have no active reminders.", ephemeral=True)
            return

        view = ReminderPagination(reminders, interaction.user)
        embed = view.create_embed()
        
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        view.message = await interaction.original_response()

    @reminder.command(name="cancel", description="Cancel a reminder by its ID.")
    @app_commands.describe(reminder_id="The ID of the reminder to cancel")
    async def reminder_cancel(self, interaction: discord.Interaction, reminder_id: int):
        async with self.bot.db.execute(
            "SELECT id FROM reminders WHERE id = ? AND user_id = ?",
            (reminder_id, interaction.user.id),
        ) as cursor:
            if not await cursor.fetchone():
                await interaction.response.send_message(
                    "❌ Reminder not found or doesn't belong to you.",
                    ephemeral=True,
                )
                return

        await self.bot.db.execute("DELETE FROM reminders WHERE id = ?", (reminder_id,))
        await self.bot.db.commit()

        await interaction.response.send_message(
            f"✅ Reminder #{reminder_id} has been canceled.",
            ephemeral=True,
        )

    @reminder.command(name="edit", description="Edit the message of an existing reminder.")
    @app_commands.describe(
        reminder_id="The ID of the reminder to edit",
        new_message="The new message for this reminder"
    )
    async def reminder_edit(self, interaction: discord.Interaction, reminder_id: int, new_message: str):
        async with self.bot.db.execute(
            "SELECT id FROM reminders WHERE id = ? AND user_id = ?",
            (reminder_id, interaction.user.id),
        ) as cursor:
            if not await cursor.fetchone():
                await interaction.response.send_message(
                    "❌ Reminder not found or doesn't belong to you.",
                    ephemeral=True,
                )
                return

        await self.bot.db.execute(
            "UPDATE reminders SET message = ? WHERE id = ?",
            (new_message, reminder_id),
        )
        await self.bot.db.commit()

        await interaction.response.send_message(
            f"✅ Reminder #{reminder_id} message updated.",
            ephemeral=True,
        )

    @reminder.command(name="repeat", description="Set or update a repeating interval for a reminder.")
    @app_commands.describe(
        reminder_id="The ID of the reminder to modify",
        interval="Repeat interval (e.g., 'every day', 'every 2 hours'), or 'none' to disable"
    )
    async def reminder_repeat(self, interaction: discord.Interaction, reminder_id: int, interval: str):
        async with self.bot.db.execute(
            "SELECT trigger_time FROM reminders WHERE id = ? AND user_id = ?",
            (reminder_id, interaction.user.id),
        ) as cursor:
            if not await cursor.fetchone():
                await interaction.response.send_message(
                    "❌ Reminder not found or doesn't belong to you.",
                    ephemeral=True,
                )
                return

        if interval.lower() in ("none", "off", "disable", "stop"):
            await self.bot.db.execute(
                "UPDATE reminders SET repeat_interval = NULL WHERE id = ?",
                (reminder_id,),
            )
            await self.bot.db.commit()
            await interaction.response.send_message(
                f"✅ Repeating disabled for reminder #{reminder_id}.",
                ephemeral=True,
            )
            return

        now = discord.utils.utcnow()
        # Use user's timezone for repeat parsing too
        user_tz = await get_user_timezone(self.bot.db, interaction.user.id)
        parse_settings = DEFAULT_DATEPARSER_SETTINGS.copy()
        if user_tz:
            parse_settings['TIMEZONE'] = get_dateparser_tz_string(user_tz)

        parsed_repeat = dateparser.parse(interval, settings=parse_settings)
        if not parsed_repeat:
            await interaction.response.send_message(
                f"❌ Could not understand the interval `{interval}`.",
                ephemeral=True,
            )
            return

        repeat_seconds = int((parsed_repeat - now).total_seconds())
        if repeat_seconds <= 0:
            await interaction.response.send_message(
                "❌ Repeat interval must be a positive duration (e.g., 'every 1 day').",
                ephemeral=True,
            )
            return
        await self.bot.db.execute(
            "UPDATE reminders SET repeat_interval = ? WHERE id = ?",
            (repeat_seconds, reminder_id),
        )
        await self.bot.db.commit()

        await interaction.response.send_message(f"✅ Reminder #{reminder_id} will now repeat every {timedelta(seconds=repeat_seconds)}.",
            ephemeral=True,
        )
    snapshot = app_commands.Group(
        name="snapshot",
        description="Manage channel layout snapshots",
        default_permissions=discord.Permissions(administrator=True)
    )
    @snapshot.command(name="view", description="View a saved channel layout snapshot.")
    @app_commands.describe(snapshot_id="The ID of the snapshot to view")
    @app_commands.checks.has_permissions(administrator=True)
    async def snapshot_view(self, interaction: discord.Interaction, snapshot_id: int):
        await interaction.response.defer(ephemeral=True)
        
        async with self.bot.db.execute(
            "SELECT guild_id, snapshot_data, created_at FROM channel_snapshots WHERE id = ?",
            (snapshot_id,)
        ) as cursor:
            row = await cursor.fetchone()
            
        if not row:
            await interaction.followup.send("❌ Snapshot not found.", ephemeral=True)
            return
            
        guild_id, data_str, created_at = row
        if guild_id != interaction.guild.id:
            await interaction.followup.send("❌ Snapshot not found in this server.", ephemeral=True)
            return
            
        data = json.loads(data_str)
        created_dt = datetime.fromtimestamp(created_at)
        
        # Build the full text representation
        lines = []
        for cat in data["categories"]:
            lines.append(f"**<:category:1534195833430474982> {cat['name']}**")
            for ch in cat["channels"]:
                icon = "<:voice_chat:1534195973058859249>" if ch["type"] == "voice" else "<:text_channel:1534196045616124054>"
                lines.append(f"  {icon} {ch['name']}")
            lines.append("") # Spacer
                
        if data["uncategorized"]:
            lines.append("**Uncategorized**")
            for ch in data["uncategorized"]:
                icon = "<:voice_chat:1534195973058859249>" if ch["type"] == "voice" else "<:text_channel:1534196045616124054>"
                lines.append(f"  {icon} {ch['name']}")
                
        full_text = "\n".join(lines)
        
        # Split into pages of 4000 characters max (safely below 4096 limit)
        # We split at newlines to avoid cutting a line in half
        pages = []
        current_page = ""
        
        for line in full_text.split("\n"):
            if len(current_page) + len(line) + 1 > 4000:
                pages.append(current_page)
                current_page = line + "\n"
            else:
                current_page += line + "\n"
                
        if current_page:
            pages.append(current_page)
            
        # If everything fits in one page, just send it
        if len(pages) == 1:
            embed = discord.Embed(
                title=f"Channel Snapshot #{snapshot_id}",
                description=pages[0],
                color=discord.Color.pink(),
                timestamp=created_dt
            )
            embed.set_footer(text="Captured on")
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            # Use pagination for large layouts
            view = LayoutPagination(pages, interaction.user, snapshot_id, created_dt)
            embed = view.create_embed()
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)
            view.message = await interaction.original_response()
    @snapshot.command(name="list", description="View all saved snapshots.")
    @app_commands.checks.has_permissions(administrator=True)
    async def snapshot_list(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        
        async with self.bot.db.execute(
            "SELECT id, created_at FROM channel_snapshots WHERE guild_id = ? ORDER BY created_at DESC",
            (interaction.guild.id,)
        ) as cursor:
            snapshots = await cursor.fetchall()

        if not snapshots:
            await interaction.followup.send("No snapshots have been recorded for this server yet.", ephemeral=True)
            return

        view = SnapshotPagination(snapshots, interaction.user)
        embed = view.create_embed()
        
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        view.message = await interaction.original_response()
    @snapshot.command(name="compare", description="Compare two channel layout snapshots")
    @app_commands.describe(snapshot_id1="The first snapshot ID", snapshot_id2="The second snapshot ID")
    @app_commands.checks.has_permissions(administrator=True)
    async def snapshot_compare(self, interaction: discord.Interaction, snapshot_id1: int, snapshot_id2: int):
        await interaction.response.defer(ephemeral=True)
        
        async with self.bot.db.execute(
            "SELECT guild_id, snapshot_data, created_at FROM channel_snapshots WHERE id = ?",
            (snapshot_id1,)
        ) as cursor:
            row1 = await cursor.fetchone()
            
        async with self.bot.db.execute(
            "SELECT guild_id, snapshot_data, created_at FROM channel_snapshots WHERE id = ?",
            (snapshot_id2,)
        ) as cursor:
            row2 = await cursor.fetchone()
            
        if not row1 or not row2:
            await interaction.followup.send("❌ One or both snapshots not found.", ephemeral=True)
            return
            
        if row1[0] != interaction.guild.id or row2[0] != interaction.guild.id:
            await interaction.followup.send("❌ Snapshots not found in this server.", ephemeral=True)
            return
            
        data1 = json.loads(row1[1])
        data2 = json.loads(row2[1])
        
        cats1 = {c['name']: c['channels'] for c in data1.get("categories", [])}
        cats2 = {c['name']: c['channels'] for c in data2.get("categories", [])}
        uncat1 = data1.get("uncategorized", [])
        uncat2 = data2.get("uncategorized", [])
        
        all_cats = sorted(set(list(cats1.keys()) + list(cats2.keys())))
        if uncat1 or uncat2:
            all_cats.append("Uncategorized")
            
        embeds = []
        current_embed = discord.Embed(
            title=f"Comparing Snapshots #{snapshot_id1} vs #{snapshot_id2}",
            color=discord.Color.blue(),
            timestamp=datetime.fromtimestamp(row2[2])
        )
        current_embed.description = f"**#{snapshot_id1}** (<t:{row1[2]}:F>)\n**#{snapshot_id2}** (<t:{row2[2]}:F>)"
        
        field_count = 0
        
        def get_channels(cat_name, cats, uncat):
            if cat_name == "Uncategorized":
                return uncat
            return cats.get(cat_name, [])
            
        def build_channel_list(channels):
            if not channels:
                return "*No channels*"
            lines = []
            for ch in channels:
                icon = "<:voice_chat:1534195973058859249>" if ch["type"] == "voice" else "<:text_channel:1534196045616124054>"
                lines.append(f"{icon} {ch['name']}")
            return "\n".join(lines)
            
        for cat_name in all_cats:
            ch1 = get_channels(cat_name, cats1, uncat1)
            ch2 = get_channels(cat_name, cats2, uncat2)
            
            # 3 fields per category (Left, Right, Spacer). Max 25 fields per embed.
            if field_count + 3 > 25:
                embeds.append(current_embed)
                current_embed = discord.Embed(title=f"Comparing #{snapshot_id1} vs #{snapshot_id2} (Cont.)", color=discord.Color.blue())
                field_count = 0
                
            current_embed.add_field(name=f"<:category:1534195833430474982> {cat_name} (#{snapshot_id1})", value=build_channel_list(ch1), inline=True)
            current_embed.add_field(name=f"<:category:1534195833430474982> {cat_name} (#{snapshot_id2})", value=build_channel_list(ch2), inline=True)
            current_embed.add_field(name="\u200b", value="\u200b", inline=True) # Spacer to force pairs
            field_count += 3
            
        embeds.append(current_embed)
        
        if len(embeds) == 1:
            await interaction.followup.send(embed=embeds[0], ephemeral=True)
        else:
            view = ComparePagination(embeds, interaction.user)
            await interaction.followup.send(embed=view.create_embed(), view=view, ephemeral=True)
            view.message = await interaction.original_response()
    @snapshot.command(name="delete", description="Delete a saved channel layout snapshot.")
    @app_commands.describe(snapshot_id="The ID of the snapshot to delete")
    @app_commands.checks.has_permissions(administrator=True)
    async def snapshot_delete(self, interaction: discord.Interaction, snapshot_id: int):
        async with self.bot.db.execute(
            "SELECT guild_id FROM channel_snapshots WHERE id = ?",
            (snapshot_id,),
        ) as cursor:
            row = await cursor.fetchone()

        if not row:
            await interaction.response.send_message("❌ Snapshot not found.", ephemeral=True)
            return

        if row[0] != interaction.guild.id:
            await interaction.response.send_message("❌ Snapshot not found in this server.", ephemeral=True)
            return

        await self.bot.db.execute(
            "DELETE FROM channel_snapshots WHERE id = ?",
            (snapshot_id,),
        )
        await self.bot.db.commit()

        await interaction.response.send_message(
            f"✅ Snapshot #{snapshot_id} has been deleted.", ephemeral=True
        )
    @app_commands.command(name="say", description="Make the bot send a message to a specific channel")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(
        channel="channel to send the message to",
        message="message to send",
        attachment="image/file to send"
    )
    async def say(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        message: str = None,
        attachment: discord.Attachment = None
    ):
        if not message and not attachment:
            await interaction.response.send_message("You must provide a message or an attachment!", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        files_to_send = []
        if attachment:
            file = await attachment.to_file()
            files_to_send.append(file)

        try:
            await channel.send(content=message, files=files_to_send)
            await interaction.followup.send(f"Message successfully sent to {channel.mention}!")
        except discord.Forbidden:
            await interaction.followup.send("I don't have permission to send messages in that channel.", ephemeral=True)
    # ============================================================
    # /convert
    # ============================================================

    convert = app_commands.Group(
        name="convert",
        description="Convert between units and currencies",
    )

    async def _currency_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        upper = current.upper().strip()
        results: list[app_commands.Choice[str]] = []
        seen_codes: set[str] = set()

        # Match ISO codes
        for code in COMMON_CURRENCIES:
            if upper in code:
                name = CURRENCY_NAMES.get(code, code)
                results.append(app_commands.Choice(name=f"{code} — {name}", value=code))
                seen_codes.add(code)

        # Match aliases (YEN → JPY, etc.)
        for alias, code in sorted(CURRENCY_ALIASES.items()):
            if upper in alias and code not in seen_codes:
                name = CURRENCY_NAMES.get(code, code)
                results.append(app_commands.Choice(name=f"{alias} → {code} — {name}", value=code))
                seen_codes.add(code)

        # If user typed a valid-looking 3-letter code not in our list, offer it raw
        if upper and len(upper) == 3 and upper.isalpha() and upper not in seen_codes:
            results.insert(0, app_commands.Choice(name=upper, value=upper))

        return results[:25]

    @convert.command(name="weight", description="Convert between weight units.")
    @app_commands.describe(amount="Amount to convert", from_unit="Source unit", to_unit="Target unit")
    @app_commands.choices(
        from_unit=[app_commands.Choice(name=k, value=k) for k in WEIGHT_UNITS],
        to_unit=[app_commands.Choice(name=k, value=k) for k in WEIGHT_UNITS],
    )
    async def convert_weight(
        self,
        interaction: discord.Interaction,
        amount: float,
        from_unit: app_commands.Choice[str],
        to_unit: app_commands.Choice[str],
    ):
        if amount < 0:
            await interaction.response.send_message("❌ Amount must be positive.", ephemeral=True)
            return

        result = amount * WEIGHT_UNITS[from_unit.value] / WEIGHT_UNITS[to_unit.value]

        embed = discord.Embed(
            description=f"**{amount:g} {from_unit.value}** = **{result:.4g} {to_unit.value}**",
            color=discord.Color.pink(),
            timestamp=discord.utils.utcnow(),
        )
        embed.set_footer(text="Weight conversion")
        await interaction.response.send_message(embed=embed)

    @convert.command(name="length", description="Convert between length / size units.")
    @app_commands.describe(amount="Amount to convert", from_unit="Source unit", to_unit="Target unit")
    @app_commands.choices(
        from_unit=[app_commands.Choice(name=k, value=k) for k in LENGTH_UNITS],
        to_unit=[app_commands.Choice(name=k, value=k) for k in LENGTH_UNITS],
    )
    async def convert_length(
        self,
        interaction: discord.Interaction,
        amount: float,
        from_unit: app_commands.Choice[str],
        to_unit: app_commands.Choice[str],
    ):
        if amount < 0:
            await interaction.response.send_message("❌ Amount must be positive.", ephemeral=True)
            return

        result = amount * LENGTH_UNITS[from_unit.value] / LENGTH_UNITS[to_unit.value]

        embed = discord.Embed(
            description=f"**{amount:g} {from_unit.value}** = **{result:.4g} {to_unit.value}**",
            color=discord.Color.pink(),
            timestamp=discord.utils.utcnow(),
        )
        embed.set_footer(text="Length conversion")
        await interaction.response.send_message(embed=embed)

    @convert.command(name="currency", description="Convert between currencies using live Wise exchange rates.")
    @app_commands.describe(
        amount="Amount to convert",
        from_currency="Source currency (e.g. USD, or type YEN, POUND, etc.)",
        to_currency="Target currency (e.g. EUR, or type YEN, POUND, etc.)",
    )
    @app_commands.autocomplete(from_currency=_currency_autocomplete, to_currency=_currency_autocomplete)
    @app_commands.checks.cooldown(1, 5.0, key=lambda i: i.user.id)
    async def convert_currency(
        self,
        interaction: discord.Interaction,
        amount: float,
        from_currency: str,
        to_currency: str,
    ):
        await interaction.response.defer()

        # Resolve aliases (YEN → JPY, etc.) then validate
        src = CURRENCY_ALIASES.get(from_currency.upper().strip(), from_currency.upper().strip())
        tgt = CURRENCY_ALIASES.get(to_currency.upper().strip(), to_currency.upper().strip())

        if len(src) != 3 or not src.isalpha():
            await interaction.followup.send(
                f"❌ `{from_currency}` is not a valid ISO-4217 currency code.\n"
                f"Use a 3-letter code like USD, EUR, GBP, JPY — or type a name like 'yen', 'pound', 'euro'.",
                ephemeral=True,
            )
            return

        if len(tgt) != 3 or not tgt.isalpha():
            await interaction.followup.send(
                f"❌ `{to_currency}` is not a valid ISO-4217 currency code.\n"
                f"Use a 3-letter code like USD, EUR, GBP, JPY — or type a name like 'yen', 'pound', 'euro'.",
                ephemeral=True,
            )
            return

        if src == tgt:
            await interaction.followup.send("❌ Source and target currencies are the same.", ephemeral=True)
            return

        # Small delay to be polite to the API
        await asyncio.sleep(0.5)

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    WISE_QUOTE_URL,
                    json={
                        "sourceCurrency": src,
                        "targetCurrency": tgt,
                        "sourceAmount": amount,
                    },
                    headers={"Content-Type": "application/json"},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        logger.error(f"[Misc] Wise API error {resp.status}: {body[:300]}")
                        await interaction.followup.send(
                            f"❌ Couldn't fetch exchange rate for {src}→{tgt}. "
                            f"Make sure both are valid ISO-4217 currency codes (e.g. USD, EUR, JPY).",
                            ephemeral=True,
                        )
                        return

                    data = await resp.json()
        except (aiohttp.ClientError, TimeoutError, asyncio.TimeoutError) as e:
            logger.error(f"[Misc] Wise API request failed: {e}")
            await interaction.followup.send(
                "❌ Couldn't reach the exchange rate service. Try again later.",
                ephemeral=True,
            )
            return

        rate = data.get("rate")
        if rate is None:
            await interaction.followup.send(
                "❌ Unexpected response from the exchange rate service.", ephemeral=True
            )
            return

        result = amount * rate
        src_name = CURRENCY_NAMES.get(src, src)
        tgt_name = CURRENCY_NAMES.get(tgt, tgt)
        src_sym = CURRENCY_SYMBOLS.get(src, "")
        tgt_sym = CURRENCY_SYMBOLS.get(tgt, "")
        src_amt = f"{int(amount):,}" if amount.is_integer() else f"{amount:,.2f}"
        result_amt = f"{result:,.2f}"

        file = discord.File("assets/wise.png", filename="wise.png")
        embed = discord.Embed(
            color=0x9fe870,
            timestamp=discord.utils.utcnow(),
            description=(
                f"**{src_sym}{src_amt} {src}** ({src_name}) = "
                f"**{tgt_sym}{result_amt} {tgt}** ({tgt_name})\n"
                f"Rate: 1 {src} = {rate:.6f} {tgt}"
            ),
        )
        embed.set_thumbnail(url="attachment://wise.png")
        embed.set_footer(text="Powered by Wise.com")
        await interaction.followup.send(embed=embed, file=file)

async def setup(bot: commands.Bot):
    await bot.add_cog(Misc(bot))