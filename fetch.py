#!/usr/bin/env python3
"""Steam profile reader for steamprofiler.org. Any public profile, not just one.

Reads STEAM_API_KEY from the environment; the key never leaves the server.
api.py imports this module and serves the two builders over HTTP:

    build_profile()      -> the dashboard payload for the current user
    build_game(appid)    -> one game's payload for the current user

Which user that is comes from set_user(). That is module state rather than a
parameter because threading a steamid through thirty call sites bought nothing
here - api.py holds a lock around set_user + build, and the cache in front of it
means almost no request reaches this code at all.
"""

import concurrent.futures
import html
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

import cards
import community
import fx
import inv
import meta

API = "https://api.steampowered.com"
OPENDOTA = "https://api.opendota.com/api"
CS2_APPID = 730
DOTA_APPID = 570
KEY = os.environ.get("STEAM_API_KEY", "").strip()
# Whose site this is. The footer credit and the example link point here.
OWNER_ID = os.environ.get("STEAM_ID", "76561198086380973")
OWNER_VANITY = os.environ.get("STEAM_VANITY", "gordziilla")
# Overridable so the container can mount the code read-only and the cache apart.
SITE_DIR = Path(os.environ.get("SITE_DIR") or Path(__file__).parent / "site")
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"

# ── Current subject ──────────────────────────────────────────────────
STEAM_ID = OWNER_ID
# Dota's own APIs are keyed on the 32-bit account id, not the 64-bit steamid.
ACCOUNT_ID = int(OWNER_ID) - 76561197960265728
PROFILE_URL = f"https://steamcommunity.com/id/{OWNER_VANITY}/"


def set_user(steamid, vanity=None):
    """Point the module at a profile. Call under a lock, then build."""
    global STEAM_ID, ACCOUNT_ID, PROFILE_URL
    STEAM_ID = str(steamid)
    ACCOUNT_ID = int(STEAM_ID) - 76561197960265728
    PROFILE_URL = (
        f"https://steamcommunity.com/id/{vanity}/" if vanity
        else f"https://steamcommunity.com/profiles/{STEAM_ID}/"
    )


VANITY_RE = re.compile(r"^[A-Za-z0-9_-]{2,64}$")
STEAMID_RE = re.compile(r"^7656119\d{10}$")


def resolve(query):
    """Turn whatever someone typed into a steamid64.

    Accepts a steamid64, a vanity name, or any profile URL of either shape.
    Returns (steamid64, vanity_or_None) or (None, None) when nothing matches."""
    q = (query or "").strip()
    if not q:
        return None, None
    if "steamcommunity.com" in q:
        m = re.search(r"/profiles/(7656119\d{10})", q)
        if m:
            return m.group(1), None
        m = re.search(r"/id/([A-Za-z0-9_-]{2,64})", q)
        if m:
            q = m.group(1)
    if STEAMID_RE.match(q):
        return q, None
    if not VANITY_RE.match(q):
        return None, None
    got = get_json("ISteamUser/ResolveVanityURL/v1/", required=False,
                   with_steamid=False, vanityurl=q)
    if got.get("success") == 1 and got.get("steamid"):
        return got["steamid"], q
    return None, None

# How many of the most-played games get a page of their own.
TABLE_ROWS = 25
# Dota portraits live under a predictable slug derived from npc_dota_hero_*.
DOTA_ART = "https://cdn.cloudflare.steamstatic.com/apps/dota2/images/dota_react/heroes"
# Every app has key art at a predictable path. Same for everyone, so it is the
# one thing a page with no player data can still be built around.
APP_ART = "https://cdn.cloudflare.steamstatic.com/steam/apps"


class SteamError(RuntimeError):
    """A lookup that cannot be satisfied - api.py turns this into a 4xx/5xx."""


class GameNotFound(RuntimeError):
    """The storefront has positively confirmed that this app does not exist."""


# Every outbound request to Steam, counted. The key's daily allowance is shared
# by everyone who uses the site, so guard.py spends it against a budget - and a
# budget needs a meter. Counted here rather than at the route, because one
# lookup is a dozen calls and only this layer knows how many.
_calls_lock = threading.Lock()
_calls = 0
PUBLIC_GAME_TTL = 24 * 3600
_catalog_lock = threading.Lock()
_catalog_cache = {}
_stats_catalog_cache = {}
_live_lock = threading.Lock()
_players_cache = {}
_news_cache = {}
# Where a Steam level sits against everybody else's. Keyed on the level and not
# on the person, because that is what it is: one integer in, one percentage out,
# the same answer for everyone at that level. So one call serves every visitor
# who is level 42 rather than one per lookup, and a day is a short life for a
# distribution that moves over months.
LEVEL_TTL = 24 * 3600
_level_lock = threading.Lock()
_level_cache = {}


def spend(n=1):
    global _calls
    with _calls_lock:
        _calls += n


def calls():
    with _calls_lock:
        return _calls



# How many of a build's requests may be in the air at once. Ten because that is
# the width of the fan-out in build_profile, and there is nothing to gain by
# queueing the tenth behind the ninth: they are ten independent questions to a
# host that answers each of them in anything from a fifth of a second to five,
# and asked one after another the visitor waits for the sum instead of for the
# slowest one.
#
# Ten threads is not ten times the load on Steam. It is the same ten requests a
# cold build always made, made together - the allowance guard.py spends against
# is counted per request and not per second, and spend() counts these the same
# way it counted them in a row.
#
# Overridable, and setting it to 1 turns every gather() below back into the
# plain sequence it replaced. That is the switch to reach for when a build is
# behaving strangely and the question is whether concurrency is why.
FANOUT = int(os.environ.get("FETCH_FANOUT", "10"))


def gather(jobs):
    """Run independent fetches together and return {name: result}.

    `jobs` maps a name to a callable of no arguments; the answer maps the same
    names to what each returned. Written order is what decides which failure a
    visitor sees: the jobs are waited on in the order they appear, so a build
    that used to die on the first of two bad calls still dies on that one and
    still reports its message, rather than on whichever thread finished first.

    Exceptions cross back into the calling thread untouched - a SteamError still
    means "show the visitor a 4xx" by the time api.py sees it. The other jobs
    are left to finish before it surfaces: their requests are already sent by
    then, and cancelling would drop answers Steam is going to send anyway.

    Only ever called with jobs that touch nothing shared. The module state they
    read - STEAM_ID, PROFILE_URL - is written once by set_user() under api.py's
    one lock and never changes under a build; the two mutable things a job here
    can reach, _calls and _level_cache, have locks of their own.

    What does NOT belong in here is two requests to steamcommunity.com. That
    host answers a burst with a 429 that then lasts minutes, which is the whole
    reason community.py exists; scrapes stay a sequence and the sequence is what
    gets handed over as one job."""
    if len(jobs) <= 1 or FANOUT <= 1:
        return {name: job() for name, job in jobs.items()}
    with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(FANOUT, len(jobs)),
            thread_name_prefix="fetch") as pool:
        futures = {name: pool.submit(job) for name, job in jobs.items()}
        # Deliberately not as_completed(): see above on which failure wins.
        return {name: f.result() for name, f in futures.items()}


def get_json(path, envelope="response", required=True, with_steamid=True, **params):
    """Steam wraps payloads in different top-level keys depending on the interface:
    'response' for IPlayerService, 'playerstats' for stats, 'result' for the Dota ones."""
    params["key"] = KEY
    if with_steamid:
        params["steamid"] = STEAM_ID
    url = f"{API}/{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    # Two tries, not four: a visitor is waiting on this, not a nightly job.
    for attempt in range(2):
        spend()
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.load(r).get(envelope, {})
        except urllib.error.HTTPError as e:
            # 403 here means the profile's game details are private, which is a
            # normal answer rather than a failure.
            if e.code in (400, 403):
                if required:
                    raise SteamError("@err.private")
                return {}
            if attempt or not required:
                if required:
                    raise SteamError("@err.refused")
                return {}
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            if attempt:
                if required:
                    raise SteamError("@err.no_answer")
                print(f"  warn: {path} unavailable -> {e}", file=sys.stderr)
                return {}
        time.sleep(1)
    return {}


def get_text(url):
    """Best-effort fetch. Returns '' rather than failing the whole build.

    Every caller of this is a scrape of steamcommunity.com, which is the same
    host cards.py and inv.py read, so it goes through community.py's budget.
    Not through its queue, though: this runs inside a build somebody is watching
    a spinner for, and three scrapes behind an eight-second interval would add
    twenty-four seconds to every cold profile. So it claims the slot rather than
    waiting for one, and the crawl behind it goes quiet instead - which is the
    right place for that cost to land.

    Two other things it owes the shared budget. It skips entirely while the host
    is refusing anyone, because asking during a refusal is what renews it; a
    build already degrades on an empty scrape, so that path is the one the code
    was written for. And it reports a 429 rather than swallowing it: before
    this, a rate-limited scrape returned '' with a warning and cards.py went on
    asking into a host that had already said no.

    Still urllib rather than community.fetch: these ask for HTML and XML rather
    than JSON, and they answer 200 with the Accept-Encoding urllib forces, so
    the bot check measured on /market and /inventory does not apply here. When
    there is a measurement saying otherwise, they move too."""
    cool = community.cooling()
    if cool > 0:
        print(f"  warn: {url} -> skipped, community cooling for {cool:.0f}s",
              file=sys.stderr)
        return ""
    community.claim()
    spend()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        if e.code == 429:
            community.note_429()
        print(f"  warn: {url} -> {e}", file=sys.stderr)
        return ""
    except Exception as e:
        print(f"  warn: {url} -> {e}", file=sys.stderr)
        return ""


def get_json_url(url, label, timeout=30):
    """Plain GET returning parsed JSON, or None. Used for endpoints outside the
    key-authenticated Steam API; never fatal, the section just goes missing."""
    spend()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except Exception as e:
        print(f"  warn: {label} unavailable -> {e}", file=sys.stderr)
        return None


def strip_tags(s):
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    return html.unescape(s).strip()


def scrape_profile():
    """Achievement totals, counts and the showcases only exist in the HTML."""
    out = {}
    page = get_text(PROFILE_URL)
    if not page:
        return out

    counts = dict(
        re.findall(
            r'<span class="count_link_label">\s*(.*?)\s*</span>\s*&nbsp;\s*'
            r'<span class="profile_count_link_total">\s*(.*?)\s*</span>',
            page,
            re.S,
        )
    )
    for label, key in (
        ("Badges", "badges"),
        ("Games", "games_listed"),
        ("Screenshots", "screenshots"),
        ("Videos", "videos"),
        ("Reviews", "reviews"),
        ("Artwork", "artwork"),
        ("Friends", "friends"),
    ):
        raw = counts.get(label, "").replace(",", "").strip()
        if raw.isdigit():
            out[key] = int(raw)

    # Achievement showcase renders exactly three .value divs: total, perfect, avg.
    vals = [strip_tags(v) for v in re.findall(r'class="value">(.*?)</div>', page, re.S)]
    if len(vals) >= 3:
        total, perfect, avg = vals[0], vals[1], vals[2]
        if total.replace(",", "").isdigit():
            out["achievements_total"] = int(total.replace(",", ""))
        if perfect.replace(",", "").isdigit():
            out["perfect_games"] = int(perfect.replace(",", ""))
        if avg.endswith("%"):
            out["avg_completion"] = avg

    # Whatever the profile put in its text showcase. On the owner's profile that
    # happens to be a spec dump; on anyone else's it is whatever they wrote, so
    # this is parsed generically and simply goes missing when there is none.
    m = re.search(
        r'<div class="showcase_slot[^"]*">\s*<div class="showcase_content_bg">(.*?)</div>',
        page, re.S,
    )
    if not m:
        m = re.search(r'I use Arch BTW.*?(?=Rarest|<div class="profile_customization")', page, re.S)
    if m:
        lines = [l.strip() for l in strip_tags(m.group(1 if m.re.groups else 0)).splitlines() if l.strip()]
        if lines:
            out["showcase"] = lines[:8]
    return out


# What comes out of the profile XML, as `tag: key`. All of it arrives in the one
# request scrape_xml already makes, and everything past the first three was
# being thrown away.
#
# What is NOT in here is groups. Measured on 2026-09-02: this document has no
# <groups> block at all, on a profile that is in seventeen of them. Steam
# publishes group names only on the group's own memberslistxml, which would be
# one request per group on the host cards.py is already pacing, so the profile
# gets the count from the Web API and says plainly that the names are not
# published anywhere cheap.
XML_FIELDS = (
    ("summary", "bio"),
    ("steamID", "persona"),
    ("memberSince", "member_since"),
    ("realname", "realname"),
    ("location", "location"),
    ("customURL", "custom_url"),
    ("privacyState", "privacy"),
    ("onlineState", "online"),
    # "In-Game<br/>Counter-Strike 2" - strip_tags turns the break into a
    # newline, and the page prints the first line.
    ("stateMessage", "status"),
    ("steamRating", "rating"),
)


def parse_xml(doc):
    """The profile XML, as a dict. Pure so it can be tested without a network.

    Empty values are dropped rather than kept: Steam sends <location></location>
    on a profile that never set one, and an empty string in the payload is a
    field the page would draw as a blank line instead of leaving out."""
    out = {}
    for tag, key in XML_FIELDS:
        m = re.search(rf"<{tag}>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</{tag}>", doc, re.S)
        if m:
            value = strip_tags(m.group(1))
            if value:
                out[key] = value
    # Two flags rather than strings, because they are yes-or-no facts and the
    # page should not be parsing "0" and "1" itself. Absent stays absent: a
    # document that did not say is not a document that said no.
    for tag, key in (("isLimitedAccount", "limited"), ("vacBanned", "vac_xml")):
        m = re.search(rf"<{tag}>(.*?)</{tag}>", doc, re.S)
        if m and m.group(1).strip() in ("0", "1"):
            out[key] = m.group(1).strip() == "1"
    return out


def scrape_xml():
    """Public profile XML: the bio, the persona name Steam actually shows, and
    the handful of facts that live nowhere else - where somebody says they are,
    whether the account is limited, what it is doing right now."""
    return parse_xml(get_text(f"{PROFILE_URL}?xml=1"))


# How many badges the panel prints. Steam serves the badge page 150 rows at a
# time, newest first, so one request covers every account this will ever show
# and the ones past it are a number rather than a tile.
BADGE_TILES = 24

