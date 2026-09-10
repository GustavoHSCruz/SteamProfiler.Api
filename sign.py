#!/usr/bin/env python3
"""steamprofiler.org - a signature, drawn rather than typeset.

The artwork generator lets somebody write a name across the bottom of the
picture in a hand. Every other string on an embed is set in Verdana, which
works because Verdana is on the machine that opens the file. A script face is
not: nobody has the same one, most people have none, and `font-family:cursive`
resolves to Comic Sans on Windows, to a serif on a lot of Linux, and to nothing
at all inside a canvas that is turning the SVG into a PNG.

Shipping the font is not an option either. A webfont is a fetch, and an embed
that fetches anything draws as a broken box on GitHub and refuses to become a
PNG in the browser - the whole reason every file here is self-contained.

So the letters are strokes, not glyphs. Each one is a short list of points a
pen passes through, and the curve between them is a Catmull-Rom spline: the
smoothing is what does the handwriting, which is why a letter is eight numbers
here instead of eight beziers. The points are laid out on one grid, every
letter enters at the baseline on its left and leaves at the baseline on its
right, and a word is therefore one continuous stroke through all of them -
which is what cursive is.

The grid, in the units the tables below are written in:

    y = -58   the top of an ascender, and the cap height
    y = -30   the top of an x-height letter
    y =   0   the baseline, where letters join
    y = +20   the bottom of a descender

`size` everywhere in this module means the cap height in pixels, so a signature
asked for at 40 is 40 pixels from the baseline to the top of a capital.

Stdlib only, like the rest of the api.
"""

from math import tan, radians

# The design grid. Only ASC is arithmetic anywhere else in the file.
ASC = 58.0
DESC = 20.0

# How far the whole thing leans. Applied once as a skew on the group rather
# than baked into every point, which is what keeps the tables readable.
SLANT = 12.0
_LEAN = tan(radians(SLANT))

# Anything not in the tables is dropped rather than drawn as a box: a signature
# with a tofu in it is worse than a signature one letter shorter. Accented
# letters are folded onto their bare forms before they get here.
SPACE = 16.0

