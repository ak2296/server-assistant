"""Built-in torrent downloader (libtorrent): no qBittorrent needed.

Same interface as qbit.QBit, so the rest of the app doesn't care which one is used.
Torrents added here are removed (files kept) as soon as they finish, so nothing is ever seeded.
"""
import json
import logging
import threading
import time
from pathlib import Path

import requests

log = logging.getLogger("torrent")


class BuiltinClient:
    tag = "server-assistant"

    def __init__(self, cfg: dict, data_dir: Path):
        try:
            import libtorrent as lt
        except ImportError as e:
            raise RuntimeError("The built-in downloader needs the 'libtorrent' component. Run Setup again, or "
                               "start.bat, with an internet connection. (Or pick qBittorrent in Settings.)") from e
        self.lt = lt
        q = cfg["qbittorrent"]
        self.category = q.get("category", "server-assistant")
        self.data = Path(data_dir)
        self.state_file = self.data / "torrents.json"
        self.files_dir = self.data / "torrent-files"
        self.files_dir.mkdir(parents=True, exist_ok=True)
        self.music_dir = Path(q.get("save_path") or self.data / "downloads")
        self.other_dir = Path(cfg.get("downloads", {}).get("save_path") or Path.home() / "Downloads")
        self.save_path = str(self.music_dir)
        self.upload_limit = int(q.get("upload_limit_kb", 0)) * 1024
        self.lock = threading.RLock()
        self.handles: dict = {}   # hash -> torrent_handle
        self.meta: dict = {}      # hash -> {"music": bool, "link": str|None, "file": str|None, "save": str}
        settings = {
            "listen_interfaces": "0.0.0.0:6881,[::]:6881",
            "enable_dht": True, "enable_lsd": True, "enable_upnp": True, "enable_natpmp": True,
            "dht_bootstrap_nodes": "router.bittorrent.com:6881,router.utorrent.com:6881,dht.transmissionbt.com:6881",
            "user_agent": "ServerAssistant/1.0",
            "announce_to_all_trackers": True,
        }
        if self.upload_limit:
            settings["upload_rate_limit"] = self.upload_limit
        self.ses = lt.session(settings)
        self._restore()

    def reconfigure(self, cfg: dict):
        """Apply changed settings without restarting the engine."""
        q = cfg["qbittorrent"]
        self.music_dir = Path(q.get("save_path") or self.data / "downloads")
        self.other_dir = Path(cfg.get("downloads", {}).get("save_path") or Path.home() / "Downloads")
        self.save_path = str(self.music_dir)
        self.upload_limit = int(q.get("upload_limit_kb", 0)) * 1024
        try:
            self.ses.apply_settings({"upload_rate_limit": self.upload_limit})
        except Exception:
            pass

    # ---------------------------------------------------------------- persistence
    def _save(self):
        with self.lock:
            tmp = self.state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.meta), encoding="utf-8")
            tmp.replace(self.state_file)

    def _restore(self):
        if not self.state_file.exists():
            return
        try:
            saved = json.loads(self.state_file.read_text(encoding="utf-8"))
        except Exception:
            return
        for _h, m in saved.items():
            try:
                self._start(m)
            except Exception as e:
                log.warning("Could not resume a torrent: %s", e)

    # ---------------------------------------------------------------- adding
    def _params(self, m: dict):
        lt = self.lt
        if m.get("file"):
            params = lt.add_torrent_params()
            params.ti = lt.torrent_info(str(m["file"]))
        else:
            params = lt.parse_magnet_uri(m["link"])
        params.save_path = m["save"]
        return params

    def _start(self, m: dict) -> str:
        Path(m["save"]).mkdir(parents=True, exist_ok=True)
        h = self.ses.add_torrent(self._params(m))
        key = str(h.info_hashes().get_best()) if hasattr(h.info_hashes(), "get_best") else str(h.info_hash())
        if self.upload_limit:
            h.set_upload_limit(self.upload_limit)
        with self.lock:
            self.handles[key] = h
            self.meta[key] = m
        return key

    def add(self, links: list[str] | None = None, files: list[tuple[str, bytes]] | None = None, music: bool = True):
        save = str(self.music_dir if music else self.other_dir)
        todo = []
        for link in links or []:
            if link.lower().startswith("magnet:"):
                todo.append({"music": music, "link": link, "file": None, "save": save})
            else:  # http(s) link to a .torrent file
                r = requests.get(link, timeout=30, headers={"User-Agent": "ServerAssistant/1.0"})
                r.raise_for_status()
                todo.append(self._store_file(r.content, music, save))
        for _name, content in files or []:
            todo.append(self._store_file(content, music, save))
        if not todo:
            raise RuntimeError("Nothing to add")
        for m in todo:
            try:
                self._start(m)
            except Exception as e:
                raise RuntimeError(f"That isn't a valid magnet link or .torrent file ({e})") from e
        self._save()

    def _store_file(self, content: bytes, music: bool, save: str) -> dict:
        info = self.lt.torrent_info(self.lt.bdecode(content))  # raises on a bad file
        path = self.files_dir / f"{info.info_hash()}.torrent"
        path.write_bytes(content)
        return {"music": music, "link": None, "file": str(path), "save": save}

    # ---------------------------------------------------------------- status
    _STATES = {"checking_files": "checkingDL", "downloading_metadata": "metaDL", "downloading": "downloading",
               "finished": "uploading", "seeding": "uploading", "allocating": "allocating",
               "checking_resume_data": "checkingResumeData"}

    def torrents(self) -> list[dict]:
        out = []
        with self.lock:
            items = list(self.handles.items())
        for key, h in items:
            try:
                st = h.status()
            except Exception:
                continue
            m = self.meta.get(key, {})
            size = st.total_wanted if st.has_metadata else 0
            remaining = max(0, size - st.total_wanted_done) if size else 0
            eta = int(remaining / st.download_rate) if st.download_rate > 1000 else 8640000
            state = "error" if st.errc.value() else self._STATES.get(str(st.state).split(".")[-1], str(st.state))
            name = st.name or ("Fetching details…" if not st.has_metadata else key)
            out.append({
                "hash": key, "name": name, "progress": float(st.progress), "size": size, "state": state,
                "dlspeed": st.download_rate, "upspeed": st.upload_rate, "eta": eta,
                "tags": self.tag, "category": self.category if m.get("music") else "",
                "save_path": st.save_path, "content_path": str(Path(st.save_path) / st.name) if st.has_metadata else "",
            })
        out.sort(key=lambda t: t["name"].lower())
        return out

    # ---------------------------------------------------------------- stopping
    def remove(self, torrent_hash: str, delete_files: bool = False):
        """Stop and forget the torrent. Files are kept unless asked otherwise, and nothing keeps seeding."""
        with self.lock:
            h = self.handles.pop(torrent_hash, None)
            m = self.meta.pop(torrent_hash, None)
        if h is not None:
            self.ses.remove_torrent(h, self.lt.session.delete_files if delete_files else 0)
        if m and m.get("file"):
            try:
                Path(m["file"]).unlink()
            except OSError:
                pass
        self._save()

    def cancel(self, torrent_hash: str):
        """Cancel a download and delete its partial files."""
        self.remove(torrent_hash, delete_files=True)

    def version(self) -> str:
        return "libtorrent " + self.lt.__version__

    def close(self):
        try:
            self.ses.pause()
        except Exception:
            pass
