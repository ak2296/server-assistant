"""Subtitle sources: built-in sites and APIs, plus websites/APIs the user adds in Settings.

Every source turns a movie + language into a list of Candidates; the best one is
downloaded. Websites are read with a small, forgiving crawler: it opens the search
page, follows links that look like the movie and the language, and collects
download links (.srt / .zip / ".../download"). Site presets are just crawler settings,
so a user-added website works the same way as the built-in ones.
"""
import io
import json
import logging
import re
import time
import unicodedata
import zipfile
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote_plus, urljoin, urlparse

import requests

log = logging.getLogger("subsources")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0 Safari/537.36")

# Names sites use for each language (lower case). Add more here if you need them.
LANG_NAMES = {
    "en": ["english"],
    "fa": ["farsi_persian", "farsi/persian", "farsi-persian", "persian", "farsi", "فارسی"],
    "sv": ["swedish", "svenska"],
    "ar": ["arabic"], "de": ["german"], "fr": ["french"], "es": ["spanish"], "it": ["italian"],
    "tr": ["turkish"], "ru": ["russian"], "nl": ["dutch"], "no": ["norwegian"], "da": ["danish"],
    "fi": ["finnish"], "pl": ["polish"], "pt": ["portuguese"], "el": ["greek"], "he": ["hebrew"],
    "zh": ["chinese"], "ja": ["japanese"], "ko": ["korean"], "hi": ["hindi"], "ur": ["urdu"],
    "ku": ["kurdish"], "ps": ["pashto"],
}
# words to prefer when the URL template asks for {lang_name}
LANG_URL_NAME = {"fa": "farsi_persian"}

SUB_EXT = (".srt", ".ass", ".ssa", ".vtt", ".sub")


@dataclass
class Movie:
    path: Path
    title: str
    year: str = ""
    imdb: str = ""      # tt0113277
    hash: str = ""
    size: int = 0

    @property
    def release(self) -> str:
        return self.path.stem


@dataclass
class Candidate:
    source: str
    lang: str
    name: str
    url: str
    score: float = 0
    referer: str = ""
    extra: dict = field(default_factory=dict)


class Blocked(Exception):
    """The site refused us (bot protection, login wall...)."""


# ------------------------------------------------------------------ helpers

