"""steamprofiler.org - the key art cache.

Every game page opens on the game's own art, and `library_hero.jpg` is around
400 KB. Sending the visitor to Steam's CDN for it means that 400 KB crosses the
internet on every page view, of every game, for every visitor.

So it is fetched once and kept. The first request for an appid goes to Steam,
writes the bytes under `data/art/`, and answers; every request after that is
served by nginx straight off disk and never reaches Python at all - nginx tries
the file first and only falls back to this module when it is missing.

Three smaller caches sit beside it, all for embed.py, which draws pictures that
have to leave the site and therefore cannot point at Steam's CDN at all: the
capsule of a game, the avatar of a profile, and the background a profile is
wearing, each inlined into an SVG as data. Same arrangement as the heroes -
fetched once, kept, never pre-fetched - in their own subdirectories, because
nginx serves the top level of this one straight to the public and those three
are read by Python and by nothing else.

Nothing is pre-fetched. A library of 351 games would be 140 MB of art nobody
asked for; the cache only ever holds the games whose pages were actually
opened.

Stdlib only, like everything else in the api container.
"""

import hashlib
import os
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "/app/data"))
ART_DIR = DATA_DIR / "art"
CDN = "https://cdn.cloudflare.steamstatic.com/steam/apps"

# The wide hero first; the small header is the fallback for the old and the
# delisted, which never had a hero image. Of the owner's hundred, nine.
VARIANTS = ("library_hero.jpg", "header.jpg")

# The same picture at the size an embed can carry. embed.py inlines these as
# data URIs, so what matters here is not looking good on a page of its own but
# being small enough that a chart of five games is still a file somebody can
# paste into a README: a capsule is 10 to 20 KB against the hero's 400.
THUMBS = ("capsule_231x87.jpg", "header.jpg")
CAP_DIR = ART_DIR / "caps"
# Avatars, which are the other picture an embed needs and the only one that does
# not belong to an appid. Steam serves them off its own hosts, and this only
# ever asks those - the URL arrives from Steam's own answer about a profile, and
# checking it anyway is what keeps this from being something a crafted payload
# could aim somewhere else.
FACE_DIR = ART_DIR / "faces"
FACE_HOSTS = ("avatars.steamstatic.com", "avatars.akamai.steamstatic.com",
              "avatars.cloudflare.steamstatic.com", "cdn.akamai.steamstatic.com",
              "steamcdn-a.akamaihd.net", "avatars.fastly.steamstatic.com",
              "community.cloudflare.steamstatic.com",
              "community.akamai.steamstatic.com")
# The third one, and the newest: what a profile is wearing. The artwork
# generator puts somebody's own Steam background behind their figures, and an
# artwork is downloaded and uploaded to Steam rather than pasted into a README,
# so this cache is allowed pictures an order of magnitude larger than the
# capsules above. Same hosts as the frames and the animated avatars, which is
# where fetch.py's ITEM_CDN points.
BACK_DIR = ART_DIR / "backs"
BACK_HOSTS = ("cdn.cloudflare.steamstatic.com", "cdn.akamai.steamstatic.com",
              "community.cloudflare.steamstatic.com",
              "community.akamai.steamstatic.com",
              "community.fastly.steamstatic.com",
              "shared.cloudflare.steamstatic.com",
              "shared.akamai.steamstatic.com", "steamcdn-a.akamaihd.net")
# A background is a wall, not a capsule: 1438x810 off Steam is a few hundred
# kilobytes. Still a ceiling, because a bad answer must not be able to fill
# the disk, and still far under what the artwork itself is allowed to weigh.
MAX_BACK_BYTES = 3 * 1024 * 1024

# Steam's art is a few hundred KB. Anything much larger is not what we asked
# for, and writing it would mean a bad response could fill the disk.
MAX_BYTES = 4 * 1024 * 1024
TIMEOUT = 20

# One lock per appid being fetched, so a burst of first-time requests for the
# same game makes one trip to Steam rather than one per connection.
_locks_guard = threading.Lock()
_locks = {}


def _lock_for(key):
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


def path_for(appid):
    return ART_DIR / f"{appid}.jpg"


