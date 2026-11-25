"""Server Assistant tray app (Windows): runs the server in the background, with a
tray icon to open the dashboard and start / stop / restart it.

start.bat launches this with pythonw.exe, so there's no console window.
The dashboard's Stop/Restart buttons make the server exit with a code this
supervisor understands: 0 = stopped on purpose, 3 = restart me.
"""
import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

import pystray
from PIL import Image

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from app.config import DATA  # noqa: E402  (settings and logs live here)
LOG = DATA / "server.log"
RESTART_CODE = 3
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "ServerAssistant"


def open_window():
    """Open the dashboard in its own app window (appwindow.py); falls back to the browser."""
    pyw = Path(sys.executable).with_name("pythonw.exe")
    exe = pyw if pyw.exists() else Path(sys.executable)
    try:
        subprocess.Popen([str(exe), str(ROOT / "appwindow.py")], cwd=str(ROOT))
    except Exception:
        webbrowser.open(dashboard_url())


def dashboard_url() -> str:
    try:
        from app.config import load_config
        return f"http://localhost:{load_config()['server']['port']}"
    except Exception:
        return "http://localhost:8765"


class Supervisor:
    def __init__(self):
        self.proc: subprocess.Popen | None = None
        self.want_running = True
        self.lock = threading.Lock()
        self.on_change = lambda: None

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self):
        with self.lock:
            self.want_running = True
            if self.running:
                return
            if LOG.exists() and LOG.stat().st_size > 5_000_000:
                LOG.replace(LOG.with_suffix(".old.log"))
            log = open(LOG, "a", encoding="utf-8")
            log.write(f"\n===== started {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
            log.flush()
            flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
            env = {**os.environ, "SA_SUPERVISED": "1", "PYTHONUNBUFFERED": "1"}
            self.proc = subprocess.Popen([sys.executable, str(ROOT / "run.py")], cwd=str(ROOT),
                                         stdout=log, stderr=subprocess.STDOUT, env=env, creationflags=flags)
        self.on_change()

    def stop(self):
        with self.lock:
            self.want_running = False
            p = self.proc
            if p and p.poll() is None:
                p.terminate()
                try:
                    p.wait(10)
                except subprocess.TimeoutExpired:
                    p.kill()
            self.proc = None
        self.on_change()

    def restart(self):
        self.stop()
        self.start()

    def watch(self):
        """Restart after crashes or a dashboard 'Restart'; stay stopped after a dashboard 'Stop'."""
        while True:
            time.sleep(2)
            p = self.proc
            if p is None or p.poll() is None:
                continue
            code = p.returncode
            self.proc = None
            if code == RESTART_CODE:
                self.start()
            elif code == 0:
                self.want_running = False
                self.on_change()
            elif self.want_running:  # crashed
                self.on_change()
                time.sleep(5)
                if self.want_running:
                    self.start()


# ---- start with Windows (HKCU Run key) ----
def _run_command() -> str:
    pyw = Path(sys.executable).with_name("pythonw.exe")
    exe = pyw if pyw.exists() else Path(sys.executable)
    return f'"{exe}" "{ROOT / "tray.py"}"'


def autostart_enabled() -> bool:
    if sys.platform != "win32":
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, RUN_NAME)
            return True
    except OSError:
        return False


def set_autostart(on: bool):
    if sys.platform != "win32":
        return
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, RUN_NAME, 0, winreg.REG_SZ, _run_command())
        else:
            try:
                winreg.DeleteValue(k, RUN_NAME)
            except OSError:
                pass


def single_instance() -> socket.socket | None:
    """Only one tray app at a time: hold a local port as a lock."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 48765))
        s.listen(1)
        return s
    except OSError:
        return None


def stop_downloader():
    """Quitting from the tray also stops the app's own qBittorrent (an app update or restart leaves it running)."""
    try:
        from app.qbmanager import Manager
        Manager(DATA).stop()
    except Exception:
        pass


def main():
    lock = single_instance()
    if lock is None:  # already running -> just open the window
        open_window()
        return

    sup = Supervisor()
    image = Image.open(ROOT / "app" / "static" / "icon-192.png")

    def status_text(_item=None):
        return "Server Assistant: running" if sup.running else "Server Assistant: stopped"

    menu = pystray.Menu(
        pystray.MenuItem(status_text, None, enabled=False),
        pystray.MenuItem("Open dashboard", lambda: open_window(), default=True),
        pystray.MenuItem("Open in browser", lambda: webbrowser.open(dashboard_url())),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Start", lambda: sup.start(), enabled=lambda _i: not sup.running),
        pystray.MenuItem("Stop", lambda: sup.stop(), enabled=lambda _i: sup.running),
        pystray.MenuItem("Restart", lambda: sup.restart()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Start with Windows", lambda: set_autostart(not autostart_enabled()),
                         checked=lambda _i: autostart_enabled()),
        pystray.MenuItem("Open log", lambda: os.startfile(LOG) if LOG.exists() and hasattr(os, "startfile") else None),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit (stop everything)", lambda: (sup.stop(), stop_downloader(), icon.stop())),
    )
    icon = pystray.Icon("ServerAssistant", image, "Server Assistant", menu)

    def changed():
        icon.title = status_text()
        try:
            icon.update_menu()
        except Exception:
            pass

    sup.on_change = changed
    sup.start()
    if "--open" in sys.argv:  # started from a shortcut: show the window right away
        threading.Thread(target=open_window, daemon=True).start()
    threading.Thread(target=sup.watch, daemon=True).start()
    icon.run()


if __name__ == "__main__":
    main()
