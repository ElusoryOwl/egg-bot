import os
import random
import sqlite3
import asyncio
import logging
import threading
import signal
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

load_dotenv()

import discord
from discord import app_commands

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("egg-bot")

# ── Intents ───────────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.members = True
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# ── Constants (overridable via environment variables) ─────────────────────────

DB_PATH             = os.environ.get("DB_PATH", "egg_bot.db")
STREAK_ROLE_NAME    = os.environ.get("STREAK_ROLE_NAME", "🐣 Egg Streak Legend")
HISTORY_DAYS        = int(os.environ.get("HISTORY_DAYS", 7))
GOLDEN_EGG_CHANCE   = float(os.environ.get("GOLDEN_EGG_CHANCE", 1 / 75))
FRIDAY_BONUS        = float(os.environ.get("FRIDAY_BONUS", 0.07))
MAX_THROWS_PER_DAY  = int(os.environ.get("MAX_THROWS_PER_DAY", 3))
STREAK_ROLE_THRESHOLD = int(os.environ.get("STREAK_ROLE_THRESHOLD", 7))
LEADERBOARD_POOL_SIZE = int(os.environ.get("LEADERBOARD_POOL_SIZE", 500))

def validate_config(
    golden_chance: float = GOLDEN_EGG_CHANCE,
    friday_bonus: float = FRIDAY_BONUS,
    max_throws: int = MAX_THROWS_PER_DAY,
    history_days: int = HISTORY_DAYS,
    streak_threshold: int = STREAK_ROLE_THRESHOLD,
) -> list[str]:
    """
    Sanity-check the gameplay constants. throw_egg()'s tiers are cumulative
    (golden < quad < single), so a misconfigured value doesn't just look
    wrong — it can silently make a whole tier unreachable or push odds over
    100%. Returns a list of human-readable problems (empty = all good).
    """
    errors = []
    if not (0 <= golden_chance < 1 / 25):
        errors.append(
            f"GOLDEN_EGG_CHANCE must be between 0 and {1/25:.4f} "
            f"(the quad-chick tier starts there), got {golden_chance}."
        )
    if friday_bonus < 0:
        errors.append("FRIDAY_BONUS must not be negative.")
    if (1 / 25) + friday_bonus > 1 or (1 / 6) + friday_bonus > 1:
        errors.append("FRIDAY_BONUS is too large — it pushes a hatch tier above 100%.")
    if max_throws < 1:
        errors.append("MAX_THROWS_PER_DAY must be at least 1.")
    if history_days < 1:
        errors.append("HISTORY_DAYS must be at least 1.")
    if streak_threshold < 1:
        errors.append("STREAK_ROLE_THRESHOLD must be at least 1.")
    return errors

_config_errors = validate_config()
if _config_errors:
    raise ValueError("Invalid egg-bot configuration:\n- " + "\n- ".join(_config_errors))

# ── Async DB bridge ────────────────────────────────────────────────────────────
# sqlite3 is synchronous. Every DB call in the command handlers below is
# awaited through run_db() so a disk write runs in a worker thread instead of
# blocking the event loop — and therefore every other command, in every
# guild, while it happens.
#
# All of those worker threads share one sqlite3.Connection (check_same_thread
# =False just disables Python's same-thread check — it does NOT make
# concurrent use from multiple threads safe; the sqlite3 docs put that
# responsibility on the caller). _db_lock serializes actual access to the
# connection so two commands running at once can never call it at the same
# time, regardless of how the deployment's SQLite build happens to be
# compiled.
_db_lock = threading.Lock()

async def run_db(func, *args, **kwargs):
    def _call():
        with _db_lock:
            return func(*args, **kwargs)
    return await asyncio.to_thread(_call)

# ── Locale → Timezone ─────────────────────────────────────────────────────────

LOCALE_TO_TIMEZONE = {
    "en-US": "America/New_York", "en-GB": "Europe/London",
    "de": "Europe/Berlin",       "fr": "Europe/Paris",
    "es-ES": "Europe/Madrid",    "es-419": "America/Mexico_City",
    "pt-BR": "America/Sao_Paulo","pt-PT": "Europe/Lisbon",
    "nl": "Europe/Amsterdam",    "it": "Europe/Rome",
    "pl": "Europe/Warsaw",       "ru": "Europe/Moscow",
    "uk": "Europe/Kiev",         "tr": "Europe/Istanbul",
    "sv-SE": "Europe/Stockholm", "da": "Europe/Copenhagen",
    "no": "Europe/Oslo",         "fi": "Europe/Helsinki",
    "cs": "Europe/Prague",       "hu": "Europe/Budapest",
    "ro": "Europe/Bucharest",    "bg": "Europe/Sofia",
    "hr": "Europe/Zagreb",       "lt": "Europe/Vilnius",
    "el": "Europe/Athens",       "ja": "Asia/Tokyo",
    "ko": "Asia/Seoul",          "zh-CN": "Asia/Shanghai",
    "zh-TW": "Asia/Taipei",      "th": "Asia/Bangkok",
    "vi": "Asia/Ho_Chi_Minh",    "id": "Asia/Jakarta",
    "hi": "Asia/Kolkata",        "ar": "Asia/Riyadh",
    "he": "Asia/Jerusalem",      "en-IN": "Asia/Kolkata",
}

