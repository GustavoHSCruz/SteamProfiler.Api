#!/usr/bin/env python3
"""steamprofiler.org - the pieces of a profile that are meant to leave it.

og.py draws one picture for one purpose: the link preview, at the one size every
scraper wants, as a PNG assembled by hand. This is the other half of that idea.
Somebody who wants their hours on a blog, in a forum signature, in a README or
in their own Steam profile needs a shape this site does not have a page for, and
needs it in a format that survives being pasted somewhere we do not control.

So everything here is SVG, and every SVG is self-contained: no stylesheet, no
webfont, no <image> pointing at a CDN. A file that fetches anything at render
time is a file that renders differently, or not at all, depending on where it
was pasted - GitHub proxies images and blocks the rest, forums strip what they
do not recognise, and a browser drawing an SVG into a canvas to save it as a
PNG refuses the moment a foreign pixel is involved. Key art and avatars are
therefore fetched here, cached on disk by art.py, and travel as data URIs.

Three families, because that is what people actually ask for:

    bars      one row per game, platform, genre or year
    banner    a strip for a blog or a forum, upright or across
    badge     the shields.io shape, one label and one figure

Every one of them answers twice. The same function serves the dynamic URL, which
re-reads the profile every quarter of an hour, and the static download, which is
those same bytes frozen the moment they were asked for. The only place that
distinction is a real limitation is Steam itself, whose About Me will not load an
image from here: there the file has to be uploaded as artwork, and what is on the
profile is a snapshot with the date it was taken printed on it.

The text output is not an image at all. A Unicode bar chart is what fits a place
that takes no pictures: a Steam About Me, a Discord message, a plain README.
"""

import base64
import re
import time
import unicodedata
from math import ceil

import art

# ── Paint ────────────────────────────────────────────────────────────
# The site's own palette, plus two the site does not use. `light` is for a page
# with a white background, which is most blogs; `steam` is Steam's own blue, for
# something that will sit on a Steam profile and should look like it belongs.
THEMES = {
    "dark": {
        "bg": "#0b0a0e", "panel": "#131219", "line": "#282631",
        "text": "#eeecf3", "dim": "#8e8a9b", "accent": "#ffb454",
        "rest": "#26232f",
    },
    "light": {
        "bg": "#ffffff", "panel": "#f4f2f7", "line": "#dcd8e4",
        "text": "#1b1a20", "dim": "#6b6779", "accent": "#c97f22",
        "rest": "#e6e2ec",
    },
    "steam": {
        "bg": "#1b2838", "panel": "#16202d", "line": "#2a475e",
        "text": "#c7d5e0", "dim": "#8f98a0", "accent": "#66c0f4",
        "rest": "#233447",
    },
}

# Verdana first, DejaVu Sans behind it: the same pair shields.io settled on,
# for the same reason. Those two are on nearly every machine that will ever
# open one of these, and the width table below is Verdana's - a fallback that
# is narrower than the measurement only ever ends a line early, which is safe,
# while a wider one would run out of its box.
FONT = "Verdana,DejaVu Sans,Geneva,sans-serif"

# Verdana advance widths, in fractions of the font size. Only the characters a
# label can hold; anything else is charged the default, which is the average of
# a lowercase letter and a digit.
WIDTHS = {
    " ": .35, "!": .32, '"': .46, "#": .82, "$": .64, "%": 1.0, "&": .75,
    "'": .27, "(": .45, ")": .45, "*": .55, "+": .82, ",": .32, "-": .42,
    ".": .32, "/": .45, ":": .34, ";": .34, "<": .82, "=": .82, ">": .82,
    "?": .54, "@": 1.0, "[": .45, "]": .45, "_": .55, "`": .55, "|": .45,
    "0": .64, "1": .64, "2": .64, "3": .64, "4": .64, "5": .64, "6": .64,
    "7": .64, "8": .64, "9": .64,
    "A": .68, "B": .69, "C": .70, "D": .77, "E": .63, "F": .58, "G": .78,
    "H": .75, "I": .42, "J": .46, "K": .69, "L": .56, "M": .84, "N": .75,
    "O": .79, "P": .60, "Q": .79, "R": .70, "S": .68, "T": .62, "U": .73,
    "V": .68, "W": 1.0, "X": .68, "Y": .62, "Z": .63,
    "a": .60, "b": .62, "c": .52, "d": .62, "e": .60, "f": .35, "g": .62,
    "h": .63, "i": .27, "j": .34, "k": .59, "l": .27, "m": .97, "n": .63,
    "o": .61, "p": .62, "q": .62, "r": .43, "s": .52, "t": .39, "u": .63,
    "v": .59, "w": .82, "x": .59, "y": .59, "z": .53,
}
DEFAULT_W = .60


def text_width(s, size):
    """About how wide `s` will draw. Used to size a badge and to trim a label,
    which are the two things that go wrong when a guess is too low."""
    return sum(WIDTHS.get(ch, DEFAULT_W) for ch in s) * size


def esc(s):
    """XML text. Everything here ends up between two tags or inside one
    attribute, so five characters have to stop being themselves."""
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("'", "&apos;"))


def clip(s, size, budget, ellipsis="…"):
    """`s`, trimmed until it fits `budget` pixels at `size`.

    Trimmed by measured width and never by a count of characters: "Grand Theft
    Auto: San Andreas" and "IIIIIIIIIIIIIIIIIIIIIII" are the same number of
    letters and nothing like the same number of pixels."""
    s = " ".join(str(s or "").split())
    if text_width(s, size) <= budget:
        return s
    tail = text_width(ellipsis, size)
    out = ""
    for ch in s:
        if text_width(out + ch, size) + tail > budget:
            break
        out += ch
    return out.rstrip() + ellipsis


