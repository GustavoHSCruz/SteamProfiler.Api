#!/usr/bin/env python3
"""The only thing on steamprofiler.org that talks to Steam.

The browser never sees the API key: it asks this service, this service asks
Steam. Everything is a GET, everything is cached, and nothing is written to disk.

    GET  /healthz
    GET  /owner                     -> who runs the site (footer credit)
    GET  /resolve?q=<anything>      -> a steamid64
    GET  /profile?id=<steamid64>    -> the dashboard payload
    GET  /game?id=<steamid64>&appid=<n>
    GET  /player?appid=<n>          -> versioned, CORS-enabled trailer payload
    GET  /companion?appid=<n>       -> versioned, CORS-enabled store companion
    GET  /meta?id=<steamid64>       -> prices and genres, out of the store cache
    GET  /unlocks?id=<steamid64>    -> one scan of the top of the library: the
                                       rarest unlocks, what is closest to a
                                       hundred percent, and the unlocks per year
                                       (also answers to its old name, /rarities)
    GET  /mates?id=<steamid64>      -> the friend list weighed against this
                                       library: who has the bigger clock, and
                                       which games nobody else on it owns

    GET  /board                     -> the approved messages, with vote counts
    POST /feedback                  -> leave a message
    POST /vote                      -> toggle a vote on a board item
    GET  /support                   -> the donation channels that are configured

    GET  /bars.svg?q=<anything>     the embeds: a chart, a strip, a badge. Every
    GET  /banner.svg?q=<anything>   one of them self-contained, so it survives
    GET  /badge.svg?q=<anything>    being pasted somewhere we do not control
    GET  /bars.txt?q=<anything>     the same chart for a place that takes no
                                    picture at all

    GET  /blog?lang=<xx>            -> the published posts, newest first
    GET  /blog/post?key=&lang=      -> one post, in the reader's language or the
                                       original, and which of the two it is
    GET  /blog/meta?key=            -> the <head> of a post, as HTML, for nginx
    GET  /sitemap.xml               -> the pages and the posts, for crawlers
    POST /blog/vote                 -> toggle a vote on a post

    GET  /admin/census              -> the traffic count, owner only
    GET  /admin/inbox               -> everything, owner only
    POST /admin/update              -> set a status or write a reply
    POST /admin/delete              -> drop a message
    GET  /admin/blog                -> every post, drafts included
    POST /admin/blog/save           -> write one
    POST /admin/blog/delete         -> drop one

The admin routes want `Authorization: Bearer <ADMIN_TOKEN>`, and nginx also keeps
them on the local network. Two locks on the same door, because the token travels
in clear text until TLS is in front of this.

fetch.py keeps the subject of a lookup in module state, so every build runs under
one lock. That serialises the rare uncached lookup, which is the right trade for
a site this size: the cache absorbs everything else, and two visitors asking for
the same cold profile means the second one waits and then gets a cache hit.

Every route that can spend the Steam key goes through guard.py first, which
prices the request, counts how many distinct profiles one address has asked
for, and keeps the day's spend under a budget. A request that is already in the
cache is priced as what it is - cheap - so the site keeps answering from what it
knows even when it has stopped being allowed to learn anything new.
"""

import html
import json
import os
import re
import secrets
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import art
import bans
import blog
import cards
import community
import embed
import census
import fetch
import fx
import houses
import proton
import guard
import inv
import meta
import og
import store
import support

PORT = int(os.environ.get("PORT", "8000"))
# How long a lookup stays fresh. Playtime does not move fast enough to care.
TTL = int(os.environ.get("CACHE_TTL", "900"))
# Current players is a live reading. The expensive catalogue and achievement
# parts beneath it have their own longer caches, so rebuilding this envelope is
# cheap and keeps the number useful.
PUBLIC_GAME_TTL = 5 * 60
PUBLIC_GAME_NEGATIVE_TTL = 30 * 24 * 3600
# How long the *browser* may hold one, which is a different question. Serving a
# warm payload costs ~15 ms, so there is nothing to save by letting a visitor
# keep it for the full quarter of an hour - and a shorter hold is what stops a
# reload after a deploy from being answered out of a cache the deploy did not
# reach. The service still only rebuilds every TTL.
CLIENT_TTL = int(os.environ.get("CLIENT_TTL", "60"))
OWNER_TTL = int(os.environ.get("OWNER_TTL", "1800"))
# Hard ceiling so a stream of lookups cannot grow the process without bound.
MAX_ENTRIES = int(os.environ.get("CACHE_MAX", "400"))
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "").strip()
# How far down the library the rarity scan goes. Three calls per game, all of
# them under the one build lock, so this number is also how long every other
# cold lookup waits behind it. Twelve is about ten seconds.
RARITY_GAMES = int(os.environ.get("RARITY_GAMES", "12"))
# Whether /econ is published at all. Off by default: it can only ever show this
# server's own trading, which most people running this have no reason to put on
# a page.
ECON_PANEL = os.environ.get("ECON_PANEL") == "1"
# How many friends the mates scan reaches. One call each, and unlike the rarity
# scan they are one call and not three - but they are calls about somebody who
# is not the subject of the page, so the ceiling is lower than the arithmetic
# would allow. Twenty is a strip's worth of people and a fifth of the price of
# a rarity scan.
MATE_FRIENDS = int(os.environ.get("MATE_FRIENDS", "20"))
# Which renderer each embed path asks for. A table rather than four branches,
# because the handler below is the same seven lines for all of them.
EMBEDS = {"/bars.svg": "bars", "/banner.svg": "banner",
          "/badge.svg": "badge", "/bars.txt": "text"}

# Bodies are tiny; anything larger is not a message.
MAX_BODY = 8 * 1024
# Except one, and it is the only write here that is not a message. A post is
# prose in up to three languages and blog.py allows forty thousand characters
# of each, which in Cyrillic is two bytes a character before JSON escaping, so
# the ceiling meant for a comment box refused posts the editor was built to
# hold - and refused them in the comment box's words. This is that number with
# room around it. Kept as its own name rather than raising MAX_BODY, because
# what may send this much is the owner behind the token and nobody else.
MAX_POST_BODY = 512 * 1024
# A form filled faster than this was not filled by a person reading it.
MIN_SECONDS = 3

_lock = threading.Lock()
_cache = {}
_public_negative = {}


def peek(key):
    """Whether a live entry is already in hand.

    The gate has to price a request before it runs, and the only thing that
    makes a lookup expensive is not having it yet."""
    hit = _cache.get(key)
    return bool(hit and hit[0] > time.time())


