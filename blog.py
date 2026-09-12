#!/usr/bin/env python3
"""The blog. One author, several languages, and a vote anyone can cast.

Same database as the messages and the same stdlib-only rule, but its own module
and its own tables: a post is written, revised and published, which is nothing
like a message that arrives once and gets triaged.

Two things here are worth stating up front, because they are what the shape of
the tables is for.

**Only the owner writes.** There is no author column and no per-post
permission, because there is exactly one author and pretending otherwise now
would be inventing a model before the thing it models exists. Writing happens
through the separate admin panel, which is the only door that carries the
ADMIN_TOKEN. What visitors get is the read side and the vote.

**A post is not one text.** The site speaks several languages and a post is
prose, so it cannot travel as keys the way every other string does - somebody
has to write it. `post_text` therefore holds one row per language actually
written, and `posts.origin` says which one is the original. A reader whose
language has no row gets the original plus a line saying so, rather than a
missing page or a machine translation nobody checked.

**A post has one address per language.** `/blog/7f3c9a/como-ver-jogos-ocultos`
is two halves that answer to different owners: `posts.pid` is a short permanent
id that resolves the post and never changes, and the tail is `post_text.slug`,
the title of that language written as a URL. The id is what a link is made of,
so a title corrected or a translation added later cannot break one; the tail is
what a person reads before clicking, in the language they read in. A post whose
Russian slug was never written is reachable at the id alone, which is the right
degradation - an address nobody can read beats a page nobody can open.

The vote is the same shape the message board already uses: one per salted
address hash per post, and casting it again takes it back. That is deliberately
the weakest part of this file - it counts addresses, not people - and it is why
the page says so. Accounts are the next step, and when they land the vote moves
to the account and this table keeps working for whoever is not signed in.
"""

import re
import secrets
import sqlite3
import threading
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timezone

import store

# One file, two subjects. store.py owns the connection settings and the salt;
# reusing both is what keeps a vote here countable the same way a vote on the
# board is, and keeps one database file to back up rather than two.
DB_PATH = store.DB_PATH

LANGS = ("en", "pt", "ru", "zh-cn", "zh-tw")
STATES = ("draft", "published")

MAX_TITLE = 140
MAX_LEDE = 400
MAX_BODY = 40000
MAX_TAGS = 6
MAX_TAG = 24
# A slug is a URL, so it is checked against what nginx will actually route:
# lowercase, digits and hyphens, nothing else. The pattern is repeated in
# nginx.conf on purpose - a slug that this accepts and that one refuses would
# be a post that exists and cannot be opened.
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")

# The permanent half of an address. Six characters out of an alphabet with no
# i, l, o, 0 or 1 in it: a post's id gets read off a phone screen and typed
# somewhere else, and those five are the ones that come back as something
# different. Thirty-one to the sixth is nine hundred million, against a blog
# one person writes, so the loop in _new_pid is there for correctness and will
# not run twice this decade.
PID_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"
PID_LEN = 6

_lock = threading.Lock()


