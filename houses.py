#!/usr/bin/env python3
"""steamprofiler.org - who published and who made every game on Steam.

The site knows a great deal about the games somebody has owned and nothing at
all about the rest of the store. `apps` in meta.db fills up one library at a
time: a game enters it because a visitor owned it, so after months of traffic
it holds a few thousand of the hundred and eighty thousand apps Steam lists.
That is the right shape for prices and genres, which are only ever asked about
a game somebody is looking at.

It is the wrong shape for a question about a company. "Everything this studio
made" is a question about the catalogue, not about a library, and answered off
`apps` it silently means "everything this studio made that somebody who came
here happened to own" - which for almost every studio on Steam is nothing, so
the studio does not exist as far as the site is concerned.

Closing that gap through the storefront costs one appdetails call per app.
That is a hundred and eighty thousand calls against a quota the site spends on
answering people, and at the rate meta.py paces itself it is four days of
crawling to learn two strings per game.

So this table is filled from SteamSpy, which publishes a thousand rows at a
time with both strings already on them. Two things about that are worth
stating plainly, because they are the reason it is allowed to be here at all:

    Nothing is about a visitor.  Neither request carries a profile, an
                        address, or anything about who is reading. The walk
                        asks for a page number; the backfill below asks about
                        an appid, and which appid is decided by where a
                        catalogue walk had got to, never by what somebody
                        opened. The source cannot learn that this server has
                        a visitor, let alone which one.

    Nothing depends.    Both fetches happen on a timer, never on the path of
                        a request. If the source is gone the table keeps what
                        it had, the screens keep working, and the only thing
                        that ages is how recently a new studio appeared.

`request=all` does not publish the whole catalogue, and finding that out the
hard way is why the rest of this file exists. It is ordered by owners and it
stops: measured on 2026-09-09 it ended at page 86, half of it, with 82,493 of
the 175,162 apps Steam lists. What falls off the end is the tail - new games,
small games, games nobody owns yet - which is most of the shop and exactly
the half where a studio has one game and no other way to be found. A publisher
that shipped its first game last week was not late to the index; it was never
going to be in it.

So there is a second source, `request=appdetails`, one app at a time, and a
`house_learned` table for what it and the store cache find. It is paced at a
request a second, it walks the catalogue snapshot in appid order and picks up
where it stopped, and it only ever asks about apps the walk did not already
answer for. That table is never dropped: the weekly rebuild swaps fresh tables
in, so anything learned outside the walk is merged into them on the way past,
or a week of backfill would disappear every Tuesday.

What is kept from it is two strings per app - who published, who developed -
and one appid per company, which is the game whose picture stands for it on
the screens. Owners, revenue estimates and playtime are not stored: the site
has its own numbers for the games it knows, measured rather than modelled,
and mixing an estimate into them would make both worthless.

The one field read and thrown away is the review count, used while the walk
is running to decide which of a company's games supplies that picture. It is
a count rather than a model, it never reaches the database and it never
reaches a reader - the whole of its influence is which capsule is on a tile.
The alternative was the lowest appid, which picks whatever a company shipped
first and is a rule that would put a 2009 shovelware port on the face of a
studio known for one game from last year.

The release year is not taken from there either. It comes from meta.db, for
the games this site has actually read, and stays absent for the rest - so a
year on these screens is always a year Steam told this server, and never one
inferred somewhere else.
"""
import json
import os
import re
import sqlite3
import threading
import time
import unicodedata
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "/app/data"))
# Its own file rather than a third pair of tables in meta.db. meta.py carries a
# migration history and a backup that are about the store cache; this is a
# rebuildable index of a public catalogue, and losing it costs a re-fetch.
DB_PATH = DATA_DIR / "houses.db"

SOURCE = "https://steamspy.com/api.php?request=all&page=%d"
# The source documents one request a minute for this endpoint. Sixty-two, not
# sixty: a limiter measuring the same window from its side must see the gap as
# comfortably over, not exactly at, the line.
PAGE_GAP = 62.0
# It ends by answering an empty page, but a source that starts erroring should
# not turn into an unbounded walk either.
MAX_PAGES = 400

