"""steamprofiler.org - the gate in front of the Steam key.

nginx already refuses more than 120 requests a minute from one address. That
stops a hammer; it does not stop the thing this site actually has to worry
about, which is that **every visitor's lookup spends the owner's key**. A
hundred requests a minute, each one a cold profile, is a thousand calls a
minute against an allowance of a hundred thousand a day - inside every limit
nginx knows about, and gone by lunchtime.

So the gate here counts what a request *costs* rather than how many there were,
and it counts three different things:

    the bucket      cost-weighted tokens per address. A cached page is 1, a
                    cold profile is 8, the cross-library rarity scan is 60,
                    because that is roughly what each one really spends.
    the subjects    distinct profiles per address per hour. This is the one
                    that stops enumeration: a person looks up themselves, a
                    couple of friends and a rival. Nobody reads forty strangers
                    an hour, and a scraper wants forty thousand.
    the budget      calls to Steam per day, across everyone. The meter is
                    fetch.calls(). Over the ceiling, cold lookups are refused
                    and everything already cached keeps being served - the site
                    degrades to what it knows instead of going dark.

An address that keeps getting refused is blocked outright for a while, because
the cheapest request to serve is the one that is not served.

All of it is in memory, like the payload cache, and a restart forgets all of
it. That is the right trade at this size: the state is worth nothing an hour
later, and writing it to disk would mean a database write on every request.

The one exception is bans.py, and it is an exception for a reason. Everything
above is about a visitor going too fast, which stops mattering minutes later.
A request for /.env is not that - it is a scan for a credential, it earns two
days, and two days is longer than this container stays up. So that one lives
on disk.

census.py is the second exception, and it is not this module's business but it
would be misleading to leave it out of a docstring that says nothing else is
written down. It counts traffic - not requests, and nothing this file computes.
It runs off the gate rather than off check(), because the gate is what sees the
static side of the site and this file never does: a visitor who reads a page and
searches nothing passes through here zero times.

Addresses are never stored. What is kept is store.ip_hash(address) - the same
salted, truncated digest the message board counts votes with, which is useless
outside this server and cannot be turned back into an address. census.py hashes
the same addresses under a different, rotating salt, on purpose: a handle that
expires is no use for a ban, and a handle that never expires is no business of
a traffic count.
"""

import os
import threading
import time
from collections import deque

import bans
import fetch
import store

# ── The bucket ───────────────────────────────────────────────────────
# Capacity is the burst a reader is allowed; refill is what they can keep up.
# A dashboard is ~10 units, a game page ~6. So this is a burst of thirty pages
# and a sustained five units a second, which no person reaches and no crawler
# stays under.
CAPACITY = float(os.environ.get("GUARD_CAPACITY", "300"))
REFILL = float(os.environ.get("GUARD_REFILL", "5"))

