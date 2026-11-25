"""The app's own qBittorrent: downloaded once, set up and started by the app, no Windows install needed.

Everything lives in <data folder>\\qbittorrent\\ :
    app\\      the program files (unpacked from the official installer, nothing is installed into Windows)
    profile\\  qBittorrent's settings and its list of torrents (kept across app updates)
    managed.json  the port and the password this app uses to talk to it

qBittorrent runs hidden next to the server. If it is already running (for example after an app update) the app
simply keeps using it, so downloads are not interrupted. An existing qBittorrent install is reused when present.
"""
import base64
import hashlib
import json
import logging
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import psutil
import requests

from . import netfetch

log = logging.getLogger("qbmanager")

QB_VERSION = "5.3.1"
QB_SHA256 = "836d0f415fdf195cb861a3bc88ac603d2bf4480f78a334d512ad0e458e7c8db5"   # from qbittorrent.org/download
QB_URLS = [
    f"https://downloads.sourceforge.net/project/qbittorrent/qbittorrent-win32/qbittorrent-{QB_VERSION}/qbittorrent_{QB_VERSION}_x64_setup.exe",
    f"https://sourceforge.net/projects/qbittorrent/files/qbittorrent-win32/qbittorrent-{QB_VERSION}/qbittorrent_{QB_VERSION}_x64_setup.exe/download",
]
SEVENZIP_MSI = "https://github.com/ip7z/7zip/releases/download/26.06/7z2606-x64.msi"   # only used to unpack the installer
WIN = sys.platform == "win32"

# What the Downloads page shows: idle | downloading | unpacking | waiting | starting | running | failed
STATE = {"state": "idle", "detail": "", "progress": 0.0, "port": 0}


def _set(state: str, detail: str = "", progress: float = 0.0):
    STATE.update(state=state, detail=detail, progress=progress)


def describe() -> str:
    """One readable line about the downloader, for when it is not running yet."""
    s = STATE
    if s["state"] == "failed":
        return s["detail"]
    return s["detail"] or "The downloader is starting…"


