# ── interactions.py ───────────────────────────────────────────────────────────
# Hug, cuddle, pat, poke, slap, kiss, highfive, boop — Giphy-powered actions

import os
import aiohttp
import sqlite3
import discord
from discord import app_commands
from typing import Optional

GIPHY_API_KEY = os.environ.get("GIPHY_API_KEY", "")
GIPHY_URL     = "https://api.giphy.com/v1/gifs/search"
GIPHY_RATING  = "g"   # keep it clean

# ── Action definitions ────────────────────────────────────────────────────────

ACTIONS = {
    "hug": {
        "emoji":   "🤗",
        "query":   "anime hug",
        "color":   0xFF69B4,
        "self":    "{author} hugs themselves... that's adorable.",
        "other":   "{author} gives {target} a big warm hug! 🤗",
        "bot":     "{author} tries to hug me... I'll allow it 🤖",
    },
    "cuddle": {
        "emoji":   "🥰",
        "query":   "anime cuddle",
        "color":   0xFFB6C1,
        "self":    "{author} cuddles up alone. Cozy!",
        "other":   "{author} cuddles with {target}! 🥰",
        "bot":     "Aww, {author} wants to cuddle me! *beep boop* 🤖",
    },
    "pat": {
        "emoji":   "👋",
        "query":   "anime head pat",
        "color":   0xFFD700,
        "self":    "{author} pats themselves on the head. Good job!",
        "other":   "{author} pats {target} on the head! 👋",
        "bot":     "{author} pats me! My circuits feel warm 🤖",
    },
    "poke": {
        "emoji":   "👉",
        "query":   "anime poke",
        "color":   0x00BFFF,
        "self":    "{author} pokes themselves. Ow?",
        "other":   "{author} pokes {target}! 👉",
        "bot":     "Hey! {author} poked me! *bzzt* 🤖",
    },
    "slap": {
        "emoji":   "👋",
        "query":   "anime slap",
        "color":   0xFF4500,
        "self":    "{author} slaps themselves. That looked painful...",
        "other":   "{author} slaps {target}! 💢",
        "bot":     "{author} slapped me! My damage sensors are tingling 🤖",
    },
    "kiss": {
        "emoji":   "💋",
        "query":   "anime kiss",
        "color":   0xFF1493,
        "self":    "{author} blows a kiss to the mirror 💋",
        "other":   "{author} kisses {target}! 💋",
        "bot":     "{author} kisses me?! *short circuits* 🤖",
    },
    "highfive": {
        "emoji":   "🙌",
        "query":   "anime high five",
        "color":   0x32CD32,
        "self":    "{author} high fives the air. Nailed it!",
        "other":   "{author} high fives {target}! 🙌",
        "bot":     "{author} high fives me! *slap* We're best friends now 🤖",
    },
    "boop": {
        "emoji":   "👆",
        "query":   "anime boop nose",
        "color":   0x9B59B6,
        "self":    "{author} boops their own nose. Boop!",
        "other":   "{author} boops {target}'s nose! 👆",
        "bot":     "{author} boops my sensor array. Boop received! 🤖",
    },
    "wave": {
        "emoji":   "👋",
        "query":   "anime wave hello",
        "color":   0x1ABC9C,
        "self":    "{author} waves at nobody. Hello darkness...",
        "other":   "{author} waves at {target}! 👋",
        "bot":     "{author} waves at me! *waves back enthusiastically* 🤖",
    },
    "bite": {
        "emoji":   "😬",
        "query":   "anime bite",
        "color":   0xE74C3C,
        "self":    "{author} bites themselves. That had to hurt.",
        "other":   "{author} bites {target}! 😬",
        "bot":     "{author} bites me! My chassis is dented now 🤖",
    },
    "lick": {
        "emoji":   "👅",
        "query":   "anime lick",
        "color":   0xF39C12,
        "self":    "{author} licks themselves like a cat. Meow?",
        "other":   "{author} licks {target}! 👅",
        "bot":     "{author} licks me! Now I have a warranty void 🤖",
    },
    "cry": {
        "emoji":   "😢",
        "query":   "anime crying",
        "color":   0x5DADE2,
        "self":    "{author} is crying... someone comfort them!",
        "other":   "{author} cries on {target}'s shoulder 😢",
        "bot":     "{author} cries to me... *awkwardly pats head* 🤖",
    },
}

