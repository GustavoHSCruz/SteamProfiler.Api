#!/usr/bin/env python3
"""steamprofiler.org - reputation, a number from 0 to 100. Experimental.

The comment wall was the old answer to "can I trust this account": a column of
"+rep good trader". It stopped meaning anything the day it became free to
fill, and a wall of them is now as easy to buy as the account it sits on. What
is expensive to fake is time and money: years on the account, hours actually
played across those years, a library, a level, friends who are not themselves
banned. So the score is built from those, each with a weight, and the weights
add up to 100.

A pure function over the profile payload build_profile() already assembled,
plus one aggregate the friend list brings along (how many of the friends it
looked at carry a VAC or game ban). Nothing here calls Steam, nothing here is
stored, and it is recomputed every time the profile is.

A signal this profile does not publish is unknown, not zero. A private friend
list is an ordinary privacy choice, and scoring it as "no friends" would punish
exactly the people who read the privacy settings. So an unknown signal leaves
the sum, the score is taken over what was measured, and `known` says how much
of the 100 that was: a 90 measured over 55 points of evidence is a weaker 90
than one measured over all of it, and the page says so.

Some facts do not weigh, they cap. A community ban or a trade ban is Steam
itself saying this account misbehaved with other people, and no amount of
hours makes that a 90. Those set a ceiling instead of subtracting points.

Every weight and every curve is a guess, which is what "experimental" means on
the page: switched on to see whether it is any use. VERSION moves when any of
them do, so a number can always be read against the rules that made it.
"""

import math
from datetime import date

VERSION = 5

# Steam's placeholder, the question mark on a blue square. Every account that
# never chose a picture has this exact hash in its avatar URL.
DEFAULT_AVATAR = "fef49e7fa7e1997310d705b2a6158ff8dc1cdfeb"

# Weight of each signal, in points out of 100. The order is the order the page
# lists them in: heaviest first.
WEIGHTS = {
    "age": 16,          # how old the account is
    "bans": 16,         # the account's own VAC and game bans
    "sustained": 10,    # hours per year x years in use: used, and for a long time
    "limited": 8,       # has spent the five dollars Steam asks for
    "level": 8,         # Steam level
    "friend_bans": 7,   # share of the friends checked that carry a ban
    "library": 7,       # games owned
    "hours": 5,         # hours played, all games
    "variety": 5,       # not an account for one game
    "friends": 5,       # how many friends
    "profile": 4,       # own avatar, own URL, something written, somewhere said
    "items": 3,         # background, frame, animated avatar, mini profile
    "badges": 3,        # badges earned
    "achievements": 2,  # achievements earned
    "community": 1,     # groups, screenshots, reviews, Workshop
}
assert sum(WEIGHTS.values()) == 100

# Ceilings, lowest wins. Each is Steam's own verdict on how the account behaved
# around other people, which is the question the score is trying to answer.
CAPS = {
    "community_ban": 25,
    "barely_played": 30,    # fewer hours than HOURS_FLOOR on the clock
    "trade_banned": 35,
    "recent_ban": 40,       # a VAC or game ban in the last year
    "new_account": 40,      # younger than a year
    "limited": 50,
    "trade_probation": 60,
}

# Friend-ban share at which that signal is worth nothing. Some banned friends
# is normal on any account that has played CS for a decade; a quarter of them
# is a circle.
FRIEND_BAN_ZERO = 0.25
# Fewer friends checked than this and the share is noise.
FRIEND_BAN_MIN = 5

# Averaging this many hours a day since the account was opened is not a person,
# it is an idler or an hour booster, and the signal that rewards long use is
# the one it was built to fool.
IMPLAUSIBLE_HOURS_PER_DAY = 16

# Sustained use is use that has not stopped. A year without a game costs
# nothing; past that the signal fades, and five years of silence is none.
IDLE_GRACE_DAYS = 365
IDLE_ZERO_DAYS = 5 * 365

# Hours per year of use at which sustained use is full: about fifty minutes a
# day, every day, for as long as the account was played.
HOURS_PER_YEAR_FULL = 300


# Below these, a signal has not started. A single game, a single friend and
# seven hours on the clock are what an account has an hour after it is made,
# and a curve that pays for them pays for nothing.
FLOORS = {"hours": 20, "library": 2, "friends": 3, "badges": 1,
          "achievements": 10, "variety": 1}

# How the curves bend, once the floor is off. The first version of this used a
# log curve, which is concave to the point of absurdity: seven hours scored 26
# out of 100 and one game scored 12, so an account with nothing in it was a
# third of the way up. Just under 1 is nearly a straight line - the score is
# earned all the way to the top, and the early slice is worth what it is.
CURVE = 0.9