def slug(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")


def lang_words(lang: str) -> list[str]:
    return LANG_NAMES.get(lang, []) + [lang]


def mentions_lang(text: str, lang: str) -> bool:
    t = text.lower().replace("%20", " ")
    for w in LANG_NAMES.get(lang, []):
        if w in t:
            return True
    # bare code only as a separate token, e.g. "/en/" or "-fa-" or "lang=fa"
    return re.search(rf"(?:^|[/_\-.=?&\s]){re.escape(lang)}(?:$|[/_\-.&\s])", t) is not None


def other_lang(text: str, lang: str) -> bool:
    """True if the text clearly names a different language than the one we want."""
    t = text.lower()
    for code, words in LANG_NAMES.items():
        if code == lang:
            continue
        for w in words:
            if len(w) > 3 and re.search(rf"(?<![a-z]){re.escape(w)}(?![a-z])", t):
                return not mentions_lang(t, lang)
    return False


def release_score(name: str, movie: Movie) -> float:
    """How well a subtitle's release name matches the video file name."""
    a = set(re.findall(r"[a-z0-9]+", name.lower()))
    b = set(re.findall(r"[a-z0-9]+", movie.release.lower()))
    if not a or not b:
        return 0
    common = a & b
    bonus = sum(2 for t in common if t in {"bluray", "brrip", "bdrip", "webrip", "web", "dl", "hdtv", "dvdrip", "1080p", "720p", "2160p", "remux", "x264", "x265", "hevc"})
    return len(common) + bonus


def fill(template: str, movie: Movie, lang: str) -> str:
    names = LANG_NAMES.get(lang, [lang])
    values = {
        "title": quote_plus(movie.title), "title_raw": movie.title, "title_slug": slug(movie.title),
        "year": movie.year, "imdb": movie.imdb, "imdb_num": movie.imdb.removeprefix("tt"),
        "lang": lang, "lang_name": LANG_URL_NAME.get(lang, names[0]), "hash": movie.hash, "size": str(movie.size),
    }
    out = template
    for k, v in values.items():
        out = out.replace("{" + k + "}", v or "")
    return out


class _Links(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self.title = ""
        self._a: list | None = None
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "a" and a.get("href"):
            self._a = [a["href"], ""]
        elif tag in ("button", "form") and (a.get("data-href") or a.get("action")):
            self.links.append((a.get("data-href") or a.get("action"), a.get("value", "") or ""))
        elif tag == "title":
            self._in_title = True

    def handle_data(self, data):
        if self._a is not None:
            self._a[1] += data
        if self._in_title:
            self.title += data

    def handle_endtag(self, tag):
        if tag == "a" and self._a is not None:
            self.links.append((self._a[0], " ".join(self._a[1].split())))
            self._a = None
        elif tag == "title":
            self._in_title = False


# ------------------------------------------------------------------ base

class Source:
    type = "base"
    label = "Source"

    def __init__(self, conf: dict, session: requests.Session, trace: list | None = None):
        self.conf = conf
        self.s = session
        self.trace = trace if trace is not None else []
        self.name = conf.get("name") or self.label

    def note(self, msg: str):
        self.trace.append(f"[{self.name}] {msg}")
        log.debug("[%s] %s", self.name, msg)

    def get(self, url: str, referer: str = "", **kw) -> requests.Response:
        self.note(f"GET {url}")
        headers = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}
        if referer:
            headers["Referer"] = referer
        headers.update(kw.pop("headers", {}))
        r = self.s.get(url, headers=headers, timeout=25, **kw)
        if r.status_code in (403, 429, 503) and ("cf-ray" in r.headers or "just a moment" in r.text[:3000].lower()
                                                 or "cloudflare" in r.text[:3000].lower()):
            raise Blocked(f"{urlparse(url).netloc} is blocking automatic access (Cloudflare bot protection)")
        if r.status_code == 429:
            raise Blocked(f"{urlparse(url).netloc} says too many requests - try later")
        r.raise_for_status()
        time.sleep(float(self.conf.get("delay", 1.0)))  # be polite
        return r

    def search(self, movie: Movie, lang: str) -> list[Candidate]:
        raise NotImplementedError

    def download(self, c: Candidate) -> bytes:
        r = self.get(c.url, referer=c.referer)
        return r.content


# ------------------------------------------------------------------ website crawler

class Website(Source):
    """Generic crawler. Settings:
       search_url      start page, with {title} {year} {imdb} {lang} {lang_name} ... filled in
       needs_imdb      skip movies without an IMDb id
       max_pages       pages to open per search (default 8)
       download_hint   regex for download links (default .srt/.zip/.rar or /download)
    """
    type = "website"
    label = "Website"

    def search(self, movie: Movie, lang: str) -> list[Candidate]:
        tmpl = self.conf.get("search_url", "")
        if not tmpl:
            return []
        if ("{imdb" in tmpl or self.conf.get("needs_imdb")) and not movie.imdb:
            self.note("skipped: no IMDb id for this movie")
            return []
        start = fill(tmpl, movie, lang)
        lang_in_start = "{lang" in tmpl
        dl_re = re.compile(self.conf.get("download_hint") or r"(\.srt|\.zip|\.rar|\.ass)(\?|$)|/download\b|download\.php|/dl/", re.I)
        host = urlparse(start).netloc
        title_s = slug(movie.title)
        title_words = [w for w in title_s.split("-") if len(w) > 2] or [title_s]
        max_pages = int(self.conf.get("max_pages", 8))

        queue = [(start, "", 0, lang_in_start, "")]  # url, referer, priority, language known, link text that led here
        seen, found = set(), []
        while queue and len(seen) < max_pages:
            # most promising pages first
            queue.sort(key=lambda q: -q[2])
            url, ref, _prio, page_lang_ok, ctx = queue.pop(0)
            if url in seen:
                continue
            seen.add(url)
            try:
                r = self.get(url, referer=ref)
            except Blocked:
                raise
            except Exception as e:
                self.note(f"failed: {e}")
                continue
            ctype = r.headers.get("Content-Type", "")
            if "html" not in ctype and not r.text.lstrip().startswith("<"):
                continue
            p = _Links()
            try:
                p.feed(r.text)
            except Exception:
                pass
            page_text = (p.title + " " + url).lower()
            page_lang = page_lang_ok or mentions_lang(page_text, lang)
            page_has_title = movie.imdb and movie.imdb in r.text or all(w in slug(r.text[:200000]) for w in title_words)

            for href, text in p.links:
                if href.startswith(("javascript:", "mailto:", "#")):
                    continue
                full = urljoin(url, href)
                if urlparse(full).netloc not in (host, "") and not dl_re.search(full):
                    continue
                blob = f"{full} {text}"
                lang_hit = mentions_lang(blob, lang)
                if other_lang(blob, lang) and not lang_hit:
                    continue
                if dl_re.search(full) and (lang_hit or page_lang) and page_has_title:
                    generic = len(text) < 8 or "download" in text.lower()
                    name = (ctx or p.title.strip() or Path(urlparse(full).path).name) if generic else text
                    score = release_score(f"{blob} {ctx}", movie) + (2 if lang_hit else 0)
                    found.append(Candidate(self.name, lang, name[:150], full, score, referer=url))
                    continue
                if full in seen:
                    continue
                s = slug(blob)
                prio = 0
                if movie.imdb and movie.imdb in full:
                    prio += 6
                if all(w in s for w in title_words):
                    prio += 4
                if movie.year and movie.year in blob:
                    prio += 1
                if lang_hit:
                    prio += 3
                if "subtitle" in s or "/sub" in full.lower():
                    prio += 1
                if prio >= 4 or (page_has_title and lang_hit):
                    queue.append((full, url, prio, page_lang or lang_hit, text or ctx))
        # de-duplicate
        uniq = {}
        for c in found:
            uniq.setdefault(c.url, c)
        self.note(f"{len(uniq)} candidate(s) after {len(seen)} page(s)")
        return list(uniq.values())

    def download(self, c: Candidate) -> bytes:
        r = self.get(c.url, referer=c.referer)
        body = r.content
        # some "download" links lead to one more page with the real button
        if body[:200].lstrip().lower().startswith((b"<!doctype", b"<html")):
            p = _Links()
            p.feed(r.text)
            dl_re = re.compile(self.conf.get("download_hint") or r"(\.srt|\.zip|\.rar|\.ass)(\?|$)|/download\b|download\.php|/dl/", re.I)
            for href, _t in p.links:
                full = urljoin(c.url, href)
                if dl_re.search(full) and full != c.url:
                    return self.get(full, referer=c.url).content
        return body


# ------------------------------------------------------------------ APIs

class OpenSubtitlesAPI(Source):
    type = "opensubtitles"
    label = "OpenSubtitles.com (API)"
    API = "https://api.opensubtitles.com/api/v1"

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.base = self.API
        self.token = None

    def _headers(self):
        h = {"Api-Key": self.conf.get("api_key", ""), "User-Agent": "ServerAssistant v1.0", "Accept": "application/json"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def _login(self):
        if self.token or not self.conf.get("username"):
            return
        r = self.s.post(f"{self.API}/login", json={"username": self.conf["username"], "password": self.conf.get("password", "")},
                        headers={**self._headers(), "Content-Type": "application/json"}, timeout=30)
        r.raise_for_status()
        d = r.json()
        self.token = d["token"]
        if d.get("base_url"):
            self.base = f"https://{d['base_url']}/api/v1"

    def search(self, movie, lang):
        if not self.conf.get("api_key"):
            self.note("no API key set")
            return []
        out = []
        attempts = []
        if movie.hash:
            attempts.append({"moviehash": movie.hash, "languages": lang})
        if movie.imdb:
            attempts.append({"imdb_id": movie.imdb.removeprefix("tt"), "languages": lang})
        attempts.append({"query": movie.title, "year": movie.year, "languages": lang, "type": "movie"})
        for params in attempts:
            params = {k: v for k, v in params.items() if v}
            self.note(f"search {params}")
            r = self.s.get(f"{self.base}/subtitles", params=params, headers=self._headers(), timeout=30)
            r.raise_for_status()
            for i in r.json().get("data", []):
                a = i["attributes"]
                if a.get("machine_translated") or not a.get("files"):
                    continue
                score = release_score(a.get("release", ""), movie) + (20 if a.get("moviehash_match") else 0) \
                    - (3 if a.get("hearing_impaired") else 0) + min(a.get("download_count", 0), 50000) / 25000
                out.append(Candidate(self.name, lang, a.get("release", ""), "", score, extra={"file_id": a["files"][0]["file_id"]}))
            if out:
                break
            time.sleep(1)
        return out

    def download(self, c):
        self._login()
        r = self.s.post(f"{self.base}/download", json={"file_id": c.extra["file_id"]},
                        headers={**self._headers(), "Content-Type": "application/json"}, timeout=30)
        if r.status_code == 406:
            raise Blocked("OpenSubtitles daily download limit reached")
        r.raise_for_status()
        return self.s.get(r.json()["link"], timeout=60).content


class SubDLAPI(Source):
    type = "subdl"
    label = "SubDL (API)"

    def search(self, movie, lang):
        if not self.conf.get("api_key"):
            self.note("no API key set")
            return []
        params = {"api_key": self.conf["api_key"], "languages": lang.upper(), "type": "movie", "subs_per_page": 30}
        if movie.imdb:
            params["imdb_id"] = movie.imdb
        else:
            params.update(film_name=movie.title, year=movie.year)
        self.note(f"search {movie.imdb or movie.title}")
        r = self.s.get("https://api.subdl.com/api/v1/subtitles", params=params, timeout=30)
        r.raise_for_status()
        d = r.json()
        if not d.get("status", True):
            self.note(f"API says: {d.get('error') or d.get('message')}")
            return []
        out = []
        for sub in d.get("subtitles", []):
            url = sub.get("url") or ""
            if not url:
                continue
            name = sub.get("release_name") or sub.get("name") or ""
            out.append(Candidate(self.name, lang, name, urljoin("https://dl.subdl.com", url), release_score(name, movie)
                                 - (3 if sub.get("hi") else 0)))
        return out


class JSONAPI(Source):
    """Generic JSON API added by the user. The app looks through the response for
    entries that have a download link, and uses any language field it finds."""
    type = "api"
    label = "JSON API"
    URL_KEYS = ("download_url", "download_link", "downloadlink", "download", "file_url", "zip", "link", "url", "href")
    LANG_KEYS = ("lang", "language", "language_code", "languagecode", "languagename", "lang_code", "iso639")
    NAME_KEYS = ("release_name", "release", "name", "title", "filename", "file_name")

    def search(self, movie, lang):
        tmpl = self.conf.get("search_url", "")
        if not tmpl:
            return []
        if "{imdb" in tmpl and not movie.imdb:
            return []
        url = fill(tmpl, movie, lang)
        headers = {}
        for line in (self.conf.get("headers") or "").splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip()] = v.strip()
        r = self.get(url, headers={"Accept": "application/json", **headers})
        try:
            data = r.json()
        except ValueError:
            self.note("response wasn't JSON")
            return []
        out = []

        def walk(node):
            if isinstance(node, dict):
                low = {k.lower(): v for k, v in node.items()}
                link = next((low[k] for k in self.URL_KEYS if isinstance(low.get(k), str) and low[k]), None)
                if link:
                    lv = next((str(low[k]) for k in self.LANG_KEYS if low.get(k)), "")
                    name = next((str(low[k]) for k in self.NAME_KEYS if low.get(k)), link)
                    if (not lv and "{lang" in tmpl) or (lv and mentions_lang(lv.lower(), lang)):
                        out.append(Candidate(self.name, lang, name, urljoin(url, link), release_score(name, movie), referer=url))
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)
        walk(data)
        self.note(f"{len(out)} candidate(s)")
        return out


