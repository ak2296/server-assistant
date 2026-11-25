"""Finds video and audio files that sit in the Plex library folders but that Plex doesn't know yet,
so Plex can be asked to look at just those folders instead of scanning whole libraries."""
import json
import logging
import os
import threading
import time
from pathlib import Path

log = logging.getLogger("unseen")

VIDEO = {".mkv", ".mp4", ".avi", ".mov", ".m4v", ".wmv", ".ts", ".mpg", ".mpeg", ".webm"}
AUDIO = {".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".wma", ".alac", ".ape"}
SKIP_DIRS = {"extras", "featurettes", "behind the scenes", "trailers", "sample", "samples", "deleted scenes", "scenes", "shorts", "interviews", "@eadir", "$recycle.bin"}
LIBTYPE = {"movie": VIDEO, "show": VIDEO, "artist": AUDIO}


def norm(p: str) -> str:
    return str(p).replace("\\", "/").rstrip("/").lower()


class Unseen:
    def __init__(self, plex, data_dir: Path):
        self.plex = plex
        self.ignore_file = Path(data_dir) / "unseen_ignore.json"
        self.result_file = Path(data_dir) / "unseen.json"   # last result, so the home page has it right after a restart
        self.items: list[dict] = []
        self.progress = {"running": False, "last_run": "", "error": "", "days": 14}
        self.stop = threading.Event()
        try:
            saved = json.loads(self.result_file.read_text(encoding="utf-8"))
            self.items, self.progress["last_run"] = saved.get("items", []), saved.get("last_run", "")
            self.items = [i for i in self.items if os.path.exists(i["path"])]   # files moved or deleted since
        except Exception:
            pass

    def _save(self):
        try:
            self.result_file.write_text(json.dumps({"items": self.items, "last_run": self.progress["last_run"]}), encoding="utf-8")
        except OSError:
            pass

    def ignored(self) -> set[str]:
        try:
            return set(json.loads(self.ignore_file.read_text()))
        except Exception:
            return set()

    def ignore(self, folder: str):
        s = self.ignored() | {norm(folder)}
        self.ignore_file.write_text(json.dumps(sorted(s)))
        self.items = [i for i in self.items if norm(i["folder"]) != norm(folder)]
        self._save()

    def _sections(self) -> list[dict]:
        out = []
        for d in self.plex.get("/library/sections").get("Directory", []):
            if d.get("type") in LIBTYPE and not self.plex._left_out(d):
                out.append({"key": d["key"], "type": d["type"], "title": d.get("title", ""),
                            "roots": [l["path"] for l in d.get("Location", []) if l.get("path")]})
        return out

    def _candidates(self, sec: dict, since: float) -> list[dict]:
        exts, found = LIBTYPE[sec["type"]], []
        for root in sec["roots"]:
            stack = [root]
            while stack and not self.stop.is_set():
                d = stack.pop()
                try:
                    with os.scandir(d) as it:
                        entries = list(it)
                except OSError:
                    continue
                for e in entries:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            if e.name.lower() not in SKIP_DIRS:
                                stack.append(e.path)
                        elif os.path.splitext(e.name)[1].lower() in exts:
                            st = e.stat()
                            low = e.name.lower()
                            if st.st_mtime >= since and "sample" not in low and "-trailer" not in low:
                                found.append({"path": e.path, "folder": d, "name": e.name, "mtime": st.st_mtime,
                                              "kind": "audio" if sec["type"] == "artist" else "video", "section": sec["key"]})
                    except OSError:
                        continue
                time.sleep(0.001)  # walks the whole library: stay gentle
        return found

    def _known(self, sec: dict) -> set[str]:
        """Every file Plex has in this library (paged, so a big music library doesn't time out)."""
        typ = {"artist": 10, "show": 4}.get(sec["type"])
        known, start = set(), 0
        while True:
            params = {"X-Plex-Container-Start": start, "X-Plex-Container-Size": 500}
            if typ:
                params["type"] = typ
            page = self.plex.get(f"/library/sections/{sec['key']}/all", **params)
            md = page.get("Metadata", []) or []
            for m in md:
                for media in m.get("Media", []) or []:
                    for part in media.get("Part", []) or []:
                        if part.get("file"):
                            known.add(norm(part["file"]))
            start += len(md)
            if not md or start >= int(page.get("totalSize", page.get("size", 0)) or 0):
                return known

    def run(self, days: int | None = None):
        if self.progress["running"] or not self.plex.configured:
            return
        days = days or self.progress["days"]
        self.progress.update(running=True, error="", days=days)
        try:
            since = time.time() - days * 86400
            ign, items = self.ignored(), []
            for sec in self._sections():
                cands = [c for c in self._candidates(sec, since) if norm(c["folder"]) not in ign]
                if not cands:
                    continue
                known = self._known(sec)
                items += [c for c in cands if norm(c["path"]) not in known]
            self.items = sorted(items, key=lambda i: -i["mtime"])
            self.progress["last_run"] = time.strftime("%Y-%m-%d %H:%M")
            self._save()
        except Exception as e:
            log.warning("Check for files Plex hasn't seen failed: %s", e)
            self.progress["error"] = str(e)[:200]
        self.progress["running"] = False

    def folders(self) -> list[dict]:
        groups: dict[str, dict] = {}
        for i in self.items:
            g = groups.setdefault(i["folder"], {"folder": i["folder"], "kind": i["kind"], "section": i["section"], "files": [], "newest": 0})
            g["files"].append(i["name"])
            g["newest"] = max(g["newest"], i["mtime"])
        return sorted(groups.values(), key=lambda g: -g["newest"])

    def send(self, folders: list[str] | None = None) -> int:
        """Ask Plex to look at just these folders (all listed ones when none are given)."""
        want = {norm(f) for f in folders} if folders else None
        n = 0
        for g in self.folders():
            if want is not None and norm(g["folder"]) not in want:
                continue
            self.plex.scan(g["section"], g["folder"])
            n += 1
            if n >= 60:
                break
        return n
