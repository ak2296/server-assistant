"""Background jobs + the phone dashboard (FastAPI)."""
import asyncio
import json
import logging
import os
import re
import psutil
import requests
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, Response
from pydantic import BaseModel

from . import backup, netfetch, tailnet, discovery, library, plex, plexlib, qbit, qbmanager, releases, safety, sorter, films, subdrop, subsources, subtitles, system, unseen
from .config import DATA, ROOT, VERSION, load_config, save_config
from .db import DB

log = logging.getLogger("web")
STATIC = Path(__file__).parent / "static"


class State:
    def __init__(self):
        self.cfg = load_config()
        self.db = DB(DATA / "server_assistant.db")
        self.plex = plex.Plex(self.cfg)
        self.qbit, self.dl_problem, self.dl_mode = None, "", "off"
        self.qbm = qbmanager.Manager(DATA)
        self.qbit = self.make_downloader()
        self.qbit_error = self.dl_problem
        self.monitor = system.SystemMonitor(self.cfg["system"]["drives"])
        self.plex_details = {}
        self.plex_details_at = 0.0
        self.details_busy = False
        self.subscan = plexlib.SubScan(self.db, self.plex)
        self.unseen = unseen.Unseen(self.plex, DATA)
        self.subdrop = subdrop.SubDrop(self.db, self.plex, DATA, self.cfg)
        self.films = films.Films(self.db, self.plex)
        self.backup_busy = False
        self.tailnet = tailnet.Tailnet()
        self.ov, self.ov_at = {}, 0.0          # short-lived cache of the Plex overview
        self.ov_lock = threading.Lock()
        self.lib_info, self.lib_info_at = {}, 0.0  # cache of the (slow) music library scan
        self.sorter = sorter.Sorter(self.db, self.cfg, on_sorted=self.after_sort)
        self.sources_path = DATA / "subtitle_sources.json"
        self.sources = subsources.load_sources(self.sources_path)
        if self.sources is None:
            self.sources = subsources.default_sources(self.cfg["subtitles"])
            for i, src in enumerate(self.sources):
                src["id"] = f"s{i}"
            subsources.save_sources(self.sources_path, self.sources)
        self.plex_folders = {"artist": [], "movie": [], "show": []}
        self.paths_at = 0.0
        try:  # last known Plex folders, so start-up never waits for Plex
            self.plex_folders = json.loads((DATA / "plex_folders.json").read_text())
        except Exception:
            pass
        self.resolve_paths(fetch=False)
        self.stop = threading.Event()
        self.release_progress = {"running": False, "current": "", "done": 0, "total": 0, "last_run": None}
        self.subs_progress = {"running": False, "last_run": None, "last_count": 0}
        self.kick_releases = threading.Event()
        self.kick_subs = threading.Event()
        self.kick_drop = threading.Event()


    def resolve_paths(self, fetch: bool = True):
        """Work out the real music/movie folders: from Plex ("auto") or the custom list."""
        if fetch and self.plex.configured:
            try:
                self.plex_folders = self.plex.folders()
                (DATA / "plex_folders.json").write_text(json.dumps(self.plex_folders))
            except Exception as e:
                log.info("Couldn't read folders from Plex: %s", e)

        def pick(setting, plex_list):
            if setting in ("auto", "", None, []):
                return list(plex_list)
            return [setting] if isinstance(setting, str) else list(setting)

        norm = lambda p: str(p).replace("\\", "/").rstrip("/").lower()
        ex_m = {norm(x) for x in self.cfg["music"].get("exclude", [])}
        ex_v = {norm(x) for x in self.cfg["subtitles"].get("exclude", [])}
        self.plex.exclude = {"artist": ex_m, "movie": ex_v}
        self.cfg["music"]["roots"] = [r for r in pick(self.cfg["music"]["library"], self.plex_folders["artist"]) if norm(r) not in ex_m]
        self.cfg["subtitles"]["roots"] = [r for r in pick(self.cfg["subtitles"]["movie_folders"], self.plex_folders["movie"]) if norm(r) not in ex_v]
        self.paths_at = time.time()

    def downloader_mode(self) -> str:
        """off | external (your own qBittorrent) | managed (the one the app sets up and starts itself)."""
        q = self.cfg["qbittorrent"]
        if q.get("engine", "builtin") == "qbittorrent":
            if not q.get("enabled"):
                return "off"
            if self.qbm.answers(q["url"]):
                self.qbm.note_external()
                return "external"
            if self.qbm.meta.get("external_seen"):
                return "external"   # their own qBittorrent is just not running right now: say so, never replace it
            # nothing was ever listening at that address: use the app's own qBittorrent instead of showing an error
        return "managed"

    def make_downloader(self):
        """The app's own qBittorrent (default) or one you run yourself, depending on Settings."""
        self.dl_problem = ""
        self.dl_mode = self.downloader_mode()
        if self.dl_mode == "off":
            return None
        if self.dl_mode == "external":
            return qbit.QBit(self.cfg)
        if self.qbm.running():
            return qbit.QBit(self.qbm.client_cfg(self.cfg))
        self.dl_problem = qbmanager.describe()
        return None

    def rebuild_clients(self):
        self.plex = plex.Plex(self.cfg)
        self.subscan.plex = self.plex
        self.subdrop.plex = self.plex
        self.films.plex = self.plex
        self.unseen.plex = self.plex
        self.qbit = self.make_downloader()
        self.qbit_error = self.dl_problem
        self.monitor.drives = [d.rstrip("\\/").upper() for d in self.cfg["system"]["drives"]]
        self.plex_details_at = 0

    def after_sort(self, dest):
        if self.cfg["plex"]["scan_after_import"] and self.plex.configured:
            self.plex.scan_path(dest)

    def import_torrent(self, path):
        return sorter.move_item(self.db, self.cfg, path, mode="move", on_sorted=self.after_sort)


state: State | None = None


def _release_loop(st: State):
    every = float(st.cfg["music"]["check_every_hours"]) * 3600
    while not st.stop.is_set():
        st.release_progress["running"] = True
        try:
            releases.refresh_owned(st.db, st.cfg)
            releases.check_all(st.db, st.cfg, st.release_progress)
        except Exception:
            log.exception("Release check crashed")
        st.release_progress.update(running=False, last_run=time.strftime("%Y-%m-%d %H:%M"))
        st.kick_releases.wait(every)
        st.kick_releases.clear()


def _subs_loop(st: State):
    every = float(st.cfg["subtitles"]["check_every_hours"]) * 3600
    while not st.stop.is_set():
        st.subs_progress["running"] = True
        try:
            n = subtitles.run(st.db, st.cfg, st.sources, st.plex)
            st.subs_progress["last_count"] = n
        except Exception:
            log.exception("Subtitle run crashed")
        st.subs_progress.update(running=False, last_run=time.strftime("%Y-%m-%d %H:%M"))
        st.kick_subs.wait(every)
        st.kick_subs.clear()


def _subdrop_loop(st: State):
    """Subtitle inbox: one folder listing a minute; matching only runs when a file is there."""
    while not st.stop.is_set():
        try:
            st.subdrop.cfg = st.cfg
            st.subdrop.run()
        except Exception:
            log.exception("Subtitle inbox crashed")
        st.kick_drop.wait(60)
        st.kick_drop.clear()


def _films_loop(st: State):
    """Film series you own part of: checked in the background once a week (Wikidata), starting 10 minutes after start."""
    st.stop.wait(600)
    while not st.stop.is_set():
        if st.plex.configured and not st.films.series(max_age=films.WEEK).get("checked"):
            st.films.check_series()
        st.stop.wait(6 * 3600)


