#!/usr/bin/env python3
"""steamprofiler.org - the trading cards a profile is actually holding.

cards.py answers what a game's card set costs. This answers which of those
cards somebody already has, and the two together answer the question neither can
answer alone: not "what does a set cost" but "what would finishing this badge
cost you", which is a different and much smaller number.

**Where it comes from.** `steamcommunity.com/inventory/<steamid>/753/6`. No key,
no sign-in, no account of any kind involved in asking - it is the same document
a browser gets when anybody opens that profile's inventory page. 753 is the
Steam Community app and 6 is the context trading cards live in.

**How it joins.** Each entry in `descriptions` carries `market_fee_app`, which
is the appid of the game the card belongs to, and `market_hash_name`, which is
character for character the `hash` cards.py already stores for every card in
that game's set, alongside its price in cents. So the join is exact and needs
nothing invented. The tags carry `item_class_2` and `cardborder_0` as well, the
same two filters cards.py puts in its market query, which is how foils and
emoticons are dropped here rather than counted against a set they are not part
of.

**Why this is memory and not SQLite.** meta.db holds facts about games: a card
set is the same for everyone who ever asks, so it is worth keeping on disk
forever. An inventory is a fact about a person, it changes every time a card
drops, and the privacy policy says a lookup lives in memory for fifteen minutes
and is then gone, with key art as its one named exception. Writing this to disk
would make that sentence false and would buy nothing - the expensive, shareable
half of the answer is already on disk in card_sets. TTL here is fifteen minutes
for exactly that reason: it is the promise, not a tuning choice.

**One page, never paged.** `count` caps at 2000 - measured, 5000 answers 400 -
and a real card inventory fits: 382 cards came back whole in 550 KB. Past that
the answer says `truncated` rather than spending four more eight-second slots on
the shared community budget to finish reading a stranger's collection.

**Private is an answer.** A closed inventory answers 403, and that is a fact
worth reporting rather than an error worth retrying: it is cached for the full
TTL so a reload does not ask again to be told the same no.

Stdlib only, like everything else in the api container.
"""

import os
import threading
import time
import urllib.parse
from datetime import datetime, timezone

import community

INVENTORY = "https://steamcommunity.com/inventory"
# Steam Community's own appid, and the context its trading cards live in.
COMMUNITY_APP = 753
CARD_CONTEXT = 6
# Steam's own tag names, as they arrive inside descriptions[].tags.
ITEM_CLASS_CARD = "item_class_2"
BORDER_NORMAL = "cardborder_0"
# Where an item's picture lives. Same host and same shape cards.py uses, so a
# card drawn from an inventory and a card drawn from a set look identical.
ECONOMY = "https://community.fastly.steamstatic.com/economy/image"
ICON_SIZE = "96fx96f"

# Fifteen minutes, which is what the privacy policy promises a lookup lives for.
# This one is not a knob to turn for performance: turning it up past what the
# policy says would need the policy to say something else first.
TTL = float(os.environ.get("INVENTORY_TTL", "900"))
# The measured ceiling on `count`. 5000 answers 400.
MAX_ITEMS = int(os.environ.get("INVENTORY_MAX", "2000"))
# How many inventories are held at once, so a stream of lookups cannot grow the
# process without bound. The same reasoning as CACHE_MAX in api.py.
CACHE_MAX = int(os.environ.get("INVENTORY_CACHE", "300"))
# A page open waits this long for the host before answering "not read yet".
PAGE_WAIT = community.INTERVAL + 0.5
# At most one on-demand read in flight, for the same reason cards.py allows one:
# the crawl already uses most of what this host tolerates and a burst of page
# opens must not add to it.
ON_DEMAND = 1
# INVENTORY_OFFLINE=1 makes every read fail at once, the way MARKET_OFFLINE does
# for the market: it is how the panel's "not read yet" and "private" states are
# reached on purpose without waiting for Valve to have a bad day.
OFFLINE = os.environ.get("INVENTORY_OFFLINE") == "1"

_cache_lock = threading.Lock()
_cache = {}
_locks_guard = threading.Lock()
_locks = {}
_on_demand = threading.BoundedSemaphore(ON_DEMAND)


def _stamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _url(steamid):
    return (f"{INVENTORY}/{steamid}/{COMMUNITY_APP}/{CARD_CONTEXT}?"
            + urllib.parse.urlencode({"l": "english", "count": MAX_ITEMS}))


def _lock_for(steamid):
    with _locks_guard:
        lock = _locks.get(steamid)
        if lock is None:
            if len(_locks) > 4000:
                _locks.clear()
            lock = _locks[steamid] = threading.Lock()
        return lock


def _tag(desc, category):
    for tag in desc.get("tags") or []:
        if (tag.get("category") or "").lower() == category:
            return tag.get("internal_name") or ""
    return ""


def _appid_of(desc):
    """Which game's card this is.

    `market_fee_app` is the direct answer and it is on every card measured. The
    Game tag is the fallback rather than the primary because it is a string that
    has to be parsed, and the other one is already an integer."""
    app = desc.get("market_fee_app")
    if isinstance(app, int) and app > 0:
        return app
    tag = _tag(desc, "game")
    rest = tag[4:] if tag.startswith("app_") else ""
    return int(rest) if rest.isdigit() else None


