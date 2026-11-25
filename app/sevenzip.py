"""7-Zip, fetched once when it is first needed (for .rar and .7z subtitle archives).

On Windows the official 7-Zip MSI is unpacked without installing anything (msiexec /a, no admin rights) into
DATA\\tools\\7zip. Elsewhere a 7z / 7za / 7zz program on the PATH is used.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
from pathlib import Path

from . import netfetch

log = logging.getLogger("sevenzip")

MSI_URL = "https://github.com/ip7z/7zip/releases/download/26.06/7z2606-x64.msi"
WIN = os.name == "nt"
_lock = threading.Lock()


def _flags() -> int:
    return 0x08000000 if WIN else 0   # CREATE_NO_WINDOW


def find(data_dir: Path) -> Path | None:
    tools = Path(data_dir) / "tools" / "7zip"
    hit = next(iter(tools.rglob("7z.exe")), None) if tools.is_dir() else None
    if hit:
        return hit
    for name in ("7z", "7zz", "7za"):
        p = shutil.which(name)
        if p:
            return Path(p)
    return None


def ensure(data_dir: Path) -> Path | None:
    """The 7-Zip program, fetching it first if needed (Windows). None if it can't be had."""
    with _lock:
        hit = find(data_dir)
        if hit or not WIN:
            return hit
        tools = Path(data_dir) / "tools"
        tools.mkdir(parents=True, exist_ok=True)
        msi = tools / "7zip.msi"
        try:
            netfetch.fetch_file(MSI_URL, msi)
            subprocess.run(f'msiexec /a "{msi}" /qn TARGETDIR="{tools / "7zip"}"', capture_output=True, timeout=240,
                           creationflags=_flags())
        except Exception as e:
            log.info("Couldn't get 7-Zip: %s", e)
        finally:
            try:
                msi.unlink()
            except OSError:
                pass
        return find(data_dir)


def extract(exe: Path, archive: Path, dest: Path) -> bool:
    dest.mkdir(parents=True, exist_ok=True)
    try:
        r = subprocess.run([str(exe), "x", "-y", f"-o{dest}", str(archive)], capture_output=True, timeout=120,
                           creationflags=_flags())
        return r.returncode == 0
    except Exception as e:
        log.info("7-Zip failed on %s: %s", archive.name, e)
        return False