def _sorter_loop(st: State):
    while not st.stop.is_set():
        try:
            st.sorter.poll()
        except Exception:
            log.exception("Sorter crashed")
        if time.time() - st.paths_at > 300:  # pick up new Plex libraries / Plex coming online
            st.resolve_paths()
        if st.qbit:
            try:
                qbit.handle_finished(st.db, st.cfg, st.qbit, st.import_torrent)
                st.qbit_error = ""
            except Exception as e:
                st.qbit_error = _friendly(e)
        st.stop.wait(15)


def _plex_library_loop(st: State):
    """Every few hours: which albums Plex has, which movies lack subtitles, and the optional backup."""
    st.stop.wait(120)  # let the dashboard and Plex settle after start-up first
    n = 0
    while not st.stop.is_set():
        if st.plex.configured:
            try:
                releases.PLEX_ALBUMS = st.plex.music_albums()
            except Exception as e:
                log.info("Couldn't read albums from Plex: %s", e)
            if n % 2 == 0:  # walks the library folders: every 6 hours, and only when the PC isn't busy
                try:
                    for _ in range(6):
                        if psutil.cpu_percent(interval=1) < 50:
                            break
                        st.stop.wait(300)
                    st.unseen.run()
                except Exception:
                    log.exception("Check for new files crashed")
            if n % 4 == 0:  # walks every movie: only every 12 hours
                try:
                    st.subscan.run()
                except Exception:
                    log.exception("Subtitle scan crashed")
        n += 1
        try:
            mark = DATA / "safety" / "daily" / "taken.txt"
            if not mark.exists() or time.time() - mark.stat().st_mtime > 20 * 3600:
                safety.snapshot(DATA, "daily")
        except Exception:
            pass
        try:
            b = st.cfg["backup"]
            if b.get("enabled") and b.get("folder"):
                files = backup.existing(b["folder"])
                due = not files or time.time() - files[-1].stat().st_mtime > float(b.get("every_days", 7)) * 86400
                if due:
                    backup.run(b["folder"], int(b.get("keep", 2)))
        except Exception as e:
            log.warning("Backup failed: %s", e)
        st.stop.wait(3 * 3600)


def _downloader_loop(st: State):
    """Keeps the app's own qBittorrent set up and running, and hands it to the app as soon as it answers."""
    while not st.stop.is_set():
        try:
            mode = st.downloader_mode()
            if mode != st.dl_mode:   # settings changed, or the person's own qBittorrent appeared
                st.qbit = st.make_downloader()
                st.qbit_error = st.dl_problem
            elif mode == "managed":
                st.qbm.tick()
                if st.qbit is None and st.qbm.running():
                    st.qbit = st.make_downloader()
                    st.qbit_error = st.dl_problem
                elif st.qbit is None:
                    st.dl_problem = qbmanager.describe()
            elif mode == "external" and st.qbit is None:
                st.qbit = st.make_downloader()
        except Exception:
            log.exception("Downloader check crashed")
        st.stop.wait(4 if qbmanager.STATE["state"] in ("downloading", "unpacking", "starting", "waiting") else 15)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global state
    state = State()
    for fn in (_release_loop, _subs_loop, _subdrop_loop, _films_loop, _sorter_loop, _plex_library_loop, _auto_update_loop):
        threading.Thread(target=fn, args=(state,), daemon=True).start()
    threading.Thread(target=state.monitor.run_forever, args=(state.stop,), daemon=True).start()
    threading.Thread(target=discovery.advertise, args=(int(state.cfg["server"]["port"]),), daemon=True).start()
    threading.Thread(target=state.tailnet.start, args=(state.cfg,), daemon=True).start()
    threading.Thread(target=state.resolve_paths, daemon=True).start()  # ask Plex for folders in the background
    threading.Thread(target=_downloader_loop, args=(state,), daemon=True).start()
    yield
    state.tailnet.stop()
    discovery.stop()
    state.stop.set()
    state.kick_releases.set()
    state.kick_subs.set()
    state.kick_drop.set()


app = FastAPI(title="Server Assistant", lifespan=lifespan)


@app.get("/")
def index():
    # no-cache = always check for a newer page, so an updated server shows its new screens right away (also in the Viewer window)
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


@app.middleware("http")
async def _slow_log(request, call_next):
    t0 = time.time()
    resp = await call_next(request)
    if time.time() - t0 > 2 and request.url.path.startswith("/api/"):
        log.info("slow request: %s took %.1fs", request.url.path, time.time() - t0)
    return resp


@app.get("/api/ping")
def ping():
    """Used by the finder page and the phone app to recognise this server (open to any page on the network)."""
    return Response('{"app":"server-assistant"}', media_type="application/json",
                    headers={"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Private-Network": "true",
                             "Cache-Control": "no-store"})


def _finder_page(request: Request) -> str:
    host = request.url.hostname or ""
    ip = host if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", host) else discovery.lan_ip()
    port = str(request.url.port or state.cfg["server"]["port"])
    html = (STATIC / "find.html").read_text(encoding="utf-8")
    return html.replace("__PORT__", port).replace("__IP__", ip).replace("__HOST__", discovery.computer_name())


@app.get("/open-server-assistant.html")
def finder(request: Request):
    """A page that finds the server by itself. Save it anywhere and open it; it works even if the IP changes."""
    return Response(_finder_page(request), media_type="text/html",
                    headers={"Content-Disposition": 'attachment; filename="Open Server Assistant.html"'})


_APK = {"at": 0.0, "ok": False}


@app.get("/app.apk")
def android_app():
    """Open http://<server>:8765/app.apk on the phone to install the Android app."""
    url = f"https://github.com/{state.cfg['updates']['repo']}/releases/download/{state.cfg['updates']['tag']}/ServerAssistant.apk"
    if time.time() - _APK["at"] > 600:  # is the newest app on GitHub? (checked at most every 10 minutes)
        try:
            _APK["ok"] = requests.head(url, allow_redirects=True, timeout=5).status_code == 200
        except Exception:
            _APK["ok"] = False
        _APK["at"] = time.time()
    if _APK["ok"]:
        return RedirectResponse(url)
    apk = STATIC / "ServerAssistant.apk"
    if not apk.exists():
        raise HTTPException(404, "APK not included")
    return FileResponse(apk, media_type="application/vnd.android.package-archive", filename="ServerAssistant.apk")


@app.get("/manifest.json")
def manifest():
    return FileResponse(STATIC / "manifest.json", media_type="application/manifest+json")


@app.get("/sw.js")
def service_worker():
    return FileResponse(STATIC / "sw.js", media_type="text/javascript")


@app.get("/icon-192.png")
def icon192():
    return FileResponse(STATIC / "icon-192.png")


@app.get("/icon-512.png")
def icon512():
    return FileResponse(STATIC / "icon-512.png")


@app.get("/favicon.ico")
def favicon():
    return FileResponse(STATIC / "favicon.ico")