def curve(x, full, floor=0):
    """0 up to `floor`, 1 at `full` and past it, and `bend` in between.

    The floor is subtracted rather than tested, so nothing jumps: an account
    one hour over it scores about nothing, not the first slice of the curve."""
    if x is None:
        return None
    reach = full - floor
    if reach <= 0 or x is None or x <= floor:
        return 0.0
    return min(1.0, ((x - floor) / reach) ** CURVE)


def _age(days):
    # A year is 0.12, three years 0.33, five 0.53, ten 1. The old curve gave
    # a year 0.32, which read a nine-month-old account as a third of a
    # lifetime.
    if days is None:
        return None
    if days < 30:
        return 0.0
    return curve(days, 3650, 30)


def _idle_days(library):
    """Days since the most recent game played, or None when no game in the
    library carries a date (Steam dates nothing played before 2009)."""
    dates = [g["last_played"] for g in library if g.get("last_played")]
    if not dates:
        return None
    try:
        last = date.fromisoformat(max(dates))
    except ValueError:
        return None
    return max(0, (date.today() - last).days)


def _awake(idle):
    """1 while the account has been played within IDLE_GRACE_DAYS, falling in
    a straight line to 0 at IDLE_ZERO_DAYS without a game. Unknown is 1: no
    date is not the same as no play."""
    if idle is None or idle <= IDLE_GRACE_DAYS:
        return 1.0
    return max(0.0, 1 - (idle - IDLE_GRACE_DAYS) / (IDLE_ZERO_DAYS - IDLE_GRACE_DAYS))


