"""steamprofiler.org - what the storefront knows about a game, kept on disk.

Everything else here reads the *player*. This reads the *game*: what it costs
today, what genres Steam files it under, and what year it came out. None of that
depends on whose profile is being looked at, which is the whole reason this
cache is worth having - one fetch of Counter-Strike serves every visitor who
ever owned it, forever.

Two shapes of request, because the storefront answers them differently:

    price    `filters=price_overview` is the one filter that honours a list of
             appids, so a 350-game library costs four requests instead of 350
             - per storefront, and there are three of them, one per language
             the site reads in. This is why the money panel fills in almost at
             once.
    detail   genres, the release year and is_free come one app at a time. A
             large library takes minutes, so the panel says how far it has got
             rather than waiting for all of it.

Both run on a background worker with a deliberate crawl (one request every two
seconds), because this is Valve's storefront rather than their API: there is no
key, no documented quota, and the widely reported ceiling is around 200 requests
per five minutes per address. Going slower than that is free - nobody is waiting
on this thread - and going faster gets the server a 429 and nothing else.

SQLite rather than a file per appid, unlike art.py. Art is only fetched for
pages somebody opened; this is fetched for whole libraries, so it is tens of
thousands of rows, and a profile build wants all of them at once.

Stdlib only, like everything else in the api container.
"""

import html
import json
import os
import re
import sqlite3
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "/app/data"))
DB_PATH = DATA_DIR / "meta.db"
STORE = "https://store.steampowered.com/api/appdetails"
REVIEWS = "https://store.steampowered.com/appreviews"
APP_LIST = "https://api.steampowered.com/IStoreService/GetAppList/v1/"
# The storefront used when nothing else is named - for the command line, and
# for a country code that is not one of ours.
COUNTRY = os.environ.get("STORE_COUNTRY", "br")
# One storefront per language the site reads in. Steam's regional pricing is
# not an exchange rate - Valve sets each region separately, and Arma 3 is
# $29.99 in the US against R$99.99 in Brazil, which no conversion would ever
# produce. So the three prices are three facts, fetched three times, and the
# page shows the one belonging to the reader rather than a sum done here.
#
# Russia is the ragged one. Bethesda, EA, Ubisoft and Activision pulled out in
# 2022, so `cc=ru` has no store page for about a hundred and ten of the games
# this cache knows, against thirty-six for the other two. That is not an error
# and it is not hidden: those land in the `absent` state, which names the
# country it is talking about.
COUNTRIES = ("us", "br", "ru")
STORE_LANGUAGES = {"en": "english", "pt": "brazilian", "ru": "russian",
                   "english": "english", "brazilian": "brazilian", "russian": "russian"}
UA = "steamprofiler.org"


def cc_of(value):
    """A country code the storefront will be asked for, or the default. Never
    the visitor's raw input: this reaches a URL, and the set is closed."""
    v = (value or "").strip().lower()
    return v if v in COUNTRIES else COUNTRY


def language_of(value):
    """One of the three storefront languages the browser can select."""
    return STORE_LANGUAGES.get((value or "").strip().lower(), "english")

# The storefront honours a list only for price_overview, and only up to a point.
BATCH = 100
# One request every two seconds. See the module docstring.
INTERVAL = float(os.environ.get("STORE_INTERVAL", "2.0"))
TIMEOUT = 20
# A 429 means the crawl was too fast anyway, so it waits and then waits longer.
BACKOFF_MIN = 60
BACKOFF_MAX = 900
# Prices move independently from the catalogue around them.
PRICE_TTL = timedelta(days=14)
# The rich catalogue changes more often than a release year: requirements,
# supported platforms, descriptions and media are edited after launch.
CATALOG_TTL = timedelta(days=30)
REVIEWS_TTL = timedelta(hours=6)
# A ceiling on the queue, so a burst of lookups cannot grow it without bound.
MAX_QUEUE = 20000

# ── The page path ────────────────────────────────────────────────────
# Everything above is the crawl: it sweeps whole libraries and nobody is
# waiting on it, which is why fourteen days is the right number there and
# would be the wrong number here. What follows is for price(), where a page
# is on somebody's screen and the number on it is being read right now.
#
# PAGE_TTL is how old a price may be before opening a game's page goes and
# asks again. Six hours is at most four storefront requests a day for a game
# somebody actually looks at - nothing beside the crawl - and short enough
# that a sale which started this morning is on the page this afternoon.
PAGE_TTL = timedelta(hours=6)
# SALE_TTL is the one that matters. A discount is a claim with an end date
# Steam does not publish, so a "-70%" older than this is not repeated: the
# row degrades to its plain price and says when it was read. Better a number
# an hour stale than a badge that is false.
SALE_TTL = timedelta(minutes=45)
# A page may not wait twenty seconds. If the storefront has not answered in
# six, the page keeps what it had.
PAGE_TIMEOUT = 6
# How long a page open will queue for the shared one-request-every-INTERVAL
# slot before answering from disk instead. It has to be longer than INTERVAL
# or it could never win the slot at all - anything shorter and a page fetch
# fails whenever the crawl asked for something in the last half second, which
# is most of the time the crawl is running. Nobody sees this wait: the page is
# already drawn and the price fills in behind it.
PAGE_WAIT = INTERVAL + 0.5
# At most two on-demand fetches in flight, ever. The crawl already uses most
# of the storefront's tolerance, and a burst of page opens must not be able
# to add one request per visitor on top of it.
ON_DEMAND = 2
# STORE_OFFLINE=1 makes every fetch fail at once. Nothing in production wants
# this; it is how the degraded states are reached on purpose instead of by
# waiting for Valve to have a bad day. See the README.
OFFLINE = os.environ.get("STORE_OFFLINE") == "1"

_db_lock = threading.Lock()
_queue_lock = threading.Lock()
# One price queue per storefront, because a request carries one `cc` and the
# hundred-appid batching only works within one. The detail pass has no queue
# per country: genres, release year and is_free are the same everywhere.
_price_wanted = {cc: deque() for cc in COUNTRIES}
_detail_wanted = deque()
_queued_price = set()          # (appid, cc)
_queued_detail = set()         # appid
_urgent_detail = set()         # appid whose cached trailer lacks stream URLs
_worker = None
_backoff = BACKOFF_MIN
_fetched = 0

# One lock per app on the page path, the way art.py holds one per appid: four
# people opening the same game at once is one request, not four.
_locks_guard = threading.Lock()
_locks = {}
# Concurrency and pace, both shared with the crawl. _last_at is the last time
# *anything* here asked the storefront for something; _cool_until is set by
# whichever path took a 429 and honoured by both, because the address is one
# address and the storefront does not care which thread annoyed it.
_on_demand = threading.BoundedSemaphore(ON_DEMAND)
_pace_lock = threading.Lock()
_last_at = 0.0
_cool_until = 0.0
_page_guard = threading.Lock()
_page_jobs = 0


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


_ready = False