def cached(key, ttl, produce):
    """Return the cached value for `key`, building it under the lock if stale.

    The lock is held across produce() on purpose - see the module docstring.
    guard.Cold caps how many threads may be queued up behind it, because every
    one of them is a connection held open for as long as the queue is long.

    **produce() must not call cached().** The lock is a plain Lock, so a
    nested call waits for a lock its own thread already holds and never wakes
    up; every request after it stacks in guard.Cold and the api answers busy
    to everything until the process is restarted. A route that needs a profile
    resolves it before it gets here and closes over the result."""
    now = time.time()
    hit = _cache.get(key)
    if hit and hit[0] > now:
        return hit[1]
    with guard.Cold(), _lock:
        # Someone may have filled it while this thread waited for the lock.
        hit = _cache.get(key)
        if hit and hit[0] > time.time():
            return hit[1]
        value = produce()
        if len(_cache) >= MAX_ENTRIES:
            for old in sorted(_cache, key=lambda k: _cache[k][0])[: MAX_ENTRIES // 4]:
                _cache.pop(old, None)
        lifetime = ttl(value) if callable(ttl) else ttl
        _cache[key] = (time.time() + lifetime, value)
        return value


def cached_value(key):
    """A live cached value, or None, without ever producing a cold one."""
    hit = _cache.get(key)
    return hit[1] if hit and hit[0] > time.time() else None


class Fail(Exception):
    """An answer the visitor should see, with the status it deserves."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def do_resolve(query):
    def produce():
        sid, vanity = fetch.resolve(query)
        if not sid:
            raise Fail(404, "@err.not_found")
        return {"steamid": sid, "vanity": vanity}

    return cached(f"r:{query.lower()}", OWNER_TTL, produce)


def do_profile(steamid):
    def produce():
        fetch.set_user(steamid)
        try:
            return fetch.build_profile()
        except fetch.SteamError as e:
            raise Fail(403, str(e))

    return cached(f"p:{steamid}", TTL, produce)


def do_game(steamid, appid):
    profile = do_profile(steamid)
    row = next((g for g in profile["top_games"] if g["appid"] == appid), None)
    if row is None:
        row = next((g for g in profile["library"] if g["appid"] == appid), None)
    if row is None:
        # Owned and never launched. `library` only holds games with hours on
        # them, so until now every one of these answered "not in this library"
        # - including every link on the pile page, which is a page made
        # entirely of them. They get a page: it has no hours because there are
        # none, and that is the whole point of it being in the pile.
        never = next((g for g in profile["unplayed"] if g["appid"] == appid), None)
        if never is not None:
            row = {"appid": appid, "name": never["name"], "rank": None,
                   "hours": 0, "share": 0, "last_played": None,
                   "linux_minutes": 0, "minutes_2weeks": 0,
                   "os": {"windows": 0, "linux": 0, "mac": 0, "deck": 0},
                   "themed": appid in fetch.GAME_LAYOUTS, "never": True}
    if row is None:
        raise Fail(404, "@err.game_absent")

    def produce():
        fetch.set_user(steamid)
        try:
            out = fetch.build_game(appid, row)
        except fetch.SteamError as e:
            raise Fail(403, str(e))
        # The same verdict as the public page carries, for the same reason:
        # it is about the game. The hours beside it are the only part of this
        # page that is about the reader.
        proton.want([appid])
        out["proton"] = proton.lookup([appid]).get(appid)
        return out

    return cached(f"g:{steamid}:{appid}", TTL, produce)


def do_deck(profile):
    """What a library would run on Linux, game by game, out of the local cache.

    Takes the profile rather than a steamid, and that is not a style choice:
    this runs inside cached()'s produce(), which holds a lock that is not
    reentrant, and do_profile() is itself a cached() call. Resolving the
    profile in here deadlocks the process - the thread waits for a lock it is
    already holding, every later request piles up behind it, and the api
    answers "busy" to everything until it is restarted. The caller resolves
    the profile first, outside the lock, and hands it in.

    Never waits on ProtonDB. What is known is answered now and what is not is
    queued for the worker, so a library nobody has looked up before comes back
    thin and is complete a few minutes later - the same bargain the money
    panel makes with the storefront, for the same reason."""
    library = profile.get("library") or []
    unplayed = profile.get("unplayed") or []

    ids = [g["appid"] for g in library] + [g["appid"] for g in unplayed]
    proton.want(ids)
    known = proton.lookup(ids)

    deck_min = 0
    linux_min = 0
    rows = []
    for game in library:
        os_min = game.get("os") or {}
        deck_min += os_min.get("deck") or 0
        linux_min += os_min.get("linux") or 0
        seen = known.get(game["appid"])
        if not seen or not seen.get("tier"):
            continue
        rows.append({
            "appid": game["appid"], "name": game.get("name"),
            "hours": game.get("hours"),
            # Deck is a slice of Steam's Linux clock rather than a fifth OS,
            # which is why both are carried and neither is added to the other.
            "deck": os_min.get("deck") or 0,
            "linux": os_min.get("linux") or 0,
            "tier": seen["tier"], "total": seen.get("total"),
        })
    for game in unplayed:
        seen = known.get(game["appid"])
        if not seen or not seen.get("tier"):
            continue
        rows.append({
            "appid": game["appid"], "name": game.get("name"),
            "hours": 0, "deck": 0, "linux": 0,
            "tier": seen["tier"], "total": seen.get("total"),
        })

    rows.sort(key=lambda r: -(r["hours"] or 0))
    rated = len(rows)
    if len(rows) > DECK_MAX:
        rows = rows[:DECK_MAX]
    return {
        "persona": (profile.get("profile") or {}).get("persona"),
        "deck_hours": round(deck_min / 60, 1),
        # Steam's Linux clock already contains the Deck's, so the two are
        # subtracted rather than printed side by side: two figures that
        # overlap read as a total, and this pair would read as double.
        "linux_hours": round(max(0, linux_min - deck_min) / 60, 1),
        "games": rows,
        # Said out loud, because a verdict this site has not fetched yet looks
        # exactly like a game nobody has reported on, and those are different.
        "coverage": {"owned": len(ids), "asked": len(known), "rated": rated,
                     "shown": len(rows)},
    }


def do_house(kind, slug):
    """One company, with the years filled in from this site's own store cache.

    The index knows which apps a company has; it deliberately does not know
    when they came out. That is the one field where the catalogue source and
    this server could disagree, so the answer is simply the store cache's: a
    game meta.py has read carries its year, and a game it has not carries
    none. The screen shows the difference rather than papering over it."""
    out = houses.shelf(kind, slug)
    if out is None:
        raise Fail(404, "@err.no_house")

    apps = out["apps"]
    out["shown"] = len(apps)
    if len(apps) > HOUSE_MAX:
        apps = apps[:HOUSE_MAX]
        out["apps"] = apps
        out["shown"] = HOUSE_MAX

    known = meta.lookup([a["appid"] for a in apps])
    read = 0
    for app in apps:
        row = known.get(app["appid"]) or {}
        catalog = row.get("catalog") or {}
        app["year"] = row.get("year")
        # This site's own name for the game wins where it has one: it is the
        # storefront's, in the storefront's spelling, fetched by this server.
        name = catalog.get("name") or row.get("name")
        if name:
            app["name"] = name
        if row.get("detailed"):
            read += 1
    # How much of this shelf the site has actually been to the store about.
    # The screen prints it rather than letting a wall of games with no year
    # look like a wall of games with no release date.
    out["read"] = read
    return out


def do_public_game(appid, cc, language="en"):
    """One game's public facts, with a persistent-store-backed negative."""
    language = meta.language_of(language)
    key = f"pg:{appid}:{cc}:{language}"
    if _public_negative.get(appid, 0) > time.time():
        raise Fail(404, "@err.game_not_found")

    def produce():
        try:
            payload = fetch.build_public_game(appid, cc, language=language)
        except fetch.GameNotFound:
            _public_negative[appid] = time.time() + PUBLIC_GAME_NEGATIVE_TTL
            raise Fail(404, "@err.game_not_found")
        except fetch.SteamError as e:
            raise Fail(503, str(e))
        store = payload.get("store") or {}
        if (store.get("state") == "absent" and store.get("detailed") and
                payload.get("achievements") is None):
            meta.mark_public_absent(appid)
            _public_negative[appid] = time.time() + PUBLIC_GAME_NEGATIVE_TTL
            raise Fail(404, "@err.game_not_found")
        # Whether it runs on Linux is a fact about the game and not about
        # anybody, so it belongs on the page about the game rather than only
        # behind a profile. Absent until the queue reaches it, and absent is
        # one of the states the short ttl below exists for.
        proton.want([appid])
        payload["proton"] = proton.lookup([appid]).get(appid)
        return payload

    return cached(key, lambda value: (30 if (
        (value.get("store") or {}).get("state") == "unknown" or
        not (value.get("store") or {}).get("detailed") or
        not value.get("catalog") or not value.get("reviews") or
        value.get("proton") is None
    ) else PUBLIC_GAME_TTL), produce)


# How many apps one /apps call may name, and how many of those may be asked
# about live. The first is a read of SQLite and is cheap at any size the front
# actually sends - the biggest franchise screen names eleven - so the cap is
# there to bound the query, not to ration it. The second bounds calls to Steam,
# which is the half that costs something: one per app, cached five minutes.
APPS_MAX = 120
APPS_LIVE_MAX = 12

# The house index and one house's shelf. Half an hour, because the table under
# them is rewritten once a week: anything shorter is asking SQLite the same
# question again for an answer that provably has not moved.
HOUSES_TTL = 1800
# The longest shelf an answer carries. Some catalogue publishers have thousands
# of apps and nobody reads a list that long, but the browser would still be
# handed all of it. The screen says when it was cut.
HOUSE_MAX = 2000
# The longest deck list an answer carries. A library of five thousand with a
# verdict on most of it is a payload nobody reads to the end; the screen says
# when it was cut.
DECK_MAX = 800


def do_apps(appids, cc, live=()):
    """What the store cache knows about a list of apps, in one storefront.

    The franchise screens are the caller: they hold their own list of appids
    and need the shop's half of each one - the name it goes by today, when it
    arrived here, what it costs, and the trailer's id. Nothing on this payload
    is indexed by a steamid, and nothing on it is about a person.

    Never fetches the catalogue inline. A miss is answered `known: false` and
    queued, so a cold franchise draws immediately with the years it already had
    and fills in behind the reader. That is the same bargain the money panel
    makes, and for the same reason: a screen somebody is reading beats a screen
    that is complete.

    Reviews are the one field that arrives only by luck. The background crawl
    fills catalogue and price for anything asked about; review totals are
    fetched by the public game page and by nothing else. So they show up here
    for the games somebody has opened, and are absent for the rest, and the
    screen prints what it has rather than holding a row back for it."""
    known = meta.lookup(appids, cc)
    cold = [appid for appid in appids if not (known.get(appid) or {}).get("detailed")]
    if cold:
        meta.want(cold, (cc,))

    out = {}
    for appid in appids:
        row = known.get(appid) or {}
        catalog = row.get("catalog") or {}
        movies = catalog.get("movies") or []
        # The one the store leads with, and the first otherwise. A game whose
        # catalogue has not been read yet has neither, and gets no button.
        movie = next((m for m in movies if m.get("highlight")), movies[0] if movies else None)
        trailer = None
        if movie and movie.get("id"):
            mp4 = movie.get("mp4") or {}
            webm = movie.get("webm") or {}
            trailer = {
                "id": movie.get("id"), "name": movie.get("name"),
                "thumb": movie.get("thumbnail"),
            }
            for key, value in (
                    ("max_mp4", mp4.get("max")), ("max_webm", webm.get("max")),
                    ("sd_mp4", mp4.get("480")), ("sd_webm", webm.get("480")),
                    ("hls", movie.get("hls_h264")), ("dash", movie.get("dash_h264"))):
                if value:
                    trailer[key] = value
            # Rows written before stream manifests were preserved still have
            # an id and thumbnail. Refresh those in the background once; the
            # next page load receives the real maximum-quality addresses.
            if not any(key in trailer for key in
                       ("max_mp4", "max_webm", "sd_mp4", "sd_webm", "hls", "dash")):
                meta.want_media([appid])
        reviews = row.get("reviews") or {}
        total = reviews.get("total") or 0
        out[str(appid)] = {
            "name": catalog.get("name") or row.get("name"),
            # Steam's own date, as Steam wrote it, plus the year out of it. The
            # string is in the storefront's language and is only ever printed;
            # the year is the part anything compares against.
            "released": (catalog.get("release") or {}).get("date"),
            "year": row.get("year"),
            "free": row.get("free"),
            "price": row.get("price"),
            "initial": row.get("initial"),
            "currency": row.get("currency"),
            "discount": row.get("discount"),
            "reviews": ({
                "total": total,
                "positive": reviews.get("positive"),
                "positive_pct": round((reviews.get("positive") or 0) * 100 / total, 1),
                "description": reviews.get("description"),
            } if total else None),
            "trailer": trailer,
            "known": bool(row.get("detailed")),
        }

    for appid in live:
        try:
            out[str(appid)]["players"] = fetch.fetch_current_players(appid).get("players")
        except fetch.SteamError:
            # One count nobody gets is a line that does not appear. It is not
            # worth failing a screen that is otherwise complete.
            pass

    return {
        # Stamped with `time`, which is what this module imports. Every other
        # payload's `generated_at` is written by fetch.py or meta.py out of
        # datetime; this one is produced here, and the two shapes parse to the
        # same instant in the browser.
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "cc": meta.cc_of(cc),
        "apps": out,
    }


def do_player(appid):
    """The small, stable media envelope third-party players may consume.

    `/apps` belongs to the franchise screens and is allowed to grow with them.
    A player needs much less: a title, a poster and the addresses Steam exposes
    for one highlighted trailer.  Keeping that contract here stops an embed
    from depending on an internal storefront payload.

    A cold app is returned as ``pending`` and queued by do_apps().  That is an
    answer rather than a long request held open while the store is thinking;
    callers may retry after the response cache expires.
    """
    row = do_apps([appid], "us", live=())["apps"][str(appid)]
    trailer = row.get("trailer")
    if not row.get("known"):
        state = "pending"
    elif not trailer:
        state = "absent"
    else:
        state = "ready"

    media = None
    if trailer:
        # Arrays leave room for another codec without changing the contract.
        # Maximum quality is deliberately first, matching the site player.
        mp4 = [url for url in (trailer.get("max_mp4"), trailer.get("sd_mp4")) if url]
        webm = [url for url in (trailer.get("max_webm"), trailer.get("sd_webm")) if url]
        # Older catalogue rows know the movie id but predate the exact source
        # URLs. Steam's conventional filenames keep those rows playable while
        # the urgent detail refresh queued by do_apps() fills the real ones.
        if not mp4 and not webm and not trailer.get("hls"):
            base = f"https://cdn.cloudflare.steamstatic.com/steam/apps/{trailer['id']}"
            mp4 = [f"{base}/movie_max.mp4", f"{base}/movie480.mp4"]
            webm = [f"{base}/movie_max_vp9.webm", f"{base}/movie480_vp9.webm"]
        media = {
            "hls": trailer.get("hls"),
            "dash": trailer.get("dash"),
            "mp4": mp4,
            "webm": webm,
        }

    return {
        "version": 1,
        "appid": appid,
        "state": state,
        "title": (trailer or {}).get("name") or row.get("name"),
        "poster": (trailer or {}).get("thumb"),
        "media": media,
        "store_url": f"https://store.steampowered.com/app/{appid}",
        "attribution": {
            "label": "steamprofiler.org",
            "url": "https://steamprofiler.org/",
        },
    }


def do_companion(appid, language="en"):
    """The small public game envelope consumed by the browser extension.

    The public game page also knows achievements and news, but collecting them
    here would make opening an ordinary Steam store page spend calls on facts
    the companion never draws. Catalogue, reviews and the live count have
    independent caches, so this endpoint stays useful without inheriting the
    site's internal payload or its cost.
    """
    row = meta.public_catalog(appid, "us", language)
    catalog = row.get("catalog") or {}

    if row.get("exists") is False:
        state = "absent"
    elif not catalog:
        state = "pending"
    else:
        state = "ready"

    players = None
    if state == "ready":
        try:
            players = fetch.fetch_current_players(appid).get("players")
        except fetch.SteamError:
            # The live count is an enhancement to an enhancement. A Steam
            # outage should leave the catalogue and the link usable rather
            # than fail all of the extension panel.
            pass

    reviews = row.get("reviews") or {}
    total = reviews.get("total") or 0
    movies = catalog.get("movies") or []
    movie = next((item for item in movies if item.get("highlight")),
                 movies[0] if movies else None)
    trailer = None
    if movie and movie.get("id"):
        mp4 = movie.get("mp4") or {}
        webm = movie.get("webm") or {}
        mp4_sources = [url for url in (mp4.get("max"), mp4.get("480")) if url]
        webm_sources = [url for url in (webm.get("max"), webm.get("480")) if url]
        if not mp4_sources and not webm_sources and not movie.get("hls_h264"):
            base = f"https://cdn.cloudflare.steamstatic.com/steam/apps/{movie['id']}"
            mp4_sources = [f"{base}/movie_max.mp4", f"{base}/movie480.mp4"]
            webm_sources = [f"{base}/movie_max_vp9.webm", f"{base}/movie480_vp9.webm"]
        trailer = {
            "state": "ready",
            "title": movie.get("name") or catalog.get("name"),
            "poster": movie.get("thumbnail"),
            "media": {
                "hls": movie.get("hls_h264"),
                "dash": movie.get("dash_h264"),
                "mp4": mp4_sources,
                "webm": webm_sources,
            },
        }
    images = catalog.get("images") or {}
    return {
        "version": 1,
        "appid": appid,
        "state": state,
        "game": {
            "name": catalog.get("name") or row.get("name"),
            "released": (catalog.get("release") or {}).get("date"),
            "year": row.get("year"),
            "free": row.get("free"),
            "image": images.get("header") or images.get("capsule"),
            "platforms": catalog.get("platforms") or {},
        },
        "reviews": ({
            "total": total,
            "positive": reviews.get("positive"),
            "positive_pct": round((reviews.get("positive") or 0) * 100 / total, 1),
            "description": reviews.get("description"),
        } if total else None),
        "players": players,
        "trailer": trailer or {
            "state": "pending" if state == "pending" else "absent",
            "title": None,
            "poster": None,
            "media": None,
        },
        "links": {
            "analysis": f"https://steamprofiler.org/g/{appid}",
            "store": f"https://store.steampowered.com/app/{appid}/",
        },
        "attribution": {
            "label": "steamprofiler.org",
            "url": "https://steamprofiler.org/",
        },
    }


def do_cards(steamid):
    """The card collection. One Steam call on top of the profile this already
    has, and two reads off disk - see fetch.build_cards for which is which."""
    profile = do_profile(steamid)

    def produce():
        fetch.set_user(steamid)
        try:
            return fetch.build_cards(profile["library"], profile["unplayed"])
        except fetch.SteamError as e:
            raise Fail(403, str(e))

    # Shorter than a profile. Half of this payload is the store cache and the
    # market cache filling in behind it, and the page is worth refreshing while
    # somebody has it open - which is exactly what /meta does for the money.
    return cached(f"c:{steamid}", 120, produce)


def do_workshop(steamid):
    """What a profile published to the Workshop. Keyless, and only ever asked
    for when the count on the profile said there was something to ask about."""
    def produce():
        fetch.set_user(steamid)
        try:
            return fetch.build_workshop()
        except fetch.SteamError as e:
            raise Fail(403, str(e))

    return cached(f"ws:{steamid}", TTL, produce)


def do_econ(steamid):
    """The owner's own trading. See fetch.build_econ for why this cannot be
    about anybody else."""
    def produce():
        fetch.set_user(steamid)
        return fetch.build_econ()

    return cached(f"e:{steamid}", 300, produce)


def do_wishlist(steamid, cc):
    """What a profile wants and what it follows. Independent of the profile:
    the wishlist names its own appids, so this never builds one, which is why it
    is a route of its own rather than another block on the dashboard payload.

    The country is in the key for the same reason it is in /meta's: three
    readers in three languages want three different totals, and they must not be
    served each other's."""
    def produce():
        fetch.set_user(steamid)
        try:
            return fetch.build_wishlist(cc)
        except fetch.SteamError as e:
            raise Fail(403, str(e))

    # Short, like /cards: most of this payload is the store cache filling in
    # behind it, and it is worth re-reading while somebody has the page open.
    return cached(f"w:{steamid}:{cc}", 120, produce)


def do_meta(steamid, cc):
    """Prices and genres, recomputed from the store cache without touching
    Steam. The dashboard asks for this again while the page is open, because
    meta.py is still filling the cache in the background and the panel would
    otherwise be stuck at whatever coverage the profile build happened to see."""
    profile = do_profile(steamid)

    def produce():
        return fetch.build_economics(profile["library"], profile["unplayed"],
                                     with_apps=True, cc=cc)

    # Short: the whole point is that the answer changes minute by minute. It is
    # a read of one SQLite table either way, and never a call to anyone.
    # The country is in the key: three readers in three languages want three
    # different totals, and they must not be served each other's.
    return cached(f"m:{steamid}:{cc}", 10, produce)


def do_rarities(steamid):
    profile = do_profile(steamid)
    rows = profile["top_games"][:RARITY_GAMES]

    def produce():
        fetch.set_user(steamid)
        try:
            return fetch.build_rarities(rows)
        except fetch.SteamError as e:
            raise Fail(403, str(e))

    return cached(f"x:{steamid}", TTL, produce)


def do_mates(steamid):
    """The friend list weighed against this library. One call per friend, so it
    is a button like the rarity scan and priced like one.

    A profile whose friend list is private has nothing to scan, and that is a
    404 rather than an empty answer: the panel is not drawn at all in that
    case, and a page that asked for this anyway asked for something that does
    not exist here."""
    profile = do_profile(steamid)
    people = ((profile.get("friend_list") or {}).get("people")) or []
    if not people:
        raise Fail(404, "@err.no_friends")
    rows = profile["library"]

    def produce():
        fetch.set_user(steamid)
        try:
            return fetch.build_mates(rows, people, MATE_FRIENDS)
        except fetch.SteamError as e:
            raise Fail(403, str(e))

    return cached(f"f:{steamid}", TTL, produce)


def do_owner():
    return cached("owner", OWNER_TTL, fetch.build_owner)


def do_blocks():
    """Everything about shut-out addresses, keyed by address rather than by row.

    This is the shape the panel needed and did not have. Two appeals from one
    address were two unrelated cards, and the ban they were about was a third
    card somewhere below saying "/.env" and nothing else - three fragments of
    one story, none of which could be acted on without the other two.

    So the join happens here, in the one module that can see both a message and
    a ban, and what comes out is one entry per address: what it tried, when,
    whether it has been let back in before, and everything it has written."""
    groups = {}

    def slot(who):
        if who not in groups:
            groups[who] = {"ip_hash": who, "ban": None, "appeals": []}
        return groups[who]

    for row in bans.recent():
        slot(row["ip_hash"])["ban"] = row
    for msg in store.appeals():
        who = msg.get("ip_hash")
        if not who:
            continue
        entry = slot(who)
        if entry["ban"] is None:
            # Banned once, already let back in or already expired. Still worth
            # showing: it is the history behind whatever they wrote.
            entry["ban"] = bans.by_hash(who)
        entry["appeals"].append(msg)

    def rank(entry):
        ban = entry["ban"] or {}
        unread = any(m["status"] == "novo" for m in entry["appeals"])
        return (
            0 if ban.get("reason") == "repeat" else 1,   # repeats first
            0 if unread else 1,                          # then anything unanswered
            0 if ban.get("active") or ban.get("until") else 1,
            -(ban.get("until") or 0),
        )

    return {"items": sorted(groups.values(), key=rank), "state": bans.state()}


class Handler(BaseHTTPRequestHandler):
    server_version = "steamprofiler"
    sys_version = ""

    @property
    def caller(self):
        """The visitor's address as nginx saw it. Only ever hashed, never stored."""
        return self.headers.get("X-Real-IP") or self.client_address[0]

    def gate(self, kind, key=None, subject=None):
        """Price this request and let it through, or raise. `key` is the cache
        entry that would satisfy it: present means the answer is already here
        and costs nothing but the bytes."""
        guard.afford(self.caller, kind, subject, is_cold=not (key and peek(key)))
        # Counted after the gate agreed, so a refused request is not a lookup.
        # The caller is deliberately not passed: census.subject() takes the
        # profile and nothing else, because the whole design is that these two
        # facts are never written down together. This is the one line where
        # both are in scope at once - keep it that way.
        if subject:
            census.subject(subject)

    def read_json(self, limit=MAX_BODY):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            raise Fail(400, "@err.empty_body")
        if length > limit:
            raise Fail(413, "@err.too_big")
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise Fail(400, "@err.bad_body")

    def require_admin(self):
        if not ADMIN_TOKEN:
            raise Fail(503, "@err.no_admin_token")
        header = self.headers.get("Authorization", "")
        token = header[7:] if header.startswith("Bearer ") else ""
        # Constant time, so a wrong token leaks nothing about the right one.
        if not secrets.compare_digest(token, ADMIN_TOKEN):
            raise Fail(401, "@err.bad_token")

    @staticmethod
    def lang(raw):
        """Which of the three languages the reader asked for.

        Checked against a closed set rather than trusted, because it reaches a
        database lookup - and it is the one parameter on this service that
        exists at all, since everything else the API says travels as a key the
        browser resolves. Prose cannot: somebody has to have written it."""
        code = (raw or "")[:2].lower()
        return code if code in blog.LANGS else "en"

    def send_jpeg(self, body, ttl):
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", f"public, max-age={ttl}, immutable")
        self.end_headers()
        self.wfile.write(body)

    def send_png(self, body, ttl):
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", f"public, max-age={ttl}")
        self.end_headers()
        self.wfile.write(body)

    def send_svg(self, body, ttl):
        """An embed. `Access-Control-Allow-Origin` because these are meant to be
        read from somebody else's page - as an <img>, which needs no permission,
        but also by a script that wants the file itself. There is nothing here
        to protect: it is one public profile, drawn, and no request to this
        service ever carries a cookie."""
        body = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "image/svg+xml; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", f"public, max-age={ttl}")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def send_text(self, body, ttl):
        body = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", f"public, max-age={ttl}")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def send_redirect(self, location):
        """303, so the browser follows with a GET and a reload of the landing
        page does not post the form a second time."""
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def read_form(self):
        """A urlencoded body. The appeal form is plain HTML with no script
        behind it, so what arrives is a form post and not the JSON every other
        write on this service speaks."""
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            raise Fail(400, "@err.empty_body")
        if length > MAX_BODY:
            raise Fail(413, "@err.too_big")
        raw = self.rfile.read(length).decode("utf-8", "replace")
        pairs = parse_qs(raw, keep_blank_values=True)
        return {k: (v[0] if v else "") for k, v in pairs.items()}

    def send_empty(self, status):
        """A status and nothing else. What auth_request wants: nginx reads the
        code and throws the body away, so producing one is pure waste on a
        route that runs on every image the site serves."""
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def do_HEAD(self):
        """The same answer as GET, minus the body.

        Without this, every route this service answers replied `501 Unsupported
        method` to a HEAD - including /sitemap.xml, which nginx serves at the
        root of the site precisely because that is where a crawler looks for it.
        The static pages were unaffected, since nginx answers those off disk and
        has always handled HEAD; the ones that come through here did not, and
        the difference was invisible from a browser, which never sends one.

        HEAD is defined as GET with the body dropped, so that is what this does
        rather than reimplementing the route table: run do_GET with the socket
        swallowed after the headers. Content-Length is therefore the length of
        the body that would have been sent, which is the whole point of asking.

        `_head` is read by nothing else; the swap below is what does the work,
        and it is restored even when the handler raises, or the connection would
        stay muted for every request after it on the same keep-alive."""
        real = self.wfile

        class HeadersOnly:
            """Passes the head through and drops the body. The split is the
            blank line that ends the headers, which is written by end_headers()
            and is the only reliable marker at this level."""

            def __init__(self, out):
                self.out, self.done = out, False

            def write(self, data):
                if self.done:
                    return len(data)
                if b"\r\n\r\n" in data:
                    head, _ = data.split(b"\r\n\r\n", 1)
                    self.done = True
                    return self.out.write(head + b"\r\n\r\n")
                return self.out.write(data)

            def flush(self):
                return self.out.flush()

        self.wfile = HeadersOnly(real)
        try:
            self.do_GET()
        finally:
            self.wfile = real

    # Which locale tag each language claims in a preview card. Written out
    # rather than derived: og:locale wants a territory, and "pt" alone is not
    # one.
    OG_LOCALE = {"en": "en_US", "pt": "pt_BR", "ru": "ru_RU"}

    # A hostname and nothing else. What arrives here was written by nginx, but
    # the route it arrives on is public, so a visitor can hand this service any
    # address they like and it would come back inside a canonical link. It
    # would only ever be their own copy of the page, and it is escaped either
    # way - this is just cheaper than reasoning about that twice.
    HOST_RE = re.compile(r"^[a-z0-9.-]{1,253}(?::\d{1,5})?$", re.I)

    def site_base(self, scheme=None, host=None):
        """Where this site answers, as an absolute prefix.

        This service has never been told its own hostname and is not given one
        to hold, because a name in a config file is a name that can drift from
        the truth. So it is read off the request every time: from what nginx
        passed in, when nginx bothered to say, and from the headers otherwise."""
        scheme = scheme if scheme in ("http", "https") else \
            self.headers.get("X-Forwarded-Proto", "http")
        host = host if host and self.HOST_RE.match(host) else \
            self.headers.get("Host", "steamprofiler.org")
        if not self.HOST_RE.match(host or ""):
            host = "steamprofiler.org"
        return f"{scheme}://{host}"

    def send_meta(self, card, base=""):
        """The head of a post, as HTML. The only route here that is not JSON.

        A link preview is built by something that does not run JavaScript, so
        the title of a post cannot arrive the way every other string on this
        site does - the page would still say "steamprofiler.org" by the time
        the card was made. nginx pastes this into the shell with an SSI include
        before the shell reaches the network.

        The address of the page is written here too, and no longer by nginx.
        A post now has one address per language and nginx cannot tell which of
        them it is serving - it has a path, not a database - so a canonical
        built out of the request URI would name whichever address the visitor
        happened to arrive at, including the bare id with no title on it. This
        service knows all three, so it says which one is which: canonical for
        the one being previewed, alternate for each of the others, and both
        absolute, because a relative hreflang is ignored.

        Every value lands inside an attribute, so every value is escaped, and
        the only ones that exist are a title, a blurb and a date that this
        service wrote itself. An unknown key answers with nothing at all: an
        empty body is a page with no preview, which is exactly what a draft or
        a typo should produce, and it keeps the include from having to care."""
        tags = []
        if card:
            title = f'{card["title"]} - steamprofiler.org'
            esc = lambda s: html.escape(s or "", quote=True)
            # The shell has a <title> of its own, below this one, and the first
            # is the one a parser reads. The browser never sees the difference:
            # post.js sets document.title from the payload as soon as it lands.
            tags = [
                f"<title>{esc(title)}</title>",
                f'<meta name="description" content="{esc(card["blurb"])}">',
                f'<meta property="og:title" content="{esc(card["title"])}">',
                f'<meta property="og:description" content="{esc(card["blurb"])}">',
                f'<meta property="og:locale" content="'
                f'{self.OG_LOCALE.get(card["lang"], "en_US")}">',
                # Large and not "summary": post.html carries the site's card at
                # 1200x630, and "summary" asks every reader of this page to
                # crop that into a thumbnail beside the text.
                '<meta name="twitter:card" content="summary_large_image">',
                f'<meta property="og:url" content="{esc(base + card["url"])}">',
                f'<link rel="canonical" href="{esc(base + card["url"])}">',
            ]
            # x-default points at the original: a reader whose language is none
            # of the three gets the version somebody actually wrote, which is
            # the same choice _pick makes on the page itself.
            alternates = dict(card["urls"])
            alternates["x-default"] = card["urls"].get(
                card["origin"], card["url"])
            for code, path in alternates.items():
                tags.append(f'<link rel="alternate" hreflang="{esc(code)}"'
                            f' href="{esc(base + path)}">')
            if card["published_at"]:
                tags.append('<meta property="article:published_time" '
                            f'content="{esc(card["published_at"])}">')
            if card["updated_at"]:
                tags.append('<meta property="article:modified_time" '
                            f'content="{esc(card["updated_at"])}">')
            for tag in card["tags"]:
                tags.append(f'<meta property="article:tag" content="{esc(tag)}">')
            tags.append(self._post_ld(card, base))
        body = "".join(tags).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # Five minutes. A title corrected in the panel should reach the next
        # scrape the same day, and nothing here is per-visitor.
        self.send_header("Cache-Control", "public, max-age=300")
        self.end_headers()
        self.wfile.write(body)

    def send_theme(self, raw):
        """The theme of one appid, as a single meta tag, or nothing.

        The same shape as send_meta and for the same reason: an SSI include has
        no way to read JSON and the page has no way to know this early. What
        goes out is one name out of a table in this process, escaped anyway
        because free text and attributes should never meet without it.

        Cached hard. A game's theme changes when somebody writes a new layout,
        which is a deploy, and a deploy restarts this process."""
        theme = ""
        if raw.isdigit():
            theme = (fetch.GAME_LAYOUTS.get(int(raw)) or {}).get("theme") or ""
        body = (f'<meta name="sp-game" content="{html.escape(theme, quote=True)}">'
                if theme else "").encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        self.wfile.write(body)

    def send_public_game_meta(self, payload):
        """Crawler-visible head tags for the SSI-backed public game shell."""
        appid = payload["appid"]
        name = payload["name"]
        theme = payload.get("theme") or ""
        base = self.site_base()
        url = f"{base}/g/{appid}"
        image = f"{base}/art/{appid}.jpg"
        total = ((payload.get("achievements") or {}).get("total"))
        achievement_word = "achievement" if total == 1 else "achievements"
        description = (f"{name}: {total} Steam {achievement_word} with global rarity."
                       if total else f"Game details for {name} on steamprofiler.org.")
        graph = {
            "@context": "https://schema.org",
            "@type": "VideoGame",
            "@id": f"{url}#game",
            "url": url,
            "name": name,
            "description": description,
            "image": image,
            "gamePlatform": "Steam",
        }
        ld = json.dumps(graph, ensure_ascii=False).replace("<", "\\u003c")
        # The storefront backfill can take minutes after deploying the name
        # migration.  Do not let a crawler make the deliberately temporary
        # fallback the canonical indexed title.  Unknown appids remain in this
        # state too, so this also keeps nonexistent games out of the index.
        robots = ('<meta name="robots" content="noindex">'
                  if name == f"app {appid}" else "")
        theme_tag = (f'<meta name="sp-game" content="{html.escape(theme, quote=True)}">'
                     if theme else "")
        tags = (
            f'<title>{html.escape(name)} — steamprofiler.org</title>'
            f'{robots}'
            f'{theme_tag}'
            f'<meta name="description" content="{html.escape(description, quote=True)}">'
            f'<link rel="canonical" href="{html.escape(url, quote=True)}">'
            f'<meta property="og:type" content="website">'
            f'<meta property="og:title" content="{html.escape(name, quote=True)}">'
            f'<meta property="og:description" content="{html.escape(description, quote=True)}">'
            f'<meta property="og:url" content="{html.escape(url, quote=True)}">'
            f'<meta property="og:image" content="{html.escape(image, quote=True)}">'
            f'<script type="application/ld+json">{ld}</script>'
        )
        body = tags.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        self.wfile.write(body)

    @staticmethod
    def _post_ld(card, base):
        """The same post, said again in the form a machine reads.

        Written here rather than in post.html for the reason the OG tags are:
        the title, the dates and the tags are per post, and a static file has
        one of each. The five static pages carry their graph in the shell; a
        post's has to be built where the post is known, which is here.

        The author is the node the home page defines at the same @id, so a
        reader that has both ends up with one person who wrote the site and
        the posts, rather than two who happen to share a name.
        """
        url = base + card["url"]
        graph = [
            {
                "@type": "Person",
                "@id": "https://steamprofiler.org/#author",
                "name": "GustavoHSCruz",
                "url": "https://github.com/GustavoHSCruz",
            },
            {
                "@type": "WebSite",
                "@id": "https://steamprofiler.org/#site",
                "url": "https://steamprofiler.org/",
                "name": "steamprofiler.org",
            },
            {
                "@type": "BlogPosting",
                "@id": f"{url}#post",
                "url": url,
                "mainEntityOfPage": url,
                "headline": card["title"],
                "description": card["blurb"],
                "datePublished": card["published_at"],
                "dateModified": card["updated_at"] or card["published_at"],
                "inLanguage": card["lang"],
                "isPartOf": {"@id": "https://steamprofiler.org/#site"},
                "author": {"@id": "https://steamprofiler.org/#author"},
                "publisher": {"@id": "https://steamprofiler.org/#author"},
                **({"keywords": card["tags"]} if card["tags"] else {}),
            },
            {
                "@type": "BreadcrumbList",
                "@id": f"{url}#breadcrumb",
                "itemListElement": [
                    {"@type": "ListItem", "position": 1,
                     "name": "steamprofiler.org", "item": base + "/"},
                    {"@type": "ListItem", "position": 2,
                     "name": "Blog", "item": base + "/blog"},
                    {"@type": "ListItem", "position": 3,
                     "name": card["title"]},
                ],
            },
        ]
        ld = json.dumps({"@context": "https://schema.org", "@graph": graph},
                        ensure_ascii=False)
        # The one escape that matters inside a script tag: a title or a blurb
        # holding the six characters "</script>" would otherwise close it and
        # spill the rest of the JSON into the page as markup.
        return ('<script type="application/ld+json">'
                f'{ld.replace("<", chr(92) + "u003c")}</script>')

    # The pages worth telling a crawler about. Everything else the site serves
    # is either a profile - which is somebody's, and is kept out of the index
    # by robots.txt - or a page that only exists as an answer to something,
    # like the ban notice and the appeal form.
    SITEMAP_PAGES = ("/", "/about", "/blog", "/feedback", "/support", "/privacy",
                     "/privacy/history", "/publishers", "/developers")

    # The franchise screens. Which exist is decided in the front end's two
    # catalogue files, so this is a copy, and it is a copy on purpose: the api
    # has no other reason to know these words, and the alternative is an
    # endpoint whose only caller would be this loop.
    #
    # The copy can fall behind, and the way it falls behind is safe: a slug
    # added there and not here is a page that works and is not in the sitemap
    # yet. A slug here and not there is the one to avoid, and it lands on the
    # index rather than on a 404, because the page redirects an unknown slug.
    SITEMAP_FRANCHISES = (
        "half-life", "counter-strike", "portal", "grand-theft-auto",
        "the-elder-scrolls", "fallout", "stalker", "arma", "dark-souls",
        "resident-evil", "ace-attorney", "ace-combat", "age-of-empires",
        "alan-wake", "amnesia", "anno", "assassins-creed", "assetto-corsa",
        "baldurs-gate", "batman-arkham", "battlefield", "bioshock",
        "borderlands", "call-of-duty", "castlevania", "cities-skylines",
        "civilization", "command-and-conquer", "company-of-heroes",
        "crusader-kings", "crysis", "dawn-of-war", "dead-island",
        "dead-rising", "dead-space", "deus-ex", "devil-may-cry", "dirt",
        "dishonored", "divinity", "dont-starve", "doom", "dragon-age",
        "dragon-ball", "dragon-quest", "dragons-dogma", "dying-light",
        "europa-universalis", "fear", "f1", "far-cry", "final-fantasy",
        "forza", "gears", "ghost-recon", "gothic", "halo", "hearts-of-iron",
        "hitman", "homeworld", "jurassic-world-evolution", "just-cause",
        "killing-floor", "kingdom-hearts", "left-4-dead", "life-is-strange",
        "little-nightmares", "mafia", "mass-effect", "max-payne",
        "mega-man", "metal-gear", "metro", "microsoft-flight-simulator",
        "middle-earth", "mirrors-edge", "monster-hunter", "mortal-kombat",
        "need-for-speed", "nier", "path-of-exile", "persona",
        "pillars-of-eternity", "planet-coaster", "planet-zoo",
        "plants-vs-zombies", "prince-of-persia", "quake", "rainbow-six",
        "red-dead", "risen", "saints-row", "serious-sam", "silent-hill",
        "sniper-elite", "sonic", "splinter-cell", "star-wars",
        "street-fighter", "tales-of", "team-fortress", "tekken",
        "the-division", "the-evil-within", "the-sims", "the-witcher",
        "titanfall", "tomb-raider", "total-war", "trackmania", "tropico",
        "truck-simulator", "two-point", "vermintide", "victoria",
        "warhammer-40000", "watch-dogs", "wolfenstein", "worms", "xcom",
        "yakuza"
    )

    def send_sitemap(self):
        """The sitemap, built rather than kept, so a post is in it the minute
        it is published and no file has to be remembered.

        The address of the site comes from the request, the way the preview's
        does: this service has never been told its own hostname, and giving it
        one to hold would be one more thing that can drift from the truth.

        A post enters once, under its original language, carrying the other two
        as alternates. Listing the three as three entries would be describing
        one post as three that happen to say the same thing, which is the shape
        a crawler reads as duplication rather than translation."""
        base = html.escape(self.site_base(), quote=True)
        today = time.strftime("%Y-%m-%d", time.gmtime())

        out = ['<?xml version="1.0" encoding="UTF-8"?>',
               '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"'
               ' xmlns:xhtml="http://www.w3.org/1999/xhtml">']
        for path in self.SITEMAP_PAGES:
            out.append(f"<url><loc>{base}{path}</loc>"
                       f"<lastmod>{today}</lastmod></url>")
        out.append(f"<url><loc>{base}/franchises</loc>"
                   f"<lastmod>{today}</lastmod></url>")
        for slug in self.SITEMAP_FRANCHISES:
            out.append(f"<url><loc>{base}/franchises/{slug}</loc>"
                       f"<lastmod>{today}</lastmod></url>")
        for appid in sorted(fetch.GAME_LAYOUTS):
            out.append(f"<url><loc>{base}/g/{appid}</loc>"
                       f"<lastmod>{today}</lastmod></url>")
        for row in blog.listing():
            # Every id and every slug matched SLUG_RE before it was stored, so
            # nothing here needs escaping in a URL or in XML.
            when = (row["updated_at"] or row["published_at"] or "")[:10] or today
            alt = "".join(
                f'<xhtml:link rel="alternate" hreflang="{code}"'
                f' href="{base}{path}"/>'
                for code, path in sorted(row["urls"].items()))
            out.append(f"<url><loc>{base}{row['urls'][row['origin']]}</loc>"
                       f"<lastmod>{when}</lastmod>{alt}</url>")
        out.append("</urlset>")

        body = "".join(out).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/xml; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "public, max-age=3600")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, status, payload, ttl=None):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # nginx caches nothing; this is for the browser and any proxy in between.
        self.send_header("Cache-Control", f"public, max-age={ttl}" if ttl else "no-store")
        # Only deliberately published, versioned contracts opt into
        # cross-origin reads. The rest of the JSON API remains an
        # implementation detail of this site's own frontend.
        if getattr(self, "public_cors", False):
            self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        """Preflight only the two versioned contracts published for reuse."""
        if urlparse(self.path).path not in ("/player", "/companion"):
            return self.send_empty(404)
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Accept")
        self.send_header("Access-Control-Max-Age", "86400")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        url = urlparse(self.path)
        # A handler can serve another request on a persistent connection. Set
        # this for every GET so a prior public response cannot make an
        # unrelated JSON route cross-origin by accident.
        self.public_cors = url.path in ("/player", "/companion")
        q = parse_qs(url.query)
        one = lambda name: (q.get(name) or [""])[0].strip()
        try:
            # Two routes that answer nginx rather than a browser. First, because
            # one of them runs on every single request the site serves and has
            # no business queueing behind the route table to find that out.
            #
            # /gate is auth_request, and it answers in the only three codes the
            # module understands: 204 serve it, 403 and 401 refuse it. Those
            # two are not interchangeable here - nginx maps 403 to the ban page
            # and 401 to the abuse page, so this is where a two-day shut-out
            # and a thirty-second one stop being the same thing to a reader.
            #
            # 401 rather than 429, which is what the abuse page will actually
            # be served as, because auth_request treats anything outside
            # {2xx, 401, 403} as a broken auth service and turns it into 500.
            # The status is put back on the way out, in nginx.conf.
            if url.path == "/gate":
                who = self.caller
                # Counted before it is judged, and counted even when refused: a
                # scanner that is already serving two days is precisely the
                # traffic the census exists to put a name on. This is the only
                # place that sees every request the site serves - fonts, key
                # art and all - which is why it is here and not in the handlers.
                # It writes nothing; census.py flushes on a timer.
                census.note(who,
                            path=self.headers.get("X-Sp-Path", ""),
                            ua=self.headers.get("X-Sp-UA", ""),
                            country=self.headers.get("CF-IPCountry"),
                            region=self.headers.get("CF-Region"))
                if bans.until(who):
                    return self.send_empty(403)
                if guard.blocked_for(who):
                    return self.send_empty(401)
                return self.send_empty(204)
            if url.path == "/trap":
                bans.ban(self.caller, reason="trap",
                         path=self.headers.get("X-Trap-Path"))
                return self.send_empty(403)
            if url.path == "/sitemap.xml":
                # Reached through nginx, which serves it at the root of the
                # site rather than under /api/ - a sitemap only counts at the
                # address a crawler looks for it.
                self.gate("sitemap")
                return self.send_sitemap()
            if url.path == "/healthz":
                return self.send_json(200, {"ok": True, "cache": len(_cache),
                                            "art": art.stats(), "store": meta.stats(),
                                            # One line for steamcommunity.com as
                                            # a whole, because the market, the
                                            # inventories and the profile
                                            # scrapes share one budget and a
                                            # cooldown on it explains all three
                                            # going quiet at once.
                                            "community": community.stats(),
                                            "inventories": inv.stats(),
                                            "guard": guard.state(),
                                            "census": census.state()})
            if url.path == "/art":
                # Only ever reached on a miss: nginx serves data/art directly
                # and falls back here when the file is not there yet. Key art
                # is the game's, not the player's, so no steamid is involved.
                raw = one("appid")
                if not raw.isdigit() or len(raw) > 8:
                    raise Fail(400, "@err.bad_appid")
                # Only ever a miss, so it always costs a trip to the CDN. Not a
                # Steam key call, but not free either.
                self.gate("art")
                body = art.get(int(raw))
                if body is None:
                    raise Fail(404, "@err.no_art")
                return self.send_jpeg(body, 30 * 24 * 3600)
            if url.path == "/price":
                # What a game costs is the game's, not the player's - the same
                # shape of fact as its key art - so no steamid is involved and
                # one answer serves every visitor.
                #
                # Deliberately NOT through cached(): that holds one lock across
                # produce() for the whole process, and a storefront that is
                # thinking would stall every cold profile on the site behind it.
                # meta.price() owns its own per-appid lock and its own cache,
                # which is the disk, exactly as art.get() does.
                raw = one("appid")
                if not raw.isdigit() or len(raw) > 8:
                    raise Fail(400, "@err.bad_appid")
                self.gate("price")
                # The country comes from the reader's language and is checked
                # against a closed set before it goes anywhere near a URL.
                out = meta.price(int(raw), meta.cc_of(one("cc")))
                # A settled answer keeps for a few minutes. One that is still
                # being learned, or that came back stale, should be asked for
                # again soon - the page polls on exactly that signal.
                unsettled = out["state"] == "unknown" or out["stale"]
                return self.send_json(200, out, ttl=30 if unsettled else 300)
            if url.path == "/game/cards":
                # A game's card set belongs to the game, like its price and its
                # key art, so no steamid is involved and one answer serves
                # everybody. Not through cached() for the same reason /price is
                # not: cards.py owns its own per-appid lock and its own cache,
                # which is the disk, and a market that is thinking must not
                # stall every cold profile on the site behind one lock.
                raw = one("appid")
                if not raw.isdigit() or len(raw) > 8:
                    raise Fail(400, "@err.bad_appid")
                self.gate("cards_set")
                out = cards.set_of(int(raw))
                # An answer nobody has yet is worth asking about again soon;
                # a set that has been read keeps for an hour, because that is
                # what the disk will say either way.
                unsettled = out["state"] == "unknown" or out["stale"]
                return self.send_json(200, out, ttl=30 if unsettled else 3600)
            if url.path == "/player":
                # One of the two versioned JSON contracts intended for use
                # outside steamprofiler.org. Set CORS before validation so an
                # embedding page can read errors too.
                raw = one("appid")
                if not raw.isdigit() or raw == "0" or len(raw) > 8:
                    raise Fail(400, "@err.bad_appid")
                appid = int(raw)
                key = f"pl:{appid}"
                self.gate("apps", key=key)

                def player_ttl(value):
                    return 20 if value.get("state") == "pending" else 600

                out = cached(key, player_ttl, lambda: do_player(appid))
                return self.send_json(200, out, ttl=player_ttl(out))
            if url.path == "/companion":
                raw = one("appid")
                if not raw.isdigit() or raw == "0" or len(raw) > 8:
                    raise Fail(400, "@err.bad_appid")
                appid = int(raw)
                language = meta.language_of(one("l"))
                key = f"cp:{appid}:{language}"
                self.gate("apps", key=key)

                def companion_ttl(value):
                    return 20 if value.get("state") == "pending" else 300

                out = cached(key, companion_ttl,
                             lambda: do_companion(appid, language))
                return self.send_json(200, out, ttl=companion_ttl(out))
            if url.path in ("/game/public", "/game/public/meta"):
                raw = one("appid")
                if not raw.isdigit() or raw == "0" or len(raw) > 8:
                    raise Fail(400, "@err.bad_appid")
                cc = meta.cc_of(one("cc"))
                language = meta.language_of(one("l"))
                key = f"pg:{int(raw)}:{cc}:{language}"
                self.gate("public_game", key=key)
                out = do_public_game(int(raw), cc, language)
                if url.path.endswith("/meta"):
                    return self.send_public_game_meta(out)
                return self.send_json(200, out, ttl=CLIENT_TTL)
            if url.path == "/apps":
                # A list of appids, answered out of the store cache. The
                # franchise screens hold their own lists and this is the only
                # thing they ask for, so the shape is theirs: many apps, one
                # round trip, nothing personal on it.
                #
                # The ids are parsed before anything else touches them. A
                # caller that sends a word gets it dropped rather than passed
                # on to a query, and one that sends five hundred gets the
                # first hundred and twenty rather than a refusal - the screen
                # that exists cannot reach that number, and something that
                # can is not a visitor to argue with.
                ids = []
                for raw in one("ids").split(",")[:APPS_MAX]:
                    raw = raw.strip()
                    if raw.isdigit() and raw != "0" and len(raw) <= 8:
                        ids.append(int(raw))
                ids = list(dict.fromkeys(ids))
                if not ids:
                    raise Fail(400, "@err.bad_appid")
                cc = meta.cc_of(one("cc"))
                # Which of them to ask Steam about. A subset of `ids`, because
                # a live count for an app the caller did not name is a call
                # spent on something nobody asked to see.
                wanted = {int(raw) for raw in one("live").split(",")
                          if raw.strip().isdigit()}
                live = [appid for appid in ids if appid in wanted][:APPS_LIVE_MAX]
                key = f"aps:{cc}:{','.join(map(str, ids))}:{','.join(map(str, live))}"
                self.gate("apps", key=key)
                # Short, because the live counts are the perishable half and
                # they are what somebody reloads for. Without them this is a
                # read of a cache that changes a few times an hour.
                return self.send_json(200, cached(key, 120 if live else 600,
                                                 lambda: do_apps(ids, cc, live)),
                                      ttl=120 if live else 600)
            if url.path == "/deck":
                who = one("id")
                if not re.fullmatch(r"\d{17}", who):
                    raise Fail(400, "@err.bad_steamid")
                key = f"dk:{who}"
                self.gate("deck", key=key)
                # Short while the queue still has this library to get through,
                # long once it does not. The first visit to a library nobody
                # has opened answers with nothing, and caching that for five
                # minutes caches the emptiness rather than the answer.
                #
                # No browser cache at all: the screen re-asks while it fills,
                # and a Cache-Control here would have it re-asking itself.
                def deck_ttl(value):
                    cov = value.get("coverage") or {}
                    return 20 if cov.get("asked", 0) < cov.get("owned", 0) else 300

                # Resolved out here, before cached() takes the lock: see the
                # note on do_deck.
                who_profile = do_profile(who)
                return self.send_json(200, cached(
                    key, deck_ttl, lambda: do_deck(who_profile)))
            if url.path == "/houses":
                # A page of companies on one axis, filtered server-side. That
                # filtering is the whole reason this is an endpoint: the list
                # is tens of thousands of names, and sending it to be narrowed
                # in a browser would be sending the haystack instead of the
                # needle.
                #
                # No Steam call, no key, no profile: a query against an index
                # a weekly worker fills. Nothing on this answer is about
                # anybody, which is why it is cached for everyone at once.
                kind = houses.kind_of(one("kind"))
                if not kind:
                    raise Fail(400, "@err.bad_house_kind")
                query = one("q")[:80]
                try:
                    start = int(one("start") or 0)
                    count = int(one("count") or 60)
                except ValueError:
                    raise Fail(400, "@err.bad_body")
                # With a profile named, the same list joined against that
                # library: which companies it is actually on, most of it
                # first. That join is the half of the filter a browser cannot
                # do - it would need all fifty thousand shelves to find out -
                # and the half a shared cache cannot hold, which is why it is
                # keyed by the profile and priced as a lookup rather than as
                # a read.
                who = one("id")
                if who:
                    if not re.fullmatch(r"\d{17}", who):
                        raise Fail(400, "@err.bad_steamid")
                    key = f"hsm:{kind}:{who}:{query.lower()}:{start}:{count}"
                    self.gate("houses", key=key)
                    # Outside cached(), for the reason spelled out on do_deck:
                    # the lock is not reentrant and do_profile takes it too.
                    lib = do_profile(who)
                    ids = [g["appid"] for g in lib.get("library") or []]
                    ids += [g["appid"] for g in lib.get("unplayed") or []]

                    return self.send_json(200, cached(
                        key, 300,
                        lambda: houses.mine(kind, ids, query, start, count)),
                        ttl=300)
                key = f"hs:{kind}:{query.lower()}:{start}:{count}"
                self.gate("houses", key=key)
                return self.send_json(200, cached(
                    key, HOUSES_TTL,
                    lambda: houses.index(kind, query, start, count)), ttl=HOUSES_TTL)
            if url.path == "/house":
                # One company and everything filed under it. The year comes
                # from the store cache rather than from the index, so a year
                # on screen is one Steam told this server and never one the
                # catalogue source guessed - and a game this site has not read
                # yet simply has no year, which the screen says.
                kind = houses.kind_of(one("kind"))
                if not kind:
                    raise Fail(400, "@err.bad_house_kind")
                slug = one("slug")[:40]
                if not slug or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}", slug):
                    raise Fail(400, "@err.bad_house")
                key = f"hs1:{kind}:{slug}"
                self.gate("house", key=key)
                return self.send_json(200, cached(
                    key, HOUSES_TTL,
                    lambda: do_house(kind, slug)), ttl=HOUSES_TTL)
            if url.path == "/game/search":
                # Autocomplete reads the catalogue learned by meta.py. It does
                # not call Steam, enqueue details or reveal anything personal.
                self.gate("game_search", key=f"gs:{one('q').lower()[:80]}")
                return self.send_json(200, {
                    "items": meta.search_games(one("q"),
                                               fallback_names=fetch.GAME_SEARCH_NAMES)
                }, ttl=300)
            # The cheap three. They cost a token each not because they are
            # expensive but because an address that has been shut out should
            # be shut out of everything, not just of the costly half.
            if url.path == "/owner":
                self.gate("owner", key="owner")
                return self.send_json(200, do_owner(), ttl=600)
            if url.path == "/resolve":
                who = one("q")
                self.gate("resolve", key=f"r:{who.lower()}")
                return self.send_json(200, do_resolve(who), ttl=600)
            if url.path == "/support":
                self.gate("support")
                return self.send_json(200, support.state(), ttl=300)
            if url.path == "/board":
                # Per-visitor ("did I vote?"), so this one is never shared cache.
                self.gate("board")
                return self.send_json(200, {"items": store.board(self.caller)})
            # The blog. Two reads of SQLite and nothing else: no Steam call, no
            # key spent, so they are priced as what they are. Never cached
            # here, for the same reason the board is not - both carry "did I
            # vote?", which is one answer per address and not one per post.
            if url.path == "/blog":
                self.gate("blog")
                return self.send_json(200, {"items": blog.published(
                    self.lang(one("lang")), self.caller)})
            if url.path == "/blog/post":
                self.gate("blog")
                # An id, an old slug or a translated one - blog.py knows which
                # of the three it is holding and this does not need to. `slug`
                # is still read as a second name for the same parameter: the
                # page that asks with it is in somebody's cache right now.
                key = (one("key") or one("slug")).lower()
                if not blog.SLUG_RE.match(key):
                    raise Fail(404, "@err.no_post")
                return self.send_json(200, blog.one(
                    key, self.lang(one("lang")), self.caller))
            if url.path == "/blog/meta":
                # nginx asking on behalf of whatever is scraping the post. The
                # key arrives from the path it already matched, so a bad one
                # here means a hand-typed URL, and an empty preview is the
                # answer to that rather than a 404 inside somebody's <head>.
                self.gate("blog")
                key = (one("key") or one("slug")).lower()
                # The readable tail of the address, which is what says in which
                # language this preview is being asked for. Not checked against
                # the pattern: it is compared to slugs that were, and anything
                # that is not one of them changes nothing.
                return self.send_meta(
                    blog.preview(key, one("title")) if blog.SLUG_RE.match(key) else None,
                    self.site_base(one("scheme"), one("host")))
            if url.path == "/admin/inbox":
                self.require_admin()
                # Feedback only. Appeals answer to /admin/blocks, next to the
                # ban they are about, because that is the only place where the
                # message and the thing it is asking about are both visible.
                return self.send_json(200, store.inbox(
                    one("status") or None, kinds=store.PUBLIC_KINDS))
            if url.path == "/admin/blocks":
                self.require_admin()
                return self.send_json(200, do_blocks())
            if url.path == "/admin/blog":
                self.require_admin()
                return self.send_json(200, blog.everything())
            if url.path == "/admin/census":
                self.require_admin()
                # Flushed first, so the panel shows the current minute rather
                # than the last time the timer fired. Reading it is the one
                # moment where being a minute stale would be noticed.
                census.flush()
                return self.send_json(200, census.report())
            if url.path == "/og.png":
                # Takes whatever is in the URL rather than a steamid, because
                # nginx hands it the path segment and knows nothing else. The
                # image is cached separately from the payload it draws: the
                # bytes are the expensive part on a repeat scrape, not the
                # lookup, which is already warm by then.
                who = one("q")
                if not who:
                    raise Fail(400, "@err.bad_steamid")
                self.gate("og", key=f"r:{who.lower()}")
                sid = do_resolve(who)["steamid"]
                self.gate("og", key=f"og:{sid}", subject=sid)
                # Resolved and built before the image is asked for, never
                # inside it: cached() holds a plain lock across produce(), so a
                # cached() call nested in another one deadlocks the thread.
                profile = do_profile(sid)
                png = cached(f"og:{sid}", TTL, lambda: og.card(profile))
                return self.send_png(png, TTL)
            if url.path == "/card.png":
                who, year = one("q"), one("year")
                current = int(time.strftime("%Y", time.gmtime()))
                if not who:
                    raise Fail(400, "@err.bad_steamid")
                if not year.isdigit() or len(year) != 4 or not 2003 <= int(year) <= current:
                    raise Fail(400, "@err.bad_year")
                self.gate("og", key=f"r:{who.lower()}")
                sid = do_resolve(who)["steamid"]
                self.gate("og", key=f"card:{sid}:{year}", subject=sid)
                profile = do_profile(sid)
                # Achievement dates cost three Steam calls per scanned game.
                # A card may reuse that result, but must never trigger it.
                unlocks = cached_value(f"x:{sid}")
                # Keep the pre-scan and post-scan pictures apart. Otherwise a
                # card opened just before the scan would keep saying "not
                # scanned" for the full profile TTL after the result existed.
                scan_state = "scanned" if unlocks is not None else "plain"
                png = cached(f"card:{sid}:{year}:{scan_state}", TTL,
                             lambda: og.year_card(profile, year, unlocks))
                return self.send_png(png, TTL)

            if url.path in EMBEDS:
                # The four shapes a profile can leave here in. All of them are
                # one profile lookup and some arithmetic, so they are priced and
                # cached as that lookup and the drawing itself is not cached at
                # all: rendering is microseconds, and a generator page trying
                # ten variants of one chart would otherwise push ten copies of
                # it into a cache that a profile needs the room in.
                kind = EMBEDS[url.path]
                who = one("q")
                if not who:
                    raise Fail(400, "@err.bad_steamid")
                self.gate("embed", key=f"r:{who.lower()}")
                sid = do_resolve(who)["steamid"]
                self.gate("embed", key=f"p:{sid}", subject=sid)
                profile = do_profile(sid)
                o = embed.options(kind, one)
                if kind == "text":
                    return self.send_text(embed.text_bars(profile, o), TTL)
                draw = {"bars": embed.bars, "banner": embed.banner,
                        "badge": embed.badge}[kind]
                return self.send_svg(draw(profile, o), TTL)

            if url.path == "/game/theme":
                # Which of the themed pages this appid is about to become, as a
                # meta tag, for nginx to paste into the shell before it leaves
                # this house. The wait screen is drawn before any of the real
                # answers arrive, and this is the one fact about the page that
                # is knowable that early: the appid is in the URL and the table
                # that maps it to a theme is right here.
                #
                # Deliberately not a JSON route the page could call. A fetch
                # would land after the first paint, so the wait would open grey
                # and turn into the game a moment later, which is a flash on
                # every game page. Arriving inside the shell, it is simply what
                # the page was.
                #
                # A game with no theme of its own answers with nothing, which
                # is what the generic page is: no tag, no attribute, and the
                # wait keeps the site's own palette.
                #
                # Not priced through the gate, unlike every route below it.
                # This one reads a dict and touches nothing else, so it is
                # cheaper than the shell nginx is already sending; charging for
                # it would only mean a visitor who paid for the page and then
                # could not have the wait screen that goes with it.
                return self.send_theme(one("appid"))
            sid = one("id")
            if not fetch.STEAMID_RE.match(sid):
                raise Fail(400, "@err.bad_steamid")
            if url.path == "/profile":
                self.gate("profile", key=f"p:{sid}", subject=sid)
                # How far the rarity scan reaches is a setting of this service,
                # not a property of the profile, so it is stitched on here
                # rather than cached inside the payload. The button needs it to
                # say what it is about to do.
                return self.send_json(200, dict(do_profile(sid),
                                                rarity_games=RARITY_GAMES), ttl=CLIENT_TTL)
            if url.path == "/game":
                raw = one("appid")
                if not raw.isdigit():
                    raise Fail(400, "@err.bad_appid")
                self.gate("game", key=f"g:{sid}:{raw}", subject=sid)
                return self.send_json(200, do_game(sid, int(raw)), ttl=CLIENT_TTL)
            if url.path == "/cards":
                self.gate("cards", key=f"c:{sid}", subject=sid)
                return self.send_json(200, do_cards(sid), ttl=60)
            if url.path == "/wishlist":
                self.gate("wishlist", key=f"w:{sid}:{meta.cc_of(one('cc'))}", subject=sid)
                return self.send_json(200, do_wishlist(sid, meta.cc_of(one("cc"))),
                                      ttl=60)
            if url.path == "/inventory":
                # Not through cached(): that holds one process-wide lock across
                # the whole build, and this can spend a community slot thinking
                # about it - which would stall every cold profile on the site
                # behind one visitor's card panel. Same reason /price and
                # /game/cards stay outside it. inv.py has its own per-steamid
                # lock and its own memory cache, and neither is shared with the
                # profile builds.
                self.gate("inventory", subject=sid)
                return self.send_json(200, fetch.build_collection(sid), ttl=60)
            if url.path == "/workshop":
                self.gate("workshop", key=f"ws:{sid}", subject=sid)
                return self.send_json(200, do_workshop(sid), ttl=300)
            if url.path == "/econ":
                # IEconService answers about the account that owns the key and
                # no other, so this is the owner's panel or it is nothing. A
                # stranger's profile gets a 404 rather than the owner's trades
                # dressed up as theirs, and ECON_PANEL is off by default because
                # most installations have no reason to publish it at all.
                if not ECON_PANEL or sid != fetch.OWNER_ID:
                    raise Fail(404, "@err.no_route")
                self.gate("econ", key=f"e:{sid}", subject=sid)
                return self.send_json(200, do_econ(sid), ttl=60)
            if url.path == "/meta":
                # Cheap in itself - one read of the store cache - but it needs
                # the profile, so it is priced as a profile when that is cold.
                self.gate("profile", key=f"p:{sid}", subject=sid)
                return self.send_json(200, do_meta(sid, meta.cc_of(one("cc"))), ttl=10)
            # Two names for one scan. `/unlocks` is what it does now that the
            # same three calls per game answer the rarest, what is still
            # locked, and the unlocks per year; `/rarities` is what it was
            # called when it answered only the first, and it stays because a
            # browser holding yesterday's dash.js is still asking for it. Same
            # handler, same cache entry, so keeping both costs nothing.
            if url.path in ("/unlocks", "/rarities"):
                self.gate("rarities", key=f"x:{sid}", subject=sid)
                return self.send_json(200, do_rarities(sid), ttl=CLIENT_TTL)
            if url.path == "/mates":
                self.gate("mates", key=f"f:{sid}", subject=sid)
                return self.send_json(200, do_mates(sid), ttl=CLIENT_TTL)
            raise Fail(404, "@err.no_route")
        except guard.Denied as e:
            self.send_json(e.status, {"error": e.message})
        except store.Rejected as e:
            self.send_json(400, {"error": str(e)})
        except Fail as e:
            self.send_json(e.status, {"error": e.message})
        except Exception:
            traceback.print_exc()
            self.send_json(502, {"error": "@err.steam_down"})

    def do_POST(self):
        url = urlparse(self.path)
        try:
            # Before read_json, because this one does not speak JSON, and
            # before the gate, because the whole point of it is to be reachable
            # by an address the gate is refusing. nginx serves its path with
            # auth_request off for the same reason.
            if url.path == "/appeal":
                return self.appeal()
            # Also before read_json, and the token first: this is the one route
            # allowed to hand the process half a megabyte, so what buys the
            # bigger ceiling is the token and not the path. An unauthenticated
            # caller naming this path is refused having sent nothing.
            if url.path == "/admin/blog/save":
                self.require_admin()
                body = self.read_json(MAX_POST_BODY)
                raw = body.get("id")
                return self.send_json(200, blog.save(
                    raw if isinstance(raw, int) else None,
                    body.get("slug"), body.get("status"), body.get("origin"),
                    body.get("tags"), body.get("texts")))
            body = self.read_json()
            if url.path in ("/feedback", "/vote", "/blog/vote"):
                # These have their own defences - a honeypot, a clock and a
                # ceiling in the database. The gate is here so that an address
                # already shut out for hammering the lookups stays shut out.
                self.gate("write")
            if url.path == "/feedback":
                return self.send_json(201, self.leave(body))
            if url.path == "/vote":
                mid = body.get("id")
                if not isinstance(mid, int):
                    raise Fail(400, "@err.bad_id")
                return self.send_json(200, store.vote(mid, self.caller))
            if url.path == "/blog/vote":
                # By whatever names the post in a URL rather than by row id,
                # because that is what the page already holds. The pages ship
                # the id; `slug` is read as the same field for the page that is
                # still in somebody's cache from before there were ids.
                key = str(body.get("key") or body.get("slug") or "").lower()
                if not blog.SLUG_RE.match(key):
                    raise Fail(400, "@err.bad_id")
                return self.send_json(200, blog.vote(key, self.caller))

            self.require_admin()
            # Lifting a ban has to happen in *this* process. bans.py keeps the
            # live list in memory and only writes through to SQLite, so a
            # DELETE run from a shell or from the admin container clears the
            # disk and changes nothing until a restart - the gate would keep
            # refusing an address whose row is already gone. Answering an
            # appeal from the panel goes through here for that reason.
            if url.path == "/admin/unban":
                who = (body.get("ip_hash") or "").strip()
                if not who:
                    raise Fail(400, "@err.bad_id")
                # One way out, and it is the appeal. An address that never
                # wrote anything is not let back in early, whatever the panel
                # shows about it: reading a path and deciding it looks
                # innocent is guessing, and the two days are already the
                # answer to a guess. The panel hides the button for these, but
                # the rule lives here, where the lift actually happens.
                if not store.appealed(who):
                    raise Fail(409, "@err.no_appeal")
                return self.send_json(200, {"ok": bans.lift(who)})
            mid = body.get("id")
            if not isinstance(mid, int):
                raise Fail(400, "@err.bad_id")
            if url.path == "/admin/update":
                return self.send_json(200, store.update(
                    mid, body.get("status"), body.get("reply")))
            if url.path == "/admin/delete":
                return self.send_json(200, store.remove(mid))
            if url.path == "/admin/blog/delete":
                return self.send_json(200, blog.remove(mid))
            raise Fail(404, "@err.no_route")
        except guard.Denied as e:
            self.send_json(e.status, {"error": e.message})
        except store.Rejected as e:
            self.send_json(400, {"error": str(e)})
        except Fail as e:
            self.send_json(e.status, {"error": e.message})
        except Exception:
            traceback.print_exc()
            self.send_json(500, {"error": "@err.save_failed"})

    def appeal(self):
        """Someone contesting a ban, from the address the ban is on.

        Deliberately not guarded. Everything else that writes goes through
        guard.afford first, and doing that here would refuse exactly the
        population the route exists for. What holds it up instead is the outer
        rate limit in nginx, the per-address ceiling store.add() already
        enforces, and the honeypot below.

        The reply is always a redirect and never a reason. A refusal that
        explains itself is a refusal a script can be tuned against, and the
        person this page is written for does not need the difference between
        "too short" and "too often" spelled out - the form says both.
        """
        form = self.read_form()
        if (form.get("website") or "").strip():
            return self.send_redirect("/appeal")

        # What the owner actually needs to answer this: which path earned the
        # ban and how hard the address leaned on it. The message row carries
        # the ip_hash on its own, which is the handle bans.lift() takes.
        hit = bans.describe(self.caller)
        if not hit:
            context = "no ban on this address"
        else:
            # REPEAT leads, because it is the one fact that changes the answer.
            # An address asking to be let out for the second time is not making
            # the same request as one asking for the first.
            mark = f"REPEAT({hit['lifts']} lifts) · " if hit["repeat"] else ""
            context = (f"{mark}{hit['path'] or '?'} · {hit['hits']}x"
                       f" · {hit['created_at']}")

        try:
            store.add(
                kind="appeal",
                title="Ban appeal",
                message=form.get("message"),
                contact=form.get("contact"),
                context=context,
                address=self.caller,
            )
        except store.Rejected:
            # Too short, or too many from this address in an hour. Same answer
            # either way; the form already says what both limits are.
            return self.send_redirect("/appeal")
        return self.send_redirect("/appeal/sent")

    def leave(self, body):
        """A message from a visitor. There is no login, so the defences are: a
        field no person can see, a clock, a length limit, and a per-address
        ceiling in the database. None of them are strong alone."""
        if (body.get("website") or "").strip():
            # Honeypot. A form filler that fills everything fills this too.
            raise Fail(400, "@err.rejected")
        try:
            elapsed = float(body.get("elapsed") or 0)
        except (TypeError, ValueError):
            elapsed = 0
        if elapsed < MIN_SECONDS:
            raise Fail(429, "@err.too_fast")

        mid = store.add(
            kind=(body.get("kind") or "").strip(),
            title=body.get("title"),
            message=body.get("message"),
            contact=body.get("contact"),
            context=body.get("context"),
            address=self.caller,
        )
        return {"id": mid, "ok": True}

    def log_message(self, fmt, *args):
        # One line per request, without the client address.
        sys.stderr.write(f"{self.command} {self.path} -> {fmt % args}\n")