# One app at a time, for the half of the catalogue the walk above never
# reaches. The source documents a request a second here rather than a minute,
# which is the whole reason filling the gap this way is possible at all.
DETAIL_SOURCE = "https://steamspy.com/api.php?request=appdetails&appid=%d"
DETAIL_GAP = 1.1
# How many apps one tick may ask about. Sized to leave the hour it runs in
# with room to spare, so the worker is never still working when the next tick
# is due and a refresh that comes due is not queued behind a backfill.
BACKFILL_BUDGET = 2500
# How many appids to read out of the catalogue at once while looking for the
# next unanswered one. Larger than the budget because most of what comes back
# is already known and skipped without a request.
BACKFILL_SCAN = 20000

# A week, which is the promise the screens make about how fresh the list of
# companies is. A studio that shipped its first game on Tuesday shows up by
# the following Tuesday, and that is fast enough for a question about who
# exists.
MAX_AGE = timedelta(days=7)
# How often the worker wakes to ask whether a refresh is due. Short compared
# to the age, so a server that was down over a weekend catches up soon after
# it comes back rather than on the next whole week.
TICK = 3600

KINDS = ("publisher", "developer")

_db_lock = threading.Lock()
_worker = None
_ready = False


# ── Names ────────────────────────────────────────────────────────────────
# The source joins several companies into one string with commas, and company
# names contain commas. `Treyarch, Raven Software, Beenox` is three and
# `CAPCOM Co., Ltd.` is one, and telling them apart is the whole job.
#
# The rule: split on the comma, then glue a fragment back on if it is only a
# corporate suffix, because no company is called "Ltd." on its own. That gets
# `FromSoftware, Inc.`, `KRAFTON, Inc.` and `CAPCOM Co., Ltd.` right without a
# list of exceptions naming particular companies - which would be the same
# hand-picking this table exists to avoid.
SUFFIX = re.compile(
    r"""^(?:
        inc | inc\. | llc | llc\. | ltd | ltd\. | limited | co | co\. | corp |
        corp\. | corporation | gmbh | s\.?a\.? | s\.?r\.?l\.? | b\.?v\.? |
        a\.?s\.? | oy | ab | plc | pty | pty\.? \s+ ltd\.? | kk | k\.?k\.? |
        pte\.? \s+ ltd\.? | sp\.? \s* z \s* o\.?o\.? | s\.?l\.? | nv | n\.?v\.?
    )$""",
    re.IGNORECASE | re.VERBOSE,
)


def split_names(raw):
    """The companies in one of the source's strings, in order, deduplicated."""
    if not raw:
        return []
    parts = [p.strip() for p in str(raw).split(",")]
    out = []
    for part in parts:
        if not part:
            continue
        if out and SUFFIX.match(part):
            out[-1] = f"{out[-1]}, {part}"
        else:
            out.append(part)
    seen, names = set(), []
    for name in out:
        name = re.sub(r"\s+", " ", name).strip()
        # A name that is only punctuation is not a company, and neither is the
        # source's own placeholder for "we do not know".
        if not name or name.lower() in {"n/a", "na", "none", "unknown", "-"}:
            continue
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        names.append(name)
    return names


def slugify(name):
    """A company's address. Empty when the name has no ascii in it at all,
    and the caller drops those rather than inventing one."""
    s = unicodedata.normalize("NFKD", name)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.encode("ascii", "ignore").decode("ascii").lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s[:40].strip("-")


# ── The table ────────────────────────────────────────────────────────────

@contextmanager
def _connect():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=20)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    # NORMAL rather than FULL, like the other caches - see meta.py. A publisher
    # learned twice is a publisher learned; nothing in this file is a fact that
    # only exists here.
    con.execute("PRAGMA synchronous=NORMAL")
    try:
        with con:
            yield con
    finally:
        con.close()


