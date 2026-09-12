#!/usr/bin/env python3
"""steamprofiler.org - the shut door, and the only thing here written to disk.

guard.py counts what a request costs and shuts an address out for half a
minute when it keeps overrunning. That is the right answer for a visitor who
is going too fast, because they are still a visitor. It is the wrong answer for
something that asked for /.env: that is not a reader who got carried away, it
is somebody looking for a credential to steal, and there is no second reading
of it. Those get two days.

Two days is why this file exists at all. Everything else the gate holds lives
in memory and is worth nothing an hour later, so a restart forgetting it costs
nothing. A ban outlives the process that issued it by design - the containers
here are restarted on every deploy, and a ban that a deploy lifts is not a ban.
So it goes to SQLite, and the memory copy in front of it is a cache, not the
record.

That cache matters more than it looks. nginx asks this module about *every*
request through auth_request, images and fonts included, so `until()` is the
hottest path in the whole site. It is a dict lookup behind an early return, and
it does not touch SQLite and does not take a lock.

Addresses are never stored, here least of all. The key is store.ip_hash(), the
same salted digest the message board counts votes with. That has a consequence
worth stating plainly: a ban cannot be read back into an address, cannot be
looked up by one from outside this server, and dies with the salt.
"""

import json
import os
import sqlite3
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import store

DATA_DIR = Path(os.environ.get("DATA_DIR", "/app/data"))
DB_PATH = DATA_DIR / "bans.db"

# Two days, as asked for. Long enough that a scanner's whole sweep is wasted and
# it moves on; short enough that a shared address behind carrier NAT is not
# punished for a week over somebody else's traffic.
BAN_FOR = int(os.environ.get("GUARD_BAN", str(2 * 24 * 3600)))

# An address let back in and banned again inside this window is a repeat, and
# the owner is told rather than left to notice. Without it the appeal is a hole
# you can drive through: get banned, write something sorry-sounding, get lifted,
# resume the sweep, and every appeal after that arrives looking like the first
# one. It is the same forty-eight hours as the ban by default, but a separate
# setting, because the question "how long is the punishment" and the question
# "how long do I remember you" are not the same question.
REOFFEND_WINDOW = int(os.environ.get("GUARD_REOFFEND", str(48 * 3600)))

# How long a ban that no longer applies is kept, counted from the moment it
# stopped applying. This is the only reason any row outlives its own expiry,
# and it is kept deliberately short: long enough to answer "have I seen this
# one before", short enough that it is not a log of who visited. A week.
HISTORY_FOR = int(os.environ.get("GUARD_HISTORY", str(7 * 24 * 3600)))

# How many distinct paths one address gets to try before this stops writing
# them down, and how many of them the panel is shown. The first number bounds
# the cost: the trap is no longer behind the gate, so a sweep now reaches this
# module once per path instead of once per sweep, and without a ceiling a
# three-hundred-path scan would be three hundred writes. The second is a
# reading limit, not a storage one - nobody needs to see the whole alphabet of
# .env suffixes to know what they are looking at.
MAX_PROBES = 60
SHOW_PATHS = 12

# Wall clock, not time.monotonic(). guard.py uses monotonic everywhere and is
# right to - it never compares across a restart. This does exactly that, and
# monotonic resets to zero when the process does, so every ban would lift on
# deploy. The trade is that a clock jump moves expiries, which at two days is
# noise.
_lock = threading.Lock()
_active = {}
_ready = False
# hash -> the paths it has already been seen trying. Purely a write filter:
# a scanner asking for /.env forty times in a row has nothing new to say
# after the first, and this is what keeps that from being forty writes.
_probe = {}


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
    # FULL, not the NORMAL the caches use. A cache may lose its last second; a
    # shut door may not - surviving a restart is the entire reason this one is
    # on disk at all. Written rarely, so being strict costs nothing.
    con.execute("PRAGMA synchronous=FULL")
    try:
        with con:
            yield con
    finally:
        con.close()