MONTHS = {name: n + 1 for n, name in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"])}


def badge_date(text):
    """The day off an "Unlocked 22 Mar, 2023 @ 7:14pm" line.

    Steam drops the year when the badge was earned this year, which is why the
    year group is optional and defaults to this one - it is the only case where
    it is missing. The clock time is thrown away: this page prints days."""
    m = re.search(r"(\d{1,2})\s+([A-Z][a-z]{2})(?:,\s*(\d{4}))?", text or "")
    if not m or m.group(2) not in MONTHS:
        return None
    try:
        return date(int(m.group(3)) if m.group(3) else date.today().year,
                    MONTHS[m.group(2)], int(m.group(1))).isoformat()
    except ValueError:
        return None


def scrape_badges():
    """The badges themselves, which no Steam API hands over whole.

    `GetBadges` knows a badge happened - an appid, a level, a timestamp - and
    nothing about what it looks like or what it is called. The names and the
    artwork only exist on the badge page, and the Steam-issued ones (Years of
    Service, the Replay badges, the collector tiers) have no appid at all, so
    for those the page is the only source there is.

    One request, asked in English so the date and the count parse the same way
    for every reader. Everything printed off it is a fact about the badge, not
    prose, so the language of the request never reaches the page.

    Best-effort, like every other scrape here: a private profile answers with a
    page that has no badge rows in it, and the panel simply does not get drawn."""
    out = {"list": [], "total": None}
    page = get_text(f"{PROFILE_URL}badges/?l=english")
    if not page:
        return out

    # "Showing 1-150 of 6,506 badges" - present only once the list paginates,
    # so a short list falls back to counting what came back, which is all of it.
    m = re.search(r"of\s+([\d,]+)\s+badges", page)
    if m:
        out["total"] = int(m.group(1).replace(",", ""))

    for chunk in re.split(r'class="badge_row\b', page)[1:]:
        # A card badge links to the set it was crafted from, which is the one
        # thing on the row that names the game as an appid rather than as text.
        href = re.search(r'badge_row_overlay"\s+href="([^"]+)"', chunk)
        appid = re.search(r"/gamecards/(\d+)", href.group(1) if href else "")
        title = re.search(r'class="badge_title">(.*?)</div>', chunk, re.S)
        # The heading carries a "View details" affordance inside it. That is
        # chrome, not part of the name.
        title = strip_tags(re.sub(r'<span class="badge_view_details".*', "",
                                  title.group(1), flags=re.S)) if title else ""
        # Steam serves the artwork lazily: the src is a spacer gif and the real
        # URL waits in an attribute until the row scrolls into view.
        icon = (re.search(r'data-delayed-image="([^"]+)"', chunk)
                or re.search(r'class="badge_icon"[^>]*src="([^"]+)"', chunk))
        # For a card badge this is the tier's own name - "Explosion", not the
        # game. For a Steam-issued one it repeats the heading.
        tier = re.search(r'class="badge_info_title">(.*?)</div>', chunk, re.S)
        tier = strip_tags(tier.group(1)) if tier else None
        level = re.search(r"Level\s+([\d,]+)\s*,", chunk)
        xp = re.search(r"([\d,]+)\s+XP", chunk)
        when = re.search(r'class="badge_info_unlocked">(.*?)</div>', chunk, re.S)
        name = title or tier
        if not name:
            continue
        out["list"].append({
            "name": name,
            # Only when it says something the heading does not.
            "tier": tier if tier and tier != name else None,
            "icon": icon.group(1) if icon else None,
            "appid": int(appid.group(1)) if appid else None,
            "level": int(level.group(1).replace(",", "")) if level else None,
            "xp": int(xp.group(1).replace(",", "")) if xp else None,
            "when": badge_date(strip_tags(when.group(1))) if when else None,
        })

    if out["total"] is None:
        out["total"] = len(out["list"])
    out["list"] = out["list"][:BADGE_TILES]
    return out


WEAPONS = {
    "ak47": "AK-47", "awp": "AWP", "m4a1": "M4A4 / M4A1-S", "hkp2000": "P2000",
    "deagle": "Desert Eagle", "glock": "Glock-18", "taser": "Zeus x27", "aug": "AUG",
    "mp7": "MP7", "p90": "P90", "sg556": "SG 553", "galilar": "Galil AR",
    "mac10": "MAC-10", "ump45": "UMP-45", "famas": "FAMAS", "p250": "P250",
    "elite": "Dual Berettas", "fiveseven": "Five-SeveN", "tec9": "Tec-9",
    "nova": "Nova", "xm1014": "XM1014", "sawedoff": "Sawed-Off", "mag7": "MAG-7",
    "bizon": "PP-Bizon", "mp9": "MP9", "cz75a": "CZ75-Auto", "revolver": "R8 Revolver",
    "negev": "Negev", "m249": "M249", "g3sg1": "G3SG1", "scar20": "SCAR-20",
    "ssg08": "SSG 08", "knife": "@cs.knife", "hegrenade": "@cs.he", "molotov": "Molotov",
}

MAPS = {
    "de_dust2": "Dust II", "de_inferno": "Inferno", "de_train": "Train",
    "de_nuke": "Nuke", "de_vertigo": "Vertigo", "de_cbble": "Cobblestone",
    "de_lake": "Lake", "de_safehouse": "Safehouse", "de_stmarc": "St. Marc",
    "de_sugarcane": "Sugarcane", "de_bank": "Bank", "de_aztec": "Aztec",
    "de_dust": "Dust", "cs_office": "Office", "cs_assault": "Assault",
    "cs_italy": "Italy", "cs_militia": "Militia", "ar_shoots": "Shoots",
    "ar_baggage": "Baggage", "ar_monastery": "Monastery",
}

# rank_tier is medal * 10 + stars.
DOTA_MEDALS = ["", "Herald", "Guardian", "Crusader", "Archon", "Legend", "Ancient", "Divine", "Immortal"]


# The last-match block reports its favourite weapon as an item definition index
# rather than a name. These are the stable CS defindexes; anything outside the
# table stays unnamed instead of being guessed at.
CS_DEFINDEX = {
    1: "deagle", 2: "elite", 3: "fiveseven", 4: "glock", 7: "ak47", 8: "aug",
    9: "awp", 10: "famas", 11: "g3sg1", 13: "galilar", 14: "m249", 16: "m4a1",
    17: "mac10", 19: "p90", 23: "mp5sd", 24: "ump45", 25: "xm1014", 26: "bizon",
    27: "mag7", 28: "negev", 29: "sawedoff", 30: "tec9", 31: "taser",
    32: "hkp2000", 33: "mp7", 34: "mp9", 35: "nova", 36: "p250", 38: "scar20",
    39: "sg556", 40: "ssg08", 60: "m4a1", 61: "hkp2000", 63: "cz75a", 64: "revolver",
}


def fetch_cs2(record_hours):
    """CS2 keeps a 215-field stat block on Steam itself - no third party needed."""
    raw = get_json("ISteamUserStats/GetUserStatsForGame/v2/", envelope="playerstats",
                   required=False, appid=CS2_APPID)
    stats = {s["name"]: s["value"] for s in (raw or {}).get("stats", [])}
    if not stats:
        print("  warn: no CS2 stats (profile may hide game details)", file=sys.stderr)
        return None

    g = lambda k: stats.get(k, 0)
    kills, deaths, shots, hits = g("total_kills"), g("total_deaths"), g("total_shots_fired"), g("total_shots_hit")
    rounds, seconds = g("total_rounds_played"), g("total_time_played")

    weapons = []
    for key, label in WEAPONS.items():
        k = g(f"total_kills_{key}")
        if not k:
            continue
        ws, wh = g(f"total_shots_{key}"), g(f"total_hits_{key}")
        # Some weapons (the Zeus, grenades, the knife) log kills but no hit counter.
        # Reporting that as 0% accuracy would be a lie, so leave it blank.
        weapons.append({
            "name": label,
            "kills": k,
            "accuracy": round(wh / ws * 100, 1) if ws and wh else None,
        })
    weapons.sort(key=lambda w: -w["kills"])

    maps = []
    for key, label in MAPS.items():
        played = g(f"total_rounds_map_{key}")
        won = g(f"total_wins_map_{key}")
        if played < 50:
            continue
        maps.append({
            "name": label,
            "rounds": played,
            "won": won,
            "win_rate": round(won / played * 100, 1),
        })
    maps.sort(key=lambda m: -m["rounds"])

    # The rank itself is the one thing this block cannot hold. CS Rating, the
    # Premier number and the competitive skill group all live on the game
    # coordinator, which only the game client talks to - Valve publishes none of
    # them through the Web API. What Steam does keep is the ladder underneath:
    # how many competitive matches were won, how many games earned XP, and the
    # contribution score the scoreboard is actually sorted by. The page prints
    # those and says plainly that the patente is not on offer.
    matches_played = g("total_matches_played")
    matches_won = g("total_matches_won")
    gg_played = g("total_gg_matches_played")
    rank = {
        "competitive_wins": g("steam_stat_matchwinscomp"),
        "xp_games": g("steam_stat_xpearnedgames"),
        "matches": matches_played,
        "matches_won": matches_won,
        "match_win_rate": round(matches_won / matches_played * 100, 1) if matches_played else None,
        "contribution_score": g("total_contribution_score"),
        "score_per_round": round(g("total_contribution_score") / rounds, 1) if rounds else None,
        "gg_played": gg_played,
        "gg_won": g("total_gg_matches_won"),
        "gg_score": g("total_gun_game_contribution_score"),
        "progressive_wins": g("total_progressive_matches_won"),
    }

    # The last match Steam saw, field for field. It is the only per-match record
    # in the whole block: everything else is a running total since 2012.
    fav_key = CS_DEFINDEX.get(g("last_match_favweapon_id"))
    fav_shots, fav_hits = g("last_match_favweapon_shots"), g("last_match_favweapon_hits")
    last = {
        "rounds": g("last_match_rounds"),
        "wins": g("last_match_wins"),
        "t_wins": g("last_match_t_wins"),
        "ct_wins": g("last_match_ct_wins"),
        "kills": g("last_match_kills"),
        "deaths": g("last_match_deaths"),
        "kd": round(g("last_match_kills") / g("last_match_deaths"), 2) if g("last_match_deaths") else None,
        "mvps": g("last_match_mvps"),
        "damage": g("last_match_damage"),
        "score": g("last_match_contribution_score"),
        "money_spent": g("last_match_money_spent"),
        "dominations": g("last_match_dominations"),
        "revenges": g("last_match_revenges"),
        "players": g("last_match_max_players"),
        "weapon": WEAPONS.get(fav_key) if fav_key else None,
        "weapon_kills": g("last_match_favweapon_kills"),
        "weapon_accuracy": round(fav_hits / fav_shots * 100, 1) if fav_shots and fav_hits else None,
    } if g("last_match_rounds") else None

    return {
        "appid": CS2_APPID,
        "record_hours": record_hours,
        "in_match_hours": round(seconds / 3600),
        "kills": kills,
        "deaths": deaths,
        "kd": round(kills / deaths, 3) if deaths else None,
        "headshot_rate": round(g("total_kills_headshot") / kills * 100, 1) if kills else None,
        "accuracy": round(hits / shots * 100, 1) if shots else None,
        "shots_fired": shots,
        "shots_hit": hits,
        "rounds": rounds,
        "round_wins": g("total_wins"),
        "round_win_rate": round(g("total_wins") / rounds * 100, 1) if rounds else None,
        "matches": g("total_matches_played"),
        "match_wins": g("total_matches_won"),
        "mvps": g("total_mvps"),
        "damage": g("total_damage_done"),
        "damage_per_round": round(g("total_damage_done") / rounds) if rounds else None,
        "money": g("total_money_earned"),
        "bombs_planted": g("total_planted_bombs"),
        "bombs_defused": g("total_defused_bombs"),
        "pistol_round_wins": g("total_wins_pistolround"),
        "taser_kills": g("total_kills_taser"),
        "knife_kills": g("total_kills_knife"),
        "weapons": weapons[:14],
        "maps": maps[:10],
        "rank": rank,
        "last_match": last,
    }


def fetch_dota(record_hours):
    """Valve's GetMatchDetails has been returning 500 for a long time, so the
    per-match aggregates come from OpenDota. Hero names still come from Valve."""
    wl = get_json_url(f"{OPENDOTA}/players/{ACCOUNT_ID}/wl", "opendota win/loss")
    if not wl or not (wl.get("win", 0) + wl.get("lose", 0)):
        return None

    totals_raw = get_json_url(f"{OPENDOTA}/players/{ACCOUNT_ID}/totals", "opendota totals") or []
    totals = {t["field"]: t for t in totals_raw}
    heroes_raw = get_json_url(f"{OPENDOTA}/players/{ACCOUNT_ID}/heroes", "opendota heroes") or []
    counts = get_json_url(f"{OPENDOTA}/players/{ACCOUNT_ID}/counts", "opendota counts") or {}
    profile = get_json_url(f"{OPENDOTA}/players/{ACCOUNT_ID}", "opendota profile") or {}

    names, slugs = {}, {}
    hero_list = get_json("IEconDOTA2_570/GetHeroes/v1/", envelope="result",
                         required=False, language="en") or {}
    for h in hero_list.get("heroes", []):
        names[h["id"]] = h["localized_name"]
        slugs[h["id"]] = h["name"].replace("npc_dota_hero_", "")

    wins, losses = wl["win"], wl["lose"]
    matches = wins + losses

    def avg(field):
        t = totals.get(field)
        return round(t["sum"] / t["n"], 1) if t and t["n"] else None

    played = [h for h in heroes_raw if h.get("games")]
    heroes = []
    for i, h in enumerate(played[:12]):
        hid = int(h["hero_id"])
        slug = slugs.get(hid)
        heroes.append({
            "name": names.get(hid, f"herói {hid}"),
            "games": h["games"],
            "wins": h["win"],
            "losses": h["games"] - h["win"],
            "win_rate": round(h["win"] / h["games"] * 100, 1),
            "face": f"{DOTA_ART}/icons/{slug}.png" if slug else None,
            # Only the signature hero earns the full card; it is the page banner.
            "art": f"{DOTA_ART}/{slug}.png" if slug and i == 0 else None,
        })

    # Radiant/Dire is the one split Dota players actually argue about.
    sides = {}
    for key, label in (("1", "radiant"), ("0", "dire")):
        c = counts.get("is_radiant", {}).get(key)
        if c and c.get("games"):
            sides[label] = {
                "games": c["games"],
                "wins": c["win"],
                "win_rate": round(c["win"] / c["games"] * 100, 1),
            }

    # The rank, as Dota itself keeps it: one number, medal * 10 + stars. Immortal
    # has no stars, it has a leaderboard position instead, so the star count is
    # only meaningful below tier 8. MMR is never published by Valve for anybody -
    # what comes back here is OpenDota's estimate from the public match record,
    # and the page has to say so rather than print it as if it were the number.
    tier = profile.get("rank_tier")
    medal = medal_name = None
    stars = 0
    if tier:
        medal_name = DOTA_MEDALS[tier // 10] if tier // 10 < len(DOTA_MEDALS) else None
        stars = tier % 10
        medal = f"{medal_name} {stars}".strip() if medal_name else None

    def mmr(key):
        v = profile.get(key)
        return round(v) if isinstance(v, (int, float)) else None

    # Older accounts carry the legacy shape instead of the computed one.
    legacy = profile.get("mmr_estimate") or {}
    rank = {
        "tier": tier,
        "medal": medal,
        "medal_name": medal_name,
        "stars": stars,
        "immortal": bool(tier) and tier // 10 >= 8,
        "leaderboard_rank": profile.get("leaderboard_rank"),
        "mmr": mmr("computed_mmr") or (round(legacy["estimate"]) if legacy.get("estimate") else None),
        "mmr_turbo": mmr("computed_mmr_turbo"),
    }

    dur = totals.get("duration")
    in_match_hours = round(dur["sum"] / 3600) if dur else None

    return {
        "appid": DOTA_APPID,
        "record_hours": record_hours,
        "in_match_hours": in_match_hours,
        "matches": matches,
        "wins": wins,
        "losses": losses,
        "win_rate": round(wins / matches * 100, 1),
        "medal": medal,
        "rank": rank,
        "kills": int(totals["kills"]["sum"]) if "kills" in totals else None,
        "deaths": int(totals["deaths"]["sum"]) if "deaths" in totals else None,
        "assists": int(totals["assists"]["sum"]) if "assists" in totals else None,
        "kda": (
            round((totals["kills"]["sum"] + totals["assists"]["sum"]) / totals["deaths"]["sum"], 2)
            if totals.get("deaths", {}).get("sum") else None
        ),
        "avg_gpm": avg("gold_per_min"),
        "avg_xpm": avg("xp_per_min"),
        "avg_last_hits": avg("last_hits"),
        "avg_denies": avg("denies"),
        "avg_duration_min": round(dur["sum"] / dur["n"] / 60, 1) if dur and dur["n"] else None,
        "heroes_played": len(played),
        "heroes_total": len(names) or None,
        "heroes": heroes,
        "sides": sides,
    }


# ── Per-game layouts ─────────────────────────────────────────────────
# Keyed by appid, because the visitor's library decides which of these turn up.
# `kind` picks the fetcher below; `theme` picks the layout in game.js. A game
# outside this table still gets a page - the generic one - so any library works.
#
# `bare` means Steam exposes nothing at all for that game (both Arma 2s and
# Valheim have no achievements; DayZ has thirteen and they are almost never
# unlocked). Those pages are built around the absence instead of hiding it.
GAME_LAYOUTS = {
    570:     {"kind": "dota",       "theme": "dota-2"},
    730:     {"kind": "cs2",        "theme": "counter-strike-2"},
    107410:  {"kind": "arma3",      "theme": "arma-3"},
    236390:  {"kind": "plain",      "theme": "war-thunder"},
    1938090: {"kind": "plain",      "theme": "call-of-duty"},
    489830:  {"kind": "plain",      "theme": "skyrim"},
    1250410: {"kind": "plain",      "theme": "msfs"},
    271590:  {"kind": "plain",      "theme": "gta-v"},
    227300:  {"kind": "plain",      "theme": "ets2"},
    33930:   {"kind": "bare",       "theme": "arma-2-oa"},
    892970:  {"kind": "bare",       "theme": "valheim"},
    12210:   {"kind": "plain",      "theme": "gta-iv"},
    33910:   {"kind": "bare",       "theme": "arma-2"},
    275850:  {"kind": "plain",      "theme": "no-mans-sky"},
    1172470: {"kind": "plain",      "theme": "apex"},
    1144200: {"kind": "readyornot", "theme": "ready-or-not"},
    255710:  {"kind": "plain",      "theme": "cities-skylines"},
    218620:  {"kind": "payday2",    "theme": "payday-2"},
    1547000: {"kind": "plain",      "theme": "gta-sa"},
    813820:  {"kind": "plain",      "theme": "realm-royale"},
    221100:  {"kind": "plain",      "theme": "dayz"},
    4000:    {"kind": "gmod",       "theme": "garrys-mod"},
    270880:  {"kind": "ats",        "theme": "ats"},
    286570:  {"kind": "plain",      "theme": "f1-2015"},
    # The twenty most played games on Steam that had no page of their own. Five
    # of them expose a real stat block and get an extractor below; the rest are
    # built out of achievements and hours, which every profile has.
    1623730: {"kind": "plain",      "theme": "palworld"},
    578080:  {"kind": "plain",      "theme": "pubg"},
    238960:  {"kind": "poe",        "theme": "poe"},
    252490:  {"kind": "rust",       "theme": "rust"},
    250900:  {"kind": "plain",      "theme": "isaac"},
    2767030: {"kind": "plain",      "theme": "marvel-rivals"},
    381210:  {"kind": "dbd",        "theme": "dbd"},
    413150:  {"kind": "stardew",    "theme": "stardew"},
    2807960: {"kind": "plain",      "theme": "bf6"},
    230410:  {"kind": "plain",      "theme": "warframe"},
    # Slay the Spire 2 shipped with no achievements and one version counter, and
    # VRChat exposes nothing at all. Both are written for having only the clock.
    2868840: {"kind": "bare",       "theme": "sts2"},
    1086940: {"kind": "plain",      "theme": "bg3"},
    2507950: {"kind": "plain",      "theme": "delta-force"},
    359550:  {"kind": "plain",      "theme": "r6"},
    322170:  {"kind": "plain",      "theme": "geometry-dash"},
    1091500: {"kind": "plain",      "theme": "cyberpunk"},
    440:     {"kind": "tf2",        "theme": "tf2"},
    2357570: {"kind": "plain",      "theme": "overwatch"},
    438100:  {"kind": "bare",       "theme": "vrchat"},

    # ── The rest of the owner's hundred ──────────────────────────────────
    # Ranks 25–100. What each of these could be built out of was settled by
    # asking GetSchemaForGame and GetUserStatsForGame first, one call per appid,
    # which is why fifteen of them get an extractor and the rest do not: the
    # answer for most of a hundred-game library really is "hours and nothing".
    #
    # Fifteen with a stat block of their own.
    363970:  {"kind": "clicker",    "theme": "clicker-heroes"},
    555570:  {"kind": "infest",     "theme": "infestation"},
    304930:  {"kind": "unturned",   "theme": "unturned"},
    222880:  {"kind": "insurgency", "theme": "insurgency"},
    239220:  {"kind": "mquest",     "theme": "mighty-quest"},
    1874880: {"kind": "reforger",   "theme": "reforger"},
    581320:  {"kind": "sandstorm",  "theme": "sandstorm"},
    2977660: {"kind": "cats",       "theme": "cats"},
    50300:   {"kind": "specops",    "theme": "spec-ops"},
    346010:  {"kind": "besiege",    "theme": "besiege"},
    675690:  {"kind": "tribal",     "theme": "tribal-wars"},
    242760:  {"kind": "forest",     "theme": "the-forest"},
    3478870: {"kind": "geoguessr",  "theme": "geoguessr"},
    339280:  {"kind": "strife",     "theme": "strife"},
    397900:  {"kind": "biztour",    "theme": "business-tour"},
    # Red Dead's stats are named AchievementStat_1…43 and Dying Light's
    # ACH_10_PROGRESS…ACH_53_PROGRESS. Both are achievement progress, most of
    # them frozen at the threshold that unlocked the achievement, so neither
    # page presents them as totals - they are built on the achievements those
    # counters were only ever tracking.
    1174180: {"kind": "plain",      "theme": "rdr2"},
    239140:  {"kind": "plain",      "theme": "dying-light"},
    # Eighteen built on their achievements, which is all they expose.
    2923300: {"kind": "plain",      "theme": "banana"},
    1172620: {"kind": "plain",      "theme": "sea-of-thieves"},
    739630:  {"kind": "plain",      "theme": "phasmophobia"},
    63380:   {"kind": "plain",      "theme": "sniper-elite-v2"},
    3017120: {"kind": "plain",      "theme": "egg-surprise"},
    346900:  {"kind": "plain",      "theme": "adventure-capitalist"},
    997010:  {"kind": "plain",      "theme": "police-sim"},
    1318690: {"kind": "plain",      "theme": "shapez"},
    1794680: {"kind": "plain",      "theme": "vampire-survivors"},
    444090:  {"kind": "plain",      "theme": "paladins"},
    291550:  {"kind": "plain",      "theme": "brawlhalla"},
    834530:  {"kind": "plain",      "theme": "yakuza-kiwami"},
    552500:  {"kind": "plain",      "theme": "vermintide-2"},
    1097150: {"kind": "plain",      "theme": "fall-guys"},
    638970:  {"kind": "plain",      "theme": "yakuza-0"},
    238320:  {"kind": "plain",      "theme": "outlast"},
    2988580: {"kind": "plain",      "theme": "yakuza-0-dc"},
    1891700: {"kind": "plain",      "theme": "tap-ninja"},
    # Fourteen that publish an achievement set and never opened it. They are
    # "plain" rather than "bare" on purpose: the size of the set is the one real
    # number these pages have, and "none of the 83" says more than "nothing".
    304050:  {"kind": "plain",      "theme": "trove"},
    284160:  {"kind": "plain",      "theme": "beamng"},
    2162800: {"kind": "plain",      "theme": "shapez-2"},
    233250:  {"kind": "plain",      "theme": "planetary-annihilation"},
    398680:  {"kind": "plain",      "theme": "ace-of-words"},
    357280:  {"kind": "plain",      "theme": "alter-world"},
    378100:  {"kind": "plain",      "theme": "nyctophobia"},
    405950:  {"kind": "plain",      "theme": "lowglow"},
    341090:  {"kind": "plain",      "theme": "on-a-roll-3d"},
    357900:  {"kind": "plain",      "theme": "make-it-indie"},
    433210:  {"kind": "plain",      "theme": "rhinos-rage"},
    299460:  {"kind": "plain",      "theme": "woodle-tree"},
    113400:  {"kind": "plain",      "theme": "apb"},
    1812620: {"kind": "plain",      "theme": "dsx"},
    # Twenty-five that expose nothing whatsoever - no stat block, no achievement
    # set at all. Asking Steam for either just spends a call to be told no.
    17390:   {"kind": "bare",       "theme": "spore"},
    243870:  {"kind": "bare",       "theme": "ghost-recon-phantoms"},
    109600:  {"kind": "bare",       "theme": "neverwinter"},
    108600:  {"kind": "bare",       "theme": "project-zomboid"},
    47410:   {"kind": "bare",       "theme": "stronghold-kingdoms"},
    12100:   {"kind": "bare",       "theme": "gta-iii"},
    306130:  {"kind": "bare",       "theme": "eso"},
    24960:   {"kind": "bare",       "theme": "bfbc2"},
    1422450: {"kind": "bare",       "theme": "deadlock"},
    7510:    {"kind": "bare",       "theme": "x-blades"},
    755790:  {"kind": "bare",       "theme": "ring-of-elysium"},
    277950:  {"kind": "bare",       "theme": "deadbreed"},
    65790:   {"kind": "bare",       "theme": "arma-cwa"},
    428430:  {"kind": "bare",       "theme": "endorlight"},
    339470:  {"kind": "bare",       "theme": "retention"},
    20510:   {"kind": "bare",       "theme": "stalker-cs"},
    # ── The Zone, one page per game ──────────────────────────────────────
    # Asked for by name, and the answer Steam gave is the reason each of these
    # looks nothing like the next. The three originals publish absolutely
    # nothing - no stat block, no achievement set, not even an empty one -
    # because they predate Steam having either. The three Enhanced Editions
    # publish an achievement set each and a "stat block" that is nothing but
    # progress towards it (`stat_zone_safari`, `stat_stalkerstrike`), so their
    # pages are built on the achievements and say so. Only the sequel has
    # counters worth printing, and only some of them.
    41700:   {"kind": "bare",       "theme": "stalker-cop"},
    1643320: {"kind": "stalker2",   "theme": "stalker-2"},
    2427410: {"kind": "plain",      "theme": "stalker-soc-ee"},
    2427420: {"kind": "plain",      "theme": "stalker-cs-ee"},
    2427430: {"kind": "plain",      "theme": "stalker-cop-ee"},
    3241660: {"kind": "bare",       "theme": "repo"},
    # ── Tamriel, one page per game ───────────────────────────────────────
    # Thirty-one years of releases, and Steam knows three completely different
    # things about them. The four from the nineties, Morrowind, the original
    # Oblivion and ESO publish nothing at all - the first six because Steam did
    # not exist or had no achievements yet, ESO because everything it counts it
    # counts on ZeniMax's own servers. Only the last three publish a set, and
    # two of those are the same seventy-five achievements as the third.
    1812290: {"kind": "bare",       "theme": "tes-arena"},
    1812390: {"kind": "bare",       "theme": "tes-daggerfall"},
    1812410: {"kind": "bare",       "theme": "tes-redguard"},
    1812420: {"kind": "bare",       "theme": "tes-battlespire"},
    22330:   {"kind": "bare",       "theme": "oblivion"},
    2623190: {"kind": "plain",      "theme": "oblivion-remastered"},
    72850:   {"kind": "plain",      "theme": "skyrim-2011"},
    611670:  {"kind": "plain",      "theme": "skyrim-vr"},
    # ── Black Mesa and after ─────────────────────────────────────────────
    # Eleven games, and seven of them publish nothing: Half-Life, both
    # expansions, the Source ports and the two deathmatch modes all predate
    # Steam achievements or never had any. Half-Life 2 and the episodes have a
    # set each; Alyx has 440 counters, and they are about how you *moved*,
    # which nothing else on this site can say.
    70:      {"kind": "bare",       "theme": "half-life"},
    50:      {"kind": "bare",       "theme": "hl-opfor"},
    130:     {"kind": "bare",       "theme": "hl-blueshift"},
    280:     {"kind": "bare",       "theme": "hl-source"},
    340:     {"kind": "bare",       "theme": "hl-lostcoast"},
    320:     {"kind": "bare",       "theme": "hl2-dm"},
    360:     {"kind": "bare",       "theme": "hldm-source"},
    220:     {"kind": "plain",      "theme": "half-life-2"},
    380:     {"kind": "plain",      "theme": "hl2-ep1"},
    420:     {"kind": "plain",      "theme": "hl2-ep2"},
    546560:  {"kind": "alyx",       "theme": "alyx"},
    # ── The rest of Liberty City, Vice City and Los Santos ───────────────
    12110:   {"kind": "bare",       "theme": "gta-vc"},
    12120:   {"kind": "bare",       "theme": "gta-sa-classic"},
    12220:   {"kind": "bare",       "theme": "gta-eflc"},
    1546970: {"kind": "plain",      "theme": "gta-iii-de"},
    1546990: {"kind": "plain",      "theme": "gta-vc-de"},
    3240220: {"kind": "plain",      "theme": "gta-v-enhanced"},
    # ── The other Counter-Strikes ────────────────────────────────────────
    10:      {"kind": "bare",       "theme": "cs-16"},
    80:      {"kind": "bare",       "theme": "cs-cz"},
    100:     {"kind": "bare",       "theme": "cs-cz-ds"},
    240:     {"kind": "css",        "theme": "cs-source"},
    273110:  {"kind": "plain",      "theme": "cs-nexon"},
    # ── Left 4 Dead ──────────────────────────────────────────────────────
    500:     {"kind": "l4d1",       "theme": "l4d1"},
    550:     {"kind": "l4d2",       "theme": "l4d2"},
    # ── The two Armas that were missing ──────────────────────────────────
    33900:   {"kind": "bare",       "theme": "arma-2-alt"},
    224860:  {"kind": "plain",      "theme": "arma-tactics"},
    292410:  {"kind": "bare",       "theme": "street-racing-syndicate"},
    1046930: {"kind": "bare",       "theme": "dota-underlords"},
    667970:  {"kind": "bare",       "theme": "vtol-vr"},
    1930:    {"kind": "bare",       "theme": "two-worlds"},
    4500:    {"kind": "bare",       "theme": "stalker-soc"},
    22320:   {"kind": "bare",       "theme": "morrowind"},
    963930:  {"kind": "bare",       "theme": "contractors-vr"},
    232010:  {"kind": "bare",       "theme": "ets1"},
    65780:   {"kind": "bare",       "theme": "arma-gold"},
}

# Names the search can use before the asynchronous storefront catalogue has
# learned them.  Most themed pages already have a populated metadata row; keep
# explicit fallbacks only for observed gaps rather than maintaining a second
# copy of every Steam title here.
GAME_SEARCH_NAMES = {
    3241660: "R.E.P.O.",
    3405690: "EA SPORTS FC™ 26",
}

# Store metadata is normally the canonical public title. Valve currently
# returns an unrelated catalogue name for this app through the metadata path,
# even though its own storefront identifies the app as Call of Duty®. Keep the
# public page and its structured metadata attached to the actual product name.
PUBLIC_GAME_NAMES = {
    1938090: "Call of Duty®",
}

# Kept out of the ranking for every profile, not just the owner's: Wallpaper
# Engine is a wallpaper app and it distorts a "most played games" list.
NOT_GAMES = {431960}

# Terrain names are proper nouns; only the catch-all needs translating, so it is
# the one that travels as a key. Same idea everywhere else in this file.
ARMA_TERRAINS = [
    ("AltisPlayTime", "Altis"), ("StratisPlayTime", "Stratis"),
    ("TanoaPlaytime", "Tanoa"), ("EnochPlayTime", "Livonia"),
    ("MaldenPlayTime", "Malden"), ("OtherWorldPlayTime", "@arma.other_maps"),
]

ARMA_ACTIVITIES = [
    ("ZeusNormalPlayerGamePlayTime", "Zeus"),
    ("CampaignPlayTime", "@arma.campaign"),
    ("3DEditorPlayTime", "@arma.editor"),
    ("ZeusPlayerPlayTime", "@arma.zeus_player"),
    ("CampaignEPAPlayTime", "@arma.east_wind"),
    ("WorkshopMissionPlayTime", "@arma.workshop"),
    ("ZeusUnitControlPlayTime", "@arma.zeus_control"),
    ("ShowcasesPlayTime", "@arma.showcases"),
    ("VRPlayTime", "@arma.vr"),
    ("FiringDrillsPlayTime", "@arma.drills"),
]

# Arma exposes per-item usage seconds under DLC prefixes. Only a few ids are
# unambiguous; the rest are shown prettified, flagged as internal names.
ARMA_ITEMS = {
    "envgII": "ENVG-II (visão noturna)", "spar16": "SPAR-16", "backBergen": "mochila Bergen",
    "fullghillie": "traje ghillie", "equip_b_carrier_gl_rig": "Carrier GL Rig",
}


def br(n, d=0):
    """pt-BR number: 4027 -> '4.027', 53.1 -> '53,1'."""
    return f"{n:,.{d}f}".replace(",", "\x00").replace(".", ",").replace("\x00", ".")


def humanize_item(token):
    if token in ARMA_ITEMS:
        return ARMA_ITEMS[token]
    t = re.sub(r"[_]+", " ", token)
    t = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", t).strip()
    # Short opaque tokens read as broken in lowercase; as caps they read as ids.
    return t.upper() if len(t) <= 4 and t.islower() else t


def fetch_public_achievements(appid):
    """The game's complete achievement catalogue, with no player involved."""
    appid = int(appid)
    now = time.time()
    hit = _catalog_cache.get(appid)
    if hit and hit[0] > now:
        return hit[1]
    with _catalog_lock:
        hit = _catalog_cache.get(appid)
        if hit and hit[0] > time.time():
            return hit[1]
        schema_raw = get_json("ISteamUserStats/GetSchemaForGame/v2/", envelope="game",
                              required=False, with_steamid=False, appid=appid)
        rarity_raw = get_json_url(
            f"{API}/ISteamUserStats/GetGlobalAchievementPercentagesForApp/v2/?gameid={appid}",
            f"global rarity for {appid}",
        ) or {}
        rarity = {
            a["name"]: float(a["percent"])
            for a in rarity_raw.get("achievementpercentages", {}).get("achievements", [])
        }
        entries = []
        for a in (schema_raw or {}).get("availableGameStats", {}).get("achievements", []):
            key = a.get("name")
            if not key:
                continue
            entries.append({
                "key": key,
                "name": a.get("displayName") or key,
                "description": (a.get("description") or "").strip(),
                "rarity": round(rarity[key], 2) if key in rarity else None,
                "icon": a.get("icon"),
                "icon_gray": a.get("icongray"),
            })
        _stats_catalog_cache[appid] = [{
            "key": stat.get("name"),
            "name": stat.get("displayName") or stat.get("name"),
            "default": stat.get("defaultvalue"),
        } for stat in (schema_raw or {}).get("availableGameStats", {}).get("stats", [])
            if stat.get("name")]
        value = ((schema_raw or {}).get("gameName") or None, entries)
        if len(_catalog_cache) >= 4000:
            _catalog_cache.clear()
            _stats_catalog_cache.clear()
        _catalog_cache[appid] = (time.time() + PUBLIC_GAME_TTL, value)
        return value


STEAM_NEWS_IMAGE_RE = re.compile(
    r"\{STEAM_CLAN_(?:LOC_)?IMAGE\}/([^\s\]\[<>'\"]+)", re.I,
)


def _news_excerpt(contents):
    """Turn Steam's HTML/BBCode news body into a card excerpt and image.

    Steam leaves clan image placeholders in the API response instead of an
    absolute URL.  Resolve the first one for the card, and remove all image
    markup from the prose so placeholders never leak into the interface.
    """
    raw = contents or ""
    match = STEAM_NEWS_IMAGE_RE.search(raw)
    image = (f"https://clan.steamstatic.com/images/{match.group(1)}"
             if match else None)
    raw = re.sub(r"\[img\].*?\[/img\]", " ", raw, flags=re.I | re.S)
    raw = STEAM_NEWS_IMAGE_RE.sub(" ", raw)
    raw = re.sub(r"\[/?[a-z][^\]]*\]", " ", raw, flags=re.I)
    excerpt = re.sub(r"\s+", " ", strip_tags(raw)).strip()
    return excerpt[:500] or None, image


def _players_held(appid, now):
    """The player count for one app, with `_live_lock` already held.

    Split out so that the two callers below share one cache and one request
    rather than each growing their own copy of this."""
    if len(_players_cache) >= 4000:
        _players_cache.clear()
    hit = _players_cache.get(appid)
    if not hit or hit[0] <= now:
        raw = get_json_url(
            f"{API}/ISteamUserStats/GetNumberOfCurrentPlayers/v1/?appid={appid}",
            f"current players for {appid}", timeout=8,
        ) or {}
        response = raw.get("response") or {}
        players = response.get("player_count") if response.get("result") == 1 else None
        hit = (now + 300, {
            "players": int(players) if isinstance(players, int) else None,
            "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        })
        _players_cache[appid] = hit
    return hit


def fetch_current_players(appid):
    """How many people are in one game right now, and nothing else.

    fetch_public_live answers this together with that app's news, because a
    game page wants both on the same screen. A screen listing eleven games in
    a series wants only this half, and reaching it through the other one would
    be eleven calls to Steam for news nobody on that page is going to read."""
    with _live_lock:
        return _players_held(int(appid), time.time())[1]


def fetch_public_live(appid):
    """Short-lived public facts Valve publishes without a player identity."""
    appid = int(appid)
    now = time.time()
    with _live_lock:
        players_hit = _players_held(appid, now)
        news_hit = _news_held(appid, now)
    return players_hit[1], news_hit[1]


def _news_held(appid, now):
    """Official game news with `_live_lock` already held."""
    if len(_news_cache) >= 4000:
        _news_cache.clear()
    hit = _news_cache.get(appid)
    if hit and hit[0] > now:
        return hit
    raw = get_json_url(
        f"{API}/ISteamNews/GetNewsForApp/v2/?appid={appid}&count=5&maxlength=500",
        f"news for {appid}", timeout=8,
    ) or {}
    news = []
    for item in (raw.get("appnews") or {}).get("newsitems", []):
        excerpt, image = _news_excerpt(item.get("contents"))
        news.append({
            "id": item.get("gid"), "title": item.get("title"),
            "url": item.get("url"), "author": item.get("author"),
            "date": item.get("date"), "feed": item.get("feedlabel"),
            "feed_name": item.get("feedname"),
            "excerpt": excerpt, "image": image,
        })
    hit = (time.time() + 3600, news)
    _news_cache[appid] = hit
    return hit


def fetch_public_news(appid):
    """Official news without also asking for the live player count."""
    with _live_lock:
        return _news_held(int(appid), time.time())[1]


def fetch_achievements(appid):
    """Unlocked achievements with global rarity, unlock dates and their icons.
    The icon URLs point at Steam's CDN, which the CSP allows for images."""
    player = get_json("ISteamUserStats/GetPlayerAchievements/v1/", envelope="playerstats",
                      required=False, appid=appid, l="english")
    entries = (player or {}).get("achievements") or []
    if not entries:
        return None

    _, catalog = fetch_public_achievements(appid)
    schema = {a["key"]: a for a in catalog}

    unlocked = []
    for e in entries:
        if not e.get("achieved"):
            continue
        meta = schema.get(e["apiname"], {})
        ts = e.get("unlocktime") or 0
        unlocked.append({
            "key": e["apiname"],
            "name": meta.get("name") or e.get("name") or e["apiname"],
            "description": (meta.get("description") or e.get("description") or "").strip(),
            "date": date.fromtimestamp(ts).isoformat() if ts else None,
            # Two decimals, not one. Every page prints this through the same
            # helper, and that helper has always had a rule for the unlocks
            # almost nobody holds: below a twentieth of a percent it says
            # "<0,1%" rather than a rounded figure. Rounding to one decimal
            # here made that rule unreachable - 0,04% arrived as 0.0 and was
            # printed as "0,0%", which reads as nobody at all rather than as
            # almost nobody. The extra digit never reaches a page: the helper
            # still prints one.
            "rarity": meta.get("rarity"),
            "icon_url": meta.get("icon"),
        })

    # Icons are served straight from Steam's CDN now. Mirroring them made sense
    # for one fixed profile; for arbitrary lookups it would mean fetching a
    # thousand images before the first page could render.
    for u in unlocked:
        u["icon"] = u.pop("icon_url", None)

    dated = sorted([u for u in unlocked if u["date"]], key=lambda u: u["date"])
    by_year = {}
    for u in dated:
        y = u["date"][:4]
        by_year[y] = by_year.get(y, 0) + 1

    # The other half of the same three calls, and until now it was thrown away:
    # what is still locked. A page can say "82 of 100" without it, but not what
    # the eighteen are - and the eighteen are the only part a reader can still
    # do anything about. Two of them travel rather than the list: the whole set
    # would put thirteen hundred entries into a payload for a game where
    # thirteen hundred is exactly the reason nobody is going to read them.
    locked = []
    for e in entries:
        if e.get("achieved"):
            continue
        m = schema.get(e["apiname"], {})
        locked.append({
            "key": e["apiname"],
            "name": m.get("name") or e.get("name") or e["apiname"],
            "description": (m.get("description") or e.get("description") or "").strip(),
            "rarity": m.get("rarity"),
            # The locked icon, which Steam draws greyed out. The unlocked one is
            # the wrong picture for something nobody here has.
            "icon": m.get("icon_gray") or m.get("icon"),
        })
    # A percentage nobody has is not the same as one nobody published, so the
    # unrated ones sort to the far end of both lists instead of reading as 0%.
    by_rarity = sorted([a for a in locked if a["rarity"] is not None],
                       key=lambda a: a["rarity"])

    ranked = sorted(unlocked, key=lambda u: (u["rarity"] is None, u["rarity"] or 0))
    return {
        "unlocked": len(unlocked),
        "total": len(entries),
        "completion": round(len(unlocked) / len(entries) * 100, 1) if entries else None,
        "rarest": ranked[:12],
        "missing": len(locked),
        # The wall: the rarest thing still locked, which is what a hundred
        # percent actually costs here.
        "hardest_missing": by_rarity[0] if by_rarity else None,
        # And the opposite, which is the more useful of the two: the most
        # common thing this profile does not have. On most libraries it is
        # something the game hands out in its first hour.
        "easiest_missing": by_rarity[-1] if by_rarity else None,
        # Unlock order, which is the only chronology these pages have to work with.
        "list": dated,
        "by_year": by_year,
        "first": dated[0] if dated else None,
        "last": dated[-1] if dated else None,
    }


def game_stats(appid):
    raw = get_json("ISteamUserStats/GetUserStatsForGame/v2/", envelope="playerstats",
                   required=False, appid=appid)
    return {s["name"]: s["value"] for s in (raw or {}).get("stats", [])}


def fetch_arma3(record_hours):
    s = game_stats(107410)
    if not s:
        return None
    g = lambda k: s.get(k, 0)
    h = lambda k: round(g(k) / 3600, 1)
    total = g("TotalPlayTime")
    mp, sp = g("MPPlayTime"), g("SPPlayTime")

    terrains = [
        {"name": label, "hours": h(key)}
        for key, label in ARMA_TERRAINS if g(key)
    ]
    terrains.sort(key=lambda t: -t["hours"])

    activities = [
        {"name": label, "hours": h(key)}
        for key, label in ARMA_ACTIVITIES if g(key)
    ]
    activities.sort(key=lambda a: -a["hours"])

    items = []
    for key, value in s.items():
        m = re.match(r"^(?:MarkPT|ExpPT|ContactPT|OrangePT|TacOpsPT|JetsPT|TankPT|HeliPT|WSPT|GMPT|SOGPT)_?(.+)$", key)
        if not m or not value or value < 1800:
            continue
        items.append({"name": humanize_item(m.group(1)), "hours": round(value / 3600, 1)})
    items.sort(key=lambda i: -i["hours"])

    return {
        "in_match_hours": round(total / 3600),
        "record_hours": record_hours,
        "mp_hours": round(mp / 3600),
        "sp_hours": round(sp / 3600),
        "mp_share": round(mp / (mp + sp) * 100, 1) if mp + sp else None,
        "zeus_hours": h("ZeusNormalPlayerGamePlayTime"),
        "zeus_units_created": g("ZeusUnitsCreated"),
        "editor_hours": h("3DEditorPlayTime"),
        "terrains": terrains,
        "activities": activities[:9],
        "items": items[:12],
    }


# PAYDAY 2 names its heists after production codenames. Only the ones that can be
# mapped with confidence are translated; the rest pass through humanize_item and
# the page says so, because a wrong heist name is worse than a visible id.
PD2_HEISTS = {
    "branchbank": "Bank Heist", "jewelry_store": "Jewelry Store",
    "four_stores": "Four Stores", "nightclub": "Nightclub",
    "mallcrasher": "Mallcrasher", "ukrainian_job": "Ukrainian Job",
    "roberts": "GO Bank", "kosugi": "Shadow Raid", "gallery": "Art Gallery",
    "safehouse": "Safehouse", "red2": "First World Bank", "run": "Heat Street",
    "escape_street": "@pd2.escape_street", "escape_cafe": "@pd2.escape_cafe",
    "escape_cafe_day": "@pd2.escape_cafe_day", "escape_park": "@pd2.escape_park",
    "escape_park_day": "@pd2.escape_park_day", "escape_garage": "@pd2.escape_garage",
    "framing_frame_1": "@pd2.day|name=Framing Frame|n=1", "framing_frame_2": "@pd2.day|name=Framing Frame|n=2",
    "framing_frame_3": "@pd2.day|name=Framing Frame|n=3",
    "firestarter_1": "@pd2.day|name=Firestarter|n=1", "firestarter_2": "@pd2.day|name=Firestarter|n=2",
    "firestarter_3": "@pd2.day|name=Firestarter|n=3",
    "alex_1": "@pd2.day|name=Rats|n=1", "alex_2": "@pd2.day|name=Rats|n=2", "alex_3": "@pd2.day|name=Rats|n=3",
    "watchdogs_1": "@pd2.day|name=Watchdogs|n=1", "watchdogs_2": "@pd2.day|name=Watchdogs|n=2",
    "election_day_1": "@pd2.day|name=Election Day|n=1", "election_day_2": "@pd2.day|name=Election Day|n=2",
    "election_day_3_skip1": "@pd2.day|name=Election Day|n=3",
    "welcome_to_the_jungle_1": "@pd2.day|name=Big Oil|n=1",
    "welcome_to_the_jungle_1_night": "@pd2.day_night|name=Big Oil|n=1",
    "welcome_to_the_jungle_2": "@pd2.day|name=Big Oil|n=2",
    "hox_1": "@pd2.day|name=Hoxton Breakout|n=1",
    "hox_2": "@pd2.day|name=Hoxton Breakout|n=2",
    "hox_3": "Hoxton Revenge",
}

PD2_ENEMIES = {
    "swat": "SWAT", "city_swat": "@pd2.city_swat", "heavy_swat": "@pd2.heavy_swat",
    "fbi_swat": "FBI SWAT", "fbi_heavy_swat": "@pd2.fbi_heavy", "fbi": "FBI",
    "cop": "@pd2.cop", "gangster": "@pd2.gangster", "security": "@pd2.security",
    "shield": "@pd2.shield", "taser": "@pd2.taser", "cloaker": "Cloaker",
    "bulldozer": "Bulldozer", "sniper": "@pd2.sniper", "medic": "@pd2.medic",
    "spooc": "Spooc", "biker": "@pd2.biker", "mobster": "@pd2.mobster",
}


def fetch_payday2(record_hours):
    """PAYDAY 2 keeps 576 counters: a level, per-heist play counts, per-weapon
    shots/hits/kills and per-enemy kills. The weapon ids are Overkill's own."""
    s = game_stats(218620)
    if not s:
        return None

    heists = []
    for key, value in s.items():
        if not key.startswith("level_") or not value:
            continue
        code = key[len("level_"):]
        if code.isdigit() or code.startswith(("safehouse_", "up_")):
            continue
        # Unmapped heists keep their raw codename; the page sets those in mono so
        # they read as ids rather than as a name someone chose.
        heists.append({
            "name": PD2_HEISTS.get(code, code),
            "mapped": code in PD2_HEISTS,
            "runs": int(value),
        })
    heists.sort(key=lambda x: -x["runs"])

    # Weapons are keyed three ways; the kill count is what ranks them.
    guns = {}
    for prefix, field in (("weapon_shots_", "shots"), ("weapon_hits_", "hits"),
                          ("weapon_kills_", "kills")):
        for key, value in s.items():
            if key.startswith(prefix) and value:
                guns.setdefault(key[len(prefix):], {})[field] = int(value)
    weapons = []
    for code, v in guns.items():
        kills, shots, hits = v.get("kills", 0), v.get("shots", 0), v.get("hits", 0)
        if not kills:
            continue
        weapons.append({
            # Raw ids, like the unmapped heists: Overkill publishes no store names,
            # and a half-guessed one would read as fact.
            "name": code,
            "kills": kills,
            "shots": shots,
            "accuracy": round(hits / shots * 100, 1) if shots and hits else None,
        })
    weapons.sort(key=lambda w: -w["kills"])

    enemies = []
    for key, value in s.items():
        if not key.startswith("enemy_kills_") or not value:
            continue
        code = key[len("enemy_kills_"):]
        enemies.append({"name": PD2_ENEMIES.get(code, humanize_item(code)), "kills": int(value)})
    enemies.sort(key=lambda e: -e["kills"])

    total_shots = sum(w["shots"] for w in weapons)
    total_hits = sum(g.get("hits", 0) for g in guns.values())
    return {
        "record_hours": record_hours,
        "level": int(s.get("player_level", 0)) or None,
        "heists": heists[:16],
        "heists_total": len(heists),
        "runs_total": sum(h["runs"] for h in heists),
        "weapons": weapons[:12],
        "enemies": enemies[:10],
        "kills": sum(e["kills"] for e in enemies),
        "shots": total_shots,
        "accuracy": round(total_hits / total_shots * 100, 1) if total_shots else None,
        "unmapped": sum(1 for h in heists[:16] if not h["mapped"]),
    }


RON_TOOLS = [
    ("PROGRESS_ARREST", "@ron.arrests"),
    ("PROGRESS_MIRRORGUN", "@ron.mirror"),
    ("PROGRESS_BREACH", "@ron.breach"),
    ("PROGRESS_DISARM", "@ron.disarm"),
    ("PROGRESS_LOCKPICK", "@ron.lockpick"),
]


def fetch_ready_or_not(record_hours):
    """Ready or Not scores each mission from 0 to 1 and keeps a few tool counters.
    The missions are only numbered in the API, so they stay numbered here."""
    s = game_stats(1144200)
    if not s:
        return None
    missions = []
    for i in range(1, 40):
        v = s.get(f"SCORE_LEVEL_{i}")
        if v is None:
            continue
        missions.append({"n": i, "score": round(float(v) * 100, 1)})
    if not missions:
        return None
    return {
        "record_hours": record_hours,
        "missions": missions,
        "perfect": sum(1 for m in missions if m["score"] >= 99.95),
        "avg_score": round(sum(m["score"] for m in missions) / len(missions), 1),
        "tools": [{"name": label, "value": int(s[key])}
                  for key, label in RON_TOOLS if s.get(key)],
    }


GMOD_STATS = [
    ("GMA_SPAWNMENUER_STAT", "@gmod.spawnmenu"),
    ("GMA_BADCODER_STAT", "@gmod.lua_errors"),
    ("GMA_NPCSPAWNER_STAT", "@gmod.npcs"),
    ("GMA_BADDIES_STAT", "@gmod.enemies"),
    ("GMA_PROPSPAWNER_STAT", "@gmod.props"),
    ("GMA_GOODIES_STAT", "@gmod.allies"),
    ("GMA_RAGDOLLSPAWNER_STAT", "@gmod.ragdolls"),
    ("GMA_BYSTANDER_STAT", "@gmod.civilians"),
    ("GMA_BALLOONPOPPER_STAT", "@gmod.balloons"),
    ("GMA_REMOVER_STAT", "@gmod.removed"),
    ("GMA_X_STARTUPS_STAT", "@gmod.launches"),
    ("GMA_X_MAPS_STAT", "@gmod.maps"),
]


def fetch_gmod(record_hours):
    """Garry's Mod counts the sandbox: what was spawned, what was killed, and how
    many times the Lua console complained about it."""
    s = game_stats(4000)
    if not s:
        return None
    minutes = int(s.get("GMA_X_MINUTES_STAT", 0))
    return {
        "record_hours": record_hours,
        "in_game_hours": round(minutes / 60) if minutes else None,
        "counters": [{"name": label, "value": int(s[key])}
                     for key, label in GMOD_STATS if s.get(key)],
    }


ATS_STATS = [
    ("finish50jobs-progress", "@ats.jobs"),
    ("unload_difficulty-progress", "@ats.hard_unloads"),
    ("ca_visit_cities-progress", "@ats.cities_ca"),
    ("az_visit_cities-progress", "@ats.cities_az"),
    ("az_colorado-progress", "@ats.colorado"),
    ("nv_visit_cities-progress", "@ats.cities_nv"),
]


def fetch_ats(record_hours):
    """American Truck Simulator only exposes achievement progress counters, which
    stop moving once the achievement unlocks. Stated as progress, not as totals."""
    s = game_stats(270880)
    if not s:
        return None
    return {
        "record_hours": record_hours,
        "counters": [{"name": label, "value": int(s[key])}
                     for key, label in ATS_STATS if s.get(key)],
    }




# Class names are proper nouns in every language, so they travel as themselves.
TF2_CLASSES = ["Scout", "Soldier", "Pyro", "Demoman", "Heavy",
               "Engineer", "Medic", "Sniper", "Spy"]


def fetch_tf2(record_hours):
    """Team Fortress 2 keeps a full accumulator per class - the same numbers its
    own Player Statistics screen shows - and a personal best beside each one.
    758 stats, the largest block on the site after CS2's, and the only one that
    is already grouped by something the player chose."""
    s = game_stats(440)
    if not s:
        return None
    g = lambda k: int(s.get(k, 0))

    classes = []
    for c in TF2_CLASSES:
        shots, hits = g(f"{c}.accum.iNumShotsFired"), g(f"{c}.accum.iNumShotsHit")
        kills, deaths = g(f"{c}.accum.iNumberOfKills"), g(f"{c}.accum.iNumDeaths")
        classes.append({
            "name": c,
            "hours": round(g(f"{c}.accum.iPlayTime") / 3600, 1),
            "kills": kills,
            "deaths": deaths,
            "kd": round(kills / deaths, 2) if deaths else None,
            "assists": g(f"{c}.accum.iKillAssists"),
            "points": g(f"{c}.accum.iPointsScored"),
            "damage": g(f"{c}.accum.iDamageDealt"),
            "captures": g(f"{c}.accum.iPointCaptures"),
            "defenses": g(f"{c}.accum.iPointDefenses"),
            "dominations": g(f"{c}.accum.iDominations"),
            "buildings": g(f"{c}.accum.iBuildingsDestroyed"),
            # Only the classes that have one: healing is the Medic's number and
            # backstabs are the Spy's, and a zero in the others is noise.
            "healed": g(f"{c}.accum.iHealthPointsHealed") or None,
            "backstabs": g(f"{c}.accum.iBackstabs") or None,
            "ubers": g(f"{c}.accum.iNumInvulnerable") or None,
            "sentry_kills": g(f"{c}.accum.iSentryKills") or None,
            "headshots": g(f"{c}.accum.iHeadshots") or None,
            "accuracy": round(hits / shots * 100, 1) if shots else None,
            # The .max. side of the same key: TF2's "personal best in one life".
            "best_kills": g(f"{c}.max.iNumberOfKills"),
            "best_points": g(f"{c}.max.iPointsScored"),
            "best_damage": g(f"{c}.max.iDamageDealt"),
        })

    played = [c for c in classes if c["hours"] or c["kills"]]
    played.sort(key=lambda c: -c["hours"])
    total_secs = sum(g(f"{c}.accum.iPlayTime") for c in TF2_CLASSES)

    # TF2 also counts something per map. Valve documents neither the unit nor
    # what increments it, so these are ranked against each other and printed as
    # the raw counter - the same treatment PAYDAY 2's unmapped heist ids get.
    maps = [{"name": k, "value": int(v)} for k, v in s.items()
            if re.match(r"^(cp|ctf|koth|pl|plr|arena|sd)_", k) and v]
    maps.sort(key=lambda m: -m["value"])

    return {
        "record_hours": record_hours,
        "in_game_hours": round(total_secs / 3600, 1) if total_secs else None,
        "classes": played,
        "top": played[0] if played else None,
        "kills": sum(c["kills"] for c in classes),
        "deaths": sum(c["deaths"] for c in classes),
        "points": sum(c["points"] for c in classes),
        "damage": sum(c["damage"] for c in classes),
        "maps": maps[:12],
    }


# Rust names its counters after the thing itself, which is rare enough to be
# worth saying: no decoding table is needed, only translation of the nouns.
RUST_HARVEST = [
    ("harvest.wood", "@rust.wood"), ("harvest.stones", "@rust.stone"),
    ("harvest.metal_ore", "@rust.metal"), ("harvest.sulfur_ore", "@rust.sulfur"),
    ("harvest.cloth", "@rust.cloth"), ("harvest.bone_fragments", "@rust.bone"),
    ("harvest.fat_animal", "@rust.fat"), ("harvest.humanmeat_raw", "@rust.humanmeat"),
]
RUST_KILLS = [
    ("kill_player", "@rust.players"), ("kill_scientist", "@rust.scientists"),
    ("kill_bear", "@rust.bear"), ("kill_wolf", "@rust.wolf"),
    ("kill_boar", "@rust.boar"), ("kill_stag", "@rust.stag"),
    ("kill_chicken", "@rust.chicken"), ("kill_horse", "@rust.horse"),
]
RUST_DEATHS = [
    ("death_fall", "@rust.fall"), ("death_suicide", "@rust.suicide"),
    ("death_selfinflicted", "@rust.self"), ("death_bear", "@rust.by_bear"),
    ("death_wolf", "@rust.by_wolf"), ("death_entity", "@rust.by_entity"),
]


def fetch_rust(record_hours):
    """Rust counts the island: what was chopped out of it, what was shot on it,
    and every way of dying there. The nouns are the counter names."""
    s = game_stats(252490)
    if not s:
        return None
    g = lambda k: int(s.get(k, 0))
    rows = lambda table: sorted(
        ({"name": label, "value": g(key)} for key, label in table if g(key)),
        key=lambda r: -r["value"])

    fired, hit = g("bullet_fired"), g("bullet_hit_player")
    fish = sum(v for k, v in s.items() if k.startswith("caught_"))
    notes = sum(v for k, v in s.items() if k.startswith("notesplayed"))

    return {
        "record_hours": record_hours,
        "harvest": rows(RUST_HARVEST),
        "kills": rows(RUST_KILLS),
        "deaths_by": rows(RUST_DEATHS),
        "deaths": g("deaths"),
        "kill_player": g("kill_player"),
        "bullets": fired,
        "hits": hit,
        "accuracy": round(hit / fired * 100, 1) if fired else None,
        "headshots": g("headshots") or g("headshot"),
        "arrows": g("arrow_fired"),
        "rockets": g("rocket_fired"),
        "blueprints": g("blueprint_studied"),
        "placed": g("placed_blocks"),
        "upgraded": g("upgraded_blocks"),
        "missions": g("missions_completed"),
        "barrels": g("destroyed_barrels"),
        "wounded_healed": g("wounded_healed"),
        # The two counters that say the most about how someone actually plays.
        "fish": fish,
        "notes_played": notes,
        "horse_km": round(g("horse_distance_ridden_km")) or None,
    }


def fetch_dbd(record_hours):
    """Dead by Daylight is two games with one clock, and it counts them apart:
    everything on the killer's side has a mirror on the survivor's."""
    s = game_stats(381210)
    if not s:
        return None
    g = lambda k: int(s.get(k, 0))
    f = lambda k: float(s.get(k, 0) or 0)

    sacrificed, killed = g("DBD_SacrificedCampers"), g("DBD_KilledCampers")
    escapes, hatch = g("DBD_Escape"), g("DBD_EscapeThroughHatch")
    return {
        "record_hours": record_hours,
        "killer": {
            "pips": g("DBD_KillerSkulls"),
            "sacrificed": sacrificed,
            "killed": killed,
            "total": sacrificed + killed,
        },
        "survivor": {
            "pips": g("DBD_CamperSkulls"),
            "escapes": escapes,
            "hatch": hatch,
            # A float in the schema: generators are counted in whole units of
            # progress, so a repair someone else finished still counts partly.
            "generators": round(f("DBD_GeneratorPct_float"), 1),
            "heals": round(f("DBD_HealPct_float"), 1),
            "unhooks": g("DBD_UnhookOrHeal"),
            "hooked_and_escaped": g("DBD_HookedAndEscape"),
            "skill_checks": g("DBD_SkillCheckSuccess"),
        },
        "bloodweb": {
            "points": g("DBD_BloodwebPoints"),
            "level": g("DBD_BloodwebMaxLevel"),
            "prestige": g("DBD_BloodwebMaxPrestigeLevel"),
        },
        # Which side this profile actually plays, by the only measure both share.
        "side": ("killer" if g("DBD_KillerSkulls") > g("DBD_CamperSkulls")
                 else "survivor" if g("DBD_CamperSkulls") else None),
    }


STARDEW_STATS = [
    ("crops", "@sdv.crops"), ("fish", "@sdv.fish"), ("forage", "@sdv.forage"),
    ("monsters", "@sdv.monsters"), ("board_quests", "@sdv.quests"),
    ("loved_gifts", "@sdv.gifts"),
]


def fetch_stardew(record_hours):
    """Seven counters, and every one of them is a whole sentence about the farm.
    The smallest honest stat block on the site."""
    s = game_stats(413150)
    if not s:
        return None
    g = lambda k: int(s.get(k, 0))
    return {
        "record_hours": record_hours,
        "money": g("money"),
        "rows": [{"name": label, "value": g(key)} for key, label in STARDEW_STATS if g(key)],
    }


POE_STATS = [
    ("ActBoss", "@poe.act_bosses"), ("YellowMap", "@poe.yellow_maps"),
    ("LegionMonolith", "@poe.monoliths"), ("ElderKill", "@poe.elder"),
]


def fetch_poe(record_hours):
    """Path of Exile exposes four counters for a game with a hundred systems.
    They are landmarks rather than totals, and the page says so."""
    s = game_stats(238960)
    if not s:
        return None
    g = lambda k: int(s.get(k, 0))
    rows = [{"name": label, "value": g(key)} for key, label in POE_STATS if g(key)]
    return {"record_hours": record_hours, "rows": rows} if rows else None


# ── The rest of the hundred ──────────────────────────────────────────
# Fifteen games with a stat block small enough to state in full. Each one keeps
# its own function rather than sharing a table-walker, because what is worth
# saying differs per game: Insurgency's numbers only mean something split by
# mode, Clicker Heroes' only mean something against each other.

def rows_from(s, table):
    """The shared half: a labelled row per counter that is not zero, biggest
    first. A zero here means "never happened", and printing it says nothing."""
    g = lambda k: int(s.get(k, 0) or 0)
    return sorted(({"name": label, "value": g(key)} for key, label in table if g(key)),
                  key=lambda r: -r["value"])


CLICKER_ROWS = [
    ("hero_levels", "@ch.hero_levels"), ("upgrades", "@ch.upgrades"),
    ("bosses_killed", "@ch.bosses"), ("treasures", "@ch.treasures"),
    ("ascensions", "@ch.ascensions"), ("mercenary_count", "@ch.mercenaries"),
]


def fetch_clicker(record_hours):
    """Clicker Heroes counts clicks and it counts kills, and the gap between the
    two is the whole genre: the game goes on killing while nobody is clicking."""
    s = game_stats(363970)
    if not s:
        return None
    g = lambda k: int(s.get(k, 0) or 0)
    clicks, killed = g("total_clicks"), g("monsters_killed")
    return {
        "record_hours": record_hours,
        "zone": g("highest_zone"),
        "clicks": clicks,
        "killed": killed,
        # The headline number, and the only one that needed arithmetic.
        "per_click": round(killed / clicks) if clicks else None,
        "rows": rows_from(s, CLICKER_ROWS),
    }


INFEST_WEAPONS = [
    ("nz_ar_kills", "@nz.ar"), ("nz_snp_kills", "@nz.snp"),
    ("nz_smg_kills", "@nz.smg"), ("nz_mg_kills", "@nz.mg"),
    ("nz_fist_kills", "@nz.fist"),
]
INFEST_ZOMBIES = [
    ("nz_zombie_kills", "@nz.zombies"), ("nz_sprzombie_kills", "@nz.runners"),
    ("nz_zombie_barekills", "@nz.barehanded"), ("nz_superz_kills", "@nz.super"),
]


def fetch_infestation(record_hours):
    """The New Z separates what was shot at: the living, in four calibres, and
    the dead, in four kinds. The two totals are worth putting side by side."""
    s = game_stats(555570)
    if not s:
        return None
    g = lambda k: int(s.get(k, 0) or 0)
    return {
        "record_hours": record_hours,
        "kills": g("nz_kills"),
        "headshots": g("nz_headshots"),
        "streak": g("nz_max_killstreak"),
        "zombies": g("nz_zombie_kills"),
        "gold": g("nz_gd_amount"),
        "minutes": g("nz_mins_inserver"),
        "characters": g("chars_created"),
        "skins": g("nz_skins_learned"),
        "trades": g("nz_trades_completed"),
        "weapons": rows_from(s, INFEST_WEAPONS),
        "undead": rows_from(s, INFEST_ZOMBIES),
    }


UNTURNED_FOUND = [
    ("Found_Items", "@unt.items"), ("Found_Experience", "@unt.experience"),
    ("Found_Resources", "@unt.resources"), ("Found_Crafts", "@unt.crafts"),
    ("Found_Plants", "@unt.plants"), ("Found_Buildables", "@unt.buildables"),
    ("Found_Fishes", "@unt.fishes"), ("Found_Throwables", "@unt.throwables"),
]


def fetch_unturned(record_hours):
    """Unturned counts three separate things: what was killed, what was picked
    up, and how far it was carried - on foot and by vehicle, kept apart."""
    s = game_stats(304930)
    if not s:
        return None
    g = lambda k: int(s.get(k, 0) or 0)
    shot, hit = g("Accuracy_Shot"), g("Accuracy_Hit")
    foot, vehicle = g("Travel_Foot"), g("Travel_Vehicle")
    return {
        "record_hours": record_hours,
        "zombies": g("Kills_Zombies_Normal"),
        "players": g("Kills_Players"),
        "animals": g("Kills_Animals"),
        "deaths": g("Deaths_Players"),
        "headshots": g("Headshots"),
        "shots": shot,
        "hits": hit,
        "accuracy": round(hit / shot * 100, 1) if shot else None,
        "foot": foot,
        "vehicle": vehicle,
        # Metres in the schema; the page says kilometres, which is readable.
        "foot_km": round(foot / 1000, 1),
        "vehicle_km": round(vehicle / 1000, 1),
        "found": rows_from(s, UNTURNED_FOUND),
    }


def fetch_insurgency(record_hours):
    """Insurgency keeps every counter twice - once for versus, once for
    co-op - and the split is the most honest thing on the page, so it is the
    page. The `*All` fields are the game's own sums; they are not re-added."""
    s = game_stats(222880)
    if not s:
        return None
    g = lambda k: int(s.get(k, 0) or 0)
    versus = {"kills": g("TotalKills"), "captures": g("TotalCaptures"),
              "mvps": g("TotalMVPs"), "hero": g("TotalHeroCaptures")}
    coop = {"kills": g("TotalKillsCoop"), "captures": g("TotalCapturesCoop"),
            "mvps": g("TotalMVPsCoop"), "hero": g("TotalHeroCapturesCoop")}
    return {
        "record_hours": record_hours,
        "versus": versus,
        "coop": coop,
        "kills": g("TotalKillsAll"),
        "captures": g("TotalCapturesAll"),
        "mvps": g("TotalMVPsAll"),
        "hero": g("TotalHeroCapturesAll"),
        "side": "coop" if coop["kills"] > versus["kills"] else "versus",
    }


MQUEST_ROWS = [
    ("NumberOfCreaturesKilled", "@mq.creatures"), ("NumberOfItemsLooted", "@mq.looted"),
    ("NumberOfPotionsUsed", "@mq.potions"), ("NumberOfVampiresKilled", "@mq.vampires"),
    ("NumberOfSpidersKilled", "@mq.spiders"), ("NumberOfBossesKilled", "@mq.bosses"),
]


def fetch_mighty_quest(record_hours):
    """The Mighty Quest is two games - raiding other castles and building your
    own - and it counts them apart. The raids are also split by whether the
    castle outranked you, which is the closest thing here to a difficulty."""
    s = game_stats(239220)
    if not s:
        return None
    g = lambda k: int(s.get(k, 0) or 0)
    higher = g("NumberOfHigherLevelCastlesDefeated")
    same = g("NumberOfSameLevelCastlesDefeated")
    lower = g("NumberOfLowerLevelCastlesDefeated")
    return {
        "record_hours": record_hours,
        "castles": {"higher": higher, "same": same, "lower": lower,
                    "total": higher + same + lower},
        "streak": g("NumberOfCastlesDefeatedConsecutively"),
        "gold": g("AmountOfGoldCollected"),
        "own": {"rooms": g("NumberOfCastleRooms"), "creatures": g("CreaturesPlacedInCastle")},
        "rows": rows_from(s, MQUEST_ROWS),
    }


def fetch_reforger(record_hours):
    """Three counters, and Reforger names them like a debrief rather than like a
    scoreboard. Two of them are what was done and one is what was done back."""
    s = game_stats(1874880)
    if not s:
        return None
    g = lambda k: int(s.get(k, 0) or 0)
    killed, died = g("STAT_ENEMIES_NEUTRALIZED"), g("STAT_PLAYER_KILLED")
    if not (killed or died or g("STAT_VEHICLES_DESTROYED")):
        return None
    return {
        "record_hours": record_hours,
        "neutralized": killed,
        "vehicles": g("STAT_VEHICLES_DESTROYED"),
        "deaths": died,
        "ratio": round(killed / died, 2) if died else None,
    }


def fetch_sandstorm(record_hours):
    """Sandstorm exposes two counters for a game with a hundred of them: how
    often an objective was taken, and how often the radio was picked up."""
    s = game_stats(581320)
    if not s:
        return None
    g = lambda k: int(s.get(k, 0) or 0)
    caps, calls = g("TotalObjectiveCaptures_TotalObjectiveCaptures"), \
        g("TotalObserverCalls_TotalObserverCalls")
    if not (caps or calls):
        return None
    return {"record_hours": record_hours, "captures": caps, "calls": calls}


def fetch_cats(record_hours):
    """One counter. It is also the entire game, so the page is that number and
    the arithmetic around it rather than a layout with a hole in the middle."""
    s = game_stats(2977660)
    clicks = int((s or {}).get("Clicks", 0) or 0)
    if not clicks:
        return None
    minutes = (record_hours or 0) * 60
    return {
        "record_hours": record_hours,
        "clicks": clicks,
        "per_minute": round(clicks / minutes, 1) if minutes else None,
        "per_hour": round(clicks / record_hours) if record_hours else None,
    }


def fetch_specops(record_hours):
    """Spec Ops keeps stats only for its multiplayer, which is not the game
    anyone remembers it for. They are shown as what they are - a handful of
    matches - and the page proper is built on the campaign's achievements."""
    s = game_stats(50300)
    if not s:
        return None
    g = lambda k: int(s.get(k, 0) or 0)
    kills, deaths = g("Kills_Kills"), g("Kills_Deaths")
    if not (kills or deaths or g("Level")):
        return None
    return {
        "record_hours": record_hours,
        "kills": kills,
        "deaths": deaths,
        "level": g("Level"),
        "streak": g("Kills_Streak"),
        "defeats": g("TeamDeathmatch_Defeats") + g("Deathmatch_Defeats"),
    }


def fetch_besiege(record_hours):
    """Besiege counts the only thing a siege engine can be judged on."""
    s = game_stats(346010)
    if not s:
        return None
    g = lambda k: int(s.get(k, 0) or 0)
    killed = g("AI_KILLED")
    if not killed:
        return None
    return {
        "record_hours": record_hours,
        "killed": killed,
        "secondaries": g("CAMPAIGN_SECONDARIES"),
        "ips": g("IPS_PROGRESS"),
    }


TRIBAL_ROWS = [
    ("stat_pillager", "@tw.plundered"), ("stat_defeat_units_many", "@tw.defeated"),
    ("stat_recruit_many", "@tw.recruited"), ("stat_build_many", "@tw.buildings"),
    ("stat_instant_complete", "@tw.rushed"), ("stat_quests", "@tw.quests"),
]


def fetch_tribal_wars(record_hours):
    """Tribal Wars ships every counter twice, as `_some` and `_many` - two
    thresholds on one number, not two numbers. Only one of each pair is read."""
    s = game_stats(675690)
    if not s:
        return None
    g = lambda k: int(s.get(k, 0) or 0)
    rows = rows_from(s, TRIBAL_ROWS)
    if not rows:
        return None
    return {"record_hours": record_hours, "points": g("stat_points_many"), "rows": rows}


FOREST_ROWS = [
    ("STAT_CHOPPED_TREES", "@tfr.trees"), ("STAT_KILLED_CANNIBALS", "@tfr.cannibals"),
    ("STAT_CHOPPED_BODIES", "@tfr.bodies"), ("STAT_UNIQUE_CRAFTED_ITEMS", "@tfr.crafted"),
    ("STAT_REVIVED_PLAYERS", "@tfr.revived"), ("STAT_COLLECTED_CASSETTES", "@tfr.cassettes"),
    ("STAT_FOUND_PASSENGERS", "@tfr.passengers"), ("STAT_PLANTED_SEEDS", "@tfr.seeds"),
    ("STAT_ATE_MUSHROOMS_TYPES", "@tfr.mushrooms"),
]


def fetch_forest(record_hours):
    """The Forest counts days survived, and then it counts what was done to stay
    alive on them. It also counts the cannibalism, which is the game's own joke
    and not one this page should quietly drop."""
    s = game_stats(242760)
    if not s:
        return None
    g = lambda k: int(s.get(k, 0) or 0)
    days = g("STAT_DAYS_SURVIVED")
    return {
        "record_hours": record_hours,
        "days": days,
        "peaceful": g("STAT_DAYS_WITHOUT_KILLS"),
        "cannibalism": g("STAT_CANNIBALISM"),
        "rows": rows_from(s, FOREST_ROWS),
    }


def fetch_geoguessr(record_hours):
    """Two counters, and the second is a subset of the first: how many duels
    were won, and how many were won without losing a round."""
    s = game_stats(3478870)
    if not s:
        return None
    g = lambda k: int(s.get(k, 0) or 0)
    wins, flawless = g("quickplayDuelsWins"), g("quickplayWinsFlawless")
    if not wins:
        return None
    return {
        "record_hours": record_hours,
        "wins": wins,
        "flawless": flawless,
        "share": round(flawless / wins * 100, 1) if wins else None,
    }


def fetch_strife(record_hours):
    """Strife keeps a win counter per hero. Six wins spread over five of them is
    a small number that says something exact, so it is drawn per hero."""
    s = game_stats(339280)
    if not s:
        return None
    g = lambda k: int(s.get(k, 0) or 0)
    wins, losses = g("STAT_WINS"), g("STAT_LOSSES")
    if not (wins or losses):
        return None
    heroes = sorted(
        ({"name": humanize_item(k[10:-5]).title(), "value": int(v)}
         for k, v in s.items()
         if k.startswith("STAT_HERO_") and k.endswith("_WINS") and int(v or 0)),
        key=lambda r: (-r["value"], r["name"]))
    return {
        "record_hours": record_hours,
        "wins": wins,
        "losses": losses,
        "played": wins + losses,
        "winrate": round(wins / (wins + losses) * 100, 1) if wins + losses else None,
        "heroes": heroes,
    }


BIZTOUR_ROWS = [
    ("STAT_TRAVELER_01", "@bt.laps"), ("STAT_WON_TRIPLE_01", "@bt.monopolies"),
    ("STAT_LUCKY", "@bt.lucky"), ("STAT_WON_LASTPLAYER_01", "@bt.last_standing"),
    ("STAT_WON_RESORT_01", "@bt.resort"),
]


def fetch_business_tour(record_hours):
    """Monopoly with the serial numbers filed off, and it counts the board: laps
    completed, monopolies closed, and the two ways a game can be won."""
    s = game_stats(397900)
    if not s:
        return None
    rows = rows_from(s, BIZTOUR_ROWS)
    if not rows:
        return None
    g = lambda k: int(s.get(k, 0) or 0)
    return {
        "record_hours": record_hours,
        "wins": g("STAT_WON_LASTPLAYER_01") + g("STAT_WON_RESORT_01"),
        "rows": rows,
    }


# One extractor per kind, so build_game does not grow a branch per game.
def fetch_stalker2(record_hours):
    """S.T.A.L.K.E.R. 2 publishes five counters, and they are not the same kind
    of thing as each other - which is the whole page.

    Every one of them pairs with a `Progressive_<name>` achievement, so the
    reflex is to treat all five as achievement progress and drop them, the way
    Red Dead's and Dying Light's are dropped. But their own display names give
    them away: three state the threshold they are counting towards ("Kill 50
    enemies through obstacles"), and those stop dead the moment it is reached.
    Two do not - "Headshots" and "Stealth kills" are just tallies that happen to
    have an achievement hanging off them somewhere.

    So the two are printed as counts and the three as progress against the
    number in their own label, and the page says which is which. Presenting all
    five as totals would quietly claim this profile stopped at exactly 50."""
    s = game_stats(1643320)
    if not s:
        return None

    # The threshold is stated in the schema's display name and nowhere else.
    capped = [
        ("NoObstacleTooBig", 50,      "@st2.obstacles"),
        ("Demoman",          20,      "@st2.bombs"),
        ("CouponsToBurn",    1000000, "@st2.coupons"),
    ]
    tallies = [
        ("DoHeadshots",    "@st2.headshots"),
        ("DoStealthKills", "@st2.stealth"),
    ]
    counts = [{"label": label, "value": int(s[key])}
              for key, label in tallies if s.get(key)]
    progress = [
        {"label": label, "value": int(s[key]), "goal": goal,
         "pct": round(min(100, s[key] / goal * 100), 1),
         # Sitting exactly on the threshold is the tell: the counter stopped
         # there rather than the profile happening to land on it.
         "stopped": s[key] >= goal}
        for key, goal, label in capped if s.get(key)
    ]
    if not counts and not progress:
        return None
    return {"record_hours": record_hours, "counts": counts, "progress": progress}


# Left 4 Dead counts under a `TD3.` prefix and Left 4 Dead 2 under `Stat.`, and
# the two sets are not the same shape at all - which is lucky, because it means
# the two pages are told apart by their data rather than by decoration.
L4D1_CAMPAIGNS = [
    ("TD3.NoMercyPlayTime.Total", "No Mercy"),
    ("TD3.DeathTollPlayTime.Total", "Death Toll"),
    ("TD3.DeadAirPlayTime.Total", "Dead Air"),
    ("TD3.BloodHarvestPlayTime.Total", "Blood Harvest"),
]
L4D1_SURVIVORS = [
    ("TD3.PlayedBill.Total", "Bill"), ("TD3.PlayedZoey.Total", "Zoey"),
    ("TD3.PlayedFrancis.Total", "Francis"), ("TD3.PlayedLouis.Total", "Louis"),
]
L4D1_INFECTED = ["Boomer", "Hunter", "Smoker"]
L4D1_GUNS = [
    ("Pistol", "@l4d.pistol"), ("Smg", "@l4d.smg"), ("Shotgun", "@l4d.shotgun"),
    ("AutoShotgun", "@l4d.autoshotgun"), ("Rifle", "@l4d.rifle"),
    ("Minigun", "@l4d.minigun"), ("PipeBomb", "@l4d.pipebomb"), ("Molotov", "@l4d.molotov"),
]


def fetch_l4d1(record_hours):
    """Left 4 Dead's own Versus scoreboard: the survivors on one side of it and
    the special infected on the other.

    Two things here are worth being careful about. The stat block also carries
    `TD2.Achievements.Count.NN`, which is achievement progress and is dropped
    whole. And the per-weapon *shot* counters do not agree with the hit
    counters - Steam reports 144 SMG shots against 1.232 hits on the profile
    this was written from - so accuracy is only published for the weapons where
    hits actually fit inside shots, and the page says why the rest are missing."""
    s = game_stats(500)
    if not s:
        return None
    g = lambda k: s.get(k, 0)

    campaigns = [{"name": name, "hours": round(g(key) / 3600, 1)}
                 for key, name in L4D1_CAMPAIGNS if g(key)]
    campaigns.sort(key=lambda c: -c["hours"])
    survivors = [{"name": name, "games": int(g(key))}
                 for key, name in L4D1_SURVIVORS if g(key)]
    survivors.sort(key=lambda p: -p["games"])

    infected = []
    for who in L4D1_INFECTED:
        spawns = g(f"TD3.TotalSpawns.{who}")
        if not spawns:
            continue
        infected.append({
            "name": who,
            "spawns": int(spawns),
            "best": int(g(f"TD3.MostDamage1Life.{who}")),
            # Seconds, and the average life of a special infected is the joke:
            # the Hunter on this profile lasted 51 of them.
            "life": int(g(f"TD3.AvgLifeSpan.{who}")),
            "attacks": int(g(f"TD3.SpecAttack.{who}")),
        })
    infected.sort(key=lambda i: -i["spawns"])

    guns, broken = [], 0
    for key, label in L4D1_GUNS:
        kills = g(f"{key}.Kills.Total") or g(f"TD3.{key}.Kills.Total")
        if not kills:
            continue
        shots, hits = g(f"TD3.{key}.Shots.Total"), g(f"TD3.{key}.Hit.Total")
        # Only where the two agree. More hits than shots is not an accuracy.
        sane = shots and hits and hits <= shots
        if shots and hits and hits > shots:
            broken += 1
        guns.append({"label": label, "kills": int(kills),
                     "accuracy": round(hits / shots * 100) if sane else None})
    guns.sort(key=lambda w: -w["kills"])

    return {
        "record_hours": record_hours,
        "games": int(g("TD3.GamesPlayed.Total")),
        "finales": int(g("TD3.FinaleFinished.Total")),
        "killed": int(g("TD3.InfectedKilled.Total")),
        "ff": int(g("TD3.FFDamage.Total")),
        "ff_worst": int(g("TD3.FFDamageGameMost.Total")),
        "best_score": int(g("TD3.HighestSurvivorScore.Total")),
        "versus_games": int(g("TD3.GamesPlayed.Versus")),
        "versus_won": int(g("TD3.GamesWon.Versus")),
        "campaigns": campaigns,
        "survivors": survivors,
        "infected": infected,
        "guns": guns,
        "guns_broken": broken,
    }


def fetch_l4d2(record_hours):
    """Left 4 Dead 2's end-of-campaign scoreboard, which is a different set of
    counters from the first game's and gets a different page for that reason.

    The line this page is actually about is friendly fire. It is the one number
    in the series everybody argues over, Valve counts it, and it is right there
    in the payload - including the worst single game, which is the number that
    settles the argument."""
    s = game_stats(550)
    if not s:
        return None
    g = lambda k: s.get(f"Stat.{k}.Total", 0)
    avg = lambda k: s.get(f"Stat.{k}.Avg", 0)
    games = int(g("GamesPlayed"))
    if not games and not g("TotalPlayTime"):
        return None

    return {
        "record_hours": record_hours,
        "games": games,
        "finales": int(g("FinaleFinished")),
        "seconds": int(g("TotalPlayTime")),
        "killed": int(g("InfectedKilled")),
        "best_score": int(g("HighestSurvivorScore")),
        # What this profile did for the team, and what it did to the team.
        "care": [
            {"label": "@l4d.kits_used", "n": int(g("KitsUsed")), "avg": round(avg("KitsUsed"), 2)},
            {"label": "@l4d.kits_shared", "n": int(g("KitsShared")), "avg": round(avg("KitsShared"), 2)},
            {"label": "@l4d.pills", "n": int(g("PillsUsed")), "avg": round(avg("PillsUsed"), 2)},
            {"label": "@l4d.revived_most", "n": int(g("TeamRevivedMost")), "avg": None},
            {"label": "@l4d.protected_most", "n": int(g("TeamProtectedMost")), "avg": None},
            {"label": "@l4d.was_revived", "n": int(g("WasRevived")), "avg": round(avg("WasRevived"), 2)},
            {"label": "@l4d.was_protected", "n": int(g("WasProtected")), "avg": round(avg("WasProtected"), 2)},
        ],
        "ff": int(g("FFDamage")),
        "ff_worst": int(g("FFDamageGameMost")),
        "ff_avg": round(s.get("Stat.FFDamageGame.Avg", 0)),
    }


# The 2004 weapon list, in the order the buy menu had them. Names are proper
# nouns and stay as they are; only the group headings travel as keys.
CSS_GUNS = [
    ("glock", "Glock"), ("usp", "USP"), ("p228", "P228"), ("deagle", "Desert Eagle"),
    ("elite", "Dual Elites"), ("fiveseven", "Five-SeveN"),
    ("m3", "M3"), ("xm1014", "XM1014"),
    ("mac10", "MAC-10"), ("tmp", "TMP"), ("mp5navy", "MP5"), ("ump45", "UMP45"), ("p90", "P90"),
    ("galil", "Galil"), ("famas", "FAMAS"), ("ak47", "AK-47"), ("m4a1", "M4A1"),
    ("sg552", "SG552"), ("aug", "AUG"),
    ("scout", "Scout"), ("awp", "AWP"), ("g3sg1", "G3SG1"), ("sg550", "SG550"),
    ("m249", "M249"), ("knife", "@css.knife"), ("hegrenade", "@css.grenade"),
]


def fetch_css(record_hours):
    """Counter-Strike: Source - the round-end scoreboard.

    CS2 already has the buy menu on this site, so this page is deliberately the
    other screen: the one everybody held Tab to read. The money counter is the
    hero number because it is the most Source thing in the payload - three
    hundred thousand dollars earned, and ninety kills to show for it."""
    s = game_stats(240)
    if not s:
        return None
    g = lambda k: s.get(k, 0)
    kills, deaths = int(g("i_Number_Of_Kills")), int(g("i_Number_Of_Deaths"))
    if not kills and not g("i_Time_Played"):
        return None

    guns = [{"name": label, "kills": int(g(f"total_kills_{key}"))}
            for key, label in CSS_GUNS if g(f"total_kills_{key}")]
    guns.sort(key=lambda w: -w["kills"])

    return {
        "record_hours": record_hours,
        "kills": kills,
        "deaths": deaths,
        "ratio": round(kills / deaths, 2) if deaths else None,
        "wins": int(g("total_wins")),
        "seconds": int(g("i_Time_Played")),
        "damage": int(g("i_Damage_Done")),
        "money": int(g("i_Money_Earned")),
        "planted": int(g("i_Number_Of_PlantedBombs")),
        "defused": int(g("i_Number_Of_DefusedBombs")),
        "hostages": int(g("i_Number_Of_RescuedHostages")),
        "guns": guns[:14],
    }


ALYX_MOVE = [
    ("Minutes in Move Type: Blink", "@alyx.blink"),
    ("Minutes in Move Type: Shift", "@alyx.shift"),
    ("Minutes in Move Type: Continuous (Head)", "@alyx.cont_head"),
    ("Minutes in Move Type: Continuous (Hand)", "@alyx.cont_hand"),
]
ALYX_DIFF = [
    ("Minutes in Difficulty: Story Mode", "@alyx.story"),
    ("Minutes in Difficulty: Easy", "@alyx.easy"),
    ("Minutes in Difficulty: Normal", "@alyx.normal"),
    ("Minutes in Difficulty: Hard", "@alyx.hard"),
]
ALYX_HANDS = [
    ("Minutes in Right Hand Mode", "@alyx.right"),
    ("Minutes in Left Hand Mode", "@alyx.left"),
    ("Minutes in Single Controller Mode", "@alyx.single"),
]


def fetch_alyx(record_hours):
    """Half-Life: Alyx - how this person moved through City 17.

    Four hundred and forty counters, and the interesting ones are not kills.
    Valve instrumented *locomotion*: minutes spent blinking, shifting or
    walking continuously, minutes with the quick turn on, which hand held the
    gun, and how many times each chapter was teleported through. No other game
    on this site reports anything like it, because no other game on this site
    had to ask whether moving would make you sick.

    All of these are in minutes, and they overlap on purpose - a minute is
    counted in a move type *and* in a difficulty *and* in a hand mode. They are
    three separate readings of the same clock, so the page shows them as three
    separate bars rather than adding any of them up."""
    s = game_stats(546560)
    if not s:
        return None

    def band(rows):
        out = [{"label": label, "minutes": round(s.get(key, 0))}
               for key, label in rows if s.get(key, 0) >= 1]
        out.sort(key=lambda r: -r["minutes"])
        total = sum(r["minutes"] for r in out)
        for r in out:
            r["share"] = round(r["minutes"] / total * 100, 1) if total else 0
        return out

    move, diff, hands = band(ALYX_MOVE), band(ALYX_DIFF), band(ALYX_HANDS)
    teleports = sum(v for k, v in s.items() if k.endswith("Num Teleports"))
    quick_on = sum(s.get(k, 0) for k in
                   ("Minutes with Quick Turn On", "Minutes with Quick Turn On (Continuous)"))
    quick_off = s.get("Minutes with Quick Turn Off", 0)

    if not (move or diff or hands or teleports):
        return None
    return {
        "record_hours": record_hours,
        "move": move,
        "difficulty": diff,
        "hands": hands,
        "teleports": int(teleports),
        "bottles": int(s.get("SIDE_GLOBAL_BREAK_BOTTLES_STAT", 0)),
        "quick_on": round(quick_on),
        "quick_off": round(quick_off),
    }


EXTRACTORS = {
    "stalker2":  ("stalker2",  fetch_stalker2),
    "alyx":      ("alyx",      fetch_alyx),
    "l4d1":      ("l4d1",      fetch_l4d1),
    "l4d2":      ("l4d2",      fetch_l4d2),
    "css":       ("css",       fetch_css),
    "clicker":   ("clicker",   fetch_clicker),
    "infest":    ("infest",    fetch_infestation),
    "unturned":  ("unturned",  fetch_unturned),
    "insurgency": ("insurgency", fetch_insurgency),
    "mquest":    ("mquest",    fetch_mighty_quest),
    "reforger":  ("reforger",  fetch_reforger),
    "sandstorm": ("sandstorm", fetch_sandstorm),
    "cats":      ("cats",      fetch_cats),
    "specops":   ("specops",   fetch_specops),
    "besiege":   ("besiege",   fetch_besiege),
    "tribal":    ("tribal",    fetch_tribal_wars),
    "forest":    ("forest",    fetch_forest),
    "geoguessr": ("geoguessr", fetch_geoguessr),
    "strife":    ("strife",    fetch_strife),
    "biztour":   ("biztour",   fetch_business_tour),
}


# ── Builders ─────────────────────────────────────────────────────────

def build_public_game(appid, cc=None, hydrate=True, language="en"):
    """One game's facts. Nothing in this payload is indexed by a steamid."""
    appid = int(appid)
    spec = GAME_LAYOUTS.get(appid, {"kind": "plain", "theme": "generic"})
    known = meta.lookup([appid], cc).get(appid) or {}
    # This lives on disk, so restarting the API does not turn an enumeration of
    # already disproved ids back into Steam-key calls.
    if hydrate and known.get("public_absent"):
        raise GameNotFound(appid)
    if hydrate:
        known = meta.public_catalog(appid, cc, language)
    schema_name, entries = fetch_public_achievements(appid) if hydrate else (None, [])
    live, news = fetch_public_live(appid) if hydrate else ({}, [])
    store = meta.price(appid, cc) if hydrate else None
    if store is not None:
        store.update({
            "cc": meta.cc_of(cc),
            "free": known.get("free"),
            "genres": (known.get("catalog") or {}).get("genres") or known.get("genres"),
            "priced": bool(known.get("priced")),
            "detailed": bool(known.get("detailed")),
        })
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "appid": appid,
        # GetSchemaForGame.gameName is an internal Valve label for many apps
        # (and is empty for others).  Store metadata is the canonical display
        # name; schema is only a last resort while that asynchronous detail
        # pass is still being learned.
        "name": (PUBLIC_GAME_NAMES.get(appid) or
                 (known.get("catalog") or {}).get("name") or known.get("name") or
                 schema_name or f"app {appid}"),
        "kind": spec["kind"],
        "theme": spec["theme"],
        "art": f"/art/{appid}.jpg",
        "store": store,
        "catalog": known.get("catalog"),
        "catalog_at": known.get("catalog_at"),
        "reviews": ((known.get("reviews") or {}) | {"checked_at": known.get("reviews_at")}
                    if known.get("reviews") else None),
        "live": live or None,
        "news": news,
        "stat_schema": _stats_catalog_cache.get(appid) or [],
        "achievements": ({"total": len(entries), "list": entries}
                         if entries else None),
    }

def build_game(appid, row=None):
    """One game's payload for the current user. `row` is that game's entry from
    build_profile()'s library, so this needs no second GetOwnedGames."""
    appid = int(appid)
    row = row or {}
    name, hours = row.get("name"), row.get("hours")
    spec = GAME_LAYOUTS.get(appid, {"kind": "plain", "theme": "generic"})
    kind = spec["kind"]

    # Composition without changing the cost of the established personal path:
    # its own achievement builder hydrates the same catalogue below.
    public = build_public_game(appid, hydrate=False)
    payload = {
        "generated_at": public["generated_at"],
        "steamid": STEAM_ID,
        "appid": appid,
        "kind": public["kind"],
        "theme": public["theme"],
        "name": name or public["name"],
        "record_hours": hours,
        "last_played": row.get("last_played"),
        "rank": row.get("rank"),
        "share": row.get("share"),
        # The game's own key art, for the pages that have little else to show.
        # Served by us rather than by Steam: the file is ~400 KB and it would
        # otherwise cross the internet on every view of every game. The first
        # request for an appid fills the cache; see art.py.
        "art": f"/art/{appid}.jpg",
        # Straight from the library listing; every page can fall back on these.
        "os": row.get("os"),
        "hours_2weeks": round(row["minutes_2weeks"] / 60, 1) if row.get("minutes_2weeks") else None,
        # The bare games expose nothing; asking Steam anyway just wastes a call.
        "achievements": None if kind == "bare" else fetch_achievements(appid),
    }

    if kind == "dota":
        payload["dota"] = fetch_dota(hours)
    elif kind == "cs2":
        payload["cs2"] = fetch_cs2(hours)
    elif kind == "arma3":
        payload["arma3"] = fetch_arma3(hours)
    elif kind == "payday2":
        payload["payday2"] = fetch_payday2(hours)
    elif kind == "readyornot":
        payload["ron"] = fetch_ready_or_not(hours)
    elif kind == "gmod":
        payload["gmod"] = fetch_gmod(hours)
    elif kind == "ats":
        payload["ats"] = fetch_ats(hours)
        payload["counters_note"] = "@note.progress_counters"
    elif kind == "tf2":
        payload["tf2"] = fetch_tf2(hours)
    elif kind == "rust":
        payload["rust"] = fetch_rust(hours)
    elif kind == "dbd":
        payload["dbd"] = fetch_dbd(hours)
    elif kind == "stardew":
        payload["stardew"] = fetch_stardew(hours)
    elif kind == "poe":
        payload["poe"] = fetch_poe(hours)
    elif kind in EXTRACTORS:
        key, extract = EXTRACTORS[kind]
        payload[key] = extract(hours)
    elif appid == 236390:
        # War Thunder's stats are achievement progress counters, most of them
        # capped at their threshold, so only the uncapped ones are honest.
        s = game_stats(236390)
        payload["counters"] = [
            {"name": "@wt.units", "value": s.get("steam_trophy_get_2000_units_stat", 0)},
            {"name": "@wt.battle_hours", "value": s.get("steam_trophy_1000_hours_in_battle_stat", 0)},
            {"name": "@wt.avatars", "value": s.get("steam_trophy_avatar_collection_stat", 0)},
        ]
        payload["counters_note"] = "@note.wt_counters"

    # Both of these expose a stat block made entirely of achievement progress -
    # AchievementStat_1…43 and ACH_10_PROGRESS…ACH_53_PROGRESS. The pages are
    # built on the achievements instead, and say why rather than leaving the
    # reader to wonder where the numbers a stats page would have gone.
    if appid in (1174180, 239140):
        payload["counters_note"] = "@note.ach_progress_only"

    # The three S.T.A.L.K.E.R. Enhanced Editions ship a counter per achievement
    # - stat_zone_safari, stat_boltman, stat_stalkerstrike - and nothing else.
    # Same trap, same answer: the pages are built on the achievements those
    # counters were following, and they say so rather than leaving a reader to
    # wonder where a stats page went.
    if appid in (2427410, 2427420, 2427430):
        payload["counters_note"] = "@note.ach_progress_only"

    # GTA V Enhanced ships `Stat_ACH10`…`Stat_ACH50` - the same achievement
    # progress the legacy release does. Arma Tactics ships tiered counters
    # (`KILLER_COUNTER`, `MASTER_KILLER_COUNTER`, `UBER_MASTER_KILLER_COUNTER`)
    # which are the three steps of one achievement rather than three totals.
    # And Counter-Strike Nexon's are named `SA2003`, `SA2056` and so on, which
    # say nothing at all. All three are built on achievements instead.
    if appid in (3240220, 224860, 273110):
        payload["counters_note"] = "@note.ach_progress_only"

    # A themed layout with no data keeps its identity and says what is missing.
    # It used to fall back to the generic page, which hid the reason: a visitor
    # looking up someone else's Dota just got a plain page with no explanation.
    filled = any(payload.get(k) for k in
                 ("dota", "cs2", "arma3", "payday2", "ron", "gmod", "ats", "counters",
                  "tf2", "rust", "dbd", "stardew", "poe",
                  *(key for key, _ in EXTRACTORS.values())))
    ach = payload.get("achievements") or {}
    payload["partial"] = not (filled or ach.get("unlocked"))
    return payload


# ── What the library cost, and what it is made of ────────────────────
# Both of these read meta.py, which reads the storefront rather than the API:
# a price and a genre belong to the game, not to the player, so one fetch
# serves every profile that owns it. Nothing here ever waits on a fetch. What
# the cache has is what gets counted, the payload says how much that was, and
# the page prints the coverage beside the number rather than implying it is
# the whole library.


def build_economics(library, unplayed, with_apps=False, cc=None):
    """The money and the genres, out of whatever the store cache already holds.

    `cc` is the storefront to total in - the reader's own, so that the number
    at the top of the profile is in the same currency as the game pages it
    links to. Steam prices each region separately, so these are three different
    totals and not one total converted three ways."""
    owned = [(g["appid"], g.get("name") or "", g.get("hours") or 0) for g in library]
    owned += [(g["appid"], g.get("name") or "", None) for g in unplayed]
    known = meta.lookup((a for a, _, _ in owned), cc)
    # Anything missing or stale is queued here and fetched in the background,
    # so a profile nobody has looked up before is complete a few minutes later.
    # Every storefront, not just this reader's: the next visitor may be reading
    # in another language, and the crawl is cheap while nobody is waiting.
    meta.want(a for a, _, _ in owned)

    currency = next((m["currency"] for m in known.values() if m.get("currency")), None)
    money = {
        "currency": currency, "total": 0, "played": 0, "never": 0,
        "owned": len(owned), "read": 0, "paid": 0, "free": 0, "unsold": 0,
        "per_hour": None, "hours_priced": 0, "cheapest": [], "dearest": [],
    }
    rates = []
    genres, genre_hours = {}, 0.0
    detailed = 0

    for appid, name, hours in owned:
        m = known.get(appid)
        if not m:
            continue
        if m["priced"]:
            money["read"] += 1
            price = m["price"]
            # `is not None` rather than truthiness. A game at a hundred percent
            # off has a price of zero, and testing the cents as a boolean sent
            # it down to "delisted" - a game being given away counted as a game
            # that cannot be bought. It costs nothing, so it adds nothing to the
            # total, but it is a game with a price and it is counted as one.
            if price is not None:
                money["paid"] += 1
                money["total"] += price
                if hours is None:
                    money["never"] += price
                else:
                    money["played"] += price
            elif m["free"]:
                money["free"] += 1
            elif m["detailed"]:
                # Detail came back and said it is not free, but the store has no
                # price: delisted. A real category, and not one to fold into
                # "free" - these are games that cannot be bought at any price.
                money["unsold"] += 1
        if m["detailed"]:
            detailed += 1
        if hours:
            for g in m["genres"] or []:
                slot = genres.setdefault(g["id"], {"id": g["id"], "name": g["name"],
                                                   "hours": 0.0, "games": 0})
                slot["hours"] += hours
                slot["games"] += 1
            if m["genres"]:
                genre_hours += hours
        # The price of an hour only means anything where both numbers exist.
        if hours and m["price"]:
            rates.append({"appid": appid, "name": name, "hours": hours,
                          "price": m["price"],
                          "per_hour": round(m["price"] / hours)})
            money["hours_priced"] += hours

    # Over the games that have both a price and hours, and nothing else: a
    # library's cost divided by its whole clock would credit the paid games
    # with every hour the free ones absorbed.
    if money["hours_priced"]:
        money["per_hour"] = round(sum(r["price"] for r in rates) / money["hours_priced"])
    money["hours_priced"] = round(money["hours_priced"])
    rates.sort(key=lambda r: r["per_hour"])
    money["cheapest"] = rates[:5]
    money["dearest"] = list(reversed(rates[-5:]))

    ranked = sorted(genres.values(), key=lambda g: -g["hours"])
    for g in ranked:
        g["hours"] = round(g["hours"], 1)
        g["share"] = round(g["hours"] / genre_hours * 100, 1) if genre_hours else 0

    out = {
        "money": money,
        # A game filed under three genres is counted in all three, so these add
        # up to more than the library does. The panel says so; splitting the
        # hours three ways would invent a division Steam never made.
        "genres": ranked[:12],
        "coverage": {
            "owned": len(owned),
            "priced": money["read"],
            "detailed": detailed,
            "played": len(library),
            # The genre chart is weighted by hours, so hours are what its
            # coverage has to be stated in. The queue is in hours order, which
            # means the twenty games that carry a library are classified in the
            # first minute and the long tail catches up over the next ten.
            "genre_hours": round(genre_hours),
            "hours": round(sum(g.get("hours") or 0 for g in library)),
        },
    }
    if with_apps:
        # Compact on purpose: this is one entry per game owned, and a long
        # library is five thousand of them. [price, year, free].
        out["apps"] = {
            str(appid): [m["price"], m["year"], (1 if m["free"] else 0) if m["free"] is not None else None]
            for appid, m in known.items() if m["priced"] or m["detailed"]
        }
    return out


def ban_record():
    """What Steam has against this account: VAC, game bans, trading.

    One call, and the only one on the site that answers a question about a
    profile rather than about a library. It is here because it is the one fact
    a reader cannot get from anywhere else on the page and can get wrong from
    the silence: a profile with thirteen years and no marks looks exactly like
    a profile with a VAC ban on it, and Steam publishes the difference.

    Named for the record rather than for the interface, because `bans` in this
    service already means something else - bans.py shuts addresses out of the
    site, and nothing here has anything to do with that.

    A clean account answers with every counter at zero, and `days_since` at
    zero as well, which is why that field is only read when there is something
    for it to be counting from. Clean is the ordinary case, so it returns None
    and the panel simply is not drawn: a row saying "no bans" on every profile
    on the site would be noise on all of them and news on none.

    The date is derived, not published. Steam counts days, so the day itself is
    today minus that count - which is exact to the day and stated as a day."""
    got = get_json("ISteamUser/GetPlayerBans/v1/", envelope="players",
                   required=False, with_steamid=False, steamids=STEAM_ID)
    row = (got or [{}])[0] if isinstance(got, list) else {}
    vac = int(row.get("NumberOfVACBans") or 0)
    game = int(row.get("NumberOfGameBans") or 0)
    community = bool(row.get("CommunityBanned"))
    # "none" is Steam's own word for an account with nothing on it here.
    economy = (row.get("EconomyBan") or "none").lower()
    if not (vac or game or community or economy != "none"):
        return None
    days = int(row.get("DaysSinceLastBan") or 0)
    return {
        "vac": vac,
        "game": game,
        "community": community,
        "economy": None if economy == "none" else economy,
        # Only where a ban exists to date. A clean account also reports zero
        # days, and printing that as "banned today" is the one way to be
        # completely wrong about this.
        "days_since": days if (vac or game) else None,
        "last": (date.fromordinal(date.today().toordinal() - days).isoformat()
                 if (vac or game) else None),
        # Steam says a ban happened and never says what it was for. The page
        # has to be as quiet about the reason as the source is.
        "vac_active": bool(row.get("VACBanned")),
    }


def build_friends():
    """The friend list, for the versus page.

    `/u/<a>/vs/<b>` has existed since the treemap did, and until now it needed
    both names typed from memory. A friend list is one call, and it turns the
    feature into something a visitor can click.

    Private friend lists are the normal case rather than an error: Steam answers
    401 and the panel simply is not drawn."""
    # `friendslist`, not `response`: this interface wraps its payload in its own
    # name, the way the stats ones use `playerstats`.
    got = get_json("ISteamUser/GetFriendList/v1/", envelope="friendslist",
                   required=False, relationship="friend")
    friends = (got or {}).get("friends") or []
    if not friends:
        return None
    # Newest first: the people someone added recently are the ones they are
    # most likely to want to look up.
    friends.sort(key=lambda f: -(f.get("friend_since") or 0))
    # A big list is hundreds of avatars nobody scrolls to, and each hundred is
    # another call. Two calls' worth is more than enough for a strip.
    friends = friends[:200]

    people = {}
    ids = [f["steamid"] for f in friends]
    for start in range(0, len(ids), 100):
        chunk = ids[start:start + 100]
        got = get_json("ISteamUser/GetPlayerSummaries/v2/", required=False,
                       with_steamid=False, steamids=",".join(chunk))
        for p in (got or {}).get("players") or []:
            people[p["steamid"]] = p

    out = []
    for f in friends:
        p = people.get(f["steamid"])
        if not p:
            continue
        since = f.get("friend_since") or 0
        out.append({
            "steamid": f["steamid"],
            "persona": p.get("personaname") or f["steamid"],
            "avatar": p.get("avatarmedium") or p.get("avatar"),
            # 3 is public, 1 is private. This is the profile's own visibility
            # and not the "game details" setting a lookup actually needs, so it
            # is a warning rather than a promise: the strip dims the private
            # ones instead of offering a link that can only end in an error.
            "public": (p.get("communityvisibilitystate") or 0) == 3,
            "since": date.fromtimestamp(since).isoformat() if since else None,
        })
    return {"total": len(out), "people": out} if out else None


def build_mates(rows, people, limit):
    """The friend list weighed against this library, one call per friend.

    The strip above it can already say who is on the list. What it cannot say is
    the thing anybody actually wants from a friend list, which is comparative:
    who here has the bigger clock, what everybody owns, and - the one nobody
    else can answer - which games on this profile nobody else on the list has
    ever bought. A library is only unusual next to other libraries.

    One `GetOwnedGames` per friend and nothing else. Playtime and the appid set
    come out of the same answer, so the leaderboard and the overlap cost the
    same single call, and the friends are taken in the order the strip already
    shows them - most recently added first - because that is the list the
    reader is looking at when they press the button.

    A friend whose game details are private answers with nothing. That is the
    common case rather than a failure, and it is reported as private instead of
    as a zero, because a zero would put them last in a ranking they are not in.

    `rows` is this profile's library, and only the games with hours on them: a
    game nobody here has launched is not evidence of anything, and asking who
    else owns it is asking about the pile rather than about this profile."""
    scanned, mates = 0, []
    # How many friends own each appid, over the ones that answered.
    owned_by = {}
    answered = 0
    mine = {g["appid"] for g in rows}

    for person in people:
        if scanned >= limit:
            break
        if not person.get("public"):
            continue
        scanned += 1
        got = get_json("IPlayerService/GetOwnedGames/v1/", required=False,
                       with_steamid=False, steamid=person["steamid"],
                       include_played_free_games=1)
        games = (got or {}).get("games")
        entry = {
            "steamid": person["steamid"],
            "persona": person["persona"],
            "avatar": person.get("avatar"),
        }
        if not games:
            # Either the details are private or the account owns nothing. Steam
            # answers the same way for both, so this says the thing that is
            # certainly true - nothing came back - and not the guess.
            mates.append({**entry, "private": True})
            continue
        answered += 1
        minutes = sum(g.get("playtime_forever", 0) for g in games)
        theirs = {g["appid"] for g in games if g["appid"] not in NOT_GAMES}
        for appid in theirs:
            owned_by[appid] = owned_by.get(appid, 0) + 1
        mates.append({
            **entry,
            "private": False,
            "hours": round(minutes / 60),
            "games": len(theirs),
            "common": len(theirs & mine),
        })

    ranked = sorted((m for m in mates if not m["private"]),
                    key=lambda m: -(m["hours"] or 0))
    for i, m in enumerate(ranked):
        m["rank"] = i + 1
    # Where this profile sits in its own leaderboard. Computed here rather than
    # left to the page, because the page would have to know that a friend list
    # is not a ranking until somebody puts the subject into it.
    my_hours = round(sum(g.get("hours") or 0 for g in rows))
    ahead = sum(1 for m in ranked if (m["hours"] or 0) > my_hours)

    # The games on this profile that none of the friends who answered own. Only
    # meaningful if somebody answered at all - with nobody to compare against,
    # every game is unowned by everybody, which is a sentence about an empty
    # set and not about this library.
    alone = []
    if answered:
        alone = [{"appid": g["appid"], "name": g["name"], "hours": g.get("hours")}
                 for g in rows if not owned_by.get(g["appid"])]
        alone.sort(key=lambda g: -(g["hours"] or 0))

    return {
        "scanned": scanned,
        "answered": answered,
        "private": scanned - answered,
        "limit": limit,
        "people": mates,
        "ranked": ranked,
        "me": {"hours": my_hours, "rank": ahead + 1, "of": len(ranked) + 1},
        # Everything, and the page shows the top of it. A library where sixty
        # games are unshared is a fact about the whole sixty.
        "alone": alone[:24],
        "alone_total": len(alone),
        "compared": len(rows),
    }


def build_rarities(rows):
    """The rarest thing on the whole profile, across games rather than inside one.

    Every game page already asks Steam what share of the world holds each of its
    achievements. Nothing ever put those side by side, and side by side is where
    they mean something: one page can say "3,1% of players have this", but only
    the whole library can say which unlock is the rarest one anybody here owns.

    Expensive - three calls per game - so it is asked for by name, from a button,
    and cached like everything else. `rows` is the top of the library, which is
    where an unlock rare enough to be worth showing is going to be.

    One scan, three answers. The rarest unlocks were the first question, and the
    same three calls per game already hold the two that were being thrown away:
    what is still locked, which is the only part anybody can still act on, and
    when each unlock happened, which is the only per-year record Steam keeps
    about a profile at all. Asking for them separately would have meant paying
    the same price twice, so the scan answers all three or none of them."""
    scanned, with_any, found = 0, 0, []
    close, perfect, years = [], [], {}
    for row in rows:
        appid, name = row["appid"], row["name"]
        scanned += 1
        try:
            got = fetch_achievements(appid)
        except SteamError:
            continue
        if not got or not got.get("unlocked"):
            continue
        with_any += 1
        for a in got["rarest"]:
            if a.get("rarity") is None:
                continue
            found.append({**a, "game": name, "appid": appid})

        # Done, and it says so. Counted rather than ranked: a game at a hundred
        # percent has no distance left to sort it by.
        if not got["missing"]:
            perfect.append({"appid": appid, "name": name, "total": got["total"]})
        else:
            close.append({
                "appid": appid,
                "name": name,
                "unlocked": got["unlocked"],
                "total": got["total"],
                "missing": got["missing"],
                "completion": got["completion"],
                "hardest": got["hardest_missing"],
                "easiest": got["easiest_missing"],
            })

        for year, n in (got.get("by_year") or {}).items():
            slot = years.setdefault(year, {"unlocks": 0, "games": []})
            slot["unlocks"] += n
            slot["games"].append({"appid": appid, "name": name, "n": n})

    found.sort(key=lambda a: a["rarity"])
    # By how many are left, not by percentage. Two of a hundred and two of five
    # are the same amount of work and a very different pair of percentages, and
    # the number a reader can act on is the count. Where the count ties, the
    # easier wall goes first: a rarity of 30% is an evening and 0.4% is a year,
    # and the ones with no published rarity sort last rather than as zero.
    close.sort(key=lambda g: (g["missing"],
                              -((g["hardest"] or {}).get("rarity") or -1)))
    for slot in years.values():
        slot["games"].sort(key=lambda g: -g["n"])
        slot["games"] = slot["games"][:6]
    return {
        "scanned": scanned,
        "with_achievements": with_any,
        "considered": len(found),
        "rarest": found[:24],
        # Everything scanned that is not finished, nearest first.
        "close": close[:12],
        "perfect": perfect,
        # Keyed by year as a string, because that is what it is used as: the
        # year page reads one entry out of this by name.
        "years": years,
    }


# Where a community item's picture lives. The API answers with a path and not
# a URL - "items/1239300/70281f0d….jpg" - because the same file is on every one
# of Steam's CDNs. This one is picked because it is already in the site's
# Content-Security-Policy for avatars, so the browser is allowed to load it
# without widening anything.
ITEM_CDN = "https://cdn.cloudflare.steamstatic.com/steamcommunity/public/images/"


def _item_url(path):
    return ITEM_CDN + path if path else None


def profile_items():
    """What the profile is wearing: the background, the frame, the animated
    avatar. Steam sells these and the profile page shows them, so a page about
    a profile that leaves them out is showing a plainer person than the one
    whose profile it is.

    Everything here is optional twice over: the call itself is `required=False`,
    because an account that has never bought a single item answers with empty
    objects and that is not a failure; and each slot is checked on its own,
    because equipping a frame and no background is ordinary.

    The background comes as a still and as a video. Both travel: the still is
    what a browser that will not play video gets, what stands in while the
    video is still arriving, and what a reader who asked for less motion keeps.

    The large movie, not the small one. The small is what Steam draws in a
    hover card and it is three hundred pixels wide; stretched across a desktop
    background it is a smear. This one covers the viewport, which is the job."""
    got = get_json("IPlayerService/GetProfileItemsEquipped/v1/", required=False)

    def slot(name, *keys):
        item = got.get(name) or {}
        if not item:
            return None
        out = {"name": item.get("item_title") or item.get("name") or None,
               "appid": item.get("appid")}
        for key in keys:
            out[key] = _item_url(item.get(key))
        # A slot whose every picture came back empty is a slot with nothing in
        # it, whatever the rest of the object says.
        return out if any(out.get(k) for k in keys) else None

    return {
        "background": slot("profile_background", "image_large",
                           "movie_webm", "movie_mp4"),
        # The one Steam draws behind the hover card, bought in the points
        # shop. A different picture from the profile background and a different
        # shape - it is a strip, not a wall - so it travels on its own and
        # lands somewhere its proportions make sense.
        "mini": slot("mini_profile_background", "image_large",
                     "movie_webm", "movie_mp4"),
        # Both, and the small one first, because "small" here does not mean
        # small: a frame's image_small is the APNG that animates - ninety
        # frames and three quarters of a megabyte - and its image_large is the
        # still preview of it. The same inversion the animated avatar has, and
        # the reason the frame was arriving frozen.
        "frame": slot("avatar_frame", "image_small", "image_large"),
        "avatar": slot("animated_avatar", "image_small"),
    }


def level_percentile(level):
    """How much of Steam is below this level, or None.

    Cheap in aggregate despite being a key call: see _level_cache above. The
    first reader at each level pays for it and everyone after that reads memory,
    which is why /profile is not priced any higher for having it."""
    if not isinstance(level, int) or level < 0:
        return None
    now = time.monotonic()
    with _level_lock:
        hit = _level_cache.get(level)
        if hit and hit[0] > now:
            return hit[1]
        if len(_level_cache) > 6000:
            _level_cache.clear()
    got = get_json("IPlayerService/GetSteamLevelDistribution/v1/", required=False,
                   with_steamid=False, player_level=level)
    pct = (got or {}).get("player_level_percentile")
    pct = round(float(pct), 2) if isinstance(pct, (int, float)) else None
    with _level_lock:
        _level_cache[level] = (now + LEVEL_TTL, pct)
    return pct


# How much of a wishlist the panel prints. Steam lets a wishlist run to
# thousands and nobody scrolls past the first screen of one; the rest is a
# number and a total, which is what the money line is for.
WISH_ROWS = 60


def build_wishlist(cc=None):
    """What this profile wants, and what it would cost today.

    Two calls and a disk read. The wishlist itself is the odd one out in this
    file: `IWishlistService/GetWishlist` answers without a key at all - measured
    - so the only thing here spending the allowance is the followed games beside
    it. The prices come from meta.py, the same cache the library totals use, in
    the same storefront, so "what my wishlist costs" and "what my library cost"
    are two numbers that can honestly sit next to each other.

    Never fatal. A wishlist can be private, and Steam says so by answering with
    nothing rather than by refusing, which is the same shape as an empty one and
    is reported as such."""
    got = get_json("IWishlistService/GetWishlist/v1/", required=False)
    items = (got or {}).get("items") or []
    followed = ((get_json("IStoreService/GetGamesFollowed/v1/", required=False)
                 or {}).get("appids") or [])

    appids = [i["appid"] for i in items if isinstance(i.get("appid"), int)]
    known = meta.lookup(appids + [a for a in followed if isinstance(a, int)], cc)
    # The same background fill the library totals use: a wishlist nobody has
    # looked up before is priced a few minutes later, and the panel says how
    # much of it is priced meanwhile rather than pretending the rest is free.
    meta.want(appids + [a for a in followed if isinstance(a, int)])

    currency = next((m["currency"] for m in known.values() if m.get("currency")), None)
    rows, total, priced = [], 0, 0
    for item in items:
        appid = item.get("appid")
        if not isinstance(appid, int):
            continue
        m = known.get(appid) or {}
        price = m.get("price")
        if price is not None:
            total += price
            priced += 1
        added = item.get("date_added")
        rows.append({
            "appid": appid,
            "name": m.get("name") or f"app {appid}",
            # Steam files an unranked wishlist entry as 0. That is "no opinion"
            # and not "first", so it sorts after everything that was ranked.
            "priority": item.get("priority") or 0,
            "added": (date.fromtimestamp(added).isoformat()
                      if isinstance(added, int) and added else None),
            "price": price,
            "initial": m.get("initial"),
            "discount": m.get("discount") or 0,
            "free": bool(m.get("free")),
            "year": m.get("year"),
        })

    # Ranked first in the order the person put them in, then the unranked by
    # when they were added, newest first. Two different orders because they are
    # two different statements: one is a decision and the other is a date.
    rows.sort(key=lambda r: (r["priority"] == 0, r["priority"],
                             r["added"] is None, (r["added"] or "")[::-1]))

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "steamid": STEAM_ID,
        "wishlist": {
            "total": len(rows),
            "shown": min(len(rows), WISH_ROWS),
            "items": rows[:WISH_ROWS],
            "on_sale": sum(1 for r in rows if r["discount"]),
        },
        "followed": {
            "total": len(followed),
            "items": [{"appid": a, "name": (known.get(a) or {}).get("name") or f"app {a}"}
                      for a in followed if isinstance(a, int)],
        },
        # Shaped like build_economics' money block so the panel that draws one
        # can draw the other, coverage and all.
        "money": {"currency": currency, "total": total,
                  "quoted": priced, "items": len(rows)},
        "filling": {"unpriced": len(rows) - priced},
    }


# How many games the collection panel prints in full. The rest are counted and
# totalled; nobody scrolls a hundred card sets.
COLLECTION_ROWS = 60


def _loose(hash_name):
    """A card hash with its "<appid>-" prefix off, casefolded.

    Only ever used when the exact join found nothing at all - see the comment at
    its one call site. Keeping the normal path exact matters more than covering
    a rename that has never been observed."""
    head, _, rest = (hash_name or "").partition("-")
    return (rest or head).strip().casefold()


def build_collection(steamid):
    """What finishing each badge would cost this profile.

    Three sources, and only one of them is a request: inv.py reads the profile's
    cards off the community host, cards.py already knows every game's set and
    what each card in it costs, and meta.py already knows the games' names. The
    join is on the card's market hash, which both sides store character for
    character.

    Takes a steamid rather than reading module state, like build_public_game
    does, because none of its three sources are keyed on who set_user last
    named."""
    inventory = inv.of(steamid)

    # A closed inventory is the ordinary case and not a failure, the same way a
    # private friend list is. Nothing here pretends to know anything, and the
    # panel is not drawn.
    if inventory["state"] in ("private", "unknown"):
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "steamid": steamid,
            "state": inventory["state"],
            "cards": {"held": 0, "games": 0, "dupes": 0},
            "count": inventory["count"], "read": inventory["read"],
            "games": [], "closest": [],
            "totals": {"complete_all": None, "quoted": 0, "games": 0},
            "currency": "USD", "rates": None, "rates_at": None,
            "filling": {"unknown_sets": 0, "stale_sets": 0, "unpriceable": 0},
        }

    held = inventory["held"]
    icons = inventory["icons"]
    appids = list(held)
    sets = cards.known(appids)
    # Whatever the market has not been asked about yet is queued behind the
    # page, the same way build_cards does it. Nobody waits.
    cards.want(appids)
    names = meta.lookup(appids)
    meta.want(a for a in appids if not (names.get(a) or {}).get("name"))

    rows, unknown_sets, stale_sets, unpriceable = [], 0, 0, 0
    for appid, mine in held.items():
        got = sets.get(appid)
        name = (names.get(appid) or {}).get("name") or f"app {appid}"

        # `unknown` is nobody having asked the market about this app yet, and
        # `none` with cards in hand is a contradiction: the market said this app
        # has no cards while the inventory is holding some, which happens when a
        # set was added after the row was written and its thirty-day life has
        # not run out. The inventory is the harder evidence either way, so both
        # answer "not known yet" and neither is totalled as zero - a cost that
        # grows as the crawl catches up is the failure build_cards already
        # avoids with its three-valued has_cards.
        if got is None or got["state"] in ("unknown", "none"):
            unknown_sets += 1
            rows.append({"appid": appid, "name": name, "set": "unknown",
                         "count": None, "have": len(mine), "need": None,
                         "dupes": sum(max(0, n - 1) for n in mine.values()),
                         "sets_held": None, "cost_to_complete": None,
                         "unpriced": 0, "stale": False, "held": [], "missing": []})
            continue

        catalogue = {c["hash"]: c for c in got["cards"] if c.get("hash")}
        mine_now = mine
        # The join is on market_hash_name, and it is exact on everything
        # measured. The fallback below fires only when the exact join matched
        # nothing at all, which is what a silent rename on Valve's side would
        # look like; firing it any earlier would trade an exact answer for a
        # fuzzy one to no purpose.
        if catalogue and not (set(mine) & set(catalogue)):
            catalogue = {_loose(h): c for h, c in catalogue.items()}
            mine_now = {_loose(h): n for h, n in mine.items()}

        have = [h for h in catalogue if h in mine_now]
        missing = [h for h in catalogue if h not in mine_now]
        prices = [catalogue[h]["cents"] for h in missing]
        # A card the set has and nobody is selling has no price, and a total
        # that skips it is not what completing the badge costs. Same rule as
        # cards._do_set applies to a set's own cost: all of it or none of it,
        # never a figure rounded down into a lie.
        if any(p is None for p in prices):
            cost = None
            unpriceable += 1
        else:
            # 0 is a real answer here, and the best one: nothing is missing, so
            # the badge can be made today for nothing.
            cost = sum(prices)
        if got["stale"]:
            stale_sets += 1

        rows.append({
            "appid": appid, "name": name,
            "set": got["state"], "count": got["count"],
            "have": len(have), "need": len(missing),
            "dupes": sum(max(0, n - 1) for h, n in mine_now.items() if h in catalogue),
            # Complete sets sitting there right now, which is zero on almost
            # every game and the answer to "can I craft one today" when it isn't.
            "sets_held": min((mine_now.get(h, 0) for h in catalogue), default=0),
            "cost_to_complete": cost,
            "unpriced": sum(1 for p in prices if p is None),
            "stale": got["stale"],
            "held": [{"hash": h, "name": catalogue[h]["name"],
                      "amount": mine_now[h],
                      "icon": icons.get(h) or catalogue[h].get("icon")}
                     for h in have],
            "missing": [{"hash": h, "name": catalogue[h]["name"],
                         "cents": catalogue[h]["cents"],
                         "icon": catalogue[h].get("icon")} for h in missing],
        })

    # Closest to done first, then cheapest. A game one card away for four cents
    # is the entire point of the panel, and it has to be the first row.
    rows.sort(key=lambda r: (r["need"] if r["need"] is not None else 99,
                             r["cost_to_complete"] if r["cost_to_complete"] is not None
                             else 10 ** 9))
    quoted = [r["cost_to_complete"] for r in rows if r["cost_to_complete"] is not None]
    # One rate for the whole answer rather than one per row: it is a fact about
    # the day, not about a game.
    rate = fx.quote()
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "steamid": steamid,
        "state": inventory["state"],
        "cards": {"held": inventory["cards"], "games": inventory["games"],
                  "dupes": inventory["dupes"]},
        "count": inventory["count"], "read": inventory["read"],
        "games": rows[:COLLECTION_ROWS],
        # The ones worth acting on: still missing something, and priced.
        "closest": [r for r in rows
                    if r["need"] and r["cost_to_complete"] is not None][:10],
        "totals": {"complete_all": sum(quoted) if quoted else None,
                   "quoted": len(quoted), "games": len(rows)},
        "currency": "USD",
        "rates": rate["rates"] if rate else None,
        "rates_at": rate["at"] if rate else None,
        # Three different sentences, and the page should be able to say which
        # one it is looking at: never asked, asked yesterday, asked and a card
        # has no seller.
        "filling": {"unknown_sets": unknown_sets, "stale_sets": stale_sets,
                    "unpriceable": unpriceable},
    }


