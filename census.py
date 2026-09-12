#!/usr/bin/env python3
"""steamprofiler.org - counting the traffic without learning who it was.

For two days this site could not answer "how many people used it". The log was
built to make that impossible - the format drops $remote_addr on purpose - and
nothing on disk recorded a visit. That was the right default and it stays the
default for everything except the one number the owner actually needed, which
is: is this the same visitor again, roughly what kind of thing is it, and
roughly where is it from.

This module is that one number, and it is built around a promise that is
stronger than "I will not look":

    Three counts, no key between any two. `visitors` says a visitor came
    back. `subjects` says a profile was looked up. `screens` says a screen was
    opened. There is no column in common, so the join does not exist - not
    withheld, absent. Nobody can run it, the owner included, because there is
    nothing to run it on.

Everything that follows is in service of keeping that true, and of keeping the
identifiable half small and short-lived.

    the epoch       The salt is derived per seven-day epoch from a seed that
                    lives in the environment, never in the data directory. When
                    the epoch turns, the old epoch's rows are collapsed into
                    plain counts and the hashes are deleted. So "the same
                    visitor again" is a question this can answer for at most a
                    week, and then it structurally cannot. Retention is not a
                    promise to delete something; it is the absence of anything
                    to delete.

    the subject     Kept per day, so the owner can see a profile's traffic over
                    time. The visitor side deliberately holds no per-day
                    activity - only first seen and last seen - because a
                    per-day record on both sides would be the join key that
                    the schema is built to not have.

    the class       Guessed from behaviour, live, on the request path, and only
                    the verdict is written down. The evidence - the sequence of
                    paths, the timing - stays in memory and dies there. A
                    label is one word; a request log is a dossier.

    the origin      Country and region as Cloudflare reports them, already
                    resolved, in a header. This server never needs the address
                    to know them and never writes one down.

    the screen      Which screen was opened, as a name off a fixed list -
                    "u.cards", "blog.post" - and never the path it was read
                    from. The profile in /u/<who>/cards is dropped before
                    anything is counted, which is what keeps this from being
                    a second subjects table with an address beside it: a
                    screen name is the same word for everybody who opens it.
                    A reload does not count twice, because the memory holds
                    which screen an address is on and only a change is a view.
                    Leaving and coming back is a change, and counts.

Like guard.py, the working state is in memory and a restart forgets the part
that has not been flushed yet. Unlike guard.py, some of it does reach disk -
because "is this the same visitor as last Tuesday" is a question about last
Tuesday, and memory cannot answer it. The flush is a timer, not a write per
request: the gate runs on every font and every image on the site, and a
database write in that path would cost more than the answer is worth.

None of this decides anything. No page changes because of what is in here and
no lookup is refused because of it. It exists so the owner can see whether
anyone is out there.

One narrow part of it does leave this server, and it is drawn tight on purpose:
public() returns four totals and a list of past weeks, all of them counts with
nobody inside them, and the status page prints those. Everything else here -
the classes, the countries, the regions, every figure that belongs to one
profile or one address - answers to the owner's panel and to nothing else.
"""

import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "/app/data"))
DB_PATH = DATA_DIR / "census.db"

# ── The epoch ────────────────────────────────────────────────────────
# Seven days, and the reason it is not thirty is worth keeping written down.
# Thirty was the first answer, and it was wrong in both directions at once.
#
# Addresses are recycled: an ISP hands out a new one on reconnect, a carrier
# NAT puts hundreds of people behind one, a phone changes network, a VPN moves
# somebody to another country. Over a longer window all of that accumulates, so
# a hash stops being "a visitor" in both directions - one hash collects several
# people, and one person shows up as several hashes. A thirty-day count of
# "unique visitors" would be a confident number that is wrong.
#
# So the window is short, which makes it *both* more private and more accurate.
# Those two usually pull against each other. Here they do not, and when that
# happens the only mistake is not taking it.
EPOCH = int(os.environ.get("CENSUS_EPOCH", str(7 * 24 * 3600)))

# The seed the per-epoch salts are derived from. In the environment, never in
# DATA_DIR - and that placement is the entire security property, not the hash.
#
# A salted digest of an address is not reversible, but it is *enumerable*: there
# are only four billion IPv4 addresses, so anybody holding the seed and the
# database can recompute the lot in minutes. Which means the two must not live
# in the same place. store.py's salt sits in DATA_DIR next to the databases it
# salts, so one copy of that directory carries both halves; this one does not
# repeat that.
#
# With no seed set, an ephemeral one is generated per process. That is a
# deliberate choice of which way to fail: recurrence tracking resets on every
# restart, which makes the feature nearly useless, and nothing is written to
# disk that a leaked directory could unpick. Failing towards useless beats
# failing towards a seed in the data volume.
_SEED_ENV = os.environ.get("CENSUS_SEED", "").strip()
_seed = _SEED_ENV or secrets.token_hex(32)
EPHEMERAL = not _SEED_ENV
if EPHEMERAL:
    print("census: no CENSUS_SEED set - using an ephemeral seed, so recurrence "
          "resets on restart. Set one in .env to keep the week.", file=sys.stderr)