def score(p, friend_bans=None):
    """The reputation of one built profile.

    `p` is build_profile()'s payload. `friend_bans` is {"sampled", "flagged"}
    or None when the friend list is private."""
    pf = p.get("profile") or {}
    totals = p.get("totals") or {}
    library = p.get("library") or []
    days = pf.get("days_since")
    hours = totals.get("hours")
    bans = pf.get("bans") or {}
    items = pf.get("items") or {}

    got = {}   # key -> (fraction 0..1 or None, value shown beside it)

    age = _age(days)
    got["age"] = (age, days)

    hours_f = curve(hours, 3000, FLOORS["hours"])

    # How much account there is to have a record on. Both matter and neither
    # substitutes: an account opened in 2011 and never played is as unproven
    # as one opened last week with a thousand hours bought into it.
    substance = 0.0 if age is None or hours_f is None else math.sqrt(age * hours_f)

    # A ban does not heal, but ten years is not last month. Half the weight
    # comes back over a decade, split across however many there are.
    #
    # A clean record is worth what there was to keep clean. This used to be a
    # flat 100 for everybody, which handed a seven-hour account sixteen points
    # - over half of everything it scored - for never having had the chance to
    # be banned. Nothing here is taken away from an account that behaved: the
    # points arrive as the account does.
    n_bans = (bans.get("vac") or 0) + (bans.get("game") or 0)
    if n_bans:
        since = bans.get("days_since") or 0
        got["bans"] = (0.5 * min(1.0, since / 3650) / n_bans, n_bans)
    else:
        got["bans"] = (substance, 0)

    per_day = totals.get("hours_per_day")
    idle = _idle_days(library)
    shown = {"per_day": per_day, "idle_days": idle}
    if age is None or hours_f is None:
        got["sustained"] = (None, None)
    elif per_day is not None and per_day > IMPLAUSIBLE_HOURS_PER_DAY:
        got["sustained"] = (0.0, shown)
    else:
        # The years are the ones the account was in use: from opening to the
        # last game played, not to today. An account played for two years
        # and shut eight years ago is two years of use, not ten.
        span = days if idle is None else max(0, days - idle)
        # How hard those years were used: hours per year of use, full at
        # HOURS_PER_YEAR_FULL and linear below it. Total hours on their own
        # say nothing here - six hundred hours over nine years is eleven
        # minutes a day, and a log curve on the total scored that as 80%.
        per_year = hours / (span / 365) if span >= 30 else 0.0
        pace = min(1.0, per_year / HOURS_PER_YEAR_FULL)
        shown["per_year"] = round(per_year)
        # A product: high only when both are. Three thousand hours on an
        # account from last spring and ten years with nothing played are both
        # close to nothing. Then it fades with the silence since.
        got["sustained"] = (_age(span) * pace * _awake(idle), shown)

    limited = pf.get("limited")
    got["limited"] = (None if limited is None else (0.0 if limited else 1.0), limited)

    # Where the level sits against every other account, which Steam
    # publishes per level. A curve of our own made level 24 worth 70% when
    # it is above 97% of Steam; the percentile is the scale the level
    # actually lives on. Level 0 has no percentile and is worth nothing.
    level = pf.get("level")
    pct = pf.get("level_percentile")
    if level is None:
        got["level"] = (None, None)
    elif pct is not None:
        got["level"] = (min(1.0, pct / 100), level)
    else:
        got["level"] = (0.0 if level == 0 else curve(level, 100), level)

    if friend_bans and (friend_bans.get("sampled") or 0) >= FRIEND_BAN_MIN:
        share = friend_bans["flagged"] / friend_bans["sampled"]
        got["friend_bans"] = (max(0.0, 1 - share / FRIEND_BAN_ZERO),
                              {"flagged": friend_bans["flagged"],
                               "sampled": friend_bans["sampled"]})
    else:
        got["friend_bans"] = (None, None)

    owned = totals.get("owned")
    got["library"] = (None if owned is None else curve(owned, 300, FLOORS["library"]), owned)
    got["hours"] = (hours_f, hours)

    # Games with at least an hour in them, discounted when one game is nearly
    # the whole account - the shape of a smurf, bought for one queue.
    real = sum(1 for g in library if (g.get("hours") or 0) >= 1)
    top = totals.get("top_game_share") or 0
    focus = 1.0 if top < 80 else max(0.3, 1 - (top - 80) / 20 * 0.7)
    got["variety"] = (curve(real, 40, FLOORS["variety"]) * focus,
                      {"games": real, "top_share": round(top)})

    friends = pf.get("friends")
    if friends is None:
        friends = (p.get("friend_list") or {}).get("total")
    got["friends"] = (None if friends is None
                      else curve(friends, 150, FLOORS["friends"]), friends)

    avatar = pf.get("avatar") or ""
    marks = {
        "avatar": bool(avatar) and DEFAULT_AVATAR not in avatar,
        "url": bool(pf.get("custom_url")),
        "text": bool(pf.get("bio") or pf.get("showcase")),
        "place": bool(pf.get("location") or pf.get("country")),
    }
    got["profile"] = (sum(marks.values()) / 4, [k for k, v in marks.items() if v])

    worn = [k for k in ("background", "frame", "avatar", "mini") if items.get(k)]
    # The background is the one visible from across the room, so it is worth
    # as much as the other three together.
    got["items"] = ((0.4 if "background" in worn else 0)
                    + 0.2 * len([k for k in worn if k != "background"]), worn)

    badges = pf.get("badge_count")
    got["badges"] = (None if badges is None
                     else curve(badges, 50, FLOORS["badges"]), badges)

    ach = pf.get("achievements_total")
    got["achievements"] = (None if ach is None
                           else curve(ach, 2000, FLOORS["achievements"]), ach)

    parts = [k for k in ("groups", "screenshots", "reviews", "workshop") if pf.get(k)]
    got["community"] = (len(parts) / 4, parts)

    signals, points, known = [], 0.0, 0
    for key, weight in WEIGHTS.items():
        frac, value = got[key]
        # `score` is the signal on its own, 0 to 100, which is what the page
        # shows. The weight is how much of the total it moves, and stays
        # in the payload for anyone reading the arithmetic.
        row = {"key": key, "weight": weight, "value": value}
        if frac is None:
            row["score"] = row["points"] = None
        else:
            row["score"] = round(frac * 100)
            row["points"] = round(weight * frac, 1)
            points += weight * frac
            known += weight
        signals.append(row)

    raw = round(points / known * 100) if known else 0

    caps = []
    # An account can be everything else and still be too new or too empty to
    # have shown it. Both are ceilings rather than deductions, and the page
    # says which one it hit and what the score would have been.
    if days is not None and days < 365:
        caps.append("new_account")
    if hours is not None and hours < FLOORS["hours"] + 5:
        caps.append("barely_played")
    if bans.get("community"):
        caps.append("community_ban")
    if bans.get("economy") == "banned":
        caps.append("trade_banned")
    elif bans.get("economy"):
        caps.append("trade_probation")
    if n_bans and (bans.get("days_since") or 0) < 365:
        caps.append("recent_ban")
    if limited:
        caps.append("limited")
    cap = min(caps, key=lambda c: CAPS[c]) if caps else None
    final = min(raw, CAPS[cap]) if cap else raw

    return {
        "version": VERSION,
        "score": final,
        # What the score would be without the ceiling, so the page can say
        # what the ceiling took.
        "raw": raw,
        "cap": {"reason": cap, "max": CAPS[cap]} if cap and CAPS[cap] < raw else None,
        "known": known,
        "signals": signals,
    }
