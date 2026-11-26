"""Movies you might be missing: other films by the directors in your Plex library, and missing parts of film series
you own some of. Filmographies come from Wikidata (free, no account or key).

Light on the server: the director list is read from Plex once every 10 minutes at most, a director's films are looked up
only when you open that director (and remembered for a week), and series are checked in the background once a week.
"""
from __future__ import annotations

import difflib
import json
import logging
import re
import threading
import time
import unicodedata
from urllib.parse import quote

import requests

from . import netfetch

log = logging.getLogger("films")

SPARQL = "https://query.wikidata.org/sparql?format=json&query="
HEADERS = {"User-Agent": "ServerAssistant/1.0 (personal media library helper; github.com/ak2296/server-assistant)",
           "Accept": "application/sparql-results+json"}
WEEK = 7 * 86400
# film, feature film, animated feature, documentary film, silent film, drama film... (instance of)
FILM_TYPES = "wd:Q11424 wd:Q24869 wd:Q202866 wd:Q93204 wd:Q226730 wd:Q130232 wd:Q17013749 wd:Q20667187"

SCHEMA = """
CREATE TABLE IF NOT EXISTS film_cache (
    id    TEXT PRIMARY KEY,   -- 'director:<name>' or 'series'
    at    REAL,
    data  TEXT
);
CREATE TABLE IF NOT EXISTS film_marks (
    film  TEXT PRIMARY KEY,   -- Wikidata id (Q...)
    mark  TEXT,               -- skip | want
    title TEXT,
    year  TEXT,
    at    REAL
);
"""


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    s = s.replace("&", " and ")
    s = re.sub(r"^(the|a|an)\s+", "", re.sub(r"[^a-z0-9]+", " ", s).strip())
    return s.strip()


