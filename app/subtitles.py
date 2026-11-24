"""Download missing subtitles for movies from the sources set up in Settings (sites and APIs)."""
import logging
import re
import struct
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests

from . import subsources
from .db import DB

log = logging.getLogger("subtitles")

API = "https://api.opensubtitles.com/api/v1"
VIDEO_EXT = {".mkv", ".mp4", ".avi", ".m4v", ".mov", ".wmv", ".ts", ".m2ts", ".mpg", ".mpeg", ".webm"}
SUB_EXT = {".srt", ".ass", ".ssa", ".sub", ".vtt"}
RETRY_AFTER = timedelta(days=7)  # don't search again for a film with no subs for a week


def movie_hash(path: Path) -> str:
    """OpenSubtitles hash: file size + 64-bit sums of the first and last 64 KB."""
    chunk = 65536
    size = path.stat().st_size
    if size < chunk * 2:
        return ""
    h = size
    with open(path, "rb") as f:
        for offset in (0, size - chunk):
            f.seek(offset)
            buf = f.read(chunk)
            for (v,) in struct.iter_unpack("<Q", buf):
                h = (h + v) & 0xFFFFFFFFFFFFFFFF
    return f"{h:016x}"


def parse_title(path: Path) -> tuple[str, str]:
    """'The.Matrix.1999.1080p.BluRay.mkv' -> ('The Matrix', '1999')."""
    name = path.stem
    m = re.search(r"^(.*?)[\s._\(\[-]+((?:19|20)\d{2})(?:[\s._\)\]-]|$)", name)
    if m:
        title, year = m.group(1), m.group(2)
    else:
        title, year = re.split(r"[\s._-](?:1080p|720p|2160p|4k|bluray|web|dvdrip|x264|x265)", name, 1, re.I)[0], ""
    return re.sub(r"[._]+", " ", title).strip(), year


def has_subtitle(video: Path, lang: str) -> bool:
    stem = video.stem.lower()
    for p in video.parent.iterdir():
        if p.suffix.lower() not in SUB_EXT:
            continue
        s = p.stem.lower()
        if s == f"{stem}.{lang}" or s.startswith(f"{stem}.{lang}.") or (lang == "en" and s == stem):
            return True
    return False


def find_videos(folders: list[str]) -> list[Path]:
    out = []
    for folder in folders:
        root = Path(folder)
        if root.is_dir():
            out.extend(
                p for p in root.rglob("*")
                if p.suffix.lower() in VIDEO_EXT and "sample" not in p.stem.lower() and p.is_file()
            )
    return sorted(out)


# Older subtitle files often use a Windows code page instead of UTF-8
# (Persian/Arabic: cp1256). Plex shows those as gibberish, so convert them.
CODEPAGES = {"fa": "cp1256", "ar": "cp1256", "ru": "cp1251", "el": "cp1253", "tr": "cp1254", "he": "cp1255"}


def to_utf8(content: bytes, dest: Path) -> bytes:
    if content.startswith(b"\xef\xbb\xbf"):
        return content
    try:
        content.decode("utf-8")
        return content
    except UnicodeDecodeError:
        pass
    lang = dest.stem.rsplit(".", 1)[-1].lower()
    for enc in (CODEPAGES.get(lang), "cp1252"):
        if not enc:
            continue
        try:
            return b"\xef\xbb\xbf" + content.decode(enc).encode("utf-8")
        except UnicodeDecodeError:
            continue
    return content


class QuotaReached(Exception):
    pass


def make_movie(video: Path, index: dict) -> subsources.Movie:
    info = index.get(str(video).replace("\\", "/").lower())
    title, year = parse_title(video)
    if info:
        title, year = info["title"] or title, info["year"] or year
    try:
        size = video.stat().st_size
    except OSError:
        size = 0
    return subsources.Movie(path=video, title=title, year=year, imdb=(info or {}).get("imdb", ""),
                            hash=movie_hash(video) if size else "", size=size)


def find_one(movie: subsources.Movie, lang: str, sources: list, blocked: dict, trace: list | None = None):
    """Try each source in order. Returns (bytes, ext, source name, release) or None, plus notes."""
    notes = []
    for src in sources:
        if src.name in blocked:
            notes.append(f"{src.name}: {blocked[src.name]}")
            continue
        try:
            cands = src.search(movie, lang)
        except subsources.Blocked as e:
            blocked[src.name] = str(e)
            notes.append(f"{src.name}: {e}")
            continue
        except Exception as e:
            notes.append(f"{src.name}: error {str(e)[:120]}")
            continue
        if not cands:
            notes.append(f"{src.name}: nothing")
            continue
        for c in sorted(cands, key=lambda c: -c.score)[:3]:
            try:
                data = src.download(c)
            except subsources.Blocked as e:
                blocked[src.name] = str(e)
                notes.append(f"{src.name}: {e}")
                break
            except Exception as e:
                notes.append(f"{src.name}: download failed {str(e)[:100]}")
                continue
            got = subsources.extract_subtitle(data, movie)
            if got:
                return (got[0], got[1], src.name, c.name), notes
            notes.append(f"{src.name}: file wasn't a subtitle (maybe RAR or a web page)")
    return None, notes


def run(db: DB, cfg: dict, sources_conf: list[dict], plex=None) -> int:
    s = cfg["subtitles"]
    if not s.get("enabled"):
        return 0
    todo = []
    for video in find_videos(s.get("roots", [])):
        for lang in s["languages"]:
            if has_subtitle(video, lang):
                continue
            prev = db.subtitle_attempt(video, lang)
            if prev and prev["status"] != "downloaded" and \
               datetime.fromisoformat(prev["at"]) > datetime.now() - RETRY_AFTER:
                continue
            todo.append((video, lang))
    if not todo:
        return 0

    index = {}
    if plex is not None and plex.configured:
        try:
            index = plex.movie_index()
        except Exception as e:
            log.info("Couldn't read movie list from Plex: %s", e)

    session = requests.Session()
    sources = [subsources.build(c, session) for c in sources_conf if c.get("enabled", True)]
    if not sources:
        return 0
    blocked: dict[str, str] = {}
    got = 0
    for video, lang in todo:
        if got >= int(s["max_downloads_per_run"]):
            break
        if len(blocked) == len(sources):
            break
        movie = make_movie(video, index)
        result, notes = find_one(movie, lang, sources, blocked)
        if not result:
            db.log_subtitle(video, lang, "not_found", "; ".join(notes)[:300])
            continue
        data, ext, src_name, release = result
        dest = video.with_name(f"{video.stem}.{lang}{ext}")
        dest.write_bytes(to_utf8(data, dest))
        db.log_subtitle(video, lang, "downloaded", f"from {src_name}" + (f" ({release[:80]})" if release else ""))
        got += 1
        log.info("Subtitle %s from %s -> %s", lang, src_name, dest.name)
    return got