INSTALLER_PS = r"""
$ErrorActionPreference = "Stop"
$server = "__SERVER__"
Write-Host ""
Write-Host "  Installing Server Assistant for $server" -ForegroundColor Yellow
$browser = $null
foreach ($key in "msedge.exe", "chrome.exe") {
    foreach ($root in "HKLM:", "HKCU:") {
        $p = (Get-ItemProperty "$root\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\$key" -ErrorAction SilentlyContinue)."(default)"
        if ($p -and (Test-Path $p)) { $browser = $p; break }
    }
    if ($browser) { break }
}
if (-not $browser) {
    foreach ($p in "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe", "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe") {
        if (Test-Path $p) { $browser = $p; break }
    }
}
if (-not $browser) { Write-Host "  Microsoft Edge or Google Chrome is needed." -ForegroundColor Red; exit 1 }
$dir = Join-Path $env:LOCALAPPDATA "ServerAssistant"
New-Item -ItemType Directory -Force -Path $dir | Out-Null
$icon = Join-Path $dir "ServerAssistant.ico"
try { Invoke-WebRequest -Uri "$server/favicon.ico" -OutFile $icon -UseBasicParsing } catch { $icon = $browser }
# A small local page finds the server by itself (name, last address, network scan), so the shortcut
# keeps working even if the router gives the server a new IP address.
$finder = Join-Path $dir "find.html"
try { Invoke-WebRequest -Uri "$server/open-server-assistant.html" -OutFile $finder -UseBasicParsing; $target = ([Uri]$finder).AbsoluteUri } catch { $target = $server }
$args_ = "--app=$target --user-data-dir=`"$(Join-Path $dir 'window')`" --window-size=1200,900 --allow-file-access-from-files"
$shell = New-Object -ComObject WScript.Shell
foreach ($lnk in (Join-Path ([Environment]::GetFolderPath("Desktop")) "Server Assistant.lnk"),
                 (Join-Path ([Environment]::GetFolderPath("Programs")) "Server Assistant.lnk")) {
    $s = $shell.CreateShortcut($lnk)
    $s.TargetPath = $browser
    $s.Arguments = $args_
    $s.IconLocation = $icon
    $s.Description = "Server Assistant"
    $s.Save()
}
Write-Host ""
Write-Host "  Done! 'Server Assistant' is now on your desktop and in the Start menu." -ForegroundColor Green
Write-Host "  Tip: while it's open, right-click it in the taskbar and choose 'Pin to taskbar'."
Start-Process $browser $args_
"""


@app.get("/install-pc-app.bat")
def pc_installer(request: Request):
    """A one-click Windows installer that already knows this server's address."""
    server = f"{request.url.scheme}://{request.url.netloc}"
    if not re.fullmatch(r"https?://[A-Za-z0-9.\-\[\]:]+", server):
        raise HTTPException(400, "bad host")
    bat = ("@echo off\r\n"
           "powershell -NoProfile -ExecutionPolicy Bypass -Command \"$s=Get-Content -LiteralPath '%~f0' -Raw; "
           "iex ($s.Substring($s.IndexOf('#PS'+'START')))\"\r\n"
           "pause\r\nexit /b\r\n#PSSTART\r\n" + INSTALLER_PS.replace("__SERVER__", server).replace("\n", "\r\n"))
    return Response(bat, media_type="application/octet-stream",
                    headers={"Content-Disposition": 'attachment; filename="Install Server Assistant.bat"'})


def _scan_library():
    lib = state.cfg["music"]["roots"]
    try:
        info = {"library": lib, "library_found": any(Path(x).is_dir() for x in lib),
                "artists": len(library.list_artists(lib))}
        state.lib_info, state.lib_info_at = info, time.time()
    finally:
        state.lib_scanning = False


def _library_info() -> dict:
    """Never make the page wait for a folder scan: answer with the last result and refresh it in the background."""
    stale = time.time() - state.lib_info_at > 30
    if stale and not getattr(state, "lib_scanning", False):
        state.lib_scanning = True
        t = threading.Thread(target=_scan_library, daemon=True)
        t.start()
        if not state.lib_info:
            t.join(2.5)  # only the very first call waits, and only briefly
    return state.lib_info or {"library": state.cfg["music"]["roots"], "library_found": True, "artists": 0}


def _today() -> str:
    return time.strftime("%Y-%m-%d")


@app.get("/api/status")
def status():
    counts = {r["status"]: r["n"] for r in state.db.query("SELECT status, COUNT(*) n FROM releases GROUP BY status")}
    up = state.db.query("SELECT COUNT(*) n FROM releases WHERE status='new' AND length(date)>=7 AND date > ?", (_today(),))[0]["n"]
    counts["upcoming"] = up
    counts["new"] = max(0, counts.get("new", 0) - up)
    return {
        **_library_info(),
        "unmatched_artists": [r["folder"] for r in state.db.query("SELECT folder FROM artists WHERE mbid='' AND COALESCE(mb_name,'')<>'AMBIGUOUS' AND folder NOT IN (SELECT artist FROM releases)")],
        "ambiguous_artists": [r["folder"] for r in state.db.query("SELECT folder FROM artists WHERE mbid='' AND mb_name='AMBIGUOUS'")],
        "release_counts": counts,
        "release_check": state.release_progress,
        "subtitles": {**state.subs_progress, "enabled": bool(state.cfg["subtitles"].get("enabled") and any(x.get("enabled", True) for x in state.sources)),
                      "sources": [x.get("name") for x in state.sources if x.get("enabled", True)]},
        "download_folders": state.cfg["music"]["download_folders"],
    }


def _is_future(r: dict) -> bool:
    d = r.get("date") or ""
    return len(d) >= 7 and d > _today()


@app.get("/api/releases")
def list_releases(status: str = "new", when: str = ""):
    """Albums that are already out. Announced albums (not released yet) only show under when=upcoming."""
    statuses = status.split(",")
    marks = ",".join("?" * len(statuses))
    rows = state.db.query(f"SELECT * FROM releases WHERE status IN ({marks}) ORDER BY date DESC, artist", statuses)
    if when == "upcoming":
        rows = [r for r in rows if _is_future(r) and r["status"] == "new"]
        rows.sort(key=lambda r: r["date"])
        return rows
    return [r for r in rows if not _is_future(r)]


class StatusBody(BaseModel):
    status: str


@app.post("/api/releases/{mbid}")
def set_release_status(mbid: str, body: StatusBody):
    if body.status not in {"new", "wanted", "skipped", "got", "gap"}:
        raise HTTPException(400, "bad status")
    cur = state.db.execute("UPDATE releases SET status=? WHERE mbid=?", (body.status, mbid))
    if not cur.rowcount:
        raise HTTPException(404, "not found")
    return {"ok": True}


class ArtistBody(BaseModel):
    artist: str


def _owned_for(folder: str) -> set[str]:
    return library.owned_albums(state.cfg["music"]["roots"], folder) | releases.PLEX_ALBUMS.get(library.normalize(folder), set())


@app.get("/api/artist/candidates")
async def artist_candidates(folder: str):
    """Artists that share this folder's name, with what tells them apart, so you can say which one is yours."""
    try:
        return await asyncio.get_running_loop().run_in_executor(None, lambda: releases.artist_candidates(folder, _owned_for(folder)))
    except Exception as e:
        raise HTTPException(502, "MusicBrainz didn't answer: " + str(e)[:120])


class PinBody(BaseModel):
    folder: str
    mbid: str


@app.post("/api/artist/pin")
def artist_pin(body: PinBody):
    """Remember which MusicBrainz artist a folder is. Their old (wrong) listings are cleared and the list is rebuilt."""
    state.db.execute("INSERT OR REPLACE INTO artist_pins (folder, mbid, at) VALUES (?,?,datetime('now'))", (body.folder, body.mbid))
    state.db.execute("INSERT OR REPLACE INTO artists (folder, mbid, mb_name, last_check) VALUES (?,?,?,NULL)", (body.folder, body.mbid, ""))
    # listings you had not acted on are rebuilt from the right artist; what you chose to download, skipped or already own stays.
    # The ones that came from Deezer/iTunes are remembered as "not this artist", so they can't come back after a later check
    # (unless the artist you picked has them on MusicBrainz too).
    for r in state.db.query("SELECT title FROM releases WHERE artist=? AND status IN ('new','gap') AND COALESCE(src,'') <> 'musicbrainz'", (body.folder,)):
        state.db.execute("INSERT OR IGNORE INTO release_blocks (artist, title) VALUES (?,?)", (body.folder, r["title"]))
    state.db.execute("DELETE FROM releases WHERE artist=? AND status IN ('new','gap')", (body.folder,))
    state.kick_releases.set()
    return {"ok": True}