def plain(s):
    """A label with its combining marks folded away.

    Unlike og.py this can print any character the reader's font has, so nothing
    is dropped - but a name is measured against a Verdana table, and Verdana is
    not what draws Cyrillic or CJK. Folding the accents keeps the common case
    (Portuguese, French, German titles) measured by the table that was actually
    built for it, and leaves everything else alone."""
    raw = unicodedata.normalize("NFKD", str(s or ""))
    kept = "".join(ch for ch in raw if not unicodedata.combining(ch))
    return " ".join(kept.split())


# ── Words ────────────────────────────────────────────────────────────
# An image carries its own text, so unlike every other route on this service
# these strings cannot be left to the browser. Only the handful a chart needs.
WORDS = {
    "en": {
        "hours": "hours", "h": "h", "games": "games", "played": "played",
        "owned": "owned", "never": "never opened", "level": "level",
        "top": "most played", "library": "library", "genres": "genres",
        "years": "last opened in", "platform": "platform", "recent": "last two weeks",
        "windows": "Windows", "linux": "Linux", "mac": "macOS", "deck": "Steam Deck",
        "since": "on Steam since", "per_day": "hours a day", "snapshot": "snapshot of",
        "achievements": "achievements", "badges": "badges", "now": "playing now",
        "nothing": "nothing yet", "of_it": "of the clock",
    },
    "pt": {
        "hours": "horas", "h": "h", "games": "jogos", "played": "jogados",
        "owned": "na conta", "never": "nunca abertos", "level": "nivel",
        "top": "mais jogado", "library": "biblioteca", "genres": "generos",
        "years": "abertos por ultimo em", "platform": "plataforma",
        "recent": "duas ultimas semanas",
        "windows": "Windows", "linux": "Linux", "mac": "macOS", "deck": "Steam Deck",
        "since": "na Steam desde", "per_day": "horas por dia", "snapshot": "retrato de",
        "achievements": "conquistas", "badges": "insignias", "now": "jogando agora",
        "nothing": "nada ainda", "of_it": "do relogio",
    },
    "ru": {
        "hours": "часов", "h": "ч", "games": "игр", "played": "запущено",
        "owned": "в аккаунте", "never": "не открыто", "level": "уровень",
        "top": "больше всего", "library": "библиотека", "genres": "жанры",
        "years": "последний запуск", "platform": "платформа",
        "recent": "две недели",
        "windows": "Windows", "linux": "Linux", "mac": "macOS", "deck": "Steam Deck",
        "since": "в Steam с", "per_day": "часов в день", "snapshot": "снимок",
        "achievements": "достижений", "badges": "значков", "now": "играет",
        "nothing": "пока ничего", "of_it": "от всего",
    },
}


def words(lang):
    return WORDS.get(lang) or WORDS["en"]


def group(n, lang):
    """A thousands separator the reader will recognise. Portuguese groups with a
    dot, Russian with a space, English with a comma."""
    sep = {"pt": ".", "ru": " "}.get(lang, ",")
    return f"{int(n):,}".replace(",", sep)


def hours_text(n, lang):
    """Hours, at the precision the figure deserves: a game with four hours on it
    is 4.2, a library with four thousand is 4,183 and the tenth is noise."""
    n = n or 0
    if n < 10:
        out = f"{round(n, 1)}".rstrip("0").rstrip(".")
        if lang == "pt":
            out = out.replace(".", ",")
        return out
    return group(round(n), lang)


# ── Small marks ──────────────────────────────────────────────────────
# Drawn from primitives on a 16 grid rather than written as path data, because
# what these have to be is recognisable at 14 pixels and editable by hand. Each
# entry is a list of shapes: a filled rect, a filled circle, or a stroked path.
GLYPHS = {
    "pad": [("rect", 1, 5, 14, 7, 3.4), ("hole", 4.6, 8.5, 1.5),
            ("hole", 11.4, 8.5, 1.5)],
    "windows": [("rect", 1, 1, 6, 6, .6), ("rect", 9, 1, 6, 6, .6),
                ("rect", 1, 9, 6, 6, .6), ("rect", 9, 9, 6, 6, .6)],
    # A stubby standing bird. Not a penguin anyone would recognise on its own,
    # which is why it only ever appears on a row that says Linux beside it.
    "linux": [("circle", 8, 5, 3.2), ("rect", 4.6, 7.4, 6.8, 7.6, 3.2),
              ("rect", 3.2, 13.4, 3, 1.8, .9), ("rect", 9.8, 13.4, 3, 1.8, .9)],
    "mac": [("circle", 8, 9.4, 4.6), ("rect", 7.3, 1.6, 1.5, 3.2, .7)],
    "deck": [("rect", 2, 4, 12, 9, 3), ("hole", 5.2, 8.5, 1.6),
             ("hole", 10.8, 8.5, 1.6)],
    "calendar": [("rect", 1.4, 3, 13.2, 11.6, 1.6), ("rect", 4, 1, 1.6, 3.2, .8),
                 ("rect", 10.4, 1, 1.6, 3.2, .8)],
    "tag": [("stroke", "M2.5 2.5h6.4l5 5-6.4 6.4-5-5z", 1.6),
            ("circle", 5.2, 5.2, 1.1)],
    "clock": [("stroke", "M8 1.6a6.4 6.4 0 1 0 0 12.8 6.4 6.4 0 1 0 0-12.8", 1.6),
              ("stroke", "M8 4.4V8.6l3 1.8", 1.6)],
}
# What each series puts in front of a row when the icon style is asked for.
SERIES_GLYPH = {"top": "pad", "recent": "clock", "genres": "tag", "years": "calendar"}