# One entry per character: (advance, main stroke, extra strokes, dots).
#
# The main stroke is the one that joins: it starts at (0, 0) and ends at
# (advance, 0), so laying the next letter at x + advance makes the two halves
# of one curve. Extras are the strokes a pen lifts for - the bar of a `t`, the
# second diagonal of an `x` - and dots are exactly that.
GLYPHS = {
    # ── Lowercase ────────────────────────────────────────────────────
    "a": (26, [(0, 0), (12, -22), (20, -29), (10, -30), (2, -17), (9, -2),
               (19, -14), (20, -29), (20, -6), (26, 0)], [], []),
    "b": (24, [(0, 0), (6, -30), (10, -58), (7, -34), (5, -12), (12, -3),
               (19, -10), (14, -18), (6, -13), (15, -6), (24, 0)], [], []),
    "c": (22, [(0, 0), (12, -20), (19, -29), (9, -30), (2, -17), (6, -4),
               (16, -4), (22, 0)], [], []),
    "d": (26, [(0, 0), (12, -22), (19, -29), (9, -30), (2, -17), (9, -2),
               (17, -12), (21, -32), (22, -52), (20, -30), (20, -6),
               (26, 0)], [], []),
    "e": (22, [(0, 0), (8, -14), (16, -16), (14, -26), (6, -24), (3, -12),
               (8, -3), (17, -5), (22, 0)], [], []),
    "f": (22, [(0, 0), (7, -20), (12, -42), (13, -56), (9, -44), (8, -22),
               (8, -2), (6, 12), (1, 17), (-1, 8), (8, -2), (17, -4),
               (22, 0)], [], []),
    "g": (26, [(0, 0), (12, -22), (19, -29), (9, -30), (2, -17), (9, -2),
               (18, -12), (20, -29), (19, -6), (16, 10), (9, 18), (4, 10),
               (13, 4), (22, 2), (26, 0)], [], []),
    "h": (26, [(0, 0), (5, -28), (9, -58), (7, -34), (5, -10), (6, -2),
               (12, -16), (18, -24), (21, -14), (20, -4), (26, 0)], [], []),
    "i": (18, [(0, 0), (6, -14), (11, -28), (12, -6), (18, 0)], [],
          [(11, -40)]),
    "j": (18, [(0, 0), (6, -14), (11, -28), (10, -4), (7, 10), (2, 17),
               (-2, 9), (6, 2), (14, 1), (18, 0)], [], [(11, -40)]),
    "k": (26, [(0, 0), (5, -28), (9, -58), (7, -34), (5, -10), (6, -2),
               (14, -12), (20, -22), (12, -16), (9, -12), (16, -8), (21, -3),
               (26, 0)], [], []),
    "l": (20, [(0, 0), (6, -26), (11, -56), (9, -32), (7, -10), (9, -2),
               (14, -2), (20, 0)], [], []),
    "m": (34, [(0, 0), (4, -20), (6, -28), (7, -8), (9, -22), (13, -29),
               (16, -10), (18, -22), (22, -29), (26, -12), (27, -4),
               (34, 0)], [], []),
    "n": (26, [(0, 0), (4, -20), (6, -28), (7, -8), (10, -22), (15, -29),
               (19, -14), (20, -4), (26, 0)], [], []),
    "o": (24, [(0, 0), (10, -18), (17, -28), (9, -30), (2, -18), (6, -4),
               (16, -6), (20, -18), (17, -27), (20, -12), (24, 0)], [], []),
    "p": (26, [(0, 0), (6, -20), (10, -29), (7, -8), (5, 8), (3, 18), (6, 0),
               (9, -14), (16, -20), (21, -13), (16, -4), (9, -6), (15, -2),
               (26, 0)], [], []),
    "q": (26, [(0, 0), (12, -22), (19, -29), (9, -30), (2, -17), (9, -2),
               (18, -12), (20, -29), (19, -8), (17, 6), (15, 17), (21, 10),
               (26, 0)], [], []),
    "r": (22, [(0, 0), (6, -18), (8, -27), (9, -14), (12, -25), (17, -27),
               (14, -22), (16, -8), (22, 0)], [], []),
    "s": (20, [(0, 0), (9, -16), (15, -27), (7, -30), (4, -22), (11, -16),
               (15, -8), (9, -3), (4, -6), (12, -2), (20, 0)], [], []),
    "t": (22, [(0, 0), (7, -24), (11, -46), (9, -24), (8, -6), (11, -2),
               (17, -5), (22, 0)], [[(2, -31), (18, -34)]], []),
    "u": (26, [(0, 0), (5, -20), (7, -29), (7, -10), (10, -3), (15, -12),
               (18, -28), (19, -10), (20, -4), (26, 0)], [], []),
    # v, w and their capitals stop at the top right instead of coming back
    # down to the baseline. A `v` drawn with an exit stroke to the baseline
    # has four strokes in it and is read as a `u`; the join to whatever comes
    # next leaves from the top, which is where a hand leaves it too.
    "v": (24, [(0, 0), (5, -20), (8, -29), (10, -12), (13, -4), (17, -18),
               (19, -28)], [], []),    "w": (32, [(0, 0), (4, -20), (7, -29), (9, -10), (12, -3), (15, -16),
               (17, -28), (19, -10), (22, -3), (25, -16), (27, -28)], [], []),    "x": (24, [(0, 0), (6, -18), (9, -28), (14, -18), (19, -4), (24, 0)],
          [[(20, -28), (12, -16), (6, -4)]], []),
    "y": (26, [(0, 0), (5, -20), (7, -29), (7, -10), (11, -3), (16, -14),
               (19, -28), (18, -8), (16, 6), (13, 17), (7, 18), (5, 10),
               (14, 4), (22, 1), (26, 0)], [], []),
    "z": (24, [(0, 0), (3, -16), (5, -30), (13, -31), (20, -30), (12, -16),
               (4, -3), (13, -2), (20, -3), (24, 0)], [], []),
    # ── Capitals ─────────────────────────────────────────────────────
    # Eleven of them are not in this table: C, O, S, U, V, W, X, Z, M, N and J
    # are the lowercase letter written large, which is a real hand and not a
    # shortcut - in a monoline script those eleven shapes genuinely are their
    # own capitals. They are built from the tables above at import time, at the
    # bottom of this section.
    #
    # The rest are here because their lowercase form is a different letter.
    # Every one of them is a single pass: a stroke that doubles back over
    # itself smooths into a blob at the size a signature is actually read at,
    # so a stem that has to be climbed and descended is written as a narrow
    # loop instead, which is how it is written by hand anyway.
    "A": (34, [(0, 0), (3, -12), (9, -34), (15, -56), (21, -34), (26, -12),
               (29, -2), (34, 0)], [[(9, -19), (26, -21)]], []),
    "B": (32, [(0, 0), (6, -28), (10, -58), (20, -55), (25, -46), (16, -31),
               (25, -22), (22, -7), (11, -2), (32, 0)], [], []),
    "D": (34, [(0, 0), (6, -28), (10, -58), (21, -55), (28, -42), (27, -20),
               (18, -6), (9, -3), (16, -1), (26, -2), (34, 0)], [], []),
    # A spine on the left and two bowls off it, in the order a pen writes
    # them. The left side is never closed: an E with a stem down its left is
    # read as a B at the size a signature is looked at.
    # A spine and two bowls hung off it to the right. The pinch between the
    # bowls stays well clear of the spine: an E whose bowls close on it is
    # read as a B, which is the one mistake this letter can make.
    "E": (30, [(0, 0), (4, -10), (4, -30), (8, -48), (17, -55), (25, -50),
               (23, -40), (15, -35), (24, -31), (28, -20), (24, -7), (14, -3),
               (22, -1), (30, 0)], [], []),    "F": (28, [(0, 0), (5, -12), (6, -30), (7, -48), (13, -57), (21, -55),
               (24, -46), (20, -42), (14, -46), (12, -32), (11, -14), (11, -3),
               (20, -2), (28, 0)], [[(3, -31), (22, -34)]], []),    # The C, and the bar across it that is the whole difference. The bar is
    # lifted for rather than drawn into the curve, because a G whose bar is
    # part of one stroke closes into an O.
    # The C, and then the bar: up the right side, left across the bowl, and
    # back down. Written into the one stroke rather than lifted for, which is
    # how it is written by hand and what keeps it from reading as an O with a
    # scratch on it.
    "G": (32, [(0, 0), (11, -7), (23, -19), (27, -39), (23, -54), (12, -56),
               (4, -44), (3, -25), (9, -8), (20, -6), (27, -16), (26, -28),
               (18, -28), (26, -27), (30, -14), (32, 0)], [], []),    "H": (32, [(0, 0), (4, -22), (8, -46), (11, -58), (12, -40), (11, -20),
               (10, -4), (18, -2), (32, 0)],
          [[(28, -58), (25, -34), (23, -12), (24, -3)], [(9, -28), (26, -30)]],
          []),
    "I": (24, [(0, 0), (6, -10), (13, -26), (17, -44), (15, -56), (8, -56),
               (5, -45), (8, -30), (13, -14), (15, -4), (24, 0)], [], []),
    "K": (32, [(0, 0), (4, -22), (8, -46), (11, -58), (12, -40), (11, -20),
               (10, -4), (18, -2), (32, 0)],
          [[(30, -56), (20, -40), (11, -28)], [(13, -26), (22, -13), (29, -3)]],
          []),
    "L": (30, [(0, 0), (8, -12), (16, -30), (21, -48), (18, -57), (10, -54),
               (7, -40), (7, -24), (8, -10), (14, -3), (24, -4),
               (30, 0)], [], []),
    "P": (28, [(0, 0), (4, -22), (8, -46), (11, -58), (12, -40), (11, -20),
               (10, -4), (18, -2), (28, 0)],
          [[(11, -56), (20, -53), (25, -44), (22, -34), (13, -31)]], []),
    "R": (32, [(0, 0), (4, -22), (8, -46), (11, -58), (12, -40), (11, -20),
               (10, -4), (18, -2), (32, 0)],
          [[(11, -56), (20, -53), (25, -44), (22, -34), (13, -31)],
           [(15, -30), (23, -16), (30, -3)]], []),
    "T": (30, [(0, 0), (7, -8), (13, -22), (17, -40), (19, -54), (14, -57),
               (10, -46), (11, -26), (13, -10), (18, -3), (30, 0)],
          [[(2, -50), (16, -56), (30, -58)]], []),    # The V, and everything below the baseline that makes it a Y.
    "U": (34, [(0, 0), (4, -22), (8, -46), (10, -58), (9, -40), (8, -22),
               (10, -8), (17, -2), (24, -8), (27, -30), (30, -56)], [], []),    "V": (38, [(0, 0), (4, -16), (8, -38), (11, -56), (15, -36), (19, -16),
               (21, -3), (26, -24), (31, -46), (34, -58)], [], []),    "Z": (32, [(0, 0), (3, -26), (6, -54), (6, -58), (16, -58), (26, -57),
               (17, -38), (9, -18), (6, -4), (16, -2), (26, -3),
               (32, 0)], [], []),
    "Y": (38, [(0, 0), (4, -16), (8, -38), (11, -56), (15, -36), (19, -16),
               (21, -3), (26, -24), (31, -46), (33, -58), (30, -32), (26, -8),
               (22, 7), (15, 17), (8, 14), (11, 6), (21, 2), (38, 0)], [], []),
    # ── Figures ──────────────────────────────────────────────────────
    # A numeral has no joining stroke, here or on paper: nobody writes a date
    # in one line without lifting the pen. So these have no main stroke at
    # all - everything is a lifted one - and a figure breaks the run it is in,
    # the same way a space does.
    "0": (22, [], [[(13, -43), (6, -40), (2, -27), (3, -13), (9, -3), (16, -7),
                    (19, -21), (17, -36), (12, -43)]], []),
    "1": (16, [], [[(3, -32), (10, -43), (11, -22), (11, -2)]], []),
    "2": (22, [], [[(3, -33), (7, -42), (15, -42), (18, -34), (12, -22),
                    (4, -10), (2, -2), (11, -2), (19, -3)]], []),
    "3": (22, [], [[(3, -36), (9, -43), (17, -41), (16, -31), (9, -26),
                    (17, -24), (20, -15), (15, -4), (6, -3), (2, -9)]], []),
    "4": (22, [], [[(14, -43), (3, -14), (19, -15)], [(15, -38), (13, -2)]], []),
    "5": (22, [], [[(18, -42), (7, -42), (5, -27), (12, -28), (19, -23),
                    (19, -11), (12, -2), (4, -5)]], []),
    "6": (22, [], [[(16, -42), (8, -36), (3, -23), (3, -11), (9, -3), (16, -7),
                    (17, -17), (10, -20), (4, -15)]], []),
    "7": (22, [], [[(2, -42), (18, -42), (9, -20), (6, -2)]], []),
    "8": (22, [], [[(12, -43), (6, -38), (8, -29), (14, -24), (18, -15),
                    (14, -4), (6, -5), (3, -14), (9, -24), (15, -31),
                    (15, -39), (11, -43)]], []),
    "9": (22, [], [[(17, -30), (10, -24), (4, -29), (6, -38), (14, -42),
                    (18, -35), (17, -20), (13, -8), (6, -2)]], []),

    # ── The four marks a name can hold ───────────────────────────────
    # Lifted for, like the figures, and for the same reason: a hyphen written
    # into the joining stroke is a joining stroke that got thicker.
    ".": (12, [], [], [(6, -2)]),
    "-": (16, [], [[(2, -16), (14, -18)]], []),
    "_": (16, [], [[(0, 6), (16, 6)]], []),
    "'": (10, [], [[(5, -58), (3, -44)]], []),
}