@app.get("/api/skipped-artists")
def skipped_artists():
    return [r["artist"] for r in state.db.query("SELECT artist FROM skipped_artists ORDER BY artist COLLATE NOCASE")]


@app.post("/api/skipped-artists")
def skip_artist(body: ArtistBody):
    """Stop looking for releases from this artist until they are removed from the list."""
    state.db.execute("INSERT OR REPLACE INTO skipped_artists (artist, at) VALUES (?, datetime('now'))", (body.artist,))
    state.db.execute("UPDATE releases SET status='skipped' WHERE artist=? AND status IN ('new','wanted')", (body.artist,))
    return {"ok": True}


@app.delete("/api/skipped-artists/{artist:path}")
def unskip_artist(artist: str):
    state.db.execute("DELETE FROM skipped_artists WHERE artist=?", (artist,))
    state.db.execute("UPDATE releases SET status='new' WHERE artist=? AND status='skipped'", (artist,))
    state.kick_releases.set()  # look for this artist's releases again right away
    return {"ok": True}


@app.get("/api/library/subtitle-gaps")
def subtitle_gaps():
    """Movies Plex says are missing a wanted subtitle language."""
    return state.subscan.gaps(state.cfg["subtitles"]["languages"])


@app.post("/api/library/scan-subtitles")
def scan_subtitles_now():
    threading.Thread(target=state.subscan.run, daemon=True).start()
    return {"ok": True}


_RECENT = {"at": 0.0, "data": {"audio": [], "video": []}}


@app.get("/api/library/recent")
def library_recent():
    """Recently added music and video for the carousels. Cached for 2 minutes so it costs Plex almost nothing."""
    if time.time() - _RECENT["at"] > 120 and state.plex.configured:
        try:
            _RECENT["data"] = state.plex.recent_split(16)
            _RECENT["at"] = time.time()
        except Exception as e:
            log.info("Couldn't read recently added from Plex: %s", e)
            _RECENT["at"] = time.time() - 90  # Plex is down: try again in 30 seconds, not on every page view
    return _RECENT["data"]


@app.get("/api/library/unseen")
def unseen_files():
    """Video and audio files in the library folders that Plex doesn't know yet."""
    f = state.unseen.folders()
    return {"folders": f[:200], "count": sum(len(g["files"]) for g in f), "progress": state.unseen.progress}


@app.post("/api/library/unseen/check")
def unseen_check(days: int = 14):
    threading.Thread(target=state.unseen.run, args=(max(1, min(days, 365)),), daemon=True).start()
    return {"ok": True}


@app.post("/api/library/unseen/send")
async def unseen_send(request: Request):
    """Ask Plex to scan only these folders (or all listed ones), not the whole library."""
    body = await request.json() if request.headers.get("content-length", "0") != "0" else {}
    try:
        n = state.unseen.send(body.get("folders"))
    except Exception as e:
        raise HTTPException(502, f"Plex didn't accept the scan: {e}")
    threading.Timer(45, state.unseen.run).start()  # check again once Plex has had time to read them
    return {"ok": True, "folders": n}


@app.post("/api/library/unseen/ignore")
async def unseen_ignore(request: Request):
    body = await request.json()
    state.unseen.ignore(body.get("folder", ""))
    return {"ok": True}


SUB_EXT = {".srt", ".ass", ".ssa", ".vtt", ".sub"}


@app.post("/api/library/add-subtitle")
async def add_subtitle(key: str = Form(...), lang: str = Form(...), file: UploadFile = File(...)):
    """Attach a subtitle file whose name doesn't match the movie: it is saved next to the movie with the right name
    (<movie file>.<lang>.srt) so Plex picks it up, then Plex is asked to rescan that folder."""
    rows = state.db.query("SELECT * FROM movie_subs WHERE rating_key=?", (key,))
    if not rows or not rows[0]["path"]:
        raise HTTPException(404, "That movie isn't in the list. Rescan and try again.")
    lang = (lang or "").strip().lower()
    if not re.fullmatch(r"[a-z]{2,3}", lang):
        raise HTTPException(400, "Choose a language.")
    ext = Path(file.filename or "").suffix.lower()
    if ext not in SUB_EXT:
        raise HTTPException(400, "Choose a subtitle file (.srt, .ass, .vtt or .sub).")
    data = await file.read()
    if not data or len(data) > 5_000_000:
        raise HTTPException(400, "That file is empty or too big for a subtitle.")
    video = Path(rows[0]["path"])
    if not video.parent.is_dir():
        raise HTTPException(400, f"The movie's folder isn't reachable from this PC: {video.parent}")
    dest = video.with_name(f"{video.stem}.{lang}{ext}")
    dest.write_bytes(data)
    langs = [x for x in (rows[0]["langs"] or "").split(",") if x] + [lang]
    state.db.execute("UPDATE movie_subs SET langs=? WHERE rating_key=?", (",".join(sorted(set(langs))), key))
    scanned = False
    try:
        scanned = bool(state.plex.scan_path(video.parent))
    except Exception as e:
        log.warning("Plex rescan after adding a subtitle failed: %s", e)
    return {"ok": True, "saved": dest.name, "plex_rescan": scanned}


@app.get("/api/subdrop")
def subdrop_status():
    """The subtitle inbox: where it is, what waits for a movie to be picked, and what happened lately."""
    waiting = state.subdrop.waiting()
    movies = [{"key": m["rating_key"], "title": m["title"], "year": m["year"] or ""} for m in state.subdrop.movies()] if waiting else []
    movies.sort(key=lambda m: (m["title"] or "").lower())
    return {"folder": str(state.subdrop.folder()), "waiting": waiting, "movies": movies, "recent": state.subdrop.recent[:15]}


@app.post("/api/subdrop/upload")
async def subdrop_upload(files: list[UploadFile] = File(...)):
    n = 0
    for f in files[:30]:
        data = await f.read()
        if not data or len(data) > 20_000_000:
            continue
        try:
            state.subdrop.add_upload(f.filename, data)
            n += 1
        except ValueError as e:
            raise HTTPException(400, str(e))
    state.kick_drop.set()
    return {"ok": True, "added": n}


class DropAssign(BaseModel):
    file: str
    key: str = ""
    lang: str = ""


@app.post("/api/subdrop/assign")
def subdrop_assign(body: DropAssign):
    lang = (body.lang or "").strip().lower()
    if not re.fullmatch(r"[a-z]{2,3}", lang):
        raise HTTPException(400, "Choose a language.")
    try:
        return {"ok": True, "saved": state.subdrop.assign(body.file, body.key, lang)}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/subdrop/discard")
def subdrop_discard(body: DropAssign):
    state.subdrop.discard(body.file)
    return {"ok": True}


@app.get("/api/films/directors")
def films_directors():
    """Directors of the movies in Plex (most movies first), the films you marked to get, and the series check."""
    try:
        lib = state.films.library()
    except Exception as e:
        raise HTTPException(502, f"Couldn't read your movies from Plex: {_friendly(e)}")
    s = state.films.series(max_age=10 * 365 * 86400)
    marks = state.films.marks()
    for x in s.get("series", []):
        x["missing"] = [dict(f, mark=marks.get(f["id"], "")) for f in x["missing"] if marks.get(f["id"]) != "skip"]
    s["series"] = [x for x in s.get("series", []) if x["missing"]]
    return {"directors": lib["directors"], "movies": len(lib["movies"]), "wanted": state.films.wanted(),
            "series": s.get("series", []), "series_checked": s.get("checked", ""), "series_progress": state.films.series_progress}


@app.get("/api/films/director")
def films_director(name: str, refresh: bool = False):
    try:
        return state.films.director(name, force=refresh)
    except Exception as e:
        raise HTTPException(502, f"Couldn't look up {name}'s films: {_friendly(e)}")