def init():
    """Create the table, drop what is past keeping, load the rest into memory."""
    global _ready
    with _lock, _connect() as con:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS bans (
                ip_hash    TEXT    PRIMARY KEY,
                until      INTEGER NOT NULL,
                reason     TEXT    NOT NULL,
                path       TEXT,
                hits       INTEGER NOT NULL DEFAULT 1,
                created_at TEXT    NOT NULL
            );
            CREATE INDEX IF NOT EXISTS bans_until ON bans (until);
        """)
        # Added after the table already existed in a running container, so it
        # arrives as a migration rather than in the CREATE above.
        have = {r["name"] for r in con.execute("PRAGMA table_info(bans)")}
        if "lifted_at" not in have:
            con.execute("ALTER TABLE bans ADD COLUMN lifted_at INTEGER")
        if "lifts" not in have:
            con.execute("ALTER TABLE bans ADD COLUMN lifts INTEGER NOT NULL DEFAULT 0")
        if "paths" not in have:
            con.execute("ALTER TABLE bans ADD COLUMN paths TEXT")

        now = _now()
        # A row is dropped once it has been irrelevant for HISTORY_FOR, counted
        # from whichever came first: being lifted, or running out. Until then it
        # stays so that ban() can recognise a face.
        con.execute(
            "DELETE FROM bans WHERE COALESCE(lifted_at, until) + ? < ?",
            (HISTORY_FOR, now),
        )
        # Lifted rows are history, not bans. Loading them back into _active is
        # exactly the bug this column invites: the row survives with `until`
        # still in the future, and a restart would silently re-apply a ban the
        # owner had already answered.
        rows = con.execute(
            "SELECT ip_hash, until FROM bans WHERE until > ? AND lifted_at IS NULL",
            (now,),
        ).fetchall()
    _active.clear()
    _active.update({r["ip_hash"]: r["until"] for r in rows})
    _ready = True
    return len(_active)


def _forget(who):
    """Drop an expired ban from the live list. The row stays: it has served its
    time, but it has not stopped being something the owner may need to
    recognise. init() is what eventually deletes it, HISTORY_FOR later."""
    _active.pop(who, None)


def until(address):
    """The moment this address is free again, or None. The hot path.

    Read without the lock on purpose. The only writers are ban() and _forget(),
    both of which mutate the dict with a single operation that the GIL makes
    atomic, so the worst a reader can see is the value from immediately before
    or immediately after - never a torn one. Taking the lock here would put
    every image and font on the site behind the same mutex as a SQLite write."""
    if not _active:
        return None
    who = store.ip_hash(address)
    stamp = _active.get(who)
    if stamp is None:
        return None
    if stamp <= _now():
        _forget(who)
        return None
    return stamp


def ban(address, seconds=None, reason="trap", path=None):
    """Shut this address out, and remember what it was reaching for.

    Called once per trap path now rather than once per sweep, because the trap
    sits outside the gate: an address already banned still lands here, and that
    is deliberate. One recorded path is not context. "Tried /.env, then
    /.git/config, then /secrets.json, forty-seven paths in all" is, and it is
    the difference between the panel showing a string and the panel showing
    what happened.

    What keeps that affordable is below: a repeat of a path already seen writes
    nothing, and after MAX_PROBES distinct ones this stops writing at all."""
    # guard.py exempts the owner's own network from every ceiling, and this has
    # to agree with it. Not for symmetry: /admin returns 404 by design and the
    # owner is the one person likely to go looking at odd paths from a machine
    # on this network.
    if guard_private(address):
        return None
    seconds = BAN_FOR if seconds is None else int(seconds)
    who = store.ip_hash(address)
    now = _now()
    stamp = now + seconds
    path = (path or "")[:200] or None

    live = _active.get(who)
    seen = _probe.setdefault(who, set())
    if live and live > now and (path is None or path in seen or len(seen) >= MAX_PROBES):
        # Already shut out, and nothing here that has not been written down.
        return live
    if path:
        seen.add(path)

    with _lock, _connect() as con:
        prior = con.execute(
            "SELECT lifted_at, lifts, paths FROM bans WHERE ip_hash = ?", (who,)
        ).fetchone()
        # Let back in, and here again. That is the one thing about a ban the
        # owner cannot work out later from the row, because the next write is
        # about to overwrite it.
        again = bool(prior and prior["lifted_at"]
                     and now - prior["lifted_at"] <= REOFFEND_WINDOW)
        if again:
            reason = "repeat"

        kept = []
        if prior and prior["paths"]:
            try:
                kept = json.loads(prior["paths"])
            except ValueError:
                kept = []
        # A new ban starts the list over. The old one belonged to the sweep
        # that earned it, and mixing the two would read as one long attack.
        if again or not live or live <= now:
            kept = []
        if path and path not in kept and len(kept) < SHOW_PATHS:
            kept.append(path)

        con.execute("""
            INSERT INTO bans (ip_hash, until, reason, path, paths, hits, created_at)
            VALUES (?, ?, ?, ?, ?, 1, ?)
            ON CONFLICT(ip_hash) DO UPDATE SET
                until     = excluded.until,
                reason    = excluded.reason,
                path      = excluded.path,
                paths     = excluded.paths,
                hits      = CASE WHEN ? THEN 1 ELSE bans.hits + 1 END,
                -- The new ban is live, so the old lift stops applying. The
                -- count of lifts is what carries the history forward.
                lifted_at = NULL
        """, (who, stamp, reason, path, json.dumps(kept), _iso(now),
              1 if (again or not live or live <= now) else 0))
    _active[who] = stamp
    if again:
        # Loud, and on stderr, because this is the case that has to reach a
        # person. It lands in `docker logs steamprofiler-api`, which is what
        # dozzle is already pointed at. /healthz counts it and the appeal
        # carries it too; this is the one that arrives without being asked for.
        hours = round((now - prior["lifted_at"]) / 3600, 1)
        print(f"BAN REPEAT: address unbanned {hours}h ago is banned again "
              f"(lifts so far: {prior['lifts']}, path: {path})", file=sys.stderr)
    return stamp


def lift(ip_hash):
    """Let one back in early, by hash - the only handle that exists.

    Marks rather than deletes. Deleting was the obvious thing and it was wrong:
    it threw away the only evidence that this address had ever been let in
    before, so an address could be banned, appeal, be lifted and resume, and
    every appeal after the first would arrive looking exactly like the first."""
    _active.pop(ip_hash, None)
    with _lock, _connect() as con:
        cur = con.execute("""
            UPDATE bans SET lifted_at = ?, lifts = lifts + 1
            WHERE ip_hash = ? AND lifted_at IS NULL
        """, (_now(), ip_hash))
    return bool(cur.rowcount)


def describe(address):
    """The ban held against this address, or None.

    Written for the appeal: what the owner needs in order to answer one is not
    "somebody says they were banned" but which path did it and how many times.
    One hit on /config.js reads like a false positive worth lifting; three
    hundred hits walking the whole .env alphabet does not."""
    return by_hash(store.ip_hash(address))


def by_hash(who):
    """describe(), for the one caller that already holds the hash rather than
    the address: the panel, reading it off an appeal."""
    now = _now()
    with _lock, _connect() as con:
        row = con.execute("""
            SELECT until, reason, path, paths, hits, created_at, lifted_at, lifts
            FROM bans WHERE ip_hash = ?
        """, (who,)).fetchone()
    if row is None:
        return None
    out = dict(row)
    try:
        out["paths"] = json.loads(row["paths"]) if row["paths"] else []
    except ValueError:
        out["paths"] = []
    out["active"] = row["until"] > now and row["lifted_at"] is None
    out["lifted_ago"] = (now - row["lifted_at"]) if row["lifted_at"] else None
    out["until_at"] = _iso(row["until"])
    # The whole question this row is kept to answer.
    out["repeat"] = row["reason"] == "repeat" or (row["lifts"] or 0) > 0
    return out


def recent(limit=100):
    """What is shut out right now, for the panel. Hashes, never addresses."""
    with _lock, _connect() as con:
        rows = con.execute("""
            SELECT ip_hash, until, reason, path, paths, hits, created_at, lifts
            FROM bans
            WHERE until > ? AND lifted_at IS NULL
            ORDER BY reason = 'repeat' DESC, until DESC
            LIMIT ?
        """, (_now(), int(limit))).fetchall()
    out = []
    for r in rows:
        row = dict(r)
        try:
            row["paths"] = json.loads(r["paths"]) if r["paths"] else []
        except ValueError:
            row["paths"] = []
        row["until_at"] = _iso(r["until"])
        # Every row this query returns is a live ban. Said out loud so the
        # panel can read one shape whether an entry came from here or from
        # by_hash(), which also returns the expired and the lifted.
        row["active"] = True
        row["repeat"] = r["reason"] == "repeat" or (r["lifts"] or 0) > 0
        out.append(row)
    return out


def state():
    """Counts for /healthz. No hashes, no paths, nothing to correlate."""
    now = _now()
    live = [t for t in _active.values() if t > now]
    # Counted off disk rather than off _active, which holds no reason. Cheap:
    # this runs on /healthz, not on a request.
    try:
        with _lock, _connect() as con:
            again = con.execute("""
                SELECT COUNT(*) FROM bans
                WHERE until > ? AND lifted_at IS NULL AND reason = 'repeat'
            """, (now,)).fetchone()[0]
            lifted = con.execute(
                "SELECT COUNT(*) FROM bans WHERE lifted_at IS NOT NULL"
            ).fetchone()[0]
    except sqlite3.Error:
        again = lifted = -1
    return {
        "banned": len(live),
        "ready": _ready,
        "longest_left": max((t - now for t in live), default=0),
        # Addresses that were let back in and earned it again inside
        # REOFFEND_WINDOW. Anything other than zero is worth a look.
        "repeat": again,
        "lifted_kept": lifted,
    }


def guard_private(address):
    """guard._private, reached without importing guard.

    guard.py imports this module, so importing it back at module level is a
    cycle. The check is deliberately the same function rather than a copy of
    the rules: if the definition of "the owner's own network" ever changes,
    it must not change in one file and not the other."""
    import guard
    return guard.TRUST_PRIVATE and guard._private(address)
