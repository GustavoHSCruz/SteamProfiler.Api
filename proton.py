#!/usr/bin/env python3
"""steamprofiler.org - whether a game runs on Linux, according to the people running it.

Steam tells this site how many minutes a library spent on Windows, on Linux, on
macOS and on the Deck, and it has told it that all along. What Steam does not
say is the other half of the question: of everything somebody owns and has
never opened on the Deck, how much of it would simply work.

Valve publishes a Deck Verified status, and it is not in anything this server
can read - not in the Web API and not in the storefront's appdetails. What is
readable is ProtonDB, where the people who actually ran the game say how it
went, and the summary of that is one word per game.

The shape here is meta.py's, deliberately:

    lookup()  never fetches. A miss is a miss, and the screen prints what it
              has along with how much of the library that was.
    want()    queues. The worker gets to it.
    _run()    drains the queue slowly, because this is a community site with a
              community's budget and a burst from here is somebody's bill.

That split is what keeps a library of five thousand games from becoming five
thousand requests the moment somebody types a name. What the screen shows on
the first visit is whatever was already known; the rest fills in behind them.

What goes out is an appid, and it goes out from this server. ProtonDB sees
this server asking about a game, at a time that has nothing to do with when
anybody looked - the queue drains on its own clock, minutes behind the visit
that filled it. It never sees a profile, an address, or a library: appids
arrive one at a time, in the order the queue happens to hold them, mixed
across everybody who has been looked up since.

A tier is a fact about a game and not about a person, so one fetch answers it
for everybody who owns it, and it is kept for a month - long enough that the
crawl is cheap, short enough that a game fixed by a Proton release stops being
described by last season's reports.
"""
import json
import os
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from collections import OrderedDict
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "/app/data"))
# Its own file. Like houses.db this is a rebuildable index of something public,
# and losing it costs a re-fetch rather than a fact about anybody.
DB_PATH = DATA_DIR / "proton.db"

SOURCE = "https://www.protondb.com/api/v1/reports/summaries/%d.json"

# One request every two seconds and never in parallel. There is no published
# rate limit to obey, which is the reason to pick a slow one rather than an
# excuse to pick a fast one.
GAP = 2.0
# How long a verdict stands before it is asked again. A month: Proton ships
# often enough that a year-old "borked" is a lie, and rarely enough that a
# week would be the same answer four times.
MAX_AGE = timedelta(days=30)
# A game ProtonDB has never heard of gets asked again after this, and not
# sooner. Most of these are DLC-shaped things and tools that will never have a
# report, and re-asking daily would spend the whole budget on them.
MISS_AGE = timedelta(days=14)

# The queue is a courtesy, not a promise. Past this it drops what it cannot
# hold rather than growing without a bound, and the next visit re-queues.
MAX_QUEUE = 6000

# The tiers, worst to best. Kept in order here because "is this better than
# that" is a question the screens ask and a string cannot answer.
TIERS = ("borked", "bronze", "silver", "gold", "platinum")
RANK = {tier: i for i, tier in enumerate(TIERS)}

_db_lock = threading.Lock()
_queue_lock = threading.Lock()
_queued = OrderedDict()
_worker = None
_ready = False


@contextmanager
def _connect():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=20)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    try:
        with con:
            yield con
    finally:
        con.close()


def init():
    """Idempotent, and called from every entry point rather than only from
    start(): the command line reads this cache too, where no worker runs."""
    global _ready
    if _ready:
        return
    with _db_lock, _connect() as con:
        con.executescript("""
            -- One row per app, whether or not there was anything to say. A
            -- game with no reports is a real answer and is written down as
            -- one: without it the queue would ask about the same silent
            -- three thousand apps for ever.
            CREATE TABLE IF NOT EXISTS reports (
                appid      INTEGER PRIMARY KEY,
                tier       TEXT,
                best       TEXT,
                trending   TEXT,
                confidence TEXT,
                score      REAL,
                total      INTEGER,
                seen_at    TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS reports_by_tier ON reports (tier);
        """)
    _ready = True


def _now():
    return datetime.now(timezone.utc)


def _age(stamp):
    if not stamp:
        return None
    try:
        seen = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=timezone.utc)
    return _now() - seen


def lookup(appids):
    """What is already known about these apps. Never fetches; a miss is a miss.

    `tier` is None for a game ProtonDB has no reports on, and the key being
    present at all is what says the question has been asked."""
    init()
    appids = [int(a) for a in appids]
    if not appids:
        return {}
    out = {}
    with _db_lock, _connect() as con:
        for start in range(0, len(appids), 400):
            chunk = appids[start:start + 400]
            marks = ",".join("?" * len(chunk))
            for r in con.execute(
                    f"SELECT * FROM reports WHERE appid IN ({marks})", chunk):
                out[r["appid"]] = {
                    "tier": r["tier"], "best": r["best"],
                    "trending": r["trending"], "confidence": r["confidence"],
                    "score": r["score"], "total": r["total"],
                }
    return out


