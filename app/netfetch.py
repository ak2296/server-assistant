"""Downloads that still work when Python's own certificate check fails (some antivirus or network filters break it).

First Python (using Windows' certificate store when available), then Windows' own curl.exe and PowerShell.
Certificate checks are never switched off.
"""
import json
import os
import subprocess
import sys
import tempfile

import requests


def _windows_fetch(url: str, dest: str, headers: dict | None = None) -> str:
    flags = subprocess.CREATE_NO_WINDOW
    hdr = []
    for k, v in (headers or {}).items():
        hdr += ["-H", f"{k}: {v}"]
    cmds = [["curl.exe", "-L", "--fail", "-sS", *hdr, "-o", dest, url],
            ["powershell", "-NoProfile", "-Command",
             "[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; "
             f"Invoke-WebRequest -UseBasicParsing -Uri '{url}' -OutFile '{dest}'"]]
    err = ""
    for cmd in cmds:
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=300, creationflags=flags)
            if r.returncode == 0 and os.path.exists(dest) and os.path.getsize(dest) > 0:
                return ""
            err = (r.stderr or r.stdout)[-160:]
        except Exception as e:
            err = str(e)[:160]
    return err


def fetch_file(url: str, dest, headers: dict | None = None, timeout: int = 120) -> None:
    """Save a URL to a file. Raises RuntimeError with a readable message."""
    dest = str(dest)
    first = ""
    try:
        with requests.get(url, stream=True, timeout=timeout, headers=headers) as r:
            r.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in r.iter_content(1 << 16):
                    f.write(chunk)
        return
    except Exception as e:
        first = str(e)[:160]
    if sys.platform == "win32":
        err = _windows_fetch(url, dest, headers)
        if not err:
            return
        raise RuntimeError(f"{first} | {err}")
    raise RuntimeError(first)


def fetch_json(url: str, headers: dict | None = None):
    fd, tmp = tempfile.mkstemp()
    os.close(fd)
    try:
        fetch_file(url, tmp, headers, timeout=20)
        with open(tmp, "rb") as f:
            return json.loads(f.read().decode("utf-8"))
    finally:
        os.unlink(tmp)
