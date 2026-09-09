"""The link preview: a profile's treemap, drawn as a PNG.

Pasting a profile into WhatsApp or Discord used to show the site's generic
description. It now shows that person's library drawn to scale, which is the
one thing here worth looking at before you click.

Written against the standard library only, like everything else that runs in
the api container. That rules out Pillow, so the PNG is assembled by hand:
zlib does the compression and the rest is a bytearray. The only text on the
card is the wordmark and the total, drawn from a 5x7 bitmap - the persona and
the numbers travel in og:title and og:description, where every platform
already renders them as real text beside the image.
"""

import struct
import re
import unicodedata
import zlib

W, H = 1200, 630          # what every scraper expects of an og:image
FOOT = 96                 # the strip under the map
BG = (11, 10, 14)         # --bg
PANEL = (19, 18, 25)      # --panel
ACCENT = (255, 180, 84)   # --accent
DIM = (142, 138, 155)     # --dim


# ── The treemap ──────────────────────────────────────────────────────

def squarify(items, x0, y0, w0, h0):
    """Bruls, Huizing & van Wijk, the same layout the page draws in the
    browser: fill the shorter side of what is left, and close the row as soon
    as one more cell would make its aspect ratios worse."""
    out = []
    total = sum(v for v, _ in items)
    if not total or w0 <= 0 or h0 <= 0:
        return out

    scale = (w0 * h0) / total
    x, y, w, h = x0, y0, w0, h0
    queue = list(items)
    row, row_area = [], 0.0

    def worst(areas, s, length):
        if not areas or s <= 0:
            return float("inf")
        mx, mn = max(areas), min(areas)
        s2, l2 = s * s, length * length
        return max((l2 * mx) / s2, s2 / (l2 * mn))

    def flush():
        nonlocal x, y, w, h, row, row_area
        if not row:
            return
        length = min(w, h)
        thick = row_area / length if length else 0
        off = 0.0
        for area, item in row:
            side = (area / row_area) * length if row_area else 0
            if w >= h:
                out.append((x, y + off, thick, side, item))
            else:
                out.append((x + off, y, side, thick, item))
            off += side
        if w >= h:
            x += thick
            w = max(0.0, w - thick)
        else:
            y += thick
            h = max(0.0, h - thick)
        row, row_area = [], 0.0

    while queue:
        value, item = queue[0]
        area = value * scale
        length = min(w, h)
        if length <= 0:
            break
        areas = [a for a, _ in row]
        if not row or worst(areas, row_area, length) >= worst(areas + [area], row_area + area, length):
            row.append((area, item))
            row_area += area
            queue.pop(0)
        else:
            flush()
    flush()
    return out


# ── A 5x7 bitmap, only the glyphs this card can print ────────────────

GLYPHS = {
    '0': ('.###.', '#...#', '#..##', '#.#.#', '##..#', '#...#', '.###.'),
    '1': ('..#..', '.##..', '..#..', '..#..', '..#..', '..#..', '.###.'),
    '2': ('.###.', '#...#', '....#', '...#.', '..#..', '.#...', '#####'),
    '3': ('#####', '...#.', '..#..', '...#.', '....#', '#...#', '.###.'),
    '4': ('...#.', '..##.', '.#.#.', '#..#.', '#####', '...#.', '...#.'),
    '5': ('#####', '#....', '####.', '....#', '....#', '#...#', '.###.'),
    '6': ('..##.', '.#...', '#....', '####.', '#...#', '#...#', '.###.'),
    '7': ('#####', '....#', '...#.', '..#..', '.#...', '.#...', '.#...'),
    '8': ('.###.', '#...#', '#...#', '.###.', '#...#', '#...#', '.###.'),
    '9': ('.###.', '#...#', '#...#', '.####', '....#', '...#.', '.##..'),
    'a': ('.###.', '#...#', '#...#', '#####', '#...#', '#...#', '#...#'),
    'b': ('###..', '#..#.', '#..#.', '###..', '#...#', '#...#', '####.'),
    'd': ('###..', '#..#.', '#...#', '#...#', '#...#', '#..#.', '###..'),
    'e': ('.###.', '#...#', '#...#', '#####', '#....', '#...#', '.###.'),
    'f': ('#####', '#....', '#....', '####.', '#....', '#....', '#....'),
    'g': ('.###.', '#...#', '#....', '#.###', '#...#', '#...#', '.###.'),
    'h': ('#...#', '#...#', '#...#', '#####', '#...#', '#...#', '#...#'),
    'i': ('.###.', '..#..', '..#..', '..#..', '..#..', '..#..', '.###.'),
    'l': ('#....', '#....', '#....', '#....', '#....', '#....', '#####'),
    'm': ('#...#', '##.##', '#.#.#', '#.#.#', '#...#', '#...#', '#...#'),
    'o': ('.###.', '#...#', '#...#', '#...#', '#...#', '#...#', '.###.'),
    'p': ('####.', '#...#', '#...#', '####.', '#....', '#....', '#....'),
    'r': ('####.', '#...#', '#...#', '####.', '#.#..', '#..#.', '#...#'),
    's': ('.####', '#....', '#....', '.###.', '....#', '....#', '####.'),
    't': ('#####', '..#..', '..#..', '..#..', '..#..', '..#..', '..#..'),
    'v': ('#...#', '#...#', '#...#', '#...#', '#...#', '.#.#.', '..#..'),
    '.': ('.....', '.....', '.....', '.....', '.....', '.##..', '.##..'),
    ' ': ('.....',) * 7,
}
# The profile card above deliberately prints almost no prose.  The year card
# names people and games, so it needs the rest of a compact ASCII alphabet.
GLYPHS.update({
    'c': ('.###.', '#...#', '#....', '#....', '#....', '#...#', '.###.'),
    'j': ('..###', '...#.', '...#.', '...#.', '...#.', '#..#.', '.##..'),
    'k': ('#...#', '#..#.', '#.#..', '##...', '#.#..', '#..#.', '#...#'),
    'n': ('#...#', '##..#', '##..#', '#.#.#', '#..##', '#..##', '#...#'),
    'q': ('.###.', '#...#', '#...#', '#...#', '#.#.#', '#..#.', '.##.#'),
    'u': ('#...#', '#...#', '#...#', '#...#', '#...#', '#...#', '.###.'),
    'w': ('#...#', '#...#', '#...#', '#.#.#', '#.#.#', '##.##', '#...#'),
    'x': ('#...#', '#...#', '.#.#.', '..#..', '.#.#.', '#...#', '#...#'),
    'y': ('#...#', '#...#', '.#.#.', '..#..', '..#..', '..#..', '..#..'),
    'z': ('#####', '....#', '...#.', '..#..', '.#...', '#....', '#####'),
    '-': ('.....', '.....', '.....', '#####', '.....', '.....', '.....'),
    ':': ('.....', '.##..', '.##..', '.....', '.##..', '.##..', '.....'),
    '?': ('.###.', '#...#', '....#', '...#.', '..#..', '.....', '..#..'),
})
GLYPH_W, GLYPH_H = 5, 7


