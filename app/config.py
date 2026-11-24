"""Load config.toml (falls back to config.example.toml)."""
import os
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent   # the program files
# Your settings, history and subtitle sources live in DATA. With the Windows installer that is
# %APPDATA%\ServerAssistant, so updating the program never touches them. Unzipped copies keep
# using their own folder, as before.
if (ROOT / "installed.flag").exists() and os.environ.get("APPDATA"):
    DATA = Path(os.environ["APPDATA"]) / "ServerAssistant"
else:
    DATA = Path(os.environ.get("SA_DATA") or ROOT)
DATA.mkdir(parents=True, exist_ok=True)
VERSION = (ROOT / "VERSION").read_text().strip() if (ROOT / "VERSION").exists() else "dev"


def load_config(path: str | Path | None = None) -> dict:
    if path is None:
        path = DATA / "config.toml"
        if not path.exists():
            path = ROOT / "config.example.toml"
    with open(path, "rb") as f:
        cfg = tomllib.load(f)

    music = cfg.setdefault("music", {})
    music.setdefault("library", "auto")   # "auto" = take the music folders from Plex
    music.setdefault("download_folders", [])
    music.setdefault("album_folder_format", "{year} - {album}")
    music.setdefault("lookback_days", 120)
    music.setdefault("release_types", ["Album", "Single", "EP"])
    music.setdefault("skip_secondary_types", ["Compilation", "Live"])
    music.setdefault("check_every_hours", 12)
    music.setdefault("catalog_gaps", False)   # also list older albums you do not own
    music.setdefault("sources", ["musicbrainz", "deezer", "itunes"])  # where to look for new releases
    music.setdefault("settle_seconds", 60)

    subs = cfg.setdefault("subtitles", {})
    subs.setdefault("enabled", False)
    subs.setdefault("movie_folders", "auto")  # "auto" = take the movie folders from Plex
    subs.setdefault("languages", ["en", "fa"])
    subs.setdefault("check_every_hours", 6)
    subs.setdefault("max_downloads_per_run", 15)

    up = cfg.setdefault("updates", {})
    up.setdefault("auto", True)
    up.setdefault("repo", "ak2296/server-assistant")
    up.setdefault("tag", "app-latest")
    up.setdefault("every_minutes", 30)
    t = cfg.setdefault("tailnet", {})
    t.setdefault("enabled", False)
    t.setdefault("mode", "serve")          # serve = this is the server PC; connect = another PC
    t.setdefault("name", "serverassistant")  # the server's name on your private network
    t.setdefault("authkey", "")
    t.setdefault("helper_url", "https://github.com/ak2296/server-assistant/releases/download/helper-latest/satailnet.exe")
    b = cfg.setdefault("backup", {})
    b.setdefault("enabled", False)
    b.setdefault("folder", "")
    b.setdefault("keep", 2)
    b.setdefault("every_days", 7)
    q = cfg.setdefault("qbittorrent", {})
    q.setdefault("engine", "builtin")   # "builtin" = downloader inside the app, "qbittorrent" = use qBittorrent
    q.setdefault("enabled", False)
    q.setdefault("url", "http://localhost:8080")
    q.setdefault("category", "server-assistant")
    q.setdefault("upload_limit_kb", 1)

    plex = cfg.setdefault("plex", {})
    plex.setdefault("url", "http://localhost:32400")
    plex.setdefault("token", "")
    plex.setdefault("scan_after_import", True)

    cfg.setdefault("system", {}).setdefault("drives", [])

    cfg.setdefault("plex", {}).setdefault("url", "http://localhost:32400")
    server = cfg.setdefault("server", {})
    server.setdefault("host", "0.0.0.0")
    server.setdefault("port", 8765)
    return cfg


# ---------------------------------------------------------------- saving

SECTION_ORDER = ["server", "music", "qbittorrent", "plex", "system", "subtitles", "backup", "tailnet", "updates"]
RUNTIME_KEYS = {"roots"}  # filled in while running, never saved


def _toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    s = str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{s}"'


def save_config(cfg: dict, path: Path | None = None) -> None:
    path = path or DATA / "config.toml"
    lines = ["# Server Assistant settings - edited from the Settings page in the app.", ""]
    for section in SECTION_ORDER + [k for k in cfg if k not in SECTION_ORDER]:
        values = cfg.get(section)
        if not isinstance(values, dict):
            continue
        lines.append(f"[{section}]")
        for k, v in values.items():
            if k in RUNTIME_KEYS or v is None:
                continue
            lines.append(f"{k} = {_toml_value(v)}")
        lines.append("")
    tmp = Path(str(path) + ".tmp")
    tmp.write_text("\n".join(lines), encoding="utf-8")
    tmp.replace(path)