def glyph(name, x, y, size, colour, bg):
    """One mark, scaled from the 16 grid onto `size` pixels at (x, y).

    A hole is a circle in the background colour rather than a cut-out path: two
    of these are the difference between a rounded rectangle and a thing with
    thumbsticks on it, and a mask would cost more than it buys at 15 pixels."""
    shapes = GLYPHS.get(name)
    if not shapes:
        return ""
    k = size / 16
    out = [f'<g transform="translate({x:.1f} {y:.1f}) scale({k:.4f})" fill="{colour}">']
    for shape in shapes:
        if shape[0] == "rect":
            _, sx, sy, w, h, r = shape
            out.append(f'<rect x="{sx}" y="{sy}" width="{w}" height="{h}" rx="{r}"/>')
        elif shape[0] in ("circle", "hole"):
            _, cx, cy, r = shape
            paint = f' fill="{bg}"' if shape[0] == "hole" else ""
            out.append(f'<circle cx="{cx}" cy="{cy}" r="{r}"{paint}/>')
        else:
            _, d, width = shape
            out.append(f'<path d="{d}" fill="none" stroke="{colour}" '
                       f'stroke-width="{width}" stroke-linejoin="round" '
                       f'stroke-linecap="round"/>')
    out.append("</g>")
    return "".join(out)


# The site's own mark, the same three rectangles as favicon.svg, for the badge
# that wants a logo. Valve's mark is deliberately not shipped here: it is not
# ours to hand out, and a badge about somebody's Steam hours does not become
# clearer by wearing the logo of a company that had no part in making it.
def mark(x, y, size, colour):
    """The three rectangles, in one colour at three strengths. One colour and
    not two, so the same call works in amber on a banner and in white on a
    badge, where a second tone would only ever be a smudge."""
    k = size / 48
    return (f'<g transform="translate({x:.1f} {y:.1f}) scale({k:.4f})" '
            f'fill="{colour}">'
            f'<rect x="0" y="0" width="20" height="36" rx="2"/>'
            f'<rect x="22" y="0" width="14" height="20" rx="2" opacity=".72"/>'
            f'<rect x="22" y="22" width="14" height="14" rx="2" opacity=".42"/>'
            f'</g>')


# ── Colour ramps ─────────────────────────────────────────────────────

def hsl_hex(h, s, l):
    """HSL to #rrggbb, so the coloured chart can walk a hue without shipping a
    palette of twelve hand-picked values that only work on one background."""
    h = (h % 360) / 360.0
    c = (1 - abs(2 * l - 1)) * s
    x = c * (1 - abs((h * 6) % 2 - 1))
    m = l - c / 2
    seg = int(h * 6) % 6
    rgb = [(c, x, 0), (x, c, 0), (0, c, x), (0, x, c), (x, 0, c), (c, 0, x)][seg]
    return "#" + "".join(f"{round((v + m) * 255):02x}" for v in rgb)


def ramp(i, n, theme):
    """The colour of row `i`. Amber through to violet on a dark background, and
    the same walk at a lower lightness on a light one, so a chart pasted into a
    white page is not five pastels nobody can tell apart."""
    if n <= 1:
        return theme["accent"]
    frac = i / (n - 1)
    light = .40 if theme["bg"] == "#ffffff" else .62
    return hsl_hex(36 - frac * 300, .72, light)


# ── The series ───────────────────────────────────────────────────────

def series(profile, show, n, lang):
    """`(title, rows)` for one of the five things a chart here can be about.

    A row is a label, a number, the text of that number, and optionally the
    appid behind it (which is what lets the art style find a picture) and the
    name of a mark (which is what the icon style draws)."""
    w = words(lang)
    library = profile.get("library") or []
    rows = []

    if show == "platform":
        # Steam attributes a game's clock per operating system, but only from
        # whenever it started doing so - which is why the page this comes from
        # prints the unattributed remainder too. A chart of four bars cannot
        # carry that footnote, so it is the one series that says `platform`
        # rather than claiming to be the whole library.
        p = profile.get("platform") or {}
        for key, glyph_name in (("windows", "windows"), ("linux", "linux"),
                                ("mac", "mac"), ("deck", "deck")):
            value = p.get(f"{key}_hours") or 0
            if value:
                rows.append({"label": w[key], "value": value,
                             "text": f"{hours_text(value, lang)} {w['h']}",
                             "glyph": glyph_name})
        rows.sort(key=lambda r: -r["value"])
        return w["platform"], rows[:n]

    if show == "genres":
        for g in (profile.get("genres") or [])[:n]:
            value = g.get("hours") or 0
            rows.append({"label": plain(g.get("name")), "value": value,
                         "text": f"{hours_text(value, lang)} {w['h']}",
                         "glyph": "tag"})
        return w["genres"], rows

    if show == "years":
        # Steam publishes one date per game, the last time it was launched. So a
        # year here holds the lifetime hours of the games whose last launch fell
        # in it, which is not the same thing as hours played that year - the
        # label says "last opened in" for exactly that reason.
        buckets = {}
        for g in library:
            year = (g.get("last_played") or "")[:4]
            if year:
                buckets[year] = buckets.get(year, 0) + (g.get("hours") or 0)
        for year in sorted(buckets, reverse=True)[:n]:
            rows.append({"label": year, "value": buckets[year],
                         "text": f"{hours_text(buckets[year], lang)} {w['h']}",
                         "glyph": "calendar"})
        return w["years"], rows

    if show == "recent":
        # Sorted by the key and not by the pair: two games with the same two
        # weeks on them would otherwise be compared as dictionaries, which
        # raises rather than tying.
        recent = sorted(library, key=lambda g: -(g.get("minutes_2weeks") or 0))
        for g in recent[:n]:
            value = (g.get("minutes_2weeks") or 0) / 60
            if value <= 0:
                continue
            rows.append({"label": plain(g.get("name")), "value": value,
                         "text": f"{hours_text(value, lang)} {w['h']}",
                         "appid": g.get("appid"), "glyph": "clock"})
        return w["recent"], rows

    for g in library[:n]:
        value = g.get("hours") or 0
        rows.append({"label": plain(g.get("name")), "value": value,
                     "text": f"{hours_text(value, lang)} {w['h']}",
                     "appid": g.get("appid"), "glyph": "pad"})
    return w["top"], rows


