"""qBittorrent Web API client: add magnets/.torrent files and watch progress."""
import logging
import time
from pathlib import Path

import requests

from .db import DB

log = logging.getLogger("qbit")


class QBit:
    def __init__(self, cfg: dict):
        q = cfg["qbittorrent"]
        self.url = q["url"].rstrip("/")
        self.username, self.password = q.get("username", ""), q.get("password", "")
        self.category = q["category"]
        self.save_path = q.get("save_path", "")
        self.upload_limit = int(q.get("upload_limit_kb", 0)) * 1024
        self.tag = "server-assistant"
        self.s = requests.Session()
        self.s.headers["Referer"] = self.url  # qBittorrent's CSRF check
        self.logged_in = False

    def _login(self):
        r = self.s.post(f"{self.url}/api/v2/auth/login",
                        data={"username": self.username, "password": self.password}, timeout=10)
        if r.status_code != 200 or r.text.strip() == "Fails.":
            raise RuntimeError("qBittorrent refused the login. Check the username and password in Settings > Downloads.")
        self.logged_in = True

    def _call(self, method, path, **kw):
        # try without logging in first (qBittorrent can skip the login for this PC); log in only when it asks
        r = self.s.request(method, f"{self.url}/api/v2/{path}", timeout=30, **kw)
        if r.status_code == 403:
            self._login()
            r = self.s.request(method, f"{self.url}/api/v2/{path}", timeout=30, **kw)
        r.raise_for_status()
        return r

    def _ensure_category(self):
        data = {"category": self.category}
        if self.save_path:
            data["savePath"] = self.save_path
        try:
            self._call("POST", "torrents/createCategory", data=data)
        except requests.HTTPError:
            pass  # already exists

    def add(self, links: list[str] | None = None, files: list[tuple[str, bytes]] | None = None,
            music: bool = True):
        """music=True: saved in our category and auto-sorted when done.
        music=False: plain download to qBittorrent's own default folder."""
        data = {"tags": self.tag}
        if self.upload_limit:
            data["upLimit"] = str(self.upload_limit)
        if music:
            self._ensure_category()
            data["category"] = self.category
            if self.save_path:
                data["savepath"] = self.save_path
        if links:
            data["urls"] = "\n".join(links)
        upload = [("torrents", (name, content, "application/x-bittorrent")) for name, content in (files or [])]
        r = self._call("POST", "torrents/add", data=data, files=upload or None)
        if r.text.strip() == "Fails.":
            raise RuntimeError("qBittorrent refused the torrent (bad link or file, or already added)")

    def torrents(self) -> list[dict]:
        r = self._call("GET", "torrents/info", params={"sort": "added_on", "reverse": "true"})
        return r.json()

    def remove(self, torrent_hash: str):
        """Remove from qBittorrent but keep the downloaded files - this stops seeding."""
        self._call("POST", "torrents/delete", data={"hashes": torrent_hash, "deleteFiles": "false"})

    def cancel(self, torrent_hash: str):
        """Cancel a download and delete its partial files."""
        self._call("POST", "torrents/delete", data={"hashes": torrent_hash, "deleteFiles": "true"})

    def version(self) -> str:
        return self._call("GET", "app/version").text


def handle_finished(db: DB, cfg: dict, client: QBit, import_fn) -> None:
    """For every finished torrent added through this app: stop it (no seeding),
    then, if it was added as music, move it into the library."""
    for t in client.torrents():
        tags = [x.strip() for x in (t.get("tags") or "").split(",")]
        if client.tag not in tags or t.get("progress", 0) < 1:
            continue
        client.remove(t["hash"])
        db.execute("INSERT OR IGNORE INTO imported (hash, name) VALUES (?,?)", (t["hash"], t["name"]))
        if t.get("category") != client.category:
            log.info("Finished (not music): %s", t["name"])
            continue
        path = Path(t.get("content_path") or Path(t["save_path"]) / t["name"])
        time.sleep(2)  # let qBittorrent release the files
        if not path.exists():
            db.log_sort(path, None, False, f"Finished torrent '{t['name']}' not found on disk (is qBittorrent on this PC?)")
            continue
        if not import_fn(path):
            db.log_sort(path, None, False, f"'{t['name']}' finished but has no audio files - left in the download folder.")
