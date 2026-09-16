"""Bounded, private request evidence for security investigations.

nginx sends completed requests over internal UDP syslog. Raw addresses and
URLs are processed transiently; only salted identifiers and scrubbed evidence
are retained. This is independent of census and never changes access decisions.
"""
import ipaddress
import json
import math
import os
import re
import socket
import sqlite3
import sys
import threading
import time
from collections import deque
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit

import census
import store

DATA_DIR = Path(os.environ.get("DATA_DIR", "/app/data"))
DB_PATH = DATA_DIR / "security.db"
RETENTION_DAYS = max(1, min(30, int(os.environ.get("SECURITY_RETENTION_DAYS", "7"))))
MAX_EVENTS = max(100, min(100000, int(os.environ.get("SECURITY_MAX_EVENTS", "20000"))))
MAX_PENDING = 2000
PORT = 1514  # Internal compose network only; never publish this UDP port.
SIGNALS = (
    ("traversal", re.compile(r"\.\.[/\\]|/etc/(passwd|shadow)|/proc/self", re.I)),
    ("sql", re.compile(r"union\s+(all\s+)?select|sleep\s*\(|waitfor\s+delay|or\s+1\s*=\s*1", re.I)),
    ("xss", re.compile(r"<\s*script|javascript:|onerror\s*=|onload\s*=", re.I)),
    ("execution", re.compile(r"(?:\b(?:cmd|exec|eval|system|shell_exec)\s*[=(]|\$\{|/bin/(?:sh|bash)|jndi:)", re.I)),
    ("secrets", re.compile(r"(?:\.env|/\.git|/\.aws|/\.ssh|secrets?\.(?:json|ya?ml)|\.sql(?:$|[?&]))", re.I)),
    ("wordpress", re.compile(r"wp-admin|wp-login|wp-content|xmlrpc", re.I)),
    ("admin", re.compile(r"phpmyadmin|/actuator|/jenkins|/solr|/admin(?:/|$|\?)", re.I)),
)
SENSITIVE_KEY = re.compile(r"token|secret|password|passwd|auth|cookie|credential|api.?key|session|email|contact|^(?:q|id|who|user|steamid|vs|key)$", re.I)
SECRET_TEXT = re.compile(r"\b(?:Bearer\s+\S+|(?:token|password|secret|api[_-]?key|authorization|cookie)\s*[=:]\s*[^\s&;,]+)", re.I)
EMAIL = re.compile(r"[\w.+-]+@[\w.-]+\.[a-z]{2,}", re.I)
RESOURCE_PATH = re.compile(r"\.(?:css|js|mjs|json|map|svg|png|jpe?g|gif|webp|ico|woff2?|ttf|otf|mp4|webm|txt|xml)$", re.I)
VISIT_IDLE = 30 * 60
_lock = threading.Lock()
_write_lock = threading.Lock()
_pending = deque()
_dropped = 0
_malformed = 0
_last_received = 0
_running = False


def decoded(text):
    for _ in range(3):
        new = unquote(text)
        if new == text:
            break
        text = new
    return text


def scrub(text, limit=500):
    text = str(text or "")
    text = SECRET_TEXT.sub("[redacted]", text)
    text = re.sub(r"(?i)(/(?:tokens?|reset-password|sessions?|api[-_]?keys?)/)[^/?#]+", r"\1[redacted]", text)
    text = re.sub(r"\b(?:gh[pousr]_|sk-)[A-Za-z0-9_-]{20,}", "[redacted]", text)
    text = EMAIL.sub("[redacted]", text)
    text = re.sub(r"\b\d{17}\b", "[profile]", text)
    text = re.sub(r"[\x00-\x1f\x7f]", " ", text)
    return text[:limit]


def signals(text):
    return [name for name, pattern in SIGNALS if pattern.search(decoded(text).replace("+", " "))]


def safe_uri(uri):
    # Keep the original target, including repeated slashes and unusual paths.
    raw_path, _, raw_query = uri[:8192].partition("?")
    raw_path = re.sub(r"(://)[^/@]*@", r"\1[redacted]@", raw_path)
    path = scrub(decoded(raw_path), 500)
    path = re.sub(r"(?i)(/u/)[^/]+", r"\1[profile]", path)
    path = re.sub(r"(?i)(/vs/)[^/]+", r"\1[profile]", path)
    params = []
    for key, value in parse_qsl(raw_query, keep_blank_values=True):
        key = scrub(key, 80)
        # Retain query evidence without deciding whether it looks suspicious.
        if SENSITIVE_KEY.search(key):
            value = "[redacted]"
        else:
            value = scrub(decoded(value), 300)
        params.append((key, value))
    query = urlencode(params)
    return (path + ("?" + query if query else ""))[:1500]