# What each route costs. Anything not listed is CHEAP: reading something this
# process already has, or something that does not touch Steam at all.
CHEAP = 1
COST = {
    "resolve": 2,
    "profile": 8,
    "game": 5,
    # Achievement schema + global rarity + live players + news on a cold app.
    # Each layer has its own cache, but the first reader really can spend four.
    "public_game": 4,
    "og": 12,
    # The embeds. Same shape as the link preview: resolve, one profile, and a
    # drawing that costs nothing. Priced identically, and in KEY_SPENDING for
    # the same reason - the profile behind it is a handful of calls to Steam.
    #
    # The versus card is two profiles, and it is not listed separately: the
    # handler spends this twice, once per person, which is the same arithmetic
    # written where the second lookup actually happens. The artwork is one
    # profile like the rest - the picture behind it comes off Steam's CDN,
    # which has no key and no allowance of ours to spend.
    "embed": 12,
    # A warm one is a single row out of SQLite and a cold one is a trip to the
    # storefront, and the handler cannot know which it will be before it asks.
    # Priced as the average of the two, which is to say cheaply.
    "price": 2,
    # Three calls per game across the top of the library. It is the most
    # expensive thing the site can be asked for, and it is priced like it.
    "rarities": 60,
    # One call per friend, up to a ceiling api.py sets. A third of the rarity
    # scan's calls, and priced at a third of it - but it is in the same class
    # as that one rather than in the class of a page: both are buttons a
    # visitor presses on purpose, and neither happens by opening anything.
    "mates": 20,
    # The profile it needs, plus one call for the badges and one for the
    # community badge's checklist. The two caches it reads after that are on
    # disk and cost nothing.
    "cards": 10,
    # One game's card set. The market has no key and no allowance of ours to
    # spend, and a warm one is a single row out of SQLite - the same case as a
    # price, and priced the same way.
    "cards_set": 2,
    # What a library would run on Linux. The profile it needs is priced on its
    # own the first time; this reads that answer against a local cache and
    # queues whatever is missing for a worker that is not on this request.
    # Nothing here calls Steam and nothing here waits on ProtonDB.
    "deck": 2,
    # The two house routes. Neither calls Steam and neither spends a byte of
    # the key: both are a query against a local index that a weekly worker
    # fills. Priced at one, which is what a read of SQLite costs anybody -
    # not zero, because an address that has been shut out should be shut out
    # of everything, and not more, because nothing scarce is being spent.
    "houses": 1,
    "house": 1,
    # A list of apps out of the store cache. Free when nobody asked for live
    # counts, and one call to Steam per named app when they did - capped at a
    # dozen by the handler, and each of those cached five minutes across every
    # visitor. Priced between a price and a game page, which is where a screen
    # of eleven rows that mostly answers off disk belongs.
    "apps": 3,
    # The wishlist itself answers without a key - measured, it is one of the
    # few IWishlistService methods that does - but the followed games beside it
    # do not, and the prices come out of the store cache for free. One key call
    # and a disk read.
    "wishlist": 4,
    # What somebody published to the Workshop. Keyless, and only ever asked for
    # when the count on the profile said there was something to ask about.
    "workshop": 3,
    # One inventory off the community host, joined against card sets already on
    # disk. No key at all, which is why it is out of KEY_SPENDING below: a spent
    # allowance must not take down a panel that never spent any of it.
    "inventory": 4,
    # The owner's own trading, which is the only account IEconService can be
    # asked about. Owner-only and off by default.
    "econ": 6,
}

# The routes that actually spend the key. Key art is fetched cold from Steam's
# CDN and costs nothing out of the allowance, so a spent budget must not take
# the pictures down with it - that would break every game page for the rest of
# the day to save nothing. The price of a game is the same case: it comes from
# the storefront, which has no key and no allowance to spend.
KEY_SPENDING = frozenset(("resolve", "profile", "game", "public_game", "og", "embed",
                          "rarities", "mates", "cards", "wishlist", "econ"))

# Distinct profiles per address, per hour.
SUBJECT_WINDOW = 3600
SUBJECT_MAX = int(os.environ.get("GUARD_SUBJECTS", "30"))

# Calls to Steam per day, everyone together. Steam allows 100k; half of it is
# the ceiling here, so a bad day still leaves the key working for tomorrow.
BUDGET = int(os.environ.get("GUARD_BUDGET", "50000"))

# Refusals inside this window before an address is shut out, and for how long.
#
# Thirty seconds, and it used to be nine hundred. The change came with bans.py:
# once anything acting in bad faith gets two days for asking after a credential,
# this stopped needing to be a punishment and went back to being what it should
# always have been, which is backpressure. It says "not this fast", waits about
# as long as somebody would wait anyway, and opens.
#
# What that costs is honest: a scraper can now cycle - twenty-five refusals,
# thirty seconds, twenty-five more, forever. That is affordable because a
# refusal is the cheapest thing this process does, nginx caps the address at
# 120r/m regardless, and the daily budget is what actually protects the key.
# What it buys is that the one population who ever reaches this by accident -
# several people behind one carrier NAT or one office address - is not shut out
# of a website for a quarter of an hour over somebody else's traffic.
STRIKE_WINDOW = 600
STRIKE_MAX = int(os.environ.get("GUARD_STRIKES", "25"))
BLOCK_FOR = int(os.environ.get("GUARD_BLOCK", "30"))

