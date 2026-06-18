import os
import random
import sqlite3
import asyncio
import aiohttp
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

load_dotenv()

import discord
from discord import app_commands

# ── Intents ───────────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.members = True
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# ── Constants ─────────────────────────────────────────────────────────────────

DB_PATH           = "egg_bot.db"
STREAK_ROLE_NAME  = "🐣 Egg Streak Legend"
HISTORY_DAYS      = 7
GOLDEN_EGG_CHANCE  = 1 / 75
FRIDAY_BONUS       = 0.07
MAX_THROWS_PER_DAY = 3

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

def streak_flavour(streak: int) -> str:
    if streak >= 30: return "\n🔥 **LEGENDARY** — 30+ day streak!"
    if streak >= 14: return "\n🔥 **ON FIRE** — two week streak!"
    if streak >= 7:  return "\n🔥 **UNSTOPPABLE** — week-long streak!"
    if streak >= 3:  return f"\n🔥 **{streak} days in a row!**"
    return ""

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
        upsert_user(self.user_id, timezone=chosen)
        now_local = datetime.now(ZoneInfo(chosen))
        embed = discord.Embed(
            title="✅ Timezone updated!",
            description=(
                f"Set to **{chosen}**.\n"
                f"Your local time: **{now_local.strftime('%I:%M %p')}**\n\n"
                f"Throw your egg with `/throwegg`!"
            ),
            color=0x57F287,
        )
        self.stop()
        await interaction.response.edit_message(embed=embed, view=None)

# ── /throwegg ─────────────────────────────────────────────────────────────────

