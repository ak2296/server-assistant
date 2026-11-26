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


def _exact_candidates(name: str) -> list[dict]:
    """MusicBrainz artists whose name (or alias) is exactly this name, best search score first."""
    data = _get("artist/", {"query": f'artist:"{name}"', "limit": 10})
    target = library.normalize(name)
    out = []
    for a in data.get("artists", []):
        names = [a.get("name", ""), a.get("sort-name", "")] + [x.get("name", "") for x in a.get("aliases", []) or []]
        if any(library.normalize(n) == target for n in names) and int(a.get("score", 0)) >= 90:
            out.append(a)
    out.sort(key=lambda a: -int(a.get("score", 0)))
    return out


def _overlap(mbid: str, owned: set[str]) -> tuple[int, list[str]]:
    """How many of the albums you own this MusicBrainz artist has made (+ a few of its titles, for display)."""
    groups = release_groups(mbid)
    hits = sum(1 for g in groups if library.is_owned(g["title"], owned))
    recent = [g["title"] for g in sorted(groups, key=lambda g: g.get("first-release-date") or "", reverse=True)[:4]]
    return hits, recent


def lookup_artist(name: str, owned: set[str] | None = None) -> tuple[str, str]:
    """Return (mbid, canonical name). ('', '') = nothing found; ('', 'AMBIGUOUS') = several artists share this name
    and your albums don't settle which one is yours (you are asked to choose)."""
    cands = _exact_candidates(name)
    if not cands:
        return "", ""
    if len(cands) == 1:
        return cands[0]["id"], cands[0]["name"]
    scored = []
    for a in cands[:4]:
        hits, _ = _overlap(a["id"], owned or set())
        scored.append((hits, int(a.get("score", 0)), a))
    scored.sort(key=lambda x: (-x[0], -x[1]))
    if scored[0][0] > 0 and (len(scored) < 2 or scored[0][0] > scored[1][0]):
        return scored[0][2]["id"], scored[0][2]["name"]
    return "", "AMBIGUOUS"


def artist_candidates(name: str, owned: set[str]) -> list[dict]:
    """For the 'Wrong artist?' picker: who could this folder be, and what do they have in common with your albums."""
    out = []
    for a in _exact_candidates(name)[:5] or []:
        try:
            hits, recent = _overlap(a["id"], owned)
        except Exception:
            hits, recent = 0, []
        life = a.get("life-span") or {}
        out.append({"mbid": a["id"], "name": a.get("name", ""), "note": a.get("disambiguation", ""), "country": a.get("country") or a.get("area", {}).get("name", ""),
                    "type": a.get("type", ""), "begin": (life.get("begin") or "")[:4], "owned": hits, "recent": recent})
    out.sort(key=lambda c: -c["owned"])
    return out


def release_groups(mbid: str) -> list[dict]:
    out, offset = [], 0
    while True:
        data = _get("release-group", {"artist": mbid, "limit": 100, "offset": offset})
        groups = data.get("release-groups", [])
        out.extend(groups)
        offset += len(groups)
        if not groups or offset >= data.get("release-group-count", 0):
            return out


