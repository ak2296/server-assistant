"""Move finished music downloads into <library>/<Artist>/<Year> - <Album>/."""
import logging
import os
import re
import shutil
import time
import zipfile
from collections import Counter
from pathlib import Path

import mutagen

from . import library
from .db import DB

log = logging.getLogger("sorter")

INCOMPLETE_EXT = {".part", ".!qb", ".crdownload", ".tmp", ".download", ".partial", ".!ut", ".bc!"}
EXTRA_EXT = {".jpg", ".jpeg", ".png", ".cue", ".log", ".m3u", ".m3u8", ".txt", ".nfo", ".pdf", ".lrc"}
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
# "Megadeth - Dystopia (2016) [FLAC]"  /  "Megadeth - 2016 - Dystopia"
_FOLDER_PATTERNS = [
    re.compile(r"^(?P<artist>.+?)\s+-\s+(?P<year>\d{4})\s+-\s+(?P<album>.+)$"),
    re.compile(r"^(?P<artist>.+?)\s+-\s+(?P<album>.+?)\s*[\(\[](?P<year>\d{4})[\)\]]"),
    re.compile(r"^(?P<artist>.+?)\s+-\s+(?P<album>.+)$"),
]


def safe_name(text: str) -> str:
    text = _ILLEGAL.sub("_", text).strip().rstrip(". ")
    return text[:120] or "Unknown"


def _first(tags, *keys) -> str:
    for k in keys:
        v = tags.get(k)
        if v:
            v = v[0] if isinstance(v, list) else v
            if str(v).strip():
                return str(v).strip()
    return ""


def read_tags(path: Path) -> dict:
    try:
        f = mutagen.File(path, easy=True)
    except Exception:
        return {}
    if not f or not f.tags:
        return {}
    t = f.tags
    year = _first(t, "date", "originaldate", "year")[:4]
    return {
        "artist": _first(t, "albumartist", "album artist", "artist"),
        "album": _first(t, "album"),
        "year": year if year.isdigit() else "",
    }


def guess_from_name(name: str) -> dict:
    name = re.sub(r"[\[\(](flac|mp3|320|v0|web|cd|24bit|16bit|lossless)[^\]\)]*[\]\)]", "", name, flags=re.I).strip()
    for pat in _FOLDER_PATTERNS:
        m = pat.match(name)
        if m:
            d = m.groupdict()
            return {"artist": d["artist"].strip(), "album": d["album"].strip(), "year": d.get("year") or ""}
    return {}


def audio_files(path: Path) -> list[Path]:
    if path.is_file():
        return [path] if path.suffix.lower() in library.AUDIO_EXT else []
    return sorted(p for p in path.rglob("*") if p.is_file() and p.suffix.lower() in library.AUDIO_EXT)


def has_incomplete(path: Path) -> bool:
    items = [path] if path.is_file() else path.rglob("*")
    return any(p.suffix.lower() in INCOMPLETE_EXT for p in items)


def _snapshot(path: Path) -> tuple:
    if path.is_file():
        st = path.stat()
        return (st.st_size, st.st_mtime)
    total, latest = 0, 0.0
    for p in path.rglob("*"):
        if p.is_file():
            st = p.stat()
            total += st.st_size
            latest = max(latest, st.st_mtime)
    return (total, latest)


def _extract_zip(zpath: Path) -> Path | None:
    dest = zpath.with_suffix("")
    try:
        with zipfile.ZipFile(zpath) as z:
            if not any(Path(n).suffix.lower() in library.AUDIO_EXT for n in z.namelist()):
                return None
            z.extractall(dest)
        zpath.unlink()
        return dest
    except zipfile.BadZipFile:
        return None


def plan_album(files: list[Path], fallback_name: str) -> dict:
    """Decide artist/album/year for a group of files (majority vote over tags)."""
    votes = {"artist": Counter(), "album": Counter(), "year": Counter()}
    for f in files:
        for k, v in read_tags(f).items():
            if v:
                votes[k][v] += 1
    info = {k: (c.most_common(1)[0][0] if c else "") for k, c in votes.items()}
    guess = guess_from_name(fallback_name)
    for k in ("artist", "album", "year"):
        if not info[k] and guess.get(k):
            info[k] = guess[k]
    return info


def destination(cfg: dict, info: dict) -> Path:
    roots = cfg["music"]["roots"]
    artist_dir = library.find_artist_dir(roots, info["artist"])
    if artist_dir is None:
        root = library.first_root(roots)
        if root is None:
            raise RuntimeError("No music library folder found - check Settings")
        artist_dir = root / safe_name(info["artist"])
    fmt = cfg["music"]["album_folder_format"]
    if not info["year"]:
        fmt = fmt.replace("{year} - ", "").replace("{year}", "")
    album_folder = safe_name(fmt.format(artist=info["artist"], album=info["album"], year=info["year"]))
    return artist_dir / album_folder


