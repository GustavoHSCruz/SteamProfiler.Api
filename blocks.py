#!/usr/bin/env python3
"""steamprofiler.org - the profiles this service will not draw.

bans.py is the door for addresses: something asked for /.env, and there is no
second reading of that, so it gets two days. This is a different question with a
different answer, and mixing the two would have meant one table pretending to
answer both.

What this is for is the Companion drawing cards inside steamcommunity.com. Every
string on such a card comes from Steam itself - the persona name, the figures,
the avatar - precisely so that nothing can be published through it that Steam is
not already publishing on the same page. That closes the channel, and it is
still not the whole answer, because Steam moderates on Steam's own schedule and
the card is on our name.

So there has to be a way to stop drawing one profile. Not because of what this
service says about somebody, but because somebody was told about it and doing
nothing is the one position with no defence: since the STF settled the reading
of art. 19 of the Marco Civil in June 2025, an extrajudicial notice is what
starts the clock, and a tool with no off switch is a tool where being notified
and refusing to act are the same act.

Three consequences shape the file:

    it is by steamid       a subject, not an address, so none of bans.py's
                           machinery about expiry, repeat offence or carrier NAT
                           means anything here.
    it does not expire     a block is lifted by a person deciding to lift it.
    it takes the art       the persona is mirrored live and disappears with the
                           card, but the avatar is a copy on our own disk that
                           nginx serves directly. A block that leaves it there
                           has not removed the picture, and the picture is what
                           ends up in the screenshot.
"""

import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import art

DATA_DIR = Path(os.environ.get("DATA_DIR", "/app/data"))
DB_PATH = DATA_DIR / "blocks.db"

_lock = threading.Lock()
# The live set, in front of SQLite for the same reason bans.py keeps one: this
# is asked on the way to drawing every card, and that question should not reach
# the disk. A restart reloads it from init().
_blocked = set()


def _now():
    return int(time.time())


def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


@contextmanager
def _connect():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    try:
        with con:
            yield con
    finally:
        con.close()


def init():
    """Create the table and load the live set. Returns how many are blocked."""
    with _lock, _connect() as con:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS blocks (
                steamid    TEXT PRIMARY KEY,
                reason     TEXT NOT NULL,
                note       TEXT,
                created_at TEXT NOT NULL
            );
        """)
        rows = con.execute("SELECT steamid FROM blocks").fetchall()
    _blocked.clear()
    _blocked.update(r["steamid"] for r in rows)
    return len(_blocked)


def blocked(steamid):
    """Whether this profile is refused. The hot path, read without the lock.

    Same reasoning as bans.until(): the only writers replace a set membership in
    one operation, so a reader sees the state from just before or just after and
    never a torn one. An empty set answers before anything else happens."""
    return bool(_blocked) and str(steamid) in _blocked


def block(steamid, reason, note=None, art_urls=()):
    """Refuse this profile from now on, and take its cached pictures with it.

    `art_urls` is whatever was read off the profile - the avatar, and the
    background if the artwork card used one. They are passed in rather than
    looked up because art.py names its files after the hash of the URL and
    deliberately keeps no map from a steamid to them, so the only moment the
    connection exists is this one, in memory, while somebody is acting on a
    complaint."""
    steamid = str(steamid)
    with _lock, _connect() as con:
        con.execute(
            "INSERT OR REPLACE INTO blocks (steamid, reason, note, created_at) "
            "VALUES (?, ?, ?, ?)",
            (steamid, reason, note, _iso(_now())),
        )
    _blocked.add(steamid)
    return sum(1 for url in art_urls if url and art.forget(url))


def lift(steamid):
    """Let a profile be drawn again. The cached art is not restored: it comes
    back on its own the next time a card is drawn, from Steam, which is where it
    should be coming from anyway."""
    steamid = str(steamid)
    with _lock, _connect() as con:
        con.execute("DELETE FROM blocks WHERE steamid = ?", (steamid,))
    _blocked.discard(steamid)


def recent(limit=100):
    """The blocks, newest first, for the admin panel."""
    with _connect() as con:
        rows = con.execute(
            "SELECT steamid, reason, note, created_at FROM blocks "
            "ORDER BY created_at DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
    return [dict(r) for r in rows]


def state():
    """How many, for /healthz."""
    return {"blocked": len(_blocked)}