def _spline(points):
    """Path data for a curve that passes through every one of `points`.

    Catmull-Rom, converted segment by segment into the cubic beziers SVG
    actually draws. The ends are doubled so the first and last segments have
    the neighbour the formula wants, which is what keeps a stroke from starting
    with a straight piece."""
    if len(points) < 2:
        return ""
    pts = [points[0]] + list(points) + [points[-1]]
    out = [f"M{points[0][0]:.1f} {points[0][1]:.1f}"]
    for i in range(1, len(pts) - 2):
        (x0, y0), (x1, y1), (x2, y2), (x3, y3) = pts[i - 1:i + 3]
        c1x, c1y = x1 + (x2 - x0) / 6, y1 + (y2 - y0) / 6
        c2x, c2y = x2 - (x3 - x1) / 6, y2 - (y3 - y1) / 6
        out.append(f"C{c1x:.1f} {c1y:.1f} {c2x:.1f} {c2y:.1f} {x2:.1f} {y2:.1f}")
    return "".join(out)


def fits(text):
    """`text` with everything this module cannot draw taken out.

    Folding accents is the caller's job - embed.plain() already does it for
    every other string that goes into a picture - so anything still unknown
    here is a script these tables were never going to have, and it leaves
    rather than drawing as a box."""
    kept = [ch for ch in str(text or "") if ch in GLYPHS or ch == " "]
    return " ".join("".join(kept).split())