@app.post("/api/films/series/check")
def films_series_check():
    threading.Thread(target=state.films.check_series, daemon=True).start()
    return {"ok": True}


class FilmMark(BaseModel):
    film: str
    mark: str = ""
    title: str = ""
    year: str = ""


@app.post("/api/films/mark")
def films_mark(body: FilmMark):
    if not re.fullmatch(r"Q\d+", body.film):
        raise HTTPException(400, "Unknown film.")
    state.films.mark(body.film, body.mark, body.title[:200], body.year[:4])
    return {"ok": True}


@app.get("/api/summary")
def weekly_summary():
    """What happened in the last 7 days."""
    week = time.time() - 7 * 86400
    iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(week))
    out = {"plays": 0, "viewers": 0, "added": 0, "subtitles": 0, "sorted": 0, "releases": 0}
    if state.plex.configured:
        try:
            hist = [h for h in state.plex.history(300) if (h.get("viewed_at") or 0) > week]
            out["plays"], out["viewers"] = len(hist), len({h.get("user") for h in hist})
            out["added"] = len([r for r in state.plex.recently_added(100) if (r.get("added_at") or 0) > week])
        except Exception:
            pass
    q = lambda sql: state.db.query(sql, (iso,))[0]["n"]
    out["subtitles"] = q("SELECT COUNT(*) n FROM subtitle_log WHERE status='downloaded' AND at > ?")
    out["sorted"] = q("SELECT COUNT(*) n FROM sort_log WHERE ok=1 AND at > ?")
    out["releases"] = q(f"SELECT COUNT(*) n FROM releases WHERE found_at > ? AND status IN ('new','wanted','got') AND (length(date) < 7 OR date <= '{_today()}')")
    drives = state.monitor.snapshot().get("drives", [])
    if drives:
        low = min(drives, key=lambda d: d["free"] / max(d["total"], 1))
        out["tightest_drive"] = {"mount": low["mount"], "free": low["free"], "total": low["total"]}
    return out


@app.get("/api/tailnet")
def tailnet_status():
    return state.tailnet.status(state.cfg)


@app.post("/api/tailnet/install")
def tailnet_install():
    url = state.cfg["tailnet"].get("helper_url", "")
    if not url:
        raise HTTPException(400, "Add the helper address under Settings → Away from home, then save.")
    state.tailnet.stop()   # a running helper locks its own file on Windows, so stop it before replacing it
    try:
        state.tailnet.download(url)
    except Exception as e:
        state.tailnet.start(state.cfg)
        raise HTTPException(500, str(e)[:200])
    state.tailnet.start(state.cfg)
    return state.tailnet.status(state.cfg)


@app.post("/api/tailnet/restart")
def tailnet_restart():
    state.tailnet.start(state.cfg)
    return state.tailnet.status(state.cfg)


@app.get("/api/backup")
def backup_info():
    b = state.cfg["backup"]
    return {**backup.status(b.get("folder", "")), "enabled": bool(b.get("enabled")), "folder": b.get("folder", ""),
            "keep": b.get("keep", 2), "every_days": b.get("every_days", 7), "busy": state.backup_busy}


@app.post("/api/backup/run")
def backup_now():
    b = state.cfg["backup"]
    if not b.get("folder"):
        raise HTTPException(400, "Choose a backup folder first (and save).")
    state.backup_busy = True
    try:
        path = backup.run(b["folder"], int(b.get("keep", 2)))
    except Exception as e:
        raise HTTPException(500, str(e)[:200])
    finally:
        state.backup_busy = False
    return {"ok": True, "file": path.name, "size": path.stat().st_size}


@app.post("/api/check-releases")
def check_now():
    state.kick_releases.set()
    return {"ok": True}


@app.post("/api/check-subtitles")
def subs_now():
    state.kick_subs.set()
    return {"ok": True}


@app.get("/api/sorted")
def sorted_log(limit: int = 50):
    return state.db.query("SELECT * FROM sort_log ORDER BY id DESC LIMIT ?", (limit,))


@app.get("/api/subtitles")
def subtitle_log(limit: int = 80):
    limit = max(1, min(limit, 2000))
    return state.db.query("SELECT * FROM subtitle_log ORDER BY at DESC LIMIT ?", (limit,))


# ---------------- downloads (qBittorrent) ----------------
def _friendly(e: Exception) -> str:
    """Plain words for the usual qBittorrent connection errors."""
    t = str(e)
    if isinstance(e, requests.ConnectionError) or "Max retries exceeded" in t or "Connection refused" in t:
        return "qBittorrent isn't answering. If you use your own, start it and check the address in Settings; otherwise wait a moment."
    return t[:200]


@app.get("/api/downloads")
def downloads():
    base = {"mode": state.dl_mode, "setup": dict(qbmanager.STATE)}
    if not state.qbit:
        if state.dl_mode == "off":
            return {**base, "enabled": False, "error": "", "torrents": []}
        return {**base, "enabled": True, "error": "" if qbmanager.STATE["state"] in ("downloading", "unpacking", "starting", "waiting", "idle")
                else state.dl_problem, "torrents": []}
    try:
        ts = state.qbit.torrents()
    except Exception as e:
        return {**base, "enabled": True, "error": _friendly(e), "torrents": []}
    mine = [t for t in ts if state.qbit.tag in (t.get("tags") or "")]
    return {**base, "enabled": True, "error": state.qbit_error, "torrents": [{
        "hash": t["hash"], "name": t["name"], "progress": t.get("progress", 0) * 100, "state": t.get("state"),
        "size": t.get("size"), "dlspeed": t.get("dlspeed"), "upspeed": t.get("upspeed"), "eta": t.get("eta"),
        "music": t.get("category") == state.qbit.category,
    } for t in mine]}


@app.post("/api/downloads/retry")
def downloads_retry():
    """Try setting up the app's own qBittorrent again right away (it otherwise retries every 10 minutes)."""
    state.qbm.retry()
    return {"ok": True}


@app.post("/api/downloads")
async def add_download(links: str = Form(""), music: bool = Form(True), mbid: str = Form(""),
                       files: list[UploadFile] | None = File(None)):
    if not state.qbit:
        raise HTTPException(400, state.dl_problem or "The downloader is still getting ready. Wait a moment and try again.")
    link_list = [l.strip() for l in links.splitlines() if l.strip()]
    bad = [l for l in link_list if not l.lower().startswith(("magnet:", "http://", "https://"))]
    if bad:
        raise HTTPException(400, "Links must be magnet: links or http(s) links to .torrent files")
    uploads = [(f.filename or "file.torrent", await f.read()) for f in (files or []) if f.filename]
    if not link_list and not uploads:
        raise HTTPException(400, "Paste a link or choose a .torrent file")
    try:  # in a worker thread: fetching a .torrent link can take a while and must not freeze the dashboard
        await asyncio.get_running_loop().run_in_executor(None, lambda: state.qbit.add(link_list, uploads, music=music))
    except Exception as e:
        raise HTTPException(502, _friendly(e))
    if mbid:
        state.db.execute("UPDATE releases SET status='wanted' WHERE mbid=? AND status IN ('new','gap')", (mbid,))
    return {"ok": True}


@app.delete("/api/downloads/{torrent_hash}")
def cancel_download(torrent_hash: str):
    """Cancel a download and delete its partial files."""
    if not state.qbit:
        raise HTTPException(400, "Downloading is switched off")
    state.qbit.cancel(torrent_hash)
    return {"ok": True}


# ---------------- server (system + Plex) ----------------
def _refresh_details():
    st = state
    try:
        st.plex_details = st.plex.details()
        st.plex_details_at = time.time()
    finally:
        st.details_busy = False


