"""Background jobs + the phone dashboard (FastAPI)."""
import asyncio
import json
import logging
import os
import re
import requests
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from . import backup, netfetch, tailnet, discovery, library, plex, plexlib, qbit, releases, sorter, subsources, subtitles, system, torrent
from .config import DATA, ROOT, VERSION, load_config, save_config
from .db import DB

log = logging.getLogger("web")
STATIC = Path(__file__).parent / "static"


class State:
    def __init__(self):
        self.cfg = load_config()
        self.db = DB(DATA / "server_assistant.db")
        self.plex = plex.Plex(self.cfg)
        self.qbit, self.dl_problem = None, ""
        self.qbit = self.make_downloader()
        self.qbit_error = self.dl_problem
        self.monitor = system.SystemMonitor(self.cfg["system"]["drives"])
        self.plex_details = {}
        self.plex_details_at = 0.0
        self.details_busy = False
        self.subscan = plexlib.SubScan(self.db, self.plex)
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

    def make_downloader(self):
        """The built-in torrent client (default) or qBittorrent, depending on Settings."""
        q = self.cfg["qbittorrent"]
        self.dl_problem = ""
        if q.get("engine", "builtin") == "builtin":
            if isinstance(self.qbit, torrent.BuiltinClient):  # keep the running engine, just apply new settings
                self.qbit.reconfigure(self.cfg)
                return self.qbit
            try:
                return torrent.BuiltinClient(self.cfg, DATA)
            except Exception as e:
                self.dl_problem = str(e)
                return None
        if isinstance(self.qbit, torrent.BuiltinClient):
            self.qbit.close()
        return qbit.QBit(self.cfg) if q.get("enabled") else None

    def rebuild_clients(self):
        self.plex = plex.Plex(self.cfg)
        self.subscan.plex = self.plex
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
                st.qbit_error = str(e)[:200]
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
            if n % 4 == 0:  # walks every movie: only every 12 hours
                try:
                    st.subscan.run()
                except Exception:
                    log.exception("Subtitle scan crashed")
        n += 1
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    global state
    state = State()
    for fn in (_release_loop, _subs_loop, _sorter_loop, _plex_library_loop, _auto_update_loop):
        threading.Thread(target=fn, args=(state,), daemon=True).start()
    threading.Thread(target=state.monitor.run_forever, args=(state.stop,), daemon=True).start()
    threading.Thread(target=discovery.advertise, args=(int(state.cfg["server"]["port"]),), daemon=True).start()
    threading.Thread(target=state.tailnet.start, args=(state.cfg,), daemon=True).start()
    threading.Thread(target=state.resolve_paths, daemon=True).start()  # ask Plex for folders in the background
    yield
    state.tailnet.stop()
    discovery.stop()
    state.stop.set()
    state.kick_releases.set()
    state.kick_subs.set()


app = FastAPI(title="Server Assistant", lifespan=lifespan)


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


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


@app.get("/app.apk")
def android_app():
    """Open http://<server>:8765/app.apk on the phone to install the Android app."""
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
        "unmatched_artists": [r["folder"] for r in state.db.query("SELECT folder FROM artists WHERE mbid='' AND folder NOT IN (SELECT artist FROM releases)")],
        "release_counts": counts,
        "release_check": state.release_progress,
        "subtitles": {**state.subs_progress, "enabled": bool(state.cfg["subtitles"].get("enabled") and any(x.get("enabled", True) for x in state.sources)),
                      "sources": [x.get("name") for x in state.sources if x.get("enabled", True)]},
        "download_folders": state.cfg["music"]["download_folders"],
    }


@app.get("/api/releases")
def list_releases(status: str = "new", when: str = ""):
    """when=upcoming: not released yet. For status=new without `when`, only albums that are already out."""
    statuses = status.split(",")
    marks = ",".join("?" * len(statuses))
    rows = state.db.query(f"SELECT * FROM releases WHERE status IN ({marks}) ORDER BY date DESC, artist", statuses)
    if statuses == ["new"]:
        today = _today()
        future = lambda r: len(r["date"] or "") >= 7 and r["date"] > today
        rows = [r for r in rows if future(r)] if when == "upcoming" else [r for r in rows if not future(r)]
        if when == "upcoming":
            rows.sort(key=lambda r: r["date"])
    return rows


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
    out["releases"] = q("SELECT COUNT(*) n FROM releases WHERE found_at > ?")
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
    try:
        state.tailnet.download(url)
    except Exception as e:
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
    return state.db.query("SELECT * FROM subtitle_log ORDER BY at DESC LIMIT ?", (limit,))


# ---------------- downloads (qBittorrent) ----------------
@app.get("/api/downloads")
def downloads():
    if not state.qbit:
        return {"enabled": bool(state.dl_problem), "error": state.dl_problem, "torrents": []}
    try:
        ts = state.qbit.torrents()
    except Exception as e:
        return {"enabled": True, "error": str(e)[:200], "torrents": []}
    mine = [t for t in ts if state.qbit.tag in (t.get("tags") or "")]
    return {"enabled": True, "error": state.qbit_error, "torrents": [{
        "hash": t["hash"], "name": t["name"], "progress": t.get("progress", 0) * 100, "state": t.get("state"),
        "size": t.get("size"), "dlspeed": t.get("dlspeed"), "upspeed": t.get("upspeed"), "eta": t.get("eta"),
        "music": t.get("category") == state.qbit.category,
    } for t in mine]}


