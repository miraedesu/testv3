#--- Imports ---
"""Wuthering Waves gacha history import + pull prediction."""
from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands
from cogs.admin import is_bot_owner
import random


logger = logging.getLogger(__name__)

#--- WuWa gacha constants ---

WUWA_TZ = timezone(timedelta(hours=8))   # WuWa global server time (CST)
HARD_PITY = 80
SOFT_PITY = 66
BASE_5STAR_RATE = 0.008
MAX_JSON_BYTES = 2 * 1024 * 1024          # 2 MB cap

#--- Standard pool (used to detect 50/50 win/lose) ---
STANDARD_5STAR_CHARACTERS = {"Verina", "Calcharo", "Lingyang", "Jianxin", "Encore"}
STANDARD_5STAR_WEAPONS = {
    "Emerald of Genesis", "Lustrous Razor", "Abyss Surges",
    "Static Mist", "Cosmic Ripples",
}

#--- Banner type IDs (Kuro's cardPoolType, used in manual entry) ---
BANNER_TYPES = {
    1: "Featured Resonator",
    2: "Featured Weapon",
    3: "Standard Resonator (Tidal Chorus)",
    4: "Standard Weapon (Winter Brume)",
    5: "Novice (Utterance of Marvels)",
}
BANNER_CHOICES = [
    app_commands.Choice(name="Featured Resonator (limited char)", value=1),
    app_commands.Choice(name="Featured Weapon (limited)",       value=2),
    app_commands.Choice(name="Standard Resonator (Tidal Chorus)", value=3),
    app_commands.Choice(name="Standard Weapon (Winter Brume)",  value=4),
    app_commands.Choice(name="Novice (Utterance of Marvels)",   value=5),
]
#--- Monte Carlo simulation ---
def _simulate_5star_sequence(
    start_pity: int,
    num_5stars: int,
    num_sims: int = 20000,
) -> list[dict]:
    """Monte Carlo: simulate pulls until N 5★s drop.

    Returns per-5★ stats: median pity, IQR for pity, median cumulative
    pulls, IQR for cumulative pulls.
    """
    pity_drops: list[list[int]] = [[] for _ in range(num_5stars)]
    cumulative: list[list[int]] = [[] for _ in range(num_5stars)]

    for _ in range(num_sims):
        pity = start_pity
        total_pulls = 0
        for k in range(num_5stars):
            #--- Pull until a 5★ drops ---
            while True:
                pity += 1
                total_pulls += 1
                if random.random() < _pity_rate(pity):
                    pity_drops[k].append(pity)
                    cumulative[k].append(total_pulls)
                    pity = 0   #--- reset after 5★ ---
                    break

    def _pct(sorted_list: list[int], p: float) -> int:
        if not sorted_list:
            return 0
        idx = int(len(sorted_list) * p)
        return sorted_list[min(idx, len(sorted_list) - 1)]

    results = []
    for k in range(num_5stars):
        pities = sorted(pity_drops[k])
        cumuls = sorted(cumulative[k])
        results.append({
            "pity_median": _pct(pities, 0.50),
            "pity_q1":     _pct(pities, 0.25),
            "pity_q3":     _pct(pities, 0.75),
            "cumul_median": _pct(cumuls, 0.50),
            "cumul_q1":     _pct(cumuls, 0.25),
            "cumul_q3":     _pct(cumuls, 0.75),
        })
    return results
#--- Soft pity model: P(5★ on pull n | no 5★ yet) ---
def _pity_rate(n: int) -> float:
    """Soft pity ramp. Linear ramp from base rate at n=66 to 100% at n=80."""
    if n < SOFT_PITY:
        return BASE_5STAR_RATE
    if n >= HARD_PITY:
        return 1.0
    #--- Linear ramp reaching 1.0 at hard pity ---
    # p(n) = BASE + (n - SOFT) * (1 - BASE) / (HARD - SOFT)
    return BASE_5STAR_RATE + (n - SOFT_PITY) * (1.0 - BASE_5STAR_RATE) / (HARD_PITY - SOFT_PITY)

#--- Pity threshold helper ---
def _pity_thresholds_from(start_pity: int) -> dict:
    """Return pity values at which cumulative 5★ prob crosses 50/80/95%,
    starting from a given current pity."""
    thresholds = {0.5: None, 0.8: None, 0.95: None}
    cum_p_no_5star = 1.0
    for i in range(HARD_PITY - start_pity):
        n = start_pity + i + 1
        rate = _pity_rate(n)
        cum_p_no_5star *= (1.0 - rate)
        cumulative = 1.0 - cum_p_no_5star
        for thresh in thresholds:
            if thresholds[thresh] is None and cumulative >= thresh:
                thresholds[thresh] = n
        if all(v is not None for v in thresholds.values()):
            break
    return thresholds