# How many cold builds may be waiting at once. api.py serialises them behind one
# lock, so a queue of a hundred is a hundred connections held open for minutes.
# Past this the answer is "busy", immediately, which is the honest one.
MAX_WAITING = int(os.environ.get("GUARD_WAITING", "8"))

# Housekeeping: forget an address that has been quiet, and never hold more
# than this many of them.
IDLE = 7200
MAX_TRACKED = 20000

# Whether a request from a private address is the owner's own network and so
# exempt. This is true today and it is worth knowing why it might stop being:
# nginx has to be putting the *visitor's* address in X-Real-IP. It does, via
# set_real_ip_from - but if a proxy is ever added in front without forwarding
# X-Forwarded-For, every request would arrive private and every request would
# be exempt. /healthz reports how many were, which is how that shows up before
# it matters. Set GUARD_TRUST_PRIVATE=0 to hold the local network to the same
# limits as everyone else.
TRUST_PRIVATE = os.environ.get("GUARD_TRUST_PRIVATE", "1") != "0"

_lock = threading.Lock()
_seen = {}
_waiting = 0
_day = None
_day_base = 0
_denied = 0
_exempt = 0


class Denied(Exception):
    """A refusal the visitor should see, with the status it deserves."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def _private(address):
    """Loopback and RFC1918. nginx puts the real client in X-Real-IP - and with
    set_real_ip_from in front, that stays true behind a TLS terminator - so a
    private address here means the request came from the owner's own network."""
    a = (address or "").strip()
    if a in ("127.0.0.1", "::1", ""):
        return True
    if a.startswith(("10.", "192.168.")):
        return True
    parts = a.split(".")
    if len(parts) == 4 and parts[0] == "172":
        try:
            return 16 <= int(parts[1]) <= 31
        except ValueError:
            return False
    return False


def _slot(who, now):
    """The record for one address hash, created full and pruned when crowded."""
    hit = _seen.get(who)
    if hit is None:
        if len(_seen) >= MAX_TRACKED:
            for key in [k for k, v in _seen.items() if now - v["seen"] > IDLE]:
                _seen.pop(key, None)
        if len(_seen) >= MAX_TRACKED:
            oldest = min(_seen, key=lambda k: _seen[k]["seen"])
            _seen.pop(oldest, None)
        hit = _seen[who] = {"tokens": CAPACITY, "at": now, "seen": now,
                            "subjects": {}, "strikes": deque(), "blocked": 0}
    return hit


def _strike(slot, now, status, message):
    """Count a refusal, and stop answering this address if they keep coming."""
    global _denied
    _denied += 1
    strikes = slot["strikes"]
    strikes.append(now)
    while strikes and now - strikes[0] > STRIKE_WINDOW:
        strikes.popleft()
    if len(strikes) >= STRIKE_MAX:
        slot["blocked"] = now + BLOCK_FOR
        strikes.clear()
        raise Denied(429, "@err.blocked")
    raise Denied(status, message)


def spent_today():
    """Calls to Steam since midnight UTC, and the day they belong to."""
    global _day, _day_base
    today = time.strftime("%Y-%m-%d", time.gmtime())
    total = fetch.calls()
    if _day != today:
        _day, _day_base = today, total
    return total - _day_base


def budget_ok():
    """Whether a cold lookup can still be afforded. Checked before the build,
    never inside it: a half-spent budget should not abandon work in progress."""
    with _lock:
        return spent_today() < BUDGET