# ── The picture frame ────────────────────────────────────────────────

def open_svg(w, h, label):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
            f'viewBox="0 0 {w} {h}" role="img" aria-label="{esc(label)}">')


def stamp(profile):
    """The date the numbers were read, as YYYY-MM-DD.

    On the dynamic URL this is close to now and says so. On a download it is the
    whole point: a picture on a Steam profile is a picture of one afternoon, and
    it should be possible to see which afternoon without asking anybody."""
    at = (profile.get("generated_at") or "")[:10]
    return at or time.strftime("%Y-%m-%d", time.gmtime())


def data_uri(blob):
    return "data:image/jpeg;base64," + base64.b64encode(blob).decode("ascii")


# A capsule is 10 to 20 KB. Anything past this is not a capsule, and inlining it
# would put a megabyte of base64 into a file somebody pastes into a README.
MAX_PICTURE = 90 * 1024


def picture(getter, *args):
    """A data URI for one cached JPEG, or None. Never raises: a chart with a
    missing thumbnail is a chart, and a chart that failed is nothing."""
    try:
        blob = getter(*args)
    except Exception:
        return None
    if not blob or len(blob) > MAX_PICTURE:
        return None
    return data_uri(blob)


# ── Bars ─────────────────────────────────────────────────────────────

def bars(profile, o):
    """One row per game, platform, genre or year.

    Four styles, and the difference between them is only what sits in front of
    the bar: nothing, a hue per row, a mark, or the game's own capsule."""
    theme = THEMES[o["theme"]]
    lang, w = o["lang"], words(o["lang"])
    style = o["style"]
    width = o["w"]
    title, rows = series(profile, o["show"], o["n"], lang)
    persona = plain((profile.get("profile") or {}).get("persona") or "")
    total = (profile.get("totals") or {}).get("hours") or 0

    pad = 14
    art_w, art_h = 74, 28          # 231x87 scaled down, the same ratio
    row_h = 40 if style == "art" else 30
    head_h = 52 if o["head"] else 10
    foot_h = 26 if o["foot"] else 8
    height = head_h + max(1, len(rows)) * row_h + foot_h
    inner = width - pad * 2

    out = [open_svg(width, height,
                    f"{persona}: {title}, steamprofiler.org")]
    out.append(f'<rect width="{width}" height="{height}" rx="10" '
               f'fill="{theme["bg"]}" stroke="{theme["line"]}"/>')
    if style == "art":
        # One clip for every thumbnail, since they are all the same size. A
        # rounded corner on a picture is the difference between a row of
        # screenshots and a row of covers.
        out.append(f'<defs><clipPath id="c"><rect width="{art_w}" '
                   f'height="{art_h}" rx="3"/></clipPath></defs>')

    if o["head"]:
        out.append(f'<text x="{pad}" y="26" font-family="{FONT}" font-size="15" '
                   f'font-weight="bold" fill="{theme["text"]}">'
                   f'{esc(clip(persona, 15, inner - 140))}</text>')
        right = f'{group(round(total), lang)} {w["h"]} · {w["library"]}'
        out.append(f'<text x="{width - pad}" y="25" text-anchor="end" '
                   f'font-family="{FONT}" font-size="11" fill="{theme["dim"]}">'
                   f'{esc(right)}</text>')
        out.append(f'<text x="{pad}" y="43" font-family="{FONT}" font-size="10" '
                   f'letter-spacing="1.4" fill="{theme["accent"]}">'
                   f'{esc(title.upper())}</text>')
        out.append(f'<rect x="{pad}" y="{head_h - 2}" width="{inner}" height="1" '
                   f'fill="{theme["line"]}"/>')

    if not rows:
        out.append(f'<text x="{pad}" y="{head_h + 20}" font-family="{FONT}" '
                   f'font-size="12" fill="{theme["dim"]}">{esc(w["nothing"])}</text>')

    top = max((r["value"] for r in rows), default=1) or 1
    for i, r in enumerate(rows):
        y = head_h + i * row_h
        colour = ramp(i, len(rows), theme) if style == "color" else theme["accent"]
        x = pad
        if style == "icon":
            out.append(glyph(r.get("glyph") or "pad", pad, y + 5, 15, colour,
                             theme["bg"]))
            x = pad + 21
        elif style == "art":
            uri = picture(art.thumb, r["appid"]) if r.get("appid") else None
            if uri:
                out.append(f'<g transform="translate({pad} {y + 3})" '
                           f'clip-path="url(#c)">'
                           f'<image width="{art_w}" height="{art_h}" '
                           f'href="{uri}" preserveAspectRatio="xMidYMid slice"/>'
                           f'</g>')
            else:
                out.append(f'<rect x="{pad}" y="{y + 3}" width="{art_w}" '
                           f'height="{art_h}" rx="3" fill="{theme["panel"]}"/>')
            x = pad + art_w + 10

        value_w = text_width(r["text"], 11)
        label_w = width - pad - x - value_w - 12
        out.append(f'<text x="{x}" y="{y + 14}" font-family="{FONT}" '
                   f'font-size="11" fill="{theme["text"]}">'
                   f'{esc(clip(r["label"], 11, label_w))}</text>')
        out.append(f'<text x="{width - pad}" y="{y + 14}" text-anchor="end" '
                   f'font-family="{FONT}" font-size="11" fill="{theme["dim"]}">'
                   f'{esc(r["text"])}</text>')
        # The bar runs under both of them and is measured against the biggest
        # row, not against the library: a chart of five games is a comparison
        # between those five.
        track = width - pad - x
        fill = max(2.0, track * (r["value"] / top))
        out.append(f'<rect x="{x}" y="{y + 20}" width="{track:.1f}" height="6" '
                   f'rx="3" fill="{theme["rest"]}"/>')
        out.append(f'<rect x="{x}" y="{y + 20}" width="{fill:.1f}" height="6" '
                   f'rx="3" fill="{colour}"/>')

    if o["foot"]:
        base = height - 9
        out.append(f'<text x="{pad}" y="{base}" font-family="{FONT}" '
                   f'font-size="9" fill="{theme["dim"]}">steamprofiler.org</text>')
        out.append(f'<text x="{width - pad}" y="{base}" text-anchor="end" '
                   f'font-family="{FONT}" font-size="9" fill="{theme["dim"]}">'
                   f'{esc(stamp(profile))}</text>')
    out.append("</svg>")
    return "".join(out)