@app.post("/api/downloads")
async def add_download(links: str = Form(""), music: bool = Form(True), mbid: str = Form(""),
                       files: list[UploadFile] | None = File(None)):
    if not state.qbit:
        raise HTTPException(400, state.dl_problem or "Downloading is switched off. Turn it on in Settings.")
    link_list = [l.strip() for l in links.splitlines() if l.strip()]
    bad = [l for l in link_list if not l.lower().startswith(("magnet:", "http://", "https://"))]
    if bad:
        raise HTTPException(400, "Links must be magnet: links or http(s) links to .torrent files")
    uploads = [(f.filename or "file.torrent", await f.read()) for f in (files or []) if f.filename]
    if not link_list and not uploads:
        raise HTTPException(400, "Paste a link or choose a .torrent file")
    try:
        state.qbit.add(link_list, uploads, music=music)
    except Exception as e:
        raise HTTPException(502, str(e)[:200])
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
        "subtitles": {"enabled": bool(sub.get("enabled")), "languages": sub["languages"],
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
    c = state.cfg
    m, sub, q, p = c["music"], c["subtitles"], c["qbittorrent"], c["plex"]

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

    m["library"] = "auto" if body.music.get("mode") != "custom" else music_custom
    m["download_folders"] = downloads
    m["exclude"] = _clean_list(body.music.get("exclude"))
    sub["exclude"] = _clean_list(body.movies.get("exclude"))
    m["lookback_days"] = int(body.music.get("lookback_days") or 120)
    m["release_types"] = [t for t in body.music.get("release_types", []) if t in ("Album", "Single", "EP")] or ["Album"]
    m["catalog_gaps"] = bool(body.music.get("catalog_gaps"))
    ud = body.updates or {}
    c["updates"] = {**c["updates"], "auto": bool(ud.get("auto")), "repo": (ud.get("repo") or c["updates"]["repo"]).strip()}
    tn = body.tailnet or {}
    c["tailnet"] = {"enabled": bool(tn.get("enabled")), "mode": "connect" if tn.get("mode") == "connect" else "serve",
                    "name": (tn.get("name") or "serverassistant").strip().lower(), "authkey": (tn.get("authkey") or "").strip(),
                    "helper_url": (tn.get("helper_url") or "").strip()}
    threading.Thread(target=state.tailnet.start, args=(c,), daemon=True).start()
    bk = body.backup or {}
    c["backup"] = {"enabled": bool(bk.get("enabled")), "folder": (bk.get("folder") or "").strip(),
                   "keep": max(1, min(10, int(bk.get("keep") or 2))), "every_days": max(1, int(bk.get("every_days") or 7))}
    if c["backup"]["enabled"] and not c["backup"]["folder"]:
        raise HTTPException(400, "Choose a folder for the backups, or turn backups off.")
    if "sources" in body.music:
        m["sources"] = [x for x in body.music["sources"] if x in ("musicbrainz", "deezer", "itunes")] or ["musicbrainz"]
    if body.music.get("album_folder_format"):
        m["album_folder_format"] = body.music["album_folder_format"]
    sub["movie_folders"] = "auto" if body.movies.get("mode") != "custom" else movie_custom

    s = body.subtitles
    sub["enabled"] = bool(s.get("enabled"))
    sub["languages"] = [x.strip().lower() for x in str(s.get("languages", "")).replace(";", ",").split(",") if x.strip()] \
        if isinstance(s.get("languages"), str) else [x.lower() for x in s.get("languages", [])]

    t = body.qbittorrent
    q["engine"] = "qbittorrent" if t.get("engine") == "qbittorrent" else "builtin"
    q["enabled"] = bool(t.get("enabled"))
    q["url"] = (t.get("url") or "http://localhost:8080").rstrip("/")
    q["username"] = t.get("username", "")
    q["password"] = secret(t.get("password"), q.get("password", ""))
    q["save_path"] = (t.get("save_path") or "").strip()
    q["upload_limit_kb"] = int(t.get("upload_limit_kb") or 0)

    p["url"] = (body.plex.get("url") or "http://localhost:32400").rstrip("/")
    p["token"] = secret(body.plex.get("token"), p.get("token", ""))
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

UPDATE_KEEP_TOP = {"config.toml", "subtitle_sources.json", "installed.flag", "server.log", "server.old.log"}
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
    new_version = z.read(prefix + "VERSION").decode().strip() if prefix + "VERSION" in names else "?"
    old_req = _file_hash(ROOT / "requirements.txt") + _file_hash(ROOT / "requirements-optional.txt")
    root = ROOT.resolve()
    written = 0
    for n in names:
        rel = n[len(prefix):]
        if not rel or rel in UPDATE_KEEP_TOP or rel.endswith(".db") or any(rel.startswith(d) or f"/{d}" in rel for d in UPDATE_SKIP_DIRS):
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