def check(address, kind, subject=None):
    """Let this request through, or raise Denied.

    `kind` names the route so it can be priced; `subject` is the profile being
    read, which is what the enumeration ceiling counts."""
    global _exempt
    if TRUST_PRIVATE and _private(address):
        _exempt += 1
        return
    # nginx already asked bans.py about this address before the request reached
    # the proxy, so in normal operation this never fires. It is here for the
    # case where it would matter most: if the auth_request line is ever lost
    # from the config, the API - the part that spends the key - stays shut to
    # anything already banned. A door that only locks in one place is not shut.
    if bans.until(address) is not None:
        raise Denied(403, "@err.banned")
    who = store.ip_hash(address)
    now = time.monotonic()
    cost = COST.get(kind, CHEAP)

    with _lock:
        slot = _slot(who, now)
        slot["seen"] = now

        if slot["blocked"]:
            if now < slot["blocked"]:
                raise Denied(429, "@err.blocked")
            slot["blocked"] = 0

        # Refill for the time that passed, then pay.
        slot["tokens"] = min(CAPACITY, slot["tokens"] + (now - slot["at"]) * REFILL)
        slot["at"] = now
        if slot["tokens"] < cost:
            wait = int((cost - slot["tokens"]) / REFILL) + 1
            _strike(slot, now, 429, f"@err.slow_down|n={wait}")
        slot["tokens"] -= cost

        if subject:
            subjects = slot["subjects"]
            for old in [s for s, t in subjects.items() if now - t > SUBJECT_WINDOW]:
                subjects.pop(old, None)
            if subject not in subjects and len(subjects) >= SUBJECT_MAX:
                _strike(slot, now, 429, f"@err.too_many_profiles|n={SUBJECT_MAX}")
            subjects[subject] = now


def blocked_for(address):
    """Seconds left on the short shut-out, or 0.

    Read-only, and that is the whole contract: nginx calls this through the
    gate on every request the site serves, so it must report what check() has
    already decided without spending a token, counting a strike or extending
    anything. Calling check() here instead would mean every image on a page
    paying its own way through the bucket.

    Read without the lock, for the same reason bans.until() is: dict.get and a
    field read are single operations the GIL will not tear, and putting the
    whole static site behind this mutex would cost more than the answer. Worst
    case is a value from a moment ago, on a window half a minute wide."""
    if TRUST_PRIVATE and _private(address):
        return 0
    slot = _seen.get(store.ip_hash(address))
    if slot is None:
        return 0
    left = slot["blocked"] - time.monotonic()
    return int(left) if left > 0 else 0


def afford(address, kind, subject, is_cold):
    """check(), plus the two ceilings that only apply to work not yet done.

    A cached answer costs nothing to serve, so a spent budget and a full queue
    are no reason to refuse one - the site keeps answering for everything it
    already knows and only stops learning new things."""
    check(address, kind if is_cold else "cached", subject)
    if not is_cold or kind not in KEY_SPENDING:
        return
    if not budget_ok():
        raise Denied(503, "@err.budget")


class Cold:
    """Held around a cold build, so the queue in front of api.py's one lock has
    a depth limit rather than growing until the connections time out."""

    def __enter__(self):
        global _waiting
        with _lock:
            if _waiting >= MAX_WAITING:
                raise Denied(503, "@err.busy")
            _waiting += 1
        return self

    def __exit__(self, *exc):
        global _waiting
        with _lock:
            _waiting -= 1
        return False


def state():
    """What the gate is holding, for /healthz. No addresses, only counts."""
    with _lock:
        blocked = sum(1 for s in _seen.values() if s["blocked"] > time.monotonic())
        return {
            "tracked": len(_seen),
            "blocked": blocked,
            "denied": _denied,
            # Requests waved through as local. If this climbs while `tracked`
            # stays at zero, nginx is not passing the visitor's address on and
            # nothing here is actually limiting anything.
            "exempt_local": _exempt,
            "waiting": _waiting,
            "steam_calls_today": spent_today(),
            "budget": BUDGET,
            # The two-day shut-outs, which outlive this process. `blocked`
            # above is the thirty-second kind and does not.
            "bans": bans.state(),
        }