def init():
    """Idempotent, and called from every entry point rather than only from
    start(): fetch.py reads this cache from the command line too, where no
    worker is running and nothing else would have created the table."""
    global _ready
    if _ready:
        return
    with _db_lock, _connect() as con:
        con.executescript("""
            -- What is true of a game everywhere: free-to-play, release year,
            -- genres. One row per app.
            CREATE TABLE IF NOT EXISTS apps (
                appid     INTEGER PRIMARY KEY,
                name      TEXT,
                exists_on_store INTEGER,
                public_absent_at TEXT,
                free      INTEGER,
                year      INTEGER,
                genres    TEXT,
                detail_at TEXT,
                catalog   TEXT,
                catalog_at TEXT,
                reviews   TEXT,
                reviews_at TEXT
            );
            -- What is only true in one shop. One row per app per storefront,
            -- because these are three independent prices and not one price
            -- seen through three exchange rates.
            CREATE TABLE IF NOT EXISTS prices (
                appid    INTEGER NOT NULL,
                cc       TEXT    NOT NULL,
                price    INTEGER,
                initial  INTEGER,
                currency TEXT,
                discount INTEGER,
                price_at TEXT,
                PRIMARY KEY (appid, cc)
            );
            -- Descriptions, categories and requirements are localized. Keep
            -- each storefront language rather than letting the last reader
            -- overwrite the language seen by everybody else.
            CREATE TABLE IF NOT EXISTS catalogues (
                appid      INTEGER NOT NULL,
                lang       TEXT    NOT NULL,
                catalog    TEXT,
                catalog_at TEXT,
                PRIMARY KEY (appid, lang)
            );
            -- The complete, shallow list used only by autocomplete. Keeping
            -- it apart from `apps` is deliberate: every row in `apps` is a
            -- candidate for the slow storefront enrichment crawl. Putting
            -- a hundred thousand names there would turn one cheap list sync
            -- into a hundred thousand appdetails requests.
            CREATE TABLE IF NOT EXISTS search_apps (
                appid               INTEGER PRIMARY KEY,
                name                TEXT    NOT NULL,
                search_name         TEXT    NOT NULL,
                last_modified       INTEGER,
                price_change_number INTEGER,
                listed              INTEGER NOT NULL DEFAULT 1,
                synced_at           TEXT    NOT NULL
            );
            CREATE INDEX IF NOT EXISTS search_apps_listed_name
                ON search_apps (listed, name COLLATE NOCASE);
            -- One normalized token per game makes word-prefix lookup use an
            -- index. Scanning and normalizing all 180k names on every browser
            -- keypress is correct but needlessly slow.
            CREATE TABLE IF NOT EXISTS search_terms (
                appid     INTEGER NOT NULL,
                term      TEXT    NOT NULL,
                active    INTEGER NOT NULL DEFAULT 1,
                synced_at TEXT    NOT NULL,
                PRIMARY KEY (appid, term)
            );
            CREATE INDEX IF NOT EXISTS search_terms_active_prefix
                ON search_terms (active, term, appid);
        """)
        _migrate(con)
        con.execute("""
            INSERT OR IGNORE INTO catalogues (appid, lang, catalog, catalog_at)
            SELECT appid, 'english', catalog, catalog_at FROM apps
            WHERE catalog_at IS NOT NULL
        """)
    _ready = True


def _migrate(con):
    """Move the single-storefront prices into the per-storefront table.

    The old `apps` table carried one price, one currency and one timestamp,
    from whichever country STORE_COUNTRY named. Those readings are real and
    worth keeping, so they become that country's rows rather than being
    thrown away and re-fetched.

    The columns are dropped afterwards on purpose. Leaving them would leave a
    second, staler answer to "what does this cost" sitting one typo away from
    being read - which is exactly the shape of the two bugs this file already
    had."""
    cols = {r["name"] for r in con.execute("PRAGMA table_info(apps)")}
    if "name" not in cols:
        con.execute("ALTER TABLE apps ADD COLUMN name TEXT")
        cols.add("name")
    if "exists_on_store" not in cols:
        con.execute("ALTER TABLE apps ADD COLUMN exists_on_store INTEGER")
        cols.add("exists_on_store")
    if "public_absent_at" not in cols:
        con.execute("ALTER TABLE apps ADD COLUMN public_absent_at TEXT")
        cols.add("public_absent_at")
    for column in ("catalog", "catalog_at", "reviews", "reviews_at"):
        if column not in cols:
            con.execute(f"ALTER TABLE apps ADD COLUMN {column} TEXT")
            cols.add(column)
    if "price_at" not in cols:
        return
    con.execute("""
        INSERT OR IGNORE INTO prices (appid, cc, price, initial, currency, discount, price_at)
        SELECT appid, ?, price, initial, currency, discount, price_at
        FROM apps WHERE price_at IS NOT NULL
    """, (COUNTRY,))
    moved = con.execute("SELECT COUNT(*) FROM prices WHERE cc = ?", (COUNTRY,)).fetchone()[0]
    for column in ("price", "initial", "currency", "discount", "price_at"):
        if column in cols:
            con.execute(f"ALTER TABLE apps DROP COLUMN {column}")
    print(f"meta: {moved} preços movidos para a loja '{COUNTRY}'", flush=True)


def _now():
    return datetime.now(timezone.utc)


def _stamp():
    return _now().isoformat(timespec="seconds")


def _age(value):
    if not value:
        return None
    try:
        when = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        # Everything written here carries its offset. A naive stamp means the
        # row was edited by hand - which is exactly how the price states are
        # reached on purpose, since `datetime('now')` in SQLite has no offset.
        # Reading it as UTC is right in both cases, and beats the TypeError
        # that subtracting it used to raise.
        when = when.replace(tzinfo=timezone.utc)
    return _now() - when


def _catalogue_row(appid, language):
    """One localized catalogue document, without touching the storefront."""
    init()
    with _db_lock, _connect() as con:
        row = con.execute(
            "SELECT catalog, catalog_at FROM catalogues WHERE appid = ? AND lang = ?",
            (int(appid), language_of(language)),
        ).fetchone()
    # Always shadow the legacy English fields returned by lookup(). Without
    # these explicit nulls, a first Portuguese/Russian request would mistake
    # the old English catalogue for a localized cache hit and never fetch it.
    return ({"catalog": json.loads(row["catalog"]) if row["catalog"] else None,
             "catalog_at": row["catalog_at"]} if row else
            {"catalog": None, "catalog_at": None})


def _save_catalogue(appid, language, catalog, at):
    with _db_lock, _connect() as con:
        con.execute("""
            INSERT INTO catalogues (appid, lang, catalog, catalog_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(appid, lang) DO UPDATE SET
                catalog = excluded.catalog, catalog_at = excluded.catalog_at
        """, (int(appid), language_of(language), catalog, at))