def init():
    """Idempotent, and called from every entry point rather than only from
    start(): the command line reads this table too, where no worker runs."""
    global _ready
    if _ready:
        return
    with _db_lock, _connect() as con:
        con.executescript("""
            -- One row per company per app per axis. The same appid appears
            -- twice when a studio published its own game, and that is not
            -- duplication: they are answers to two different questions.
            CREATE TABLE IF NOT EXISTS houses (
                kind  TEXT    NOT NULL,
                slug  TEXT    NOT NULL,
                appid INTEGER NOT NULL,
                PRIMARY KEY (kind, slug, appid)
            ) WITHOUT ROWID;
            CREATE INDEX IF NOT EXISTS houses_by_app ON houses (appid);

            -- The company itself, with the count kept beside it. Counting
            -- fifty thousand shelves on every index request is the one query
            -- that would make these screens slow.
            CREATE TABLE IF NOT EXISTS house_names (
                kind     TEXT    NOT NULL,
                slug     TEXT    NOT NULL,
                name     TEXT    NOT NULL,
                games    INTEGER NOT NULL DEFAULT 0,
                flagship INTEGER,
                PRIMARY KEY (kind, slug)
            ) WITHOUT ROWID;
            CREATE INDEX IF NOT EXISTS house_names_ranked
                ON house_names (kind, games DESC, name COLLATE NOCASE);
            -- Lowercased once and written down, because the filter on the
            -- index page is a prefix-and-substring match over fifty thousand
            -- names and doing it with lower() in the query scans them all.
            CREATE INDEX IF NOT EXISTS house_names_search
                ON house_names (kind, name COLLATE NOCASE);

            -- The app's name as the catalogue lists it. The year is not here:
            -- it comes from meta.db, only for what this site has read, so a
            -- year on screen is always one Steam told this server.
            CREATE TABLE IF NOT EXISTS house_apps (
                appid INTEGER PRIMARY KEY,
                name  TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS house_sync (
                id     INTEGER PRIMARY KEY CHECK (id = 1),
                at     TEXT,
                pages  INTEGER,
                apps   INTEGER,
                houses INTEGER
            );

            -- What was learned outside the weekly walk: the per-app backfill
            -- below, and the store cache when a visitor opens a game the walk
            -- never covered. This is the one table refresh() does not drop,
            -- and the reason it cannot is arithmetic: the walk reaches half
            -- the catalogue, so a rebuild that kept only what the walk found
            -- would throw away the other half every week.
            CREATE TABLE IF NOT EXISTS house_learned (
                kind     TEXT    NOT NULL,
                slug     TEXT    NOT NULL,
                appid    INTEGER NOT NULL,
                name     TEXT    NOT NULL,
                app_name TEXT    NOT NULL,
                at       TEXT,
                PRIMARY KEY (kind, slug, appid)
            ) WITHOUT ROWID;
            CREATE INDEX IF NOT EXISTS house_learned_by_app
                ON house_learned (appid);

            -- Where the catalogue walk stopped. One row, one number: the
            -- backfill resumes from the appid after it, so a restart costs
            -- the current batch and nothing else.
            CREATE TABLE IF NOT EXISTS house_backfill (
                id     INTEGER PRIMARY KEY CHECK (id = 1),
                cursor INTEGER NOT NULL DEFAULT 0,
                asked  INTEGER NOT NULL DEFAULT 0,
                found  INTEGER NOT NULL DEFAULT 0,
                at     TEXT,
                laps   INTEGER NOT NULL DEFAULT 0
            );
        """)
        # An index built before the picture existed keeps its rows and gains
        # the column; refresh() sees it empty and fills it on the next walk.
        cols = {r["name"] for r in con.execute("PRAGMA table_info(house_names)")}
        if "flagship" not in cols:
            con.execute("ALTER TABLE house_names ADD COLUMN flagship INTEGER")
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


def state():
    """When the catalogue was last walked, and how big it came back."""
    init()
    with _db_lock, _connect() as con:
        row = con.execute("SELECT * FROM house_sync WHERE id = 1").fetchone()
    if not row:
        return {"at": None, "pages": 0, "apps": 0, "houses": 0, "art": 0,
                "stale": True}
    age = _age(row["at"])
    # Counted rather than assumed: an index written before the capsule existed
    # is complete by its own timestamp and still has no picture on any tile,
    # and the walk that fills it in has to be allowed to run.
    with _db_lock, _connect() as con:
        art = con.execute(
            "SELECT COUNT(*) FROM house_names WHERE flagship IS NOT NULL").fetchone()[0]
    return {
        "at": row["at"], "pages": row["pages"], "apps": row["apps"],
        "houses": row["houses"], "art": art,
        "stale": age is None or age > MAX_AGE,
    }


# ── Learning one app ─────────────────────────────────────────────────────