def _fetch(steamid):
    """One inventory, as `(status, body)`. The seam the tests replace."""
    if OFFLINE:
        return None, None
    return community.fetch(_url(steamid))


def _parse(raw):
    """Steam's answer, folded down to `{appid: {hash: amount}}` and a few counts.

    Everything that is not a normal-border trading card is dropped here rather
    than downstream: a foil is a second collection with its own prices and its
    own badge, and counting one against the set cards.py priced would quietly
    make the set look nearer to done than it is.

    Nothing about the individual item survives - no assetid, no instanceid, no
    item name. What is kept is how many copies of each card, which is all the
    join needs and the least that answers the question."""
    assets = raw.get("assets") or []
    descriptions = raw.get("descriptions") or []
    # Steam sends the two lists separately and pairs them on this key. A
    # description that never arrives is a card that cannot be identified, and it
    # is dropped rather than guessed at.
    by_class = {}
    for desc in descriptions:
        key = (str(desc.get("classid")), str(desc.get("instanceid")))
        by_class[key] = desc

    held, icons, names = {}, {}, {}
    cards = 0
    for asset in assets:
        desc = by_class.get((str(asset.get("classid")), str(asset.get("instanceid"))))
        if desc is None:
            continue
        if _tag(desc, "item_class") != ITEM_CLASS_CARD:
            continue
        if _tag(desc, "cardborder") != BORDER_NORMAL:
            continue
        appid = _appid_of(desc)
        name = desc.get("market_hash_name")
        if not appid or not name:
            continue
        # `amount` is per asset row and one card can be spread over several
        # rows, so both are summed rather than counted.
        try:
            amount = max(1, int(asset.get("amount") or 1))
        except (TypeError, ValueError):
            amount = 1
        slot = held.setdefault(appid, {})
        slot[name] = slot.get(name, 0) + amount
        cards += amount
        icon = desc.get("icon_url")
        if icon and name not in icons:
            icons[name] = f"{ECONOMY}/{icon}/{ICON_SIZE}"
    total = raw.get("total_inventory_count")
    return {
        "held": held,
        "icons": icons,
        "cards": cards,
        "games": len(held),
        "dupes": sum(max(0, n - 1) for game in held.values() for n in game.values()),
        "count": total if isinstance(total, int) else None,
        "read": len(assets),
    }


def _shape(state, parsed=None):
    base = {
        "state": state,
        "held": {},
        "icons": {},
        "cards": 0,
        "games": 0,
        "dupes": 0,
        "count": None,
        "read": 0,
        "checked_at": _stamp() if state != "unknown" else None,
    }
    if parsed:
        base.update(parsed)
    return base


def _remember(steamid, shape):
    with _cache_lock:
        if len(_cache) >= CACHE_MAX:
            # Oldest quarter, the same eviction api.py's payload cache uses.
            for key in sorted(_cache, key=lambda k: _cache[k][0])[:CACHE_MAX // 4]:
                _cache.pop(key, None)
        _cache[steamid] = (time.monotonic() + TTL, shape)
    return shape


def known(steamid):
    """What is in memory, without asking anybody. A miss is a miss."""
    with _cache_lock:
        hit = _cache.get(steamid)
        if hit and hit[0] > time.monotonic():
            return hit[1]
        if hit:
            del _cache[steamid]
    return None


def of(steamid, wait=PAGE_WAIT):
    """One profile's cards, read now if the host will have us and answered as
    "not read yet" if it will not.

    Blocks for at most one community slot. Nobody is ever made to wait on this
    host, so a page that arrives during a cooldown draws everything else and
    says this part is still coming."""
    steamid = str(steamid)
    hit = known(steamid)
    if hit is not None:
        return hit

    got = _on_demand.acquire(blocking=False)
    if not got:
        # Somebody else is already reading one. Answering "not read yet" beats
        # queueing, because the queue is eight seconds long per person in it.
        return _shape("unknown")
    try:
        with _lock_for(steamid):
            # Somebody may have filled it while this waited for the lock.
            hit = known(steamid)
            if hit is not None:
                return hit
            if community.cooling() > 0 or not community.reserve(max_wait=wait):
                return _shape("unknown")
            try:
                status, raw = _fetch(steamid)
            except community.Throttled:
                return _shape("unknown")
            # A closed inventory. Remembered for the full TTL: it is an answer,
            # and asking again on the next page load only spends a slot to be
            # told the same no.
            if status == 403:
                return _remember(steamid, _shape("private"))
            if status != 200 or not isinstance(raw, dict) or not raw.get("success"):
                # Not an answer, so not cached. The next page open tries again.
                return _shape("unknown")
            parsed = _parse(raw)
            state = "ok"
            if parsed["count"] is not None and parsed["read"] < parsed["count"]:
                # More than MAX_ITEMS. The cards that were read are all real and
                # the totals below them are honest about being partial.
                state = "truncated"
            return _remember(steamid, _shape(state, parsed))
    finally:
        _on_demand.release()


def forget():
    """Drop everything held. For the tests, and for a person who asks."""
    with _cache_lock:
        _cache.clear()


def stats():
    with _cache_lock:
        return {"held": len(_cache)}
