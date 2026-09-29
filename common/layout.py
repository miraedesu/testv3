"""Single source of truth for channel-layout serialization.

cogs/misc.py (startup baselines) and cogs/member_events.py (runtime move
tracking) must produce IDENTICAL dicts, or every restart "detects a change"
that never happened and writes junk snapshots. Keep this the only definition.
"""
from __future__ import annotations

import discord


def capture_layout(guild: discord.Guild) -> dict:
    """Current channel layout as a JSON-serializable dict. Deterministic:
    channels sorted by position, stage channels classified as voice.
    When storing/comparing, dump with sort_keys=True (sorts dict keys only —
    list order is handled here)."""
    def ch_type(ch: discord.abc.GuildChannel) -> str:
        return "voice" if isinstance(
            ch, (discord.VoiceChannel, discord.StageChannel)) else "text"

    categories = []
    for category in sorted(guild.categories, key=lambda c: c.position):
        channels = [
            {"name": ch.name, "type": ch_type(ch)}
            for ch in sorted(category.channels, key=lambda c: c.position)
        ]
        categories.append({"name": category.name, "channels": channels})

    uncategorized = [
        {"name": ch.name, "type": ch_type(ch)}
        for ch in sorted(guild.channels, key=lambda c: c.position)
        if not isinstance(ch, discord.CategoryChannel) and ch.category is None
    ]
    return {"categories": categories, "uncategorized": uncategorized}