# ── Bars, as text ────────────────────────────────────────────────────
# For the places that take no picture at all: a Steam About Me, a Discord
# message, a plain-text README, a signature on a board that strips images.

FULL, EMPTY = "█", "░"
# The eighths, for the cell the bar ends in the middle of. Without them a
# twenty-cell chart can only say five percent at a time.
PARTIAL = ("", "▏", "▎", "▍", "▌", "▋", "▊", "▉")


def text_bars(profile, o):
    lang, w = o["lang"], words(o["lang"])
    title, rows = series(profile, o["show"], o["n"], lang)
    persona = (profile.get("profile") or {}).get("persona") or ""
    cells = o["cells"]
    if not rows:
        return f"{persona} - {w['nothing']} - steamprofiler.org\n"

    # Padded to the widest label so the bars line up. That only holds in a
    # monospaced box, which is what every place this is for gives it: a code
    # fence, a [code] block, the About Me of a Steam profile.
    label_w = min(o["label_w"], max(len(r["label"]) for r in rows))
    value_w = max(len(r["text"]) for r in rows)
    top = max(r["value"] for r in rows) or 1

    lines = [f"{persona} · {title} · steamprofiler.org"]
    for r in rows:
        label = r["label"]
        label = label if len(label) <= label_w else label[:label_w - 1].rstrip() + "…"
        exact = cells * (r["value"] / top)
        whole = int(exact)
        rest = PARTIAL[int((exact - whole) * 8)]
        # A row with hours on it never draws as an empty bar. macOS against a
        # Windows clock is four hundredths of a cell, and a line of nothing
        # reads as "no hours" when the figure beside it says twelve.
        if not whole and not rest and r["value"] > 0:
            rest = PARTIAL[1]
        bar = (FULL * whole + rest).ljust(cells, EMPTY)[:cells]
        lines.append(f"{label.ljust(label_w)}  {bar}  {r['text'].rjust(value_w)}")
    lines.append(f"{stamp(profile)}")
    return "\n".join(lines) + "\n"


# ── Banners ──────────────────────────────────────────────────────────
# Fixed sizes, because that is how the places these go to are built: a blog
# header, a forum signature, and the three ad slots every layout on the web
# still has a hole for.
PRESETS = {
    "blog":  (760, 160, "h"),
    "forum": (520, 120, "h"),
    "wide":  (728, 90, "h"),
    "card":  (300, 250, "v"),
    "tower": (300, 600, "v"),
    "side":  (240, 400, "v"),
}


def facts(profile, lang, keep=3):
    """The figures a banner puts in its own boxes, in the order they matter."""
    w = words(lang)
    totals = profile.get("totals") or {}
    who = profile.get("profile") or {}
    out = [(group(round(totals.get("hours") or 0), lang), w["hours"]),
           (group(totals.get("owned") or 0, lang), w["games"]),
           (group(totals.get("played") or 0, lang), w["played"])]
    if who.get("level") is not None:
        out.append((group(who["level"], lang), w["level"]))
    return out[:keep]


def avatar_uri(profile):
    url = (profile.get("profile") or {}).get("avatar")
    return picture(art.avatar, url) if url else None


def face(profile, x, y, side, theme):
    """The avatar, or the site's mark on a panel when Steam has no picture or
    the fetch did not come back."""
    uri = avatar_uri(profile)
    if uri:
        return (f'<g><clipPath id="f"><rect x="{x:.0f}" y="{y:.0f}" '
                f'width="{side:.0f}" height="{side:.0f}" rx="6"/></clipPath>'
                f'<image x="{x:.0f}" y="{y:.0f}" width="{side:.0f}" '
                f'height="{side:.0f}" href="{uri}" clip-path="url(#f)" '
                f'preserveAspectRatio="xMidYMid slice"/></g>')
    return (f'<rect x="{x:.0f}" y="{y:.0f}" width="{side:.0f}" height="{side:.0f}" '
            f'rx="6" fill="{theme["panel"]}"/>'
            + mark(x + side * .28, y + side * .28, side * .44, theme["accent"]))


def stat(x, y, value, label, size, theme, anchor="start"):
    return (f'<text x="{x:.0f}" y="{y:.0f}" text-anchor="{anchor}" '
            f'font-family="{FONT}" font-size="{size}" font-weight="bold" '
            f'fill="{theme["accent"]}">{esc(value)}</text>'
            f'<text x="{x:.0f}" y="{y + 12:.0f}" text-anchor="{anchor}" '
            f'font-family="{FONT}" font-size="9" letter-spacing=".6" '
            f'fill="{theme["dim"]}">{esc(label.upper())}</text>')


