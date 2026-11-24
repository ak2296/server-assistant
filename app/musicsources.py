"""Extra places to look for new releases besides MusicBrainz: Deezer and iTunes.

Both are free, need no key and no login. Each function returns a list of dicts:
    {id, title, type ('Album'|'Single'|'EP'), date 'YYYY-MM-DD', cover, url, secondary: [..]}
"""
import logging
import re
import time

import requests

from . import library

log = logging.getLogger("musicsources")
HEADERS = {"User-Agent": "ServerAssistant/1.0 (personal media library helper)"}


def _get(url: str, params: dict) -> dict:
    for attempt in range(3):
        r = requests.get(url, params=params, headers=HEADERS, timeout=25)
        if r.status_code in (429, 503):
            time.sleep(2 * (attempt + 1))
            continue
        r.raise_for_status()
        return r.json()
    r.raise_for_status()
    return {}


def _secondary(title: str) -> list[str]:
    """Guess MusicBrainz-style secondary types from the title, so the same skip list applies."""
    t = title.lower()
    out = []
    if re.search(r"\blive (at|in|from|on)\b|\(live|\[live|\blive\)$", t):
        out.append("Live")
    if re.search(r"remix|remixes|remixed", t):
        out.append("Remix")
    if re.search(r"\bdemos?\b", t):
        out.append("Demo")
    if re.search(r"greatest hits|best of|anthology|collection\b", t):
        out.append("Compilation")
    return out


# ---------------------------------------------------------------- Deezer
def deezer(name: str) -> list[dict]:
    target = library.normalize(name)
    data = _get("https://api.deezer.com/search/artist", {"q": name, "limit": 10})
    best = None
    for a in data.get("data", []):
        if library.normalize(a.get("name", "")) == target:
            if best is None or a.get("nb_fan", 0) > best.get("nb_fan", 0):
                best = a
    if not best:
        return []
    out = []
    albums = _get(f"https://api.deezer.com/artist/{best['id']}/albums", {"limit": 100})
    for al in albums.get("data", []):
        rtype = {"album": "Album", "single": "Single", "ep": "EP"}.get((al.get("record_type") or "").lower())
        if not rtype or not al.get("release_date"):
            continue
        out.append({"id": f"dz:{al['id']}", "title": al["title"], "type": rtype, "date": al["release_date"],
                    "cover": al.get("cover_medium") or al.get("cover") or "", "url": al.get("link") or "",
                    "secondary": _secondary(al["title"])})
    return out


# ---------------------------------------------------------------- iTunes / Apple Music
_SUFFIX = re.compile(r"\s+-\s+(Single|EP)\s*$", re.I)


def itunes(name: str) -> list[dict]:
    target = library.normalize(name)
    data = _get("https://itunes.apple.com/search", {"term": name, "entity": "musicArtist", "limit": 10})
    artist_id = None
    for a in data.get("results", []):
        if library.normalize(a.get("artistName", "")) == target:
            artist_id = a.get("artistId")
            break
    if not artist_id:
        return []
    data = _get("https://itunes.apple.com/lookup", {"id": artist_id, "entity": "album", "limit": 200, "sort": "recent"})
    out = []
    for al in data.get("results", []):
        if al.get("wrapperType") != "collection":
            continue
        title = al.get("collectionName", "")
        m = _SUFFIX.search(title)
        rtype = {"single": "Single", "ep": "EP"}[m.group(1).lower()] if m else "Album"
        title = _SUFFIX.sub("", title).strip()
        date = (al.get("releaseDate") or "")[:10]
        if not title or not date:
            continue
        cover = (al.get("artworkUrl100") or "").replace("100x100", "300x300")
        out.append({"id": f"it:{al['collectionId']}", "title": title, "type": rtype, "date": date,
                    "cover": cover, "url": al.get("collectionViewUrl") or "", "secondary": _secondary(title)})
    return out


SOURCES = {"deezer": deezer, "itunes": itunes}