def transfer(src: Path, dst: Path, mode: str) -> None:
    """mode 'move' moves the file; mode 'link' leaves the original in place (so a
    torrent keeps seeding) and hardlinks it - no extra disk space when both are on
    the same drive - or copies it when they're on different drives."""
    if mode == "move":
        shutil.move(str(src), str(dst))
        return
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def move_item(db: DB, cfg: dict, item: Path, mode: str = "move", on_sorted=None) -> bool:
    """Sort one finished download (file or folder). Returns True if it was handled."""
    if mode == "move" and item.is_file() and item.suffix.lower() == ".zip":
        extracted = _extract_zip(item)
        if not extracted:
            return False
        item = extracted

    files = audio_files(item)
    if not files:
        return False

    name = item.stem if item.is_file() else item.name
    # A folder can hold several albums (e.g. a discography) - group files by album tag.
    groups: dict[str, list[Path]] = {}
    for f in files:
        groups.setdefault(read_tags(f).get("album") or "", []).append(f)

    all_ok = True
    for files_in_album in groups.values():
        info = plan_album(files_in_album, name)
        if not info["artist"] or not info["album"]:
            db.log_sort(item, None, False, "Couldn't tell the artist/album (no tags, unclear name). Left in place.")
            all_ok = False
            continue
        dest = destination(cfg, info)
        dest.mkdir(parents=True, exist_ok=True)
        src_root = item if item.is_dir() else item.parent
        moved = 0
        for f in files_in_album:
            rel = f.relative_to(src_root) if item.is_dir() else Path(f.name)
            # keep "CD1"/"Disc 2" subfolders, flatten anything else
            sub = rel.parent if re.match(r"^(cd|disc|disk)\s*\d+", rel.parent.name, re.I) else Path()
            target = dest / sub / f.name
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                continue
            transfer(f, target, mode)
            moved += 1
        # bring cover art / cue / log files along
        if item.is_dir():
            for extra in {f.parent for f in files_in_album}:
                for p in extra.iterdir():
                    if p.is_file() and p.suffix.lower() in EXTRA_EXT and not (dest / p.name).exists():
                        transfer(p, dest / p.name, mode)
        db.log_sort(item, dest, True, f"{moved} file(s): {info['artist']} - {info['album']}")
        # tick off the matching release in the dashboard
        for r in db.query("SELECT * FROM releases WHERE status IN ('new','wanted')"):
            if library.normalize(r["artist"]) == library.normalize(dest.parent.name) and \
               library.is_owned(r["title"], {library.normalize(info["album"])}):
                db.execute("UPDATE releases SET status='got' WHERE mbid=?", (r["mbid"],))
        log.info("Sorted %s -> %s", item, dest)
        if on_sorted:
            try:
                on_sorted(dest)
            except Exception as e:
                log.warning("After-sort hook failed: %s", e)

    # remove the emptied download folder
    if mode == "move" and item.is_dir() and all_ok and not audio_files(item):
        shutil.rmtree(item, ignore_errors=True)
    return True


class Sorter:
    """Polls the download folders; moves items once they've stopped changing."""

    def __init__(self, db: DB, cfg: dict, on_sorted=None):
        self.db, self.cfg, self.on_sorted = db, cfg, on_sorted
        self.seen: dict[Path, tuple] = {}  # path -> (snapshot, first time seen with this snapshot)
        self.failed: dict[Path, tuple] = {}

    def poll(self) -> None:
        settle = int(self.cfg["music"]["settle_seconds"])
        for folder in self.cfg["music"]["download_folders"]:
            root = Path(folder)
            if not root.is_dir():
                continue
            for item in root.iterdir():
                if item.name.startswith(".") or has_incomplete(item):
                    continue
                try:
                    snap = _snapshot(item)
                except OSError:
                    continue
                if self.failed.get(item) == snap:
                    continue  # already tried and couldn't sort; wait for it to change
                prev = self.seen.get(item)
                if not prev or prev[0] != snap:
                    self.seen[item] = (snap, time.time())
                    continue
                if time.time() - prev[1] < settle:
                    continue
                self.seen.pop(item, None)
                try:
                    handled = move_item(self.db, self.cfg, item, on_sorted=self.on_sorted)
                    if item.exists():
                        self.failed[item] = _snapshot(item)
                    if not handled:
                        self.failed[item] = snap
                except Exception as e:
                    log.exception("Sorting %s failed", item)
                    self.db.log_sort(item, None, False, f"Error: {e}")
                    if item.exists():
                        self.failed[item] = _snapshot(item)