# ── Database ──────────────────────────────────────────────────────────────────

_db_conn: sqlite3.Connection | None = None

def get_db() -> sqlite3.Connection:
    global _db_conn
    if _db_conn is None:
        _db_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _db_conn.row_factory = sqlite3.Row
        _db_conn.execute("PRAGMA journal_mode=WAL")
    return _db_conn

def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id        INTEGER PRIMARY KEY,
            timezone       TEXT    NOT NULL DEFAULT 'UTC',
            last_throw     TEXT,
            streak         INTEGER NOT NULL DEFAULT 0,
            longest_streak INTEGER NOT NULL DEFAULT 0,
            total_throws   INTEGER NOT NULL DEFAULT 0,
            total_hatches  INTEGER NOT NULL DEFAULT 0,
            reminders      INTEGER NOT NULL DEFAULT 0,
            throws_today   INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS history (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER NOT NULL,
            throw_date TEXT    NOT NULL,
            result     TEXT    NOT NULL,
            chicks     INTEGER NOT NULL DEFAULT 0
        );

        CREATE INDEX IF NOT EXISTS idx_history_user_date
            ON history(user_id, throw_date DESC);

        CREATE TABLE IF NOT EXISTS guild_settings (
            guild_id         INTEGER PRIMARY KEY,
            streak_threshold INTEGER
        );
    """)
    # Migrate existing DBs: add throws_today if missing
    try:
        conn.execute("ALTER TABLE users ADD COLUMN throws_today INTEGER NOT NULL DEFAULT 0")
        conn.commit()
    except Exception:
        pass  # Column already exists

def get_user(user_id: int) -> sqlite3.Row | None:
    return get_db().execute(
        "SELECT * FROM users WHERE user_id = ?", (user_id,)
    ).fetchone()

def upsert_user(user_id: int, **kwargs):
    conn = get_db()
    existing = conn.execute(
        "SELECT 1 FROM users WHERE user_id = ?", (user_id,)
    ).fetchone()
    if existing:
        sets = ", ".join(f"{k} = ?" for k in kwargs)
        conn.execute(
            f"UPDATE users SET {sets} WHERE user_id = ?",
            (*kwargs.values(), user_id)
        )
    else:
        kwargs["user_id"] = user_id
        cols = ", ".join(kwargs.keys())
        vals = ", ".join("?" * len(kwargs))
        conn.execute(
            f"INSERT INTO users ({cols}) VALUES ({vals})",
            tuple(kwargs.values())
        )
    conn.commit()

def add_history(user_id: int, throw_date: str, result: str, chicks: int):
    conn = get_db()
    conn.execute(
        "INSERT INTO history (user_id, throw_date, result, chicks) VALUES (?, ?, ?, ?)",
        (user_id, throw_date, result, chicks)
    )
    conn.commit()

def get_history(user_id: int, days: int = 7) -> list:
    return get_db().execute(
        "SELECT * FROM history WHERE user_id = ? ORDER BY throw_date DESC LIMIT ?",
        (user_id, days)
    ).fetchall()

def get_leaderboard(limit: int = 10) -> list:
    return get_db().execute(
        """SELECT user_id, total_hatches, total_throws, streak, longest_streak
           FROM users ORDER BY total_hatches DESC, streak DESC LIMIT ?""",
        (limit,)
    ).fetchall()

def get_reminder_users() -> list:
    return get_db().execute(
        "SELECT * FROM users WHERE reminders = 1"
    ).fetchall()

def get_guild_streak_threshold(guild_id: int) -> int:
    row = get_db().execute(
        "SELECT streak_threshold FROM guild_settings WHERE guild_id = ?", (guild_id,)
    ).fetchone()
    if row and row["streak_threshold"] is not None:
        return row["streak_threshold"]
    return STREAK_ROLE_THRESHOLD

def set_guild_streak_threshold(guild_id: int, threshold: int):
    conn = get_db()
    conn.execute(
        """INSERT INTO guild_settings (guild_id, streak_threshold) VALUES (?, ?)
           ON CONFLICT(guild_id) DO UPDATE SET streak_threshold = excluded.streak_threshold""",
        (guild_id, threshold),
    )
    conn.commit()

def reset_user_streak(user_id: int):
    conn = get_db()
    conn.execute("UPDATE users SET streak = 0 WHERE user_id = ?", (user_id,))
    conn.commit()

# ── Helpers ───────────────────────────────────────────────────────────────────

def guess_timezone(locale: str | None) -> tuple[str, bool]:
    if locale and locale in LOCALE_TO_TIMEZONE:
        return LOCALE_TO_TIMEZONE[locale], True
    return "UTC", False

def throw_egg(is_friday: bool = False) -> dict:
    roll = random.random()
    if roll < GOLDEN_EGG_CHANCE:
        return {"hatched": True, "chicks": 1, "golden": True}
    bonus = FRIDAY_BONUS if is_friday else 0
    if roll < (1 / 25) + bonus:
        return {"hatched": True, "chicks": 4, "golden": False}
    if roll < (1 / 6) + bonus:
        return {"hatched": True, "chicks": 1, "golden": False}
    return {"hatched": False, "chicks": 0, "golden": False}

def get_midnight_remaining(tz: ZoneInfo) -> str:
    now = datetime.now(tz)
    midnight = now.replace(hour=23, minute=59, second=59, microsecond=999999)
    total_seconds = int((midnight - now).total_seconds()) + 1
    hours, rem = divmod(total_seconds, 3600)
    minutes, seconds = divmod(rem, 60)
    parts = []
    if hours:   parts.append(f"{hours}h")
    if minutes: parts.append(f"{minutes}m")
    if seconds: parts.append(f"{seconds}s")
    return " ".join(parts) or "a moment"

def history_to_emoji(rows: list) -> str:
    icons = []
    for row in reversed(rows):
        if row["result"] == "golden":
            icons.append("🌟")
        elif row["result"] == "hatch":
            icons.append("🐥" if row["chicks"] == 1 else "🐥x4")
        else:
            icons.append("💀")
    return " ".join(icons) if icons else "No throws yet"

def streak_flavour(streak: int, threshold: int = STREAK_ROLE_THRESHOLD) -> str:
    if streak >= 30: return "\n🔥 **LEGENDARY** — 30+ day streak!"
    if streak >= 14: return "\n🔥 **ON FIRE** — two week streak!"
    if streak >= threshold:  return "\n🔥 **UNSTOPPABLE** — week-long streak!"
    if streak >= 3:  return f"\n🔥 **{streak} days in a row!**"
    return ""

def stats_milestone_text(streak: int, longest_streak: int, threshold: int) -> str:
    """A small forward-looking nudge shown on /eggstats."""
    if streak == 0:
        return "🥚 Throw today to start a new streak!"
    if streak < threshold:
        left = threshold - streak
        s = "s" if left != 1 else ""
        return f"🔥 {left} more day{s} to earn the streak role!"
    if streak < longest_streak:
        left = longest_streak - streak
        s = "s" if left != 1 else ""
        return f"🏆 {left} more day{s} to beat your best streak!"
    return "🎉 You're on your best streak ever — keep it going!"

# ── Shared embed styling ───────────────────────────────────────────────────────

class Colors:
    GOLD    = 0xF5C518   # golden egg
    SUCCESS = 0x57F287   # hatch
    INFO    = 0x5865F2   # neutral / info
    WARN    = 0xFFA347   # out of eggs
    MISS    = 0x4E5058   # splat (soft dark grey, matches Discord's dark theme)

def bot_footer_icon() -> str | None:
    return client.user.display_avatar.url if client.user else None

def streak_bar(streak: int, threshold: int = STREAK_ROLE_THRESHOLD) -> str:
    """A little 🔥/⚫ progress bar toward the streak-role threshold."""
    filled = min(streak, threshold)
    bar = "🔥" * filled + "⚫" * (threshold - filled)
    return f"{bar} (+{streak - threshold})" if streak > threshold else bar

async def assign_streak_role(guild: discord.Guild, member: discord.Member):
    role = discord.utils.get(guild.roles, name=STREAK_ROLE_NAME)
    if not role:
        try:
            role = await guild.create_role(
                name=STREAK_ROLE_NAME, color=discord.Color.gold()
            )
        except discord.Forbidden:
            return
    if role not in member.roles:
        try:
            await member.add_roles(role)
        except discord.Forbidden:
            pass

async def remove_streak_role(guild: discord.Guild, member: discord.Member):
    role = discord.utils.get(guild.roles, name=STREAK_ROLE_NAME)
    if role and role in member.roles:
        try:
            await member.remove_roles(role)
        except discord.Forbidden:
            pass

# ── Timezone UI ───────────────────────────────────────────────────────────────

COMMON_TIMEZONES = [
    ("UTC",                 "UTC (no offset)"),
    ("America/New_York",    "🇺🇸 Eastern (US)"),
    ("America/Chicago",     "🇺🇸 Central (US)"),
    ("America/Denver",      "🇺🇸 Mountain (US)"),
    ("America/Los_Angeles", "🇺🇸 Pacific (US)"),
    ("America/Sao_Paulo",   "🇧🇷 Brazil"),
    ("America/Mexico_City", "🇲🇽 Mexico"),
    ("Europe/London",       "🇬🇧 London"),
    ("Europe/Paris",        "🇫🇷 Paris / Berlin / Rome"),
    ("Europe/Moscow",       "🇷🇺 Moscow"),
    ("Europe/Istanbul",     "🇹🇷 Istanbul"),
    ("Asia/Karachi",        "🇵🇰 Karachi (PKT)"),
    ("Asia/Kolkata",        "🇮🇳 India (IST)"),
    ("Asia/Dhaka",          "🇧🇩 Dhaka"),
    ("Asia/Riyadh",         "🇸🇦 Riyadh"),
    ("Asia/Dubai",          "🇦🇪 Dubai"),
    ("Asia/Bangkok",        "🇹🇭 Bangkok / Jakarta"),
    ("Asia/Shanghai",       "🇨🇳 China"),
    ("Asia/Tokyo",          "🇯🇵 Tokyo / Seoul"),
    ("Asia/Ho_Chi_Minh",    "🇻🇳 Vietnam"),
    ("Australia/Sydney",    "🇦🇺 Sydney"),
    ("Pacific/Auckland",    "🇳🇿 Auckland"),
    ("Africa/Cairo",        "🇪🇬 Cairo"),
    ("Africa/Lagos",        "🇳🇬 Lagos"),
]

class TimezoneView(discord.ui.View):
    def __init__(self, user_id: int, current_tz: str):
        super().__init__(timeout=60)
        self.user_id = user_id
        options = [
            discord.SelectOption(label=label, value=tz, default=(tz == current_tz))
            for tz, label in COMMON_TIMEZONES
        ]
        self.select = discord.ui.Select(
            placeholder="Pick your timezone...", options=options
        )
        self.select.callback = self.on_select
        self.add_item(self.select)

    async def on_select(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message(
                "This isn't your menu!", ephemeral=True
            )
        chosen = self.select.values[0]
        await run_db(upsert_user, self.user_id, timezone=chosen)
        now_local = datetime.now(ZoneInfo(chosen))
        embed = discord.Embed(
            title="✅ Timezone Updated",
            description=(
                f"> 🌍 **{chosen}**\n"
                f"> 🕒 Local time right now: **{now_local.strftime('%I:%M %p')}**\n\n"
                f"You're all set — throw your egg with `/throwegg`!"
            ),
            color=Colors.SUCCESS,
        )
        embed.set_author(
            name=interaction.user.display_name,
            icon_url=interaction.user.display_avatar.url,
        )
        embed.set_footer(text="egg-bot", icon_url=bot_footer_icon())
        self.stop()
        await interaction.response.edit_message(embed=embed, view=None)

# ── /throwegg ─────────────────────────────────────────────────────────────────

@tree.command(
    name="throwegg",
    description="🥚 Throw an egg and find out if today will be a good or bad day!"
)
@app_commands.describe(private="Only show the result to you (default: off)")
async def throw_egg_command(interaction: discord.Interaction, private: bool = False):
    user_id = interaction.user.id
    row = await run_db(get_user, user_id)

    # Auto-detect timezone on first use
    if row is None:
        guessed_tz, was_guessed = guess_timezone(str(interaction.locale))
        await run_db(upsert_user, user_id, timezone=guessed_tz)
        row = await run_db(get_user, user_id)
        tz_obj = ZoneInfo(guessed_tz)
        now_local = datetime.now(tz_obj)
        notice = (
            f"> 🌍 Guessed as **{guessed_tz}** from your Discord language.\n"
            f"> 🕒 Your local time looks like **{now_local.strftime('%I:%M %p')}**\n\n"
            f"❌ Wrong? Fix it any time with `/mytimezone`."
        ) if was_guessed else (
            f"> 🌍 Couldn't detect it, so I defaulted to **UTC**.\n\n"
            f"❌ Not right? Fix it any time with `/mytimezone`."
        )
        hint = discord.Embed(
            title="🌍 Timezone Auto-Detected", description=notice, color=Colors.INFO
        )
        hint.set_footer(text="This only shows once", icon_url=bot_footer_icon())
        await interaction.response.send_message(embed=hint, ephemeral=True)

    tz = ZoneInfo(row["timezone"])
    now = datetime.now(tz)
    today_str = now.date().isoformat()
    is_friday = now.weekday() == 4

    # Reset throws_today counter if it's a new day
    throws_today = row["throws_today"] if row["last_throw"] == today_str else 0

    # All throws used up?
    if throws_today >= MAX_THROWS_PER_DAY:
        remaining = get_midnight_remaining(tz)
        embed = discord.Embed(
            title="🥚 No Eggs Left Today!",
            description=(
                f"You've used all **{MAX_THROWS_PER_DAY} eggs** for today.\n\n"
                f"⏳ Come back in **{remaining}**."
            ),
            color=Colors.WARN,
        )
        embed.set_thumbnail(url=interaction.user.display_avatar.url)
        embed.set_footer(
            text=f"{MAX_THROWS_PER_DAY} eggs per day • resets at midnight",
            icon_url=bot_footer_icon(),
        )
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    threshold = (
        await run_db(get_guild_streak_threshold, interaction.guild.id)
        if interaction.guild else STREAK_ROLE_THRESHOLD
    )

    # Throw!
    result  = throw_egg(is_friday)
    hatched = result["hatched"]
    golden  = result["golden"]
    chicks  = result["chicks"]
    name    = interaction.user.display_name

    # Streak calculation — only on first throw of the day
    yesterday = (now.date() - timedelta(days=1)).isoformat()
    if throws_today == 0:
        if hatched:
            new_streak = (row["streak"] + 1) if row["last_throw"] == yesterday else 1
        else:
            new_streak = 0
    else:
        new_streak = row["streak"]

    longest    = max(row["longest_streak"], new_streak)
    result_str = "golden" if golden else ("hatch" if hatched else "miss")

    await run_db(
        upsert_user,
        user_id,
        last_throw     = today_str,
        throws_today   = throws_today + 1,
        streak         = new_streak,
        longest_streak = longest,
        total_throws   = row["total_throws"] + 1,
        total_hatches  = row["total_hatches"] + (1 if hatched else 0),
    )
    await run_db(add_history, user_id, today_str, result_str, chicks)

    # Role reward — based on the *current* streak value, not this particular
    # throw's outcome. Without this, a miss on someone's 2nd or 3rd throw of
    # the day stripped their streak role even though the streak itself
    # (which only changes on the day's first throw) was untouched.
    if interaction.guild:
        member = interaction.guild.get_member(user_id)
        if member:
            if new_streak >= threshold:
                await assign_streak_role(interaction.guild, member)
            else:
                await remove_streak_role(interaction.guild, member)

    friday_note = "\n\n🎉 *Friday bonus active — slightly better odds today!*" if is_friday else ""

    if golden:
        odds = round(1 / GOLDEN_EGG_CHANCE) if GOLDEN_EGG_CHANCE > 0 else "∞"
        embed = discord.Embed(
            title="🌟 A GOLDEN EGG!",
            description=(
                f"> 🌟 **1-in-{odds} miracle.** Today will be absolutely **extraordinary**.\n\n"
                f"*Fortune favors the bold — and today, that's you.*{friday_note}"
            ),
            color=Colors.GOLD,
        )
    elif hatched and chicks == 4:
        embed = discord.Embed(
            title="🥚💥 CRACK! Four chicks hatched!",
            description=(
                f"> 🐥🐥🐥🐥 **Four chicks at once!** Incredible odds.\n\n"
                f"✨ **Today is going to be an AMAZING day!** ✨\n"
                f"*The stars have truly aligned. Go conquer the world!*"
                f"{streak_flavour(new_streak, threshold)}{friday_note}"
            ),
            color=Colors.SUCCESS,
        )
    elif hatched:
        embed = discord.Embed(
            title="🥚✨ CRACK! The egg hatches!",
            description=(
                f"> 🐥 **A chick hatched!**\n\n"
                f"✨ **Today is going to be a GREAT day!** ✨\n"
                f"*Good fortune smiles upon you. Seize the day!*"
                f"{streak_flavour(new_streak, threshold)}{friday_note}"
            ),
            color=Colors.GOLD,
        )
    else:
        embed = discord.Embed(
            title="🥚💦 Splat. Just egg on the floor.",
            description=(
                f"> 💀 No chick today...\n\n"
                f"**Brace yourself — rough day ahead.**\n"
                f"*The egg has spoken. Stay cautious and survive.*{friday_note}"
            ),
            color=Colors.MISS,
        )

    embed.set_author(name=name, icon_url=interaction.user.display_avatar.url)
    embed.set_thumbnail(url=interaction.user.display_avatar.url)

    hist = await run_db(get_history, user_id, HISTORY_DAYS)
    embed.add_field(
        name=f"📅 Last {HISTORY_DAYS} Days", value=history_to_emoji(hist), inline=False
    )
    embed.add_field(name="🔥 Streak", value=f"**{new_streak}**\n{streak_bar(new_streak, threshold)}", inline=True)
    embed.add_field(name="🏆 Best",   value=f"**{longest}**",    inline=True)
    embed.add_field(
        name="📊 Hatch Rate",
        value=f"**{row['total_hatches'] + (1 if hatched else 0)}**/{row['total_throws'] + 1}",
        inline=True,
    )
    throws_left = MAX_THROWS_PER_DAY - (throws_today + 1)
    if throws_left > 0:
        s = "s" if throws_left != 1 else ""
        footer_txt = f"🥚 {throws_left} egg{s} left today • resets at midnight"
    else:
        footer_txt = "🥚 No eggs left today • resets at midnight"
    embed.set_footer(text=footer_txt, icon_url=bot_footer_icon())
    embed.timestamp = now

    if interaction.response.is_done():
        await interaction.followup.send(embed=embed, ephemeral=private)
    else:
        await interaction.response.send_message(embed=embed, ephemeral=private)

# ── /eggstats ─────────────────────────────────────────────────────────────────

@tree.command(name="eggstats", description="📊 See your egg throwing stats.")
@app_commands.describe(user="Whose stats to check (leave blank for yours)")
async def egg_stats(
    interaction: discord.Interaction, user: discord.Member = None
):
    target = user or interaction.user
    row = await run_db(get_user, target.id)

    if not row or row["total_throws"] == 0:
        msg = (
            "You have" if not user else f"{target.display_name} has"
        ) + " never thrown an egg!\nUse `/throwegg` to start."
        return await interaction.response.send_message(msg, ephemeral=True)

    rate = row["total_hatches"] / row["total_throws"] * 100
    hist = await run_db(get_history, target.id, HISTORY_DAYS)
    threshold = (
        await run_db(get_guild_streak_threshold, interaction.guild.id)
        if interaction.guild else STREAK_ROLE_THRESHOLD
    )

    embed = discord.Embed(title="📊 Egg Report Card", color=Colors.INFO)
    embed.set_author(name=target.display_name, icon_url=target.display_avatar.url)
    embed.set_thumbnail(url=target.display_avatar.url)

    embed.add_field(name="🥚 Total Throws",  value=f"**{row['total_throws']}**",  inline=True)
    embed.add_field(name="🐥 Total Hatches", value=f"**{row['total_hatches']}**", inline=True)
    embed.add_field(name="📈 Hatch Rate",    value=f"**{rate:.1f}%**",            inline=True)

    embed.add_field(
        name="🔥 Current Streak",
        value=f"**{row['streak']}**\n{streak_bar(row['streak'], threshold)}",
        inline=True,
    )
    embed.add_field(name="🏆 Best Streak", value=f"**{row['longest_streak']}**", inline=True)
    if target.id == interaction.user.id:
        embed.add_field(name="🌍 Timezone", value=f"**{row['timezone']}**", inline=True)

    embed.add_field(
        name=f"📅 Last {HISTORY_DAYS} Days", value=history_to_emoji(hist), inline=False
    )
    embed.add_field(
        name="🎯 Next Goal",
        value=stats_milestone_text(row["streak"], row["longest_streak"], threshold),
        inline=False,
    )
    embed.set_footer(text="egg-bot", icon_url=bot_footer_icon())
    await interaction.response.send_message(embed=embed)

# ── /eggleaderboard ───────────────────────────────────────────────────────────

@tree.command(name="eggleaderboard", description="🏆 Server egg leaderboard.")
async def egg_leaderboard(interaction: discord.Interaction):
    if not interaction.guild:
        return await interaction.response.send_message(
            "This command only works in a server.", ephemeral=True
        )

    rows = await run_db(get_leaderboard, LEADERBOARD_POOL_SIZE)
    if not rows:
        return await interaction.response.send_message(
            "No throws yet!", ephemeral=True
        )

    lines = []
    medals = ["🥇", "🥈", "🥉"]
    rank = 0
    for row in rows:
        member = interaction.guild.get_member(row["user_id"])
        if not member:
            continue
        rank += 1
        medal = medals[rank - 1] if rank <= 3 else f"`#{rank}`"
        rate  = f"{row['total_hatches']}/{row['total_throws']}"
        lines.append(
            f"{medal} **{member.display_name}**\n"
            f"　　🐥 {row['total_hatches']} hatches ({rate}) · 🔥 {row['streak']} streak"
        )
        if rank >= 10:
            break

    if not lines:
        return await interaction.response.send_message(
            "No throws yet in this server!", ephemeral=True
        )

    embed = discord.Embed(
        title="🏆 Egg Leaderboard",
        description="\n\n".join(lines),
        color=Colors.GOLD,
    )
    embed.set_author(name=interaction.guild.name, icon_url=(
        interaction.guild.icon.url if interaction.guild.icon else None
    ))
    if interaction.guild.icon:
        embed.set_thumbnail(url=interaction.guild.icon.url)
    embed.set_footer(
        text="Ranked by total hatches • server members only", icon_url=bot_footer_icon()
    )
    await interaction.response.send_message(embed=embed)

# ── /eggreminder ──────────────────────────────────────────────────────────────

@tree.command(
    name="eggreminder",
    description="🔔 Toggle daily DM reminders when your egg resets."
)
async def egg_reminder(interaction: discord.Interaction):
    user_id = interaction.user.id
    row = await run_db(get_user, user_id)
    current = row["reminders"] if row else 0
    new_val = 0 if current else 1
    await run_db(upsert_user, user_id, reminders=new_val)
    status = "**enabled** 🔔" if new_val else "**disabled** 🔕"
    embed = discord.Embed(
        title="🔔 Daily Reminders" if new_val else "🔕 Daily Reminders",
        description=(
            f"Daily egg reminders are now {status}.\n\n"
            + (
                "I'll DM you each day once your egg is ready to throw again!"
                if new_val
                else "No more reminders — turn them back on any time with `/eggreminder`."
            )
        ),
        color=Colors.SUCCESS if new_val else Colors.MISS,
    )
    embed.set_footer(text="egg-bot", icon_url=bot_footer_icon())
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ── /mytimezone ───────────────────────────────────────────────────────────────

@tree.command(name="mytimezone", description="🌍 Check or change your timezone.")
async def my_timezone(interaction: discord.Interaction):
    row = await run_db(get_user, interaction.user.id)
    current = row["timezone"] if row else "UTC"
    embed = discord.Embed(
        title="🌍 Your Timezone",
        description=f"> Currently set to **{current}**\n\nPick a different one below if it's wrong.",
        color=Colors.INFO,
    )
    embed.set_author(name=interaction.user.display_name, icon_url=interaction.user.display_avatar.url)
    embed.set_footer(text="egg-bot", icon_url=bot_footer_icon())
    view = TimezoneView(interaction.user.id, current)
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

# ── /eggadmin ─────────────────────────────────────────────────────────────────

eggadmin_group = app_commands.Group(
    name="eggadmin",
    description="🛠️ Server admin controls for egg-bot.",
    default_permissions=discord.Permissions(manage_guild=True),
    guild_only=True,
)

@eggadmin_group.command(
    name="resetstreak", description="Reset a member's current streak to 0."
)
@app_commands.describe(member="The member whose streak to reset")
async def eggadmin_resetstreak(interaction: discord.Interaction, member: discord.Member):
    await run_db(reset_user_streak, member.id)
    await remove_streak_role(interaction.guild, member)
    embed = discord.Embed(
        title="🔄 Streak Reset",
        description=f"{member.mention}'s streak has been reset to **0**.\n"
                     f"Their best-streak record is kept.",
        color=Colors.INFO,
    )
    embed.set_footer(text="egg-bot", icon_url=bot_footer_icon())
    await interaction.response.send_message(embed=embed, ephemeral=True)

@eggadmin_group.command(
    name="setthreshold",
    description="Set how many days in a row earn the streak role in this server.",
)
@app_commands.describe(days="Number of consecutive days required (1-365)")
async def eggadmin_setthreshold(
    interaction: discord.Interaction, days: app_commands.Range[int, 1, 365]
):
    await run_db(set_guild_streak_threshold, interaction.guild.id, days)
    embed = discord.Embed(
        title="🛠️ Streak Threshold Updated",
        description=(
            f"The streak role in **{interaction.guild.name}** now requires "
            f"**{days}** day{'s' if days != 1 else ''} in a row.\n\n"
            f"This overrides the bot-wide default (currently "
            f"**{STREAK_ROLE_THRESHOLD}**) for this server only."
        ),
        color=Colors.INFO,
    )
    embed.set_footer(text="egg-bot", icon_url=bot_footer_icon())
    await interaction.response.send_message(embed=embed, ephemeral=True)

@eggadmin_group.command(
    name="reminderstatus",
    description="See how many members in this server have daily reminders on.",
)
async def eggadmin_reminderstatus(interaction: discord.Interaction):
    users = await run_db(get_reminder_users)
    count = sum(1 for row in users if interaction.guild.get_member(row["user_id"]))
    embed = discord.Embed(
        title="🔔 Reminder Status",
        description=f"**{count}** member{'s' if count != 1 else ''} in this server "
                     f"currently have daily reminders enabled.",
        color=Colors.INFO,
    )
    embed.set_footer(text="egg-bot", icon_url=bot_footer_icon())
    await interaction.response.send_message(embed=embed, ephemeral=True)

tree.add_command(eggadmin_group)

# ── Reminder loop ─────────────────────────────────────────────────────────────

_reminded: set[int] = set()

async def reminder_loop():
    await client.wait_until_ready()
    while not client.is_closed():
        users = await run_db(get_reminder_users)
        for row in users:
            try:
                tz = ZoneInfo(row["timezone"])
                now = datetime.now(tz)
                today_str = now.date().isoformat()

                in_window       = now.hour == 0 and now.minute < 10
                already_threw   = row["last_throw"] == today_str
                already_reminded = row["user_id"] in _reminded

                if in_window and not already_threw and not already_reminded:
                    _reminded.add(row["user_id"])
                    user = await client.fetch_user(row["user_id"])
                    embed = discord.Embed(
                        title="🥚 Your Egg Has Reset!",
                        description=(
                            f"> 🌍 It's a new day in **{row['timezone']}**.\n\n"
                            f"Go throw your egg with `/throwegg`! 🐥"
                        ),
                        color=Colors.SUCCESS,
                    )
                    embed.set_footer(text="egg-bot", icon_url=bot_footer_icon())
                    await user.send(embed=embed)

                elif not in_window and row["user_id"] in _reminded:
                    _reminded.discard(row["user_id"])

            except Exception:
                pass
        await asyncio.sleep(300)

# ── Global error handler ───────────────────────────────────────────────────────
# Without this, any unhandled exception inside a slash command just shows the
# user "This interaction failed" with nothing logged anywhere.

@tree.error
async def on_app_command_error(
    interaction: discord.Interaction, error: app_commands.AppCommandError
):
    if isinstance(error, app_commands.CommandOnCooldown):
        message = f"⏳ Slow down! Try again in {error.retry_after:.1f}s."
    elif isinstance(error, app_commands.MissingPermissions):
        message = "🚫 You don't have permission to do that."
    else:
        log.exception(
            "Unhandled error in /%s", getattr(interaction.command, "name", "?"),
            exc_info=error,
        )
        message = "⚠️ Something went wrong running that command. Please try again."

    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except discord.HTTPException:
        pass

# ── Startup ───────────────────────────────────────────────────────────────────

_started = False

@client.event
async def on_ready():
    global _started
    if _started:
        # on_ready can fire again after a reconnect; guard against
        # re-syncing commands and spawning a second reminder_loop task.
        log.info("Reconnected as %s.", client.user)
        return
    _started = True

    await run_db(init_db)
    await tree.sync()
    asyncio.create_task(reminder_loop())
    log.info("Logged in as %s — all systems go!", client.user)

# ── Graceful shutdown ───────────────────────────────────────────────────────────
# `docker stop` sends SIGTERM, and Python has NO default handling for it —
# unlike SIGINT (Ctrl+C), which Python turns into a KeyboardInterrupt on its
# own, an unhandled SIGTERM kills the process immediately with zero cleanup.
# We register an explicit handler for both signals so the gateway connection
# is closed cleanly and the DB connection is closed instead of just cut off.

async def main():
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise ValueError("Missing DISCORD_TOKEN in .env file or environment.")

    loop = asyncio.get_running_loop()

    async def _shutdown(sig_name: str):
        log.info("Received %s — shutting down gracefully...", sig_name)
        await client.close()

    for sig, name in ((signal.SIGINT, "SIGINT"), (signal.SIGTERM, "SIGTERM")):
        try:
            loop.add_signal_handler(sig, lambda n=name: asyncio.create_task(_shutdown(n)))
        except NotImplementedError:
            pass  # e.g. Windows, which doesn't support add_signal_handler

    try:
        async with client:
            await client.start(token)
    finally:
        if _db_conn is not None:
            log.info("Closing database connection...")
            _db_conn.close()
        log.info("Shutdown complete.")

if __name__ == "__main__":
    asyncio.run(main())