def lookup(appids, cc=None):
    """What is already known about these apps, in one storefront. Never
    fetches; a miss is a miss.

    The caller decides what a partial answer is worth - for the money panel it
    is worth a lot, because the number is true for the games it covers and the
    page prints the coverage beside it."""
    init()
    cc = cc_of(cc)
    appids = list(appids)
    if not appids:
        return {}
    out = {}
    with _db_lock, _connect() as con:
        for start in range(0, len(appids), 400):
            chunk = appids[start:start + 400]
            marks = ",".join("?" * len(chunk))
            # Left join: an app whose detail pass has run but whose price in
            # this country has not is a real state, and the money panel counts
            # it as "still reading" rather than as free.
            rows = con.execute(f"""
                SELECT a.appid, a.name, a.exists_on_store, a.public_absent_at,
                       a.free, a.year, a.genres, a.detail_at,
                       a.catalog, a.catalog_at, a.reviews, a.reviews_at,
                       p.price, p.initial, p.currency, p.discount, p.price_at
                FROM apps a
                LEFT JOIN prices p ON p.appid = a.appid AND p.cc = ?
                WHERE a.appid IN ({marks})
            """, (cc, *chunk)).fetchall()
            for r in rows:
                out[r["appid"]] = {
                    "name": r["name"],
                    "exists": (bool(r["exists_on_store"])
                               if r["exists_on_store"] is not None else None),
                    "public_absent": bool((age := _age(r["public_absent_at"]))
                                          is not None and age.days < 30),
                    "price": r["price"],
                    "initial": r["initial"],
                    "currency": r["currency"],
                    "discount": r["discount"],
                    "free": bool(r["free"]) if r["free"] is not None else None,
                    "year": r["year"],
                    "genres": json.loads(r["genres"]) if r["genres"] else None,
                    "catalog": json.loads(r["catalog"]) if r["catalog"] else None,
                    "catalog_at": r["catalog_at"],
                    "reviews": json.loads(r["reviews"]) if r["reviews"] else None,
                    "reviews_at": r["reviews_at"],
                    "priced": bool(r["price_at"]),
                    # `name` became part of the detail contract after the
                    # existing catalogue had already been crawled.  A legacy
                    # timestamp therefore does not mean the new detail pass is
                    # complete until it has either learned a name or confirmed
                    # that the app is absent from the store.
                    "detailed": bool(r["detail_at"] and
                                     (r["name"] or r["exists_on_store"] == 0)),
                }
    return out


def want(appids, countries=None):
    """Queue whatever is missing or stale, in every storefront. Returns
    nothing; the worker gets to it when it gets to it, and the next lookup()
    sees more than the last one.

    Every country by default, because the money panel has to be able to total
    a whole library in the reader's own currency, and it can only do that if
    the crawl has been filling all three all along."""
    init()
    countries = tuple(cc_of(c) for c in countries) if countries else COUNTRIES
    appids = [int(a) for a in appids]
    if not appids:
        return

    detail_at, priced_at = {}, {}
    with _db_lock, _connect() as con:
        for start in range(0, len(appids), 400):
            chunk = appids[start:start + 400]
            marks = ",".join("?" * len(chunk))
            for r in con.execute(
                    f"SELECT appid, name, exists_on_store, detail_at, catalog_at "
                    f"FROM apps WHERE appid IN ({marks})", chunk):
                detail_at[r["appid"]] = (
                    r["catalog_at"] if r["catalog_at"] or r["exists_on_store"] == 0 else None)
            for r in con.execute(
                    f"SELECT appid, cc, price_at FROM prices WHERE appid IN ({marks})", chunk):
                priced_at[(r["appid"], r["cc"])] = r["price_at"]

    with _queue_lock:
        if len(_queued_price) + len(_queued_detail) >= MAX_QUEUE:
            return
        for appid in appids:
            due = (a := _age(detail_at.get(appid))) is None or a > CATALOG_TTL
            if due and appid not in _queued_detail:
                _queued_detail.add(appid)
                _detail_wanted.append(appid)
            for cc in countries:
                at = priced_at.get((appid, cc))
                if (a := _age(at)) is not None and a <= PRICE_TTL:
                    continue
                if (appid, cc) in _queued_price:
                    continue
                _queued_price.add((appid, cc))
                _price_wanted[cc].append(appid)


def want_media(appids):
    """Force a detail refresh for cached movies saved before stream URLs.

    This is a one-time schema evolution disguised as a queue operation: old
    catalogue JSON has the trailer id and thumbnail but not Steam's newer HLS
    and DASH fields. Once the refreshed row contains one, callers stop asking.
    No worker means a test or command-line read, where queueing cannot help."""
    if _worker is None:
        return
    with _queue_lock:
        for raw in appids:
            appid = int(raw)
            if appid in _queued_detail:
                # It may already be waiting at the back of the compatibility
                # crawl. Move that same job rather than queueing it twice.
                try:
                    _detail_wanted.remove(appid)
                except ValueError:
                    # The worker has already taken it; that in-flight refresh
                    # will write the media fields we need.
                    continue
            elif len(_queued_price) + len(_queued_detail) >= MAX_QUEUE:
                continue
            else:
                _queued_detail.add(appid)
            _urgent_detail.add(appid)
            _detail_wanted.appendleft(appid)


def mark_public_absent(appid):
    """Persist a negative confirmed by both the store and achievement schema."""
    init()
    _save(int(appid), public_absent_at=_stamp())


# ── One app, for one page that is open ───────────────────────────────
# lookup() and want() are for libraries: read what is there, queue the rest,
# nobody waits. This is the other shape. A game page names one app and has a
# reader in front of it, so it blocks - briefly - and then gives up and
# answers with what is on disk. The pattern is art.py's: one lock per appid,
# fetch on a miss, degrade rather than fail.


def _lock_for(appid, cc):
    with _locks_guard:
        lock = _locks.get((appid, cc))
        if lock is None:
            # Bounded by how many games have pages, which is bounded by the
            # libraries people look up. Clearing is safe: a thread holding one
            # keeps its own object, and the worst a collision can cost is one
            # redundant request.
            if len(_locks) > 4000:
                _locks.clear()
            lock = _locks[(appid, cc)] = threading.Lock()
        return lock


def _page_enter():
    global _page_jobs
    with _page_guard:
        _page_jobs += 1


def _page_leave():
    global _page_jobs
    with _page_guard:
        _page_jobs = max(0, _page_jobs - 1)


def _page_pending():
    with _page_guard:
        return _page_jobs > 0


def _pace(max_wait=None, background=False):
    """Hold the storefront to one request every INTERVAL, across both the crawl
    and the page path - it is one address and one tolerance.

    Returns False when the next slot is further off than the caller will wait,
    and the caller answers from disk instead of queueing behind it."""
    global _last_at
    deadline = None if max_wait is None else time.monotonic() + max_wait
    while True:
        # A catalogue crawl can wait; somebody with the page already open
        # cannot. Without this yield the worker repeatedly won the two-second
        # slot and a cold public page stayed incomplete until its queue turn.
        if background and _page_pending():
            time.sleep(0.05)
            continue
        with _pace_lock:
            now = time.monotonic()
            wait = _last_at + INTERVAL - now
            if wait <= 0:
                _last_at = now
                return True
        if deadline is not None and now + wait > deadline:
            return False
        time.sleep(min(wait, 0.25))


def _note_429():
    """One cooldown, set by whichever path took it and honoured by both."""
    global _cool_until
    with _pace_lock:
        _cool_until = time.monotonic() + BACKOFF_MIN