def _candidates(db: DB, cfg: dict, folder: str, owned: set[str] | None = None) -> list[dict]:
    """Collect releases for one artist from every enabled source (MusicBrainz first)."""
    music = cfg["music"]
    sources = [x.lower() for x in music.get("sources", ["musicbrainz", "deezer", "itunes"])]
    found: list[dict] = []
    owned = owned or set()
    pin = (db.query("SELECT mbid FROM artist_pins WHERE folder=?", (folder,)) or [{}])[0].get("mbid") or ""
    known = set(owned)   # titles that tell apart two artists with the same name: what you own + what MusicBrainz lists

    if "musicbrainz" in sources or pin:
        try:
            row = db.query("SELECT * FROM artists WHERE folder=?", (folder,))
            if pin:
                mbid, mb_name = pin, (row[0]["mb_name"] if row else "")
            elif row and row[0]["mbid"] is not None and row[0]["mb_name"] != "AMBIGUOUS":
                mbid, mb_name = row[0]["mbid"], row[0]["mb_name"]
            else:
                mbid, mb_name = lookup_artist(folder, owned)
                db.execute("INSERT OR REPLACE INTO artists (folder, mbid, mb_name, last_check) VALUES (?,?,?,?)",
                           (folder, mbid, mb_name, None))
            if mbid:
                for g in release_groups(mbid):
                    known.add(library.normalize(g["title"]))
                    found.append({"id": g["id"], "title": g["title"], "type": g.get("primary-type") or "",
                                  "date": g.get("first-release-date") or "",
                                  "cover": f"https://coverartarchive.org/release-group/{g['id']}/front-250",
                                  "url": f"https://musicbrainz.org/release-group/{g['id']}",
                                  "secondary": g.get("secondary-types") or [], "src": "musicbrainz"})
            elif mb_name == "AMBIGUOUS":
                return []   # several artists share the name: nothing is listed until you choose (Music > Skipped/unmatched)
        except Exception as e:
            log.warning("MusicBrainz failed for %s: %s", folder, e)

    for name in ("deezer", "itunes"):
        if name not in sources:
            continue
        try:
            for r in musicsources.SOURCES[name](folder, known, strict=bool(pin)):
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
    # releases we already know for this artist, so the same record from two sources (or an edition) shows up once
    seen = [r["title"] for r in db.query("SELECT title FROM releases WHERE artist=?", (folder,))]
    is_seen = lambda t: any(library.same_title(t, x) for x in seen)
    added = 0

    cands = _candidates(db, cfg, folder, owned)
    # releases you said belong to a same-name stranger: never offered again, unless your chosen artist has them on MusicBrainz
    blocked = [r["title"] for r in db.query("SELECT title FROM release_blocks WHERE artist=?", (folder,))]
    if blocked:
        mb = [g["title"] for g in cands if g.get("src") == "musicbrainz"]
        cands = [g for g in cands if g.get("src") == "musicbrainz"
                 or not any(library.same_title(g["title"], b) for b in blocked)
                 or any(library.same_title(g["title"], m) for m in mb)]
    for g in cands:
        d = g["date"]
        if len(d) < 4:
            continue
        if d < cutoff:
            # older albums you don't own: the "Missing albums" list (off by default)
            if music.get("catalog_gaps") and g["type"] == "Album" and not g["secondary"] and not library.is_owned(g["title"], owned):
                if not is_seen(g["title"]):
                    seen.append(g["title"])
                    db.execute("INSERT OR IGNORE INTO releases (mbid, artist, title, type, date, status, found_at, cover, url, src) "
                               "VALUES (?,?,?,?,?,?,?,?,?,?)", (g["id"], folder, g["title"], g["type"], d, "gap", now(), g["cover"], g["url"], g["src"]))
            continue
        if (g["type"] or "").lower() not in wanted_types:
            continue
        if any(s.lower() in skip_secondary for s in g["secondary"]):
            continue
        if is_seen(g["title"]):
            continue
        seen.append(g["title"])
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


_RANK = {"got": 0, "wanted": 1, "skipped": 2, "new": 3, "gap": 4}
_SRC = {"musicbrainz": 0, "deezer": 1, "itunes": 2}


def dedupe_releases(db: DB) -> int:
    """Merge copies of the same release that came from different sources. Keeps one; your choice (Later/Skipped/Got) is kept."""
    removed = 0
    rows = db.query("SELECT * FROM releases ORDER BY artist")
    by_artist: dict[str, list[dict]] = {}
    for r in rows:
        by_artist.setdefault(r["artist"], []).append(r)
    for artist, items in by_artist.items():
        used: set[str] = set()
        for r in items:
            if r["mbid"] in used:
                continue
            group = [r] + [o for o in items if o["mbid"] != r["mbid"] and o["mbid"] not in used and library.same_title(r["title"], o["title"])]
            used.update(g["mbid"] for g in group)
            if len(group) == 1:
                continue
            # the status you chose wins; among equals prefer the best source, then an Album over a Single
            best_status = min((g["status"] for g in group), key=lambda x: _RANK.get(x, 9))
            keep = sorted(group, key=lambda g: (_SRC.get(g["src"] or "musicbrainz", 3), {"Album": 0, "EP": 1, "Single": 2}.get(g["type"], 3),
                                                 0 if g["cover"] else 1))[0]
            if keep["status"] != best_status:
                db.execute("UPDATE releases SET status=? WHERE mbid=?", (best_status, keep["mbid"]))
            for g in group:
                if g["mbid"] != keep["mbid"]:
                    db.execute("DELETE FROM releases WHERE mbid=?", (g["mbid"],))
                    removed += 1
    return removed


def check_all(db: DB, cfg: dict, progress: dict | None = None) -> int:
    dedupe_releases(db)
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