@app.get("/api/server")
def server_overview():
    now = time.time()
    if now - state.ov_at > 3:  # many tabs/phones polling share one answer, computed by one request at a time
        if state.ov_lock.acquire(blocking=not state.ov):
            try:
                if time.time() - state.ov_at > 3:
                    state.ov, state.ov_at = state.plex.overview(), time.time()
            finally:
                state.ov_lock.release()
        # else: someone is already asking Plex; answer with the last result instead of piling on
    overview = state.ov or {"configured": state.plex.configured, "online": False}
    # slow Plex data (libraries, history, update check...) refreshes in the background, never in the request
    if overview.get("online") and now - state.plex_details_at > 60 and not state.details_busy:
        state.details_busy = True
        threading.Thread(target=_refresh_details, daemon=True).start()
    details = state.plex_details if overview.get("online") else {}
    return {"system": state.monitor.snapshot(), "plex": {**overview, **details},
            "app": {"version": VERSION, "uptime": time.time() - STARTED, "update": UPDATE_INFO, "auto": bool(state.cfg["updates"].get("auto"))}}


_THUMB_POOL = ThreadPoolExecutor(3, thread_name_prefix="thumb")  # posters must never use up the threads the pages need
_THUMBS: dict = {}


@app.get("/api/plex/thumb")
async def plex_thumb(path: str, w: int = 240):
    if not path.startswith("/library/"):
        raise HTTPException(400, "bad path")
    key = (path, min(w, 600))
    hit = _THUMBS.get(key)
    if not hit:
        try:
            hit = await asyncio.get_running_loop().run_in_executor(_THUMB_POOL, state.plex.thumb, path, min(w, 600))
        except Exception:
            raise HTTPException(404, "no image")
        if len(_THUMBS) > 300:
            _THUMBS.pop(next(iter(_THUMBS)))
        _THUMBS[key] = hit
    return Response(hit[0], media_type=hit[1], headers={"Cache-Control": "max-age=86400"})


@app.post("/api/plex/scan")
def plex_scan(section: str | None = None):
    state.plex.scan(section)
    state.plex_details_at = 0
    return {"ok": True}


class StopBody(BaseModel):
    reason: str = "The server is being restarted."


@app.post("/api/plex/sessions/{session_id}/stop")
def plex_stop(session_id: str, body: StopBody):
    try:
        state.plex.stop_stream(session_id, body.reason)
    except Exception as e:
        raise HTTPException(502, f"Plex refused (stopping streams needs Plex Pass): {str(e)[:120]}")
    return {"ok": True}


# ---------------- settings ----------------
SECRET = "__keep__"  # sent back to the page instead of real passwords


def _folder_mode(setting):
    return "auto" if setting in ("auto", "", None, []) else "custom"


def _as_list(setting):
    if setting in ("auto", "", None):
        return []
    return [setting] if isinstance(setting, str) else list(setting)


@app.get("/api/settings")
def get_settings():
    c = state.cfg
    m, sub, q, p = c["music"], c["subtitles"], c["qbittorrent"], c["plex"]
    return {
        "plex_folders": state.plex_folders,
        "plex_connected": state.plex.configured,
        "music": {"exclude": m.get("exclude", []), "mode": _folder_mode(m["library"]), "custom": _as_list(m["library"]), "in_use": m["roots"],
                  "download_folders": m["download_folders"], "lookback_days": m["lookback_days"],
                  "release_types": m["release_types"], "album_folder_format": m["album_folder_format"],
                  "sources": m["sources"], "catalog_gaps": bool(m.get("catalog_gaps"))},
        "updates": {"auto": bool(c["updates"].get("auto")), "repo": c["updates"].get("repo", "")},
        "tailnet": {k: c["tailnet"].get(k, "") for k in ("enabled", "mode", "name", "authkey", "helper_url")},
        "backup": {"enabled": bool(c["backup"].get("enabled")), "folder": c["backup"].get("folder", ""),
                   "keep": c["backup"].get("keep", 2), "every_days": c["backup"].get("every_days", 7)},
        "movies": {"exclude": sub.get("exclude", []), "mode": _folder_mode(sub["movie_folders"]), "custom": _as_list(sub["movie_folders"]), "in_use": sub["roots"]},
        "subtitles": {"enabled": bool(sub.get("enabled")), "languages": sub["languages"], "drop_folder": sub.get("drop_folder", ""),
                      "drop_default": str(DATA / "Subtitle inbox"),
                      "api_key": SECRET if sub.get("api_key") else "", "username": sub.get("username", ""),
                      "password": SECRET if sub.get("password") else ""},
        "qbittorrent": {"engine": q.get("engine", "builtin"), "enabled": bool(q.get("enabled")), "url": q["url"], "username": q.get("username", ""),
                        "password": SECRET if q.get("password") else "", "save_path": q.get("save_path", ""),
                        "upload_limit_kb": q.get("upload_limit_kb", 1)},
        "plex": {"url": p["url"], "token": SECRET if p.get("token") else "", "auto_token": not p.get("token") and state.plex.configured},
        "system": {"drives": c["system"]["drives"]},
    }


class SettingsBody(BaseModel):
    music: dict
    movies: dict
    subtitles: dict
    qbittorrent: dict
    backup: dict = {}
    tailnet: dict = {}
    updates: dict = {}
    plex: dict
    system: dict


def _clean_list(v) -> list[str]:
    if isinstance(v, str):
        v = v.splitlines()
    return [x.strip().strip('"') for x in (v or []) if str(x).strip()]