def _row(appid, cc):
    """One app in one storefront, plus when that price was read."""
    init()
    with _db_lock, _connect() as con:
        r = con.execute("""
            SELECT a.appid, a.name, a.exists_on_store, a.free, a.year, a.genres, a.detail_at,
                   p.price, p.initial, p.currency, p.discount, p.price_at
            FROM apps a
            LEFT JOIN prices p ON p.appid = a.appid AND p.cc = ?
            WHERE a.appid = ?
        """, (cc, int(appid))).fetchone()
    if r is None:
        return None
    return {
        "name": r["name"],
        "exists": (bool(r["exists_on_store"])
                   if r["exists_on_store"] is not None else None),
        "price": r["price"], "initial": r["initial"], "currency": r["currency"],
        "discount": r["discount"] or 0,
        "free": bool(r["free"]) if r["free"] is not None else None,
        "year": r["year"],
        "genres": json.loads(r["genres"]) if r["genres"] else None,
        "priced": bool(r["price_at"]),
        "detailed": bool(r["detail_at"] and
                         (r["name"] or r["exists_on_store"] == 0)),
        "price_at": r["price_at"],
    }


def _state(row):
    """The one place a row becomes a word. Everything that draws a price asks
    this instead of re-deriving it from four nullable columns and getting a
    different answer than the last thing that tried.

    The order is not arbitrary. `priced` is checked before anything, and the
    price before `free`, because the two passes land separately: there is a
    real window where the price has come back and the detail pass has not, and
    the honest answer in it is "still reading" rather than "not sold"."""
    if row is None or not row["priced"]:
        return "unknown"
    if row["price"] is not None:
        if row["price"] == 0:
            # A hundred percent off. Free right now and not free-to-play, which
            # is a different sentence and a much better one.
            return "free_now" if row["discount"] else "unsold"
        return "sale" if row["discount"] else "paid"
    if not row["detailed"]:
        return "unknown"
    if row["free"] is True:
        return "free"
    if row["free"] is False:
        # A store page exists and has no price in this country. That is either
        # delisted or simply not sold here, and the row cannot tell them apart -
        # so nothing downstream may say "delisted".
        return "unsold"
    return "absent"          # the storefront has no usable page for this app


def _due(row):
    age = _age(row["price_at"])
    if age is None:
        return True
    return age > (SALE_TTL if row["discount"] else PAGE_TTL)


def _shape(appid, cc, row, stale):
    """The answer, with the stale-sale rule applied.

    This is where "-70% is not a lie" is enforced, and it is enforced here
    rather than in the page so that no renderer is able to skip it."""
    state = _state(row)
    price = initial = currency = year = checked = None
    cut = 0
    if row is not None:
        price, initial, currency = row["price"], row["initial"], row["currency"]
        cut = row["discount"] or 0
        year, checked = row["year"], row["price_at"]
    if stale and state == "sale":
        # Keep the number, drop the claim. A price out of date is a fact with a
        # date on it; a discount out of date is a false advertisement.
        state, initial, cut = "paid", None, 0
    elif stale and state == "free_now":
        # Zero only means anything as "free right now". Without the claim it is
        # not a cheaper price, it is no information.
        state, price, initial, cut = "unknown", None, None, 0
    return {
        "appid": int(appid), "state": state,
        "price": price, "initial": initial, "discount": cut,
        "currency": currency, "country": cc,
        "year": year, "checked_at": checked, "stale": bool(stale),
    }


def fresh(appid, cc=None):
    """Whether price() would answer without leaving the process."""
    row = _row(appid, cc_of(cc))
    return row is not None and not _due(row)


def price(appid, cc=None):
    """What this one app costs right now, in one shop, for one page that is
    open.

    Only the reader's own storefront is fetched here - one request per page
    open, exactly as before the site learned three currencies. The other two
    are the crawl's job, and nobody is waiting on them.

    Never raises. A storefront that is down costs the page a paragraph, not an
    error: every path out of here returns a shape, and the worst of them says
    the price has not been read yet."""
    init()
    appid = int(appid)
    cc = cc_of(cc)
    row = _row(appid, cc)

    # The detail pass is what tells free apart from delisted, and it is one
    # request per app - not something to do while somebody waits. Queue it and
    # answer with what there is; the page asks again in a few seconds.
    if row is None or not row["detailed"]:
        want([appid])

    if row is not None and not _due(row):
        return _shape(appid, cc, row, stale=False)

    with _lock_for(appid, cc):
        row = _row(appid, cc)           # filled while this thread waited?
        if row is not None and not _due(row):
            return _shape(appid, cc, row, stale=False)
        if OFFLINE or time.monotonic() < _cool_until:
            return _shape(appid, cc, row, stale=True)
        if not _on_demand.acquire(blocking=False):
            return _shape(appid, cc, row, stale=True)
        _page_enter()
        try:
            if not _pace(max_wait=PAGE_WAIT):
                return _shape(appid, cc, row, stale=True)
            try:
                ok = _do_prices([appid], cc, timeout=PAGE_TIMEOUT)
            except urllib.error.HTTPError:
                _note_429()
                return _shape(appid, cc, row, stale=True)
        finally:
            _page_leave()
            _on_demand.release()
        return _shape(appid, cc, _row(appid, cc), stale=not ok)


def _get(params, timeout=TIMEOUT):
    """One GET at the storefront. Returns the parsed body, or None.

    The timeout is a parameter because the crawl and the page path want
    different ones: the crawl can afford twenty seconds, a page cannot."""
    if OFFLINE:
        return None
    url = f"{STORE}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept": "application/json", "Accept-Encoding": "identity",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise
        return None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None


def _get_url(url, timeout=TIMEOUT):
    """The other public storefront endpoint, under the same pace and policy."""
    if OFFLINE:
        return None
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept": "application/json", "Accept-Encoding": "identity",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise
        return None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None