def names_from(value):
    """The companies in whatever shape the caller has them.

    The two sources disagree and both are right for themselves. SteamSpy
    joins them into one string with commas, which is what split_names() is
    for and why that function is as careful as it is. The storefront hands
    over a list, already split, where running the comma rule again would be
    wrong twice over: `CAPCOM Co., Ltd.` would come apart, and a list that
    was never a string would be stringified into `['Horny Capybara Studio']`
    and stored with the brackets on it.

    That second one is not hypothetical. It is what this function was written
    after: the first version passed the list straight to split_names and the
    publisher went into the index under its own repr."""
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        out, seen = [], set()
        for item in value:
            who = " ".join(str(item or "").split())
            key = who.casefold()
            if who and key not in seen:
                seen.add(key)
                out.append(who)
        return out
    return split_names(value)


def _rows_for(appid, app_name, by_kind):
    """The (kind, slug, name) triples one app contributes, deduplicated."""
    out = []
    for kind in KINDS:
        for who in names_from(by_kind.get(kind)):
            slug = slugify(who)
            if slug:
                out.append((kind, slug, who[:200]))
    return out


def learn(appid, app_name, publishers=None, developers=None):
    """Record who published and who made one app, from outside the walk.

    Two callers: the backfill below, and the store cache when it reads a
    game's catalogue for a page somebody opened. Both know two strings about
    one app and neither can wait a week for the walk to maybe cover it.

    Written twice on purpose. `house_learned` is the durable copy that
    survives the weekly rebuild; the live tables get the same rows so the
    company is on the screens now rather than after the next walk. A studio
    whose only game came out this morning is the case this exists for, and
    telling it to come back Tuesday is not an answer."""
    init()
    try:
        appid = int(appid)
    except (TypeError, ValueError):
        return 0
    app_name = " ".join(str(app_name or "").split())[:200]
    if not appid or not app_name:
        return 0
    rows = _rows_for(appid, app_name, {"publisher": publishers,
                                       "developer": developers})
    if not rows:
        return 0

    stamp = _now().isoformat(timespec="seconds")
    with _db_lock, _connect() as con:
        con.executemany(
            "INSERT OR REPLACE INTO house_learned"
            " (kind, slug, appid, name, app_name, at) VALUES (?, ?, ?, ?, ?, ?)",
            [(kind, slug, appid, name, app_name, stamp) for kind, slug, name in rows])
        con.execute("INSERT OR REPLACE INTO house_apps (appid, name) VALUES (?, ?)",
                    (appid, app_name))
        con.executemany(
            "INSERT OR IGNORE INTO houses (kind, slug, appid) VALUES (?, ?, ?)",
            [(kind, slug, appid) for kind, slug, _ in rows])
        # A company nobody had heard of gets its row, and this app as the
        # picture because it is the only game the index knows it has. A
        # company that already existed keeps the face the walk chose for it,
        # which was picked from review counts across everything it shipped.
        con.executemany(
            "INSERT OR IGNORE INTO house_names (kind, slug, name, games, flagship)"
            " VALUES (?, ?, ?, 0, ?)",
            [(kind, slug, name, appid) for kind, slug, name in rows])
        for kind, slug, _ in rows:
            con.execute(
                "UPDATE house_names SET games ="
                " (SELECT COUNT(*) FROM houses h WHERE h.kind = ? AND h.slug = ?)"
                " WHERE kind = ? AND slug = ?", (kind, slug, kind, slug))
    return len(rows)


def learned_state():
    """How far the backfill has walked, and what it has to show for it."""
    init()
    with _db_lock, _connect() as con:
        row = con.execute("SELECT * FROM house_backfill WHERE id = 1").fetchone()
        rows = con.execute("SELECT COUNT(*) FROM house_learned").fetchone()[0]
        apps = con.execute(
            "SELECT COUNT(DISTINCT appid) FROM house_learned").fetchone()[0]
    return {
        "cursor": row["cursor"] if row else 0,
        "asked": row["asked"] if row else 0,
        "found": row["found"] if row else 0,
        "laps": row["laps"] if row else 0,
        "at": row["at"] if row else None,
        "rows": rows, "apps": apps,
    }