@app.post("/api/settings")
def save_settings(body: SettingsBody):
    """Only the settings the page actually sent are changed. A page that is older than the server (for example left
    open during an update) can therefore never reset settings it doesn't know about."""
    c = state.cfg
    m, sub, q, p = c["music"], c["subtitles"], c["qbittorrent"], c["plex"]
    sent = body.model_fields_set

    music_custom = _clean_list(body.music.get("custom"))
    movie_custom = _clean_list(body.movies.get("custom"))
    downloads = _clean_list(body.music.get("download_folders"))
    missing = [f for f in (
        (music_custom if body.music.get("mode") == "custom" else []) +
        (movie_custom if body.movies.get("mode") == "custom" else []) + downloads
    ) if not Path(f).is_dir()]
    if missing:
        raise HTTPException(400, "These folders don't exist on the server: " + ", ".join(missing))
    if body.music.get("mode") == "custom" and not music_custom:
        raise HTTPException(400, "Add at least one music folder, or choose 'From Plex'")

    def secret(new, old):
        return old if new == SECRET else (new or "")

    def has(d: dict, key: str) -> bool:
        return key in d

    def merge_exclude(old: list, body_part: dict) -> list:
        """Only the folders the page actually showed can be (un)ticked. Folders it did not show (Plex was not answering,
        or an older page) keep their earlier choice, so a page with an empty list can never wipe your selection."""
        n = lambda x: str(x).replace("\\", "/").rstrip("/").lower()
        unchecked = _clean_list(body_part.get("exclude"))
        shown = {n(x) for x in _clean_list(body_part.get("shown"))}
        kept = [x for x in old if n(x) not in shown and n(x) not in {n(u) for u in unchecked}]
        return kept + unchecked

    bm = body.music
    if has(bm, "mode"):
        m["library"] = "auto" if bm.get("mode") != "custom" else music_custom
    if has(bm, "download_folders"):
        m["download_folders"] = downloads
    if has(bm, "exclude"):
        m["exclude"] = merge_exclude(m.get("exclude", []), bm)
    if has(body.movies, "exclude"):
        sub["exclude"] = merge_exclude(sub.get("exclude", []), body.movies)
    if has(bm, "lookback_days"):
        m["lookback_days"] = int(bm.get("lookback_days") or 120)
    if has(bm, "release_types"):
        m["release_types"] = [t for t in bm.get("release_types", []) if t in ("Album", "Single", "EP")] or ["Album"]
    if has(bm, "catalog_gaps"):
        m["catalog_gaps"] = bool(bm.get("catalog_gaps"))
    if "updates" in sent and body.updates:
        ud = body.updates
        c["updates"] = {**c["updates"], "auto": bool(ud["auto"]) if "auto" in ud else bool(c["updates"].get("auto")),
                        "repo": (ud.get("repo") or c["updates"]["repo"]).strip()}
    if "tailnet" in sent and body.tailnet:
        tn, old = body.tailnet, c["tailnet"]
        c["tailnet"] = {**old, "enabled": bool(tn["enabled"]) if "enabled" in tn else bool(old.get("enabled")),
                        "mode": ("connect" if tn.get("mode") == "connect" else "serve") if "mode" in tn else old.get("mode", "serve"),
                        "name": (tn.get("name") or old.get("name") or "serverassistant").strip().lower(),
                        "authkey": (tn.get("authkey") or "").strip() if "authkey" in tn else old.get("authkey", ""),
                        "helper_url": (tn.get("helper_url") or "").strip() if "helper_url" in tn else old.get("helper_url", "")}
        threading.Thread(target=state.tailnet.start, args=(c,), daemon=True).start()
    if "backup" in sent and body.backup:
        bk, old = body.backup, c["backup"]
        c["backup"] = {"enabled": bool(bk["enabled"]) if "enabled" in bk else bool(old.get("enabled")),
                       "folder": (bk.get("folder") or "").strip() if "folder" in bk else old.get("folder", ""),
                       "keep": max(1, min(10, int(bk.get("keep") or old.get("keep", 2)))),
                       "every_days": max(1, int(bk.get("every_days") or old.get("every_days", 7)))}
        if c["backup"]["enabled"] and not c["backup"]["folder"]:
            raise HTTPException(400, "Choose a folder for the backups, or turn backups off.")
    if "sources" in bm:
        m["sources"] = [x for x in bm["sources"] if x in ("musicbrainz", "deezer", "itunes")] or ["musicbrainz"]
    if bm.get("album_folder_format"):
        m["album_folder_format"] = bm["album_folder_format"]
    if has(body.movies, "mode"):
        sub["movie_folders"] = "auto" if body.movies.get("mode") != "custom" else movie_custom

    s = body.subtitles
    if has(s, "enabled"):
        sub["enabled"] = bool(s.get("enabled"))
    if has(s, "drop_folder"):
        df = str(s.get("drop_folder") or "").strip().strip('"')
        if df and not Path(df).is_dir():
            try:
                Path(df).mkdir(parents=True, exist_ok=True)
            except OSError:
                raise HTTPException(400, f"The subtitle inbox folder can't be made: {df}")
        sub["drop_folder"] = df
        state.kick_drop.set()
    if has(s, "languages"):
        sub["languages"] = [x.strip().lower() for x in str(s.get("languages", "")).replace(";", ",").split(",") if x.strip()] \
            if isinstance(s.get("languages"), str) else [x.lower() for x in s.get("languages", [])]

    t = body.qbittorrent
    if has(t, "engine"):
        q["engine"] = "qbittorrent" if t.get("engine") == "qbittorrent" else "builtin"
    if has(t, "enabled"):
        q["enabled"] = bool(t.get("enabled"))
    if has(t, "url"):
        q["url"] = (t.get("url") or "http://localhost:8080").rstrip("/")
    if has(t, "username"):
        q["username"] = t.get("username", "")
    if has(t, "password"):
        q["password"] = secret(t.get("password"), q.get("password", ""))
    if has(t, "save_path"):
        q["save_path"] = (t.get("save_path") or "").strip()
    if has(t, "upload_limit_kb"):
        q["upload_limit_kb"] = int(t.get("upload_limit_kb") or 0)

    if has(body.plex, "url"):
        p["url"] = (body.plex.get("url") or "http://localhost:32400").rstrip("/")
    if has(body.plex, "token"):
        p["token"] = secret(body.plex.get("token"), p.get("token", ""))
    if has(body.system, "drives"):
        c["system"]["drives"] = _clean_list(body.system.get("drives"))

    save_config(c)
    old_roots = list(m.get("roots", []))
    state.rebuild_clients()
    state.resolve_paths()
    if m["roots"] != old_roots:
        state.kick_releases.set()
    return {"ok": True, "music_in_use": m["roots"], "movies_in_use": sub["roots"]}


@app.get("/api/browse")
def browse(path: str = ""):
    """Folder picker for the Settings page: drives, then subfolders."""
    import psutil
    if not path:
        drives = []
        for d in psutil.disk_partitions(all=False):
            if "cdrom" not in d.opts and d.fstype:
                drives.append(d.mountpoint)
        return {"path": "", "parent": None, "folders": sorted(set(drives))}
    base = Path(path)
    if not base.is_dir():
        raise HTTPException(404, "Folder not found")
    try:
        folders = sorted((str(x) for x in base.iterdir() if x.is_dir() and not x.name.startswith(("$", "."))), key=str.lower)
    except PermissionError:
        folders = []
    parent = str(base.parent) if base.parent != base else ""
    return {"path": str(base), "parent": parent, "folders": folders[:500]}


# ---------------- stop / restart from the dashboard ----------------
SUPERVISED = os.environ.get("SA_SUPERVISED") == "1"  # started by tray.py


def _exit_later(code: int):
    def go():
        time.sleep(0.8)  # let the HTTP response go out first
        if code == 3 and not SUPERVISED:
            os.execv(sys.executable, [sys.executable] + sys.argv)  # restart in place
        os._exit(code)
    threading.Thread(target=go, daemon=True).start()


# ---------------- updates ----------------
import hashlib
import io
import subprocess
import zipfile

UPDATE_KEEP_TOP = {"config.toml", "config.toml.bak", "subtitle_sources.json", "installed.flag", "server.log", "server.old.log",
                   "unseen_ignore.json", "unseen.json", "plex_folders.json", "torrents.json", "subtitle_inbox.json"}
UPDATE_SKIP_TOP_DIRS = ("safety/", "qbittorrent/", "downloads/", "backups/", "tools/", "Subtitle inbox/")
UPDATE_SKIP_DIRS = (".venv/", "__pycache__/", "android-source/", "desktop-app/", "installer/")


def _file_hash(p: Path) -> str:
    return hashlib.sha1(p.read_bytes()).hexdigest() if p.exists() else ""


@app.get("/api/version")
def version_info():
    return {"version": VERSION, "installed": (ROOT / "installed.flag").exists(), "data_folder": str(DATA),
            "program_folder": str(ROOT), "update": UPDATE_INFO, "auto": bool(state.cfg["updates"].get("auto"))}


def _apply_update(raw: bytes) -> dict:
    """Replace the program files from a ServerAssistant .zip, then restart. Settings and history are kept."""
    try:
        z = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile:
        raise HTTPException(400, "That is not a .zip file.")
    names = [n for n in z.namelist() if not n.endswith("/")]
    runs = sorted((n for n in names if n.endswith("run.py")), key=len)
    if not runs or (runs[0][:-6] + "app/web.py") not in names:
        raise HTTPException(400, "This zip doesn't look like a Server Assistant update.")
    prefix = runs[0][:-6]
    safety.snapshot(DATA, "before-update")   # settings and lists are copied aside first, whatever happens next
    new_version = z.read(prefix + "VERSION").decode().strip() if prefix + "VERSION" in names else "?"
    old_req = _file_hash(ROOT / "requirements.txt") + _file_hash(ROOT / "requirements-optional.txt")
    root = ROOT.resolve()
    written = 0
    for n in names:
        rel = n[len(prefix):]
        if not rel or rel in UPDATE_KEEP_TOP or rel.endswith(".db") or any(rel.startswith(d) or f"/{d}" in rel for d in UPDATE_SKIP_DIRS + UPDATE_SKIP_TOP_DIRS):
            continue
        target = (ROOT / rel).resolve()
        if root not in target.parents:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(z.read(n))
        written += 1
    new_req = _file_hash(ROOT / "requirements.txt") + _file_hash(ROOT / "requirements-optional.txt")
    if new_req != old_req:  # new components needed
        py = Path(sys.executable).with_name("python.exe")
        py = py if py.exists() else Path(sys.executable)
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        r = subprocess.run([str(py), "-m", "pip", "install", "-q", "-r", str(ROOT / "requirements.txt")],
                           capture_output=True, text=True, creationflags=flags)
        if r.returncode:
            log.warning("pip install failed: %s", r.stderr[-500:])
        opt = ROOT / "requirements-optional.txt"
        for pkg in (opt.read_text().split() if opt.exists() else []):  # one by one: a failing extra must not block the rest
            subprocess.run([str(py), "-m", "pip", "install", "-q", pkg], capture_output=True, creationflags=flags)
    log.info("Updated to %s (%d files); restarting", new_version, written)
    _exit_later(3)
    return {"ok": True, "version": new_version, "files": written}