#--- Timestamp parser ---

def _parse_timestamp(s) -> Optional[int]:
    """Parse a wuwatracker/manual timestamp into UTC Unix seconds.

    Handles ISO 8601, 'YYYY-MM-DD HH:MM:SS', and Unix epoch (int/str).
    Assumes UTC+8 if no tz info is present (WuWa server time).
    """
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return int(s)
    if isinstance(s, str):
        s = s.strip()
        if s.isdigit():
            return int(s)
        #--- Try ISO 8601 first ---
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=WUWA_TZ)
            return int(dt.timestamp())
        except ValueError:
            pass
        #--- Try common explicit formats ---
        for fmt in (
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%dT%H:%M",
        ):
            try:
                dt = datetime.strptime(s, fmt)
                dt = dt.replace(tzinfo=WUWA_TZ)
                return int(dt.timestamp())
            except ValueError:
                continue
    return None

#--- JSON parser ---

def _parse_wuwatracker_json(raw: bytes) -> list[dict]:
    """Parse wuwatracker-pulls JSON. Returns list of normalized pull dicts.
    Raises ValueError on malformed/too-large input.
    """
    if len(raw) > MAX_JSON_BYTES:
        raise ValueError(
            f"File too large ({len(raw):,} bytes; limit {MAX_JSON_BYTES:,})"
        )
    try:
        data = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise ValueError(f"Invalid JSON: {e}")

    #--- wuwatracker format is an object with a "pulls" list ---
    if isinstance(data, dict):
        records = data.get("pulls", [])
    elif isinstance(data, list):
        records = data   #--- bare list fallback ---
    else:
        raise ValueError("Expected a JSON object with a 'pulls' list.")

    if not isinstance(records, list):
        raise ValueError("'pulls' must be a list of pull records.")

    #--- Validate + normalize each record ---
    out: list[dict] = []
    for rec in records:
        if not isinstance(rec, dict):
            continue
        name = rec.get("name")
        time_str = rec.get("time")
        #--- wuwatracker uses "qualityLevel" (3/4/5) ---
        quality = (rec.get("qualityLevel")
                   or rec.get("qualityType")
                   or rec.get("rarity")
                   or rec.get("quality"))
        if name is None or time_str is None or quality is None:
            continue

        #--- Normalize rarity (3/4/5) ---
        try:
            rarity = int(quality)
        except (ValueError, TypeError):
            continue
        if rarity not in (3, 4, 5):
            continue

        #--- Parse timestamp ---
        ts = _parse_timestamp(time_str)
        if ts is None:
            continue

        #--- cardPoolType is the banner type (1-5), used for grouping ---
        card_pool_type = rec.get("cardPoolType")
        if card_pool_type is None:
            #--- Fallback for older formats ---
            card_pool_type = rec.get("cardPoolId") or rec.get("gachaId") or 0
        try:
            card_pool_type = int(card_pool_type)
        except (ValueError, TypeError):
            card_pool_type = 0

        out.append({
            "card_pool_id": str(card_pool_type),
            "banner_type": card_pool_type,
            "item_id": rec.get("resourceId"),
            "item_name": str(name),
            "rarity": rarity,
            "pulled_at_unix": ts,
        })
    return out


#--- Cog: WuWa ---

