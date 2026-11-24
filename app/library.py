"""Read the music library: artist folders and the albums already owned."""
import re
import unicodedata
from pathlib import Path

AUDIO_EXT = {".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".wma", ".alac", ".aiff", ".ape", ".wv"}

_YEAR_PREFIX = re.compile(r"^\s*[\(\[]?\d{4}[\)\]]?\s*[-–._]?\s*")
_BRACKETS = re.compile(r"[\(\[\{].*?[\)\]\}]")


def normalize(text: str) -> str:
    """Lower-case, strip accents, brackets and punctuation for loose matching."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower().replace("&", "and")
    text = _BRACKETS.sub(" ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    text = re.sub(r"^the ", "", text.strip())
    return text.strip()


def album_title_from_folder(name: str) -> str:
    """'1990 - Rust in Peace [FLAC]' -> 'Rust in Peace'."""
    return _YEAR_PREFIX.sub("", name).strip()


def _roots(library) -> list[Path]:
    """Music library setting -> list of existing folders (one or several roots)."""
    if not library:
        return []
    items = [library] if isinstance(library, (str, Path)) else list(library)
    return [Path(x) for x in items if x and Path(x).is_dir()]


def list_artists(library) -> list[str]:
    names = set()
    for root in _roots(library):
        names.update(p.name for p in root.iterdir() if p.is_dir() and not p.name.startswith("."))
    return sorted(names, key=str.lower)


def owned_albums(library, artist_folder: str) -> set[str]:
    """Normalized titles of album folders (and loose audio files) under an artist, in every root."""
    titles = set()
    for root in _roots(library):
        d = root / artist_folder
        if not d.is_dir():
            continue
        for p in d.iterdir():
            if p.is_dir():
                titles.add(normalize(album_title_from_folder(p.name)))
            elif p.suffix.lower() in AUDIO_EXT:
                titles.add(normalize(p.stem))
    titles.discard("")
    return titles


def find_artist_dir(library, artist: str) -> Path | None:
    """Existing artist folder matching a tag value (case/accents/'The' ignored), in any root."""
    target = normalize(artist)
    for root in _roots(library):
        for p in root.iterdir():
            if p.is_dir() and normalize(p.name) == target:
                return p
    return None


def find_artist_folder(library, artist: str) -> str | None:
    d = find_artist_dir(library, artist)
    return d.name if d else None


def first_root(library) -> Path | None:
    roots = _roots(library)
    return roots[0] if roots else None


_EDITION_WORDS = re.compile(
    r"\b(deluxe|edition|expanded|remaster(ed)?|anniversary|bonus|tracks?|special|limited|version|\d+(th|st|nd|rd)|flac|mp3|web|cd)\b"
)


def _core(text: str) -> str:
    return re.sub(r"\s+", " ", _EDITION_WORDS.sub(" ", text)).strip()


def is_owned(title: str, owned: set[str]) -> bool:
    t = _core(normalize(title))
    if not t:
        return False
    for o in owned:
        o = _core(o)
        if t == o:
            return True
        # allow small extras like "deluxe edition" / "remastered", but not
        # a short single title that merely appears inside an album name
        short, long_ = sorted((t, o), key=len)
        if len(short) >= 6 and long_.startswith(short) and len(short) / len(long_) >= 0.45:
            return True
    return False