# ------------------------------------------------------------------ presets

PRESETS = {
    "yify": {"type": "website", "name": "YIFY Subtitles", "base_url": "https://yifysubtitles.ch",
             "search_url": "{base_url}/movie-imdb/{imdb}", "needs_imdb": True, "max_pages": 6,
             "note": "Movies only, matched by IMDb id. If the site moves, change the address."},
    "subf2m": {"type": "website", "name": "subf2m (good for Persian)", "base_url": "https://subf2m.co",
               "search_url": "{base_url}/subtitles/searchbytitle?query={title}+{year}&l=", "max_pages": 10,
               "note": "Large Persian collection. Often protected by Cloudflare, which can block the app."},
    "podnapisi": {"type": "website", "name": "Podnapisi", "base_url": "https://www.podnapisi.net",
                  "search_url": "{base_url}/subtitles/search/advanced?keywords={title}&year={year}&language={lang}&movie_type=movie",
                  "max_pages": 6, "note": "No account needed."},
    "subsource": {"type": "website", "name": "SubSource", "base_url": "https://subsource.net",
                  "search_url": "{base_url}/search/{title}", "max_pages": 8, "enabled": False,
                  "note": "Subscene's successor. Partly built with JavaScript, so it may not work."},
    "opensubtitles": {"type": "opensubtitles", "name": "OpenSubtitles.com (API)",
                      "note": "Free account + API key: opensubtitles.com -> profile -> API consumers."},
    "subdl": {"type": "subdl", "name": "SubDL (API)", "enabled": False,
              "note": "API key from subdl.com (free account -> API). Big Persian/Subscene archive."},
}