@app.post("/api/update")
async def update_program(file: UploadFile = File(...)):
    return _apply_update(await file.read())


def _vtuple(v: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", v or "")[:4]) or (0,)


def latest_release(cfg: dict) -> dict:
    """What the GitHub release says is the newest version, and where to download it."""
    u = cfg["updates"]
    j = netfetch.fetch_json(f"https://api.github.com/repos/{u['repo']}/releases/tags/{u['tag']}",
                            {"Accept": "application/vnd.github+json"})
    asset = next((a for a in j.get("assets", []) if a["name"] == "ServerAssistant.zip"), None)
    version = (j.get("name") or j.get("tag_name") or "").lstrip("v ")
    return {"version": version, "url": asset["browser_download_url"] if asset else "", "published": j.get("published_at", "")}


def _busy_downloading() -> bool:
    try:
        return any(t.get("progress", 1) < 1 for t in state.qbit.torrents())
    except Exception:
        return False


UPDATE_INFO = {"at": "", "result": "", "error": ""}
STARTED = time.time()


def _note_update(result: str = "", error: str = ""):
    UPDATE_INFO.update(at=time.strftime("%H:%M"), result=result, error=error)


def _install_latest(cfg: dict) -> dict:
    info = latest_release(cfg)
    if not info["url"] or _vtuple(info["version"]) <= _vtuple(VERSION):
        _note_update("up to date")
        return {"updated": False, "latest": info["version"]}
    tmp = DATA / "update.zip.part"
    netfetch.fetch_file(info["url"], tmp)
    raw = tmp.read_bytes()
    tmp.unlink(missing_ok=True)
    _note_update("installing " + info["version"])
    r = _apply_update(raw)
    return {"updated": True, "latest": info["version"], **r}


@app.get("/api/updates/check")
def updates_check():
    try:
        info = latest_release(state.cfg)
    except Exception as e:
        raise HTTPException(502, f"Couldn't reach GitHub: {str(e)[:120]}")
    return {**info, "current": VERSION, "available": _vtuple(info["version"]) > _vtuple(VERSION),
            "auto": bool(state.cfg["updates"].get("auto"))}


@app.post("/api/updates/install")
def updates_install():
    try:
        return _install_latest(state.cfg)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"Update failed: {str(e)[:150]}")


def _auto_update_loop(st: State):
    """When you push a new version to GitHub, the server picks it up by itself."""
    st.stop.wait(90)
    while not st.stop.is_set():
        u = st.cfg["updates"]
        if u.get("auto") and not _busy_downloading():
            try:
                r = _install_latest(st.cfg)
                if r.get("updated"):
                    return  # the program restarts
            except Exception as e:
                _note_update(error=str(e)[:140])
                log.info("Update check failed: %s", e)
        st.stop.wait(180)  # every 3 minutes: one tiny request to GitHub


@app.get("/api/control")
def control_info():
    return {"supervised": SUPERVISED}


@app.post("/api/control/{action}")
def control(action: str):
    if action == "restart":
        _exit_later(3)
    elif action == "stop":
        _exit_later(0)
    else:
        raise HTTPException(400, "unknown action")
    return {"ok": True}


# ---------------- subtitle sources ----------------
SECRET_FIELDS = ("api_key", "password", "headers")


def _mask(src: dict) -> dict:
    return {k: (SECRET if k in SECRET_FIELDS and v else v) for k, v in src.items()}


def _unmask(new: dict) -> dict:
    old = next((x for x in state.sources if x.get("id") == new.get("id")), {})
    return {k: (old.get(k, "") if v == SECRET else v) for k, v in new.items()}


@app.get("/api/subsources")
def get_subsources():
    presets = {k: {"name": v["name"], "note": v.get("note", ""), "type": v["type"],
                   "config": {kk: vv for kk, vv in v.items() if kk != "note"}} for k, v in subsources.PRESETS.items()}
    return {"sources": [_mask(x) for x in state.sources], "presets": presets, "languages": state.cfg["subtitles"]["languages"]}


class SourcesBody(BaseModel):
    sources: list[dict]


@app.post("/api/subsources")
def save_subsources(body: SourcesBody):
    clean = []
    for i, src in enumerate(body.sources):
        src = _unmask(src)
        src.setdefault("id", f"s{int(time.time() * 1000)}{i}")
        if src.get("type") not in subsources.TYPES:
            raise HTTPException(400, f"Unknown source type: {src.get('type')}")
        if src["type"] in ("website", "api") and not (src.get("search_url") or "").strip():
            raise HTTPException(400, f"'{src.get('name') or 'New source'}' needs a search address")
        clean.append(src)
    state.sources = clean
    subsources.save_sources(state.sources_path, clean)
    return {"ok": True}


class TestBody(BaseModel):
    source: dict
    title: str = "Heat"
    year: str = "1995"
    imdb: str = "tt0113277"
    lang: str = "fa"


@app.post("/api/subsources/test")
def test_subsource(body: TestBody):
    """Search one source for a movie and try downloading the best hit (nothing is saved)."""
    import requests as rq
    trace: list[str] = []
    src = subsources.build(_unmask(body.source), rq.Session(), trace)
    movie = subsources.Movie(path=Path(f"{body.title} ({body.year}).mkv"), title=body.title, year=body.year,
                             imdb=body.imdb.strip())
    result = {"candidates": [], "trace": trace}
    try:
        cands = sorted(src.search(movie, body.lang), key=lambda c: -c.score)
    except subsources.Blocked as e:
        result["error"] = str(e)
        return result
    except Exception as e:
        result["error"] = f"Search failed: {str(e)[:200]}"
        return result
    result["candidates"] = [{"name": c.name, "url": c.url, "score": round(c.score, 1)} for c in cands[:10]]
    if not cands:
        result["error"] = "Nothing found for this movie/language."
        return result
    try:
        data = src.download(cands[0])
        got = subsources.extract_subtitle(data, movie)
        if got:
            text = subtitles.to_utf8(got[0], Path(f"x.{body.lang}{got[1]}")).decode("utf-8", "replace").lstrip("\ufeff")
            lines = [l for l in text.splitlines() if l.strip() and "-->" not in l and not l.strip().isdigit()]
            result["ok"] = f"Downloaded a {got[1]} subtitle ({max(1, len(got[0]) // 1024)} KB): {cands[0].name[:80]}"
            result["sample"] = lines[:4]
        else:
            result["error"] = "Found a link, but the download wasn't a subtitle file (could be RAR, or a page that needs a click)."
    except subsources.Blocked as e:
        result["error"] = str(e)
    except Exception as e:
        result["error"] = f"Download failed: {str(e)[:200]}"
    return result
