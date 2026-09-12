#!/usr/bin/env python3
"""Where the messages live. SQLite, stdlib only, one file on disk.

Nothing here identifies a person. The only trace kept of who sent what is a
salted hash of the address, and that exists to rate limit and to count one vote
per person - not to know who they are. The salt belongs in IP_SALT, and falls
back to a file beside the database; see ip_hash() for why the environment is
the right place for it and why the file cannot simply be dropped.
"""

import hashlib
import os
import re
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "/app/data"))
DB_PATH = DATA_DIR / "recados.db"
SALT_PATH = DATA_DIR / "ip-salt"

# Kinds and states are stored values, so they stay as the opaque ids they became
# - the front end maps them to a label per language.
# "appeal" is not offered by the feedback form and never will be: it is written
# only by /appeal, the one route a shut-out address can still reach. It shares
# this table because it wants exactly what the table already does - an inbox the
# owner reads, and the ip_hash of whoever wrote it, which here is not a rate
# limit but the handle bans.lift() needs.
KINDS = ("bug", "ideia", "outro", "appeal")
# Which kinds the public board is allowed to show. An allow-list and not a
# deny-list, deliberately: the board filtered on status alone until an appeal
# could be filed, and an appeal marked "recusado" would have been published
# verbatim, including whatever the sender wrote to explain themselves. Listing
# what may be shown means the next kind added here is private until somebody
# decides otherwise, instead of public until somebody remembers.
PUBLIC_KINDS = ("bug", "ideia", "outro")
STATES = ("novo", "lido", "aceito", "fazendo", "feito", "recusado")
PUBLIC_STATES = ("aceito", "fazendo", "feito", "recusado")

MAX_MESSAGE = 2000
MAX_CONTACT = 200
MAX_TITLE = 120
# The floor for "wrote something". A constant rather than a literal because two
# places ask the question now: add(), refusing the form, and appealed(), which
# decides whether a ban may be lifted at all. Those two must not drift apart.
MIN_MESSAGE = 10
# Per address, per hour. Enough for someone reporting three bugs in a row.
RATE_PER_HOUR = 5

_lock = threading.Lock()
_salt = None


class Rejected(ValueError):
    """Input the sender should see a message about, not a stack trace."""


@contextmanager
def _connect():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    # FULL, like blog.py and unlike the caches: somebody typed this. A message
    # that arrived, was answered with "recebido" and then vanished is worse than
    # a slow write, and the traffic here is a handful of messages a day.
    con.execute("PRAGMA synchronous=FULL")
    con.execute("PRAGMA foreign_keys=ON")
    try:
        with con:
            yield con
    finally:
        con.close()