# How often the memory is written down. Long enough that the gate never waits
# on SQLite, short enough that a deploy loses minutes rather than a day.
FLUSH_EVERY = int(os.environ.get("CENSUS_FLUSH", "60"))
# A visitor nobody has heard from in this long is finished: classified, written
# and dropped out of memory.
IDLE = int(os.environ.get("CENSUS_IDLE", "1800"))
# The ceiling on how many are tracked at once, for the same reason guard.py has
# one: this is memory an anonymous caller can ask for.
MAX_TRACKED = 20000

# ── What the classes mean ────────────────────────────────────────────
# Self-declared first, because a crawler that wants to be found says so and has
# no reason to lie - and one that wants to hide does not put itself on a list.
# That makes the UA trustworthy for exactly this: reading the ones that opted in.
# It is worthless for the rest, which is why it is only the first of three tiers.
AI_UA = re.compile(
    r"claudebot|claude-searchbot|claude-user|gptbot|oai-searchbot|chatgpt-user|"
    r"perplexitybot|perplexity-user|ccbot|bytespider|amazonbot|applebot-extended|"
    r"google-extended|meta-externalagent|meta-externalfetcher|cohere|"
    r"diffbot|omgili|timpibot|youbot", re.I)
SEARCH_UA = re.compile(
    r"googlebot|bingbot|slurp|duckduckbot|baiduspider|yandex(bot|images)|"
    r"applebot|sogou|exabot|facebookexternalhit|twitterbot|linkedinbot|"
    r"telegrambot|whatsapp|discordbot|redditbot|petalbot|seznambot", re.I)
TOOL_UA = re.compile(
    r"curl|wget|python-requests|python-urllib|aiohttp|httpx|go-http-client|"
    r"okhttp|java/|libwww|node-fetch|axios|guzzle|postman|insomnia|"
    r"headlesschrome|phantomjs|puppeteer|playwright|selenium|"
    r"dataprovider|builtwith|checkmarknetwork|pathscan|masscan|zgrab|nuclei|"
    r"uptime|monitor|statuscake|pingdom|steamprofiler-dev-server", re.I)

# A request for one of these is not a reader taking a wrong turn. bans.py
# already answers them with two days; this only needs to label them, so the
# pattern can be looser than one that decides a punishment.
SCAN_PATH = re.compile(
    r"\.env|wp-admin|wp-login|wp-content|xmlrpc|phpmyadmin|/\.git|/\.aws|/\.ssh|"
    r"\.php$|/vendor/|/actuator|/solr|/jenkins|autodiscover|/owa/|struts|"
    r"/config\.|/backup|/dump|\.sql$|/shell", re.I)

# What a browser fetches that nothing else bothers with. A crawler reads the
# HTML and leaves; it does not come back for a woff2, because nothing it does
# needs a typeface. This is the one behavioural signal here that is both cheap
# and hard to fake by accident - and it is still only "likely", because faking
# it on purpose is a few lines of code.
FONT_PATH = re.compile(r"\.woff2?$|\.ttf$|\.otf$", re.I)
ASSET_PATH = re.compile(r"\.css$|\.js$|\.svg$|\.png$|\.jpe?g$|\.ico$", re.I)

