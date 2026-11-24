"""Small, optional backup of Plex's database and settings (not the posters/metadata cache).

Plex already writes its own dated copy of the library database every few days; this zips the newest
one together with Preferences.xml, keeps only the last few zips, and tells you the size beforehand.
"""
import logging
import os
import sqlite3
import sys
import tempfile
import time
import zipfile
from pathlib import Path

log = logging.getLogger("backup")


def plex_base() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", "")) / "Plex Media Server"
    return Path("/var/lib/plexmediaserver/Library/Application Support/Plex Media Server")


def _db_dir() -> Path:
    return plex_base() / "Plug-in Support" / "Databases"


def sources() -> list[tuple[Path, str]]:
    """[(file on disk, name inside the zip)]: the newest dated database copy (or the live one) and Preferences.xml."""
    out = []
    d = _db_dir()
    dated = sorted(d.glob("com.plexapp.plugins.library.db-*"), key=lambda p: p.stat().st_mtime) if d.is_dir() else []
    dated = [p for p in dated if not p.name.endswith(("-wal", "-shm"))]
    live = d / "com.plexapp.plugins.library.db"
    if dated:
        out.append((dated[-1], "com.plexapp.plugins.library.db"))
    elif live.exists():
        out.append((live, "com.plexapp.plugins.library.db"))
    prefs = plex_base() / "Preferences.xml"
    if prefs.exists():
        out.append((prefs, "Preferences.xml"))
    return out


def estimate() -> int:
    """About how big one backup zip will be (databases compress to roughly a third)."""
    return int(sum(p.stat().st_size for p, _ in sources()) * 0.4)


def existing(folder: str) -> list[Path]:
    f = Path(folder) if folder else None
    return sorted(f.glob("plex-backup-*.zip"), key=lambda p: p.stat().st_mtime) if f and f.is_dir() else []


def run(folder: str, keep: int = 2) -> Path:
    srcs = sources()
    if not srcs:
        raise RuntimeError("Couldn't find Plex's database on this PC.")
    dest = Path(folder)
    dest.mkdir(parents=True, exist_ok=True)
    name = dest / time.strftime("plex-backup-%Y%m%d-%H%M%S.zip")
    tmp = name.with_suffix(".part")
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            for path, arc in srcs:
                if path.name == "com.plexapp.plugins.library.db":  # the live file: copy it safely first
                    with tempfile.TemporaryDirectory() as td:
                        copy = Path(td) / "db"
                        src = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
                        dst = sqlite3.connect(copy)
                        src.backup(dst)
                        src.close()
                        dst.close()
                        z.write(copy, arc)
                else:
                    z.write(path, arc)
        tmp.replace(name)
    finally:
        tmp.unlink(missing_ok=True)
    for old in existing(folder)[:-max(1, int(keep))]:  # keep only the newest few
        old.unlink(missing_ok=True)
    log.info("Backup written: %s (%d bytes)", name, name.stat().st_size)
    return name


def status(folder: str) -> dict:
    files = existing(folder)
    return {"estimate": estimate(), "found": bool(sources()),
            "last": time.strftime("%Y-%m-%d %H:%M", time.localtime(files[-1].stat().st_mtime)) if files else "",
            "last_size": files[-1].stat().st_size if files else 0, "copies": len(files)}