@tree.command(
    name="throwegg",
    description="🥚 Throw an egg and find out if today will be a good or bad day!"
)
async def throw_egg_command(interaction: discord.Interaction):
    user_id = interaction.user.id
    row = get_user(user_id)

    # Auto-detect timezone on first use
    if row is None:
        guessed_tz, was_guessed = guess_timezone(str(interaction.locale))
        upsert_user(user_id, timezone=guessed_tz)
        row = get_user(user_id)
        tz_obj = ZoneInfo(guessed_tz)
        now_local = datetime.now(tz_obj)
        notice = (
            f"I guessed your timezone as **{guessed_tz}** from your Discord language.\n"
            f"Your local time looks like **{now_local.strftime('%I:%M %p')}**.\n\n"
            f"❌ **Wrong?** Use `/mytimezone` to fix it."
        ) if was_guessed else (
            f"Couldn't detect your timezone, defaulted to **UTC**.\n"
            f"❌ **Not UTC?** Use `/mytimezone` to fix it."
        )
        hint = discord.Embed(
            title="🌍 Timezone Auto-Detected", description=notice, color=0x5865F2
        )
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
            title="🥚 No eggs left today!",
            description=(
                f"You've used all **{MAX_THROWS_PER_DAY} eggs** for today.\n"
                f"Come back in **{remaining}**!"
            ),
            color=0xFFA500,
        )
        embed.set_footer(text=f"{MAX_THROWS_PER_DAY} eggs per day — resets at midnight!")
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)
        return

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

    upsert_user(
        user_id,
        last_throw     = today_str,
        throws_today   = throws_today + 1,
        streak         = new_streak,
        longest_streak = longest,
        total_throws   = row["total_throws"] + 1,
        total_hatches  = row["total_hatches"] + (1 if hatched else 0),
    )
    add_history(user_id, today_str, result_str, chicks)

    # Role reward
    if interaction.guild:
        member = interaction.guild.get_member(user_id)
        if member:
            if hatched and new_streak >= 7:
                await assign_streak_role(interaction.guild, member)
            elif not hatched:
                await remove_streak_role(interaction.guild, member)

    friday_note = "\n🎉 *Friday bonus active — slightly better odds today!*" if is_friday else ""

    if golden:
        embed = discord.Embed(
            title="🌟 A GOLDEN EGG!",
            description=(
                f"**{name}, you found a golden egg!!**\n\n"
                f"🌟 This is a 1-in-100 miracle. Today will be absolutely **extraordinary**.\n\n"
                f"*Fortune favors the bold — and today, that's you.*{friday_note}"
            ),
            color=0xFFD700,
        )
    elif hatched and chicks == 4:
        embed = discord.Embed(
            title="🥚 *CRACK!* FOUR chicks!",
            description=(
                f"🐥🐥🐥🐥 **Four chicks hatched!** Incredible!\n\n"
                f"✨ **{name}, today is going to be an AMAZING day!** ✨\n\n"
                f"*The stars have truly aligned. Go conquer the world!*"
                f"{streak_flavour(new_streak)}{friday_note}"
            ),
            color=0x57F287,
        )
    elif hatched:
        embed = discord.Embed(
            title="🥚 *CRACK!* The egg hatches!",
            description=(
                f"🐥 **A chick hatched!**\n\n"
                f"✨ **{name}, today is going to be a GREAT day!** ✨\n\n"
                f"*Good fortune smiles upon you. Seize the day!*"
                f"{streak_flavour(new_streak)}{friday_note}"
            ),
            color=0xFFD700,
        )
    else:
        embed = discord.Embed(
            title="🥚 *Splat.* Just egg on the floor.",
            description=(
                f"No chick today...\n\n"
                f"💀 **{name}, brace yourself — rough day ahead.**\n\n"
                f"*The egg has spoken. Stay cautious and survive.*{friday_note}"
            ),
            color=0x808080,
        )

    hist = get_history(user_id, HISTORY_DAYS)
    embed.add_field(
        name=f"Last {HISTORY_DAYS} days", value=history_to_emoji(hist), inline=False
    )
    embed.add_field(name="🔥 Streak", value=str(new_streak), inline=True)
    embed.add_field(name="🏆 Best",   value=str(longest),    inline=True)
    embed.add_field(
        name="📊 Rate",
        value=f"{row['total_hatches'] + (1 if hatched else 0)}/{row['total_throws'] + 1}",
        inline=True,
    )
    throws_left = MAX_THROWS_PER_DAY - (throws_today + 1)
    if throws_left > 0:
        s = "s" if throws_left != 1 else ""
        footer_txt = f"{throws_left} egg{s} left today • Resets at midnight"
    else:
        footer_txt = "No eggs left today • Resets at midnight"
    embed.set_footer(text=footer_txt)
    embed.timestamp = now

    if interaction.response.is_done():
        await interaction.followup.send(embed=embed)
    else:
        await interaction.response.send_message(embed=embed)

# ── /eggstats ─────────────────────────────────────────────────────────────────

@tree.command(name="eggstats", description="📊 See your egg throwing stats.")
@app_commands.describe(user="Whose stats to check (leave blank for yours)")
async def egg_stats(
    interaction: discord.Interaction, user: discord.Member = None
):
    target = user or interaction.user
    row = get_user(target.id)

    if not row or row["total_throws"] == 0:
        msg = (
            "You have" if not user else f"{target.display_name} has"
        ) + " never thrown an egg!\nUse `/throwegg` to start."
        return await interaction.response.send_message(msg, ephemeral=True)

    rate = row["total_hatches"] / row["total_throws"] * 100
    hist = get_history(target.id, HISTORY_DAYS)

    embed = discord.Embed(
        title=f"📊 {target.display_name}'s Egg Stats", color=0x5865F2
    )
    embed.add_field(name="🥚 Total Throws",   value=str(row["total_throws"]),   inline=True)
    embed.add_field(name="🐥 Total Hatches",  value=str(row["total_hatches"]),  inline=True)
    embed.add_field(name="📈 Hatch Rate",     value=f"{rate:.1f}%",             inline=True)
    embed.add_field(name="🔥 Current Streak", value=str(row["streak"]),         inline=True)
    embed.add_field(name="🏆 Best Streak",    value=str(row["longest_streak"]), inline=True)
    if target.id == interaction.user.id:
        embed.add_field(name="🌍 Timezone", value=row["timezone"], inline=True)
    embed.add_field(
        name=f"Last {HISTORY_DAYS} days", value=history_to_emoji(hist), inline=False
    )
    embed.set_thumbnail(url=target.display_avatar.url)
    await interaction.response.send_message(embed=embed)