if __name__ == "__main__":
    if not fetch.KEY:
        raise SystemExit("set STEAM_API_KEY (see .env)")
    store.init()
    blog.init()
    # Before the socket opens, not after: nginx starts asking /gate about every
    # request the moment this answers, and an empty ban list would let through
    # exactly the addresses that were shut out by the previous run.
    held = bans.init()
    # Same reason, one step weaker: the gate counts every request, so the tables
    # have to exist before the first one arrives. A census that fails is caught
    # and logged rather than fatal - it is the one thing here nothing depends on.
    census.init()
    # The storefront crawler. It owns its own pace and never blocks a request;
    # what it fills in is read off disk by whoever asks next.
    meta.start()
    # The market crawler, on the same terms: its own pace, its own
    # disk, and never in the way of a request.
    cards.start()
    # One exchange rate a day, so the card prices in dollars can be read beside
    # an approximation in the reader's own money. One request, once a day.
    fx.start()
    houses.start()
    proton.start()
    ready = [c["label"] for c in support.channels()]
    if not ADMIN_TOKEN:
        print("  aviso: sem ADMIN_TOKEN - /admin fica indisponível", file=sys.stderr)
    print(f"api on :{PORT}  cache ttl {TTL}s  "
          f"orçamento {guard.BUDGET} chamadas/dia  "
          f"banidos: {held}  "
          f"censo: janela de {census.EPOCH // 86400}d"
          f"{' (sal efêmero)' if census.EPHEMERAL else ''}  "
          f"apoio: {', '.join(ready) or 'nada configurado'}", file=sys.stderr)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