def width(text, size):
    """About how wide `text` will draw at cap height `size`.

    The lean adds to it: a letter skewed twelve degrees puts the top of its
    ascender most of a stroke to the right of where the advance says it ends,
    and a signature measured without that runs off its own corner."""
    k = size / ASC
    total = sum(SPACE if ch == " " else GLYPHS[ch][0]
                for ch in fits(text) if ch == " " or ch in GLYPHS)
    return (total + ASC * _LEAN) * k


def draw(text, x, y, size, colour, opacity=1.0):
    """`text` written at (x, y), the baseline, in one hand.

    Returns the SVG, or an empty string when there is nothing drawable left -
    a caller that asked for a signature of "олег" gets no signature rather than
    a row of empty boxes, and can tell the difference by the empty answer."""
    text = fits(text)
    if not text:
        return ""

    k = size / ASC
    # Every letter that joins, as one list of points. A space flushes the run
    # in hand and starts another: a pen leaves the paper between words, and a
    # curve drawn straight through the gap would tie them together.
    runs, run, extras, dots, at = [], [], [], [], 0.0
    for ch in text:
        if ch == " ":
            if run:
                runs.append(run)
            run, at = [], at + SPACE
            continue
        advance, main, more, spots = GLYPHS[ch]
        extras.extend([(px + at, py) for px, py in stroke] for stroke in more)
        dots.extend((px + at, py) for px, py in spots)
        # A figure or a mark has no joining stroke, so it ends the run it
        # landed in rather than being dragged into one.
        if not main:
            if run:
                runs.append(run)
            run, at = [], at + advance
            continue
        shifted = [(px + at, py) for px, py in main]
        # The join: this letter's first point and the last one already in hand
        # are the same place, and writing it twice is a stall in the curve.
        if run and abs(run[-1][0] - shifted[0][0]) < .01 and \
                abs(run[-1][1] - shifted[0][1]) < .01:
            shifted = shifted[1:]
        run.extend(shifted)
        at += advance
    if run:
        runs.append(run)

    # One stroke width for the whole thing, thick enough to read at the size it
    # was asked for and thin enough that a loop does not fill in.
    pen = max(1.2, ASC * .052)
    body = [f'<path d="{_spline(one)}"/>' for one in runs]
    body += [f'<path d="{_spline(one)}"/>' for one in extras]
    body += [f'<circle cx="{px:.1f}" cy="{py:.1f}" r="{pen * .78:.1f}" '
             f'fill="{colour}" stroke="none"/>' for px, py in dots]
    fade = f' opacity="{opacity:.2f}"' if opacity < 1 else ""
    return (f'<g{fade} transform="translate({x:.1f} {y:.1f}) scale({k:.4f}) '
            f'skewX({-SLANT:g})" fill="none" stroke="{colour}" '
            f'stroke-width="{pen:.2f}" stroke-linecap="round" '
            f'stroke-linejoin="round">' + "".join(body) + "</g>")


