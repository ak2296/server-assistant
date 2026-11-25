"""Runs the small satailnet helper (an embedded Tailscale node) so the dashboard works away from home wifi.

Server PC  -> mode "serve":   the helper joins your private network and passes traffic to this dashboard.
Other PCs  -> mode "connect": the helper opens http://127.0.0.1:8766 here and passes it through the network to the server.
It never touches Plex or any other port.
"""
import json
import socket
import logging
import os
import subprocess
import sys
import threading
import urllib.request

from . import config, netfetch

log = logging.getLogger("tailnet")
EXE = "satailnet.exe" if sys.platform == "win32" else "satailnet"


def _free_port(preferred: int) -> int:
    """The preferred port if nothing uses it, otherwise any free one (an old helper may still hold 8766)."""
    for p in (preferred, 0):
        with socket.socket() as sk:
            try:
                sk.bind(("127.0.0.1", p))
                return sk.getsockname()[1]
            except OSError:
                continue
    return preferred


class Tailnet:
    def __init__(self):
        self.proc = None
        self.local = ""
        self.info = {"state": "off"}
        self.lock = threading.Lock()

    @property
    def exe(self):
        return config.DATA / "bin" / EXE

    def installed(self) -> bool:
        return self.exe.is_file()

    def _fetch(self, url: str, dest) -> str:
        try:
            netfetch.fetch_file(url, dest, timeout=60)
            return ""
        except Exception as e:
            return str(e)[:240]

    def download(self, url: str):
        self.exe.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.exe.with_suffix(".part")
        tmp.unlink(missing_ok=True)
        err = self._fetch(url, tmp)
        if err or not tmp.exists() or tmp.stat().st_size < 1_000_000:
            tmp.unlink(missing_ok=True)
            raise RuntimeError("Download failed (" + (err or "file too small") + "). Download satailnet.exe in your browser from the helper address and save it in "
                               + str(self.exe.parent))
        tmp.replace(self.exe)
        if sys.platform != "win32":
            self.exe.chmod(0o755)

    def start(self, cfg: dict):
        t = cfg["tailnet"]
        with self.lock:
            self.stop_locked()
            if not t.get("enabled"):
                self.info = {"state": "off"}
                return
            if not self.installed():
                self.info = {"state": "missing"}
                return
            self._kill_strays()
            state_dir = config.DATA / "tailnet"
            state_dir.mkdir(parents=True, exist_ok=True)
            port = str(cfg["server"]["port"]) if "port" in cfg.get("server", {}) else "8765"
            args = [str(self.exe), "-mode", t["mode"], "-dir", str(state_dir), "-port", port]
            self.local = ""
            if t["mode"] == "serve":
                args += ["-hostname", t["name"], "-local", f"127.0.0.1:{port}"]
            else:
                lp = _free_port(8766)
                self.local = f"http://127.0.0.1:{lp}"
                args += ["-hostname", t["name"] + "-" + (os.environ.get("COMPUTERNAME") or "pc").lower(),
                         "-remote", t["name"], "-listen", f"127.0.0.1:{lp}"]
            env = dict(os.environ)
            if t.get("authkey"):
                env["TS_AUTHKEY"] = t["authkey"]
            flags = 0x08000000 if sys.platform == "win32" else 0  # no console window
            self.info = {"state": "starting"}
            self.proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
                                         text=True, creationflags=flags)
            threading.Thread(target=self._read, args=(self.proc,), daemon=True).start()

    def _read(self, proc):
        for line in proc.stdout:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if isinstance(msg, dict) and msg.get("state"):
                self.info = msg
        if proc is self.proc and self.info.get("state") not in ("error", "off"):
            self.info = {"state": "error", "error": "The helper stopped. Turn it off and on again."}

    def _kill_strays(self):
        """An older helper left running (after an update or crash) would share this folder's node key and show up as
        'Duplicate node key' in Tailscale. Stop any helper started from this exact file before starting a new one."""
        try:
            import psutil
            me = os.path.normcase(str(self.exe))
            for p in psutil.process_iter(["exe"]):
                try:
                    if p.info["exe"] and os.path.normcase(p.info["exe"]) == me and (not self.proc or p.pid != self.proc.pid):
                        p.kill()
                except Exception:
                    pass
        except Exception:
            pass

    def stop_locked(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None

    def stop(self):
        with self.lock:
            self.stop_locked()
            self.info = {"state": "off"}

    def status(self, cfg: dict) -> dict:
        t = cfg["tailnet"]
        return {**self.info, "enabled": bool(t.get("enabled")), "mode": t["mode"], "name": t["name"],
                "installed": self.installed(), "local": self.local, "helper_url": t.get("helper_url", "")}