def want(appids):
    """Queue whatever is missing or stale. Returns nothing; the worker gets to
    it when it gets to it, and the next lookup() sees more than the last."""
    init()
    appids = [int(a) for a in appids]
    if not appids:
        return
    fresh = set()
    with _db_lock, _connect() as con:
        for start in range(0, len(appids), 400):
            chunk = appids[start:start + 400]
            marks = ",".join("?" * len(chunk))
            for r in con.execute(
                    f"SELECT appid, tier, seen_at FROM reports WHERE appid IN ({marks})",
                    chunk):
                age = _age(r["seen_at"])
                limit = MAX_AGE if r["tier"] else MISS_AGE
                if age is not None and age < limit:
                    fresh.add(r["appid"])
    with _queue_lock:
        if len(_queued) >= MAX_QUEUE:
            return
        for appid in appids:
            if appid not in fresh:
                _queued[appid] = True


def _fetch(appid):
    """One app's summary, or None for a game nobody has reported on.

    Never raises. A source that is down is a screen with fewer verdicts on it,
    which the screen already knows how to say."""
    req = urllib.request.Request(SOURCE % appid, headers={
        # A real name, so the source can see who is asking and block it by
        # name if it ever wants to.
        "User-Agent": "steamprofiler.org (+https://steamprofiler.org)",
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as answer:
            data = json.load(answer)
    except urllib.error.HTTPError as e:
        # 404 is the answer "no reports", and it is written down as one.
        return {} if e.code == 404 else None
    except Exception:  # noqa: BLE001 - a crawl must survive anything
        return None
    if not isinstance(data, dict):
        return {}
    tier = data.get("tier")
    return {
        "tier": tier if tier in RANK else None,
        "best": data.get("bestReportedTier"),
        "trending": data.get("trendingTier"),
        "confidence": data.get("confidence"),
        "score": data.get("score"),
        "total": data.get("total"),
    }


def _save(appid, row):
    with _db_lock, _connect() as con:
        con.execute("""
            INSERT INTO reports (appid, tier, best, trending, confidence, score,
                                 total, seen_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(appid) DO UPDATE SET
                tier = excluded.tier, best = excluded.best,
                trending = excluded.trending, confidence = excluded.confidence,
                score = excluded.score, total = excluded.total,
                seen_at = excluded.seen_at
        """, (appid, row.get("tier"), row.get("best"), row.get("trending"),
              row.get("confidence"), row.get("score"), row.get("total"),
              _now().isoformat(timespec="seconds")))


def drain(limit=None):
    """Fetch what is queued, paced. Returns how many were written."""
    init()
    done = 0
    while limit is None or done < limit:
        with _queue_lock:
            if not _queued:
                return done
            appid, _ = _queued.popitem(last=False)
        row = _fetch(appid)
        if row is None:
            # The source did not answer. Put it back at the end rather than
            # writing down a verdict nobody gave.
            with _queue_lock:
                if len(_queued) < MAX_QUEUE:
                    _queued[appid] = True
            time.sleep(GAP * 4)
            continue
        _save(appid, row)
        done += 1
        time.sleep(GAP)
    return done


def state():
    """How much of the cache exists, for a screen that has to say so."""
    init()
    with _db_lock, _connect() as con:
        known = con.execute("SELECT COUNT(*) FROM reports").fetchone()[0]
        rated = con.execute(
            "SELECT COUNT(*) FROM reports WHERE tier IS NOT NULL").fetchone()[0]
    with _queue_lock:
        waiting = len(_queued)
    return {"known": known, "rated": rated, "queued": waiting}


def _run():
    while True:
        try:
            if not drain(limit=200):
                time.sleep(20)
        except Exception as e:  # noqa: BLE001 - a crawl thread must not die
            print(f"proton: {e}", flush=True)
            time.sleep(30)


def start():
    """One worker, started once. Daemon: the queue is in memory and losing it
    costs nothing, because the next visit fills it again."""
    global _worker
    init()
    if _worker is None:
        _worker = threading.Thread(target=_run, name="proton", daemon=True)
        _worker.start()
    return _worker


if __name__ == "__main__":
    import sys
    init()
    ids = [int(a) for a in sys.argv[1:] if a.isdigit()]
    if ids:
        want(ids)
        print("buscados:", drain())
        print(json.dumps(lookup(ids), ensure_ascii=False, indent=1))
    print(json.dumps(state(), ensure_ascii=False, indent=1))