# ── The capitals that are their own lowercase, written large ─────────
# C, O, S, U, V, W, X, Z, M, N and J are the same shape at both cases in a
# monoline script - which is why they are not in the table above. Building
# them from it, rather than typing the same curve twice with every number
# multiplied, is what keeps the two cases one hand: a change to `o` is a
# change to `O`.
CAP_H = 52.0
_GROW = CAP_H / 30.0   # the x-height letters are 30 tall


def _grown(points):
    return [(round(px * _GROW, 1), round(py * _GROW, 1)) for px, py in points]


for _low, _up in (("c", "C"), ("o", "O"), ("s", "S"), ("w", "W"), ("x", "X"),
                  ("m", "M"), ("n", "N"), ("j", "J")):
    _adv, _main, _more, _dots = GLYPHS[_low]
    # The dot goes with the growing: a capital J does not carry the one its
    # lowercase does.
    GLYPHS[_up] = (round(_adv * _GROW, 1), _grown(_main),
                   [_grown(one) for one in _more], [])

# Q is the O with a tail, and is built here rather than typed out for the same
# reason the eleven above are: two hand-written copies of the same ellipse
# would stop being the same ellipse the first time one of them was touched.
_o_adv, _o_main, _o_more, _ = GLYPHS["O"]
GLYPHS["Q"] = (_o_adv, _o_main, _o_more + [[(20, -14), (29, -1), (36, 8)]], [])