# How many Workshop items the panel prints. The same reasoning as BADGE_TILES:
# past this it is a number, not a wall of thumbnails.
WORKSHOP_ITEMS = 24


def workshop_count():
    """How many things this profile published to the Workshop.

    Keyless. Answered as a count on the profile so the heavier call below is
    only ever made for the profiles that have something to show, which is a
    small minority of them."""
    got = get_json("IPublishedFileService/GetUserFileCount/v1/", required=False,
                   totalonly=True)
    total = (got or {}).get("total")
    return int(total) if isinstance(total, int) else None


def _pick(value, kind):
    """One field off a Workshop item, or None.

    Read defensively on purpose. The owner of this site has published nothing,
    so the shape of a non-empty answer could not be checked against a real one
    here - and a panel that half-builds a row from a field that came back in an
    unexpected shape is worse than a panel that leaves the row out."""
    if kind is int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    if kind is str:
        value = (value or "") if isinstance(value, str) else ""
        return value.strip() or None
    return None


def build_workshop():
    """What this profile published to the Workshop.

    One keyless call. Everything about a row is optional except its id and its
    title: without those two there is nothing to link to and nothing to call it,
    and the row is dropped rather than drawn as a blank."""
    got = get_json("IPublishedFileService/GetUserFiles/v1/", required=False,
                   numperpage=WORKSHOP_ITEMS, return_previews=True,
                   return_vote_data=True, return_short_description=True)
    got = got or {}
    total = _pick(got.get("total"), int) or 0
    rows = []
    for item in got.get("publishedfiledetails") or []:
        fid = _pick(item.get("publishedfileid"), str)
        title = _pick(item.get("title"), str)
        if not fid or not title:
            continue
        # `or {}` and not a default argument: Steam sends the key with a null
        # in it on an item nobody has voted on, and a default only fires when
        # the key is absent altogether.
        votes = item.get("vote_data") or {}
        up = _pick(votes.get("votes_up"), int)
        down = _pick(votes.get("votes_down"), int)
        created = _pick(item.get("time_created"), int)
        updated = _pick(item.get("time_updated"), int)
        rows.append({
            "id": fid,
            "title": title,
            "preview": _pick(item.get("preview_url"), str),
            "appid": _pick(item.get("consumer_appid") or item.get("creator_appid"), int),
            "subscriptions": _pick(item.get("subscriptions"), int),
            "favorited": _pick(item.get("favorited"), int),
            "views": _pick(item.get("views"), int),
            # Absent rather than zero when nobody voted: no votes and a
            # unanimous downvote are different things.
            "votes": ({"up": up or 0, "down": down or 0} if up is not None
                      or down is not None else None),
            "created": date.fromtimestamp(created).isoformat() if created else None,
            "updated": date.fromtimestamp(updated).isoformat() if updated else None,
            "description": _pick(item.get("short_description"), str),
        })
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "steamid": STEAM_ID,
        "total": total,
        "items": rows,
        "truncated": total > len(rows),
    }