def _detail(appid, tries=2):
    """One app from the per-app endpoint, or None."""
    for attempt in range(tries):
        req = urllib.request.Request(
            DETAIL_SOURCE % int(appid),
            headers={"User-Agent": "steamprofiler.org"})
        try:
            with urllib.request.urlopen(req, timeout=30) as answer:
                body = answer.read()
        except (urllib.error.URLError, OSError, TimeoutError):
            if attempt + 1 == tries:
                return None
            time.sleep(DETAIL_GAP * 3)
            continue
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None
    return None


def backfill(budget=BACKFILL_BUDGET, catalogue=None):
    """Ask about the apps the walk never covered, a second at a time.

    `catalogue` is the appid source, defaulting to the snapshot meta.py keeps
    of every public game on Steam. That is the right side to walk: the whole
    shop, so that "which of these has nobody told us about" is a question with
    an answer, rather than the part of the shop this site has already read.

    Ends the lap by rewinding the cursor. The catalogue grows, companies get
    renamed, and an app that answered nothing today may answer next month, so
    the walk is a loop rather than a job that finishes."""
    init()
    getter = catalogue or _catalogue
    with _db_lock, _connect() as con:
        row = con.execute("SELECT * FROM house_backfill WHERE id = 1").fetchone()
    cursor = row["cursor"] if row else 0
    asked = found = 0
    laps = (row["laps"] if row else 0)

    while asked < budget:
        batch = getter(cursor, BACKFILL_SCAN)
        if not batch:
            # Off the end of the catalogue: the lap is done and the next one
            # starts at the beginning, where by then there will be new apps.
            cursor = 0
            laps += 1
            break
        cursor = batch[-1]
        with _db_lock, _connect() as con:
            marks = ",".join("?" * len(batch))
            known = {r[0] for r in con.execute(
                f"SELECT appid FROM house_apps WHERE appid IN ({marks})", batch)}
        for appid in batch:
            if appid in known:
                continue
            if asked >= budget:
                # Stopped mid-batch, so the cursor goes back to just before
                # this app rather than to the end of a batch that was not
                # finished. Asking twice is cheap; skipping is not.
                cursor = appid - 1
                break
            value = _detail(appid)
            asked += 1
            time.sleep(DETAIL_GAP)
            if not value:
                continue
            if learn(appid, value.get("name"),
                     value.get("publisher"), value.get("developer")):
                found += 1

    stamp = _now().isoformat(timespec="seconds")
    with _db_lock, _connect() as con:
        con.execute("""
            INSERT INTO house_backfill (id, cursor, asked, found, at, laps)
            VALUES (1, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                cursor = excluded.cursor, at = excluded.at, laps = excluded.laps,
                asked = house_backfill.asked + excluded.asked,
                found = house_backfill.found + excluded.found
        """, (cursor, asked, found, stamp, laps))
    return {"asked": asked, "found": found, "cursor": cursor, "laps": laps}


def _catalogue(after, limit):
    """Every public game Steam lists, ascending. Imported here rather than at
    the top of the file: houses is readable from the command line against its
    own database, and that should not need the store cache to open."""
    import meta
    return meta.catalogue_appids(after, limit)


# ── Filling it ───────────────────────────────────────────────────────────

def _fetch(page, tries=3):
    url = SOURCE % page
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={
                # A real name rather than a browser's, so the source can see
                # who is asking and block it by name if it ever wants to.
                "User-Agent": "steamprofiler.org (+https://steamprofiler.org)",
            })
            with urllib.request.urlopen(req, timeout=120) as answer:
                return json.load(answer)
        except urllib.error.HTTPError as e:
            # Past the last page the source answers 500 rather than an empty
            # object, so that is an ending and not a failure.
            if e.code in (400, 404, 500) and attempt == tries - 1:
                return None
            time.sleep(20)
        except Exception:  # noqa: BLE001 - a crawl must survive anything
            if attempt == tries - 1:
                return None
            time.sleep(20)
    return None


