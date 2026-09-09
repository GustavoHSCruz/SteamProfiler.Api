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

So this table is filled from SteamSpy, which publishes the whole catalogue a
thousand rows at a time with both strings already on them. Two things about
that are worth stating plainly, because they are the reason it is allowed to
be here at all:

    Nothing goes out.   The request is a page number. Not an appid, not a
                        profile, not an address, nothing about anybody who
                        visited. There is no way for the source to learn that
                        this server has a visitor, let alone which one.

    Nothing depends.    The fetch happens on a weekly timer, never on the path
                        of a request. If the source is gone the table keeps
                        what it had, the screens keep working, and the only
                        thing that ages is how recently a new studio appeared.

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
        try:
            result = refresh()
            if not result.get("skipped"):
                print(f"houses: {result}", flush=True)
        except Exception as e:  # noqa: BLE001 - a crawl thread must not die
            print(f"houses: {e}", flush=True)
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
    print(json.dumps(state(), ensure_ascii=False, indent=1))