# ── The screens ──────────────────────────────────────────────────────
# What the site is made of, as a fixed list of names, and the one place that
# turns an address into one. The path is read here and thrown away: /u/<who>/
# cards is counted as "u.cards" and the <who> never reaches a dict, never mind
# the disk. That is what lets this live in the same module as the visitor half
# without being the join the schema exists to not have - a screen name is the
# same word for everyone who opens it, so a row of this table describes a page
# rather than a person.
#
# Four families, because "which screen" is the question and /u/<who>/franchises
# is a different one from /franchises even though both say franchises: one is
# this library's shelves, the other is the whole catalogue. Separate names,
# separate families, and the panel totals each family on its own.
#
# A path that matches nothing here is not counted at all, and that is
# deliberate rather than lazy. The alternative is an "other" bucket, and on a
# site that answers the open internet the bucket is /wp-admin and /.env within
# a week - the biggest row on the panel, made of traffic nobody wants read.
# The cost is that a new page has to be added below to show up, and these
# patterns are the ones nginx.conf already routes: if the two ever disagree,
# this is the half that silently reads zero.
# The third column is what the address carries besides the name of the screen,
# and there are exactly two answers because there are exactly two kinds of tail
# on this site.
#
#   "appid"   Valve's number for a product. Written down, because "which games
#             do people open" is a question about games and an appid is not a
#             handle for anybody.
#
#   "slug"    A shelf or a post: half-life, valve, facepunch, a blog key, a
#             Steam news id. Never written down - it is held for the length of
#             one visit, in memory, to tell two openings of the same *screen*
#             apart. Without it, reading one franchise page and then another
#             would count once, because both are "cat.franchise".
#
# What is deliberately NOT captured anywhere is the profile: not in /u/<who>,
# not the rival in /u/<who>/vs/<other>. Those are the only parts of any address
# here that are a person, and this module never holds one, not even for the
# half hour the rest of this lives for. The price is one undercount that is
# worth naming: an address that opens the same shelf under two different
# profiles counts once, because the shelf is all that is kept.
SCREENS = (
    # /u/<who> - one library, and every way of looking at it. The main event,
    # so it is first and it is the family with the most in it.
    (re.compile(r"^/u/[^/]+/?$"), "u.dash", None),
    (re.compile(r"^/u/[^/]+/(\d{1,8})/?$"), "u.game", "appid"),
    (re.compile(r"^/u/[^/]+/vs/[^/]+/?$"), "u.versus", None),
    (re.compile(r"^/u/[^/]+/backlog/?$"), "u.backlog", None),
    (re.compile(r"^/u/[^/]+/cards/?$"), "u.cards", None),
    (re.compile(r"^/u/[^/]+/year/(\d{4})/?$"), "u.year", "slug"),
    (re.compile(r"^/u/[^/]+/franchises/?$"), "u.franchises", None),
    (re.compile(r"^/u/[^/]+/franchises/([^/]+)/?$"), "u.franchise", "slug"),
    (re.compile(r"^/u/[^/]+/publishers/?$"), "u.publishers", None),
    (re.compile(r"^/u/[^/]+/publishers/([^/]+)/?$"), "u.publisher", "slug"),
    (re.compile(r"^/u/[^/]+/developers/?$"), "u.developers", None),
    (re.compile(r"^/u/[^/]+/developers/([^/]+)/?$"), "u.developer", "slug"),
    (re.compile(r"^/u/[^/]+/deck/?$"), "u.deck", None),
    (re.compile(r"^/u/[^/]+/embed/?$"), "u.embed", None),
    (re.compile(r"^/u/[^/]+/ids/?$"), "u.ids", None),
    # The catalogue: the same shelves with nobody's library on them.
    (re.compile(r"^/g/(\d{1,8})/?$"), "cat.game", "appid"),
    (re.compile(r"^/franchises/?$"), "cat.franchises", None),
    (re.compile(r"^/franchises/([^/]+)/?$"), "cat.franchise", "slug"),
    (re.compile(r"^/publishers/?$"), "cat.publishers", None),
    (re.compile(r"^/publishers/([^/]+)/?$"), "cat.publisher", "slug"),
    (re.compile(r"^/developers/?$"), "cat.developers", None),
    (re.compile(r"^/developers/([^/]+)/?$"), "cat.developer", "slug"),
    # The blog. The index and a post are two screens, so "they read it" and
    # "they saw it was there" cannot be added up into one number by accident.
    (re.compile(r"^/blog/?$"), "blog.index", None),
    (re.compile(r"^/blog/([^/]+)(?:/[^/]+)?/?$"), "blog.post", "slug"),
    # The site's own pages, in the order somebody meets them.
    (re.compile(r"^/$"), "site.home", None),
    (re.compile(r"^/news/?$"), "site.news", None),
    (re.compile(r"^/news/([^/]+)/?$"), "site.news_post", "slug"),
    (re.compile(r"^/about/?$"), "site.about", None),
    (re.compile(r"^/privacy/?$"), "site.privacy", None),
    (re.compile(r"^/privacy/history/?$"), "site.privacy_log", None),
    (re.compile(r"^/status/?$"), "site.status", None),
    (re.compile(r"^/feedback/?$"), "site.feedback", None),
    (re.compile(r"^/support/?$"), "site.support", None),
    (re.compile(r"^/extension/?$"), "site.extension", None),
    (re.compile(r"^/translate/?$"), "site.translate", None),
    (re.compile(r"^/appeal/?$"), "site.appeal", None),
    (re.compile(r"^/appeal/sent/?$"), "site.appeal_sent", None),
)

# The panel draws them in this order rather than by size, for the reason the
# class bars already are: rows that hold still are rows that can be compared
# with last week's. Built from the list above so there is one list, not two.
SCREEN_ORDER = tuple(name for _, name, _carries in SCREENS)
FAMILIES = ("u", "cat", "blog", "site")

# A cheap no before the list above is walked. The gate runs on every font, every
# icon and every key art, and none of those is a screen: anything under the
# api, the art or the bundle, and anything whose last segment has a file
# extension, is refused here for the price of one match instead of thirty-six.
NOT_A_SCREEN = re.compile(r"^/(?:api|art|assets)/|\.[A-Za-z0-9]{2,5}$")