class WuWa(commands.Cog):
    """Wuthering Waves gacha history import + pull prediction."""

    wuwa = app_commands.Group(
        name="wuwa",
        description="Wuthering Waves gacha history and prediction",
        default_permissions=discord.Permissions(administrator=True),
    )
    pull_history = app_commands.Group(
        name="pull_history",
        description="Import or log your pull history",
        parent=wuwa,
    )

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    #--- Storage helpers ---

    async def _latest_pull_ts_per_pool(self, user_id: int) -> dict[str, int]:
        """Latest stored pull timestamp per card_pool_id for a user.
        Used to dedup JSON imports.
        """
        async with self.bot.db.execute(
            "SELECT card_pool_id, MAX(pulled_at_unix) "
            "FROM wuwa_pulls WHERE user_id = ? GROUP BY card_pool_id",
            (user_id,),
        ) as cur:
            rows = await cur.fetchall()
        return {pid: int(ts) if ts else 0 for pid, ts in rows}

    #--- /wuwa pull_history import ---

    @pull_history.command(
        name="import",
        description="Upload a wuwatracker-pulls.json export to import your pull history",
    )
    @app_commands.describe(file="wuwatracker-pulls.json file (max 2 MB)")
    @is_bot_owner()
    async def pull_history_import(
        self,
        interaction: discord.Interaction,
        file: discord.Attachment,
    ):
        #--- Defer: parsing + inserting takes a moment ---
        await interaction.response.defer(ephemeral=True)

        #--- File size guard ---
        if file.size > MAX_JSON_BYTES:
            await interaction.followup.send(
                f"File is {file.size:,} bytes — limit is {MAX_JSON_BYTES:,} bytes.",
                ephemeral=True,
            )
            return
        if not file.filename.lower().endswith(".json"):
            await interaction.followup.send(
                "File must be a `.json` file.",
                ephemeral=True,
            )
            return

        #--- Download ---
        try:
            raw = await file.read()
        except Exception:
            logger.exception("[WuWa] Failed to read attachment %s", file.id)
            await interaction.followup.send(
                "Couldn't download the file. Try again.",
                ephemeral=True,
            )
            return

        #--- Parse ---
        try:
            records = _parse_wuwatracker_json(raw)
        except ValueError as e:
            await interaction.followup.send(f"Couldn't parse: {e}", ephemeral=True)
            return

        if not records:
            await interaction.followup.send(
                "No valid pull records found in the file.",
                ephemeral=True,
            )
            return

        #--- Dedup: drop records whose timestamp <= latest stored
        #--- for the same card_pool_id (avoids double-import) ---
        latest_per_pool = await self._latest_pull_ts_per_pool(interaction.user.id)
        new_records = []
        skipped = 0
        for r in records:
            pool = r["card_pool_id"]
            if pool in latest_per_pool and r["pulled_at_unix"] < latest_per_pool[pool]:
                skipped += 1
                continue
            new_records.append(r)

        if not new_records:
            await interaction.followup.send(
                f"All {len(records)} pulls already in DB — nothing to import.",
                ephemeral=True,
            )
            return

        #--- Insert all new records (dedup handled above by timestamp) ---
        rows = [
            (interaction.user.id,
             r["card_pool_id"],
             r["banner_type"],   # <-- was None, now uses cardPoolType directly
             str(r["item_id"]) if r["item_id"] is not None else None,
             r["item_name"],
             r["rarity"],
             r["pulled_at_unix"],
             "json")
            for r in new_records
        ]
        cur = await self.bot.db.executemany(
            """INSERT INTO wuwa_pulls
               (user_id, card_pool_id, banner_type, item_id, item_name,
                rarity, pulled_at_unix, source)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
        await self.bot.db.commit()
        inserted = cur.rowcount if cur.rowcount and cur.rowcount > 0 else len(rows)

        #--- Build summary ---
        new_5star = sum(1 for r in new_records if r["rarity"] == 5)
        await interaction.followup.send(
            f"✅ Imported **{inserted}** new pulls ({new_5star} were 5★). "
            f"Skipped {skipped} already in DB. "
            f"Use `/wuwa predict` to see your prediction.",
            ephemeral=True,
        )

    #--- /wuwa pull_history manual ---

    @pull_history.command(
        name="manual",
        description="Manually log a 5★ pull (name, banner, time)",
    )
    @app_commands.describe(
        item_name="5★ character or weapon name (e.g. 'Jiyan', 'Verdant Summit')",
        banner_type="Which banner you pulled on",
        pulled_at="Time of pull — format: YYYY-MM-DD HH:MM (WuWa server time, UTC+8)",
    )
    @app_commands.choices(banner_type=BANNER_CHOICES)
    @is_bot_owner()
    async def pull_history_manual(
        self,
        interaction: discord.Interaction,
        item_name: str,
        banner_type: app_commands.Choice[int],
        pulled_at: str,
    ):
        #--- Parse time ---
        ts = _parse_timestamp(pulled_at)
        if ts is None:
            await interaction.response.send_message(
                "Couldn't parse time. Use `YYYY-MM-DD HH:MM` "
                "(WuWa server time, UTC+8).",
                ephemeral=True,
            )
            return

        #--- Synthetic pool id so manual entries don't collide with JSON ones ---
        synthetic_pool = f"manual_{banner_type.value}"
        try:
            await self.bot.db.execute(
                """INSERT INTO wuwa_pulls
                   (user_id, card_pool_id, banner_type, item_id,
                    item_name, rarity, pulled_at_unix, source)
                   VALUES (?, ?, ?, NULL, ?, 5, ?, 'manual')""",
                (interaction.user.id,
                 synthetic_pool,
                 banner_type.value,
                 item_name.strip(),
                 ts),
            )
            await self.bot.db.commit()
        except Exception:
            logger.exception("[WuWa] Manual insert failed")
            await interaction.response.send_message(
                "DB error — pull not saved.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            f"✅ Logged 5★ **{item_name}** on **{banner_type.name}** "
            f"at <t:{ts}:F>.\n"
            f"Use `/wuwa predict` to see your prediction.",
            ephemeral=True,
        )

    @wuwa.command(
        name="predict",
        description="Predict your next 1-5 5★s based on your imported history",
    )
    @app_commands.describe(
        five_stars="How many upcoming 5★s to predict (1-5)"
    )
    @is_bot_owner()
    async def predict(
        self,
        interaction: discord.Interaction,
        five_stars: int = 5,
    ):
        #--- Defer: history scan + analysis ---
        await interaction.response.defer(ephemeral=True)

        five_stars = max(1, min(5, five_stars))

        #--- Load all of user's pulls sorted by time ---
        async with self.bot.db.execute(
            "SELECT card_pool_id, banner_type, item_name, rarity, "
            "pulled_at_unix, source "
            "FROM wuwa_pulls WHERE user_id = ? ORDER BY pulled_at_unix ASC",
            (interaction.user.id,),
        ) as cur:
            rows = await cur.fetchall()

        if not rows:
            await interaction.followup.send(
                "No pulls in DB. Use `/wuwa pull_history import` first.",
                ephemeral=True,
            )
            return

        #--- Group by card_pool_id to compute per-pool pity ---
        pools: dict[str, list[dict]] = {}
        for card_pool_id, banner_type, item_name, rarity, ts, source in rows:
            pools.setdefault(card_pool_id, []).append({
                "banner_type": banner_type,
                "item_name": item_name,
                "rarity": rarity,
                "ts": ts,
                "source": source,
            })

        #--- Identify each pool's banner type ---
        # For manual entries: banner_type is already stored.
        # For JSON imports: infer from 5★ item names (limited vs standard).
        pool_banner_type: dict[str, int] = {}
        for pid, pulls in pools.items():
            manual_bt = next(
                (p["banner_type"] for p in pulls if p["banner_type"] is not None),
                None,
            )
            if manual_bt is not None:
                pool_banner_type[pid] = manual_bt
                continue
            five_star_names = [p["item_name"] for p in pulls if p["rarity"] == 5]
            if not five_star_names:
                pool_banner_type[pid] = 0
                continue
            any_std_char = any(n in STANDARD_5STAR_CHARACTERS for n in five_star_names)
            any_std_weapon = any(n in STANDARD_5STAR_WEAPONS for n in five_star_names)
            any_limited = any(
                n not in STANDARD_5STAR_CHARACTERS | STANDARD_5STAR_WEAPONS
                for n in five_star_names
            )
            if any_limited and any_std_char:
                pool_banner_type[pid] = 1   #--- Featured Resonator (had 50/50) ---
            elif any_limited:
                pool_banner_type[pid] = 2   #--- Featured Weapon ---
            elif any_std_char:
                pool_banner_type[pid] = 3   #--- Standard Resonator ---
            elif any_std_weapon:
                pool_banner_type[pid] = 4   #--- Standard Weapon ---
            else:
                pool_banner_type[pid] = 0

        #--- Pick the pool to predict on ---
        # Prefer Featured Resonator (most useful for 50/50 questions);
        # otherwise the pool with the most recent pull.
        featured_pids = [pid for pid, bt in pool_banner_type.items() if bt == 1]
        if featured_pids:
            target_pid = max(featured_pids, key=lambda p: pools[p][-1]["ts"])
        else:
            target_pid = max(pools.keys(), key=lambda p: pools[p][-1]["ts"])

        target_pulls = pools[target_pid]
        target_bt = pool_banner_type.get(target_pid, 0)

        #--- Compute current pity: pulls since last 5★ in this pool ---
        last_5star_idx = -1
        for i, p in enumerate(target_pulls):
            if p["rarity"] == 5:
                last_5star_idx = i
        current_pity = len(target_pulls) - 1 - last_5star_idx

        #--- Determine 50/50 state (Featured Resonator only) ---
        guaranteed_featured: Optional[bool] = None
        if target_bt == 1 and last_5star_idx >= 0:
            last_5star_name = target_pulls[last_5star_idx]["item_name"]
            #--- Lost 50/50 = got a standard char → next is guaranteed featured
            guaranteed_featured = last_5star_name in STANDARD_5STAR_CHARACTERS

        #--- Thresholds for the very next 5★ (used in probability section) ---
        thresholds_first = _pity_thresholds_from(current_pity)
        soft_pity_remaining = max(0, SOFT_PITY - current_pity)

        #--- Lucky time analysis (only 5★s that dropped below soft pity) ---
        # Compute pity at each 5★ drop, keep only the "lucky" ones (< SOFT_PITY)
        lucky_times: list[int] = []
        lucky_pities: list[int] = []
        prev_5star_idx = -1
        for i, p in enumerate(target_pulls):
            if p["rarity"] == 5:
                pity_at_drop = i - prev_5star_idx   # pulls since last 5★ (incl. this one)
                if pity_at_drop < SOFT_PITY:
                    lucky_times.append(p["ts"])
                    lucky_pities.append(pity_at_drop)
                prev_5star_idx = i

        total_5star = sum(1 for p in target_pulls if p["rarity"] == 5)
        lucky_info = None
        if len(lucky_times) >= 3:
            wuwa_dts = [datetime.fromtimestamp(ts, tz=WUWA_TZ)
                        for ts in lucky_times]
            hour_counts = Counter(d.hour for d in wuwa_dts)
            dow_counts = Counter(d.weekday() for d in wuwa_dts)
            best_hour, best_hour_n = hour_counts.most_common(1)[0]
            best_dow, best_dow_n = dow_counts.most_common(1)[0]
            dow_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
            avg_pity = sum(lucky_pities) / len(lucky_pities)
            min_pity = min(lucky_pities)
            lucky_info = {
                "best_hour": best_hour,
                "best_hour_n": best_hour_n,
                "best_dow": dow_names[best_dow],
                "best_dow_n": best_dow_n,
                "lucky_count": len(lucky_times),
                "total_5star": total_5star,
                "avg_pity": avg_pity,
                "min_pity": min_pity,
            }

        #--- Build embed ---
        bt_name = BANNER_TYPES.get(target_bt, "Unknown banner")
        embed = discord.Embed(
            title="🔮 WuWa Pull Prediction",
            color=discord.Color.gold(),
        )
        #--- Truncate the pool id for display ---
        pool_display = target_pid if len(target_pid) <= 24 else target_pid[:21] + "…"
        embed.add_field(
            name="Banner",
            value=f"{bt_name}\n`{pool_display}`",
            inline=False,
        )
        embed.add_field(
            name="Current pity",
            value=f"**{current_pity}** / {HARD_PITY}",
            inline=True,
        )
        embed.add_field(
            name="50/50 state",
            value=(
                "Unknown — no 5★ in history"
                if guaranteed_featured is None
                else ("🟢 **Guaranteed featured** (lost last 50/50)"
                      if guaranteed_featured
                      else "🟡 **Coin flip** (won last 50/50)")
            ),
            inline=True,
        )
        #--- Soft pity status ---
        if current_pity >= HARD_PITY:
            soft_line = "**Hard pity — next pull is guaranteed 5★**"
        elif current_pity >= SOFT_PITY:
            soft_line = (
                f"**In soft pity now** (pull {current_pity}) — "
                f"rates climbing until hard pity at {HARD_PITY}"
            )
        else:
            soft_line = (
                f"Soft pity starts at **pull {SOFT_PITY}** "
                f"({soft_pity_remaining} more until rates begin climbing)"
            )
        embed.add_field(
            name="Soft pity",
            value=soft_line,
            inline=False,
        )

        #--- Next N 5★s (Monte Carlo simulation, 20k runs) ---
        sim_results = _simulate_5star_sequence(current_pity, five_stars, num_sims=20000)

        five_star_lines = []
        for k, r in enumerate(sim_results, start=1):
            if k == 1:
                pulls_needed = max(0, r["pity_median"] - current_pity)
                pity_q1 = r["pity_q1"]
                pity_q3 = r["pity_q3"]
                pulls_q1 = max(0, pity_q1 - current_pity)
                pulls_q3 = max(0, pity_q3 - current_pity)
                five_star_lines.append(
                    f"**#{k}** · **{pulls_needed}** more pulls "
                    f"(typical {pulls_q1}–{pulls_q3})\n"
                    f"  → drops at pity **{r['pity_median']}** "
                    f"(IQR {pity_q1}–{pity_q3})\n"
                    f"  → cumulative: **{r['cumul_median']}** "
                    f"(IQR {r['cumul_q1']}–{r['cumul_q3']})"
                )
            else:
                pulls_needed = r["pity_median"]
                pity_q1 = r["pity_q1"]
                pity_q3 = r["pity_q3"]
                five_star_lines.append(
                    f"**#{k}** · **{pulls_needed}** more pulls "
                    f"(typical {pity_q1}–{pity_q3})\n"
                    f"  → drops at pity **{r['pity_median']}** "
                    f"(IQR {pity_q1}–{pity_q3})\n"
                    f"  → cumulative: **{r['cumul_median']}** "
                    f"(IQR {r['cumul_q1']}–{r['cumul_q3']})"
                )

        #--- 50/50 note for featured banner subsequent 5★s ---
        if target_bt == 1 and five_stars > 1:
            five_star_lines.append(
                f"\n*Note: 50/50 outcome of each 5★ affects the next one's "
                f"status. Simulations don't model this — pull counts assume "
                f"every 5★ is obtained.*"
            )

        embed.add_field(
            name=f"Next {five_stars} 5★s (Monte Carlo, 20k sims)",
            value="\n".join(five_star_lines),
            inline=False,
        )
        #--- Probability thresholds for the very next 5★ ---
        timing_lines = []
        for thresh in (0.5, 0.8, 0.95):
            n = thresholds_first[thresh]
            if n is not None:
                remaining = n - current_pity
                if remaining <= 0:
                    timing_lines.append(
                        f"**{int(thresh*100)}%** — already passed "
                        f"(currently at {current_pity})"
                    )
                else:
                    timing_lines.append(
                        f"**{int(thresh*100)}%** chance by **pity {n}** "
                        f"({remaining} more pulls)"
                    )
        embed.add_field(
            name="Probability thresholds (next 5★ only)",
            value="\n".join(timing_lines) or "Unknown",
            inline=False,
        )

        #--- Lucky time (only sub-soft-pity drops; fun, non-scientific) ---
        if lucky_info:
            embed.add_field(
                name=(
                    f"🎲 Lucky time "
                    f"({lucky_info['lucky_count']} of "
                    f"{lucky_info['total_5star']} 5★s dropped below soft pity)"
                ),
                value=(
                    f"Among your **{lucky_info['lucky_count']}** lucky 5★s "
                    f"(pity < {SOFT_PITY}):\n"
                    f"• Best pity: **{lucky_info['min_pity']}** · "
                    f"avg pity: **{lucky_info['avg_pity']:.1f}**\n"
                    f"• Most common hour: **{lucky_info['best_hour']:02d}:00** "
                    f"WuWa time "
                    f"({lucky_info['best_hour_n']} of "
                    f"{lucky_info['lucky_count']} lucky 5★s)\n"
                    f"• Most common weekday: **{lucky_info['best_dow']}** "
                    f"({lucky_info['best_dow_n']} of "
                    f"{lucky_info['lucky_count']} lucky 5★s)\n\n"
                    f"⚠️ Each pull is independent — shown for entertainment only."
                ),
                inline=False,
            )
        elif total_5star >= 3:
            embed.add_field(
                name="🎲 Lucky time",
                value=(
                    f"You have **{total_5star}** 5★s but none dropped below "
                    f"soft pity ({SOFT_PITY}). No lucky pulls to analyze — "
                    f"you're a pity farmer! 🌾"
                ),
                inline=False,
            )

        #--- Total across all pools (for footer clarity) ---
        total_all_pools = sum(len(pulls) for pulls in pools.values())
        embed.set_footer(text=(
            f"{len(target_pulls)} pulls in this pool · "
            f"{total_all_pools} total across all banners · "
            f"Soft pity {SOFT_PITY} · Hard pity {HARD_PITY}"
        ))

        await interaction.followup.send(embed=embed, ephemeral=True)

#--- Cog entry point ---

async def setup(bot: commands.Bot):
    await bot.add_cog(WuWa(bot))