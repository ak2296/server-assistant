"""Open Server Assistant in its own window (no browser tabs, no address bar).

Uses pywebview with the Edge WebView2 engine that comes with Windows 10/11. If that isn't available,
falls back to an Edge/Chrome app window, then to the normal browser.
"""
import os
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


def server_url() -> str:
    try:
        from app.config import load_config
        return f"http://localhost:{load_config()['server']['port']}"
    except Exception:
        return "http://localhost:8765"


def wait_for_server(url: str, seconds: int = 30) -> bool:
    for _ in range(seconds * 2):
        try:
            urllib.request.urlopen(url + "/api/ping", timeout=1).read()
            return True
        except Exception:
            time.sleep(0.5)
    return False


def has_webview2() -> bool:
    if sys.platform != "win32":
        return False
    import winreg
    guid = r"{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
    for hive, path in ((winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{guid}"),
                       (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{guid}"),
                       (winreg.HKEY_CURRENT_USER, rf"Software\Microsoft\EdgeUpdate\Clients\{guid}")):
        try:
            with winreg.OpenKey(hive, path) as k:
                if winreg.QueryValueEx(k, "pv")[0]:
                    return True
        except OSError:
            continue
    return False


def browser_app_window(url: str) -> bool:
    """Edge / Chrome in app mode: a plain window with no tabs."""
    candidates = [os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
                  os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
                  os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
                  os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe")]
    for exe in candidates:
        if os.path.exists(exe):
            profile = Path(os.environ.get("LOCALAPPDATA", str(ROOT))) / "ServerAssistant" / "window"
            subprocess.Popen([exe, f"--app={url}", f"--user-data-dir={profile}", "--window-size=1280,860"])
            return True
    return False


def main():
    url = server_url()
    if not wait_for_server(url):
        webbrowser.open(url)  # the server isn't up; the browser shows a clear "can't connect" page
        return
    if has_webview2():
        try:
            import webview
            webview.create_window("Server Assistant", url, width=1280, height=860, min_size=(420, 600),
                                  background_color="#1f2326")
            webview.start(storage_path=str(Path(os.environ.get("LOCALAPPDATA", str(ROOT))) / "ServerAssistant" / "webview"))
            return
        except Exception:
            pass
    if not browser_app_window(url):
        webbrowser.open(url)


if __name__ == "__main__":
    main()
