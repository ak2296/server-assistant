"""SQLite storage for artists, releases, sort log and subtitle log."""
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS artists (
    folder      TEXT PRIMARY KEY,   -- folder name in the library
    mbid        TEXT,               -- MusicBrainz id ('' = not found)
    mb_name     TEXT,
    last_check  TEXT
);
CREATE TABLE IF NOT EXISTS releases (
    mbid        TEXT PRIMARY KEY,   -- MusicBrainz release-group id
    artist      TEXT,
    title       TEXT,
    type        TEXT,
    date        TEXT,
    status      TEXT DEFAULT 'new', -- new | wanted | skipped | got
    found_at    TEXT
);
CREATE TABLE IF NOT EXISTS skipped_artists (
    artist      TEXT PRIMARY KEY,   -- library folder name; ignored until removed from this list
    at          TEXT
);
CREATE TABLE IF NOT EXISTS artist_pins (
    folder      TEXT PRIMARY KEY,   -- library folder name
    mbid        TEXT,               -- the MusicBrainz artist you picked for it (same-name artists are told apart this way)
    at          TEXT
);
CREATE TABLE IF NOT EXISTS movie_subs (
    rating_key  TEXT PRIMARY KEY,   -- Plex item id
    title       TEXT,
    year        TEXT,
    path        TEXT,
    langs       TEXT,               -- subtitle languages Plex sees, comma separated
    checked_at  TEXT
);
CREATE TABLE IF NOT EXISTS sort_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    at          TEXT,
    source      TEXT,
    dest        TEXT,
    ok          INTEGER,
    message     TEXT
);
CREATE TABLE IF NOT EXISTS imported (
    hash        TEXT PRIMARY KEY,   -- qBittorrent torrents already sorted
    name        TEXT
);
CREATE TABLE IF NOT EXISTS subtitle_log (
    video       TEXT,
    language    TEXT,
    status      TEXT,               -- downloaded | not_found | error
    at          TEXT,
    message     TEXT,
    PRIMARY KEY (video, language)
);
"""


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class DB:
    def __init__(self, path: str | Path):
        self.lock = threading.Lock()
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        for col in ("cover", "url", "src"):  # added later: older databases need the new columns
            try:
                self.conn.execute(f"ALTER TABLE releases ADD COLUMN {col} TEXT")
            except sqlite3.OperationalError:
                pass
        self.conn.commit()

    def execute(self, sql, params=()):
        with self.lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return cur

    def query(self, sql, params=()) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    # --- sort log ---
    def log_sort(self, source, dest, ok, message=""):
        self.execute(
            "INSERT INTO sort_log (at, source, dest, ok, message) VALUES (?,?,?,?,?)",
            (now(), str(source), str(dest or ""), 1 if ok else 0, message),
        )

    # --- subtitles ---
    def log_subtitle(self, video, language, status, message=""):
        self.execute(
            "INSERT OR REPLACE INTO subtitle_log (video, language, status, at, message) VALUES (?,?,?,?,?)",
            (str(video), language, status, now(), message),
        )

    def subtitle_attempt(self, video, language):
        rows = self.query(
            "SELECT * FROM subtitle_log WHERE video=? AND language=?", (str(video), language)
        )
        return rows[0] if rows else None