CLASSES = ("visitor", "ai", "search", "tool", "scanner", "unknown")
# How much the label is worth. "declared" is the crawler's own word for itself,
# "likely" is behaviour, "unsure" is a shrug written down honestly rather than
# rounded up to the nearest confident answer.
CONFIDENCES = ("declared", "likely", "unsure")

_lock = threading.Lock()
_slots = {}
_subjects = {}
_started = False
_flushes = 0
_dropped = 0


@contextmanager
def _connect():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    # NORMAL rather than FULL - see meta.py for the measurement. This one is not
    # re-derivable and still belongs on NORMAL: what a power cut can lose is the
    # counts since the last flush, and a visit counted as zero visits is the
    # error this file was already built to tolerate. Losing the whole week to a
    # 135ms write on the request path is not.
    con.execute("PRAGMA synchronous=NORMAL")
    try:
        with con:
            yield con
    finally:
        con.close()


def init():
    """The tables, and the shape of them is the promise.

    `visitors` has no day column and `subjects` has no visitor column. Neither
    omission is an oversight and neither should be repaired: a day on both
    sides is the join, and the join is the thing this schema exists to not
    have. If a future change needs per-day visitor activity, it needs a reason
    better than convenience, and it needs the privacy policy changed first.

    `screens` has neither a visitor nor a profile in it, for the same reason
    twice over. It says a screen was opened, how many times, on what day, and
    what kind of caller it was - four columns with nobody inside them."""
    with _lock, _connect() as con:
        con.executescript("""
            -- One row per visitor per epoch - and per run of behaviour within
            -- it. `seq` is what makes a class change into a new row instead of
            -- a rewritten one: an address that was answering like a scraper and
            -- starts answering like a person has most likely changed hands, and
            -- adding the second one's visits to the first one's counter would
            -- be the wrong answer twice.
            CREATE TABLE IF NOT EXISTS visitors (
                epoch      INTEGER NOT NULL,
                hash       TEXT    NOT NULL,
                seq        INTEGER NOT NULL DEFAULT 0,
                first_seen INTEGER NOT NULL,
                last_seen  INTEGER NOT NULL,
                hits       INTEGER NOT NULL DEFAULT 0,
                class      TEXT    NOT NULL DEFAULT 'unknown',
                confidence TEXT    NOT NULL DEFAULT 'unsure',
                country    TEXT,
                region     TEXT,
                PRIMARY KEY (epoch, hash, seq)
            );
            CREATE INDEX IF NOT EXISTS visitors_epoch ON visitors (epoch);

            -- How often a profile was looked up, by day. No visitor column,
            -- now or ever.
            CREATE TABLE IF NOT EXISTS subjects (
                day     TEXT    NOT NULL,
                steamid TEXT    NOT NULL,
                hits    INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (day, steamid)
            );
            CREATE INDEX IF NOT EXISTS subjects_day ON subjects (day);

            -- What survives a rotation: counts, and nothing a person is in.
            -- This is the long history, and it is the only part that is kept
            -- indefinitely, because there is nothing in it to keep.
            CREATE TABLE IF NOT EXISTS epochs (
                epoch     INTEGER PRIMARY KEY,
                began_at  INTEGER NOT NULL,
                ended_at  INTEGER NOT NULL,
                visitors  INTEGER NOT NULL DEFAULT 0,
                hits      INTEGER NOT NULL DEFAULT 0,
                by_class  TEXT,
                by_origin TEXT
            );

            -- How many times a screen was opened, by day and by what kind of
            -- caller it was. No visitor column and no profile column, now or
            -- ever: what is in here is the name of a page, which is the same
            -- name for everybody who opens it.
            --
            -- The class is kept because the same row means two different
            -- things depending on it - a hundred openings of /blog by readers
            -- and a hundred by a crawler are not one number - and because the
            -- panel is where the question "are the AI crawlers reading this"
            -- gets answered.
            CREATE TABLE IF NOT EXISTS screens (
                day    TEXT    NOT NULL,
                screen TEXT    NOT NULL,
                class  TEXT    NOT NULL DEFAULT 'unknown',
                hits   INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (day, screen, class)
            );
            CREATE INDEX IF NOT EXISTS screens_day ON screens (day);

            -- The two game screens carry the one thing worth keeping off the
            -- path: which game. An appid is Valve's number for a product, not
            -- a handle for a person, and it is the whole reason this table can
            -- exist beside the promise above - nothing here says who opened it
            -- and there is no column to say it in.
            CREATE TABLE IF NOT EXISTS screen_games (
                day    TEXT    NOT NULL,
                screen TEXT    NOT NULL,
                class  TEXT    NOT NULL DEFAULT 'unknown',
                appid  TEXT    NOT NULL,
                hits   INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (day, screen, class, appid)
            );
            CREATE INDEX IF NOT EXISTS screen_games_day ON screen_games (day);
        """)


