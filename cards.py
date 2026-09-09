#!/usr/bin/env python3
"""steamprofiler.org - the trading cards of a game, kept on disk.

meta.py reads the storefront and answers "what does this game cost". This reads
the Community Market and answers the other question a game has an economy for:
what its card set is, and what one of each card costs today. Same shape of fact
as a price - it belongs to the game and not to whoever is looking at it - so it
is cached the same way, in SQLite, and one fetch of Dota 2 serves everybody who
ever owned it.

**Where the numbers come from.** There is no API for this. The set and its
prices come from the Community Market's own search, `/market/search/render`,
which answers for one app at a time:

    appid=753                       the Steam Community app, where cards live
    category_753_Game[]=tag_app_X   the game whose cards these are
    category_753_item_class[]=tag_item_class_2      trading cards, not emoticons
    category_753_cardborder[]=tag_cardborder_0      the normal ones, not foils

No key, no documented quota, and a stricter tolerance than the storefront -
which is why the pace here is slower than meta.py's and the cooldown after a
429 is longer. Nobody is ever made to wait on it: a page asks, gets what is on
disk, and the fetch fills the row in behind it.

**Only the normal cards.** A badge needs one of each card in the set, and that
is what the cost on the page is. Foils are a second collection with their own
prices, and including them doubles every request for a figure that answers a
different question. The border filter is therefore part of the query rather
than something filtered out afterwards: what is not asked for is not paged
through.

**Prices are in dollars, and the page says so.** The market's search ignores
`currency` - measured: the same request with `currency=7` comes back byte for
byte identical, and `country=BR` only changes the label to "$0.04 USD" - so the
only way to ask in reais is `/market/priceoverview` once per card, which turns
one request per game into one per card per currency. This therefore keeps the
cents exactly as they arrive and labels them USD.

Beside that dollar the answer carries fx.py's rate for the currencies the site
reads in, so a page can print an approximation next to it. The approximation is
never the whole answer: the cents stay the figure of record, because a card at
$0.05 was listed at R$0,22, R$0,24 and R$0,26 on three different games in the
same minute, and no single rate reproduces that. fx.py's docstring has the
measurements and the reason.

**What a row means.** `count = 0` is not a failure: it is the market saying
this app has no cards, which is true of most of Steam and worth remembering so
it is never asked twice. A row with cards keeps its names and pictures forever
- a set does not change - and it is the prices on it that go stale.

Stdlib only, like everything else in the api container.
"""

import json
import os
import sqlite3
import threading
import time
import urllib.parse
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import community
import fx

DATA_DIR = Path(os.environ.get("DATA_DIR", "/app/data"))
# The same file meta.py writes: one cache of "facts about a game", two tables
# in it, one backup to take. blog.py sits beside store.py for the same reason.
DB_PATH = DATA_DIR / "meta.db"
MARKET = "https://steamcommunity.com/market/search/render/"
# Where an item's picture lives. The market hands over a token, not a URL.
ECONOMY = "https://community.fastly.steamstatic.com/economy/image"
ICON_SIZE = "96fx96f"
# Steam Community's own appid. Every card of every game is an item inside it.
COMMUNITY_APP = 753
# The store category that says a game drops cards at all, which is how a
# library is filtered down to the games worth asking the market about.
CARDS_CATEGORY = 29

