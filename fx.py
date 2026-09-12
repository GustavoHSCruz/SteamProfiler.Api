#!/usr/bin/env python3
"""steamprofiler.org - one exchange rate a day, so the card prices can be read.

Every price on this site is quoted rather than converted. meta.py asks the
storefront belonging to the reader's language and prints what it answers,
because Valve prices each region separately and no exchange rate turns $29.99
into R$99.99. Cards are the one exception, and not by choice: the Community
Market's search ignores the `currency` parameter entirely - cards.py has the
measurements - so what arrives is cents of a dollar and nothing else.

This module exists to say, beside that dollar, roughly what it is in the money
the reader uses. The dollar stays the figure of record. The conversion goes
next to it, labelled as an approximation, and never appears on its own.

**Why it is an approximation and not a price.** The rate here is the day's
commercial one. Steam uses something very close to it on expensive items - a
card at $20.93 came back as R$108.67, which is 5.192 against this source's
5.197 on the same day - but cards are not expensive items. They cost three to
eight cents, and in that range two things break the arithmetic:

    rounding     every currency has its own floor and its own increment, and a
                 three-cent item feels all of it
    the pool     every currency has its own queue of listings, so the cheapest
                 offer in reais is not the cheapest offer in dollars converted

How much that costs is measurable. Three cards from three different games, all
at $0.05 within the same minute, were listed at R$0,22, R$0,24 and R$0,26. Over
a set of eight cards the errors largely cancel, but never enough to become a
price: it is the right order of magnitude, and that is how the page says it.

**Where it comes from.** @fawazahmed0/currency-api, served by jsDelivr, with
the project's own mirror as the fallback. No key, no signup, no published
quota, and six kilobytes a day for the two currencies this site reads in. If
both sources fail the last good rate stands; once that passes MAX_AGE the
estimate leaves the page rather than quietly going stale, which is the same
rule meta.py's SALE_TTL follows for a discount.

Stdlib only, like everything else in the api container.
"""

import json
import os
import sqlite3
import threading
import time
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "/app/data"))
# The same file meta.py and cards.py write: one cache of facts about games and
# the money they cost, two tables in it, one backup to take.
DB_PATH = DATA_DIR / "meta.db"

# jsDelivr first because it is an actual CDN; the second is the mirror the
# project itself publishes and documents as the fallback. Both serve the same
# file, so either one answers the whole question.
SOURCES = (
    "https://cdn.jsdelivr.net/npm/@fawazahmed0/currency-api@latest"
    "/v1/currencies/usd.min.json",
    "https://latest.currency-api.pages.dev/v1/currencies/usd.min.json",
)
# The currencies of the three storefronts the site reads in. The dollar is the
# base and needs no row: it is what the card cents already arrive in.
WANTED = ("BRL", "RUB")
UA = "steamprofiler.org"
TIMEOUT = 15

# The source publishes once a day. Reading twice a day catches the turn without
# ever depending on what time it happens.
TTL = timedelta(hours=12)
# Past this the rate is no longer served. A week-old rate is not a poor
# approximation, it is a number nobody can date - and the page goes back to
# showing the dollar alone, which is still correct.
MAX_AGE = timedelta(days=7)
# How long to wait before trying again after a failure. Both sources down at
# once is rare enough not to deserve any hurry.
RETRY = timedelta(minutes=20)
# How often the worker wakes to see whether it is time.
TICK = 900
# A sanity ceiling rather than a guess about exchange rates: a currency worth
# less than this per dollar, or more than that, is a corrupted answer and not a
# quote. It is there for a source that one day answers zero, or text, or a
# field that moved.
RATE_MIN = 0.001
RATE_MAX = 10_000_000
# How long quote() may answer from memory before reading the table again. It is
# asked once per card set, so a library with three hundred of them would open
# three hundred connections for two floats that change once a day.
CACHE = 60
# FX_OFFLINE=1 makes every rate read fail at once, the way MARKET_OFFLINE does
# for the market: it is how the page without an estimate is reached on purpose.
OFFLINE = os.environ.get("FX_OFFLINE") == "1"

_db_lock = threading.Lock()
_worker = None
_ready = False
_next_try = 0.0
_cache = None
_cache_until = 0.0