def banner(profile, o):
    width, height, shape = PRESETS[o["preset"]]
    theme = THEMES[o["theme"]]
    lang, w = o["lang"], words(o["lang"])
    who = profile.get("profile") or {}
    persona = plain(who.get("persona") or "")
    library = profile.get("library") or []
    top_game = library[0] if library else None

    out = [open_svg(width, height, f"{persona} on Steam, steamprofiler.org")]
    out.append(f'<rect width="{width}" height="{height}" rx="10" '
               f'fill="{theme["bg"]}" stroke="{theme["line"]}"/>')
    # One amber edge, which is the only ornament any of these get. It is what
    # makes a strip read as a strip and not as a box that lost its page.
    out.append(f'<rect x="10" width="{width - 20}" height="3" rx="1.5" '
               f'fill="{theme["accent"]}"/>')

    if shape == "h":
        pad = 12 if height < 110 else 16
        side = min(height - pad * 2 - 4, 120)
        out.append(face(profile, pad, (height - side) / 2 + 2, side, theme))
        left = pad + side + 14

        keep = 2 if width < 600 else 3
        cells = facts(profile, lang, keep)
        size = 20 if height >= 140 else 17
        # Laid out from the right edge inwards, each box as wide as the wider of
        # its two lines. Anything left over is the name's, which is the one
        # thing here that can be trimmed without losing a figure.
        column = max(max(text_width(v, size), text_width(l.upper(), 9) + 3)
                     for v, l in cells) + 22
        boxes, x = [], width - pad
        for value, label in reversed(cells):
            boxes.append((x, value, label))
            x -= column
        base = height / 2 + (8 if height < 110 else 6)
        for bx, value, label in boxes:
            out.append(stat(bx, base, value, label, size, theme, anchor="end"))
        room = x - left - 6

        name_size = 21 if height >= 140 else 17
        out.append(f'<text x="{left}" y="{height / 2 - (14 if height >= 110 else 8):.0f}" '
                   f'font-family="{FONT}" font-size="{name_size}" font-weight="bold" '
                   f'fill="{theme["text"]}">{esc(clip(persona, name_size, room))}</text>')
        line = []
        if who.get("level") is not None and len(cells) < 4:
            line.append(f'{w["level"]} {who["level"]}')
        if who.get("member_since"):
            line.append(f'{w["since"]} {who["member_since"][:4]}')
        if line:
            out.append(f'<text x="{left}" y="{height / 2 + 2:.0f}" '
                       f'font-family="{FONT}" font-size="11" fill="{theme["dim"]}">'
                       f'{esc(clip(" · ".join(line), 11, room))}</text>')
        if top_game and height >= 110:
            label = f'{w["top"]}: {plain(top_game.get("name"))} · ' \
                    f'{hours_text(top_game.get("hours"), lang)} {w["h"]}'
            out.append(f'<text x="{left}" y="{height / 2 + 22:.0f}" '
                       f'font-family="{FONT}" font-size="11" fill="{theme["text"]}">'
                       f'{esc(clip(label, 11, room))}</text>')
        out.append(f'<text x="{left}" y="{height - pad + 2:.0f}" '
                   f'font-family="{FONT}" font-size="9" fill="{theme["dim"]}">'
                   f'steamprofiler.org · {esc(stamp(profile))}</text>')
        out.append("</svg>")
        return "".join(out)

    # Upright. The same facts stacked, and every piece below the name is drawn
    # only if what is left of the height can hold it. A 300x250 rectangle and a
    # 300x600 skyscraper are the same layout with a different number of things
    # in it, so the height is spent from the top down and whatever does not fit
    # is simply not drawn - a box that overflows is worse than a box with two
    # figures in it instead of three.
    pad, foot = 14, 26
    mid = width / 2
    side = min(width - pad * 2, max(52, int(height * .26)), 112)
    y = pad + 6
    out.append(face(profile, mid - side / 2, y, side, theme))
    y += side + 22
    out.append(f'<text x="{mid:.0f}" y="{y:.0f}" text-anchor="middle" '
               f'font-family="{FONT}" font-size="17" font-weight="bold" '
               f'fill="{theme["text"]}">{esc(clip(persona, 17, width - pad * 2))}</text>')
    y += 8
    sub = []
    if who.get("level") is not None:
        sub.append(f'{w["level"]} {who["level"]}')
    if who.get("member_since"):
        sub.append(who["member_since"][:4])
    if sub and height - y - foot > 60:
        y += 8
        out.append(f'<text x="{mid:.0f}" y="{y:.0f}" text-anchor="middle" '
                   f'font-family="{FONT}" font-size="10" fill="{theme["dim"]}">'
                   f'{esc(" · ".join(sub))}</text>')
    y += 26

    # Shorter boxes on a short banner. A 300x250 rectangle holds two figures at
    # this height and one at the taller one, and two is the difference between a
    # card and a card with a hole under it.
    box, step, figure = (34, 42, 16) if height >= 300 else (28, 34, 14)
    fits = max(0, min(3, int((height - y - foot) // step)))
    for value, label in facts(profile, lang, fits):
        out.append(f'<rect x="{pad}" y="{y - box / 2 + 2:.0f}" '
                   f'width="{width - pad * 2}" height="{box}" rx="6" '
                   f'fill="{theme["panel"]}"/>')
        out.append(f'<text x="{pad + 10}" y="{y + 6:.0f}" font-family="{FONT}" '
                   f'font-size="9" letter-spacing=".6" fill="{theme["dim"]}">'
                   f'{esc(label.upper())}</text>')
        out.append(f'<text x="{width - pad - 10}" y="{y + 7:.0f}" text-anchor="end" '
                   f'font-family="{FONT}" font-size="{figure}" font-weight="bold" '
                   f'fill="{theme["accent"]}">{esc(value)}</text>')
        y += step

    # Whatever height is left, in rows of one game each, and only whole rows.
    if height - y - foot >= 40 and library:
        out.append(f'<text x="{pad}" y="{y:.0f}" font-family="{FONT}" '
                   f'font-size="9" letter-spacing="1.2" fill="{theme["accent"]}">'
                   f'{esc(w["top"].upper())}</text>')
        y += 14
        rows = library[:int((height - y - foot) // 26)]
        top = max((g.get("hours") or 0 for g in rows), default=1) or 1
        for g in rows:
            value = f'{hours_text(g.get("hours"), lang)} {w["h"]}'
            budget = width - pad * 2 - text_width(value, 10) - 8
            out.append(f'<text x="{pad}" y="{y + 8:.0f}" font-family="{FONT}" '
                       f'font-size="10" fill="{theme["text"]}">'
                       f'{esc(clip(plain(g.get("name")), 10, budget))}</text>')
            out.append(f'<text x="{width - pad}" y="{y + 8:.0f}" text-anchor="end" '
                       f'font-family="{FONT}" font-size="10" fill="{theme["dim"]}">'
                       f'{esc(value)}</text>')
            track = width - pad * 2
            fill = max(2.0, track * ((g.get("hours") or 0) / top))
            out.append(f'<rect x="{pad}" y="{y + 13:.0f}" width="{track}" '
                       f'height="5" rx="2.5" fill="{theme["rest"]}"/>')
            out.append(f'<rect x="{pad}" y="{y + 13:.0f}" width="{fill:.1f}" '
                       f'height="5" rx="2.5" fill="{theme["accent"]}"/>')
            y += 26

    out.append(f'<text x="{mid:.0f}" y="{height - 10}" text-anchor="middle" '
               f'font-family="{FONT}" font-size="9" fill="{theme["dim"]}">'
               f'steamprofiler.org · {esc(stamp(profile))}</text>')
    out.append("</svg>")
    return "".join(out)


# ── Badges ───────────────────────────────────────────────────────────
# The shields.io shape, because a README that already has six of these has a
# row and this has to stand in it. Same height, same font, same paddings.

BADGE_COLOURS = {
    "amber": "#ffb454", "orange": "#e07b1f", "green": "#4c9a2a",
    "brightgreen": "#4bc21b", "blue": "#3d7bd6", "violet": "#8a63d2",
    "red": "#cc3333", "pink": "#d6559a", "grey": "#5d5a6b", "gray": "#5d5a6b",
    "black": "#1a1822", "steam": "#66c0f4",
}
LABEL_BG = {"dark": "#3d3b47", "light": "#5c5866", "steam": "#2a475e"}


def ink(colour):
    """Black or white over `colour`, whichever can be read on it. Amber with
    white text is the one mistake that makes a badge look homemade.

    Three digits are expanded first. `red` was written `#c33` in the table above
    and went straight through to here, where the third pair is an empty string
    and int() raises - which is a 500 on a route whose whole point is that it
    cannot fail, because nobody sees a 500 inside an <img>."""
    raw = colour.lstrip("#")
    if len(raw) == 3:
        raw = "".join(ch * 2 for ch in raw)
    r, g, b = (int(raw[i:i + 2], 16) for i in (0, 2, 4))
    return "#0b0a0e" if (r * .299 + g * .587 + b * .114) > 150 else "#ffffff"


def metric(profile, key, lang):
    """`(label, figure)` for one thing worth putting in a badge."""
    w = words(lang)
    totals = profile.get("totals") or {}
    who = profile.get("profile") or {}
    library = profile.get("library") or []
    top = library[0] if library else None
    now = (profile.get("now") or {}).get("playing")
    platform = profile.get("platform") or {}

    if key == "games":
        return w["games"], group(totals.get("owned") or 0, lang)
    if key == "played":
        return w["played"], group(totals.get("played") or 0, lang)
    if key == "never":
        return w["never"], group(totals.get("never_played") or 0, lang)
    if key == "level":
        return w["level"], group(who.get("level") or 0, lang)
    if key == "top":
        return w["top"], plain(top.get("name")) if top else w["nothing"]
    if key == "linux":
        return w["linux"], f'{group(platform.get("linux_hours") or 0, lang)} {w["h"]}'
    if key == "deck":
        return w["deck"], f'{group(platform.get("deck_hours") or 0, lang)} {w["h"]}'
    if key == "achievements":
        return w["achievements"], group(who.get("achievements_total") or 0, lang)
    if key == "badges":
        return w["badges"], group(who.get("badge_count") or 0, lang)
    if key == "since":
        return w["since"], (who.get("member_since") or "")[:4] or "?"
    if key == "per_day":
        return w["per_day"], hours_text(totals.get("hours_per_day") or 0, lang)
    if key == "now":
        # The one badge that is only worth the URL when it is dynamic: static,
        # it says what somebody was playing on the afternoon they made it.
        return w["now"], plain(now) if now else w["nothing"]
    return w["hours"], f'{group(round(totals.get("hours") or 0), lang)} {w["h"]}'


def badge(profile, o):
    theme_name = o["theme"]
    theme = THEMES[theme_name]
    label, value = metric(profile, o["metric"], o["lang"])
    if o["label"]:
        label = o["label"]
    right = o["colour"] or theme["accent"]
    left = LABEL_BG.get(theme_name, "#3d3b47")
    big = o["style"] == "big"

    if big:
        label, value = label.upper(), value.upper()
        size, height, base, side, gap, track = 10, 28, 18, 10, 9, 1.3
    else:
        size, height, base, side, gap, track = 11, 20, 14, 6, 4, 0

    logo_w = (14 + gap) if o["logo"] else 0
    # Rounded up, both halves. Down is a badge whose last letter sits on the
    # edge of its own colour, and half a pixel of padding is invisible.
    label_w = ceil(side * 2 + logo_w + text_width(label, size) + track * len(label))
    value_w = ceil(side * 2 + text_width(value, size) + track * len(value))
    width = label_w + value_w
    rx = 0 if o["style"] == "square" else (4 if big else 3)

    out = [open_svg(width, height, f"{label}: {value}")]
    out.append(f'<clipPath id="r"><rect width="{width}" height="{height}" '
               f'rx="{rx}" fill="#fff"/></clipPath>')
    out.append(f'<g clip-path="url(#r)">')
    out.append(f'<rect width="{label_w}" height="{height}" fill="{left}"/>')
    out.append(f'<rect x="{label_w}" width="{value_w}" height="{height}" '
               f'fill="{right}"/>')
    if o["style"] == "plastic":
        # The shine. It is the only difference between plastic and flat, and it
        # is one rectangle: white at the top, black at the bottom, both faint.
        out.append('<linearGradient id="s" x2="0" y2="100%">'
                   '<stop offset="0" stop-color="#fff" stop-opacity=".7"/>'
                   '<stop offset=".1" stop-color="#aaa" stop-opacity=".1"/>'
                   '<stop offset=".9" stop-color="#000" stop-opacity=".3"/>'
                   '<stop offset="1" stop-color="#000" stop-opacity=".5"/>'
                   '</linearGradient>'
                   f'<rect width="{width}" height="{height}" fill="url(#s)"/>')
    out.append("</g>")

    if o["logo"]:
        out.append(mark(side, (height - 14) / 2, 14, "#ffffff"))

    weight = ' font-weight="bold"' if big else ""
    spacing = f' letter-spacing="{track}"' if track else ""
    for x, s, colour, anchor in ((side + logo_w, label, "#ffffff", "start"),
                                 (label_w + side, value, ink(right), "start")):
        # Drawn twice: once in black at a tenth strength one pixel lower, then
        # in its own colour. That shadow is what keeps eleven-pixel text legible
        # on a mid-tone, and it is what every badge on every README already has.
        out.append(f'<text x="{x}" y="{base + 1}" font-family="{FONT}" '
                   f'font-size="{size}"{weight}{spacing} fill="#000" '
                   f'fill-opacity=".25">{esc(s)}</text>')
        out.append(f'<text x="{x}" y="{base}" font-family="{FONT}" '
                   f'font-size="{size}"{weight}{spacing} fill="{colour}">'
                   f'{esc(s)}</text>')
    out.append("</svg>")
    return "".join(out)


# ── What the URL is allowed to say ───────────────────────────────────
# Every value is clamped or falls back, and nothing here refuses. A typo in the
# query string of an <img> is a broken picture in somebody's README, and the
# reader of that README cannot fix it - so a chart that ignores one unknown word
# is worth more than an error nobody sees.

SHOWS = ("top", "recent", "platform", "genres", "years")
STYLES = ("plain", "color", "icon", "art")
BADGE_STYLES = ("flat", "square", "plastic", "big")
METRICS = ("hours", "games", "played", "never", "level", "top", "linux", "deck",
           "achievements", "badges", "since", "per_day", "now")
HEX = re.compile(r"^#?(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
# A custom label is text somebody else will read, and it is drawn with a
# measured width. Newlines and angle brackets are escaped anyway; the cap is
# what stops a badge from being a paragraph.
LABEL_MAX = 24


def pick(value, allowed, fallback):
    return value if value in allowed else fallback


def whole(value, low, high, fallback):
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return fallback


def colour_of(value):
    """A named colour, a hex triple, or None for the theme's own accent."""
    if not value:
        return None
    if value.lower() in BADGE_COLOURS:
        return BADGE_COLOURS[value.lower()]
    if HEX.match(value):
        raw = value.lstrip("#")
        if len(raw) == 3:
            raw = "".join(ch * 2 for ch in raw)
        return "#" + raw.lower()
    return None


def options(kind, get):
    """Read one query string into the settings a renderer takes."""
    o = {
        "theme": pick(get("theme"), THEMES, "dark"),
        "lang": pick(get("lang"), WORDS, "en"),
        "show": pick(get("show"), SHOWS, "top"),
    }
    if kind == "badge":
        o.update({
            "metric": pick(get("metric"), METRICS, "hours"),
            "style": pick(get("style"), BADGE_STYLES, "flat"),
            "label": " ".join(get("label").split())[:LABEL_MAX],
            "colour": colour_of(get("color") or get("colour")),
            "logo": get("logo") not in ("", "0", "no", "false", "none"),
        })
        return o
    if kind == "banner":
        o["preset"] = pick(get("preset") or get("size"), PRESETS, "blog")
        return o
    if kind == "text":
        o.update({"n": whole(get("n"), 1, 15, 5),
                  "cells": whole(get("cells"), 6, 40, 18),
                  "label_w": whole(get("pad"), 8, 40, 22)})
        return o
    o.update({
        "style": pick(get("style"), STYLES, "plain"),
        "w": whole(get("w"), 240, 900, 420),
        "head": get("head") not in ("0", "no", "false"),
        "foot": get("foot") not in ("0", "no", "false"),
    })
    # The art style is the only one that can send this service to Steam's CDN,
    # once per row, the first time a game is asked for. Fewer rows than the
    # other styles allow, for that reason and no other.
    o["n"] = whole(get("n"), 1, 8 if o["style"] == "art" else 15, 5)
    return o