def build_econ():
    """The site owner's own trading, and nobody else's.

    IEconService takes no steamid: it answers about the account that owns the
    key, which is this site's owner, whatever profile is on screen. There is no
    version of this that is a panel about the profile being looked up, so the
    route refuses anyone else and the panel has to say what it is.

    Counts and a date, never rows. A trade history names the accounts on the
    other side of every trade, and those people did not agree to appear on a
    public website."""
    offers = (get_json("IEconService/GetTradeOffersSummary/v1/", required=False,
                       with_steamid=False) or {})
    history = (get_json("IEconService/GetTradeHistory/v1/", required=False,
                        with_steamid=False, max_trades=1, include_total=True) or {})
    last = None
    for trade in history.get("trades") or []:
        when = trade.get("time_init")
        if isinstance(when, int) and when:
            last = date.fromtimestamp(when).isoformat()
        break
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "owner": True,
        "offers": {
            "pending_received": offers.get("pending_received_count"),
            "pending_sent": offers.get("pending_sent_count"),
            "new_offers": offers.get("new_received_count"),
        },
        "trades": {"total": history.get("total_trades"), "last": last},
    }


def build_profile():
    """The dashboard payload for the current user: library, totals, treemap,
    recent activity and the index of game pages. One page of API calls, no
    per-game work - the game pages are fetched on demand, one at a time."""
    owned = get_json("IPlayerService/GetOwnedGames/v1/",
                     include_appinfo=1, include_played_free_games=1,
                     # Same request, more columns: has_workshop, has_dlc,
                     # has_market, capsule_filename and sort_as ride along for
                     # nothing. The other extended fields the docs mention -
                     # has_community_visible_stats, content_descriptorids,
                     # playtime_disconnected - already arrive without this and
                     # are not what it is here for.
                     include_extended_appinfo=1)
    games = [g for g in owned.get("games", []) if g["appid"] not in NOT_GAMES]
    if not games:
        raise SteamError("@err.games_hidden")

    # Everything else this build needs, asked for at once rather than one at a
    # time. Ten independent questions: nine to the key-authenticated API and one
    # to the community site, which is itself three scrapes that have to stay in
    # a row. A cold build used to cost the sum of all of them; it now costs the
    # slowest.
    #
    # The library above is not in here on purpose. A hidden library ends the
    # build, and ending it before ten more requests go out is the difference
    # between a refused lookup costing one call and eleven.
    def community_pages():
        """The three steamcommunity.com scrapes, in order, on one thread.

        These do not fan out. That host is the one that answers a burst of a
        dozen with a 429 lasting minutes, so they stay a sequence - and the
        sequence runs beside the API calls instead of after them, which is where
        the three round trips they cost actually go away.

        The order inside is the order it always was, and for the reason it
        always was: the profile page says whether the community site is
        answering this build at all, and the badge page is a third request to
        it. When the profile page came back with nothing - rate-limited, or a
        profile that shows a stranger nothing - asking again spends a request to
        be told so twice, and spends it at the exact moment Steam is asking for
        less."""
        scraped = scrape_profile()
        reachable = bool(scraped)
        # `is not None` rather than a plain truth test. The filter is here so an
        # empty XML field cannot clobber something the HTML scrape got right,
        # and parse_xml already drops the empty ones - but it also answers
        # booleans now, and `limited: False` is the good, common case that a
        # truth test would throw away, turning "this account is fine" into
        # "nobody knows".
        scraped.update({k: v for k, v in scrape_xml().items() if v is not None})
        worn = scrape_badges() if reachable else {"list": [], "total": None}
        return scraped, worn

    def level_and_rank():
        """The level and where it sits against everybody else's. One after the
        other because the second is a question about the answer to the first,
        and both behind one name because that is one thread's work."""
        level = get_json("IPlayerService/GetSteamLevel/v1/",
                         required=False).get("player_level")
        return level, level_percentile(level)

    got = gather({
        # First, because it is the only one of these that can fail the build:
        # everything else is required=False and answers empty. gather() waits in
        # this order, so a private profile still reports what it always did.
        "summaries": lambda: get_json("ISteamUser/GetPlayerSummaries/v2/",
                                      steamids=STEAM_ID, with_steamid=False),
        "level": level_and_rank,
        "recent": lambda: get_json("IPlayerService/GetRecentlyPlayedGames/v1/",
                                   required=False).get("games", []),
        "badges": lambda: get_json("IPlayerService/GetBadges/v1/", required=False),
        "items": profile_items,
        "record": ban_record,
        "published": workshop_count,
        # Groups as a number and nothing else, which is all Steam publishes
        # without a request per group. GetUserGroupList answers bare 64-bit ids;
        # the profile XML, which would have been free, has no groups block at
        # all any more - measured on a profile that is in seventeen of them. The
        # names live only on each group's own memberslistxml, and seventeen
        # requests to the host cards.py is pacing is not a panel, it is an
        # outage.
        "groups": lambda: ((get_json("ISteamUser/GetUserGroupList/v1/", required=False)
                            or {}).get("groups") or []),
        # Up to three calls of its own, which is exactly why it is here rather
        # than at the bottom of the build where it used to be.
        "friends": build_friends,
        "community": community_pages,
    })

    player = (got["summaries"].get("players") or [{}])[0]
    level, percentile = got["level"]
    recent = got["recent"]
    badges = got["badges"]
    items = got["items"]
    record = got["record"]
    published = got["published"]
    group_ids = got["groups"]
    friend_list = got["friends"]
    scraped, worn = got["community"]

    played = [g for g in games if g.get("playtime_forever", 0) > 0]
    total_min = sum(g.get("playtime_forever", 0) for g in games)
    linux_min = sum(g.get("playtime_linux_forever", 0) for g in games)
    win_min = sum(g.get("playtime_windows_forever", 0) for g in games)
    mac_min = sum(g.get("playtime_mac_forever", 0) for g in games)
    deck_min = sum(g.get("playtime_deck_forever", 0) for g in games)
    # Deck is a device slice inside Steam's Linux clock, not a fourth,
    # disjoint OS bucket. Adding it here double-counts those minutes.
    attributed = linux_min + win_min + mac_min
    ranked = sorted(played, key=lambda g: -g["playtime_forever"])

    def row(g, rank):
        lp = g.get("rtime_last_played") or 0
        return {
            "appid": g["appid"],
            "name": g.get("name", f"app {g['appid']}"),
            "rank": rank,
            "hours": round(g["playtime_forever"] / 60, 1),
            "share": round(g["playtime_forever"] / total_min * 100, 2) if total_min else 0,
            "last_played": date.fromtimestamp(lp).isoformat() if lp else None,
            "linux_minutes": g.get("playtime_linux_forever", 0),
            # Per-OS and recent minutes for this one game. Cheap here, and the
            # only true thing some pages have to work with.
            "os": {
                "windows": g.get("playtime_windows_forever", 0),
                "linux": g.get("playtime_linux_forever", 0),
                "mac": g.get("playtime_mac_forever", 0),
                # Subset of linux, exposed separately as a device detail.
                "deck": g.get("playtime_deck_forever", 0),
            },
            "minutes_2weeks": g.get("playtime_2weeks", 0),
            # Whether this game has a layout of its own, so the index can say so.
            "themed": g["appid"] in GAME_LAYOUTS,
            # Three facts about the game that arrive with the library and used
            # to be dropped. Absent rather than false, because they ride on
            # every row of a library that can run to five hundred of them.
            #
            # `market` is items of the game's own on the Community Market -
            # skins, hats, keys. It is emphatically NOT "this game drops
            # trading cards": measured against the appids in a real card
            # inventory, eleven of fourteen games with cards report
            # has_market false, Portal 2 among them. Anything about cards
            # still has to ask cards.has_cards.
            **({"market": True} if g.get("has_market") else {}),
            **({"workshop": True} if g.get("has_workshop") else {}),
            **({"dlc": True} if g.get("has_dlc") else {}),
            # Steam's own sort key, which moves the article to the end the way
            # a shelf does: "Team Fortress Classic" files under "Team Fortress".
            # Absent on most games, and absent here when it is.
            **({"sort_as": g["sort_as"]} if g.get("sort_as") else {}),
        }

    library = [row(g, i + 1) for i, g in enumerate(ranked)]
    never = sorted(
        ({"appid": g["appid"], "name": g.get("name", f"app {g['appid']}")}
         for g in games if not g.get("playtime_forever", 0)),
        key=lambda g: g["name"].lower())
    created = player.get("timecreated")
    created_dt = datetime.fromtimestamp(created, timezone.utc) if created else None
    days_since = (datetime.now(timezone.utc) - created_dt).days if created_dt else None

    # No cc: the profile payload is shared by every visitor, so its economics
    # are the default storefront's. The dashboard replaces them at once with
    # /meta in the reader's own currency, which is the same thing the panel
    # already did to watch the crawl fill in.
    economics = build_economics(library, never)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "steamid": STEAM_ID,
        "profile": {
            "persona": scraped.get("persona") or player.get("personaname"),
            "url": player.get("profileurl") or PROFILE_URL,
            "avatar": player.get("avatarfull"),
            "bio": scraped.get("bio"),
            "showcase": scraped.get("showcase", []),
            "member_since": created_dt.date().isoformat() if created_dt else None,
            "days_since": days_since,
            "level": level,
            # 99.31 means "above 99.31% of accounts". None when the level is
            # unknown or Steam declined to say.
            "level_percentile": percentile,
            "groups": len(group_ids) or None,
            # The count only. The items are their own route and are never asked
            # for on a profile that published nothing, which is most of them.
            "workshop": published,
            # Where somebody says they are, and the name they chose to publish.
            # Both are blank on most profiles and are simply absent then, which
            # is why the page can draw the block only when there is one.
            "location": scraped.get("location"),
            # The same place as an ISO code, for a flag. The XML line above is
            # what the person typed; this is what Steam filed it under, and only
            # one of the two can be drawn as a flag.
            "country": player.get("loccountrycode"),
            "region": player.get("locstatecode"),
            "realname": scraped.get("realname"),
            "custom_url": scraped.get("custom_url"),
            # "online", "offline", "in-game", and the line under it that says
            # which game. A fact with a clock on it: true when the build ran and
            # stamped with generated_at like everything else here.
            "online": scraped.get("online"),
            "status": scraped.get("status"),
            # A limited account is one that has never spent the five dollars
            # Steam asks for before it will let an account do much. It is the
            # single most useful thing on a stranger's profile and it is
            # published in the XML for free.
            "limited": scraped.get("limited"),
            # A day, not a clock. Steam publishes the exact second somebody was
            # last online and printing it would be a surveillance feature on a
            # page about hours played - the same choice badge_date already makes
            # a few hundred lines up, for the same reason.
            "last_seen": (date.fromtimestamp(player["lastlogoff"]).isoformat()
                          if isinstance(player.get("lastlogoff"), int) else None),
            "badge_count": scraped.get("badges") or worn["total"]
                           or len(badges.get("badges", [])) or None,
            "xp": badges.get("player_xp"),
            "xp_to_next": badges.get("player_xp_needed_to_level_up"),
            # The badges as things rather than as a count: the newest handful
            # with their artwork, and how many there are behind them. Empty on
            # a profile that keeps its badges to itself, which is what makes
            # the panel disappear instead of standing there saying nothing.
            "badges": worn["list"],
            "friends": scraped.get("friends"),
            "screenshots": scraped.get("screenshots"),
            "reviews": scraped.get("reviews"),
            "achievements_total": scraped.get("achievements_total"),
            "perfect_games": scraped.get("perfect_games"),
            "avg_completion": scraped.get("avg_completion"),
            # What the profile is wearing. Kept under one key rather than
            # spread across three, because the page treats them as one thing:
            # either this profile is dressed or it is not.
            "items": items,
            # None on an account with nothing against it, which is most of
            # them. See ban_record() for why that is not the same as zero.
            "bans": record,
        },
        "totals": {
            "hours": round(total_min / 60),
            "days": round(total_min / 60 / 24),
            "owned": len(games),
            "played": len(played),
            "never_played": len(games) - len(played),
            "hours_per_day": round(total_min / 60 / days_since, 2) if days_since else None,
            "top_game_share": library[0]["share"] if library else 0,
            "top10_share": round(sum(g["playtime_forever"] for g in ranked[:10]) / total_min * 100)
                           if total_min else 0,
        },
        "platform": {
            "linux_hours": round(linux_min / 60),
            "windows_hours": round(win_min / 60),
            "mac_hours": round(mac_min / 60),
            "deck_hours": round(deck_min / 60),
            "attributed_hours": round(attributed / 60),
            "linux_share": round(linux_min / attributed * 100, 1) if attributed else 0,
            "unattributed_hours": round((total_min - attributed) / 60),
        },
        # Everything launched at least once, for the treemap.
        "library": library,
        # The rest of what is owned. They have no hours, no rank and no page,
        # so they carry only what a list needs - but "every game" means every
        # game, and on most accounts this is the larger half.
        "unplayed": never,
        # The top 25, which is what gets its own page.
        "top_games": library[:TABLE_ROWS],
        # What the library cost and what it is made of, out of the store cache.
        # Both are partial on a profile nobody has looked up before, and both
        # carry the coverage that says how partial. /meta re-reads them without
        # touching Steam, which is how the panels fill in while the page is open.
        "money": economics["money"],
        "genres": economics["genres"],
        "store_coverage": economics["coverage"],
        # Who this profile can be put side by side with, when Steam will say.
        "friend_list": friend_list,
        "now": {
            "playing": player.get("gameextrainfo"),
            "hours_2weeks": round(sum(g.get("playtime_2weeks", 0) for g in recent) / 60, 1),
            "games": [
                {"name": g.get("name"), "hours": round(g.get("playtime_2weeks", 0) / 60, 1)}
                for g in sorted(recent, key=lambda g: -g.get("playtime_2weeks", 0))
            ],
        },
    }