# ── /eggleaderboard ───────────────────────────────────────────────────────────

@tree.command(name="eggleaderboard", description="🏆 Server egg leaderboard.")
async def egg_leaderboard(interaction: discord.Interaction):
    if not interaction.guild:
        return await interaction.response.send_message(
            "This command only works in a server.", ephemeral=True
        )

    rows = get_leaderboard(50)
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
        medal = medals[rank - 1] if rank <= 3 else f"`{rank}.`"
        rate  = f"{row['total_hatches']}/{row['total_throws']}"
        lines.append(
            f"{medal} **{member.display_name}** — "
            f"{row['total_hatches']} hatches ({rate}) • 🔥 {row['streak']} streak"
        )
        if rank >= 10:
            break

    if not lines:
        return await interaction.response.send_message(
            "No throws yet in this server!", ephemeral=True
        )

    embed = discord.Embed(
        title="🏆 Egg Leaderboard",
        description="\n".join(lines),
        color=0xFFD700,
    )
    embed.set_footer(text="Ranked by total hatches • server members only")
    await interaction.response.send_message(embed=embed)

# ── /eggreminder ──────────────────────────────────────────────────────────────

@tree.command(
    name="eggreminder",
    description="🔔 Toggle daily DM reminders when your egg resets."
)
async def egg_reminder(interaction: discord.Interaction):
    user_id = interaction.user.id
    row = get_user(user_id)
    current = row["reminders"] if row else 0
    new_val = 0 if current else 1
    upsert_user(user_id, reminders=new_val)
    status = "**enabled** 🔔" if new_val else "**disabled** 🔕"
    await interaction.response.send_message(
        f"Daily egg reminders {status}.\n"
        + (
            "I'll DM you each day when your egg is ready!"
            if new_val
            else "No more reminders."
        ),
        ephemeral=True,
    )

# ── /mytimezone ───────────────────────────────────────────────────────────────

@tree.command(name="mytimezone", description="🌍 Check or change your timezone.")
async def my_timezone(interaction: discord.Interaction):
    row = get_user(interaction.user.id)
    current = row["timezone"] if row else "UTC"
    embed = discord.Embed(
        title="🌍 Your Timezone",
        description=f"Currently: **{current}**\n\nPick a different one below if it's wrong.",
        color=0x5865F2,
    )
    view = TimezoneView(interaction.user.id, current)
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

# ── Reminder loop ─────────────────────────────────────────────────────────────

_reminded: set[int] = set()

async def reminder_loop():
    await client.wait_until_ready()
    while not client.is_closed():
        users = get_reminder_users()
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
                        title="🥚 Your egg has reset!",
                        description=(
                            f"It's a new day in **{row['timezone']}**.\n\n"
                            f"Go throw your egg with `/throwegg`! 🐥"
                        ),
                        color=0x57F287,
                    )
                    await user.send(embed=embed)

                elif not in_window and row["user_id"] in _reminded:
                    _reminded.discard(row["user_id"])

            except Exception:
                pass
        await asyncio.sleep(300)

# ── Startup ───────────────────────────────────────────────────────────────────

@client.event
async def on_ready():
    init_db()
    from interactions import init_interactions_db, register_action_commands
    init_interactions_db(get_db())
    register_action_commands(tree, get_db)
    await tree.sync()
    asyncio.create_task(reminder_loop())
    print(f"✅ Logged in as {client.user} — all systems go!")

if __name__ == "__main__":
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise ValueError("Missing DISCORD_TOKEN in .env file or environment.")
    client.run(token)