def _plain(value):
    """Store HTML reduced to readable text before it reaches our public API."""
    if not isinstance(value, str):
        return None
    text = re.sub(r"<\s*(?:br|/p|/li)\s*/?>", "\n", value, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    lines = [" ".join(line.split()) for line in html.unescape(text).splitlines()]
    return "\n".join(line for line in lines if line).strip() or None


def _store_catalog(data):
    """The useful, stable part of appdetails, without executable store HTML."""
    def people(key):
        return [str(item).strip() for item in (data.get(key) or []) if str(item).strip()]

    def tagged(key):
        return [{"id": int(item["id"]), "name": (item.get("description") or "").strip()}
                for item in (data.get(key) or [])
                if str(item.get("id", "")).isdigit()]

    requirements = {}
    for platform, key in (("windows", "pc_requirements"),
                          ("macos", "mac_requirements"),
                          ("linux", "linux_requirements")):
        raw = data.get(key) if isinstance(data.get(key), dict) else {}
        minimum = _plain(raw.get("minimum"))
        recommended = _plain(raw.get("recommended"))
        if minimum or recommended:
            requirements[platform] = {"minimum": minimum, "recommended": recommended}

    languages = _plain(data.get("supported_languages")) or ""
    languages = [part.strip().strip("*").strip() for part in languages.replace("\n", ",").split(",")]
    languages = [part for part in languages if part and not part.lower().startswith("languages with")]

    screenshots = [{
        "id": item.get("id"), "thumbnail": item.get("path_thumbnail"),
        "full": item.get("path_full"),
    } for item in (data.get("screenshots") or []) if item.get("path_full")]
    movies = [{
        "id": item.get("id"), "name": item.get("name"),
        "thumbnail": item.get("thumbnail"), "highlight": bool(item.get("highlight")),
        "webm": item.get("webm") or {}, "mp4": item.get("mp4") or {},
        "dash_av1": item.get("dash_av1"), "dash_h264": item.get("dash_h264"),
        "hls_h264": item.get("hls_h264"),
    } for item in (data.get("movies") or []) if item.get("id")]
    groups = []
    for group in data.get("package_groups") or []:
        subs = []
        for sub in group.get("subs") or []:
            subs.append({
                "packageid": sub.get("packageid"),
                "option": _plain(sub.get("option_text")),
                "price_cents": sub.get("price_in_cents_with_discount"),
                "free_license": bool(sub.get("is_free_license")),
            })
        groups.append({"name": group.get("name"), "title": group.get("title"), "subs": subs})

    release = data.get("release_date") if isinstance(data.get("release_date"), dict) else {}
    support = data.get("support_info") if isinstance(data.get("support_info"), dict) else {}
    achievements = data.get("achievements") if isinstance(data.get("achievements"), dict) else {}
    recommendations = data.get("recommendations") if isinstance(data.get("recommendations"), dict) else {}
    metacritic = data.get("metacritic") if isinstance(data.get("metacritic"), dict) else {}
    age = re.search(r"\d+", str(data.get("required_age") or ""))
    return {
        "name": (data.get("name") or "").strip() or None,
        "type": data.get("type"),
        "required_age": int(age.group()) if age else None,
        "description": _plain(data.get("short_description")),
        "about": _plain(data.get("about_the_game")),
        "developers": people("developers"),
        "publishers": people("publishers"),
        "platforms": {key: bool(value) for key, value in (data.get("platforms") or {}).items()},
        "languages": languages,
        "categories": tagged("categories"),
        "genres": tagged("genres"),
        "release": {"coming_soon": bool(release.get("coming_soon")), "date": release.get("date")},
        "recommendations": recommendations.get("total"),
        "metacritic": ({"score": metacritic.get("score"), "url": metacritic.get("url")}
                       if metacritic else None),
        "achievements": {
            "total": achievements.get("total"),
            "highlighted": achievements.get("highlighted") or [],
        } if achievements else None,
        "dlc": [int(item) for item in (data.get("dlc") or []) if str(item).isdigit()],
        "packages": [int(item) for item in (data.get("packages") or []) if str(item).isdigit()],
        "package_groups": groups,
        "requirements": requirements,
        "screenshots": screenshots,
        "movies": movies,
        "images": {
            "header": data.get("header_image"), "capsule": data.get("capsule_image"),
            "background": data.get("background_raw") or data.get("background"),
        },
        "website": data.get("website"),
        "support": {"url": support.get("url"), "email": support.get("email")},
        "content_descriptors": data.get("content_descriptors") or None,
        "ratings": data.get("ratings") or None,
    }


def _save(appid, **fields):
    cols = ", ".join(f"{k} = ?" for k in fields)
    with _db_lock, _connect() as con:
        con.execute("INSERT OR IGNORE INTO apps (appid) VALUES (?)", (appid,))
        con.execute(f"UPDATE apps SET {cols} WHERE appid = ?",
                    (*fields.values(), appid))


# Every branch below writes every money column, and that is not tidiness. Two
# of them used to write only some, and both left a row that lied:
#
#   success:false wrote the timestamp and nothing else, so a delisted game kept
#   quoting its last known price - refreshed every fourteen days into looking
#   current, forever;
#
#   no price_overview cleared price and currency but left initial and discount,
#   so a game that went free after a sale read as "no price, fifty percent off".
#
# A row is either a price or it is not one. There is no half.
_CLEARED = {"price": None, "initial": None, "currency": None, "discount": 0}


def _save_price(appid, cc, **fields):
    cols = ", ".join(f"{k} = ?" for k in fields)
    with _db_lock, _connect() as con:
        con.execute("INSERT OR IGNORE INTO prices (appid, cc) VALUES (?, ?)", (appid, cc))
        con.execute(f"UPDATE prices SET {cols} WHERE appid = ? AND cc = ?",
                    (*fields.values(), appid, cc))
        # A price is the first thing learned about an app, and the detail pass
        # needs a row of its own to land in later.
        con.execute("INSERT OR IGNORE INTO apps (appid) VALUES (?)", (appid,))


def _do_prices(batch, cc, timeout=TIMEOUT):
    body = _get({"appids": ",".join(str(a) for a in batch), "cc": cc,
                 "filters": "price_overview"}, timeout=timeout)
    if body is None:
        return False
    for appid in batch:
        entry = (body or {}).get(str(appid)) or {}
        if not entry.get("success"):
            # Gone from the store. That is an answer, and writing the timestamp
            # is what stops it being asked again on every lookup - but the old
            # price has to go with it.
            _save_price(appid, cc, **_CLEARED, price_at=_stamp())
            continue
        # `data` is an empty list rather than an object when there is no price:
        # the app is free, or it is no longer sold. Which of the two it is comes
        # from the detail pass, not from here.
        data = entry.get("data")
        price = (data or {}).get("price_overview") if isinstance(data, dict) else None
        if not price:
            _save_price(appid, cc, **_CLEARED, price_at=_stamp())
            continue
        final = price.get("final")
        initial = price.get("initial")
        cut = price.get("discount_percent") or 0
        # Two invariants the storefront does not always hold to. A prepurchase
        # comes back with a discount and initial == final; enforcing it here
        # means no page downstream has to know that.
        if final is None or initial is None or initial <= final:
            initial, cut = final, 0
        _save_price(appid, cc, price=final, initial=initial,
                    currency=price.get("currency"), discount=cut, price_at=_stamp())
    return True


def _do_detail(appid, timeout=TIMEOUT, language="english"):
    # No filter on purpose. appdetails already costs one request, and its full
    # response is the catalogue we would otherwise spend years rebuilding one
    # tiny field at a time.
    language = language_of(language)
    body = _get({"appids": appid, "cc": COUNTRY, "l": language}, timeout=timeout)
    if body is None:
        return False
    entry = (body or {}).get(str(appid)) or {}
    data = entry.get("data") if entry.get("success") else None
    if not isinstance(data, dict):
        at = _stamp()
        fields = {"name": None, "exists_on_store": 0, "free": None,
                  "year": None}
        if language == "english":
            fields.update(genres=None, catalog=None, catalog_at=at, detail_at=at)
        _save(appid, **fields)
        _save_catalogue(appid, language, None, at)
        return True

    # Genre ids are stable numbers; the English description travels with them
    # only as the fallback for an id the dictionary has never seen. The browser
    # translates the rest, the same way it does everything else the API sends.
    genres = [{"id": int(g["id"]), "name": g.get("description") or ""}
              for g in (data.get("genres") or []) if str(g.get("id", "")).isdigit()]
    year = None
    date = ((data.get("release_date") or {}).get("date") or "")
    for token in date.replace(",", " ").split():
        if len(token) == 4 and token.isdigit() and 1970 < int(token) < 2100:
            year = int(token)
            break
    at = _stamp()
    catalog = json.dumps(_store_catalog(data), ensure_ascii=False, separators=(",", ":"))
    fields = {
        "exists_on_store": 1,
        "free": 1 if data.get("is_free") else 0,
        "year": year,
    }
    if language == "english":
        fields.update(
            name=(data.get("name") or "").strip() or None,
            genres=json.dumps(genres, ensure_ascii=False) if genres else None,
            catalog=catalog, catalog_at=at, detail_at=at,
        )
    _save(appid, **fields)
    _save_catalogue(appid, language, catalog, at)
    # Who published and who made it, handed to the company index. Free: both
    # strings are already in the answer this call had to make anyway, and the
    # index only reaches half the catalogue on its own - so a game somebody
    # opened is a game whose studio should stop being invisible, without
    # anyone waiting for a weekly walk to maybe get there.
    #
    # Imported here rather than at the top: houses reads this module for the
    # catalogue snapshot, and two modules importing each other at load time is
    # a cycle that only shows up in whichever one is imported first.
    try:
        import houses
        houses.learn(appid, (data.get("name") or "").strip(),
                     data.get("publishers"), data.get("developers"))
    except Exception:  # noqa: BLE001 - the store cache is not the index's keeper
        pass
    return True


def _do_reviews(appid, timeout=TIMEOUT):
    overall_query = urllib.parse.urlencode({
        "json": 1, "language": "all", "purchase_type": "all", "num_per_page": 0,
    })
    body = _get_url(f"{REVIEWS}/{int(appid)}?{overall_query}", timeout=timeout)
    summary = (body or {}).get("query_summary")
    if not isinstance(summary, dict):
        return False
    review = {
        "score": summary.get("review_score"),
        "description": summary.get("review_score_desc"),
        "positive": summary.get("total_positive"),
        "negative": summary.get("total_negative"),
        "total": summary.get("total_reviews"),
    }
    # Steam's documented `day_range` filter gives the panel a useful trend
    # instead of repeating the lifetime score already printed by the store.
    # It is deliberately best-effort: a failure here must not discard the
    # lifetime summary that was fetched successfully above.
    recent_query = urllib.parse.urlencode({
        "json": 1, "filter": "all", "day_range": 30, "language": "all",
        "purchase_type": "all", "num_per_page": 0,
    })
    recent = None
    try:
        # This is a second storefront request, so it takes a second paced slot
        # instead of riding immediately behind the lifetime query above.
        if _pace(max_wait=PAGE_WAIT):
            recent_body = _get_url(
                f"{REVIEWS}/{int(appid)}?{recent_query}", timeout=timeout)
            recent = (recent_body or {}).get("query_summary")
    except urllib.error.HTTPError:
        # Preserve the lifetime result, but honour Steam's cooldown globally.
        _note_429()
    if isinstance(recent, dict):
        review["recent"] = {
            "days": 30,
            "score": recent.get("review_score"),
            "description": recent.get("review_score_desc"),
            "positive": recent.get("total_positive"),
            "negative": recent.get("total_negative"),
            "total": recent.get("total_reviews"),
        }
    _save(appid, reviews=json.dumps(review, separators=(",", ":")), reviews_at=_stamp())
    return True


def public_catalog(appid, cc=None, language="english"):
    """Rich storefront facts for one open public page, persisted between runs.

    Catalogue and review summary have independent clocks. A miss may briefly
    use the shared storefront slot; a hit is one SQLite read.
    """
    appid = int(appid)
    cc = cc_of(cc)
    language = language_of(language)

    def due(row, field, ttl):
        return not row or (age := _age(row.get(field))) is None or age > ttl

    def read():
        row = lookup([appid], cc).get(appid) or {}
        row.update(_catalogue_row(appid, language))
        return row

    row = read()
    if not due(row, "catalog_at", CATALOG_TTL) and not due(row, "reviews_at", REVIEWS_TTL):
        return row
    with _lock_for(appid, f"catalog:{language}"):
        row = read()
        need_catalog = due(row, "catalog_at", CATALOG_TTL)
        need_reviews = due(row, "reviews_at", REVIEWS_TTL)
        if OFFLINE or time.monotonic() < _cool_until or not (need_catalog or need_reviews):
            return row or {}
        if not _on_demand.acquire(blocking=False):
            return row or {}
        _page_enter()
        try:
            if need_catalog:
                if not _pace(max_wait=PAGE_WAIT):
                    need_reviews = False
                else:
                    try:
                        _do_detail(appid, timeout=PAGE_TIMEOUT, language=language)
                    except urllib.error.HTTPError:
                        _note_429()
                        need_reviews = False
            current = lookup([appid], cc).get(appid) or {}
            if need_reviews and current.get("exists") is not False:
                if _pace(max_wait=PAGE_WAIT):
                    try:
                        _do_reviews(appid, timeout=PAGE_TIMEOUT)
                    except urllib.error.HTTPError:
                        _note_429()
        finally:
            _page_leave()
            _on_demand.release()
    return read()


def _next_job():
    """Prices first, and one storefront at a time, because a request carries
    one `cc`. Countries are drained in order rather than round-robin: a whole
    library in one currency is more useful than a third of it in each."""
    with _queue_lock:
        # A visitor is waiting for this trailer. It is still rate-limited by
        # the shared storefront pace, but it must not sit behind thousands of
        # background price reads before obtaining its stream manifest.
        if _detail_wanted and _detail_wanted[0] in _urgent_detail:
            return ("detail", None, _detail_wanted.popleft())
        for cc in COUNTRIES:
            q = _price_wanted[cc]
            if q:
                batch = [q.popleft() for _ in range(min(BATCH, len(q)))]
                return ("price", cc, batch)
        if _detail_wanted:
            return ("detail", None, _detail_wanted.popleft())
    return (None, None, None)


def _done_price(appids, cc):
    with _queue_lock:
        pending = set(_price_wanted[cc])
        for appid in appids:
            if appid not in pending:
                _queued_price.discard((appid, cc))


def _done_detail(appid):
    with _queue_lock:
        if appid not in _detail_wanted:
            _queued_detail.discard(appid)
            _urgent_detail.discard(appid)


def _run():
    global _backoff, _fetched
    while True:
        kind, cc, job = _next_job()
        if kind is None:
            time.sleep(2)
            continue
        # A page open that took a 429 shut the storefront down for everybody,
        # this thread included. Ignoring that here would spend the cooldown
        # earning a second one.
        cool = _cool_until - time.monotonic()
        if cool > 0:
            time.sleep(min(cool, BACKOFF_MAX))
        # The pace is shared, so the crawl waits its turn behind a page the same
        # way a page waits behind the crawl. Nobody is reading this thread's
        # output, so it waits as long as it has to.
        _pace(background=True)
        try:
            ok = _do_prices(job, cc) if kind == "price" else _do_detail(job)
            _backoff = BACKOFF_MIN
        except urllib.error.HTTPError:
            # Too fast. Put the work back and stop asking for a while.
            with _queue_lock:
                if kind == "price":
                    _price_wanted[cc].extendleft(reversed(job))
                else:
                    _detail_wanted.appendleft(job)
            _note_429()
            time.sleep(_backoff)
            _backoff = min(_backoff * 2, BACKOFF_MAX)
            continue
        if ok:
            _fetched += 1
            if kind == "price":
                _done_price(job, cc)
            else:
                _done_detail(job)


def start():
    """One worker, started once. Daemon: it holds nothing that must be flushed."""
    global _worker
    init()
    if _worker is None:
        # Compatibility/enrichment crawl. Queue detail only: using want() here
        # would also enqueue every historical app in every storefront.
        with _db_lock, _connect() as con:
            missing_catalog = [r[0] for r in con.execute(
                "SELECT appid FROM apps "
                "WHERE (name IS NULL OR catalog IS NULL) "
                "AND exists_on_store IS NOT 0 "
                "ORDER BY appid LIMIT ?", (MAX_QUEUE,))]
        with _queue_lock:
            for appid in missing_catalog:
                if appid not in _queued_detail:
                    _queued_detail.add(appid)
                    _detail_wanted.append(appid)
        _worker = threading.Thread(target=_run, name="meta", daemon=True)
        _worker.start()
    return _worker


def _app_list_page(api_key, last_appid=0):
    """One official Steam game-list page. The key never leaves this request."""
    params = {
        "key": api_key,
        "max_results": 50000,
        "include_games": "true",
        "include_dlc": "false",
        "include_software": "false",
        "include_videos": "false",
        "include_hardware": "false",
    }
    if last_appid:
        params["last_appid"] = int(last_appid)
    request = urllib.request.Request(
        f"{APP_LIST}?{urllib.parse.urlencode(params)}",
        headers={"User-Agent": UA},
    )
    with urllib.request.urlopen(request, timeout=90) as response:
        body = json.load(response)
    page = body.get("response") if isinstance(body, dict) else None
    if not isinstance(page, dict) or not isinstance(page.get("apps"), list):
        raise ValueError("Steam returned an invalid app-list page")
    return page


def sync_search_catalog(api_key, page_getter=None):
    """Atomically refresh the complete public-game autocomplete catalogue.

    All pages are fetched before SQLite is touched. A timeout or malformed
    continuation therefore leaves the last good snapshot serving searches.
    Rows no longer returned by Steam are marked unlisted rather than deleted,
    which preserves the audit trail and makes a mistaken partial response
    reversible on the next successful sync.
    """
    api_key = (api_key or "").strip()
    if not api_key:
        raise ValueError("STEAM_API_KEY is required")
    getter = page_getter or (lambda cursor: _app_list_page(api_key, cursor))
    games = {}
    cursor = 0
    pages = 0
    while True:
        page = getter(cursor)
        pages += 1
        for item in page.get("apps", []):
            if not isinstance(item, dict):
                continue
            try:
                appid = int(item.get("appid"))
            except (TypeError, ValueError):
                continue
            name = " ".join(str(item.get("name") or "").split())
            if not (0 < appid <= 99999999 and name):
                continue
            games[appid] = (
                appid, name, " ".join(_search_words(name)),
                item.get("last_modified"), item.get("price_change_number"),
            )
        if not page.get("have_more_results"):
            break
        next_cursor = page.get("last_appid")
        try:
            next_cursor = int(next_cursor)
        except (TypeError, ValueError):
            raise ValueError("Steam omitted the app-list continuation") from None
        if next_cursor <= cursor:
            raise ValueError("Steam returned a non-progressing app-list continuation")
        cursor = next_cursor

    if not games:
        raise ValueError("Steam returned an empty game catalogue")
    # This value is also the snapshot generation. Seconds are not enough: two
    # consecutive syncs inside one second would otherwise make removed rows
    # look as though the second run had seen them.
    synced_at = _now().isoformat(timespec="microseconds")
    init()
    rows = [(*row, synced_at) for row in games.values()]
    with _db_lock, _connect() as con:
        con.executemany("""
            INSERT INTO search_apps
                (appid, name, search_name, last_modified,
                 price_change_number, listed, synced_at)
            VALUES (?, ?, ?, ?, ?, 1, ?)
            ON CONFLICT(appid) DO UPDATE SET
                name = excluded.name,
                search_name = excluded.search_name,
                last_modified = excluded.last_modified,
                price_change_number = excluded.price_change_number,
                listed = 1,
                synced_at = excluded.synced_at
        """, rows)
        con.executemany("""
            INSERT INTO search_terms (appid, term, active, synced_at)
            VALUES (?, ?, 1, ?)
            ON CONFLICT(appid, term) DO UPDATE SET
                active = 1,
                synced_at = excluded.synced_at
        """, ((appid, term, synced_at)
              for appid, name, _, _, _ in games.values()
              for term in set(_search_words(name))))
        con.execute(
            "UPDATE search_apps SET listed = 0 WHERE synced_at <> ?",
            (synced_at,),
        )
        con.execute(
            "UPDATE search_terms SET active = 0 WHERE synced_at <> ?",
            (synced_at,),
        )
    return {"games": len(games), "pages": pages, "synced_at": synced_at}


def catalogue_appids(after=0, limit=5000):
    """Appids from the catalogue snapshot, ascending, for a walk that resumes.

    The snapshot is every public game Steam lists, which is what makes it the
    right side to walk when the question is "what is missing": anything asking
    that has to start from the whole shop rather than from the part of it this
    site happens to have read.

    Ascending and after a cursor rather than paged by offset, so a walk that
    stops halfway through and comes back an hour later carries on instead of
    counting from the beginning into a table that has moved underneath it."""
    init()
    with _db_lock, _connect() as con:
        return [r[0] for r in con.execute(
            "SELECT DISTINCT appid FROM search_terms "
            "WHERE active = 1 AND appid > ? ORDER BY appid LIMIT ?",
            (int(after), max(1, min(50000, int(limit)))))]


def _search_snapshot(con, term, numeric, wanted):
    """Search the large shallow catalogue without normalizing it per request."""
    if numeric >= 0:
        row = con.execute(
            "SELECT appid, name FROM search_apps WHERE listed = 1 AND appid = ?",
            (numeric,),
        ).fetchone()
        return [row] if row else []

    words = _search_words(term)
    if words:
        joins, params = [], []
        for index, word in enumerate(words):
            alias = f"t{index}"
            if len(word) >= 2:
                joins.append(
                    f"JOIN search_terms {alias} ON {alias}.appid = s.appid "
                    f"AND {alias}.active = 1 AND {alias}.term >= ? "
                    f"AND {alias}.term < ?"
                )
                params.extend((word, f"{word}\uffff"))
            else:
                joins.append(
                    f"JOIN search_terms {alias} ON {alias}.appid = s.appid "
                    f"AND {alias}.active = 1 AND {alias}.term = ?"
                )
                params.append(word)
        normalized = " ".join(words)
        rows = con.execute(f"""
            SELECT DISTINCT s.appid, s.name FROM search_apps s
            {' '.join(joins)}
            WHERE s.listed = 1
            ORDER BY s.search_name = ? DESC, s.search_name LIKE ? DESC,
                     length(s.search_name), length(s.name), s.name COLLATE NOCASE
            LIMIT ?
        """, (*params, normalized, f"{normalized}%", wanted)).fetchall()
        # Word-prefix matches are higher quality than arbitrary substrings.
        # If there are any, do not pad them with unrelated titles merely to
        # fill the autocomplete box.
        if rows:
            return list(rows)

    # Preserve useful mid-word lookup (`craft` -> Minecraft) as the fallback.
    # It scans names only when the indexed word-prefix pass found nothing.
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    like = f"%{escaped}%"
    prefix = f"{escaped}%"
    rows = con.execute("""
        SELECT appid, name FROM search_apps
        WHERE listed = 1
          AND (name LIKE ? ESCAPE '\\' COLLATE NOCASE OR appid = ?)
        ORDER BY appid = ? DESC,
                 name LIKE ? ESCAPE '\\' COLLATE NOCASE DESC,
                 length(name), name COLLATE NOCASE
        LIMIT ?
    """, (like, numeric, numeric, prefix, wanted)).fetchall()
    return list(rows)


def search_games(query, limit=8, fallback_names=None):
    """Public Steam games, ordered like an autocomplete.

    This never reaches Steam: the complete shallow snapshot is refreshed by
    the explicit `sync-search` command, while names learned from real profile
    and page visits remain a fallback for delisted or legacy games.
    """
    init()
    term = " ".join((query or "").split())[:80]
    if len(term) < 2:
        return []
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    like = f"%{escaped}%"
    prefix = f"{escaped}%"
    numeric = int(term) if term.isdigit() and len(term) <= 8 else -1
    wanted = max(1, min(int(limit), 12))
    with _db_lock, _connect() as con:
        rows = _search_snapshot(con, term, numeric, wanted)
        seen = {row["appid"] for row in rows}
        remaining = wanted - len(rows)
        if remaining:
            marks = ",".join("?" for _ in seen)
            exclude = f" AND appid NOT IN ({marks})" if seen else ""
            local = con.execute(f"""
            SELECT appid, name FROM apps
            WHERE name IS NOT NULL
              AND exists_on_store IS NOT 0
              AND (name LIKE ? ESCAPE '\\' COLLATE NOCASE OR appid = ?)
              {exclude}
            ORDER BY appid = ? DESC,
                     name LIKE ? ESCAPE '\\' COLLATE NOCASE DESC,
                     length(name), name COLLATE NOCASE
            LIMIT ?
            """, (like, numeric, *seen, numeric, prefix, remaining)).fetchall()
            rows = [*rows, *local]

        # The local fallback is small (only visited/owned games), so its
        # normalization may stay in Python. The full snapshot above stores its
        # normalized form once during sync instead of rebuilding ~100k names
        # for every keypress.
        if len(rows) < wanted and not term.isdigit():
            seen = {row["appid"] for row in rows}
            query_words = _search_words(term)
            if query_words:
                candidates = con.execute("""
                    SELECT appid, name FROM apps
                    WHERE name IS NOT NULL AND exists_on_store IS NOT 0
                """).fetchall()
                inferred = []
                for row in candidates:
                    if row["appid"] in seen:
                        continue
                    words = _search_words(row["name"])
                    if all(any(word == candidate or
                               (len(word) >= 2 and candidate.startswith(word))
                               for candidate in words)
                           for word in query_words):
                        inferred.append(row)
                inferred.sort(key=lambda row: (
                    len(_search_words(row["name"])),
                    len(row["name"]), row["name"].casefold()))
                rows = [*rows, *inferred[:wanted - len(rows)]]
    out = [{"appid": row["appid"], "name": row["name"]} for row in rows]
    seen = {row["appid"] for row in out}
    query_words = _search_words(term)
    for appid, name in (fallback_names or {}).items():
        if len(out) >= wanted or appid in seen:
            continue
        words = _search_words(name)
        if (numeric == appid or
                (query_words and all(any(word == candidate or
                                         (len(word) >= 2 and candidate.startswith(word))
                                     for candidate in words)
                                     for word in query_words))):
            out.append({"appid": appid, "name": name})
    return out


def _search_words(value):
    """Words suitable for human game-name lookup, ignoring store symbols."""
    # A dotted title is an acronym, not four one-letter search terms. This
    # makes REPO, R.E and R.E.P.O. converge on the same searchable word (and
    # does the same useful thing for titles such as S.T.A.L.K.E.R.).
    value = re.sub(
        r"(?<!\w)(?:[a-z0-9]\s*\.\s*)+[a-z0-9](?:\s*\.)?",
        lambda match: re.sub(r"[^a-z0-9]", "", match.group(0), flags=re.I),
        value or "", flags=re.I,
    )
    # Remove symbols before NFKD: otherwise ™ expands to the letters "TM" and
    # glues itself to the preceding word ("FC™" -> "fctm").
    plain = "".join(" " if unicodedata.category(c).startswith("S") else c
                    for c in value)
    plain = unicodedata.normalize("NFKD", plain).casefold()
    plain = "".join(c for c in plain if not unicodedata.combining(c))
    return re.findall(r"[a-z0-9]+", plain)


def stats():
    """How much has been learned, for /healthz. Priced is per storefront,
    because "how far along is the crawl" now has three answers and an average
    of them would hide the one that is behind."""
    try:
        with _db_lock, _connect() as con:
            row = con.execute(
                "SELECT COUNT(*) AS n, SUM(detail_at IS NOT NULL) AS detailed,"
                " SUM(catalog_at IS NOT NULL) AS catalogued,"
                " SUM(reviews_at IS NOT NULL) AS reviewed"
                " FROM apps").fetchone()
            priced = {cc: 0 for cc in COUNTRIES}
            for r in con.execute(
                    "SELECT cc, COUNT(*) AS n FROM prices"
                    " WHERE price_at IS NOT NULL GROUP BY cc"):
                priced[r["cc"]] = r["n"]
            search = con.execute(
                "SELECT SUM(listed = 1) AS n, MAX(synced_at) AS synced_at "
                "FROM search_apps"
            ).fetchone()
    except sqlite3.Error:
        return {"apps": 0, "priced": {}, "detailed": 0, "catalogued": 0,
                "reviewed": 0, "queued": 0, "search_games": 0,
                "search_synced_at": None}
    with _queue_lock:
        queued = len(_queued_price) + len(_queued_detail)
    return {"apps": row["n"] or 0, "priced": priced,
            "detailed": row["detailed"] or 0, "queued": queued,
            "catalogued": row["catalogued"] or 0, "reviewed": row["reviewed"] or 0,
            "search_games": search["n"] or 0,
            "search_synced_at": search["synced_at"],
            "requests": _fetched}


def _main(argv):
    if argv == ["sync-search"]:
        try:
            result = sync_search_catalog(os.environ.get("STEAM_API_KEY"))
        except Exception as exc:
            # urllib exceptions include the requested URL, which includes the
            # key. Report only the exception class and safe explicit messages.
            message = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
            print(f"search catalogue sync failed: {message}", file=sys.stderr)
            return 1
        print(f"search catalogue: {result['games']} games in {result['pages']} page(s), "
              f"synced {result['synced_at']}")
        return 0
    if argv == ["search-status"]:
        state = stats()
        print(f"search catalogue: {state['search_games']} games, "
              f"synced {state['search_synced_at'] or 'never'}")
        return 0
    print("usage: python meta.py {sync-search|search-status}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
