"""Repair for the built-in downloader: rebuilds the program's Python environment on Python 3.13.

libtorrent (the download engine) exists only for Python 3.9 to 3.13. The app keeps running while the new environment is
built next to the old one (.venv313). Only when it is complete and libtorrent loads in it does the app swap the two and
restart. If anything goes wrong before that, nothing has been changed and the app just keeps running.
Progress is kept in memory (shown on the Downloads page) and written to fix-downloader.log.
"""
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import psutil

from . import netfetch

PY_URL = "https://www.python.org/ftp/python/3.13.1/python-3.13.1-amd64.exe"
STATE = {"running": False, "done": False, "error": "", "lines": []}
_lock = threading.Lock()
NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _log(root: Path, msg: str):
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    STATE["lines"].append(line)
    del STATE["lines"][:-200]
    try:
        with open(root / "fix-downloader.log", "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _run(root: Path, cmd: list[str], timeout: int = 900) -> tuple[int, str]:
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, creationflags=NOWIN, cwd=str(root))
    out = ((r.stdout or "") + (r.stderr or "")).strip()
    if out:
        _log(root, "  " + out.replace("\n", "\n  ")[-600:])
    return r.returncode, out


def find_313(root: Path):
    local = os.environ.get("LOCALAPPDATA", "")
    cands = [[str(Path(local) / "Programs/Python/Python313/python.exe")],
             [str(Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Python313/python.exe")],
             ["py", "-3.13"]]
    for c in cands:
        if len(c) == 1 and not Path(c[0]).exists():
            continue
        try:
            r = subprocess.run(c + ["-c", "import sys;print(sys.version_info[0]*100+sys.version_info[1], sys.maxsize>2**32)"],
                               capture_output=True, text=True, timeout=30, creationflags=NOWIN)
            if r.returncode == 0 and r.stdout.strip().startswith("313 True"):
                return c
        except Exception:
            continue
    return None


def start(root: Path) -> bool:
    with _lock:
        if STATE["running"]:
            return False
        STATE.update(running=True, done=False, error="", lines=[])
    threading.Thread(target=_work, args=(root,), daemon=True).start()
    return True


def _work(root: Path):
    try:
        if sys.platform != "win32":
            raise RuntimeError("This repair is only for the Windows program.")
        (root / "fix-downloader.log").write_text("", encoding="utf-8")
        _log(root, f"Server runs on Python {sys.version.split()[0]} from {sys.prefix}")
        py = find_313(root)
        if py:
            _log(root, f"Found Python 3.13: {' '.join(py)}")
        else:
            installer = Path(os.environ.get("TEMP", str(root))) / "python-3.13.1-amd64.exe"
            _log(root, "Python 3.13 not found. Downloading it (about 27 MB)...")
            netfetch.fetch_file(PY_URL, installer, timeout=300)
            _log(root, f"Downloaded {installer.stat().st_size // 1024 // 1024} MB. Installing for your Windows user only (your own Python is not changed)...")
            code, _ = _run(root, [str(installer), "/quiet", "InstallAllUsers=0", "PrependPath=0", "Include_launcher=0",
                                  "Include_test=0", "Shortcuts=0"], timeout=900)
            if code != 0:
                raise RuntimeError(f"The Python installer stopped with code {code}.")
            installer.unlink(missing_ok=True)
            py = find_313(root)
            if not py:
                raise RuntimeError("Python 3.13 was installed but could not be found afterwards.")
        new = root / ".venv313"
        if new.exists():
            shutil.rmtree(new, ignore_errors=True)
        _log(root, "Creating the new program environment...")
        code, _ = _run(root, py + ["-m", "venv", str(new)])
        vpy = new / "Scripts" / "python.exe"
        if code != 0 or not vpy.exists():
            raise RuntimeError("Could not create the Python 3.13 environment.")
        _log(root, "Installing the required components (a minute or two)...")
        req = str(root / "requirements.txt")
        code, _ = _run(root, [str(vpy), "-m", "pip", "install", "--disable-pip-version-check", "-q", "--use-feature=truststore", "-r", req])
        if code != 0:
            code, _ = _run(root, [str(vpy), "-m", "pip", "install", "--disable-pip-version-check", "-q", "-r", req])
        if code != 0:
            raise RuntimeError("Installing the required components failed (see the lines above).")
        opt = root / "requirements-optional.txt"
        for pkg in (opt.read_text().split() if opt.exists() else []):
            _log(root, f"Installing {pkg}...")
            code, _ = _run(root, [str(vpy), "-m", "pip", "install", "--disable-pip-version-check", "-q", "--use-feature=truststore", pkg])
            if code != 0:
                _run(root, [str(vpy), "-m", "pip", "install", "--disable-pip-version-check", "-q", pkg])
        code, out = _run(root, [str(vpy), "-c", "import libtorrent; print('libtorrent', libtorrent.__version__)"])
        if code != 0:
            raise RuntimeError("libtorrent still can't be loaded in the new environment. Nothing was changed.")
        _log(root, "The new environment works. Switching to it and restarting in a few seconds...")
        _swap(root)
    except Exception as e:  # nothing has been swapped: the app keeps running as before
        STATE["error"] = str(e)[:300]
        _log(root, "ERROR: " + str(e)[:300] + " (nothing was changed; the app keeps running as before)")
    finally:
        STATE["running"] = False


def _swap(root: Path):
    old = root / ".venv-old"
    if old.exists():
        shutil.rmtree(old, ignore_errors=True)
    cmd = root / "swap-env.cmd"
    cmd.write_text("\r\n".join([
        "@echo off", 'cd /d "%~dp0"', "set n=0", ":wait", "timeout /t 2 /nobreak >nul",
        'ren ".venv" ".venv-old" 2>nul', 'if exist ".venv-old" goto swapped',
        "set /a n+=1", "if %n% lss 10 goto wait", "goto begin",
        ":swapped", 'ren ".venv313" ".venv"',
        ":begin", 'if not exist ".venv\\Scripts\\pythonw.exe" ren ".venv-old" ".venv"',
        'start "" ".venv\\Scripts\\pythonw.exe" tray.py --open', ""]), encoding="ascii")
    subprocess.Popen(["cmd", "/c", str(cmd)], cwd=str(root), creationflags=NOWIN | subprocess.CREATE_NEW_PROCESS_GROUP, close_fds=True)
    STATE["done"] = True
    time.sleep(1.5)  # let the page read the final message
    me, prefix = os.getpid(), str(root / ".venv").lower()
    for p in psutil.process_iter(["pid", "exe"]):  # the tray, the app window and this server all run from the old environment
        try:
            if p.info["pid"] != me and p.info["exe"] and p.info["exe"].lower().startswith(prefix):
                p.kill()
        except Exception:
            continue
    os._exit(0)
