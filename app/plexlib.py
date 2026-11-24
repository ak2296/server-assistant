"""Library facts that come from Plex: which albums you own, and which movies lack subtitles."""
import logging
import threading
import time

from . import library
from .db import DB, now

log = logging.getLogger("plexlib")

# 3-letter codes Plex uses -> our 2-letter settings
LANG_CODES = {"en": {"en", "eng"}, "fa": {"fa", "per", "fas", "persian"}, "sv": {"sv", "swe"}, "de": {"de", "ger", "deu"},
              "fr": {"fr", "fre", "fra"}, "es": {"es", "spa"}, "ar": {"ar", "ara"}, "tr": {"tr", "tur"}, "it": {"it", "ita"},
              "ru": {"ru", "rus"}, "pt": {"pt", "por"}, "nl": {"nl", "dut", "nld"}, "fi": {"fi", "fin"}, "da": {"da", "dan"},
              "no": {"no", "nor", "nob"}, "pl": {"pl", "pol"}, "ja": {"ja", "jpn"}, "ko": {"ko", "kor"}, "zh": {"zh", "chi", "zho"}}


def has_lang(langs: list[str], want: str) -> bool:
    codes = LANG_CODES.get(want, {want})
    return any(l in codes or l.split("-")[0] in codes for l in langs)


class SubScan:
    """Reads the subtitle languages of every movie from Plex, in the background."""

    def __init__(self, db: DB, plex):
        self.db, self.plex = db, plex
        self.progress = {"running": False, "done": 0, "total": 0, "last_run": "", "error": ""}
        self.stop = threading.Event()

    def run(self):
        if self.progress["running"] or not self.plex.configured:
            return
        self.progress.update(running=True, done=0, total=0, error="")
        try:
            def cb(i, n):
                self.progress.update(done=i, total=n)
            items = self.plex.movie_streams(cb, self.stop)
            keep = {i["key"] for i in items}
            for it in items:
                self.db.execute("INSERT OR REPLACE INTO movie_subs (rating_key, title, year, path, langs, checked_at) VALUES (?,?,?,?,?,?)",
                                (it["key"], it["title"], str(it["year"]), it["path"], ",".join(it["langs"]), now()))
            for r in self.db.query("SELECT rating_key FROM movie_subs"):
                if r["rating_key"] not in keep and items:
                    self.db.execute("DELETE FROM movie_subs WHERE rating_key=?", (r["rating_key"],))
            self.progress["last_run"] = time.strftime("%Y-%m-%d %H:%M")
        except Exception as e:
            log.warning("Subtitle scan failed: %s", e)
            self.progress["error"] = str(e)[:200]
        self.progress["running"] = False

    def gaps(self, languages: list[str]) -> dict:
        rows = self.db.query("SELECT * FROM movie_subs ORDER BY title COLLATE NOCASE")
        missing = []
        for r in rows:
            langs = [x for x in (r["langs"] or "").split(",") if x]
            lack = [l for l in languages if not has_lang(langs, l)]
            if lack:
                missing.append({"title": r["title"], "year": r["year"], "missing": lack, "have": langs})
        return {"total": len(rows), "missing": missing, "progress": self.progress}


def plex_owned(albums: dict, artist_folder: str) -> set[str]:
    return albums.get(library.normalize(artist_folder), set())