# How many of a library's card sets are queued with the market at once. A
# five-hundred-game library is five hundred requests at one every four seconds,
# and the games somebody is likely to look at are at the top of it. The rest
# arrive when a page asks for them.
CARD_QUEUE = 120


def build_cards(library, unplayed):
    """The card collection of the profile currently set: badges crafted, sets
    still open, and what those sets cost today.

    Three sources, and it is worth saying which answers what. `GetBadges` is
    the only one that knows what this account has actually made - the badge, its
    level, the day it was crafted and how many people share it. The store cache
    knows which of the owned games drop cards at all, which is what separates
    "not crafted" from "there is nothing here to craft". And cards.py knows
    what one of each card goes for, which is the number nobody has without
    opening fifteen market tabs.

    Only the first is a Steam call. The other two are reads off disk that
    queue what they did not find, so this answer improves by itself while the
    page is open and nothing here ever waits on the market."""
    badges = get_json("IPlayerService/GetBadges/v1/", required=False)
    # The Community Badge, which is the one that is a checklist rather than a
    # card set. Steam answers with quest ids and a completed flag and publishes
    # no names for them anywhere, so this can honestly be a count and nothing
    # more - "23 of 28", not a list of what is left. It is the least this whole
    # panel does, and it is one call.
    quests = (get_json("IPlayerService/GetCommunityBadgeProgress/v1/", required=False)
              or {}).get("quests") or []
    rows = badges.get("badges") or []

    owned = [(g["appid"], g.get("name") or "", g.get("hours") or 0) for g in library]
    owned += [(g["appid"], g.get("name") or "", 0) for g in unplayed]
    names = {appid: name for appid, name, _ in owned}
    hours = {appid: hour for appid, _, hour in owned}
    known = meta.lookup(appid for appid, _, _ in owned)

    crafted, seen = [], set()
    for badge in rows:
        appid = badge.get("appid")
        if not appid:
            # Steam's own badges - the years of service, the summer sales, the
            # ones for owning a Deck. They are badges and they are counted at
            # the top, but they are not a card set and there is nothing to
            # craft, buy or complete about them here.
            continue
        seen.add(appid)
        when = badge.get("completion_time") or 0
        crafted.append({
            "appid": appid,
            "name": names.get(appid) or (known.get(appid) or {}).get("name")
                    or f"app {appid}",
            "level": badge.get("level"),
            "xp": badge.get("xp"),
            # border_color 1 is the foil badge, which is the same set crafted
            # out of the other half of the drops. Steam reports it as a border
            # rather than as a kind, and it is kept as the fact it is.
            "foil": bool(badge.get("border_color")),
            "when": date.fromtimestamp(when).isoformat() if when else None,
            "scarcity": badge.get("scarcity"),
            # Whether this profile still owns the game the badge is for. A
            # badge outlives the licence, so this is genuinely a third state
            # and not a bug in the library.
            "owned": appid in names,
        })
    crafted.sort(key=lambda b: (b["when"] or "", b["appid"]), reverse=True)
    # A badge outlives the licence, so a game sold, refunded or delisted years
    # ago is a row with an appid and no name. The store cache is asked about
    # those, and the name arrives on the next read - which is better than
    # printing "app 480730" forever, and better than guessing.
    meta.want(b["appid"] for b in crafted if b["appid"] not in names)

    # What is left. `has_cards` is three-valued on purpose: a game the store
    # cache has not reached yet is not a game without cards, and counting it as
    # one would print a total that shrinks as the crawl catches up.
    open_sets, unclassified = [], 0
    for appid, name, _ in owned:
        if appid in seen:
            continue
        answer = cards.has_cards((known.get(appid) or {}).get("catalog"))
        if answer is None:
            unclassified += 1
            continue
        if answer:
            open_sets.append({"appid": appid, "name": name or f"app {appid}",
                              "hours": hours.get(appid) or 0})
    open_sets.sort(key=lambda g: (-(g["hours"] or 0), g["name"].lower()))

    # The market is asked about the ones somebody is most likely to read, and
    # the answer for the rest arrives the first time one of their pages is
    # opened. Crafted sets are queued too: the page prints what the set that is
    # already made would cost today, which is the only figure that says what a
    # badge was worth making.
    cards.want([g["appid"] for g in open_sets[:CARD_QUEUE]]
               + [b["appid"] for b in crafted[:CARD_QUEUE]])
    sets = cards.known([g["appid"] for g in open_sets] + [b["appid"] for b in crafted])

    def priced(row):
        got = sets.get(row["appid"])
        if not got or got["state"] in ("unknown", "none"):
            return row
        row["cost"] = got["cost"]
        row["count"] = got["count"]
        row["stale"] = got["stale"]
        return row

    open_sets = [priced(g) for g in open_sets]
    crafted = [priced(b) for b in crafted]

    # What finishing the open sets would cost, over the ones there is a price
    # for. Printed with its own coverage rather than as a round number: half a
    # library's worth of sets is a real answer and pretending it is the whole
    # library is not.
    quoted = [g["cost"] for g in open_sets if g.get("cost")]
    # One rate for the whole answer rather than one per row: it is a fact about
    # the day, not about a game, and the page multiplies with it where it needs
    # to. None when there is no fresh one, and the page then prints dollars
    # alone - see fx.py for why an old rate is worse than no rate.
    rate = fx.quote()
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "steamid": STEAM_ID,
        "level": badges.get("player_level"),
        "xp": badges.get("player_xp"),
        "xp_to_next": badges.get("player_xp_needed_to_level_up"),
        "community_badge": ({"done": sum(1 for q in quests if q.get("completed")),
                             "total": len(quests)} if quests else None),
        "badges": {
            "total": len(rows),
            "game": len(crafted),
            "other": len(rows) - len(crafted),
            "xp": sum(b.get("xp") or 0 for b in rows),
        },
        "crafted": crafted,
        "open": open_sets,
        "currency": "USD",
        "rates": rate["rates"] if rate else None,
        "rates_at": rate["at"] if rate else None,
        "cost": {
            "open": sum(quoted) if quoted else None,
            "quoted": len(quoted),
            "sets": len(open_sets),
        },
        # Two different kinds of "not yet". `unclassified` is the store cache
        # still filling; the sets without a cost are the market still filling.
        "filling": {
            "unclassified": unclassified,
            "unpriced": len(open_sets) - len(quoted),
        },
    }