def epoch_of(when=None):
    return int((when if when is not None else time.time()) // EPOCH)


def _salt(epoch):
    """The epoch's salt, derived rather than stored.

    Nothing has to hold a salt file, nothing has to remember to rotate one, and
    the rotation cannot be forgotten: it is a division. The seed can rebuild any
    epoch's salt, which would matter if old hashes were still around - they are
    not, because the rotation deletes them."""
    return hmac.new(_seed.encode(), f"epoch:{epoch}".encode(), hashlib.sha256).digest()


def _subject_of(address):
    """What gets hashed. IPv4 whole; IPv6 down to the /64.

    IPv6 privacy extensions rotate a client's address by design, often daily,
    so hashing the whole thing would make every modern visitor a new person
    every day - inflating the count worst for the most up-to-date half of the
    traffic. The /64 is the subscriber's prefix, which is the thing that
    actually persists."""
    raw = (address or "").strip()
    if not raw:
        return "?"
    try:
        ip = ipaddress.ip_address(raw)
    except ValueError:
        return raw[:64]
    if ip.version == 6:
        return str(ipaddress.ip_network(f"{ip}/64", strict=False).network_address)
    return str(ip)


def visitor_hash(address, epoch=None):
    """The handle. Salted per epoch, truncated, and useless a week later."""
    epoch = epoch_of() if epoch is None else epoch
    return hmac.new(_salt(epoch), _subject_of(address).encode(),
                    hashlib.sha256).hexdigest()[:32]


def screen_of(path):
    """Three answers about one path: which screen it is, which game if it is
    one of the two that name one, and what tells it apart from another opening
    of the same screen. (None, None, None) for everything else, which is most
    of what comes through the gate.

    The second is written down and the third never is. They are the same string
    on the game screens and they are different questions: an appid is a fact
    about the site worth keeping, and the third is a comparison that happens in
    memory and is thrown away with the slot.

    Public because the tests read it: the map above is the one part of this
    module that goes stale on its own, quietly, when a route is renamed in
    nginx.conf and nobody thinks to look here."""
    if not path or len(path) > 300 or NOT_A_SCREEN.search(path):
        return None, None, None
    for pattern, name, carries in SCREENS:
        found = pattern.match(path)
        if found:
            tail = found.group(1) if carries else None
            return name, (tail if carries == "appid" else None), tail
    return None, None, None


def _classify(slot):
    """One label and how much it is worth, from what is in memory right now.

    Deliberately readable rather than clever. Every branch here is one the owner
    should be able to check against the panel and disagree with."""
    if slot["scan"]:
        return "scanner", "declared" if slot["scan"] > 1 else "likely"
    ua = slot["ua"] or ""
    if AI_UA.search(ua):
        return "ai", "declared"
    if SEARCH_UA.search(ua):
        return "search", "declared"
    if TOOL_UA.search(ua):
        return "tool", "declared"
    # A page that was actually rendered: stylesheet or script *and* a webfont.
    if slot["fonts"] and slot["assets"]:
        return "visitor", "likely"
    if slot["assets"]:
        return "visitor", "unsure"
    return "unknown", "unsure"


def note(address, path="", ua="", country=None, region=None):
    """One request, counted. Called from the gate, so: on everything.

    This runs for every font, every icon and every key art on every page, which
    is the entire reason it touches no database. It takes the lock for a dict
    update and leaves."""
    now = time.time()
    epoch = epoch_of(now)
    who = visitor_hash(address, epoch)
    is_scan = bool(path and SCAN_PATH.search(path))
    is_font = bool(path and FONT_PATH.search(path))
    is_asset = bool(path and ASSET_PATH.search(path))
    # Outside the lock: it is a handful of regexes and most requests are
    # refused by the first of them.
    screen, game, tail = screen_of(path)
    with _lock:
        _start_locked()
        slot = _slots.get(who)
        if slot is None:
            if len(_slots) >= MAX_TRACKED:
                _evict_locked(now)
            if len(_slots) >= MAX_TRACKED:
                global _dropped
                _dropped += 1
                return
            slot = _slots[who] = {
                "epoch": epoch, "seq": 0, "first": now, "last": now, "hits": 0,
                "flushed": 0, "scan": 0, "fonts": 0, "assets": 0, "ua": "",
                "country": None, "region": None, "class": None, "conf": None,
                "dirty": True,
                # Which screen this address is on, and what it has opened
                # since the last flush. The first is what makes a reload not
                # count, and it is a pair - the screen and whatever tells two
                # openings of it apart - held here and nowhere else. The
                # second is written down and then emptied.
                "screen": None, "views": {},
            }
        slot["last"] = now
        slot["hits"] += 1
        slot["dirty"] = True
        if is_scan:
            slot["scan"] += 1
        if is_font:
            slot["fonts"] += 1
        if is_asset:
            slot["assets"] += 1
        # Last one wins. A slot is one address, and an address that changes its
        # user-agent mid-run is a fact about it worth having rather than an
        # average of two strings.
        if ua:
            slot["ua"] = ua[:300]
        if country:
            slot["country"] = country[:8]
        if region:
            slot["region"] = region[:64]

        was = slot["class"]
        slot["class"], slot["conf"] = _classify(slot)
        # A changed verdict on a slot already written down means the row on disk
        # is about somebody else's traffic. Close it and start a new one rather
        # than letting the two accumulate into a single wrong visitor.
        if was is not None and was != slot["class"] and slot["flushed"]:
            slot["seq"] += 1
            slot["first"] = now
            slot["hits"] = 1
            slot["flushed"] = 0

        # A view is a *change* of screen, which is the whole definition. F5 on
        # the same address is the same screen and is not counted again; going
        # somewhere else and coming back is two changes and is counted twice.
        # Nothing here is a session and nothing here is a person: it is one
        # address's current page, held in memory, gone within the half hour.
        if screen and (screen, tail) != slot["screen"]:
            slot["screen"] = (screen, tail)
            day = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d")
            key = (day, screen, game)
            slot["views"][key] = slot["views"].get(key, 0) + 1


def subject(steamid):
    """A profile was looked up today. Which visitor did it is not recorded, and
    there is nowhere in this schema to record it."""
    if not steamid:
        return
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with _lock:
        _start_locked()
        key = (day, str(steamid)[:32])
        _subjects[key] = _subjects.get(key, 0) + 1


# ── Writing it down ──────────────────────────────────────────────────

def _evict_locked(now):
    """Drop what has gone quiet. Called under the lock, from note()."""
    for key in [k for k, v in _slots.items() if now - v["last"] > IDLE]:
        _slots.pop(key, None)
    if len(_slots) >= MAX_TRACKED:
        oldest = min(_slots, key=lambda k: _slots[k]["last"])
        _slots.pop(oldest, None)


def flush():
    """Write the dirty slots and the day's subject counts, then rotate if the
    epoch turned. Returns what it wrote, for /healthz."""
    global _flushes
    now = time.time()
    with _lock:
        pending = [(h, dict(s)) for h, s in _slots.items() if s["dirty"]]
        subs = dict(_subjects)
        _subjects.clear()
        # The screens every slot has opened since the last time, attributed to
        # what that slot has turned out to be. The class is read now rather
        # than when the page was opened, because when it was opened the answer
        # was usually "unknown": a browser has not asked for a stylesheet or a
        # webfont yet at the moment it asks for the page, and those two are
        # what say it is a browser. A minute later the verdict has settled, and
        # it is the same verdict for every page in that minute.
        views = {}
        for s in _slots.values():
            if not s["views"]:
                continue
            kind = s["class"] or "unknown"
            for (day, screen, game), n in s["views"].items():
                key = (day, screen, kind, game)
                views[key] = views.get(key, 0) + n
            s["views"] = {}
        for h, s in _slots.items():
            if s["dirty"]:
                s["dirty"] = False
                s["flushed"] = 1
        for key in [k for k, v in _slots.items() if now - v["last"] > IDLE]:
            _slots.pop(key, None)
    if not pending and not subs and not views:
        _rotate()
        return {"visitors": 0, "subjects": 0, "screens": 0}
    with _connect() as con:
        for who, s in pending:
            con.execute("""
                INSERT INTO visitors (epoch, hash, seq, first_seen, last_seen,
                                      hits, class, confidence, country, region)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT (epoch, hash, seq) DO UPDATE SET
                    last_seen  = excluded.last_seen,
                    hits       = excluded.hits,
                    class      = excluded.class,
                    confidence = excluded.confidence,
                    country    = COALESCE(excluded.country, visitors.country),
                    region     = COALESCE(excluded.region,  visitors.region),
                    -- Never moved backwards by a later write: the first sighting
                    -- is the first sighting.
                    first_seen = MIN(visitors.first_seen, excluded.first_seen)
            """, (s["epoch"], who, s["seq"], int(s["first"]), int(s["last"]),
                  s["hits"], s["class"] or "unknown", s["conf"] or "unsure",
                  s["country"], s["region"]))
        for (day, sid), hits in subs.items():
            con.execute("""
                INSERT INTO subjects (day, steamid, hits) VALUES (?,?,?)
                ON CONFLICT (day, steamid) DO UPDATE SET hits = hits + excluded.hits
            """, (day, sid, hits))
        for (day, screen, kind, game), hits in views.items():
            con.execute("""
                INSERT INTO screens (day, screen, class, hits) VALUES (?,?,?,?)
                ON CONFLICT (day, screen, class)
                DO UPDATE SET hits = hits + excluded.hits
            """, (day, screen, kind, hits))
            if game:
                con.execute("""
                    INSERT INTO screen_games (day, screen, class, appid, hits)
                    VALUES (?,?,?,?,?)
                    ON CONFLICT (day, screen, class, appid)
                    DO UPDATE SET hits = hits + excluded.hits
                """, (day, screen, kind, game, hits))
    _flushes += 1
    _rotate()
    return {"visitors": len(pending), "subjects": len(subs),
            "screens": len(views)}


def _rotate():
    """Collapse every finished epoch into counts and delete its hashes.

    This is the retention, and it is worth being precise about what it does:
    after it runs there is no identifier left for that week anywhere, so "was
    this the same visitor" is not a question that can be asked of it again -
    not by the owner, not by anyone who takes a copy of the file. What remains
    is how many there were, of what kind, from where.

    Runs on every flush rather than on a schedule, because a schedule is a
    thing that can be off while the service is up."""
    current = epoch_of()
    with _connect() as con:
        old = [r["epoch"] for r in con.execute(
            "SELECT DISTINCT epoch FROM visitors WHERE epoch < ?", (current,))]
        for ep in old:
            rows = con.execute(
                "SELECT * FROM visitors WHERE epoch = ?", (ep,)).fetchall()
            by_class, by_origin = {}, {}
            hits = 0
            for r in rows:
                by_class[r["class"]] = by_class.get(r["class"], 0) + 1
                where = r["country"] or "?"
                if r["region"]:
                    where = f"{where}/{r['region']}"
                by_origin[where] = by_origin.get(where, 0) + 1
                hits += r["hits"]
            con.execute("""
                INSERT INTO epochs (epoch, began_at, ended_at, visitors, hits,
                                    by_class, by_origin)
                VALUES (?,?,?,?,?,?,?)
                ON CONFLICT (epoch) DO UPDATE SET
                    visitors = excluded.visitors, hits = excluded.hits,
                    by_class = excluded.by_class, by_origin = excluded.by_origin
            """, (ep, ep * EPOCH, (ep + 1) * EPOCH, len(rows), hits,
                  json.dumps(by_class, sort_keys=True),
                  json.dumps(by_origin, sort_keys=True)))
            con.execute("DELETE FROM visitors WHERE epoch = ?", (ep,))


def _loop():
    while True:
        time.sleep(FLUSH_EVERY)
        try:
            flush()
        except Exception as exc:                        # noqa: BLE001
            # A census that cannot write is not a reason to stop serving pages.
            print(f"census: flush failed: {exc}", file=sys.stderr)


def _start_locked():
    global _started
    if _started:
        return
    _started = True
    threading.Thread(target=_loop, name="census", daemon=True).start()


# ── Reading it back ──────────────────────────────────────────────────

def report(days=14):
    """Everything the panel shows. Two halves that do not meet."""
    current = epoch_of()
    with _lock:
        live = len(_slots)
    with _connect() as con:
        rows = con.execute("""
            SELECT hash, seq, first_seen, last_seen, hits, class, confidence,
                   country, region
            FROM visitors WHERE epoch = ? ORDER BY last_seen DESC
        """, (current,)).fetchall()
        subs = [dict(r) for r in con.execute("""
            SELECT day, steamid, hits FROM subjects
            WHERE day >= date('now', ?) ORDER BY day DESC, hits DESC
        """, (f"-{int(days)} days",))]
        history = [dict(r) for r in con.execute("""
            SELECT epoch, began_at, ended_at, visitors, hits, by_class, by_origin
            FROM epochs ORDER BY epoch DESC LIMIT 26
        """)]
        screen_rows = [dict(r) for r in con.execute("""
            SELECT day, screen, class, hits FROM screens
            WHERE day >= date('now', ?)
        """, (f"-{int(days)} days",))]
        game_rows = [dict(r) for r in con.execute("""
            SELECT screen, class, appid, SUM(hits) AS hits FROM screen_games
            WHERE day >= date('now', ?)
            GROUP BY screen, class, appid
        """, (f"-{int(days)} days",))]
    for h in history:
        h["by_class"] = json.loads(h["by_class"] or "{}")
        h["by_origin"] = json.loads(h["by_origin"] or "{}")

    by_class, by_country, by_region = {}, {}, {}
    returning = 0
    for r in rows:
        by_class[r["class"]] = by_class.get(r["class"], 0) + 1
        by_country[r["country"] or "?"] = by_country.get(r["country"] or "?", 0) + 1
        key = f"{r['country'] or '?'}/{r['region'] or '?'}"
        by_region[key] = by_region.get(key, 0) + 1
        # "It is them again" - seen across more than a single sitting.
        if r["last_seen"] - r["first_seen"] > IDLE:
            returning += 1

    per_day = {}
    for s in subs:
        per_day[s["day"]] = per_day.get(s["day"], 0) + s["hits"]

    # Rolled up here rather than in the panel, because the panel would be
    # rolling up a few thousand rows in a browser to draw forty bars. The
    # class breakdown survives the roll-up so the panel can still ask for one
    # kind of caller without asking the server again.
    by_screen, screen_days = {}, {}
    for r in screen_rows:
        kinds = by_screen.setdefault(r["screen"], {})
        kinds[r["class"]] = kinds.get(r["class"], 0) + r["hits"]
        day = screen_days.setdefault(r["day"], {})
        day[r["class"]] = day.get(r["class"], 0) + r["hits"]
    games = {}
    for r in game_rows:
        kinds = games.setdefault((r["screen"], r["appid"]), {})
        kinds[r["class"]] = kinds.get(r["class"], 0) + r["hits"]
    screen_games = sorted(
        ({"screen": screen, "appid": appid, "by_class": kinds,
          "hits": sum(kinds.values())} for (screen, appid), kinds in games.items()),
        key=lambda g: -g["hits"])[:40]

    return {
        # Never "unique users". What this counts is addresses that came back
        # inside a week, which is a different thing and the panel says so.
        "epoch": current,
        "epoch_began": current * EPOCH,
        "epoch_ends": (current + 1) * EPOCH,
        "window_days": EPOCH / 86400,
        "seen": len(rows),
        "returning": returning,
        "hits": sum(r["hits"] for r in rows),
        "by_class": by_class,
        "by_country": by_country,
        "by_region": by_region,
        "visitors": [dict(r) for r in rows[:200]],
        "subjects": subs,
        "subject_totals": _totals(subs),
        "per_day": per_day,
        # In the order the map declares them, not by size: a panel whose rows
        # hold still is one that can be read against last week's.
        "screens": [{"screen": name, "by_class": by_screen[name],
                     "hits": sum(by_screen[name].values())}
                    for name in SCREEN_ORDER if name in by_screen],
        "screen_days": screen_days,
        "screen_games": screen_games,
        "screen_window_days": int(days),
        "history": history,
        "live": live,
        "ephemeral": EPHEMERAL,
    }


def _totals(subs):
    out = {}
    for s in subs:
        out[s["steamid"]] = out.get(s["steamid"], 0) + s["hits"]
    return [{"steamid": k, "hits": v}
            for k, v in sorted(out.items(), key=lambda kv: -kv[1])]


def public():
    """The handful of numbers the public status page is allowed to print.

    Everything else in this module answers to the owner and to nobody else.
    What is here is the part that survives a rotation anyway - counts with
    nobody in them - plus the running week, and the names say what they are:
    `addresses`, not "visitors", because an address is what was counted, and a
    carrier network behind one of them is a hundred people while one person on
    wifi and on a phone is two.

    Deliberately not here, and not by omission: the class breakdown, the
    countries, the regions, and every per-profile figure. A weekly total cannot
    be anybody. A country column on a site this size, some weeks, could be."""
    current = epoch_of()
    since = f"-{int(EPOCH // 86400)} days"
    try:
        with _connect() as con:
            now = con.execute(
                "SELECT COUNT(*) AS addresses, COALESCE(SUM(hits), 0) AS requests"
                " FROM visitors WHERE epoch = ?", (current,)).fetchone()
            looked = con.execute(
                "SELECT COALESCE(SUM(hits), 0) AS n FROM subjects"
                " WHERE day >= date('now', ?)", (since,)).fetchone()
            weeks = con.execute(
                "SELECT began_at, visitors, hits FROM epochs"
                " ORDER BY epoch DESC LIMIT 12").fetchall()
    except sqlite3.Error:
        return None
    return {
        "window_days": EPOCH / 86400,
        "began_at": current * EPOCH,
        "addresses": now["addresses"],
        "requests": now["requests"],
        "lookups": looked["n"],
        # Whole weeks that have already closed. The one in progress is the
        # block above and is not in here, because half a week drawn beside
        # twelve whole ones reads as a collapse in traffic.
        "weeks": [{"began_at": r["began_at"], "addresses": r["visitors"],
                   "requests": r["hits"]} for r in weeks],
    }


def state():
    """For /healthz. Counts, never a hash."""
    with _lock:
        return {
            "tracked": len(_slots),
            "pending_subjects": len(_subjects),
            "pending_views": sum(len(s["views"]) for s in _slots.values()),
            "flushes": _flushes,
            "dropped": _dropped,
            "epoch": epoch_of(),
            "window_days": EPOCH / 86400,
            "ephemeral": EPHEMERAL,
        }