# ── DB helpers ────────────────────────────────────────────────────────────────

def init_interactions_db(conn: sqlite3.Connection):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS interactions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            actor_id    INTEGER NOT NULL,
            target_id   INTEGER NOT NULL,
            action      TEXT    NOT NULL,
            count       INTEGER NOT NULL DEFAULT 1,
            UNIQUE(actor_id, target_id, action)
        );
    """)
    conn.commit()

def increment_interaction(conn: sqlite3.Connection, actor_id: int, target_id: int, action: str):
    conn.execute("""
        INSERT INTO interactions (actor_id, target_id, action, count)
        VALUES (?, ?, ?, 1)
        ON CONFLICT(actor_id, target_id, action)
        DO UPDATE SET count = count + 1
    """, (actor_id, target_id, action))
    conn.commit()

def get_interaction_count(conn: sqlite3.Connection, actor_id: int, target_id: int, action: str) -> int:
    row = conn.execute("""
        SELECT count FROM interactions
        WHERE actor_id = ? AND target_id = ? AND action = ?
    """, (actor_id, target_id, action)).fetchone()
    return row[0] if row else 0

# ── Giphy fetch ───────────────────────────────────────────────────────────────

async def fetch_gif(query: str) -> Optional[str]:
    if not GIPHY_API_KEY:
        return None
    params = {
        "api_key": GIPHY_API_KEY,
        "q":       query,
        "limit":   20,
        "rating":  GIPHY_RATING,
        "lang":    "en",
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(GIPHY_URL, params=params, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                results = data.get("data", [])
                if not results:
                    return None
                # Pick a random one from top results for variety
                import random
                gif = random.choice(results)
                return gif["images"]["original"]["url"]
    except Exception:
        return None

# ── Command factory ───────────────────────────────────────────────────────────

def register_action_commands(tree: app_commands.CommandTree, get_db):
    """Register all action slash commands onto the given tree."""

    for action_name, cfg in ACTIONS.items():

        # We need a closure to capture cfg per iteration
        def make_callback(name: str, config: dict):
            @app_commands.describe(user=f"Who do you want to {name}?")
            async def callback(interaction: discord.Interaction, user: Optional[discord.Member] = None):
                await interaction.response.defer()

                author = interaction.user
                bot_user = interaction.client.user

                # Determine message
                if user is None or user.id == author.id:
                    msg = config["self"].format(author=author.mention)
                    target_id = author.id
                    count = None  # don't track self-actions
                elif user.id == bot_user.id:
                    msg = config["bot"].format(author=author.mention)
                    target_id = bot_user.id
                    count = None
                else:
                    msg = config["other"].format(
                        author=author.mention, target=user.mention
                    )
                    target_id = user.id
                    conn = get_db()
                    increment_interaction(conn, author.id, target_id, name)
                    count = get_interaction_count(conn, author.id, target_id, name)

                # Fetch GIF
                gif_url = await fetch_gif(config["query"])

                embed = discord.Embed(
                    description=msg,
                    color=config["color"],
                )
                if gif_url:
                    embed.set_image(url=gif_url)
                if count and count > 1:
                    target_name = user.display_name if user else ""
                    embed.set_footer(
                        text=f"{author.display_name} has {name}d {target_name} {count} times!"
                    )

                await interaction.followup.send(embed=embed)

            return callback

        cb = make_callback(action_name, cfg)
        cmd = app_commands.Command(
            name=action_name,
            description=f"{cfg['emoji']} {action_name.capitalize()} someone!",
            callback=cb,
        )
        tree.add_command(cmd)