def refresh(force=False):
    """Walk the catalogue and rebuild the index. Returns what changed.

    The whole walk is over an hour, so it is built into scratch tables and
    swapped in at the end: a reader during the walk sees last week's list,
    complete, rather than this week's list half-written."""
    init()
    if not force:
        current = state()
        if not current["stale"] and current["art"]:
            return {"skipped": True, **current}

    rows = []          # (kind, slug, appid)
    names = {}         # (kind, slug) -> name
    apps = {}          # appid -> name
    # (kind, slug) -> (reviews, appid). The review count lives here, in memory,
    # for as long as the walk does: it decides which capsule stands for the
    # company and then it is gone.
    face = {}
    pages = 0

    for page in range(MAX_PAGES):
        data = _fetch(page)
        if data is None:
            break
        if not isinstance(data, dict) or not data:
            break
        pages += 1
        for value in data.values():
            if not isinstance(value, dict):
                continue
            try:
                appid = int(value.get("appid"))
            except (TypeError, ValueError):
                continue
            name = (value.get("name") or "").strip()
            if not appid or not name:
                continue
            apps[appid] = name[:200]
            try:
                seen = int(value.get("positive") or 0) + int(value.get("negative") or 0)
            except (TypeError, ValueError):
                seen = 0
            for kind in KINDS:
                for who in split_names(value.get(kind)):
                    slug = slugify(who)
                    if not slug:
                        continue
                    rows.append((kind, slug, appid))
                    names.setdefault((kind, slug), who[:200])
                    key = (kind, slug)
                    if seen >= face.get(key, (-1, 0))[0]:
                        face[key] = (seen, appid)
        time.sleep(PAGE_GAP)

    # A walk that came back with almost nothing is a source having a bad day,
    # and replacing a good table with it is worse than being a week stale.
    if pages < 2 or len(apps) < 1000:
        return {"failed": True, "pages": pages, "apps": len(apps)}

    with _db_lock, _connect() as con:
        con.executescript("""
            DROP TABLE IF EXISTS houses_new;
            DROP TABLE IF EXISTS house_names_new;
            DROP TABLE IF EXISTS house_apps_new;
            CREATE TABLE houses_new (
                kind TEXT NOT NULL, slug TEXT NOT NULL, appid INTEGER NOT NULL,
                PRIMARY KEY (kind, slug, appid)
            ) WITHOUT ROWID;
            CREATE TABLE house_names_new (
                kind TEXT NOT NULL, slug TEXT NOT NULL, name TEXT NOT NULL,
                games INTEGER NOT NULL DEFAULT 0, flagship INTEGER,
                PRIMARY KEY (kind, slug)
            ) WITHOUT ROWID;
            CREATE TABLE house_apps_new (
                appid INTEGER PRIMARY KEY, name TEXT NOT NULL
            );
        """)
        con.executemany(
            "INSERT OR IGNORE INTO houses_new (kind, slug, appid) VALUES (?, ?, ?)", rows)
        con.executemany(
            "INSERT OR IGNORE INTO house_names_new (kind, slug, name, flagship)"
            " VALUES (?, ?, ?, ?)",
            [(kind, slug, name, face.get((kind, slug), (0, None))[1])
             for (kind, slug), name in names.items()])
        con.executemany(
            "INSERT OR REPLACE INTO house_apps_new (appid, name) VALUES (?, ?)",
            list(apps.items()))
        # Everything learned outside this walk, folded in before the counting.
        # Without this the swap below would be a weekly amnesia: the walk
        # reaches half the catalogue, so the other half - which only the
        # backfill and the store cache know about - would vanish and be
        # re-fetched from nothing every time.
        #
        # INSERT OR IGNORE for the names, so a company the walk already found
        # keeps the face the walk chose. The walk picks it from review counts
        # across everything the company shipped; a learned row only ever knows
        # about one app and would replace a considered choice with an
        # arbitrary one.
        con.executescript("""
            INSERT OR IGNORE INTO houses_new (kind, slug, appid)
                SELECT kind, slug, appid FROM house_learned;
            INSERT OR IGNORE INTO house_names_new (kind, slug, name, flagship)
                SELECT kind, slug, name, appid FROM house_learned;
            INSERT OR IGNORE INTO house_apps_new (appid, name)
                SELECT appid, app_name FROM house_learned;
        """)
        con.execute("""
            UPDATE house_names_new SET games = (
                SELECT COUNT(*) FROM houses_new h
                WHERE h.kind = house_names_new.kind AND h.slug = house_names_new.slug)
        """)
        # A name that ended up with no games is a row nobody can open.
        con.execute("DELETE FROM house_names_new WHERE games = 0")
        con.executescript("""
            DROP TABLE IF EXISTS houses;
            DROP TABLE IF EXISTS house_names;
            DROP TABLE IF EXISTS house_apps;
            ALTER TABLE houses_new      RENAME TO houses;
            ALTER TABLE house_names_new RENAME TO house_names;
            ALTER TABLE house_apps_new  RENAME TO house_apps;
            CREATE INDEX IF NOT EXISTS houses_by_app ON houses (appid);
            CREATE INDEX IF NOT EXISTS house_names_ranked
                ON house_names (kind, games DESC, name COLLATE NOCASE);
            CREATE INDEX IF NOT EXISTS house_names_search
                ON house_names (kind, name COLLATE NOCASE);
        """)
        total = con.execute("SELECT COUNT(*) FROM house_names").fetchone()[0]
        con.execute("""
            INSERT INTO house_sync (id, at, pages, apps, houses)
            VALUES (1, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                at = excluded.at, pages = excluded.pages,
                apps = excluded.apps, houses = excluded.houses
        """, (_now().isoformat(timespec="seconds"), pages, len(apps), total))

    return {"pages": pages, "apps": len(apps), "houses": total}


