"""Find new releases for artists in the library using the MusicBrainz API."""
import logging
import time
from datetime import date, timedelta

import requests

from . import library, musicsources
from .db import DB, now

log = logging.getLogger("releases")

PLEX_ALBUMS: dict = {}   # {normalized artist: {normalized albums}} from Plex, refreshed by the web app

MB = "https://musicbrainz.org/ws/2"
HEADERS = {"User-Agent": "ServerAssistant/1.0 (personal media library helper)", "Accept": "application/json"}
_last_call = 0.0


def _get(path: str, params: dict) -> dict:
    """MusicBrainz allows ~1 request per second."""
    global _last_call
    wait = 1.1 - (time.time() - _last_call)
    if wait > 0:
        time.sleep(wait)
    params = {**params, "fmt": "json"}
    for attempt in range(3):
        _last_call = time.time()
        r = requests.get(f"{MB}/{path}", params=params, headers=HEADERS, timeout=30)
        if r.status_code == 503:  # rate limited
            time.sleep(3 * (attempt + 1))
            continue
        r.raise_for_status()
        return r.json()
    r.raise_for_status()
    return {}


def lookup_artist(name: str) -> tuple[str, str]:
    """Return (mbid, canonical name), or ('', '') when there's no confident match."""
    data = _get("artist/", {"query": f'artist:"{name}"', "limit": 5})
    target = library.normalize(name)
    best = None
    for a in data.get("artists", []):
        names = [a.get("name", ""), a.get("sort-name", "")] + [x.get("name", "") for x in a.get("aliases", []) or []]
        exact = any(library.normalize(n) == target for n in names)
        score = int(a.get("score", 0)) + (50 if exact else 0)
        if best is None or score > best[0]:
            best = (score, a)
    if best and best[0] >= 140:  # exact name match with a high search score
        return best[1]["id"], best[1]["name"]
    return "", ""


def release_groups(mbid: str) -> list[dict]:
    out, offset = [], 0
    while True:
        data = _get("release-group", {"artist": mbid, "limit": 100, "offset": offset})
        groups = data.get("release-groups", [])
        out.extend(groups)
        offset += len(groups)
        if not groups or offset >= data.get("release-group-count", 0):
            return out


def _candidates(db: DB, cfg: dict, folder: str) -> list[dict]:
    """Collect releases for one artist from every enabled source (MusicBrainz first)."""
    music = cfg["music"]
    sources = [x.lower() for x in music.get("sources", ["musicbrainz", "deezer", "itunes"])]
    found: list[dict] = []

    if "musicbrainz" in sources:
        try:
            row = db.query("SELECT * FROM artists WHERE folder=?", (folder,))
            if row and row[0]["mbid"] is not None:
                mbid, mb_name = row[0]["mbid"], row[0]["mb_name"]
            else:
                mbid, mb_name = lookup_artist(folder)
                db.execute("INSERT OR REPLACE INTO artists (folder, mbid, mb_name, last_check) VALUES (?,?,?,?)",
                           (folder, mbid, mb_name, None))
            if mbid:
                for g in release_groups(mbid):
                    found.append({"id": g["id"], "title": g["title"], "type": g.get("primary-type") or "",
                                  "date": g.get("first-release-date") or "",
                                  "cover": f"https://coverartarchive.org/release-group/{g['id']}/front-250",
                                  "url": f"https://musicbrainz.org/release-group/{g['id']}",
                                  "secondary": g.get("secondary-types") or [], "src": "musicbrainz"})
        except Exception as e:
            log.warning("MusicBrainz failed for %s: %s", folder, e)

    for name in ("deezer", "itunes"):
        if name not in sources:
            continue
        try:
            for r in musicsources.SOURCES[name](folder):
                found.append({**r, "src": name})
        except Exception as e:
            log.warning("%s failed for %s: %s", name, folder, e)
        time.sleep(0.2)
    return found


def check_artist(db: DB, cfg: dict, folder: str) -> int:
    music = cfg["music"]
    cutoff = (date.today() - timedelta(days=int(music["lookback_days"]))).isoformat()
    wanted_types = {t.lower() for t in music["release_types"]}
    skip_secondary = {t.lower() for t in music["skip_secondary_types"]}
    owned = library.owned_albums(music["roots"], folder) | PLEX_ALBUMS.get(library.normalize(folder), set())
    # releases we already know for this artist, so the same album from two sources shows up once
    seen = {(library._core(library.normalize(r["title"])), (r["type"] or "").lower())
            for r in db.query("SELECT title, type FROM releases WHERE artist=?", (folder,))}
    added = 0

    for g in _candidates(db, cfg, folder):
        d = g["date"]
        if len(d) < 4:
            continue
        if d < cutoff:
            # older albums you don't own: the "Missing albums" list (off by default)
            if music.get("catalog_gaps") and g["type"] == "Album" and not g["secondary"] and not library.is_owned(g["title"], owned):
                key = (library._core(library.normalize(g["title"])), "album")
                if key not in seen:
                    seen.add(key)
                    db.execute("INSERT OR IGNORE INTO releases (mbid, artist, title, type, date, status, found_at, cover, url, src) "
                               "VALUES (?,?,?,?,?,?,?,?,?,?)", (g["id"], folder, g["title"], g["type"], d, "gap", now(), g["cover"], g["url"], g["src"]))
            continue
        if (g["type"] or "").lower() not in wanted_types:
            continue
        if any(s.lower() in skip_secondary for s in g["secondary"]):
            continue
        key = (library._core(library.normalize(g["title"])), g["type"].lower())
        if key in seen:
            continue
        seen.add(key)
        status = "got" if library.is_owned(g["title"], owned) else "new"
        cur = db.execute(
            "INSERT OR IGNORE INTO releases (mbid, artist, title, type, date, status, found_at, cover, url, src) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (g["id"], folder, g["title"], g["type"], d, status, now(), g["cover"], g["url"], g["src"]),
        )
        if cur.rowcount and status == "new":
            added += 1

    db.execute("UPDATE artists SET last_check=? WHERE folder=?", (now(), folder))
    return added


def check_all(db: DB, cfg: dict, progress: dict | None = None) -> int:
    skipped = {r["artist"] for r in db.query("SELECT artist FROM skipped_artists")}
    artists = [a for a in library.list_artists(cfg["music"]["roots"]) if a not in skipped]
    total = 0
    for i, folder in enumerate(artists, 1):
        if progress is not None:
            progress.update(current=folder, done=i - 1, total=len(artists))
        try:
            total += check_artist(db, cfg, folder)
        except Exception as e:  # keep going if one artist fails
            log.warning("Release check failed for %s: %s", folder, e)
    if progress is not None:
        progress.update(current="", done=len(artists), total=len(artists))
    log.info("Release check done: %d new release(s) across %d artists", total, len(artists))
    return total


def refresh_owned(db: DB, cfg: dict) -> None:
    """Mark listed releases as 'got' once the album shows up in the library (folders or Plex)."""
    lib = cfg["music"]["roots"]
    for r in db.query("SELECT * FROM releases WHERE status IN ('new','wanted','gap')"):
        owned = library.owned_albums(lib, r["artist"]) | PLEX_ALBUMS.get(library.normalize(r["artist"]), set())
        if library.is_owned(r["title"], owned):
            db.execute("UPDATE releases SET status='got' WHERE mbid=?", (r["mbid"],))