def _hash_password(pw: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha512", pw.encode(), salt, 100000, 64)
    b = lambda x: base64.b64encode(x).decode()
    return f'"@ByteArray({b(salt)}:{b(dk)})"'


def _flags() -> int:
    return subprocess.CREATE_NO_WINDOW if WIN else 0


class Manager:
    def __init__(self, data_dir):
        self.base = Path(data_dir) / "qbittorrent"
        self.app = self.base / "app"
        self.profile = self.base / "profile"
        self.meta_file = self.base / "managed.json"
        self.next_try = 0.0
        self.force_elevated = False
        self.lock = threading.Lock()
        self.meta = self._load_meta()
        STATE["port"] = self.meta["port"]

    # ---------------------------------------------------------------- settings of the managed copy
    def _load_meta(self) -> dict:
        try:
            m = json.loads(self.meta_file.read_text(encoding="utf-8"))
            if m.get("port") and m.get("password"):
                return m
        except Exception:
            pass
        m = {"port": self._free_port(), "password": secrets.token_urlsafe(12)}
        self.base.mkdir(parents=True, exist_ok=True)
        self.meta_file.write_text(json.dumps(m), encoding="utf-8")
        return m

    def _save_meta(self):
        try:
            self.meta_file.write_text(json.dumps(self.meta), encoding="utf-8")
        except OSError:
            pass

    def retry(self):
        """The person pressed Try again: this one time the Windows permission prompt may appear."""
        self.force_elevated = True
        self.next_try = 0.0

    @staticmethod
    def _free_port() -> int:
        import socket
        for p in range(18080, 18100):
            with socket.socket() as s:
                if s.connect_ex(("127.0.0.1", p)) != 0:
                    return p
        return 18080

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.meta['port']}"

    def client_cfg(self, cfg: dict) -> dict:
        """The app's settings, pointed at the managed qBittorrent."""
        q = {**cfg["qbittorrent"], "url": self.url, "username": "admin", "password": self.meta["password"]}
        return {**cfg, "qbittorrent": q}

    # ---------------------------------------------------------------- is it there / is it running
    def find_exe(self) -> Path | None:
        names = ["qbittorrent.exe"] if WIN else ["qbittorrent-nox"]
        for n in names:
            for p in (self.app / n, *self.app.rglob(n)):
                if p.exists():
                    return p
        if WIN:
            for var in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
                root = os.environ.get(var)
                if root:
                    for sub in ("qBittorrent", r"Programs\qBittorrent"):
                        p = Path(root) / sub / "qbittorrent.exe"
                        if p.exists():
                            return p
        else:
            w = shutil.which("qbittorrent-nox")
            return Path(w) if w else None
        return None

    @staticmethod
    def answers(url: str, timeout: float = 3) -> bool:
        """True when a qBittorrent Web UI answers at this address (even if it asks for a login)."""
        try:
            r = requests.get(url.rstrip("/") + "/api/v2/app/version", timeout=timeout, headers={"Referer": url})
            return r.status_code in (200, 401, 403)
        except Exception:
            return False

    def running(self) -> bool:
        return self.answers(self.url)

    def note_external(self):
        """The person's own qBittorrent answered once: from now on it is never replaced by the app's own."""
        if not self.meta.get("external_seen"):
            self.meta["external_seen"] = True
            self._save_meta()

    # ---------------------------------------------------------------- the settings file
    def _ini_path(self) -> Path:
        return self.profile / "qBittorrent" / "config" / "qBittorrent.ini"

    def _write_ini(self):
        """First run: write the settings qBittorrent needs. Later runs: only make sure the few keys we depend on are right."""
        want = {
            r"WebUI\Enabled": "true", r"WebUI\Port": str(self.meta["port"]), r"WebUI\Address": "127.0.0.1",
            r"WebUI\LocalHostAuth": "false", r"WebUI\Username": "admin",
            r"Win32\NeverCheckFileAssocation": "true",   # no "make qBittorrent the default for .torrent?" question
            r"General\StartMinimized": "true", r"GUI\StartUpWindowState": "Hidden",
        }
        ini = self._ini_path()
        ini.parent.mkdir(parents=True, exist_ok=True)
        if not ini.exists():
            lines = ["[LegalNotice]", "Accepted=true", "", "[Preferences]"]
            lines += [f"{k}={v}" for k, v in want.items()]
            lines.append(f"WebUI\\Password_PBKDF2={_hash_password(self.meta['password'])}")
            lines.append("")
            ini.write_text("\n".join(lines), encoding="utf-8")
            return
        text = ini.read_text(encoding="utf-8", errors="replace").splitlines()
        seen = set()
        out = []
        for ln in text:
            key = ln.split("=", 1)[0]
            if key in want:
                out.append(f"{key}={want[key]}")
                seen.add(key)
            else:
                out.append(ln)
        missing = [f"{k}={v}" for k, v in want.items() if k not in seen]
        if missing:
            if "[Preferences]" in out:
                i = out.index("[Preferences]") + 1
                out[i:i] = missing
            else:
                out += ["", "[Preferences]", *missing]
        if "[LegalNotice]" not in out:
            out = ["[LegalNotice]", "Accepted=true", ""] + out
        ini.write_text("\n".join(out) + "\n", encoding="utf-8")

    # ---------------------------------------------------------------- download + unpack
    def _download(self, urls: list[str], dest: Path, label: str):
        last = ""
        for url in urls:
            try:
                with requests.get(url, stream=True, timeout=60, headers={"User-Agent": "curl/8.4.0"}) as r:
                    r.raise_for_status()
                    total = int(r.headers.get("Content-Length") or 0)
                    got = 0
                    with open(dest, "wb") as f:
                        for chunk in r.iter_content(1 << 17):
                            f.write(chunk)
                            got += len(chunk)
                            if total:
                                _set("downloading", f"Downloading {label} ({got // 1048576} of {total // 1048576} MB)", got / total)
                if dest.stat().st_size > 100_000:
                    return
                last = "the download was empty"
            except Exception as e:
                last = str(e)[:160]
        try:  # Windows' own curl / PowerShell, for networks that break Python's certificate check
            netfetch.fetch_file(urls[0], dest)
            if dest.exists() and dest.stat().st_size > 100_000:
                return
        except Exception as e:
            last = (last + " | " + str(e))[:300]
        raise RuntimeError(f"Could not download {label}: {last}")

    @staticmethod
    def _sha256(p: Path) -> str:
        h = hashlib.sha256()
        with open(p, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()

    def _unpack(self, setup: Path, tmp: Path) -> bool:
        """Unpack the installer into app\\ without installing anything (no admin rights needed)."""
        _set("unpacking", "Unpacking qBittorrent…", 0.0)
        msi, seven = tmp / "7zip.msi", tmp / "7zip"
        self._download([SEVENZIP_MSI], msi, "7-Zip (to unpack the installer)")
        subprocess.run(f'msiexec /a "{msi}" /qn TARGETDIR="{seven}"', capture_output=True, timeout=240, creationflags=_flags())
        sz = next(iter(seven.rglob("7z.exe")), None)
        if not sz:
            return False
        shutil.rmtree(self.app, ignore_errors=True)
        self.app.mkdir(parents=True, exist_ok=True)
        r = subprocess.run([str(sz), "x", "-y", f"-o{self.app}", str(setup)], capture_output=True, timeout=300, creationflags=_flags())
        if next(iter(self.app.rglob("qbittorrent.exe")), None):
            return True
        log.warning("7-Zip could not unpack the installer: %s", (r.stdout or b"")[-300:])
        return False

    def _run_installer(self, setup: Path):
        """Last resort: the real installer, silently, into app\\. Windows asks for permission once (the admin prompt)."""
        _set("waiting", "Windows is asking for permission to set up qBittorrent. On the server PC, click Yes.", 0.0)
        shutil.rmtree(self.app, ignore_errors=True)
        q = lambda x: str(x).replace("'", "''")
        ps = (f"$p = Start-Process -FilePath '{q(setup)}' -ArgumentList '/S','/D={q(self.app)}' -Verb RunAs -Wait -PassThru; "
              "exit $p.ExitCode")
        try:
            subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, timeout=300, creationflags=_flags())
        except subprocess.TimeoutExpired:
            pass   # nobody clicked the prompt; the check below reports it

    def install(self):
        if not WIN:
            raise RuntimeError("The app's own qBittorrent is only set up on Windows. Install qbittorrent-nox, or use your own qBittorrent in Settings.")
        tmp = self.base / "tmp"
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True, exist_ok=True)
        try:
            setup = tmp / "qbittorrent_setup.exe"
            self._download(QB_URLS, setup, f"qBittorrent {QB_VERSION}")
            if self._sha256(setup).lower() != QB_SHA256:
                raise RuntimeError("The qBittorrent download did not match its published checksum, so it was not used.")
            ok = False
            try:
                ok = self._unpack(setup, tmp)
            except Exception as e:
                log.warning("Unpacking without admin failed: %s", e)
            if not ok:
                if self.meta.get("elevated_tried") and not self.force_elevated:
                    raise RuntimeError("qBittorrent could not be unpacked without admin rights, and the Windows permission prompt was already shown once. "
                                       "Press Try again and click Yes on the server PC, or install qBittorrent from qbittorrent.org (the app finds it by itself).")
                self.meta["elevated_tried"] = True
                self._save_meta()
                self.force_elevated = False
                self._run_installer(setup)
            if not next(iter(self.app.rglob("qbittorrent.exe")), None):
                raise RuntimeError("qBittorrent could not be set up automatically. Install it from qbittorrent.org (the app finds it by itself), "
                                   "or approve the Windows permission prompt next time.")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    # ---------------------------------------------------------------- run
    def start(self, exe: Path):
        _set("starting", "Starting qBittorrent…", 0.0)
        self._write_ini()
        args = [str(exe), f"--profile={self.profile}", f"--webui-port={self.meta['port']}"]
        if WIN:
            args.append("--no-splash")
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            si.wShowWindow = 0  # hidden: no window pops up on the server PC
            kw = {"startupinfo": si, "creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
        else:
            kw = {"start_new_session": True}
        subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=str(exe.parent), **kw)
        for _ in range(60):
            if self.running():
                return
            time.sleep(1)
        raise RuntimeError("qBittorrent started but its Web UI did not answer on port %d." % self.meta["port"])

    def stop(self):
        """Stop the managed qBittorrent (used when you quit the app from the tray). It is asked to save first."""
        try:
            requests.post(self.url + "/api/v2/app/shutdown", timeout=5, headers={"Referer": self.url})
        except Exception:
            pass
        procs = []
        for p in psutil.process_iter(["name", "cmdline"]):
            try:
                cmd = " ".join(p.info.get("cmdline") or [])
                if "qbittorrent" in (p.info.get("name") or "").lower() and str(self.profile) in cmd:
                    procs.append(p)
            except psutil.Error:
                pass
        _, alive = psutil.wait_procs(procs, timeout=15)
        for p in alive:
            try:
                p.terminate()
            except psutil.Error:
                pass

    def tick(self):
        """Called every few seconds: set up, start or restart the managed qBittorrent as needed."""
        with self.lock:
            if self.running():
                _set("running", "")
                return
            if time.time() < self.next_try:
                return
            try:
                exe = self.find_exe()
                if not exe:
                    self.install()
                    exe = self.find_exe()
                if not exe:
                    raise RuntimeError("qBittorrent is not available.")
                self.start(exe)
                _set("running", "")
                log.info("qBittorrent is running on port %d", self.meta["port"])
            except Exception as e:
                msg = str(e)[:400]
                log.warning("Managed qBittorrent failed: %s", msg)
                _set("failed", msg)
                self.next_try = time.time() + 600   # try again in 10 minutes