# ── Reading it ───────────────────────────────────────────────────────────

def kind_of(word):
    """The axis a request names, or None. `publishers` and `developers` are
    what the addresses say; the table stores the singular."""
    word = (word or "").strip().lower().rstrip("s")
    return word if word in KINDS else None


def index(kind, query="", start=0, count=60):
    """A page of companies, biggest shelf first.

    Filtered here rather than in the browser, which is the whole reason this
    is an endpoint: the list is tens of thousands of names and shipping it to
    be filtered is shipping the thing instead of the answer."""
    init()
    start = max(0, int(start or 0))
    count = max(1, min(200, int(count or 60)))
    query = (query or "").strip()
    where, args = ["kind = ?"], [kind]
    if query:
        where.append("name LIKE ? ESCAPE '\\'")
        args.append("%" + query.replace("\\", "\\\\")
                    .replace("%", "\\%").replace("_", "\\_") + "%")
    sql = " WHERE " + " AND ".join(where)
    with _db_lock, _connect() as con:
        total = con.execute("SELECT COUNT(*) FROM house_names" + sql, args).fetchone()[0]
        rows = con.execute(
            "SELECT slug, name, games, flagship FROM house_names" + sql
            + " ORDER BY games DESC, name COLLATE NOCASE LIMIT ? OFFSET ?",
            (*args, count, start)).fetchall()
    return {
        "houses": [{"slug": r["slug"], "name": r["name"], "games": r["games"],
                    "art": r["flagship"]} for r in rows],
        "total": total, "start": start, "count": len(rows),
    }


def mine(kind, appids, query="", start=0, count=60):
    """The companies one library is actually on, most of it first.

    This is the half of the filter the index cannot do in a browser and the
    api cannot do in a shared cache: which of fifty thousand companies a
    particular profile has anything from. It is answered here because the join
    is a join - five thousand appids against an index - and doing it any other
    way means shipping one of the two sides across the network.

    `owned` is how many of this company's games are in that library and
    `games` is how many it has in all, so the tile can say six of a hundred
    and fifty-one without the shelf being sent."""
    init()
    start = max(0, int(start or 0))
    count = max(1, min(200, int(count or 60)))
    ids = [int(a) for a in appids if a]
    if not ids:
        return {"houses": [], "total": 0, "start": 0, "count": 0}
    query = (query or "").strip()

    where, args = ["h.kind = ?"], [kind]
    if query:
        where.append("n.name LIKE ? ESCAPE '\\'")
        args.append("%" + query.replace("\\", "\\\\")
                    .replace("%", "\\%").replace("_", "\\_") + "%")
    clause = " AND ".join(where)

    with _db_lock, _connect() as con:
        # A temporary table rather than a VALUES list: a library is thousands
        # of appids, and sqlite parses a thousand-row VALUES clause every time
        # somebody types a letter into the filter.
        con.execute("CREATE TEMP TABLE lib (appid INTEGER PRIMARY KEY)")
        con.executemany("INSERT OR IGNORE INTO lib (appid) VALUES (?)",
                        [(i,) for i in ids])
        sql = f"""
            SELECT n.slug AS slug, n.name AS name, n.games AS games,
                   n.flagship AS flagship, COUNT(*) AS owned
            FROM houses h
            JOIN lib ON lib.appid = h.appid
            JOIN house_names n ON n.kind = h.kind AND n.slug = h.slug
            WHERE {clause}
            GROUP BY h.slug
        """
        total = con.execute(
            f"SELECT COUNT(*) FROM ({sql})", args).fetchone()[0]
        rows = con.execute(
            sql + " ORDER BY owned DESC, n.games DESC, n.name COLLATE NOCASE"
                  " LIMIT ? OFFSET ?",
            (*args, count, start)).fetchall()
        con.execute("DROP TABLE lib")
    return {
        "houses": [{"slug": r["slug"], "name": r["name"], "games": r["games"],
                    "art": r["flagship"], "owned": r["owned"]} for r in rows],
        "total": total, "start": start, "count": len(rows),
    }


