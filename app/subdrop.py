"""Subtitle inbox: drop subtitle files with any name into one folder; each one is matched to a movie in Plex,
saved next to it as <movie file>.<lang>.<ext> (so Plex picks it up) and Plex is asked to rescan that folder.

Light on purpose: one directory listing a minute, and matching only when a new file is there.
Files the app can't place with confidence go to "Not matched" and wait for the person to pick the movie.
"""
from __future__ import annotations

import difflib
import json
import logging
import os
import re
import shutil
import time
import zipfile
from pathlib import Path

from . import sevenzip

log = logging.getLogger("subdrop")

SUB_EXT = {".srt", ".ass", ".ssa", ".vtt", ".sub"}
ARCHIVE_EXT = {".zip", ".rar", ".7z"}
TEXT_EXT = {".srt", ".ass", ".ssa", ".vtt"}
DONE, UNMATCHED = "Done", "Not matched"
LANG_NAMES = {"fa": "Persian", "en": "English"}
SETTLE_SECONDS = 15          # a file still being copied is left alone this long
MAX_BYTES = 5_000_000

# words in a file name that say which language the subtitle is
LANG_WORDS = {
    "fa": {"fa", "fas", "per", "persian", "farsi", "parsi", "فارسی"},
    "en": {"en", "eng", "english"},
}
# release words: everything from the first of these on is not part of the title
JUNK = re.compile(r"\b(2160p|1080p|720p|480p|4k|uhd|bluray|blu ray|brrip|bdrip|webrip|web dl|webdl|web|hdtv|dvdrip|"
                  r"hdrip|x264|x265|h264|h265|hevc|aac|ac3|dts|remux|proper|repack|extended|unrated|imax|yify|yts|"
                  r"rarbg|subtitle|subtitles|subs?)\b")


def norm(s: str) -> str:
    s = s.lower().replace("&", " and ")
    s = re.sub(r"[\._\-\[\]\(\)\{\},:;!?'\"]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def parse_name(filename: str) -> tuple[str, str]:
    """(title words, year) guessed from a subtitle or video file name."""
    stem = Path(filename).stem
    n = norm(stem)
    for words in LANG_WORDS.values():          # drop language words at the end ("movie.fa", "movie persian")
        n = re.sub(r"(\s(%s))+$" % "|".join(re.escape(w) for w in words), "", n)
    year = ""
    # the last year that isn't at the very start ("2001 a space odyssey 1968": 2001 is the title)
    years = [m for m in re.finditer(r"\b(19[2-9]\d|20[0-4]\d)\b", n) if m.start() > 0]
    if years:
        year = years[-1].group(1)
        n = n[:years[-1].start()]
    j = JUNK.search(n)
    if j and j.start() > 0:
        n = n[:j.start()]
    return n.strip(), year


def detect_lang(filename: str, data: bytes) -> str:
    words = set(norm(Path(filename).stem).split())
    for lang, ws in LANG_WORDS.items():
        if words & ws:
            return lang
    text = decode(data)
    letters = [c for c in text[:20000] if c.isalpha()]
    if letters and sum(1 for c in letters if "؀" <= c <= "ۿ") / len(letters) > 0.3:
        return "fa"
    return "en"


def decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "utf-16"):
        try:
            t = data.decode(enc)
            if enc == "utf-16" and not data[:2] in (b"\xff\xfe", b"\xfe\xff"):
                continue
            return t
        except UnicodeDecodeError:
            continue
    try:
        return data.decode("cp1256")           # old Persian/Arabic subtitles
    except UnicodeDecodeError:
        return data.decode("latin-1")


def to_utf8(data: bytes, ext: str) -> bytes:
    """Text subtitles are saved as UTF-8 so Persian shows correctly in Plex; other formats are kept as they are."""
    if ext not in TEXT_EXT:
        return data
    return decode(data).encode("utf-8")