# The pace, the cooldown and the 429 all live in community.py now: the market
# is one of three readers of steamcommunity.com and they share one budget. See
# that module's docstring for why a limiter of our own would be worse than no
# limiter at all.
INTERVAL = community.INTERVAL
BACKOFF_MIN = community.BACKOFF_MIN
BACKOFF_MAX = community.BACKOFF_MAX
# The market pages ten at a time when it feels like it. Three pages covers the
# largest normal set Valve ships; a fourth would be paging for nothing.
PAGE = 100
MAX_PAGES = 3
# Prices move daily, so a set read yesterday is worth reading again. The names
# and the pictures in the row are not what expires - see the docstring.
PRICE_TTL = timedelta(hours=12)
# A game with no cards is a permanent fact in every way that matters. It is
# still re-checked eventually, because Valve does add card sets to old games.
NONE_TTL = timedelta(days=30)
# A page open waits this long for the market before answering off disk.
PAGE_WAIT = INTERVAL + 0.5
PAGE_TIMEOUT = 8
# At most one on-demand fetch in flight. The crawl already uses most of what
# the market tolerates, and a burst of page opens must not add to it.
ON_DEMAND = 1
MAX_QUEUE = 5000
# MARKET_OFFLINE=1 makes every fetch fail at once, the way STORE_OFFLINE does
# for meta.py: it is how the degraded states are reached on purpose.
OFFLINE = os.environ.get("MARKET_OFFLINE") == "1"

# Raised by community.fetch on a 429. Re-exported under the old name because
# it is caught by name in two places below and it is the same exception.
Throttled = community.Throttled


_db_lock = threading.Lock()
_queue_lock = threading.Lock()
_wanted = deque()
_queued = set()
_worker = None
_backoff = BACKOFF_MIN

_locks_guard = threading.Lock()
_locks = {}
_on_demand = threading.BoundedSemaphore(ON_DEMAND)
_ready = False


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
    """Idempotent, and called from every entry point rather than only from
    start(): the command line reads this cache too, where no worker runs."""
    global _ready
    if _ready:
        return
    with _db_lock, _connect() as con:
        con.executescript("""
            -- One row per app the market has been asked about. `count = 0`
            -- means asked and answered "no cards", which is an answer worth
            -- keeping and not a hole.
            CREATE TABLE IF NOT EXISTS card_sets (
                appid   INTEGER PRIMARY KEY,
                cards   TEXT,
                count   INTEGER,
                cost    INTEGER,
                seen_at TEXT
            );
        """)
    _ready = True


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
        when = when.replace(tzinfo=timezone.utc)
    return _now() - when


# ── The market ───────────────────────────────────────

def _lock_for(appid):
    with _locks_guard:
        lock = _locks.get(appid)
        if lock is None:
            if len(_locks) > 4000:
                _locks.clear()
            lock = _locks[appid] = threading.Lock()
        return lock


def _query(appid, start):
    return MARKET + "?" + urllib.parse.urlencode({
        "norender": 1,
        "appid": COMMUNITY_APP,
        "start": start,
        "count": PAGE,
        "sort_column": "name",
        "sort_dir": "asc",
        "category_753_Game[]": f"tag_app_{int(appid)}",
        "category_753_item_class[]": "tag_item_class_2",
        "category_753_cardborder[]": "tag_cardborder_0",
    })


def _get(url):
    """One market page, or None.

    The transport and the bot check it has to get past are in community.py,
    because inv.py needs the same ones and the host counts both against one
    budget. What stays here is the offline switch, which is this module's own:
    MARKET_OFFLINE reaches the degraded card panel on purpose without reaching
    anything else.

    Kept as a named function rather than calling community directly at the two
    call sites, because it is the seam tests/test_cards.py replaces to answer
    without a network."""
    if OFFLINE:
        return None
    return community.get_json(url)


def _name_of(row):
    """The card's own name. The market calls it "Tiny (Trading Card)", because
    that is the item; the set is read as a list of characters, so the part that
    is the same on all of them comes off."""
    name = (row.get("name") or "").strip()
    if name.endswith(")") and "(" in name:
        head = name[:name.rfind("(")].strip()
        if head:
            return head
    return name


def _card(row):
    asset = row.get("asset_description") or {}
    icon = asset.get("icon_url")
    price = row.get("sell_price")
    return {
        "name": _name_of(row),
        "hash": row.get("hash_name") or asset.get("market_hash_name"),
        # Cents, exactly as the market quotes them to a buyer. None when the
        # card exists and nobody is selling one, which happens on dead games
        # and is the reason the set cost can be incomplete.
        "cents": int(price) if isinstance(price, int) and price > 0 else None,
        "listings": row.get("sell_listings"),
        "icon": f"{ECONOMY}/{icon}/{ICON_SIZE}" if icon else None,
    }