class Canvas:
    """RGB pixels in a flat bytearray, and the four operations this needs."""

    def __init__(self, w, h, fill):
        self.w, self.h = w, h
        self.px = bytearray(bytes(fill) * (w * h))

    def rect(self, x, y, w, h, colour):
        x0, y0 = max(0, int(x)), max(0, int(y))
        x1, y1 = min(self.w, int(x + w)), min(self.h, int(y + h))
        if x1 <= x0 or y1 <= y0:
            return
        line = bytes(colour) * (x1 - x0)
        for row in range(y0, y1):
            at = (row * self.w + x0) * 3
            self.px[at:at + len(line)] = line

    def stripes(self, x, y, w, h, a, b):
        """The tail block. It is a group of games rather than one game, so it
        gets the same striping the page uses instead of a solid tone."""
        x0, y0 = max(0, int(x)), max(0, int(y))
        x1, y1 = min(self.w, int(x + w)), min(self.h, int(y + h))
        for row in range(y0, y1):
            at = (row * self.w) * 3
            for col in range(x0, x1):
                colour = a if ((col + row) // 7) % 2 else b
                self.px[at + col * 3:at + col * 3 + 3] = bytes(colour)

    def text(self, s, x, y, scale, colour):
        """Draw `s` at `scale`, returning where it ended."""
        for ch in s:
            g = GLYPHS.get(ch, GLYPHS[' '])
            for row, bits in enumerate(g):
                for col, bit in enumerate(bits):
                    if bit == '#':
                        self.rect(x + col * scale, y + row * scale, scale, scale, colour)
            x += (GLYPH_W + 1) * scale
        return x

    def text_width(self, s, scale):
        return len(s) * (GLYPH_W + 1) * scale - scale

    def png(self):
        raw = bytearray()
        stride = self.w * 3
        for row in range(self.h):
            raw.append(0)  # filter: none. The image is flat colour, so the
            raw += self.px[row * stride:(row + 1) * stride]  # filters buy nothing.

        def chunk(tag, data):
            out = struct.pack(">I", len(data)) + tag + data
            return out + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

        return (b"\x89PNG\r\n\x1a\n"
                + chunk(b"IHDR", struct.pack(">IIBBBBB", self.w, self.h, 8, 2, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
                + chunk(b"IEND", b""))


def mix(base, over, alpha):
    return tuple(round(b + (o - b) * alpha) for b, o in zip(base, over))


def readable(value, limit):
    """Best-effort text for the deliberately tiny built-in bitmap font."""
    raw = unicodedata.normalize("NFKD", str(value or ""))
    raw = "".join(ch for ch in raw if not unicodedata.combining(ch)).lower()
    # A character this font cannot draw is dropped, not turned into "?". The
    # substitution printed "call of duty?" on the annual card - a registered
    # trademark sign became a question mark, and the title read as a question.
    # A "?" that the title really has survives, because "?" is a glyph.
    clean = "".join(ch if ch in GLYPHS else "" for ch in raw)
    clean = re.sub(r"\s{2,}", " ", clean).strip()
    # Only if nothing at all survived: a blank where a name goes is worse than
    # an obvious placeholder.
    return (clean[:limit].rstrip() or "?")


def card(profile):
    """The PNG for one profile payload, as bytes."""
    library = profile.get("library") or []
    total_hours = (profile.get("totals") or {}).get("hours") or 0
    c = Canvas(W, H, BG)

    map_h = H - FOOT
    named = [g for g in library if (g.get("hours") or 0) >= 12][:72]
    tail = library[len(named):]
    tail_hours = sum(g.get("hours") or 0 for g in tail)

    items = [(g["hours"], g) for g in named]
    if tail_hours > 0:
        items.append((tail_hours, {"tail": True}))

    top = max((g["hours"] for g in named), default=1) or 1
    for x, y, w, h, item in squarify(items, 0, 0, W, map_h):
        if w < 1 or h < 1:
            continue
        # A two-pixel gap, the same one the page leaves between cells.
        gx, gy = x + 1, y + 1
        gw, gh = max(1, w - 2), max(1, h - 2)
        if item.get("tail"):
            c.stripes(gx, gy, gw, gh, mix(BG, ACCENT, 0.06), mix(BG, ACCENT, 0.13))
        else:
            alpha = 0.05 + 0.92 * ((item["hours"] / top) ** 0.42)
            c.rect(gx, gy, gw, gh, mix(BG, ACCENT, alpha))

    # The strip: the wordmark on the left, the total on the right. No persona
    # and no game names - those are text in og:title and og:description, and a
    # bitmap font would only render them worse than the platform will.
    c.rect(0, map_h, W, FOOT, PANEL)
    c.rect(0, map_h, W, 2, mix(PANEL, ACCENT, 0.35))
    c.text("steamprofiler.org", 40, map_h + 34, 4, DIM)

    hours = f"{int(total_hours)} h"
    c.text(hours, W - 40 - c.text_width(hours, 6), map_h + 24, 6, ACCENT)
    return c.png()


def year_card(profile, year, unlocks=None):
    """One annual card, using the same honest approximation as /year/<year>.

    Steam publishes only a game's lifetime clock and its last-played date.  An
    annual bucket therefore means "games last played in this year", not hours
    accrued during it.  Achievement dates are genuinely annual, but are shown
    only when the separately cached scan was supplied by the caller.
    """
    games = sorted(
        (g for g in (profile.get("library") or [])
         if (g.get("last_played") or "")[:4] == str(year)),
        key=lambda g: -(g.get("hours") or 0))
    hours = round(sum(g.get("hours") or 0 for g in games))
    persona = readable((profile.get("profile") or {}).get("persona") or "profile", 28)
    unlock_slot = ((unlocks or {}).get("years") or {}).get(str(year))

    c = Canvas(W, H, BG)
    c.rect(0, 0, W, 12, ACCENT)
    c.text(str(year), 54, 48, 12, ACCENT)
    c.text(persona, 58, 158, 5, DIM)
    # Fixed columns, both at scale 3: their labels end at x=361 and x=659.
    c.text("games last played", 58, 222, 3, DIM)
    c.text(str(len(games)), 58, 262, 8, ACCENT)
    c.text("lifetime hours", 410, 222, 3, DIM)
    c.text(str(hours), 410, 262, 8, ACCENT)

    # A compact podium: the bar length carries rank even when a title contains
    # characters the bitmap font cannot reproduce.
    top = max((g.get("hours") or 0 for g in games[:4]), default=1) or 1
    for i, game in enumerate(games[:4]):
        y = 372 + i * 48
        amount = game.get("hours") or 0
        c.rect(58, y + 28, 510 * amount / top, 8, mix(PANEL, ACCENT, .75 - i * .1))
        # Trimmed by measured width and not by a count of letters. The count
        # was 34 and "grand theft auto: san andreas" is 29, so it passed and
        # then ran straight into the hours column: what fits a row is a number
        # of pixels, and the two are only the same when every letter is.
        label = f"{round(amount)} h"
        label_w = c.text_width(label, 3)
        budget = (650 - label_w - 24) - 58
        name = readable(game.get("name"), 60)
        if c.text_width(name, 3) > budget:
            while name and c.text_width(name + "...", 3) > budget:
                name = name[:-1].rstrip()
            name += "..."
        c.text(name, 58, y, 3, (235, 232, 240))
        c.text(label, 650 - label_w, y, 3, DIM)

    c.rect(730, 205, 412, 250, PANEL)
    c.text("unlocked", 770, 246, 4, DIM)
    if unlock_slot is None:
        c.text("not scanned", 770, 306, 6, DIM)
    else:
        c.text(str(unlock_slot.get("unlocks") or 0), 770, 306, 10, ACCENT)
        c.text("achievements", 770, 390, 4, DIM)

    c.rect(0, H - 70, W, 70, PANEL)
    c.text("steamprofiler.org", 40, H - 46, 4, DIM)
    c.text("annual snapshot", W - 40 - c.text_width("annual snapshot", 4),
           H - 46, 4, DIM)
    return c.png()
