"""Daily database backups (SQLite's online backup: safe while the bot is writing)."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


def backup_db(db_path: Path, backup_dir: Path, now_ms: int, tz: ZoneInfo, keep: int = 14) -> Path:
    """Copy the database to backup_dir/scout-YYYY-MM-DD.db and keep only the newest `keep` copies."""
    backup_dir.mkdir(parents=True, exist_ok=True)
    day = datetime.fromtimestamp(now_ms / 1000, tz).strftime("%Y-%m-%d")
    target = backup_dir / f"{db_path.stem}-{day}.db"
    partial = target.with_suffix(".partial")
    with closing(sqlite3.connect(db_path)) as source, closing(sqlite3.connect(partial)) as copy:
        source.backup(copy)
    partial.replace(target)  # a half-written backup never looks like a finished one
    for old in sorted(backup_dir.glob(f"{db_path.stem}-*.db"))[:-keep]:
        old.unlink()
    return target


def latest_backup(backup_dir: Path, stem: str = "scout") -> Path | None:
    backups = sorted(backup_dir.glob(f"{stem}-*.db"))
    return backups[-1] if backups else None