def _do_set(appid):
    """Fetch one game's card set and write the row. Returns True when the
    market answered, False when it did not - a False leaves the old row alone,
    because yesterday's prices beat an empty panel."""
    cards = []
    total = None
    for page in range(MAX_PAGES):
        if page:
            community.reserve()
        data = _get(_query(appid, len(cards)))
        if data is None or not data.get("success"):
            return False
        total = int(data.get("total_count") or 0)
        rows = data.get("results") or []
        cards.extend(_card(row) for row in rows if row.get("name"))
        if not rows or len(cards) >= total:
            break

    # Deduplicated on the hash rather than trusted: paging a list the market is
    # re-sorting under us is the one way the same card arrives twice, and a set
    # with two Tinys in it would price a badge nobody has to buy.
    seen, unique = set(), []
    for card in cards:
        if card["hash"] in seen:
            continue
        seen.add(card["hash"])
        unique.append(card)

    priced = [c["cents"] for c in unique if c["cents"] is not None]
    cost = sum(priced) if len(priced) == len(unique) and unique else None
    with _db_lock, _connect() as con:
        con.execute(
            "INSERT INTO card_sets (appid, cards, count, cost, seen_at)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(appid) DO UPDATE SET"
            " cards = excluded.cards, count = excluded.count,"
            " cost = excluded.cost, seen_at = excluded.seen_at",
            (int(appid),
             json.dumps(unique, ensure_ascii=False, separators=(",", ":")) if unique else None,
             len(unique), cost, _stamp()))
    return True


# ── Reading ──────────────────────────────────────────────────────────

def _row(appid):
    init()
    with _db_lock, _connect() as con:
        return con.execute("SELECT * FROM card_sets WHERE appid = ?",
                           (int(appid),)).fetchone()


def _shape(appid, row):
    """One app's answer, in the vocabulary the page speaks.

        unknown   nobody has asked the market about this app yet
        none      asked, and this game has no cards
        fresh     a set, priced within PRICE_TTL
        stale     a set, priced longer ago than that - shown, and said so
    """
    if row is None:
        return {"appid": int(appid), "state": "unknown", "count": None,
                "cost": None, "currency": "USD", "cards": [], "stale": False,
                "checked_at": None}
    age = _age(row["seen_at"])
    cards = json.loads(row["cards"]) if row["cards"] else []
    if not cards:
        return {"appid": int(appid), "state": "none", "count": 0, "cost": None,
                "currency": "USD", "cards": [], "stale": False,
                "checked_at": row["seen_at"]}
    stale = age is None or age > PRICE_TTL
    # The rate rides along only where there is a figure to convert. An app with
    # no set, or one nobody has asked about yet, has nothing to approximate and
    # a rate on that answer would be a number about nothing.
    rate = fx.quote()
    return {
        "appid": int(appid),
        "state": "stale" if stale else "fresh",
        "count": row["count"],
        # One of each card at today's lowest listing, in cents. None when a
        # card in the set has no seller, because a total missing a card is not
        # what a set costs and rounding it down would be a lie.
        "cost": row["cost"],
        "currency": "USD",
        # What a dollar was worth on the day fx.py last read, per currency the
        # site reads in, so the page can print "≈ R$ x" beside the dollar. None
        # when there is no fresh rate, and then the page prints the dollar
        # alone rather than an estimate nobody can date.
        "rates": rate["rates"] if rate else None,
        "rates_at": rate["at"] if rate else None,
        "cards": cards,
        "stale": stale,
        "checked_at": row["seen_at"],
    }


def _due(row):
    if row is None:
        return True
    age = _age(row["seen_at"])
    if age is None:
        return True
    if not row["cards"]:
        return age > NONE_TTL
    return age > PRICE_TTL