def similar(a: str, b: str, both_years: bool, years_differ: bool) -> bool:
    """Nearly the same title: a misspelling or another spelling, never just a different number (Heat / Heat 2)."""
    if years_differ or not a or not b or re.findall(r"\d+", a) != re.findall(r"\d+", b):
        return False
    if abs(len(a) - len(b)) > max(3, len(a) // 4):
        return False
    need = 0.8 if both_years else 0.9
    sm = difflib.SequenceMatcher(None, a, b)
    return min(len(a), len(b)) >= 4 and sm.quick_ratio() >= need and sm.ratio() >= need


def _q(sparql: str) -> list[dict]:
    url = SPARQL + quote(sparql)
    try:
        r = requests.get(url, headers=HEADERS, timeout=60)
        r.raise_for_status()
        j = r.json()
    except Exception as e:
        log.info("Wikidata: %s; trying Windows' own downloader", e)
        j = netfetch.fetch_json(url, headers=HEADERS)   # for networks that break Python's certificate check
    out = []
    for b in (j or {}).get("results", {}).get("bindings", []):
        out.append({k: v.get("value", "") for k, v in b.items()})
    return out


def _qid(uri: str) -> str:
    return uri.rsplit("/", 1)[-1] if uri else ""


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


class Films:
    def __init__(self, db, plex):
        self.db, self.plex = db, plex
        with db.lock:
            db.conn.executescript(SCHEMA)
            db.conn.commit()
        self._lib = {"at": 0.0, "movies": [], "directors": []}
        self._lock = threading.Lock()
        self.series_progress = {"running": False, "last_run": "", "error": ""}

    # ---------------------------------------------------------------- your library (from Plex)
    def library(self, force: bool = False) -> dict:
        """Your movies and their directors, read from Plex at most every 10 minutes."""
        with self._lock:
            if not force and time.time() - self._lib["at"] < 600 and self._lib["movies"]:
                return self._lib
        movies, counts = [], {}
        for d in self.plex.get("/library/sections").get("Directory", []):
            if d.get("type") != "movie" or self.plex._left_out(d):
                continue
            mc = self.plex.get(f"/library/sections/{d['key']}/all", includeGuids=1)
            for m in mc.get("Metadata", []):
                imdb = ""
                for g in m.get("Guid", []) or []:
                    if g.get("id", "").startswith("imdb://"):
                        imdb = g["id"][7:]
                if not imdb:
                    mm = re.search(r"(tt\d{5,})", m.get("guid", ""))
                    imdb = mm.group(1) if mm else ""
                dirs = [x.get("tag") for x in m.get("Director", []) or [] if x.get("tag")]
                movies.append({"title": m.get("title", ""), "original": m.get("originalTitle", "") or "",
                               "year": str(m.get("year") or ""), "imdb": imdb, "directors": dirs})
                for n in dirs:
                    counts[n] = counts.get(n, 0) + 1
        directors = sorted(({"name": n, "count": c} for n, c in counts.items()), key=lambda x: (-x["count"], x["name"].lower()))
        with self._lock:
            self._lib = {"at": time.time(), "movies": movies, "directors": directors}
            return self._lib

    def _owned(self, lib: dict):
        """A test "do you have this film?" that copes with spelling and language differences: the IMDb id when both
        sides have one, otherwise the title (Plex's title or original title against Wikidata's English or original
        title), exactly or nearly the same (Madame Bovary / Madam Bovarie) when the years agree."""
        imdbs = {m["imdb"] for m in lib["movies"] if m["imdb"]}
        mine = []
        for m in lib["movies"]:
            y = int(m["year"]) if m["year"].isdigit() else 0
            for t in {norm(m["title"]), norm(m.get("original", ""))} - {""}:
                mine.append((t, y))
        exact: dict[str, list[int]] = {}
        first: dict[str, list] = {}   # by first letter, so the near-match test only compares likely candidates
        for t, y in mine:
            exact.setdefault(t, []).append(y)
            first.setdefault(t[0], []).append((t, y))

        def has(f: dict) -> bool:
            if f.get("imdb") and f["imdb"] in imdbs:
                return True
            y = int(f["year"]) if str(f.get("year", "")).isdigit() else 0
            for t in {norm(f.get("title", "")), norm(f.get("original", ""))} - {""}:
                ys = exact.get(t)
                if ys is not None and (not y or any(not oy or abs(oy - y) <= 1 for oy in ys)):
                    return True
                for ot, oy in first.get(t[0], []):
                    if similar(t, ot, both_years=bool(y and oy and abs(oy - y) <= 1), years_differ=bool(y and oy and abs(oy - y) > 1)):
                        return True
            return False
        return has

    # ---------------------------------------------------------------- cache
    def _cache_get(self, key: str, max_age: float):
        r = self.db.query("SELECT at, data FROM film_cache WHERE id=?", (key,))
        if r and time.time() - r[0]["at"] < max_age:
            return json.loads(r[0]["data"])
        return None

    def _cache_put(self, key: str, data):
        self.db.execute("INSERT OR REPLACE INTO film_cache (id, at, data) VALUES (?,?,?)", (key, time.time(), json.dumps(data)))

    def marks(self) -> dict:
        return {r["film"]: r["mark"] for r in self.db.query("SELECT film, mark FROM film_marks")}

    def mark(self, film: str, mark: str, title: str = "", year: str = ""):
        if mark in ("skip", "want"):
            self.db.execute("INSERT OR REPLACE INTO film_marks (film, mark, title, year, at) VALUES (?,?,?,?,?)",
                            (film, mark, title, year, time.time()))
        else:
            self.db.execute("DELETE FROM film_marks WHERE film=?", (film,))

    def wanted(self) -> list[dict]:
        return self.db.query("SELECT film, title, year FROM film_marks WHERE mark='want' ORDER BY at DESC")

    # ---------------------------------------------------------------- a director's films
    def _filmography(self, name: str) -> list[dict]:
        """All films directed by people with this name, grouped by person, from Wikidata."""
        rows = _q(f'''SELECT ?person ?film ?filmLabel ?date ?imdb ?orig WHERE {{
  ?person rdfs:label "{_esc(name)}"@en .
  ?film wdt:P57 ?person ; wdt:P31 ?type .
  VALUES ?type {{ {FILM_TYPES} }}
  OPTIONAL {{ ?film wdt:P577 ?date }}
  OPTIONAL {{ ?film wdt:P345 ?imdb }}
  OPTIONAL {{ ?film wdt:P1476 ?orig }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en,mul". }}
}} LIMIT 900''')
        films: dict[str, dict] = {}
        for r in rows:
            q = _qid(r.get("film", ""))
            if not q:
                continue
            f = films.setdefault(q, {"id": q, "person": _qid(r.get("person", "")), "title": r.get("filmLabel", ""),
                                     "year": "", "date": "", "imdb": "", "original": ""})
            if r.get("orig") and not f["original"]:
                f["original"] = r["orig"]
            d = r.get("date", "")[:10]
            if d and (not f["date"] or d < f["date"]):   # earliest release date
                f["date"], f["year"] = d, d[:4]
            if r.get("imdb", "").startswith("tt"):
                f["imdb"] = r["imdb"]
        return [f for f in films.values() if not re.fullmatch(r"Q\d+", f["title"])]   # no English title: skip

    def director(self, name: str, force: bool = False) -> dict:
        key = "director:" + name
        films = None if force else self._cache_get(key, WEEK)
        if films is None:
            films = self._filmography(name)
            self._cache_put(key, films)
        lib = self.library()
        has = self._owned(lib)
        # more than one person with this name: keep the one whose films you own most of
        by_person: dict[str, list] = {}
        for f in films:
            by_person.setdefault(f["person"], []).append(f)
        if len(by_person) > 1:
            best = max(by_person.values(), key=lambda fs: (sum(1 for f in fs if has(f)), len(fs)))
            films = best
        today = time.strftime("%Y-%m-%d")
        marks = self.marks()
        owned, missing, upcoming = [], [], []
        for f in sorted(films, key=lambda f: f["date"] or "9999"):
            item = dict(f, mark=marks.get(f["id"], ""))
            if has(f):
                owned.append(item)
            elif f["date"] and f["date"] > today:
                upcoming.append(item)
            else:
                missing.append(item)
        return {"name": name, "found": bool(films), "owned": owned, "missing": missing, "upcoming": upcoming}

    # ---------------------------------------------------------------- series you own part of
    def series(self, max_age: float = WEEK) -> dict:
        cached = self._cache_get("series", max_age)
        return cached or {"series": [], "checked": ""}

    def check_series(self):
        """Background: which of your movies belong to a series, and which parts of those series are missing."""
        if self.series_progress["running"]:
            return
        self.series_progress.update(running=True, error="")
        try:
            lib = self.library(force=True)
            imdbs = sorted({m["imdb"] for m in lib["movies"] if m["imdb"]})
            series: dict[str, str] = {}
            for i in range(0, len(imdbs), 150):     # which series do your movies belong to
                vals = " ".join(f'"{x}"' for x in imdbs[i:i + 150])
                for r in _q(f'''SELECT DISTINCT ?series ?seriesLabel WHERE {{
  VALUES ?imdb {{ {vals} }}
  ?film wdt:P345 ?imdb ; wdt:P179 ?series .
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en,mul". }}
}}'''):
                    series[_qid(r["series"])] = r.get("seriesLabel", "")
                time.sleep(1)
            has = self._owned(lib)
            today = time.strftime("%Y-%m-%d")
            out = []
            ids = list(series)
            for i in range(0, len(ids), 60):        # all films of those series
                vals = " ".join(f"wd:{x}" for x in ids[i:i + 60])
                parts: dict[str, dict] = {}
                for r in _q(f'''SELECT ?series ?film ?filmLabel ?date ?imdb ?ord ?orig WHERE {{
  VALUES ?series {{ {vals} }}
  ?film p:P179 ?st . ?st ps:P179 ?series .
  ?film wdt:P31 ?type . VALUES ?type {{ {FILM_TYPES} }}
  OPTIONAL {{ ?st pq:P1545 ?ord }}
  OPTIONAL {{ ?film wdt:P1476 ?orig }}
  OPTIONAL {{ ?film wdt:P577 ?date }}
  OPTIONAL {{ ?film wdt:P345 ?imdb }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en,mul". }}
}}'''):
                    s, q = _qid(r["series"]), _qid(r["film"])
                    f = parts.setdefault(s, {}).setdefault(q, {"id": q, "title": r.get("filmLabel", ""), "date": "", "year": "",
                                                               "imdb": "", "order": r.get("ord", ""), "original": ""})
                    if r.get("orig") and not f["original"]:
                        f["original"] = r["orig"]
                    d = r.get("date", "")[:10]
                    if d and (not f["date"] or d < f["date"]):
                        f["date"], f["year"] = d, d[:4]
                    if r.get("imdb", "").startswith("tt"):
                        f["imdb"] = r["imdb"]
                for s, fs in parts.items():
                    films = [f for f in fs.values() if not re.fullmatch(r"Q\d+", f["title"])]
                    have = [f for f in films if has(f)]
                    miss = [f for f in films if not has(f) and f["date"] and f["date"] <= today]
                    if have and miss and len(films) <= 40:   # very long "series" are franchises, not a set to complete
                        key = lambda f: (float(f["order"]) if re.fullmatch(r"\d+(\.\d+)?", f["order"] or "") else 999, f["date"] or "9999")
                        out.append({"id": s, "name": series.get(s, ""), "have": len(have), "total": len(films),
                                    "missing": sorted(miss, key=key)})
                time.sleep(1)
            out.sort(key=lambda x: x["name"].lower())
            self._cache_put("series", {"series": out, "checked": time.strftime("%Y-%m-%d %H:%M")})
            self.series_progress["last_run"] = time.strftime("%Y-%m-%d %H:%M")
        except Exception as e:
            log.info("Series check failed: %s", e)
            self.series_progress["error"] = str(e)[:200]
        finally:
            self.series_progress["running"] = False