TYPES = {c.type: c for c in (Website, OpenSubtitlesAPI, SubDLAPI, JSONAPI)}


def default_sources(old_subs_conf: dict | None = None) -> list[dict]:
    """First-run list. Carries over an OpenSubtitles key from the old settings."""
    old = old_subs_conf or {}
    out = []
    for key in ("subf2m", "yify", "podnapisi", "opensubtitles", "subdl", "subsource"):
        p = {k: v for k, v in PRESETS[key].items() if k != "note"}
        p.setdefault("enabled", True)
        p["preset"] = key
        if key == "opensubtitles":
            p.update(api_key=old.get("api_key", ""), username=old.get("username", ""), password=old.get("password", ""))
            p["enabled"] = bool(old.get("api_key"))
        out.append(p)
    return out


def build(conf: dict, session: requests.Session, trace: list | None = None) -> Source:
    conf = dict(conf)
    if conf.get("base_url"):
        conf["search_url"] = conf.get("search_url", "").replace("{base_url}", conf["base_url"].rstrip("/"))
    cls = TYPES.get(conf.get("type"), Website)
    return cls(conf, session, trace)


# ------------------------------------------------------------------ archives

def extract_subtitle(data: bytes, movie: Movie) -> tuple[bytes, str] | None:
    """Return (subtitle bytes, extension) from a raw subtitle or a zip/rar archive."""
    if data[:2] == b"PK":
        try:
            z = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile:
            return None
        files = [n for n in z.namelist() if n.lower().endswith(SUB_EXT) and not n.startswith("__MACOSX")]
        if not files:
            return None
        best = max(files, key=lambda n: (release_score(n, movie), n.lower().endswith(".srt"), -len(n)))
        return z.read(best), Path(best).suffix.lower()
    if data[:4] == b"Rar!":
        try:
            import rarfile  # optional: pip install rarfile + UnRAR/7-Zip on PATH
            rf = rarfile.RarFile(io.BytesIO(data))
            files = [n for n in rf.namelist() if n.lower().endswith(SUB_EXT)]
            if files:
                best = max(files, key=lambda n: release_score(n, movie))
                return rf.read(best), Path(best).suffix.lower()
        except Exception:
            pass
        return None
    head = data[:2000].lstrip(b"\xef\xbb\xbf").lstrip()
    if re.match(rb"^\d+\s*\r?\n\d{1,2}:\d{2}:\d{2}[,.]\d{3}\s*-->", head):
        return data, ".srt"
    if head.startswith(b"[Script Info]"):
        return data, ".ass"
    if head.startswith(b"WEBVTT"):
        return data, ".vtt"
    return None


def load_sources(path: Path) -> list[dict] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def save_sources(path: Path, sources: list[dict]):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(sources, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