def set_of(appid, wait=PAGE_WAIT):
    """One game's cards, for a page that is open.

    Blocks briefly - at most one market slot plus the request - and then gives
    up and answers with whatever is on disk. art.py and meta.price() have the
    same shape: one lock per appid, fetch on a miss, degrade rather than fail."""
    init()
    appid = int(appid)
    row = _row(appid)
    if not _due(row):
        return _shape(appid, row)

    got = _on_demand.acquire(blocking=False)
    if not got:
        want([appid])
        return _shape(appid, row)
    community.page_enter()
    try:
        with _lock_for(appid):
            # Somebody else may have filled it while this thread waited.
            row = _row(appid)
            if not _due(row):
                return _shape(appid, row)
            cool = community.cooling()
            if cool > 0 or not community.reserve(max_wait=wait):
                want([appid])
                return _shape(appid, row)
            try:
                _do_set(appid)
            except Throttled:
                want([appid])
            return _shape(appid, _row(appid))
    finally:
        community.page_leave()
        _on_demand.release()


def known(appids):
    """What is already on disk for these apps, without fetching. The cards view
    prints a set's cost beside the games it has one for, and says nothing at
    all about the rest rather than waiting for the market to catch up."""
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
                    f"SELECT * FROM card_sets WHERE appid IN ({marks})", chunk):
                out[r["appid"]] = _shape(r["appid"], r)
    return out


def want(appids):
    """Queue whatever is missing or stale. Nobody waits; the worker gets to it,
    and the next read off disk has it."""
    init()
    appids = [int(a) for a in appids]
    if not appids:
        return
    rows = {}
    with _db_lock, _connect() as con:
        marks = ",".join("?" * len(appids[:MAX_QUEUE]))
        for r in con.execute(
                f"SELECT * FROM card_sets WHERE appid IN ({marks})", appids[:MAX_QUEUE]):
            rows[r["appid"]] = r
    with _queue_lock:
        for appid in appids:
            if len(_wanted) >= MAX_QUEUE:
                break
            if appid in _queued or not _due(rows.get(appid)):
                continue
            _queued.add(appid)
            _wanted.append(appid)


def _run():
    global _backoff
    while True:
        with _queue_lock:
            appid = _wanted.popleft() if _wanted else None
        if appid is None:
            time.sleep(2)
            continue
        cool = community.cooling()
        if cool > 0:
            time.sleep(min(cool, BACKOFF_MAX))
        community.reserve(background=True)
        try:
            _do_set(appid)
            _backoff = BACKOFF_MIN
        except Throttled:
            # Too fast. Put it back and stop asking for a while.
            with _queue_lock:
                _wanted.appendleft(appid)
            time.sleep(_backoff)
            _backoff = min(_backoff * 2, BACKOFF_MAX)
            continue
        except Exception as e:  # noqa: BLE001 - a crawl thread must not die
            print(f"cards: {appid} -> {e}", flush=True)
        with _queue_lock:
            if appid not in _wanted:
                _queued.discard(appid)


def start():
    """One worker, started once. Daemon: it holds nothing that must be flushed."""
    global _worker
    init()
    if _worker is None:
        _worker = threading.Thread(target=_run, name="cards", daemon=True)
        _worker.start()
    return _worker


def has_cards(catalog):
    """Whether the storefront says this game drops cards, read off the cached
    catalogue meta.py already has. None when that catalogue has not been
    fetched yet, which is a third answer and not a no."""
    if not catalog:
        return None
    categories = catalog.get("categories")
    if categories is None:
        return None
    return any(int(c.get("id", 0)) == CARDS_CATEGORY for c in categories)


if __name__ == "__main__":
    import sys

    init()
    for arg in sys.argv[1:] or ["570"]:
        print(json.dumps(set_of(int(arg), wait=30), ensure_ascii=False, indent=1))