def _fetch(url):
    """One JPEG off Steam, or None. Every reason to say no is in here."""
    req = urllib.request.Request(url, headers={"User-Agent": "steamprofiler.org"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            if r.status != 200:
                return None
            body = r.read(MAX_BYTES + 1)
    except (urllib.error.URLError, OSError):
        return None
    # A JPEG starts with FF D8. Steam answers some misses with an HTML page and
    # a 200, and writing that as `<appid>.jpg` would cache the mistake.
    if len(body) > MAX_BYTES or not body.startswith(b"\xff\xd8"):
        return None
    return body


# What a picture is, read off its first bytes. Steam answers some misses with
# an HTML page and a 200, and a cache that trusted the extension would keep
# that page under a name ending in .jpg for as long as the disk lasts.
def sniff(body):
    """`image/jpeg`, `image/png`, or None for something that is neither."""
    if body[:2] == b"\xff\xd8":
        return "image/jpeg"
    if body[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    return None


def _fetch_picture(url, cap=MAX_BACK_BYTES):
    """One JPEG or PNG off Steam, or None. The wider cousin of _fetch(): a
    profile background is published in both formats and a cache that only took
    one of them would be empty for half the people who bought one."""
    req = urllib.request.Request(url, headers={"User-Agent": "steamprofiler.org"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            if r.status != 200:
                return None
            body = r.read(cap + 1)
    except (urllib.error.URLError, OSError):
        return None
    if len(body) > cap or not sniff(body):
        return None
    return body


def _download(appid, variants=VARIANTS):
    """The first variant that answers, or None if the app has no art at all."""
    for name in variants:
        body = _fetch(f"{CDN}/{appid}/{name}")
        if body is not None:
            return body
    return None


def _keep(dest, key, produce):
    """The bytes at `dest`, produced and written once if they are not there yet.

    The lock is per `key` rather than per file so a burst of first-time requests
    for the same picture makes one trip to Steam and not one per connection."""
    try:
        return dest.read_bytes()
    except OSError:
        pass

    with _lock_for(key):
        # Another thread may have written it while this one waited.
        try:
            return dest.read_bytes()
        except OSError:
            pass

        body = produce()
        if body is None:
            return None

        # Written under a temporary name and renamed, because nginx serves this
        # directory directly: a half-written file would be served as the art.
        dest.parent.mkdir(parents=True, exist_ok=True)
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


def get(appid):
    """The art for `appid`, from disk if it is there and from Steam if not.

    Returns the bytes, or None when Steam has no art for this app - the page
    drops the band rather than showing a broken frame, so None is an answer
    rather than an error."""
    appid = int(appid)
    return _keep(path_for(appid), f"art:{appid}", lambda: _download(appid))


def thumb(appid):
    """The capsule for `appid`, small enough to travel inside an SVG."""
    appid = int(appid)
    return _keep(CAP_DIR / f"{appid}.jpg", f"cap:{appid}",
                 lambda: _download(appid, THUMBS))


def avatar(url):
    """One profile picture, cached by the URL it came from.

    Named after a hash of that URL and not after the steamid: Steam already
    names these after the hash of the image, so a new picture is a new URL and
    therefore a new file, and nobody has to work out when to expire the old one.
    A steamid is also not something this cache should be storing the name of."""
    try:
        host = urllib.parse.urlparse(url).hostname or ""
    except (TypeError, ValueError):
        return None
    if host.lower() not in FACE_HOSTS:
        return None
    name = hashlib.sha1(url.encode("utf-8")).hexdigest()[:24]
    return _keep(FACE_DIR / f"{name}.jpg", f"face:{name}", lambda: _fetch(url))


def backdrop(url):
    """One profile background, cached by the URL it came from.

    Named after a hash of that URL for the same reason avatars are: Steam
    names the file after the item, so a profile that changes its background is
    a different URL and therefore a different file, and nothing here has to
    work out when the old one stopped being true. A steamid is again not
    something this cache should be storing the name of.

    The extension is `.bin` and not `.jpg`, because these arrive as either
    format and the bytes say which - the callers sniff it back out with
    sniff(), and a file named after a format it is not would be a lie on disk
    that nginx would eventually serve."""
    try:
        host = urllib.parse.urlparse(url).hostname or ""
    except (TypeError, ValueError):
        return None
    if host.lower() not in BACK_HOSTS:
        return None
    name = hashlib.sha1(url.encode("utf-8")).hexdigest()[:24]
    return _keep(BACK_DIR / f"{name}.bin", f"back:{name}",
                 lambda: _fetch_picture(url))


def forget(url):
    """Drop the cached copy of one profile picture. True if a file went.

    Everything else in this module only ever adds, because a picture named after
    the hash of its own URL never stops being the right answer for that URL, and
    a profile that changes its avatar simply asks for a different file.

    This exists for the one case that is not about the picture being stale. When
    a profile is blocked - blocks.py, and the reason a block has to be possible
    at all - the card stops being drawn, but the avatar it was drawn from is
    already bytes on our disk, and nginx serves this directory directly. So the
    block has to take the file with it. The caller passes the URL it read off
    the profile; the name is recomputed here rather than stored anywhere,
    because a steamid is not something this cache keeps."""
    for hosts, directory, suffix in ((FACE_HOSTS, FACE_DIR, ".jpg"),
                                     (BACK_HOSTS, BACK_DIR, ".bin")):
        try:
            host = (urllib.parse.urlparse(url).hostname or "").lower()
        except (TypeError, ValueError):
            return False
        if host not in hosts:
            continue
        name = hashlib.sha1(url.encode("utf-8")).hexdigest()[:24]
        try:
            (directory / f"{name}{suffix}").unlink()
            return True
        except OSError:
            return False
    return False


def stats():
    """How much has been kept, for /healthz."""
    out = {}
    for label, pattern in (("hero", "*.jpg"), ("caps", "caps/*.jpg"),
                           ("faces", "faces/*.jpg"), ("backs", "backs/*.bin")):
        try:
            files = list(ART_DIR.glob(pattern))
        except OSError:
            files = []
        out[label] = {"count": len(files),
                      "bytes": sum(f.stat().st_size for f in files)}
    # The two figures /healthz has always printed, kept where they were: the
    # hero cache is the one that can actually fill a disk.
    out["count"] = out["hero"]["count"]
    out["bytes"] = out["hero"]["bytes"]
    return out