def build_owner():
    """The footer credit: the owner's persona as Steam shows it today, plus the
    avatar. Small enough to cache hard and cheap enough to refresh often."""
    set_user(OWNER_ID, OWNER_VANITY)
    summaries = get_json("ISteamUser/GetPlayerSummaries/v2/", steamids=OWNER_ID,
                         with_steamid=False, required=False)
    player = (summaries.get("players") or [{}])[0]
    return {
        "steamid": OWNER_ID,
        "persona": player.get("personaname") or OWNER_VANITY,
        "avatar": player.get("avatar") or player.get("avatarfull"),
        "url": f"/u/{OWNER_VANITY}",
    }


def main():
    """Debug helper: dump a profile (and optionally one game) as JSON."""
    args = sys.argv[1:]
    query = args[0] if args else OWNER_VANITY
    sid, vanity = resolve(query)
    if not sid:
        raise SystemExit(f"não consegui resolver {query!r}")
    set_user(sid, vanity)
    if len(args) > 1:
        appid = int(args[1])
        profile = build_profile()
        row = next((g for g in profile["library"] if g["appid"] == appid), None)
        if row is None:
            raise SystemExit(f"appid {appid} não está na biblioteca desse perfil")
        data = build_game(appid, row)
    else:
        data = build_profile()
    print(json.dumps(data, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
