"""steamprofiler.org - the key art cache.

Every game page opens on the game's own art, and `library_hero.jpg` is around
400 KB. Sending the visitor to Steam's CDN for it means that 400 KB crosses the
internet on every page view, of every game, for every visitor.

So it is fetched once and kept. The first request for an appid goes to Steam,
writes the bytes under `data/art/`, and answers; every request after that is
served by nginx straight off disk and never reaches Python at all - nginx tries
the file first and only falls back to this module when it is missing.

Nothing is pre-fetched. A library of 351 games would be 140 MB of art nobody
asked for; the cache only ever holds the games whose pages were actually
opened.

Stdlib only, like everything else in the api container.
"""

import os
import threading
import urllib.error
import urllib.request
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "/app/data"))
ART_DIR = DATA_DIR / "art"
CDN = "https://cdn.cloudflare.steamstatic.com/steam/apps"

# The wide hero first; the small header is the fallback for the old and the
# delisted, which never had a hero image. Of the owner's hundred, nine.
VARIANTS = ("library_hero.jpg", "header.jpg")

# Steam's art is a few hundred KB. Anything much larger is not what we asked
# for, and writing it would mean a bad response could fill the disk.
MAX_BYTES = 4 * 1024 * 1024
TIMEOUT = 20

# One lock per appid being fetched, so a burst of first-time requests for the
# same game makes one trip to Steam rather than one per connection.
_locks_guard = threading.Lock()
_locks = {}


def _lock_for(appid):
    with _locks_guard:
        return _locks.setdefault(appid, threading.Lock())


def path_for(appid):
    return ART_DIR / f"{appid}.jpg"


def _download(appid):
    """The first variant that answers, or None if the app has no art at all."""
    for name in VARIANTS:
        req = urllib.request.Request(
            f"{CDN}/{appid}/{name}",
            headers={"User-Agent": "steamprofiler.org"},
        )
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                if r.status != 200:
                    continue
                body = r.read(MAX_BYTES + 1)
        except (urllib.error.URLError, OSError):
            continue
        # A JPEG starts with FF D8. Steam answers some misses with an HTML page
        # and a 200, and writing that as `<appid>.jpg` would cache the mistake.
        if len(body) > MAX_BYTES or not body.startswith(b"\xff\xd8"):
            continue
        return body
    return None


def get(appid):
    """The art for `appid`, from disk if it is there and from Steam if not.

    Returns the bytes, or None when Steam has no art for this app - the page
    drops the band rather than showing a broken frame, so None is an answer
    rather than an error."""
    appid = int(appid)
    dest = path_for(appid)
    try:
        return dest.read_bytes()
    except OSError:
        pass

    with _lock_for(appid):
        # Another thread may have written it while this one waited.
        try:
            return dest.read_bytes()
        except OSError:
            pass

        body = _download(appid)
        if body is None:
            return None

        # Written under a temporary name and renamed, because nginx serves this
        # directory directly: a half-written file would be served as the art.
        ART_DIR.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            tmp.write_bytes(body)
            os.replace(tmp, dest)
        except OSError:
            # A read-only or full disk is not worth failing the request over -
            # the bytes are in hand, so answer with them and skip the cache.
            try:
                tmp.unlink()
            except OSError:
                pass
        return body


def stats():
    """How much has been kept, for /healthz."""
    try:
        files = list(ART_DIR.glob("*.jpg"))
    except OSError:
        return {"count": 0, "bytes": 0}
    return {"count": len(files), "bytes": sum(f.stat().st_size for f in files)}