class SubDrop:
    def __init__(self, db, plex, data_dir: Path, cfg: dict):
        self.db, self.plex, self.cfg = db, plex, cfg
        self.data_dir = Path(data_dir)
        self.log_path = self.data_dir / "subtitle_inbox.json"
        try:
            self.recent = json.loads(self.log_path.read_text(encoding="utf-8"))
        except Exception:
            self.recent = []

    # ---------------------------------------------------------------- folders
    def folder(self) -> Path:
        f = str(self.cfg["subtitles"].get("drop_folder") or "").strip()
        return Path(f) if f else self.data_dir / "Subtitle inbox"

    def _ensure(self) -> Path | None:
        f = self.folder()
        try:
            f.mkdir(parents=True, exist_ok=True)
            return f
        except OSError as e:
            log.info("Subtitle inbox %s can't be used: %s", f, e)
            return None

    # ---------------------------------------------------------------- matching
    def movies(self) -> list[dict]:
        return [dict(r) for r in self.db.query("SELECT rating_key, title, year, path, langs FROM movie_subs") if r["path"]]

    def candidates(self, filename: str, movies: list[dict] | None = None, n: int = 3) -> list[dict]:
        title, year = parse_name(filename)
        if not title:
            return []
        out = []
        for mv in movies if movies is not None else self.movies():
            t1 = norm(mv["title"] or "")
            t2, y2 = parse_name(Path(mv["path"]).name)
            score = max(difflib.SequenceMatcher(None, title, t1).ratio(), difflib.SequenceMatcher(None, title, t2).ratio())
            my = str(mv.get("year") or y2 or "")
            if year and my:
                score = score + 0.1 if year == my else score * 0.6
            out.append({"key": mv["rating_key"], "title": mv["title"], "year": my, "score": round(min(score, 1.0), 3)})
        out.sort(key=lambda x: -x["score"])
        return out[:n]

    @staticmethod
    def confident(c: list[dict]) -> bool:
        if not c or c[0]["score"] < 0.82:
            return False
        return len(c) == 1 or c[0]["score"] - c[1]["score"] >= 0.08

    # ---------------------------------------------------------------- placing
    def place(self, src: Path, key: str, lang: str) -> str:
        """Save src next to the movie as <movie>.<lang><ext>. Returns the new file name. Raises ValueError with a
        message for the person when it can't."""
        rows = self.db.query("SELECT * FROM movie_subs WHERE rating_key=?", (key,))
        if not rows or not rows[0]["path"]:
            raise ValueError("That movie isn't in the list any more.")
        video = Path(rows[0]["path"])
        if not video.parent.is_dir():
            raise ValueError(f"The movie's folder isn't reachable: {video.parent}")
        ext = src.suffix.lower()
        dest = video.with_name(f"{video.stem}.{lang}{ext}")
        if dest.exists():
            raise ValueError(f"{video.name} already has a {LANG_NAMES.get(lang, lang)} subtitle; it was not replaced.")
        data = src.read_bytes()
        dest.write_bytes(to_utf8(data, ext))
        langs = [x for x in (rows[0]["langs"] or "").split(",") if x] + [lang]
        self.db.execute("UPDATE movie_subs SET langs=? WHERE rating_key=?", (",".join(sorted(set(langs))), key))
        try:
            self.plex.scan_path(video.parent)
        except Exception as e:
            log.info("Plex rescan after placing a subtitle failed: %s", e)
        return dest.name

    def _note(self, entry: dict):
        entry["at"] = time.strftime("%Y-%m-%d %H:%M")
        self.recent = [entry] + [r for r in self.recent if r.get("file") != entry.get("file")][:49]
        try:
            self.log_path.write_text(json.dumps(self.recent, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

    @staticmethod
    def _move(src: Path, folder: Path) -> Path:
        folder.mkdir(exist_ok=True)
        dest = folder / src.name
        i = 1
        while dest.exists():
            dest = folder / f"{src.stem} ({i}){src.suffix}"
            i += 1
        shutil.move(str(src), str(dest))
        return dest

    def _take_subs(self, folder: Path, inbox: Path):
        for f in folder.rglob("*"):
            if f.is_file() and f.suffix.lower() in SUB_EXT and 0 < f.stat().st_size <= MAX_BYTES:
                out = inbox / f.name
                if out.exists():
                    out = inbox / f"{out.stem} ({int(time.time())}){out.suffix}"
                shutil.copyfile(f, out)

    def _unpack(self, a: Path, inbox: Path):
        """Subtitles often come zipped or as .rar: take the subtitle files out, then put the archive in Done.
        .zip is opened directly; .rar and .7z need 7-Zip, which is fetched once the first time one shows up."""
        if a.suffix.lower() == ".zip":
            try:
                with zipfile.ZipFile(a) as zf:
                    for info in zf.infolist():
                        name = Path(info.filename).name
                        if Path(name).suffix.lower() in SUB_EXT and 0 < info.file_size <= MAX_BYTES:
                            out = inbox / name
                            if out.exists():
                                out = inbox / f"{out.stem} ({int(time.time())}){out.suffix}"
                            out.write_bytes(zf.read(info))
                self._move(a, inbox / DONE)
                return
            except (zipfile.BadZipFile, OSError) as e:
                log.info("Couldn't open %s as a zip (%s), trying 7-Zip", a.name, e)
        exe = sevenzip.ensure(self.data_dir)
        if not exe:
            self._move(a, inbox / UNMATCHED)
            self._note({"file": a.name, "ok": False, "message": "This archive needs 7-Zip, which couldn't be set up. Unpack it yourself and drop the subtitle in."})
            return
        tmp = self.data_dir / "tmp-subtitle-archive"
        shutil.rmtree(tmp, ignore_errors=True)
        ok = sevenzip.extract(exe, a, tmp)
        found = ok and any(f.suffix.lower() in SUB_EXT for f in tmp.rglob("*"))
        if found:
            self._take_subs(tmp, inbox)
            self._move(a, inbox / DONE)
        else:
            self._move(a, inbox / UNMATCHED)
            self._note({"file": a.name, "ok": False, "message": "No subtitle file found inside (or the archive is damaged or has a password)."})
        shutil.rmtree(tmp, ignore_errors=True)

    def run(self) -> int:
        """One look at the inbox. Returns how many subtitles were placed."""
        inbox = self._ensure()
        if inbox is None:
            return 0
        now = time.time()
        files = [p for p in inbox.iterdir() if p.is_file() and now - p.stat().st_mtime > SETTLE_SECONDS]
        archives = [p for p in files if p.suffix.lower() in ARCHIVE_EXT]
        for z in archives:
            self._unpack(z, inbox)
        if archives:
            files = [p for p in inbox.iterdir() if p.is_file() and p.suffix.lower() not in ARCHIVE_EXT]
        subs = [p for p in files if p.suffix.lower() in SUB_EXT]
        if not subs:
            return 0
        movies = self.movies()
        placed = 0
        for p in subs:
            try:
                if p.stat().st_size > MAX_BYTES:
                    self._move(p, inbox / UNMATCHED)
                    self._note({"file": p.name, "ok": False, "message": "Too big for a subtitle file."})
                    continue
                lang = detect_lang(p.name, p.read_bytes()[:200000])
                c = self.candidates(p.name, movies)
                if self.confident(c):
                    try:
                        saved = self.place(p, c[0]["key"], lang)
                        self._move(p, inbox / DONE)
                        self._note({"file": p.name, "ok": True, "lang": lang, "movie": c[0]["title"], "year": c[0]["year"],
                                    "message": f"Saved as {saved}"})
                        placed += 1
                        continue
                    except ValueError as e:
                        self._move(p, inbox / DONE)
                        self._note({"file": p.name, "ok": False, "lang": lang, "movie": c[0]["title"], "message": str(e)})
                        continue
                self._move(p, inbox / UNMATCHED)
                self._note({"file": p.name, "ok": False, "lang": lang,
                            "message": "Couldn't tell which movie this is. Pick it below." if movies else
                            "No movie list yet: Plex hasn't been scanned. Pick the movie once it is."})
            except OSError as e:
                log.info("Subtitle inbox: %s: %s", p.name, e)
        self._tidy(inbox / DONE)
        return placed

    @staticmethod
    def _tidy(done: Path, days: int = 30):
        if not done.is_dir():
            return
        cut = time.time() - days * 86400
        for p in done.iterdir():
            try:
                if p.is_file() and p.stat().st_mtime < cut:
                    p.unlink()
            except OSError:
                pass

    # ---------------------------------------------------------------- for the page
    def waiting(self) -> list[dict]:
        """Files in "Not matched", each with the movies it most likely belongs to."""
        um = self.folder() / UNMATCHED
        if not um.is_dir():
            return []
        movies = self.movies()
        out = []
        for p in sorted(um.iterdir(), key=lambda x: -x.stat().st_mtime)[:40]:
            if p.is_file() and p.suffix.lower() in SUB_EXT:
                try:
                    lang = detect_lang(p.name, p.read_bytes()[:200000])
                except OSError:
                    lang = "en"
                out.append({"file": p.name, "lang": lang, "suggest": self.candidates(p.name, movies, 5)})
        return out

    def assign(self, file: str, key: str, lang: str) -> str:
        p = self.folder() / UNMATCHED / Path(file).name
        if not p.is_file():
            raise ValueError("That file isn't waiting any more.")
        saved = self.place(p, key, lang)
        self._move(p, self.folder() / DONE)
        mv = self.db.query("SELECT title, year FROM movie_subs WHERE rating_key=?", (key,))
        self._note({"file": p.name, "ok": True, "lang": lang, "movie": mv[0]["title"] if mv else "",
                    "message": f"Saved as {saved}"})
        return saved

    def discard(self, file: str):
        p = self.folder() / UNMATCHED / Path(file).name
        if p.is_file():
            self._move(p, self.folder() / DONE)

    def add_upload(self, name: str, data: bytes):
        inbox = self._ensure()
        if inbox is None:
            raise ValueError(f"The subtitle inbox folder can't be used: {self.folder()}")
        name = Path(name or "subtitle.srt").name
        if Path(name).suffix.lower() not in SUB_EXT | ARCHIVE_EXT:
            raise ValueError("Choose subtitle files (.srt, .ass, .vtt, .sub) or a .zip / .rar / .7z of them.")
        out = inbox / name
        if out.exists():
            out = inbox / f"{out.stem} ({int(time.time())}){out.suffix}"
        out.write_bytes(data)
        # backdate so the next run picks it up without waiting for SETTLE_SECONDS
        t = time.time() - SETTLE_SECONDS - 1
        os.utime(out, (t, t))