@contextmanager
def _connect():
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    # FULL, deliberately, while the caches around it run on NORMAL. This is the
    # one database holding something a person typed, and nothing re-derives a
    # post that a power cut swallowed. It is also written one post at a time by
    # one author, so the fsync it costs is never in a visitor's way.
    con.execute("PRAGMA synchronous=FULL")
    con.execute("PRAGMA foreign_keys=ON")
    try:
        with con:
            yield con
    finally:
        con.close()


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def init():
    with _lock, _connect() as con:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS posts (
                id           INTEGER PRIMARY KEY,
                slug         TEXT    NOT NULL UNIQUE,
                status       TEXT    NOT NULL DEFAULT 'draft',
                origin       TEXT    NOT NULL DEFAULT 'en',
                tags         TEXT,
                created_at   TEXT    NOT NULL,
                updated_at   TEXT    NOT NULL,
                published_at TEXT
            );
            CREATE TABLE IF NOT EXISTS post_text (
                post_id INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
                lang    TEXT    NOT NULL,
                title   TEXT    NOT NULL,
                lede    TEXT,
                body    TEXT    NOT NULL,
                machine INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (post_id, lang)
            );
            CREATE TABLE IF NOT EXISTS post_votes (
                post_id    INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
                ip_hash    TEXT    NOT NULL,
                created_at TEXT    NOT NULL,
                PRIMARY KEY (post_id, ip_hash)
            );
            CREATE INDEX IF NOT EXISTS idx_post_live ON posts(status, published_at);
        """)
        # `machine`, `pid` and the per-language `slug` all arrived after the
        # tables did. CREATE TABLE IF NOT EXISTS is silent about a table that
        # already exists with fewer columns, so they are added by hand and the
        # check is the column list itself rather than a version number nobody
        # would remember to bump.
        columns = {r["name"] for r in con.execute("PRAGMA table_info(post_text)")}
        if "machine" not in columns:
            con.execute("ALTER TABLE post_text ADD COLUMN"
                        " machine INTEGER NOT NULL DEFAULT 0")
        if "slug" not in columns:
            con.execute("ALTER TABLE post_text ADD COLUMN slug TEXT")
        # Tags followed the same road as the slug, and for the same reason: they
        # are words a reader reads, not keys, so a post filed under "lançamento"
        # was showing exactly that to somebody reading the Russian text. What
        # stays on `posts` is the original's set, which is what the fallback
        # below hands to a language nobody has tagged yet.
        if "tags" not in columns:
            con.execute("ALTER TABLE post_text ADD COLUMN tags TEXT")
        if "pid" not in {r["name"] for r in con.execute("PRAGMA table_info(posts)")}:
            con.execute("ALTER TABLE posts ADD COLUMN pid TEXT")
        # Unique, and NULL is allowed through it: SQLite counts two NULLs as
        # different, which is what lets the column exist for a moment before
        # _backfill fills it in.
        con.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_post_pid ON posts(pid)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_post_text_slug ON post_text(slug)")
        _backfill(con)


def _new_pid(con):
    """A short permanent id, checked against the slugs as well as the ids.

    Both land in the same place in a URL, so they share one namespace: an id
    that collided with somebody's old slug would resolve to whichever row the
    lookup happened to try first."""
    while True:
        pid = "".join(secrets.choice(PID_ALPHABET) for _ in range(PID_LEN))
        if not con.execute("SELECT 1 FROM posts WHERE pid = ? OR slug = ?",
                           (pid, pid)).fetchone():
            return pid


def _backfill(con):
    """Addresses for the posts written before this file had any.

    The old slug is not thrown away and not moved: it stays in `posts.slug`,
    goes on resolving forever and is what every link already out there points
    at. What is added beside it is an id, and a per-language slug guessed from
    each title - the same guess the panel makes while somebody types, which is
    enough for Latin text and comes back empty for Cyrillic. Empty is left
    empty on purpose: the honest address for a Russian title nobody has
    transliterated yet is the id alone, until the panel asks the model."""
    for row in con.execute("SELECT id FROM posts WHERE pid IS NULL").fetchall():
        con.execute("UPDATE posts SET pid = ? WHERE id = ?", (_new_pid(con), row["id"]))
    # The original language keeps the address the post was published under,
    # rather than a fresh guess at it: that one is in somebody's history.
    con.execute(
        "UPDATE post_text SET slug = (SELECT slug FROM posts WHERE posts.id = post_id)"
        " WHERE slug IS NULL AND lang = (SELECT origin FROM posts WHERE posts.id = post_id)")
    for row in con.execute(
            "SELECT post_id, lang, title FROM post_text WHERE slug IS NULL").fetchall():
        guess = slugify(row["title"])
        if guess:
            con.execute("UPDATE post_text SET slug = ? WHERE post_id = ? AND lang = ?",
                        (guess, row["post_id"], row["lang"]))
    # The tags a post already had are the original's, and only the original's.
    # They are copied onto that row and deliberately not onto the other two:
    # a Russian text carrying the Portuguese tags is the thing being fixed, and
    # guessing a translation here would be inventing words nobody wrote. A
    # language with none falls back to these until somebody tags it.
    con.execute(
        "UPDATE post_text SET tags = (SELECT tags FROM posts WHERE posts.id = post_id)"
        " WHERE tags IS NULL AND lang = (SELECT origin FROM posts WHERE posts.id = post_id)")


# ── Reading ──────────────────────────────────────────────────────────

def _texts_of(con, post_id):
    return {r["lang"]: dict(r) for r in con.execute(
        "SELECT lang, title, lede, body, machine, slug, tags"
        " FROM post_text WHERE post_id = ?", (post_id,))}


def path_of(pid, slug=None):
    """The address of a post, written in one place.

    Four files need it - the index, the page, the sitemap and the preview - and
    a fifth spelling of it would be the one that goes stale."""
    return f"/blog/{pid}/{slug}" if slug else f"/blog/{pid}"


def _resolve(con, key, published_only=True):
    """The post a URL segment names, and the language that segment was written
    in, or None for a segment that carries no language.

    Three shapes answer, in the order of how old a link is likely to be: the
    id, which is what every link made from now on carries; the original slug,
    which is what the links made before this existed carry and which keeps
    working for as long as they do; and a per-language slug on its own, which
    nothing here emits but somebody will type after deleting an id by hand."""
    key = (key or "").strip().lower()
    if not SLUG_RE.match(key):
        return None, None
    live = " AND status = 'published'" if published_only else ""
    row = con.execute(f"SELECT * FROM posts WHERE pid = ?{live}", (key,)).fetchone()
    if row:
        return row, None
    row = con.execute(f"SELECT * FROM posts WHERE slug = ?{live}", (key,)).fetchone()
    if row:
        return row, None
    hit = con.execute("SELECT post_id, lang FROM post_text WHERE slug = ?"
                      " ORDER BY post_id LIMIT 1", (key,)).fetchone()
    if hit:
        row = con.execute(f"SELECT * FROM posts WHERE id = ?{live}",
                          (hit["post_id"],)).fetchone()
        if row:
            return row, hit["lang"]
    return None, None


def _pick(texts, want, origin):
    """The text this reader gets, and whether it is the one they asked for.

    Order: their language, then the original, then English, then whatever
    exists. The last two steps are for a post written only in Portuguese being
    read in Russian - the answer is the Portuguese, said out loud, and not an
    empty page."""
    for lang in (want, origin, "en", *sorted(texts)):
        if lang in texts:
            return texts[lang], lang
    return None, None


def _excerpt(text, limit=240):
    """The first paragraph, with the markers taken off.

    The index is a list of links, not a preview of the formatting, so a heading
    or a bullet in the opening lines would otherwise arrive as `## ` on the
    card. The lede is used instead whenever one was written."""
    body = (text or "").strip()
    for block in re.split(r"\n\s*\n", body):
        block = block.strip()
        if not block or block.startswith("```"):
            continue
        flat = re.sub(r"^\s*(?:[#>-]+\s*)", "", block)
        flat = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", flat)
        flat = re.sub(r"[*`]", "", flat)
        flat = re.sub(r"\s+", " ", flat).strip()
        if not flat:
            continue
        return flat if len(flat) <= limit else flat[:limit].rsplit(" ", 1)[0] + "…"
    return ""


def _tags(row):
    return [t for t in (row["tags"] or "").split(",") if t]


def _tags_of(row, text):
    """The tags this reader sees: their language's, or the original's.

    The fallback is the whole point of keeping a set on `posts` as well. A
    translation added before anybody tagged it would otherwise arrive with no
    tags at all, and an untagged post reads as a post about nothing rather than
    as a post whose tags are still in the other language."""
    return _tags(text) if (text or {}).get("tags") else _tags(row)


def _card(con, row, lang, who):
    texts = _texts_of(con, row["id"])
    text, served = _pick(texts, lang, row["origin"])
    if text is None:
        return None
    votes, voted = _tally(con, row["id"], who)
    return {
        "pid": row["pid"],
        # The address this reader should be sent to: their language's slug when
        # there is one, and the id by itself when there is not.
        "url": path_of(row["pid"], text["slug"]),
        "slug": row["slug"],
        "title": text["title"],
        "excerpt": (text["lede"] or "").strip() or _excerpt(text["body"]),
        "tags": _tags_of(row, text),
        "published_at": row["published_at"],
        "updated_at": row["updated_at"],
        "lang": served,
        "origin": row["origin"],
        "langs": sorted(texts),
        "translated": served == lang,
        "votes": votes,
        "voted": voted,
    }


def _tally(con, post_id, who):
    votes = con.execute("SELECT COUNT(*) FROM post_votes WHERE post_id = ?",
                        (post_id,)).fetchone()[0]
    voted = bool(who) and bool(con.execute(
        "SELECT 1 FROM post_votes WHERE post_id = ? AND ip_hash = ?",
        (post_id, who)).fetchone())
    return votes, voted


def published(lang="en", address=None):
    """The index: every published post, newest first."""
    who = store.ip_hash(address) if address else None
    with _lock, _connect() as con:
        rows = con.execute(
            "SELECT * FROM posts WHERE status = 'published'"
            " ORDER BY published_at DESC, id DESC").fetchall()
        cards = [_card(con, r, lang, who) for r in rows]
    return [c for c in cards if c]


def one(key, lang="en", address=None):
    """A single published post, with the two beside it.

    `key` is whatever was in the URL - an id, an old slug, a translated one -
    because a reader arrives holding a link somebody else made, and which of
    the three it is says nothing about which language they read in.

    The neighbours are read here rather than by the page, because the page
    would need the whole index to work out which two they are."""
    who = store.ip_hash(address) if address else None
    with _lock, _connect() as con:
        row, _ = _resolve(con, key)
        if not row:
            raise store.Rejected("@err.no_post")
        texts = _texts_of(con, row["id"])
        text, served = _pick(texts, lang, row["origin"])
        if text is None:
            raise store.Rejected("@err.no_post")
        votes, voted = _tally(con, row["id"], who)

        def neighbour(direction):
            cmp, order = (">", "ASC") if direction == "next" else ("<", "DESC")
            near = con.execute(
                f"SELECT * FROM posts WHERE status = 'published'"
                f" AND (published_at, id) {cmp} (?, ?)"
                f" ORDER BY published_at {order}, id {order} LIMIT 1",
                (row["published_at"], row["id"])).fetchone()
            if not near:
                return None
            side, _ = _pick(_texts_of(con, near["id"]), lang, near["origin"])
            return {"pid": near["pid"], "url": path_of(near["pid"], side["slug"]),
                    "title": side["title"]} if side else None

        return {
            "pid": row["pid"],
            # Where the reader ought to be standing. The page puts it in the
            # address bar without reloading, so a link opened in English by
            # somebody who reads Portuguese becomes a Portuguese address
            # without ever having been a Portuguese request.
            "url": path_of(row["pid"], text["slug"]),
            # Every language this post has an address in, for the page to hand
            # to the language picker and for the head to declare.
            "urls": {code: path_of(row["pid"], t["slug"])
                     for code, t in sorted(texts.items())},
            "slug": row["slug"],
            "title": text["title"],
            "lede": (text["lede"] or "").strip() or None,
            "body": text["body"],
            "tags": _tags_of(row, text),
            "published_at": row["published_at"],
            "updated_at": row["updated_at"],
            "lang": served,
            "origin": row["origin"],
            "langs": sorted(texts),
            "translated": served == lang,
            # Said out loud on the page. A translation nobody read is still
            # worth having; passing it off as written prose is not.
            "machine": bool(text["machine"]),
            "votes": votes,
            "voted": voted,
            "prev": neighbour("prev"),
            "next": neighbour("next"),
        }


def preview(key, title=None):
    """Title and blurb of one published post, for a link preview.

    Two halves of the address arrive separately because they answer different
    questions: `key` says which post - an id, an old slug, whatever the link
    was made of - and `title` is the readable tail, which says which language,
    because it is the title of exactly one of them. Neither is trusted to be
    the other: a tail that names no language at all, or none, leaves the answer
    in the post's original language.

    Whatever builds a preview runs no JavaScript and has no localStorage to
    read, so the URL is the only language it can be holding, and honouring it
    is what makes `/blog/7f3c9a/como-ver-jogos-ocultos` a Portuguese card in a
    Portuguese chat rather than an English one that happens to sit under a
    Portuguese address.

    Returns None rather than raising: a key that is a draft, a typo or a post
    that has been taken down is not an error here, it is a page with no preview
    to give, and the shell is served either way."""
    with _lock, _connect() as con:
        row, asked = _resolve(con, key)
        if not row:
            return None
        texts = _texts_of(con, row["id"])
        title = (title or "").strip().lower()
        for code, one_text in texts.items():
            if title and one_text["slug"] == title:
                asked = code
                break
        text, served = _pick(texts, asked or row["origin"], row["origin"])
        if text is None:
            return None
        return {
            "pid": row["pid"],
            "url": path_of(row["pid"], text["slug"]),
            "urls": {code: path_of(row["pid"], t["slug"])
                     for code, t in sorted(texts.items())},
            "slug": row["slug"],
            "origin": row["origin"],
            "title": text["title"],
            # 200 rather than the index's 240: this one lands in a card that
            # cuts it off itself, and being cut twice reads worse than short.
            "blurb": (text["lede"] or "").strip() or _excerpt(text["body"], 200),
            "lang": served,
            "tags": _tags_of(row, text),
            "published_at": row["published_at"],
            "updated_at": row["updated_at"],
        }


def listing():
    """Every published post's addresses and last change, for the sitemap.

    One entry per post carrying every language it exists in, rather than one
    per address: the three are the same post, and a sitemap that lists them as
    three is a sitemap asking to be read as duplicates. The original comes
    first, because it is the one the entry is filed under.

    Deliberately not `published()`: that one counts the votes and picks a text
    per post, and a crawler asking for a list of URLs needs none of it."""
    with _lock, _connect() as con:
        rows = con.execute(
            "SELECT * FROM posts WHERE status = 'published'"
            " ORDER BY published_at DESC, id DESC").fetchall()
        out = []
        for row in rows:
            urls = {r["lang"]: path_of(row["pid"], r["slug"]) for r in con.execute(
                "SELECT lang, slug FROM post_text WHERE post_id = ?", (row["id"],))}
            if not urls:
                continue
            out.append({
                "pid": row["pid"],
                "origin": row["origin"] if row["origin"] in urls else min(urls),
                "urls": urls,
                "published_at": row["published_at"],
                "updated_at": row["updated_at"],
            })
    return out


def vote(key, address):
    """One vote per address per post. Voting again takes it back.

    A draft cannot be voted on, and that is checked here and not left to the
    fact that the index never offered a button: the key arrives from the
    caller, so what is reachable has to be decided where the write happens."""
    who = store.ip_hash(address)
    with _lock, _connect() as con:
        row, _ = _resolve(con, key, published_only=False)
        if not row or row["status"] != "published":
            raise store.Rejected("@err.no_post")
        gone = con.execute(
            "DELETE FROM post_votes WHERE post_id = ? AND ip_hash = ?",
            (row["id"], who)).rowcount
        if not gone:
            con.execute(
                "INSERT INTO post_votes (post_id, ip_hash, created_at) VALUES (?, ?, ?)",
                (row["id"], who, _now()))
        total = con.execute("SELECT COUNT(*) FROM post_votes WHERE post_id = ?",
                            (row["id"],)).fetchone()[0]
    return {"votes": total, "voted": not gone}


# ── Writing, which only the owner reaches ────────────────────────────

def slugify(title):
    """A URL out of a title, or nothing.

    Nothing is a real answer and not a failure: this drops every character it
    does not recognise, so a Russian title comes back empty, and empty means
    the post is addressed by its id until somebody - the panel, asking the
    model on the owner's desk - writes a readable one. The old behaviour of
    returning "post" would have put four identical addresses on the site.

    A title that only half survives is thrown away too. "Новая запись про
    Steam" leaves "steam" behind, which is not a short address for that post,
    it is one word of it standing in for a sentence it does not mean. The id
    alone says less and claims nothing, which is the better of the two."""
    title = (title or "").strip()
    # NFKD splits an accented letter into the letter and the accent, and the
    # ascii encode then drops the accent and keeps the letter: "ação" becomes
    # "acao" rather than "a-o". Portuguese titles go through here every day, so
    # this is the common case and not the clever one. Cyrillic decomposes into
    # nothing ascii can hold and is dropped whole, which is the point below.
    folded = unicodedata.normalize("NFKD", title.lower()).encode("ascii", "ignore").decode()
    flat = re.sub(r"[^a-z0-9]+", "-", folded).strip("-")[:80]
    kept = len(flat.replace("-", ""))
    whole = sum(c.isalnum() for c in title)
    if not kept or kept * 2 < whole:
        return ""
    return flat


def _clean(text, limit, field):
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", (text or "")).strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    if len(text) > limit:
        raise store.Rejected(f"@err.too_long|field={field}|limit={limit}")
    return text


def _clean_tags(raw):
    """A tag is read, not routed, so what it may contain is a letter.

    This used to keep `[a-z0-9 -]` and drop the rest, which is the rule a slug
    needs and the wrong rule here: it turned "lançamento" into "lanamento" and
    a Russian tag into nothing at all, which is a tag deleted for being written
    in the language of the text it is on. `isalnum` is per character and knows
    every alphabet, so the comma stays the separator and the accent stays on
    the word."""
    seen = []
    for tag in re.split(r"[,\n]", raw or ""):
        tag = "".join(c for c in tag.lower().strip()
                      if c.isalnum() or c in " -")[:MAX_TAG].strip()
        # Two spaces where a stripped character used to be would otherwise
        # survive as a hole in the middle of the word.
        tag = re.sub(r"\s{2,}", " ", tag)
        if tag and tag not in seen:
            seen.append(tag)
    if len(seen) > MAX_TAGS:
        raise store.Rejected(f"@err.too_many_tags|n={MAX_TAGS}")
    return ",".join(seen)


def _clean_texts(raw):
    """What was actually written, per language.

    A language with neither a title nor a body was simply not written and is
    dropped rather than stored empty; one with only half of the pair is a
    mistake worth saying out loud, because it would publish a titled post with
    no text in it."""
    out = {}
    for lang in LANGS:
        one_lang = (raw or {}).get(lang) or {}
        title = _clean(one_lang.get("title"), MAX_TITLE, "@field.title")
        lede = _clean(one_lang.get("lede"), MAX_LEDE, "@field.lede")
        body = _clean(one_lang.get("body"), MAX_BODY, "@field.body")
        if not title and not body:
            continue
        if not title or len(body) < 10:
            raise store.Rejected(f"@err.half_written|lang={lang}")
        # The readable half of this language's address. Whatever the panel sent
        # is trimmed to what a URL can hold rather than refused: it arrives from
        # a model most of the time, and a slug with a comma in it is a model
        # being a model, not the owner making a mistake worth stopping for.
        slug = re.sub(r"[^a-z0-9-]+", "-", (one_lang.get("slug") or "").lower().strip())
        slug = re.sub(r"-{2,}", "-", slug).strip("-")[:80].strip("-")
        # Falling back to the title is what makes the field optional: type an
        # English post, save it, and the address reads like the title without
        # anyone having asked for that.
        if not slug:
            slug = slugify(title)
        # Whether the reader is owed a warning about this text. Set by the
        # panel's translate button and cleared by the owner reading it, which
        # is a claim only a person can make - so it travels with the text
        # rather than being inferred from anything here.
        # This language's own tags. Optional, and empty means "not tagged in
        # this language yet" rather than "untagged": the read side falls back
        # to the original's set, so a fresh translation is never a post filed
        # under nothing.
        out[lang] = {"title": title, "lede": lede, "body": body,
                     "slug": slug or None,
                     "tags": _clean_tags(one_lang.get("tags")) or None,
                     "machine": bool(one_lang.get("machine"))}
    if not out:
        raise store.Rejected("@err.blog_empty")
    return out


def save(post_id, slug, status, origin, tags, texts):
    """Create or update. `post_id` empty means create.

    `slug` here is the post's fixed address, the one that predates the id and
    is now optional: a post saved without one is addressed by its id and by its
    per-language slugs, and nothing is lost. It is still accepted, and still
    unique, because the posts written before this change have one and it is in
    somebody's bookmarks.

    `published_at` is set the first time a post goes live and never moved
    afterwards. Editing a published post updates `updated_at` instead, so a
    typo fixed a month later does not send the post back to the top of the
    index as though it were new."""
    if status not in STATES:
        raise store.Rejected("@err.bad_status")
    if origin not in LANGS:
        raise store.Rejected("@err.bad_lang")
    slug = (slug or "").strip().lower()
    if slug and not SLUG_RE.match(slug):
        raise store.Rejected("@err.bad_slug")
    tags = _clean_tags(tags)
    texts = _clean_texts(texts)
    if origin not in texts:
        raise store.Rejected("@err.origin_missing")
    # What `posts.tags` holds from here on is the original's set, and it is
    # there as the fallback for a language nobody has tagged. The argument is
    # still honoured for a panel that has not been updated yet, and loses to
    # the origin's own field the moment one is sent.
    tags = texts[origin]["tags"] or tags

    now = _now()
    with _lock, _connect() as con:
        # Against the ids as well, for the reason _new_pid checks the slugs:
        # one namespace, because a URL has one slot for either of them.
        if slug and con.execute(
                "SELECT 1 FROM posts WHERE (slug = ? OR pid = ?) AND id IS NOT ?",
                (slug, slug, post_id)).fetchone():
            raise store.Rejected("@err.slug_taken")

        if post_id:
            row = con.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
            if not row:
                raise store.Rejected("@err.no_post")
            pid = row["pid"] or _new_pid(con)
            published_at = row["published_at"]
            if status == "published" and not published_at:
                published_at = now
            con.execute(
                "UPDATE posts SET pid = ?, slug = ?, status = ?, origin = ?, tags = ?,"
                " updated_at = ?, published_at = ? WHERE id = ?",
                (pid, slug or pid, status, origin, tags, now, published_at, post_id))
        else:
            pid = _new_pid(con)
            cur = con.execute(
                "INSERT INTO posts (pid, slug, status, origin, tags, created_at,"
                " updated_at, published_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (pid, slug or pid, status, origin, tags, now, now,
                 now if status == "published" else None))
            post_id = cur.lastrowid

        # Replaced whole rather than merged: a language cleared in the panel is
        # a translation withdrawn, and a merge would leave the old text live
        # with no way to take it down.
        con.execute("DELETE FROM post_text WHERE post_id = ?", (post_id,))
        for lang, text in texts.items():
            con.execute(
                "INSERT INTO post_text"
                " (post_id, lang, title, lede, body, machine, slug, tags)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (post_id, lang, text["title"], text["lede"] or None, text["body"],
                 int(text["machine"]), text["slug"], text["tags"]))
    return {"id": post_id, "pid": pid, "slug": slug or pid, "status": status,
            "url": path_of(pid, (texts.get(origin) or {}).get("slug"))}


def remove(post_id):
    with _lock, _connect() as con:
        cur = con.execute("DELETE FROM posts WHERE id = ?", (post_id,))
        if not cur.rowcount:
            raise store.Rejected("@err.no_post")
    return {"deleted": post_id}


def everything():
    """Every post, drafts included, with all translated texts. The panel's payload.

    It carries the whole body of every language, which is the one place on this
    site where that is the right thing to send: the editor has to be able to
    open a post without a second request per language, and this answer only
    ever leaves the api container towards the owner's own panel."""
    with _lock, _connect() as con:
        rows = con.execute(
            "SELECT * FROM posts ORDER BY"
            " CASE status WHEN 'draft' THEN 0 ELSE 1 END,"
            " COALESCE(published_at, updated_at) DESC").fetchall()
        posts = []
        for row in rows:
            votes, _ = _tally(con, row["id"], None)
            texts = _texts_of(con, row["id"])
            posts.append(dict(row) | {
                "tags": _tags(row),
                "texts": texts,
                # The address the panel's own "open it" link uses, so the one
                # place that builds it is still path_of.
                "url": path_of(row["pid"], (texts.get(row["origin"]) or {}).get("slug")),
                "votes": votes,
            })
        counts = {r["status"]: r["n"] for r in con.execute(
            "SELECT status, COUNT(*) AS n FROM posts GROUP BY status")}
    return {"posts": posts, "counts": counts, "total": len(posts)}
