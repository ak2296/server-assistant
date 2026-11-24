"""Read-mostly Plex Media Server client (local API on port 32400)."""
import logging
import os
import re
import sys
import time
from pathlib import Path

import requests

log = logging.getLogger("plex")


def find_local_token() -> str:
    """On Windows, Plex keeps its token in the registry (or Preferences.xml)."""
    if sys.platform == "win32":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Plex, Inc.\Plex Media Server") as k:
                return winreg.QueryValueEx(k, "PlexOnlineToken")[0]
        except OSError:
            pass
        prefs = Path(os.environ.get("LOCALAPPDATA", "")) / "Plex Media Server" / "Preferences.xml"
    else:
        prefs = Path("/var/lib/plexmediaserver/Library/Application Support/Plex Media Server/Preferences.xml")
    try:
        m = re.search(r'PlexOnlineToken="([^"]+)"', prefs.read_text(encoding="utf-8", errors="ignore"))
        return m.group(1) if m else ""
    except OSError:
        return ""


def _quality(media: dict) -> str:
    res = str(media.get("videoResolution") or "")
    if res:
        return f"{res}p" if res.isdigit() else res.upper()
    codec = str(media.get("audioCodec") or "").upper()
    bitrate = media.get("bitrate")
    return f"{codec} {bitrate} kbps" if codec and bitrate else codec


def _norm_path(p: str) -> str:
    return p.replace("\\", "/").rstrip("/").lower()