def event(raw):
    """Sanitise every completed request; labels never decide collection."""
    if not isinstance(raw, dict):
        raise ValueError("invalid event")
    request = str(raw.get("request", ""))
    parts = request.split(" ", 2)
    method, uri = (parts[0], parts[1]) if len(parts) >= 2 else ("?", request)
    address = str(ipaddress.ip_address(raw["address"]))
    status = int(raw["status"])
    if not 100 <= status <= 599:
        raise ValueError("invalid status")
    ua = str(raw.get("ua", ""))[:2048]
    evidence = signals(uri)
    kind = ("scanner" if evidence or census.SCAN_PATH.search(decoded(uri)) else
            "ai" if census.AI_UA.search(ua) else
            "search" if census.SEARCH_UA.search(ua) else
            "tool" if census.TOOL_UA.search(ua) else "unknown")
    if status == 429:
        evidence.append("rate")
    duration = float(raw.get("duration", 0))
    if not math.isfinite(duration) or duration < 0:
        raise ValueError("invalid duration")
    at = float(raw.get("at") or time.time())
    if not math.isfinite(at):
        raise ValueError("invalid timestamp")
    content_type = str(raw.get("type", "")).lower()
    dest = str(raw.get("dest", ""))
    document = is_document(uri, dest, content_type)
    page = uri if document else local_page(raw.get("referer", ""), raw.get("host", ""))
    return {
        "at": at, "actor": store.ip_hash(address),
        "kind": kind, "signals": evidence, "method": scrub(method, 80),
        "uri": safe_uri(uri), "status": status,
        "duration_ms": round(min(duration, 3600) * 1000),
        "bytes": max(0, min(2**40, int(raw.get("bytes", 0)))),
        "country": scrub(raw.get("country"), 8), "ua": scrub(ua),
        "page": safe_uri(page) if page else "",
        "context": store.ip_hash("screen:" + decoded(page)) if page else "",
        "document": int(document), "start_at": at - min(duration, 3600),
        "category": category(uri, document),
    }


def category(uri, document=False):
    path = uri.partition("?")[0]
    if document:
        return "document"
    if path.startswith("/api/"):
        return "api"
    if RESOURCE_PATH.search(path) or path.startswith(("/assets/", "/fonts/", "/art/")):
        return "asset"
    return "other"


def is_document(uri, dest="", content_type=""):
    if category(uri) != "other" or uri.partition("?")[0] in ("/healthz", "/_gate"):
        return False
    if dest in ("document", "iframe"):
        return True
    if dest:
        return False
    path = decoded(uri.partition("?")[0]).replace("[profile]", "hidden")
    return bool(census.screen_of(path)[0] or "text/html" in content_type)


def local_page(referer, host):
    """Only keep a page on the receiving site, never an external referrer."""
    if not referer or not host:
        return ""
    try:
        parts = urlsplit(str(referer)[:8192])
        if parts.scheme not in ("http", "https") or parts.netloc.lower() != str(host).lower():
            return ""
        page = parts.path + ("?" + parts.query if parts.query else "")
        return page if is_document(page) else ""
    except ValueError:
        return ""


@contextmanager
def _connect():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    try:
        with con:
            yield con
    finally:
        con.close()


