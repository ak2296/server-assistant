"""Safety copies of your settings and lists, so an update (or a folder mix-up) can never silently wipe them.

Copies live in <data folder>\\safety\\<name>\\ : config.toml, subtitle_sources.json and the database.
  - "before-update": taken right before an update is applied
  - "daily": refreshed about once a day while the app runs
At start-up, if the settings file is gone, or the database is empty while a copy has your lists, they are put back.
"""
import logging
import shutil
import sqlite3
import time
from pathlib import Path

log = logging.getLogger("safety")
FILES = ("config.toml", "subtitle_sources.json")
DB_NAME = "server_assistant.db"


def _dir(data: Path, name: str) -> Path:
    return data / "safety" / name


def _rows(db_path: Path) -> int:
    """How much of your own data a database holds (releases and skipped artists)."""
    try:
        con = sqlite3.connect(str(db_path))
        try:
            n = 0
            for t in ("releases", "skipped_artists"):
                n += con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            return n
        finally:
            con.close()
    except Exception:
        return 0


def snapshot(data: Path, name: str) -> None:
    """Copy settings and database into safety/<name>. Never raises."""
    try:
        d = _dir(data, name)
        d.mkdir(parents=True, exist_ok=True)
        for f in FILES:
            if (data / f).exists():
                shutil.copy2(data / f, d / f)
        src = data / DB_NAME
        if src.exists():
            tmp = d / (DB_NAME + ".tmp")
            a, b = sqlite3.connect(str(src)), sqlite3.connect(str(tmp))
            try:
                a.backup(b)   # a consistent copy even while the app is using the database
            finally:
                b.close()
                a.close()
            if _rows(tmp) == 0 and _rows(d / DB_NAME) > 0:
                tmp.unlink(missing_ok=True)   # never replace a copy that has your lists with an empty one
            else:
                tmp.replace(d / DB_NAME)
        (d / "taken.txt").write_text(time.strftime("%Y-%m-%d %H:%M:%S"), encoding="utf-8")
    except Exception as e:
        log.warning("Could not make a safety copy (%s): %s", name, e)


def restore_if_lost(data: Path) -> list[str]:
    """Put back settings or lists that disappeared. Returns what was restored."""
    done = []
    try:
        copies = sorted((p for p in (data / "safety").glob("*") if p.is_dir()), key=lambda p: (p / "taken.txt").stat().st_mtime
                        if (p / "taken.txt").exists() else 0, reverse=True)
        for f in FILES:
            if not (data / f).exists():
                for c in copies:
                    if (c / f).exists():
                        shutil.copy2(c / f, data / f)
                        done.append(f)
                        break
        db = data / DB_NAME
        if _rows(db) == 0:
            best = max(copies, key=lambda c: _rows(c / DB_NAME), default=None)
            if best and _rows(best / DB_NAME) > 0:
                if db.exists():
                    shutil.copy2(db, data / "safety" / "empty-database-before-restore.db")
                shutil.copy2(best / DB_NAME, db)
                done.append("database")
    except Exception as e:
        log.warning("Restore check failed: %s", e)
    if done:
        log.warning("Restored from a safety copy: %s", ", ".join(done))
    return done