class Plex:
    def __init__(self, cfg: dict):
        p = cfg["plex"]
        self.url = p["url"].rstrip("/")
        self.exclude = {"artist": set(), "movie": set()}  # folders the user left out (set by the server)
        self.token = p.get("token") or find_local_token()
        self.s = requests.Session()
        self.s.headers.update({
            "Accept": "application/json",
            "X-Plex-Token": self.token,
            "X-Plex-Client-Identifier": "server-assistant",
            "X-Plex-Product": "Server Assistant",
        })
        self._accounts: dict[int, str] = {}
        self._accounts_at = 0.0

    @property
    def configured(self) -> bool:
        return bool(self.token)

    def get(self, path: str, **params) -> dict:
        r = self.s.get(f"{self.url}{path}", params=params, timeout=6)
        r.raise_for_status()
        if not r.content:
            return {}
        data = r.json()
        return data.get("MediaContainer", data)

    # ---------- building blocks ----------
    def server(self) -> dict:
        mc = self.get("/")
        return {
            "name": mc.get("friendlyName"), "version": mc.get("version"),
            "platform": f'{mc.get("platform", "")} {mc.get("platformVersion", "")}'.strip(),
            "plex_pass": bool(mc.get("myPlexSubscription")), "account": mc.get("myPlexUsername"),
            "transcoder_video_sessions": mc.get("transcoderActiveVideoSessions", 0),
        }

    def remote_access(self) -> dict:
        try:
            r = self.s.get(f"{self.url}/myplex/account", timeout=10)
            r.raise_for_status()
            mp = r.json().get("MyPlex", {})
            return {"state": mp.get("mappingState"), "error": mp.get("mappingError"),
                    "public": f'{mp.get("publicAddress", "")}:{mp.get("publicPort", "")}'.strip(":"),
                    "signed_in": mp.get("signInState")}
        except Exception:
            return {}

    def update(self) -> dict:
        try:
            mc = self.get("/updater/status")
        except Exception:
            return {}
        rel = (mc.get("Release") or [{}])[0]
        return {"available": bool(rel.get("version")), "version": rel.get("version"), "state": rel.get("state")}

    def accounts(self) -> dict[int, str]:
        if time.time() - self._accounts_at > 3600:
            try:
                self._accounts = {a["id"]: a.get("name") or "Owner" for a in self.get("/accounts").get("Account", [])}
                self._accounts_at = time.time()
            except Exception:
                pass
        return self._accounts

    def sessions(self) -> list[dict]:
        out = []
        for m in self.get("/status/sessions").get("Metadata", []):
            media = (m.get("Media") or [{}])[0]
            ts = m.get("TranscodeSession") or {}
            player, sess = m.get("Player") or {}, m.get("Session") or {}
            decision = "Direct play"
            if ts:
                v, a = ts.get("videoDecision"), ts.get("audioDecision")
                decision = "Transcoding" if "transcode" in (v, a) else "Direct stream"
                if ts.get("transcodeHwRequested") or ts.get("transcodeHwFullPipeline"):
                    decision += " (HW)"
            out.append({
                "session_id": sess.get("id"),
                "type": m.get("type"),
                "title": m.get("title"),
                "subtitle": " · ".join(x for x in (
                    m.get("grandparentTitle"), m.get("parentTitle") if m.get("type") == "track" else
                    (f'S{m.get("parentIndex")}E{m.get("index")}' if m.get("type") == "episode" else str(m.get("year") or ""))
                ) if x),
                "thumb": m.get("grandparentThumb") if m.get("type") == "episode" else (m.get("parentThumb") if m.get("type") == "track" else m.get("thumb")),
                "user": (m.get("User") or {}).get("title"),
                "player": player.get("title") or player.get("product"),
                "platform": player.get("platform"),
                "state": player.get("state"),  # playing | paused | buffering
                "local": player.get("local"),
                "location": sess.get("location"),
                "bandwidth_kbps": sess.get("bandwidth"),
                "progress": (m.get("viewOffset", 0) / m["duration"] * 100) if m.get("duration") else 0,
                "duration": m.get("duration"), "offset": m.get("viewOffset"),
                "quality": _quality(media),
                "decision": decision,
                "transcode_speed": ts.get("speed"),
                "throttled": ts.get("throttled"),
            })
        return out

    def libraries(self) -> list[dict]:
        # second count per type: shows -> episodes (4), music -> albums (9)
        extra = {"show": (4, "episodes"), "artist": (9, "albums")}
        unit = {"movie": "movies", "show": "shows", "artist": "artists", "photo": "items"}
        libs = []
        for d in self.get("/library/sections").get("Directory", []):
            key, typ = d["key"], d.get("type")
            lib = {"key": key, "title": d.get("title"), "type": typ, "refreshing": d.get("refreshing", False),
                   "scanned_at": d.get("scannedAt"),
                   "locations": [loc.get("path") for loc in d.get("Location", [])]}
            try:
                lib["count"] = self.get(f"/library/sections/{key}/all", **{"X-Plex-Container-Start": 0, "X-Plex-Container-Size": 0}).get("totalSize")
                lib["unit"] = unit.get(typ, "items")
                if typ in extra:
                    t, label = extra[typ]
                    lib["count2"] = self.get(f"/library/sections/{key}/all", type=t, **{"X-Plex-Container-Start": 0, "X-Plex-Container-Size": 0}).get("totalSize")
                    lib["unit2"] = label
            except Exception:
                pass
            libs.append(lib)
        return libs

    def recently_added(self, n: int = 12) -> list[dict]:
        items = []
        for m in self.get("/library/recentlyAdded", **{"X-Plex-Container-Start": 0, "X-Plex-Container-Size": n}).get("Metadata", []):
            items.append({"title": m.get("title"), "parent": m.get("parentTitle") or m.get("grandparentTitle"),
                          "type": m.get("type"), "year": m.get("year"), "added_at": m.get("addedAt"),
                          "thumb": m.get("thumb") or m.get("parentThumb")})
        return items

    def recent_split(self, n: int = 16) -> dict:
        """Recently added music and video from one small request, for the two carousels."""
        out = {"audio": [], "video": []}
        for d in self.get("/library/sections").get("Directory", []):
            typ = d.get("type")
            if typ not in ("artist", "movie", "show") or self._left_out(d):
                continue
            params = {"X-Plex-Container-Start": 0, "X-Plex-Container-Size": n}
            if typ == "artist":
                params["type"] = 9  # albums, not single tracks
            try:
                md = self.get(f"/library/sections/{d['key']}/recentlyAdded", **params).get("Metadata", [])
            except Exception as e:
                log.debug("recently added in %s failed: %s", d.get("title"), e)
                continue
            for m in md:
                out["audio" if typ == "artist" else "video"].append(
                    {"title": m.get("title"), "parent": m.get("parentTitle") or m.get("grandparentTitle"),
                     "type": m.get("type"), "year": m.get("year"), "added_at": m.get("addedAt"),
                     "thumb": m.get("thumb") or m.get("parentThumb") or m.get("grandparentThumb")})
        for k in out:
            seen, uniq = set(), []
            for x in sorted(out[k], key=lambda x: -(x.get("added_at") or 0)):
                key = (x["title"], x["parent"], x["year"])
                if key not in seen:
                    seen.add(key); uniq.append(x)
            out[k] = uniq[:n]
        return out

    def history(self, n: int = 80) -> list[dict]:
        names = self.accounts()
        rows = []
        mc = self.get("/status/sessions/history/all", sort="viewedAt:desc",
                      **{"X-Plex-Container-Start": 0, "X-Plex-Container-Size": n})
        for m in mc.get("Metadata", []):
            rows.append({"title": m.get("title"), "parent": m.get("grandparentTitle") or m.get("parentTitle"),
                         "type": m.get("type"), "viewed_at": m.get("viewedAt"),
                         "user": names.get(m.get("accountID"), "")})
        return rows

    def activities(self) -> list[dict]:
        return [{"title": a.get("title"), "subtitle": a.get("subtitle"), "progress": a.get("progress"), "type": a.get("type")}
                for a in self.get("/activities").get("Activity", [])]

    def _left_out(self, d: dict) -> bool:
        """True when every folder of this library was unticked in Settings."""
        paths = [_norm_path(l.get("path", "")) for l in d.get("Location", []) if l.get("path")]
        ex = self.exclude.get(d.get("type"), set())
        return bool(paths) and all(p in ex for p in paths)

    def folders(self) -> dict:
        """Folders Plex uses for each kind of library: {"artist": [...], "movie": [...], "show": [...]}."""
        out = {"artist": [], "movie": [], "show": []}
        for d in self.get("/library/sections").get("Directory", []):
            for loc in d.get("Location", []):
                if d.get("type") in out and loc.get("path"):
                    out[d["type"]].append(loc["path"])
        return out

    def movie_index(self) -> dict:
        """{lower-case file path: {"title", "year", "imdb"}} for every movie Plex knows."""
        index = {}
        for d in self.get("/library/sections").get("Directory", []):
            if d.get("type") != "movie":
                continue
            mc = self.get(f"/library/sections/{d['key']}/all", includeGuids=1)
            for m in mc.get("Metadata", []):
                imdb = ""
                for g in m.get("Guid", []) or []:
                    gid = g.get("id", "")
                    if gid.startswith("imdb://"):
                        imdb = gid[7:]
                if not imdb:  # older agents: com.plexapp.agents.imdb://tt0113277?lang=en
                    mm = re.search(r"(tt\d{5,})", m.get("guid", ""))
                    imdb = mm.group(1) if mm else ""
                info = {"title": m.get("title"), "year": str(m.get("year") or ""), "imdb": imdb}
                for media in m.get("Media", []) or []:
                    for part in media.get("Part", []) or []:
                        if part.get("file"):
                            index[part["file"].replace("\\", "/").lower()] = info
        return index

    def music_albums(self) -> dict[str, set[str]]:
        """{normalized artist: {normalized album titles}} for every music library, as Plex knows them."""
        from . import library
        out: dict[str, set[str]] = {}
        for d in self.get("/library/sections").get("Directory", []):
            if d.get("type") != "artist" or self._left_out(d):
                continue
            for a in self.get(f"/library/sections/{d['key']}/albums").get("Metadata", []):
                artist = library.normalize(a.get("parentTitle") or "")
                title = library.normalize(a.get("title") or "")
                if artist and title:
                    out.setdefault(artist, set()).add(title)
        return out

    def movie_streams(self, progress=None, stop=None) -> list[dict]:
        """Every movie with the subtitle languages Plex sees (embedded and sidecar files)."""
        movies = []
        for d in self.get("/library/sections").get("Directory", []):
            if d.get("type") == "movie" and not self._left_out(d):
                movies += self.get(f"/library/sections/{d['key']}/all").get("Metadata", [])
        out = []
        for i, m in enumerate(movies, 1):
            if stop is not None and stop.is_set():
                break
            time.sleep(0.03)  # be gentle: this walks every movie, don't slow Plex down for people watching
            if progress:
                progress(i, len(movies))
            langs, path = set(), ""
            try:
                detail = self.get(f"/library/metadata/{m['ratingKey']}").get("Metadata", [{}])[0]
            except Exception:
                continue
            for media in detail.get("Media", []) or []:
                for part in media.get("Part", []) or []:
                    path = path or part.get("file", "")
                    for st in part.get("Stream", []) or []:
                        if st.get("streamType") == 3:
                            langs.add((st.get("languageCode") or st.get("language") or "?").lower())
            out.append({"key": str(m["ratingKey"]), "title": m.get("title", ""), "year": m.get("year") or "",
                        "path": path, "langs": sorted(langs)})
            time.sleep(0.02)  # be gentle with Plex
        return out

    # ---------- actions ----------
    def scan(self, section_key: str | None = None, path: str | None = None):
        keys = [section_key] if section_key else [d["key"] for d in self.get("/library/sections").get("Directory", [])]
        for k in keys:
            params = {"path": path} if path else {}
            self.s.get(f"{self.url}/library/sections/{k}/refresh", params=params, timeout=10).raise_for_status()

    def scan_path(self, path: Path):
        """Scan only the library section that contains this folder (used after sorting music)."""
        p = str(path).replace("\\", "/").lower()
        for d in self.get("/library/sections").get("Directory", []):
            for loc in d.get("Location", []):
                root = loc.get("path", "").replace("\\", "/").lower().rstrip("/")
                if root and p.startswith(root):
                    self.scan(d["key"], str(path))
                    return True
        return False

    def stop_stream(self, session_id: str, reason: str):
        """Needs Plex Pass."""
        r = self.s.get(f"{self.url}/status/sessions/terminate", params={"sessionId": session_id, "reason": reason}, timeout=10)
        r.raise_for_status()

    def thumb(self, path: str, width: int = 240) -> tuple[bytes, str]:
        r = self.s.get(f"{self.url}/photo/:/transcode",
                       params={"url": path, "width": width, "height": int(width * 1.5), "minSize": 1, "upscale": 1},
                       timeout=15)
        r.raise_for_status()
        return r.content, r.headers.get("Content-Type", "image/jpeg")

    # ---------- everything for the dashboard ----------
    def overview(self) -> dict:
        out = {"configured": self.configured, "online": False}
        if not self.configured:
            return out
        try:
            out["server"] = self.server()
            out["online"] = True
        except Exception as e:
            out["error"] = str(e)[:200]
            return out
        for key, fn in (("sessions", self.sessions), ("activities", self.activities)):
            try:
                out[key] = fn()
            except Exception as e:
                out[key] = []
                log.debug("%s failed: %s", key, e)
        return out

    def details(self) -> dict:
        """Slower data - refreshed less often."""
        out = {}
        for key, fn in (("libraries", self.libraries), ("history", self.history),
                        ("remote", self.remote_access), ("update", self.update)):
            try:
                out[key] = fn()
            except Exception as e:
                out[key] = [] if key in ("libraries", "recent", "history") else {}
                log.debug("%s failed: %s", key, e)
        return out
