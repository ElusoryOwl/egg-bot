# 🥚 egg-bot

A Discord bot game: throw an egg once (or a few times) a day for a chance to
hatch chicks, build up a streak, and climb your server's leaderboard.

## Commands

| Command | Description |
|---|---|
| `/throwegg [private]` | Throw today's egg(s). Up to `MAX_THROWS_PER_DAY` per day. Pass `private: true` to only show the result to yourself. |
| `/eggstats [user]` | See your (or someone else's) throwing stats, recent history, and next milestone. |
| `/eggleaderboard` | Server-wide leaderboard, ranked by hatches then streak. |
| `/eggreminder` | Toggle a daily DM reminder if you haven't thrown yet. |
| `/mytimezone` | Check or change the timezone your daily reset is based on. |

### Admin commands (require **Manage Server**, server-only)

| Command | Description |
|---|---|
| `/eggadmin resetstreak <member>` | Reset a member's current streak to 0 (their best streak is kept). |
| `/eggadmin setthreshold <days>` | Override, for this server only, how many days in a row earn the streak role. |
| `/eggadmin reminderstatus` | See how many members in this server have daily reminders enabled. |

Timezone is auto-guessed from your Discord client locale on first use, and
can be changed any time with `/mytimezone`.

## Setup

1. **Create a Discord application** at the
   [Discord Developer Portal](https://discord.com/developers/applications),
   add a Bot user, and copy its token.
2. Under **Bot → Privileged Gateway Intents**, enable **Server Members
   Intent** (the bot needs it to look up members for role rewards).
3. Under **OAuth2 → URL Generator**, select the `bot` and
   `applications.commands` scopes, and at minimum the **Manage Roles** and
   **Send Messages** bot permissions. Use the generated URL to invite the
   bot to your server.
4. Copy `.env.example` to `.env` and fill in `DISCORD_TOKEN`.
5. Run it:

   ```bash
   pip install -r requirements.txt
   python bot.py
   ```

   Or with Docker:

   ```bash
   docker build -t egg-bot .
   docker run -d --env-file .env -v egg-bot-data:/data \
     --restart unless-stopped --name egg-bot egg-bot
   ```

   The `-v egg-bot-data:/data` volume keeps the SQLite database (streaks,
   stats, leaderboard) across container rebuilds — without it, recreating
   the container wipes everyone's progress. `--restart unless-stopped`
   brings the bot back up automatically if it crashes or the host reboots.

## Backups

The database is a single SQLite file, so back it up regularly:

```bash
python scripts/backup_db.py            # writes to ./backups, keeps the last 14
python scripts/backup_db.py --keep 30  # keep more history
```

It's safe to run this while the bot is live — it uses SQLite's online
backup API rather than copying the file directly, so it can't grab a
half-written page mid-write. Schedule it with cron; for Docker, run it
inside the running container so it shares the same volume:

```cron
0 * * * * docker exec egg-bot python scripts/backup_db.py
```

## Configuration

All of these are optional; sensible defaults are used if omitted. Set them
in `.env` or as environment variables — see `.env.example`. Invalid values
(e.g. odds that overlap or go over 100%) make the bot refuse to start with
a clear error instead of silently misbehaving.

| Variable | Default | Meaning |
|---|---|---|
| `DISCORD_TOKEN` | *(required)* | Your bot token. |
| `DB_PATH` | `egg_bot.db` | Path to the SQLite database file. |
| `STREAK_ROLE_NAME` | `🐣 Egg Streak Legend` | Role auto-assigned once a streak reaches the threshold. The bot's own role must sit above this role for it to assign it. |
| `STREAK_ROLE_THRESHOLD` | `7` | Bot-wide default days-in-a-row needed for the streak role. Server admins can override this per server with `/eggadmin setthreshold`. |
| `HISTORY_DAYS` | `7` | How many days of history `/eggstats` shows. |
| `GOLDEN_EGG_CHANCE` | `0.0133` (1/75) | Chance of a golden egg per throw. |
| `FRIDAY_BONUS` | `0.07` | Extra hatch chance added on Fridays. |
| `MAX_THROWS_PER_DAY` | `3` | Throws allowed per user per day. |
| `LEADERBOARD_POOL_SIZE` | `500` | How many top global rows `/eggleaderboard` scans before filtering to this server's members. Raise this if a very large or very active bot's top 500 by hatches doesn't cover a given server. |
| `LOG_LEVEL` | `INFO` | Python logging level (`DEBUG`, `INFO`, `WARNING`, ...). |

## Development

```bash
pip install -r requirements-dev.txt
pytest
```

Tests cover the pure game-logic functions (odds, timezone guessing,
countdown formatting, streak flavour text, config validation, milestone
text) and the small DB helpers backing `/eggadmin` — nothing that touches
Discord or the network. A GitHub Actions workflow
(`.github/workflows/tests.yml`) runs the suite on every push and pull
request, on Python 3.11 and 3.12.

## Notes on this fork/version

Compared to the original, this version:

- Runs all SQLite calls through a thread pool (`asyncio.to_thread`) so a
  database write never blocks the bot from responding to other users while
  it happens.
- Adds a global slash-command error handler, so a bug in one command shows
  the user a clean message and logs the real error, instead of a bare
  "This interaction failed."
- Guards against `on_ready` firing more than once after a reconnect, which
  previously re-synced commands and spawned an extra reminder loop each
  time.
- Fixes a streak-role bug: with `MAX_THROWS_PER_DAY > 1`, missing on a
  2nd/3rd throw of the day used to strip the streak role even though the
  streak itself (which only updates on the day's first throw) hadn't
  actually broken.
- Serializes SQLite access with an explicit lock. `check_same_thread=False`
  only disables Python's same-thread check — it does not make concurrent
  use of one connection from multiple threads safe, and `run_db()` runs
  every DB call on a worker thread. Without the lock this relies on an
  unstated property of whichever SQLite build ends up in a given
  deployment.
- Fixes the golden-egg result text always saying "1-in-75" regardless of
  the actual configured `GOLDEN_EGG_CHANCE`.
- Handles `SIGTERM` explicitly (what `docker stop` sends) so the gateway
  connection and database close cleanly on shutdown instead of the process
  being killed with no cleanup — Python has no default handling for
  `SIGTERM` the way it does for `SIGINT`/Ctrl+C.
- Adds `scripts/backup_db.py` for online, non-blocking SQLite backups with
  automatic pruning, plus a documented cron/Docker schedule.
- Documents `--restart unless-stopped` for the Docker run command so the
  bot comes back up automatically after a crash or host reboot.
- Makes gameplay constants configurable via environment variables, with
  startup validation so a bad value fails loudly instead of silently
  producing broken odds.
- Adds a `guild_settings` table and `/eggadmin` command group so server
  admins can override the streak-role threshold per server, reset a
  member's streak, and check reminder opt-in counts — without touching
  the bot's `.env`.
- Adds an index on `history(user_id, throw_date)` and makes the
  leaderboard's candidate pool size configurable, so both stay fast as
  usage grows.
- Adds a `private` option to `/throwegg` and a "next goal" nudge to
  `/eggstats`.
- Runs as a non-root user in Docker, and persists the database on a
  declared volume.
- Removes the GIF-reaction (`hug`/`cuddle`/etc.) commands and the Giphy
  dependency — this build is just the egg game.
- Adds a `pytest` suite (20 tests) and CI for the previously-untested game
  logic, run automatically on every push/PR.