def shelf(kind, slug):
    """One company: its name, and every app filed under it on that axis.

    The year is not here. api.py fills it from the store cache for the apps
    that have been read, and leaves it absent for the rest."""
    init()
    with _db_lock, _connect() as con:
        who = con.execute(
            "SELECT name, games, flagship FROM house_names WHERE kind = ? AND slug = ?",
            (kind, slug)).fetchone()
        if not who:
            return None
        rows = con.execute("""
            SELECT h.appid AS appid, a.name AS name
            FROM houses h LEFT JOIN house_apps a ON a.appid = h.appid
            WHERE h.kind = ? AND h.slug = ?
        """, (kind, slug)).fetchall()
    return {
        "slug": slug, "name": who["name"], "games": who["games"],
        "art": who["flagship"],
        "apps": [{"appid": r["appid"], "name": r["name"] or f"app {r['appid']}"}
                 for r in rows],
    }


def houses_of(appid):
    """Which companies an app is filed under, both axes. Not used by the house
    screens - it is here because a game page asking "who made this" is the
    same table read the other way round."""
    init()
    with _db_lock, _connect() as con:
        rows = con.execute("""
            SELECT h.kind AS kind, h.slug AS slug, n.name AS name
            FROM houses h JOIN house_names n ON n.kind = h.kind AND n.slug = h.slug
            WHERE h.appid = ?
        """, (appid,)).fetchall()
    out = {kind: [] for kind in KINDS}
    for r in rows:
        out[r["kind"]].append({"slug": r["slug"], "name": r["name"]})
    return out


# ── The worker ───────────────────────────────────────────────────────────

def _run():
    while True:
        spent = False
        try:
            result = refresh()
            if not result.get("skipped"):
                print(f"houses: {result}", flush=True)
                spent = True
        except Exception as e:  # noqa: BLE001 - a crawl thread must not die
            print(f"houses: {e}", flush=True)
        # The rest of the tick goes to the gap, but only on a tick that did
        # not just spend an hour walking pages. The weekly refresh is the one
        # that keeps the whole index true, and it should never be waiting
        # behind a backfill that will still be there next hour.
        if not spent:
            try:
                got = backfill()
                if got["asked"]:
                    print(f"houses backfill: {got}", flush=True)
            except Exception as e:  # noqa: BLE001
                print(f"houses backfill: {e}", flush=True)
        time.sleep(TICK)


def start():
    """One worker, started once. Daemon: it holds nothing that must be
    flushed, and a refresh interrupted halfway leaves the old table in place
    because the new one is only swapped in at the end."""
    global _worker
    init()
    if _worker is None:
        _worker = threading.Thread(target=_run, name="houses", daemon=True)
        _worker.start()
    return _worker


if __name__ == "__main__":
    import sys
    init()
    if "--refresh" in sys.argv:
        print(json.dumps(refresh(force=True), ensure_ascii=False, indent=1))
    if "--backfill" in sys.argv:
        budget = BACKFILL_BUDGET
        for arg in sys.argv:
            if arg.startswith("--budget="):
                budget = int(arg.split("=", 1)[1])
        print(json.dumps(backfill(budget), ensure_ascii=False, indent=1))
    print(json.dumps(state(), ensure_ascii=False, indent=1))
    print(json.dumps(learned_state(), ensure_ascii=False, indent=1))
