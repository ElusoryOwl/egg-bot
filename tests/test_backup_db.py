"""
Tests for scripts/backup_db.py — run as a subprocess against a scratch
SQLite file so we exercise the real script, not a reimplementation of it.
"""
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

SCRIPT = str(Path(__file__).resolve().parent.parent / "scripts" / "backup_db.py")


def _make_db(path: Path):
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    conn.execute("INSERT INTO t (v) VALUES ('hello')")
    conn.commit()
    conn.close()


def _run_backup(db_path: Path, backup_dir: Path, keep: int = 14):
    env = {**os.environ, "DB_PATH": str(db_path), "BACKUP_DIR": str(backup_dir)}
    return subprocess.run(
        [sys.executable, SCRIPT, "--keep", str(keep)],
        env=env, capture_output=True, text=True, check=True,
    )


def test_backup_creates_a_readable_copy(tmp_path):
    db_path = tmp_path / "egg_bot.db"
    backup_dir = tmp_path / "backups"
    _make_db(db_path)

    _run_backup(db_path, backup_dir)

    files = list(backup_dir.glob("egg_bot-*.db"))
    assert len(files) == 1
    conn = sqlite3.connect(str(files[0]))
    assert conn.execute("SELECT v FROM t").fetchone() == ("hello",)


def test_backup_missing_db_exits_nonzero(tmp_path):
    result = subprocess.run(
        [sys.executable, SCRIPT],
        env={**os.environ, "DB_PATH": str(tmp_path / "nope.db"),
             "BACKUP_DIR": str(tmp_path / "backups")},
        capture_output=True, text=True,
    )
    assert result.returncode != 0


def test_backup_same_second_reruns_dont_overwrite(tmp_path):
    """
    Regression test: running the backup twice within the same second used
    to produce the same filename and silently overwrite the first backup.
    """
    db_path = tmp_path / "egg_bot.db"
    backup_dir = tmp_path / "backups"
    _make_db(db_path)

    _run_backup(db_path, backup_dir, keep=99)
    _run_backup(db_path, backup_dir, keep=99)
    _run_backup(db_path, backup_dir, keep=99)

    assert len(list(backup_dir.glob("egg_bot-*.db"))) == 3


def test_backup_prunes_to_keep_count(tmp_path):
    db_path = tmp_path / "egg_bot.db"
    backup_dir = tmp_path / "backups"
    _make_db(db_path)

    for _ in range(5):
        _run_backup(db_path, backup_dir, keep=2)

    assert len(list(backup_dir.glob("egg_bot-*.db"))) == 2