def init():
    with _connect() as con:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS requests (
                id INTEGER PRIMARY KEY,
                at REAL NOT NULL, actor TEXT NOT NULL, kind TEXT NOT NULL,
                signals TEXT NOT NULL, method TEXT NOT NULL, uri TEXT NOT NULL,
                status INTEGER NOT NULL, duration_ms INTEGER NOT NULL,
                bytes INTEGER NOT NULL, country TEXT NOT NULL, ua TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS requests_at ON requests(at);
            CREATE INDEX IF NOT EXISTS requests_actor ON requests(actor, id);
        """)
        columns = {row[1] for row in con.execute("PRAGMA table_info(requests)")}
        for name, declaration in (("page", "TEXT NOT NULL DEFAULT ''"),
                                  ("context", "TEXT NOT NULL DEFAULT ''"),
                                  ("document", "INTEGER NOT NULL DEFAULT -1"),
                                  ("start_at", "REAL NOT NULL DEFAULT 0"),
                                  ("category", "TEXT NOT NULL DEFAULT ''")):
            if name not in columns:
                con.execute(f"ALTER TABLE requests ADD COLUMN {name} {declaration}")
        _prune(con)


def _prune(con):
    con.execute("DELETE FROM requests WHERE at < ?", (time.time() - RETENTION_DAYS * 86400,))
    con.execute("DELETE FROM requests WHERE id <= (SELECT id FROM requests ORDER BY id DESC LIMIT 1 OFFSET ?)", (MAX_EVENTS,))


def ingest(packet):
    global _dropped, _malformed, _last_received
    try:
        # nginx's tag marks the JSON boundary, even when a URL contains '{'.
        payload = packet.decode("utf-8", errors="replace").split("spsecurity: ", 1)[1]
        item = event(json.loads(payload))
    except (ValueError, KeyError, TypeError, IndexError, UnicodeError, OverflowError):
        with _lock:
            _malformed += 1
        return
    with _lock:
        _last_received = time.time()
        if len(_pending) >= MAX_PENDING:
            _dropped += 1
            return
        _pending.append(item)


def flush():
    with _write_lock:
        with _lock:
            items = list(_pending)
            _pending.clear()
        try:
            with _connect() as con:
                con.executemany("""INSERT INTO requests
                    (at, actor, kind, signals, method, uri, status, duration_ms, bytes, country, ua,
                     page, context, document, start_at, category)
                    VALUES (:at,:actor,:kind,:signals,:method,:uri,:status,:duration_ms,:bytes,:country,:ua,
                            :page,:context,:document,:start_at,:category)
                """, [{**item, "signals": json.dumps(item["signals"])} for item in items])
                _prune(con)
        except Exception:
            global _dropped
            with _lock:
                room = MAX_PENDING - len(_pending)
                _pending.extendleft(reversed(items[:room]))
                _dropped += max(0, len(items) - room)
            raise


def reset():
    """Clear stored and pending evidence while keeping collection active."""
    # Use the same lock order as flush so an older batch cannot come back
    # after deletion. Pending evidence survives if the transaction fails.
    with _write_lock:
        with _lock:
            with _connect() as con:
                con.execute("DELETE FROM requests")
            _pending.clear()


def validate_filters(kind, actor, status, before):
    if kind not in ("", "scanner", "tool", "ai", "search", "unknown"):
        raise ValueError("invalid kind")
    if actor and not re.fullmatch(r"[a-f0-9]{8,64}", actor):
        raise ValueError("invalid actor")
    if status not in ("", "2xx", "3xx", "4xx", "5xx"):
        raise ValueError("invalid status")
    if not 0 <= before < 2**63:
        raise ValueError("invalid cursor")


def validate_excluded(value):
    values = [part for part in re.split(r"[\s,]+", str(value or "")) if part]
    if len(values) > 50 or any(not re.fullmatch(r"[a-f0-9]{8,64}", part) for part in values):
        raise ValueError("invalid excluded actor")
    return set(values)


def collection_state():
    with _lock:
        return {"retention_days": RETENTION_DAYS, "max_events": MAX_EVENTS,
                "running": _running, "last_received": _last_received,
                "dropped": _dropped, "malformed": _malformed}


def report(kind="", actor="", status="", q="", before=0):
    validate_filters(kind, actor, status, before)
    flush()
    clauses, args = [], []
    for name, value in (("kind", kind), ("actor", actor)):
        if value:
            clauses.append(f"{name} = ?")
            args.append(value)
    if status:
        clauses.append("status BETWEEN ? AND ?")
        args.extend((int(status[0]) * 100, int(status[0]) * 100 + 99))
    if q:
        clauses.append("(instr(lower(uri), ?) > 0 OR instr(lower(ua), ?) > 0)")
        args.extend((q[:100].lower(), q[:100].lower()))
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    with _connect() as con:
        total = con.execute("SELECT COUNT(*) FROM requests" + where, args).fetchone()[0]
        cursor_where = where + (" AND " if where else " WHERE ") + "id < ?" if before else where
        rows = [dict(row) for row in con.execute(
            "SELECT * FROM requests" + cursor_where + " ORDER BY id DESC LIMIT 101",
            [*args, before] if before else args)]
        actors = [dict(row) for row in con.execute("SELECT actor, COUNT(*) AS requests, MAX(at) AS last_seen FROM requests" + where + " GROUP BY actor ORDER BY requests DESC LIMIT 20", args)]
    has_more = len(rows) > 100
    rows = rows[:100]
    for row in rows:
        row["signals"] = json.loads(row["signals"])
    return {"items": rows, "total": total, "actors": actors,
            "next_before": rows[-1]["id"] if has_more else None, **collection_state()}


def screens_report(kind="", actor="", status="", q="", before=0, visit=0, unassigned=False, exclude=""):
    """One visit per document, with related client requests in its details.

    Related requests must provide a same-site page referrer and the same
    origin/UA. We never guess a page from the last request made by an IP.
    Sorting by request start handles assets that finish before their document.
    """
    validate_filters(kind, actor, status, before)
    excluded = validate_excluded(exclude)
    if not 0 <= visit < 2**63:
        raise ValueError("invalid visit")
    flush()
    with _connect() as con:
        rows = [dict(row) for row in con.execute("SELECT * FROM requests ORDER BY CASE WHEN start_at = 0 THEN at - duration_ms / 1000.0 ELSE start_at END, id")]
    groups, active, orphans = [], {}, []
    for row in rows:
        row["signals"] = json.loads(row["signals"])
        document = bool(row["document"]) if row["document"] >= 0 else is_document(row["uri"])
        row["category"] = row["category"] or category(row["uri"], document)
        page = row["page"] or (row["uri"] if document else "")
        if not page:
            orphans.append(row)
            continue
        context = row["context"] or store.ip_hash("screen:" + decoded(page))
        key = row["actor"], row["ua"], context
        start = row["start_at"] or row["at"] - row["duration_ms"] / 1000
        group = active.get(key)
        if document or not group or start - group["last_seen"] > VISIT_IDLE:
            group = {"id": row["id"], "at": row["at"], "start_at": start,
                     "last_seen": row["at"], "uri": page, "actor": row["actor"],
                     "ua": row["ua"], "country": row["country"], "kind": row["kind"],
                     "status": row["status"] if document else None,
                     "document_present": document, "requests": []}
            groups.append(group)
            active[key] = group
        group["last_seen"] = max(group["last_seen"], row["at"])
        group["requests"].append(row)

    def matches(row, page=""):
        return (row["actor"] not in excluded) and (not kind or row["kind"] == kind) and (not actor or row["actor"] == actor) and (
            not status or row["status"] // 100 == int(status[0])) and (
            not q or q[:100].lower() in (row["uri"] + " " + row["ua"] + " " + page).lower())

    def page_of(requests):
        requests = [row for row in requests if row["actor"] not in excluded]
        requests = sorted(requests, key=lambda row: row["id"], reverse=True)
        total = len(requests)
        selected = [row for row in requests if not before or row["id"] < before][:101]
        has_more = len(selected) > 100
        selected = selected[:100]
        return {"items": selected, "total": total,
                "next_before": selected[-1]["id"] if has_more else None, **collection_state()}

    if unassigned:
        return page_of([row for row in orphans if matches(row)])
    if visit:
        group = next((group for group in groups if group["id"] == visit), None)
        return page_of(group["requests"] if group else [])
    groups = [group for group in groups if any(matches(row, group["uri"]) for row in group["requests"])]
    groups.sort(key=lambda group: group["id"], reverse=True)
    total = len(groups)
    selected = [group for group in groups if not before or group["id"] < before][:101]
    has_more = len(selected) > 100
    selected = selected[:100]
    items = []
    for group in selected:
        requests = group["requests"]
        items.append({key: value for key, value in group.items() if key != "requests"} | {
            "request_count": len(requests), "bytes": sum(row["bytes"] for row in requests),
            "span_ms": max(0, round((group["last_seen"] - group["start_at"]) * 1000)),
            "errors": sum(row["status"] >= 400 for row in requests),
            "counts": {cat: sum(row["category"] == cat for row in requests) for cat in ("document", "api", "asset", "other")},
            "signals": sorted({signal for row in requests for signal in row["signals"]})})
    return {"items": items, "total": total,
            "request_total": sum(row["actor"] not in excluded for row in rows),
            "unassigned_total": sum(matches(row) for row in orphans),
            "next_before": items[-1]["id"] if has_more else None, **collection_state()}


def start():
    global _running
    init()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", PORT))
    sock.settimeout(1)
    _running = True

    def receive():
        while True:
            try:
                packet, _ = sock.recvfrom(65535)
                ingest(packet)
            except socket.timeout:
                pass

    def write():
        while True:
            time.sleep(5)
            try:
                flush()
            except Exception as exc:
                print(f"security log: flush failed ({type(exc).__name__})", file=sys.stderr)

    threading.Thread(target=receive, name="security-receive", daemon=True).start()
    threading.Thread(target=write, name="security-write", daemon=True).start()
