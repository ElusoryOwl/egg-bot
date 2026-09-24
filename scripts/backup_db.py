#!/usr/bin/env python3
"""
Snapshot egg-bot's SQLite database to a timestamped backup file.

Safe to run while the bot is live: it uses SQLite's online backup API
(Connection.backup), which takes a consistent snapshot without locking out
the running bot process — unlike just `cp`-ing the file, which can copy a
half-written page if a write is in progress.

Usage:
    python scripts/backup_db.py [--keep N]

Environment variables (same ones the bot itself reads):
    DB_PATH      Path to the live database (default: egg_bot.db)
    BACKUP_DIR   Where to write backups (default: backups)

Typical setups:

  Bare-metal / systemd:
      Add a cron entry:
          0 * * * * cd /path/to/egg-bot && python3 scripts/backup_db.py

  Docker:
      Run it inside the running container on a host cron schedule, so it
      shares the same volume as the live database:
          0 * * * * docker exec egg-bot python scripts/backup_db.py
      (Mount a second volume for BACKUP_DIR, e.g. -v egg-bot-backups:/app/backups,
      if you want backups to survive the container being removed too.)
"""
import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path

DB_PATH = os.environ.get("DB_PATH", "egg_bot.db")
BACKUP_DIR = os.environ.get("BACKUP_DIR", "backups")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--keep", type=int, default=14,
        help="Number of most recent backups to keep (default: 14)",
    )
    args = parser.parse_args()

    src_path = Path(DB_PATH)
    if not src_path.exists():
        print(f"No database found at {src_path!s}, nothing to back up.", file=sys.stderr)
        sys.exit(1)

    backup_dir = Path(BACKUP_DIR)
    backup_dir.mkdir(parents=True, exist_ok=True)

    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest_path = backup_dir / f"egg_bot-{stamp}.db"
    # Guard against two runs landing in the same second (e.g. manual re-runs
    # while testing) silently overwriting each other.
    n = 1
    while dest_path.exists():
        dest_path = backup_dir / f"egg_bot-{stamp}-{n}.db"
        n += 1

    src = sqlite3.connect(str(src_path))
    dest = sqlite3.connect(str(dest_path))
    try:
        with dest:
            src.backup(dest)
    finally:
        src.close()
        dest.close()

    size_kb = dest_path.stat().st_size / 1024
    print(f"Backed up {src_path} -> {dest_path} ({size_kb:.1f} KB)")

    # Prune old backups beyond --keep, oldest first.
    backups = sorted(
        backup_dir.glob("egg_bot-*.db"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    for old in backups[args.keep:]:
        old.unlink()
        print(f"Pruned old backup: {old}")


if __name__ == "__main__":
    main()