def init():
    with _lock, _connect() as con:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS messages (
                id         INTEGER PRIMARY KEY,
                created_at TEXT    NOT NULL,
                kind       TEXT    NOT NULL,
                title      TEXT    NOT NULL,
                message    TEXT    NOT NULL,
                contact    TEXT,
                context    TEXT,
                status     TEXT    NOT NULL DEFAULT 'novo',
                reply      TEXT,
                ip_hash    TEXT    NOT NULL
            );
            CREATE TABLE IF NOT EXISTS votes (
                message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
                ip_hash    TEXT    NOT NULL,
                created_at TEXT    NOT NULL,
                PRIMARY KEY (message_id, ip_hash)
            );
            CREATE INDEX IF NOT EXISTS idx_status ON messages(status);
            CREATE INDEX IF NOT EXISTS idx_rate ON messages(ip_hash, created_at);
        """)


def ip_hash(address):
    """A salted, truncated digest. Enough to count once; useless as an identity.

    The salt comes from IP_SALT if it is set, and from the file beside the
    database if it is not. The environment is the one that should be set, and
    the reason is a property of the hash rather than of this function: there are
    four billion IPv4 addresses, so a salt and a table of digests in the same
    place is a table of addresses to anybody holding both. They were in the same
    place - this file wrote the salt into DATA_DIR, next to the databases it
    salts, so one copy of that directory carried both halves. IP_SALT moves one
    half somewhere a copy of the data volume does not reach.

    The file stays as the fallback, and must: it holds the salt that every
    existing vote, rate limit and appeal was hashed under. Changing the value
    would not lose the rows, it would silently orphan them - a banned address
    could no longer be matched to the appeal it filed. So the migration is to
    copy the file's value into IP_SALT, not to generate a new one.

    Unlike census.py's, this salt never rotates. A ban that expires because a
    secret was rotated is not a ban, and one vote per person stops being one
    vote per person."""
    global _salt
    if _salt is None:
        env = os.environ.get("IP_SALT", "").strip()
        if env:
            _salt = env
        else:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            if not SALT_PATH.exists():
                SALT_PATH.write_text(secrets.token_hex(32), encoding="utf-8")
            _salt = SALT_PATH.read_text(encoding="utf-8").strip()
    return hashlib.sha256(f"{_salt}:{address or '?'}".encode()).hexdigest()[:32]


def _clean(text, limit, field):
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", (text or "")).strip()
    # Collapse the runs of blank lines people paste in, keep the paragraphs.
    text = re.sub(r"\n{3,}", "\n\n", text)
    if len(text) > limit:
        raise Rejected(f"@err.too_long|field={field}|limit={limit}")
    return text


def add(kind, title, message, contact, context, address):
    if kind not in KINDS:
        raise Rejected("@err.pick_kind")
    title = _clean(title, MAX_TITLE, "@field.title")
    message = _clean(message, MAX_MESSAGE, "@field.message")
    contact = _clean(contact, MAX_CONTACT, "@field.contact")
    context = _clean(context, 300, "@field.context")
    if len(title) < 3:
        raise Rejected("@err.title_short")
    if len(message) < MIN_MESSAGE:
        raise Rejected("@err.message_short")

    who = ip_hash(address)
    since = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    with _lock, _connect() as con:
        recent = con.execute(
            "SELECT COUNT(*) FROM messages WHERE ip_hash = ? AND created_at > ?",
            (who, since),
        ).fetchone()[0]
        if recent >= RATE_PER_HOUR:
            raise Rejected(f"@err.rate|n={recent}")
        # A repeat of the exact same message is a double-submit, not a new one.
        twin = con.execute(
            "SELECT id FROM messages WHERE ip_hash = ? AND message = ? AND created_at > ?",
            (who, message, since),
        ).fetchone()
        if twin:
            return twin["id"]
        cur = con.execute(
            "INSERT INTO messages (created_at, kind, title, message, contact, context, ip_hash)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (datetime.now(timezone.utc).isoformat(timespec="seconds"), kind, title,
             message, contact or None, context or None, who),
        )
        return cur.lastrowid


def board(address=None):
    """The public board: only what was approved, newest work first.

    Carries whether this visitor already voted, so the button can say so."""
    who = ip_hash(address) if address else None
    order = ("CASE status WHEN 'fazendo' THEN 0 WHEN 'aceito' THEN 1"
             " WHEN 'feito' THEN 2 ELSE 3 END, votes DESC, m.created_at DESC")
    with _lock, _connect() as con:
        rows = con.execute(f"""
            SELECT m.id, m.created_at, m.kind, m.title, m.message, m.status, m.reply,
                   (SELECT COUNT(*) FROM votes v WHERE v.message_id = m.id) AS votes,
                   EXISTS(SELECT 1 FROM votes v WHERE v.message_id = m.id
                          AND v.ip_hash = ?) AS voted
            FROM messages m
            WHERE m.status IN ({','.join('?' * len(PUBLIC_STATES))})
              AND m.kind   IN ({','.join('?' * len(PUBLIC_KINDS))})
            ORDER BY {order}
        """, (who or "", *PUBLIC_STATES, *PUBLIC_KINDS)).fetchall()
    return [dict(r) | {"voted": bool(r["voted"])} for r in rows]


def vote(message_id, address):
    """One vote per address per message. Voting again takes it back."""
    who = ip_hash(address)
    with _lock, _connect() as con:
        row = con.execute("SELECT status, kind FROM messages WHERE id = ?",
                          (message_id,)).fetchone()
        if not row:
            raise Rejected("@err.no_message")
        # Both halves of what board() shows, checked the same way. A vote is a
        # write against a row id the caller supplies, so "it is not on the
        # board" has to be decided here too and not left to the fact that the
        # board never offered a button for it.
        if row["status"] not in PUBLIC_STATES or row["kind"] not in PUBLIC_KINDS:
            raise Rejected("@err.not_on_board")
        gone = con.execute(
            "DELETE FROM votes WHERE message_id = ? AND ip_hash = ?", (message_id, who)
        ).rowcount
        if not gone:
            con.execute(
                "INSERT INTO votes (message_id, ip_hash, created_at) VALUES (?, ?, ?)",
                (message_id, who, datetime.now(timezone.utc).isoformat(timespec="seconds")),
            )
        total = con.execute(
            "SELECT COUNT(*) FROM votes WHERE message_id = ?", (message_id,)
        ).fetchone()[0]
    return {"votes": total, "voted": not gone}


def inbox(status=None, limit=300, kinds=None):
    """Everything of the kinds asked for, for the owner.

    `kinds` exists because the panel stopped being one list. Feedback is
    triaged by status and answered; an appeal is a person asking to be let back
    in and belongs beside the ban it is about, not in a queue with bug
    reports."""
    where, args = [], []
    if status:
        where.append("m.status = ?")
        args.append(status)
    if kinds:
        where.append(f"m.kind IN ({','.join('?' * len(kinds))})")
        args.extend(kinds)
    where = ("WHERE " + " AND ".join(where)) if where else ""
    with _lock, _connect() as con:
        rows = con.execute(f"""
            SELECT m.*, (SELECT COUNT(*) FROM votes v WHERE v.message_id = m.id) AS votes
            FROM messages m {where}
            ORDER BY CASE m.status WHEN 'novo' THEN 0 ELSE 1 END, m.created_at DESC
            LIMIT ?
        """, (*args, limit)).fetchall()
        # The same filter, or the header counts things the list below does not
        # show and the panel says "3 new" over two cards.
        kwhere = f"WHERE kind IN ({','.join('?' * len(kinds))})" if kinds else ""
        counts = {r["status"]: r["n"] for r in con.execute(
            f"SELECT status, COUNT(*) AS n FROM messages {kwhere} GROUP BY status",
            tuple(kinds or ()))}
    # The hash is stripped from ordinary messages, because for those it is only
    # a rate-limit counter and handing it to the panel would invite treating it
    # as an identity. An appeal is the exception, and not by accident: it is a
    # request to lift a ban, bans.py is keyed on exactly this hash, and without
    # it the panel can read the appeal but has no way to act on it. So it is
    # carried for that one kind, where it is not an identity but the handle.
    def visible(row):
        row = dict(row)
        if row.get("kind") != "appeal":
            row.pop("ip_hash", None)
        return row

    return {
        "counts": counts,
        "total": sum(counts.values()),
        "messages": [visible(r) for r in rows],
    }


def appeals():
    """Every appeal, newest first, carrying the hash that ties it to a ban.

    Grouped by address rather than listed, by the caller: two messages from one
    address are one conversation, and the panel showing them as two unrelated
    cards was the whole complaint."""
    with _lock, _connect() as con:
        rows = con.execute("""
            SELECT id, created_at, message, contact, context, status, reply, ip_hash
            FROM messages WHERE kind = 'appeal' ORDER BY created_at DESC
        """).fetchall()
    return [dict(r) for r in rows]


def appealed(who):
    """Did this address come and say something? The only door out of a ban.

    A ban is lifted for a person who asked, and for nobody else. An address
    that never wrote sits out its two days, however harmless the paths in the
    panel happen to look, because "it looks like a false positive from here"
    is a guess and an appeal is not. This is what api.py checks before it will
    call bans.lift(), so the rule holds whatever the page in front of it says.

    The length is measured here rather than assumed from add(). It has refused
    anything shorter than MIN_MESSAGE for as long as the form has existed, but
    a row is not obliged to have arrived through today's version of it, and an
    appeal with nothing written in it is exactly the case this must not pass.
    """
    with _lock, _connect() as con:
        row = con.execute("""
            SELECT 1 FROM messages
            WHERE kind = 'appeal' AND ip_hash = ?
              AND LENGTH(TRIM(message)) >= ?
            LIMIT 1
        """, (who or "", MIN_MESSAGE)).fetchone()
    return row is not None


def update(message_id, status=None, reply=None):
    if status is not None and status not in STATES:
        raise Rejected("@err.bad_status")
    sets, args = [], []
    if status is not None:
        sets.append("status = ?")
        args.append(status)
    if reply is not None:
        sets.append("reply = ?")
        args.append(_clean(reply, 600, "@field.reply") or None)
    if not sets:
        raise Rejected("@err.nothing_to_change")
    with _lock, _connect() as con:
        cur = con.execute(f"UPDATE messages SET {', '.join(sets)} WHERE id = ?",
                          (*args, message_id))
        if not cur.rowcount:
            raise Rejected("@err.no_message")
        row = con.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
    return {k: v for k, v in dict(row).items() if k != "ip_hash"}


def remove(message_id):
    with _lock, _connect() as con:
        cur = con.execute("DELETE FROM messages WHERE id = ?", (message_id,))
        if not cur.rowcount:
            raise Rejected("@err.no_message")
    return {"deleted": message_id}