@contextmanager
def _connect():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    # NORMAL rather than FULL, like the other caches - see meta.py. One rate a
    # day is written here, and the worst a power cut can do is send the next
    # start to ask for today's rate a second time.
    con.execute("PRAGMA synchronous=NORMAL")
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
            -- One row per currency. `day` is the date the source stamps, which
            -- is the day of the quote; `seen_at` is when this server read it.
            -- They answer different questions and neither stands in for the
            -- other.
            CREATE TABLE IF NOT EXISTS fx_rates (
                cur     TEXT PRIMARY KEY,
                rate    REAL NOT NULL,
                day     TEXT,
                seen_at TEXT NOT NULL
            );
        """)
    _ready = True


def _now():
    return datetime.now(timezone.utc)


def _age(value):
    if not value:
        return None
    try:
        when = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return _now() - when


def _get(url):
    if OFFLINE:
        return None
    request = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as answer:
            if answer.status != 200:
                return None
            return json.loads(answer.read().decode("utf-8", "replace"))
    except (OSError, TimeoutError, ValueError):
        return None


def _read_source():
    """The two sources, in order, until one answers. Returns
    {"day": "2026-08-30", "rates": {"BRL": 5.19...}} or None.

    An answer carrying none of the wanted currencies counts as a failure and
    moves on to the next source: it is likelier that the format changed than
    that the real stopped existing."""
    for url in SOURCES:
        data = _get(url)
        if not isinstance(data, dict):
            continue
        table = data.get("usd")
        if not isinstance(table, dict):
            continue
        rates = {}
        for cur in WANTED:
            value = table.get(cur.lower())
            if isinstance(value, (int, float)) and RATE_MIN < value < RATE_MAX:
                rates[cur] = float(value)
        if rates:
            day = data.get("date")
            return {"day": day if isinstance(day, str) else None, "rates": rates}
    return None


def refresh(force=False):
    """Read the source and write it down, if it is time. True when it wrote.

    A failure erases nothing: yesterday's rate stays in the table and stays
    served until MAX_AGE, because an approximation from yesterday still says
    what the page needs to say and an empty panel says nothing at all."""
    global _next_try
    init()
    if not force:
        if time.monotonic() < _next_try or not _due():
            return False
    got = _read_source()
    if got is None:
        _next_try = time.monotonic() + RETRY.total_seconds()
        return False
    stamp = _now().isoformat(timespec="seconds")
    with _db_lock, _connect() as con:
        con.executemany(
            "INSERT INTO fx_rates (cur, rate, day, seen_at) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(cur) DO UPDATE SET"
            " rate = excluded.rate, day = excluded.day, seen_at = excluded.seen_at",
            [(cur, rate, got["day"], stamp) for cur, rate in got["rates"].items()])
    _next_try = 0.0
    _forget()
    return True


def _forget():
    """Drop the memo, so the next quote() reads what was just written."""
    global _cache, _cache_until
    _cache, _cache_until = None, 0.0


def _rows():
    init()
    with _db_lock, _connect() as con:
        return con.execute("SELECT * FROM fx_rates").fetchall()


def _due():
    rows = {r["cur"]: r for r in _rows()}
    for cur in WANTED:
        row = rows.get(cur)
        if row is None:
            return True
        age = _age(row["seen_at"])
        if age is None or age > TTL:
            return True
    return False


def quote():
    """The day's rates, ready to go into an answer, or None.

    Only what is still inside MAX_AGE goes in. `at` is the day the source
    stamped, and that is the one the page shows: a reader wants to know when
    the rate is from, not when this server woke up to fetch it.

    Never raises. This is called from the middle of an answer about cards, and
    a card set is worth reading whether or not there is a rate to caption it
    with - a table that cannot be opened means no estimate, not no page."""
    global _cache, _cache_until
    now = time.monotonic()
    if now < _cache_until:
        return _cache
    try:
        rows = _rows()
    except (sqlite3.Error, OSError):
        return _cache
    rates, day = {}, None
    for row in rows:
        if row["cur"] not in WANTED:
            continue
        age = _age(row["seen_at"])
        if age is None or age > MAX_AGE:
            continue
        rates[row["cur"]] = row["rate"]
        day = day or row["day"] or (row["seen_at"] or "")[:10]
    _cache = {"rates": rates, "at": day} if rates else None
    _cache_until = now + CACHE
    return _cache


def _run():
    while True:
        try:
            refresh()
        except Exception as e:  # noqa: BLE001 - a crawl thread must not die
            print(f"fx: {e}", flush=True)
        time.sleep(TICK)


def start():
    """One worker, started once. Daemon: it holds nothing that must be
    flushed."""
    global _worker
    init()
    if _worker is None:
        _worker = threading.Thread(target=_run, name="fx", daemon=True)
        _worker.start()
    return _worker


if __name__ == "__main__":
    init()
    print("refresh:", refresh(force=True))
    print(json.dumps(quote(), ensure_ascii=False, indent=1))
