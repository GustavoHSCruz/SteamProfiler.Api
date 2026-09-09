#!/usr/bin/env python3
"""steamprofiler.org - everything this service says to steamcommunity.com.

Three readers here talk to that one host: cards.py asks the Community Market
what a game's card set costs, inv.py asks what cards a person is holding, and
fetch.py scrapes the profile pages the Web API does not cover. They are three
mouths on one budget, and the budget is small - this is the half of Steam that
answers a burst of a dozen with a 429 that then lasts minutes.

**Why one module rather than a limiter each.** Three limiters do not divide the
allowance three ways, they multiply the real rate by three, and each one is
blind to the other two: cards.py takes a 429 and sleeps ten minutes while inv.py
keeps asking and renews it, and the site loses the card prices - the expensive,
shared, on-disk half of the answer - to pay for one visitor's panel. So the
pace, the cooldown and the priority yield live here, once, and everybody goes
through them.

**The two ways to take a slot.** `reserve()` waits for one and is what a crawl
does. `claim()` takes it without waiting and is what a visitor-facing scrape
does: a person is watching a spinner, and three profile scrapes queued behind an
eight-second interval would add twenty-four seconds to every cold build. The
slot is still stamped, so what a visitor spends is paid back by the crawl going
quiet for a moment, which is the right place for that cost to land.

**The Accept-Encoding bot check.** urllib cannot send a request without an
`Accept-Encoding`: when the caller sets none, http.client fills in `identity` on
its way out, and urllib gives no way to stop it. This host refuses that request
- measured, on both `/market/search/render` and `/inventory/`: `identity` and
`gzip` alike come back 429 on the first try, and the same request with no such
header at all answers 200 every time. It is a bot check rather than a rate
limit, and the only way past it is not to send the header, which is what
`skip_accept_encoding` is for.

fetch.py's scrapes are the exception and stay on urllib: they ask for HTML and
XML rather than JSON, they answer 200 that way today, and swapping their
transport on a hunch would risk the one path a visitor is actually waiting on.
When there is a measurement saying otherwise, they move here too.

Stdlib only, like everything else in the api container.
"""

import http.client
import json
import os
import threading
import time
import urllib.parse

# One request every eight seconds. Four times slower than the storefront, and
# that is not caution for its own sake: seven requests a minute is under every
# ceiling anybody has measured, and nothing that goes through here is ever in a
# hurry. The variable is still called MARKET_INTERVAL because it is set in
# deployed .env files under that name, and renaming it would silently drop the
# value and quadruple the real rate on the next deploy.
INTERVAL = float(os.environ.get("MARKET_INTERVAL", "8.0"))
TIMEOUT = 20
# Ten minutes, and up to an hour. A 429 from this host lasts noticeably longer
# than a storefront one, and asking again early only renews it.
BACKOFF_MIN = 600
BACKOFF_MAX = 3600
UA = "steamprofiler.org"

# Swapped by the tests so no test sleeps eight seconds waiting for a slot. The
# one concession in this module made purely for testability, and it is worth it:
# the alternative is a suite that either takes minutes or never exercises the
# pacing at all.
_clock = time.monotonic
_sleep = time.sleep


class Throttled(RuntimeError):
    """The host said no, too fast. Everything that catches this puts its work
    back on the queue and stops asking for a while."""


_pace_lock = threading.Lock()
_last_at = 0.0
_cool_until = 0.0
_page_guard = threading.Lock()
_page_jobs = 0
_sent = 0
_throttled = 0


def reserve(max_wait=None, background=False):
    """Wait for the next slot and take it.

    Returns False when the next slot is further off than the caller will wait,
    and that caller answers from disk instead of queueing behind it."""
    global _last_at
    deadline = None if max_wait is None else _clock() + max_wait
    while True:
        # Somebody with a page open goes first; the crawl has all day.
        if background and page_pending():
            _sleep(0.05)
            continue
        with _pace_lock:
            now = _clock()
            wait = _last_at + INTERVAL - now
            if wait <= 0:
                _last_at = now
                return True
        if deadline is not None and now + wait > deadline:
            return False
        _sleep(min(wait, 0.25))


def claim():
    """Take the slot now, without waiting for it.

    For the requests a visitor is watching. It never refuses and never blocks;
    what it does is stamp the clock, so the next background reader waits its
    full interval and the host sees the same average rate either way."""
    global _last_at
    with _pace_lock:
        _last_at = _clock()


def note_429():
    """Put the whole host to sleep, for every reader.

    Called from anywhere a 429 arrives, including fetch.py's scrapes. Before
    this module those scrapes swallowed the refusal and cards.py went on asking
    into a host that had already said no."""
    global _cool_until, _throttled
    with _pace_lock:
        _cool_until = _clock() + BACKOFF_MIN
        _throttled += 1


def cooling():
    """Seconds left on the cooldown, or 0. Check it before spending a slot:
    asking during a refusal is what renews the refusal."""
    with _pace_lock:
        return max(0.0, _cool_until - _clock())


def page_enter():
    global _page_jobs
    with _page_guard:
        _page_jobs += 1


def page_leave():
    global _page_jobs
    with _page_guard:
        _page_jobs = max(0, _page_jobs - 1)


def page_pending():
    with _page_guard:
        return _page_jobs > 0


def fetch(url):
    """One request to this host, as `(status, data)`.

    Raises Throttled on a 429 so the caller can back off. `status` is None when
    the request never got an answer at all, and `data` is None whenever the body
    was not JSON this could parse.

    Two levels rather than one because a 403 is a real answer: it is an
    inventory that exists and is not being shown to us, which the page says out
    loud. Flattening it into None would make a private inventory
    indistinguishable from Valve being down, and those two deserve different
    sentences. Callers that have no such case use get_json below and never see
    a status.

    The caller checks its own `*_OFFLINE` flag before getting here: which
    degraded state is being reached on purpose belongs to the module doing the
    reaching, not to the transport."""
    global _sent
    parts = urllib.parse.urlsplit(url)
    conn = http.client.HTTPSConnection(parts.netloc, timeout=TIMEOUT)
    try:
        conn.putrequest("GET", parts.path + ("?" + parts.query if parts.query else ""),
                        skip_accept_encoding=True)
        conn.putheader("User-Agent", UA)
        conn.putheader("Accept", "application/json")
        conn.endheaders()
        answer = conn.getresponse()
        body = answer.read()
        _sent += 1
        if answer.status == 429:
            note_429()
            raise Throttled(url)
        if answer.status != 200:
            return answer.status, None
        try:
            return 200, json.loads(body.decode("utf-8", "replace"))
        except json.JSONDecodeError:
            return 200, None
    except (OSError, TimeoutError, http.client.HTTPException):
        return None, None
    finally:
        conn.close()


def get_json(url):
    """One request, for the callers that treat every refusal the same way:
    the body, or None."""
    return fetch(url)[1]


def stats():
    """What /healthz prints about this host."""
    with _pace_lock:
        return {"sent": _sent, "throttled": _throttled,
                "cooling_for": round(max(0.0, _cool_until - _clock()), 1)}


def reset():
    """Back to a cold process. For the tests, which share one module across
    cases and would otherwise carry a cooldown from one into the next."""
    global _last_at, _cool_until, _page_jobs, _sent, _throttled
    with _pace_lock:
        _last_at = 0.0
        _cool_until = 0.0
        _sent = 0
        _throttled = 0
    with _page_guard:
        _page_jobs = 0
