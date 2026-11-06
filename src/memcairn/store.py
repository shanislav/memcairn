"""Memory store: all reads and writes go through here."""

from __future__ import annotations

import bisect
import contextlib
import functools
import json
import logging
import math
import os
import re
import sqlite3
import threading
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import secretscan, temporal
from .db import now

log = logging.getLogger(__name__)

MEMORY_TYPES = ("user", "feedback", "project", "reference", "fact", "todo", "decision", "howto")
TODO_STATES = ("open", "done", "dropped")

# How much each memory type deserves a place in the session-start context.
TYPE_PRIOR = {"feedback": 1.0, "user": 1.0, "decision": 0.85, "project": 0.8, "todo": 0.75,
              "howto": 0.7, "reference": 0.6, "fact": 0.5}
LONG_CONTENT = 500

# Entity type aliases folded together on write (lowercased keys).
TYPE_ALIASES = {
    "projekt": "project",
    "web/projekt": "project",
    "servers": "server",
    "configuration": "config",
    "rules": "rule",
    "pravidlo": "rule",
    "pravidla": "rule",
    "zistenie": "finding",
    "infrastruktura": "infrastructure",
    "architektura": "architecture",
    "claude instancia": "claude-instance",
}


class NotFound(LookupError):
    pass


class Conflict(RuntimeError):
    pass


@dataclass
class Hit:
    memory: sqlite3.Row
    entity: str | None
    score: float
    parts: dict[str, float] = field(default_factory=dict)

    @property
    def status(self) -> str | None:
        m = self.memory
        if m["deleted_at"]:
            return "forgotten"
        if m["status"] in ("done", "dropped"):
            return m["status"]
        if m["superseded_by"]:
            return "old"
        t = now()
        if m["expires_at"] and m["expires_at"] <= t:
            return "expired"
        if m["valid_to"] and m["valid_to"] <= t:
            return "ended"
        return None


@dataclass
class RecallResult:
    hits: list[Hit]
    coverage: float  # confidence: best relevance (0-1) of any single hit
    chunks: list[DocHit] = field(default_factory=list)
    note: str = ""  # e.g. that the query's date was widened


@dataclass
class DocHit:
    """A matching part of a shared document (section or table row)."""
    doc: str
    anchor: str
    path: str
    kind: str
    content: str
    score: float
    parts: dict[str, float] = field(default_factory=dict)


# Cosine similarity mapped to 0..1 relevance; calibrated on the benchmark for the
# default multilingual model (unrelated text scores around the floor).
SIM_FLOOR = 0.40
# Calibrated on real data with the default model: real duplicates and "same fact, newer
# value" pairs score 0.85-0.90; below that it's mostly related-but-different facts.
DUP_SIMILARITY = 0.86
SIM_CEIL = 0.75


def fold(text: str) -> str:
    """Lowercase and strip diacritics, as the FTS tokenizer does: marks on Latin, Greek or
    Cyrillic letters go (é -> e), while marks that are part of a letter in scripts without
    spaces stay (Japanese ど is not と, Thai vowels are letters)."""
    nfkd = unicodedata.normalize("NFKD", text.lower())
    out: list[str] = []
    for c in nfkd:
        if unicodedata.combining(c) and not (out and UNSPACED.match(out[-1])):
            continue
        out.append(c)
    return unicodedata.normalize("NFC", "".join(out))


# Function words that carry no topic (sk/cs/en); dropped from queries, not from stored text.
STOPWORDS = frozenset("""
a aj ako ale alebo by co ci do ho je jak jaky ja k kde kdy ked ktory ktora ktore ku mi na nad
nie o od po pod pre preco proc pri s sa si so su ta tak tam ten to tu uz v ve vo z za ze zo
an and are can do does for from how in is it of on or the this to was what when where which
who why with
""".split())


# Words that name the same thing (folded prefixes; sk/cs/en). A query word that starts with
# one member also matches the others, in FTS and in coverage. Kept small on purpose: each
# group must be the same thing, not merely related. More groups: one per line, members
# separated by spaces, in $MEMCAIRN_SYNONYMS (a file).
SYNONYMS = [
    "ilo bmc ipmi idrac ilom imm",
    "zaloh backup",
    "hesl password passw",
    "nasad deploy",
    "databaz database",
    "postgres pgsql",
    "certifik certificate cert",
    "tlaciar printer",
]


@functools.cache
def _synonym_groups() -> tuple[tuple[str, ...], ...]:
    lines = list(SYNONYMS)
    path = os.environ.get("MEMCAIRN_SYNONYMS")
    if path:
        with contextlib.suppress(OSError):
            lines += Path(path).read_text(encoding="utf-8").splitlines()
    return tuple(tuple(fold(w) for w in line.split()) for line in lines
                 if line.strip() and not line.lstrip().startswith("#"))


@functools.cache
def alternatives(term: str) -> tuple[str, ...]:
    """The term plus the members of its synonym group (prefixes)."""
    out = [term]
    for group in _synonym_groups():
        if any(term.startswith(m) if len(m) >= 3 else term == m for m in group):
            out += [m for m in group if m not in out]
    return tuple(out)


def covered(q: str, words: set[str], stems: set[str]) -> bool:
    """Does a text with these words match query word q (prefix, stem or a synonym)?"""
    for alt in alternatives(q):
        if stem(alt) in stems or any(t.startswith(alt) or t.startswith(stem(alt)) for t in words):
            return True
    return False


def query_terms(text: str) -> list[str]:
    all_terms = terms(text)
    content = [t for t in all_terms if t not in STOPWORDS]
    return content or all_terms


# Scripts written without spaces between words: Thai, Japanese kana, CJK ideographs, Hangul.
UNSPACED = re.compile(r"[฀-๿぀-ヿ㐀-䶿一-鿿가-힯"
                      r"豈-﫿]+")


def split_unspaced(text: str) -> str:
    """'报销系统' -> ' 报销 销系 系统 ': in scripts without spaces, overlapping character
    pairs stand in for words. Applied to indexed text and queries alike, so full-text search
    and term coverage work there too."""
    def pairs(m: re.Match) -> str:
        s = m.group(0)
        return " " + (s if len(s) == 1 else " ".join(s[i:i + 2] for i in range(len(s) - 1))) + " "
    return UNSPACED.sub(pairs, text)


def terms(text: str, limit: int = 16) -> list[str]:
    seen: list[str] = []
    for t in re.findall(r"\w+", split_unspaced(fold(text))):
        if (len(t) >= 2 or UNSPACED.match(t)) and t not in seen:
            seen.append(t)
    return seen[:limit]


def stem(term: str) -> str:
    # Crude, language-agnostic: drop inflection endings of longer words (obedu -> obed).
    return term[: max(4, len(term) - 2)] if len(term) >= 5 else term


def normalize_type(t: str | None) -> str:
    if not t:
        return "thing"
    key = t.strip().lower()
    return TYPE_ALIASES.get(key, key)


IPV4 = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")


def different_hosts(a: str, b: str) -> bool:
    """Both texts name IP addresses and share none: likely two machines on one template
    ("Replica1: .211 | MySQL" vs "Replica2: .212 | MySQL"), not one fact said twice."""
    ips_a, ips_b = set(IPV4.findall(a)), set(IPV4.findall(b))
    return bool(ips_a and ips_b and not ips_a & ips_b)


def _desc(day: str) -> str:
    """Sort key that orders ISO dates newest first (empty = unknown = last)."""
    return "".join(chr(0x7F - ord(c)) for c in day) if day else chr(0x7F)


def parse_id(ref: str | int) -> int:
    s = str(ref).strip().lower().lstrip("m#")
    if not s.isdigit():
        raise NotFound(f"bad memory id: {ref}")
    return int(s)


class _Rows:
    """A finished statement: all its rows, plus lastrowid and rowcount."""
    __slots__ = ("_rows", "lastrowid", "rowcount")

    def __init__(self, cur: sqlite3.Cursor):
        self._rows = cur.fetchall()
        self.lastrowid, self.rowcount = cur.lastrowid, cur.rowcount

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list:
        return self._rows

    def __iter__(self):
        return iter(self._rows)


class SerialConnection:
    """The one SQLite connection, shared by the server's threads (each MCP tool call runs
    in its own) and its event loop. A sqlite3 connection must not be used by two threads
    at the same moment ("bad parameter or other API misuse"), so every statement runs
    under the store's lock and is read to the end inside it. The lock is the one write
    transactions hold, so a statement never lands in the middle of someone's write."""

    def __init__(self, conn: sqlite3.Connection, lock):
        self._conn, self._lock = conn, lock

    def execute(self, sql: str, params=()) -> _Rows:
        with self._lock:
            return _Rows(self._conn.execute(sql, params))

    def executemany(self, sql: str, seq) -> _Rows:
        with self._lock:
            return _Rows(self._conn.executemany(sql, seq))

    def __getattr__(self, name):
        return getattr(self._conn, name)


class Store:
    def __init__(self, conn: sqlite3.Connection, embedder=None):
        self.lock = threading.RLock()
        self.conn = SerialConnection(conn, self.lock)
        self.embedder = embedder
        self.defer_vectors = False
        self._active_days: dict[int, list[str]] | None = None
        self.todos_left = 0  # set by context(max_todos=...)
        self.vindex = None
        self._dirty: set[int] = set()
        if embedder is not None:
            from .vectors import VectorIndex

            self.vindex = VectorIndex()
            self.vindex.load(self.conn.execute(
                """SELECT v.memory_id, v.vec FROM memory_vec v JOIN memory m
                   ON m.id = v.memory_id WHERE v.model = ? AND m.deleted_at IS NULL""",
                (embedder.model,)).fetchall())
        self._split_unspaced_once()

    def _split_unspaced_once(self) -> None:
        """Databases from before 0.2 indexed Chinese, Japanese, Thai… text as one word per
        run of characters; re-index those entries once with character pairs."""
        if self.conn.execute("SELECT 1 FROM setting WHERE key = 'index_unspaced'").fetchone():
            return
        with self._tx():
            for r in self.conn.execute("SELECT id, content FROM memory").fetchall():
                if UNSPACED.search(r["content"]):
                    self._put_fts(self._row(r["id"]))
            for c in self.conn.execute(
                    "SELECT id, content, path, doc_name FROM doc_chunk").fetchall():
                if UNSPACED.search(c["content"]) or UNSPACED.search(c["path"] or ""):
                    self.conn.execute("DELETE FROM doc_chunk_fts WHERE rowid = ?", (c["id"],))
                    self.conn.execute(
                        "INSERT INTO doc_chunk_fts (rowid, content, path, doc) VALUES (?,?,?,?)",
                        (c["id"], split_unspaced(c["content"]), split_unspaced(c["path"] or ""),
                         c["doc_name"].replace("_", " ")))
            self.conn.execute("INSERT INTO setting (key, value) VALUES ('index_unspaced', '1')")

    # ---------- helpers ----------

    def _tx(self):
        store = self

        class _Tx:
            def __enter__(self):
                store.lock.acquire()
                store.conn.execute("BEGIN IMMEDIATE")

            def __exit__(self, exc_type, *_):
                try:
                    store.conn.execute("ROLLBACK" if exc_type else "COMMIT")
                    if exc_type:
                        store._dirty.clear()
                    elif store._dirty:
                        store._flush_vectors()
                finally:
                    store.lock.release()

        return _Tx()

    # ---------- embeddings ----------

    @staticmethod
    def _embed_text(row: sqlite3.Row) -> str:
        tags = " ".join(json.loads(row["tags"]))
        head = f"{row['entity_name']}: " if row["entity_name"] else ""
        return f"{head}{row['content']} {tags}".strip()

    def _flush_vectors(self) -> None:
        """Embed memories changed by the last transaction (called after COMMIT, under lock)."""
        ids, self._dirty = sorted(self._dirty), set()
        if self.embedder is None or self.defer_vectors:
            return  # bulk imports call backfill_vectors() once at the end instead
        rows = [self._row(mid) for mid in ids]
        live = [r for r in rows if not r["deleted_at"]]
        for r in rows:
            if r["deleted_at"]:
                self.vindex.remove(r["id"])
        if live:
            try:
                self._store_vectors(live)
            except Exception:  # the write itself is committed; backfill retries later
                log.exception("embedding failed; memories stay searchable by full text")

    def _store_vectors(self, rows: list[sqlite3.Row]) -> None:
        vecs = self.embedder.docs([self._embed_text(r) for r in rows])
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.conn.executemany(
                "INSERT OR REPLACE INTO memory_vec (memory_id, model, vec) VALUES (?,?,?)",
                [(r["id"], self.embedder.model, v.tobytes()) for r, v in zip(rows, vecs)])
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        for r, v in zip(rows, vecs):
            self.vindex.upsert(r["id"], v)

    def backfill_vectors(self, batch: int = 64) -> int:
        """Embed memories that have no vector for the current model. Returns how many."""
        if self.embedder is None:
            return 0
        done = 0
        while True:
            with self.lock:
                ids = [r[0] for r in self.conn.execute(
                    """SELECT m.id FROM memory m LEFT JOIN memory_vec v
                       ON v.memory_id = m.id AND v.model = ?
                       WHERE m.deleted_at IS NULL AND v.memory_id IS NULL LIMIT ?""",
                    (self.embedder.model, batch))]
                if not ids:
                    return done
                self._store_vectors([self._row(mid) for mid in ids])
            done += len(ids)

    def scope_id(self, key: str | None, create: bool = True) -> int:
        key = (key or "global").strip()
        row = self.conn.execute("SELECT id FROM scope WHERE key = ?", (key,)).fetchone()
        if row:
            return row["id"]
        if not create:
            raise NotFound(f"unknown scope: {key}")
        kind = key.split(":", 1)[0] if ":" in key else "custom"
        parent = self.scope_id("global")
        cur = self.conn.execute(
            "INSERT INTO scope (key, kind, parent_id) VALUES (?, ?, ?)", (key, kind, parent)
        )
        return cur.lastrowid

    def scope_chain(self, key: str | None) -> list[int]:
        """The scope and its ancestors; recall sees all of them."""
        try:
            sid: int | None = self.scope_id(key, create=False)
        except NotFound:
            sid = self.scope_id("global")
        chain = []
        while sid is not None and sid not in chain:
            chain.append(sid)
            row = self.conn.execute("SELECT parent_id FROM scope WHERE id = ?", (sid,)).fetchone()
            sid = row["parent_id"] if row else None
        return chain

    def find_entity(self, name: str, scope: str | None = None) -> sqlite3.Row | None:
        chain = self.scope_chain(scope)
        q = f"""SELECT * FROM entity WHERE name = ? AND deleted_at IS NULL
                AND scope_id IN ({",".join("?" * len(chain))})"""
        rows = self.conn.execute(q, (name.strip(), *chain)).fetchall()
        # Prefer the most specific scope.
        rows.sort(key=lambda r: chain.index(r["scope_id"]))
        return rows[0] if rows else None

    def entity_id(self, name: str, scope: str | None, type: str | None = None) -> int:
        row = self.find_entity(name, scope)
        if row:
            if type and row["type"] == "thing":
                self.conn.execute(
                    "UPDATE entity SET type = ?, updated_at = ? WHERE id = ?",
                    (normalize_type(type), now(), row["id"]),
                )
            return row["id"]
        t = now()
        cur = self.conn.execute(
            "INSERT INTO entity (scope_id, name, type, created_at, updated_at) VALUES (?,?,?,?,?)",
            (self.scope_id(scope), name.strip(), normalize_type(type), t, t),
        )
        return cur.lastrowid

    def _row(self, mid: int) -> sqlite3.Row:
        row = self.conn.execute(
            """SELECT m.*, e.name AS entity_name, s.key AS scope_key FROM memory m
               LEFT JOIN entity e ON e.id = m.entity_id
               JOIN scope s ON s.id = m.scope_id WHERE m.id = ?""",
            (mid,),
        ).fetchone()
        if not row:
            raise NotFound(f"m{mid} not found")
        return row

    def _snapshot(self, mid: int, op: str, client: str | None) -> None:
        row = self._row(mid)
        snap = dict(row)
        self.conn.execute(
            """INSERT INTO memory_history (memory_id, version, changed_at, client, op, snapshot)
               VALUES (?,?,?,?,?,?)""",
            (mid, row["version"], now(), client, op, json.dumps(snap, ensure_ascii=False)),
        )

    def _index(self, mid: int) -> None:
        self._dirty.add(mid)
        self._active_days = None
        self._put_fts(self._row(mid))

    def _put_fts(self, row: sqlite3.Row) -> None:
        """(Re)write a memory's full-text entry; forgotten ones have none."""
        self.conn.execute("DELETE FROM memory_fts WHERE rowid = ?", (row["id"],))
        if row["deleted_at"]:
            return
        tags = " ".join(json.loads(row["tags"]))
        self.conn.execute(
            "INSERT INTO memory_fts (rowid, content, tags, entity) VALUES (?,?,?,?)",
            (row["id"], split_unspaced(row["content"]), split_unspaced(tags),
             split_unspaced(row["entity_name"] or "")),
        )

    def episode(self, scope: str | None, client: str | None, raw: str | None = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO episode (scope_id, client, created_at, raw) VALUES (?,?,?,?)",
            (self.scope_id(scope), client, now(), raw),
        )
        return cur.lastrowid

    # ---------- writes ----------

    def remember(
        self,
        content: str,
        *,
        type: str = "fact",
        entity: str | None = None,
        entity_type: str | None = None,
        tags: list[str] | None = None,
        importance: float | None = None,
        pinned: bool = False,
        scope: str | None = None,
        valid_from: str | None = None,
        expires_at: str | None = None,
        idempotency_key: str | None = None,
        client: str | None = None,
        episode_id: int | None = None,
        recorded_at: str | None = None,
        check_duplicates: bool = True,
        agent: bool = False,
    ) -> tuple[sqlite3.Row, list[sqlite3.Row]]:
        """agent=True: a pin becomes a request the user approves (see pin_requests)."""
        content = content.strip()
        if not content:
            raise ValueError("content is empty")
        secretscan.check(content)
        with self._tx():
            if idempotency_key:
                row = self.conn.execute(
                    "SELECT id FROM memory WHERE client IS ? AND idempotency_key = ?",
                    (client, idempotency_key),
                ).fetchone()
                if row:
                    return self._row(row["id"]), []
            sid = self.scope_id(scope)
            eid = self.entity_id(entity, scope, entity_type) if entity else None
            t = now()
            cur = self.conn.execute(
                """INSERT INTO memory (scope_id, entity_id, type, content, tags, importance,
                   pinned, valid_from, recorded_at, updated_at, expires_at, episode_id, client,
                   idempotency_key, status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    sid,
                    eid,
                    (type or "fact").lower(),
                    content,
                    json.dumps(tags or [], ensure_ascii=False),
                    0.5 if importance is None else max(0.0, min(1.0, importance)),
                    int(bool(pinned) and not agent),
                    valid_from,
                    recorded_at or t,
                    recorded_at or t,
                    expires_at,
                    episode_id,
                    client,
                    idempotency_key,
                    "open" if (type or "").lower() == "todo" else None,
                ),
            )
            mid = cur.lastrowid
            self._index(mid)
            self._snapshot(mid, "create", client)
            if pinned and agent:
                self._request_pin(mid, client)
            dups = self.near_duplicates(mid) if check_duplicates else []
        # after COMMIT: the new memory's vector exists now (embedded on commit)
        if check_duplicates:
            dups += [r for r in self.similar_memories(mid) if r["id"] not in {d["id"] for d in dups}]
        return self._row(mid), dups[:3]

    def similar_memories(self, mid: int, threshold: float = DUP_SIMILARITY,
                         limit: int = 3) -> list[sqlite3.Row]:
        """Same-entity memories whose meaning is close: candidates to update or supersede.

        Similarity can't tell a paraphrase from a different claim about the same subject
        (both score ~0.9), so these are only ever offered, never merged.
        """
        if self.vindex is None or mid not in self.vindex.pos:
            return []
        row = self._row(mid)
        with self.vindex.lock:
            vec = self.vindex.matrix[self.vindex.pos[mid]].copy()
        out = []
        for other, sim in self.vindex.search(vec, 12):
            if other == mid or sim < threshold:
                continue
            cand = self._row(other)
            if (cand["entity_id"] == row["entity_id"] and not cand["deleted_at"]
                    and not cand["superseded_by"]
                    and not different_hosts(row["content"], cand["content"])):
                out.append(cand)
            if len(out) >= limit:
                break
        return out

    def near_duplicates(self, mid: int, limit: int = 3, threshold: float = 0.5) -> list[sqlite3.Row]:
        row = self._row(mid)
        mine = set(terms(row["content"], limit=64))
        if not mine:
            return []
        query = " OR ".join(f'"{t}"' for t in list(mine)[:16])
        cands = self.conn.execute(
            """SELECT m.id, m.content FROM memory_fts JOIN memory m ON m.id = memory_fts.rowid
               WHERE memory_fts MATCH ? AND m.id != ? AND m.deleted_at IS NULL
               AND m.superseded_by IS NULL ORDER BY bm25(memory_fts) LIMIT 20""",
            (query, mid),
        ).fetchall()
        out = []
        for c in cands:
            theirs = set(terms(c["content"], limit=64))
            jacc = len(mine & theirs) / len(mine | theirs) if theirs else 0
            if jacc >= threshold:
                out.append(self._row(c["id"]))
        return out[:limit]

    def update(
        self,
        ref: str | int,
        *,
        content: str | None = None,
        supersede: bool = False,
        expected_version: int | None = None,
        type: str | None = None,
        entity: str | None = None,
        tags: list[str] | None = None,
        importance: float | None = None,
        pinned: bool | None = None,
        expires_at: str | None = None,
        status: str | None = None,
        client: str | None = None,
        agent: bool = False,
    ) -> sqlite3.Row:
        """agent=True: pinning becomes a request the user approves, and changing what a
        pinned fact says unpins it until approved again (pinned text reaches every session
        on every machine, so only the user puts it there)."""
        mid = parse_id(ref)
        if status is not None and status not in TODO_STATES:
            raise ValueError(f"status must be one of {', '.join(TODO_STATES)}")
        if content is not None:
            content = content.strip()
            if not content:
                raise ValueError("content is empty")
            secretscan.check(content)
        with self._tx():
            old = self._row(mid)
            if old["deleted_at"]:
                raise Conflict(f"m{mid} is forgotten; restore it first")
            if old["superseded_by"]:
                raise Conflict(f"m{mid} was superseded by m{old['superseded_by']}; edit that one")
            if expected_version is not None and expected_version != old["version"]:
                raise Conflict(
                    f"m{mid} is at v{old['version']}, not v{expected_version}. "
                    f"Current: {old['content']}"
                )
            fields = {
                "content": content if content is not None else old["content"],
                "type": type.lower() if type else old["type"],
                "entity_id": self.entity_id(entity, old["scope_key"]) if entity else old["entity_id"],
                "tags": json.dumps(tags, ensure_ascii=False) if tags is not None else old["tags"],
                "importance": old["importance"] if importance is None else max(0.0, min(1.0, importance)),
                "pinned": old["pinned"] if pinned is None else int(pinned),
                "expires_at": old["expires_at"] if expires_at is None else (expires_at or None),
            }
            new_type = fields["type"]
            if status is not None and new_type != "todo":
                raise ValueError("status applies to todos only")
            fields["status"] = (status or old["status"] or "open") if new_type == "todo" else None
            ask_pin = False
            if agent and pinned is not False:
                changed = supersede or fields["content"] != old["content"]
                ask_pin = (bool(pinned) and not old["pinned"]) or (old["pinned"] and changed)
                fields["pinned"] = 0 if ask_pin else old["pinned"]
            if pinned is not None:  # decided now (or re-asked below): drop an older request
                self.conn.execute("DELETE FROM pin_request WHERE memory_id = ?", (mid,))
            t = now()
            if supersede:
                cur = self.conn.execute(
                    """INSERT INTO memory (scope_id, entity_id, type, content, tags, importance,
                       pinned, valid_from, recorded_at, updated_at, expires_at, client, status)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        old["scope_id"], fields["entity_id"], fields["type"], fields["content"],
                        fields["tags"], fields["importance"], fields["pinned"], t, t, t,
                        fields["expires_at"], client, fields["status"],
                    ),
                )
                new_id = cur.lastrowid
                self.conn.execute(
                    """UPDATE memory SET valid_to = ?, superseded_by = ?, pinned = 0,
                       version = version + 1, updated_at = ? WHERE id = ?""",
                    (t, new_id, t, mid),
                )
                self._index(new_id)
                self._index(mid)
                self._snapshot(mid, "superseded", client)
                self._snapshot(new_id, "create", client)
                if ask_pin:
                    self._request_pin(new_id, client)
                return self._row(new_id)
            sets = ", ".join(f"{k} = ?" for k in fields)
            self.conn.execute(
                f"UPDATE memory SET {sets}, version = version + 1, updated_at = ? WHERE id = ?",
                (*fields.values(), t, mid),
            )
            self._index(mid)
            self._snapshot(mid, "update", client)
            if ask_pin:
                self._request_pin(mid, client)
            return self._row(mid)

    # ---------- pin requests ----------

    def _request_pin(self, mid: int, client: str | None) -> None:
        self.conn.execute("INSERT OR REPLACE INTO pin_request (memory_id, client, requested_at)"
                          " VALUES (?, ?, ?)", (mid, client, now()))

    def pin_requested(self, mid: int) -> bool:
        return self.conn.execute("SELECT 1 FROM pin_request WHERE memory_id = ?",
                                 (mid,)).fetchone() is not None

    def pin_requests(self) -> list[sqlite3.Row]:
        """Live memories an agent asked to pin, oldest request first."""
        return self.conn.execute(
            """SELECT m.*, e.name AS entity_name, p.client AS requested_by, p.requested_at
               FROM pin_request p JOIN memory m ON m.id = p.memory_id
               LEFT JOIN entity e ON e.id = m.entity_id
               WHERE m.deleted_at IS NULL AND m.superseded_by IS NULL
               ORDER BY p.requested_at, m.id""").fetchall()

    def decide_pin(self, ref: str | int, approve: bool, client: str | None = None) -> sqlite3.Row:
        """The user's answer to a pin request: pin the memory, or just drop the request."""
        mid = parse_id(ref)
        if not self.pin_requested(mid):
            raise NotFound(f"no pin request for m{mid}")
        if approve:
            return self.update(mid, pinned=True, client=client)
        with self._tx():
            self.conn.execute("DELETE FROM pin_request WHERE memory_id = ?", (mid,))
        return self._row(mid)

    def forget(self, ref: str | int, *, reason: str | None = None, undo: bool = False,
               client: str | None = None) -> sqlite3.Row:
        mid = parse_id(ref)
        with self._tx():
            row = self._row(mid)
            if undo:
                if not row["deleted_at"]:
                    return row
                self.conn.execute(
                    "UPDATE memory SET deleted_at = NULL, version = version + 1, updated_at = ? "
                    "WHERE id = ?", (now(), mid))
                op = "restore"
            else:
                if row["deleted_at"]:
                    return row
                self.conn.execute(
                    "UPDATE memory SET deleted_at = ?, version = version + 1, updated_at = ? "
                    "WHERE id = ?", (now(), now(), mid))
                op = f"forget: {reason}" if reason else "forget"
            self._index(mid)
            self._snapshot(mid, op, client)
            return self._row(mid)

    def link(self, from_entity: str, to_entity: str, relation: str, *, scope: str | None = None,
             remove: bool = False, client: str | None = None) -> bool:
        """Returns True when something changed."""
        relation = relation.strip()
        with self._tx():
            if remove:
                a, b = self.find_entity(from_entity, scope), self.find_entity(to_entity, scope)
                if not a or not b:
                    return False
                cur = self.conn.execute(
                    """UPDATE relation SET deleted_at = ?, valid_to = ? WHERE from_entity = ?
                       AND to_entity = ? AND type = ? AND deleted_at IS NULL""",
                    (now(), now(), a["id"], b["id"], relation),
                )
                return cur.rowcount > 0
            a = self.entity_id(from_entity, scope)
            b = self.entity_id(to_entity, scope)
            cur = self.conn.execute(
                """INSERT OR IGNORE INTO relation (scope_id, from_entity, to_entity, type,
                   valid_from, recorded_at) VALUES (?,?,?,?,?,?)""",
                (self.scope_id(scope), a, b, relation, now(), now()),
            )
            return cur.rowcount > 0

    def forget_entity(self, name: str, *, scope: str | None = None, client: str | None = None) -> int:
        """Soft-delete an entity with all its memories and relations. Returns memories removed."""
        ent = self.find_entity(name, scope)
        if not ent:
            return 0
        ids = [r["id"] for r in self.conn.execute(
            "SELECT id FROM memory WHERE entity_id = ? AND deleted_at IS NULL", (ent["id"],))]
        for mid in ids:
            self.forget(mid, reason=f"entity {name} deleted", client=client)
        with self._tx():
            t = now()
            self.conn.execute("UPDATE entity SET deleted_at = ? WHERE id = ?", (t, ent["id"]))
            self.conn.execute(
                """UPDATE relation SET deleted_at = ?, valid_to = ? WHERE deleted_at IS NULL
                   AND (from_entity = ? OR to_entity = ?)""", (t, t, ent["id"], ent["id"]))
        return len(ids)

    # ---------- reads ----------

    def get(self, refs: list[str | int], *, touch: bool = True) -> list[sqlite3.Row]:
        rows = []
        for ref in refs:
            mid = parse_id(ref)
            rows.append(self._row(mid))
            if touch:
                with self._tx():
                    self.conn.execute(
                        """UPDATE memory SET access_count = access_count + 1,
                           last_accessed_at = ? WHERE id = ?""", (now(), mid))
        return rows

    def history(self, ref: str | int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM memory_history WHERE memory_id = ? ORDER BY id", (parse_id(ref),)
        ).fetchall()

    def predecessors(self, ref: str | int) -> list[sqlite3.Row]:
        """Earlier versions this memory replaced (supersede chain), oldest first."""
        out, frontier, seen = [], [parse_id(ref)], set()
        while frontier:
            mid = frontier.pop()
            for r in self.conn.execute(
                    """SELECT m.*, e.name AS entity_name, s.key AS scope_key FROM memory m
                       LEFT JOIN entity e ON e.id = m.entity_id JOIN scope s ON s.id = m.scope_id
                       WHERE m.superseded_by = ?""", (mid,)):
                if r["id"] not in seen:
                    seen.add(r["id"])
                    out.append(r)
                    frontier.append(r["id"])
        return sorted(out, key=lambda r: r["recorded_at"])

    def relations(self, entity_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT r.type, a.name AS from_name, b.name AS to_name FROM relation r
               JOIN entity a ON a.id = r.from_entity JOIN entity b ON b.id = r.to_entity
               WHERE r.deleted_at IS NULL AND (r.from_entity = ? OR r.to_entity = ?)""",
            (entity_id, entity_id),
        ).fetchall()

    def recall(
        self,
        query: str = "",
        *,
        scope: str | None = None,
        type: str | None = None,
        entity: str | None = None,
        deep: bool = False,
        include_old: bool = False,
        limit: int = 50,
        _window: tuple | None = None,
    ) -> RecallResult:
        chain = self.scope_chain(scope)
        where = [f"m.scope_id IN ({','.join('?' * len(chain))})", "m.deleted_at IS NULL"]
        args: list = list(chain)
        # _window: the fallback below, (since, until) or (None, None) for no time filter
        query, since, until = temporal.parse(query) if _window is None else (query, *_window)
        if since:  # "last week", "v októbri", "2026-10"...: written or changed in that range
            where.append("((m.recorded_at >= ? AND m.recorded_at < ?) OR "
                         "(m.updated_at >= ? AND m.updated_at < ?))")
            args += [since.isoformat(), until.isoformat()] * 2
        current_only = not (deep or include_old)
        if current_only:
            # Superseded facts stay searchable: when an old wording matches the query best,
            # recall answers with its successor (see _promote_successors).
            t = now()
            where += ["(m.superseded_by IS NOT NULL OR ((m.expires_at IS NULL OR m.expires_at > ?)"
                      " AND (m.valid_to IS NULL OR m.valid_to > ?)))"]
            args += [t, t]
        if type:
            where.append("m.type = ?")
            args.append(type.lower())
        if entity:
            where.append("e.name = ? COLLATE NOCASE")
            args.append(entity.strip())

        qterms = query_terms(query)
        base = """SELECT m.*, e.name AS entity_name, s.key AS scope_key {rank} FROM {src}
                  LEFT JOIN entity e ON e.id = m.entity_id JOIN scope s ON s.id = m.scope_id"""
        def listing() -> RecallResult:
            sql = base.format(rank=", 0 AS rank", src="memory m")
            sql += " WHERE " + " AND ".join(where)
            order = ("m.updated_at DESC" if since
                     else "m.pinned DESC, m.importance DESC, m.updated_at DESC")
            sql += f" ORDER BY {order} LIMIT {int(limit)}"
            hits = [self._score(r, []) for r in self.conn.execute(sql, args).fetchall()]
            if current_only:
                hits = [h for h in hits if not h.memory["superseded_by"]]
            if not since:  # a time-only query keeps newest-first
                hits.sort(key=lambda h: h.score, reverse=True)
            return RecallResult(hits=hits, coverage=1.0)

        if not qterms:
            return listing()

        # Lexical candidates (FTS5 BM25).
        fts_terms = [stem(a) if deep else a for t in qterms for a in alternatives(t)]
        match = " OR ".join(f'"{t}"*' for t in dict.fromkeys(fts_terms))
        sql = base.format(rank=", bm25(memory_fts, 1.0, 0.5, 2.0) AS rank",
                          src="memory_fts JOIN memory m ON m.id = memory_fts.rowid")
        sql += " WHERE memory_fts MATCH ? AND " + " AND ".join(where)
        sql += f" ORDER BY rank LIMIT {int(limit) * (3 if deep else 2)}"
        rows = {r["id"]: r for r in self.conn.execute(sql, (match, *args)).fetchall()}
        if since and not rows and not deep:
            found = listing() if _window is None else RecallResult(hits=[], coverage=0.0)
            if found.hits:  # "what did we do yesterday": leftover words are just filler
                return found
            # Nothing noted in that range matches: the date says when it happened, not
            # when it was written down. Look a week either side, then at any time.
            again = dict(scope=scope, type=type, entity=entity, include_old=include_old,
                         limit=limit)
            # The answer says so: the agent must not take these as from that period.
            if _window is None and until - since <= timedelta(days=31):
                wide = self.recall(query, **again,
                                   _window=(since - timedelta(7), until + timedelta(7)))
                if wide.hits:
                    wide.note = wide.note or ("nothing noted in that period matches; these "
                                              "are from the week around it")
                    return wide
            anytime = self.recall(query, **again, _window=(None, None))
            anytime.note = "nothing noted in that period matches; these are from any time"
            return anytime

        # Semantic candidates, deep mode only: on the benchmark, full text alone already
        # finds almost everything that shares words with the query, and mixing vectors in
        # there added noise. Vectors earn their keep where words don't match at all.
        sims: dict[int, float] = {}
        if deep and self.vindex is not None and len(self.vindex):
            try:
                qvec = self.embedder.query(query)
            except Exception:  # model missing or broken: degrade to full text
                log.exception("embedding the query failed; using full-text recall only")
                qvec = None
        else:
            qvec = None
        if qvec is not None:
            sims = self.vindex.similarities(qvec)
            nearest = [mid for mid, _ in self.vindex.search(qvec, 40)
                       if mid not in rows]
            if nearest:
                sql = base.format(rank=", 0 AS rank", src="memory m")
                sql += (" WHERE " + " AND ".join(where)
                        + f" AND m.id IN ({','.join('?' * len(nearest))})")
                for r in self.conn.execute(sql, (*args, *nearest)).fetchall():
                    rows[r["id"]] = r

        weights = self._term_weights(qterms)
        hits = [self._score(r, qterms, weights) for r in rows.values()]
        for h in hits:  # age by activity on the topic, not by the calendar (as in context)
            h.parts["recency"] = round(math.exp(-0.05 * self._active_age(h.memory)), 2)
        worst = max((-h.memory["rank"] for h in hits), default=0.0)
        best_sim = max((sims.get(h.memory["id"], 0.0) for h in hits), default=0.0)

        def combine(lexical: float, semantic: float | None) -> float:
            if semantic is None:
                return lexical
            # Agreement of both signals is rewarded; either one alone still counts.
            return 0.7 * max(lexical, semantic) + 0.3 * min(lexical, semantic)

        for h in hits:
            # bm25 is normalized within this result set, so it orders hits but says nothing
            # absolute; confidence ("rel") therefore uses term coverage, not bm25.
            bm = -h.memory["rank"] / worst if worst > 0 else 0.0
            h.parts["bm25"] = round(bm, 2)
            semantic = ordering = None
            if sims:
                sim = sims.get(h.memory["id"], 0.0)
                semantic = min(1.0, max(0.0, (sim - SIM_FLOOR) / (SIM_CEIL - SIM_FLOOR)))
                h.parts["sim"] = round(sim, 2)
                # For ordering, similarity also counts relative to the best one here, as
                # bm25 does: across languages a small model's similarities stay low, and on
                # an absolute scale they could never outrank a partial word match.
                nearest = min(1.0, max(0.0, (sim - SIM_FLOOR) / max(best_sim - SIM_FLOOR, 1e-6)))
                ordering = 0.5 * semantic + 0.5 * nearest
            h.parts["rel"] = round(combine(h.parts["coverage"], semantic), 2)
            ranking = combine(0.6 * h.parts["coverage"] + 0.4 * bm, ordering)
            h.score = round(
                ranking * 0.75 + 0.15 * h.parts["importance"] + 0.1 * h.parts["recency"]
                + (0.1 if h.memory["pinned"] else 0.0) - (0.2 if h.status else 0.0), 3)
        if current_only:
            hits = self._promote_successors(hits)
        hits.sort(key=lambda h: h.score, reverse=True)
        # Document parts have no type or entity: a filter on either asks for facts only.
        chunks = [] if (type or entity) else self._doc_hits(match, qterms,
                                                             limit=12 if deep else 6)
        confidence = max([h.parts["rel"] for h in hits] + [c.parts["rel"] for c in chunks],
                         default=0.0)
        return RecallResult(hits=hits[:limit], coverage=confidence, chunks=chunks)

    def _doc_hits(self, match: str, qterms: list[str], limit: int) -> list[DocHit]:
        """Parts of shared documents matching the query, scored like facts but with a small
        handicap, so what agents wrote down ranks first on a tie."""
        try:
            rows = self.conn.execute(
                """SELECT c.*, bm25(doc_chunk_fts, 1.0, 0.8, 0.5) AS rank FROM doc_chunk_fts
                   JOIN doc_chunk c ON c.id = doc_chunk_fts.rowid
                   WHERE doc_chunk_fts MATCH ? ORDER BY rank LIMIT ?""",
                (match, limit)).fetchall()
        except sqlite3.OperationalError:  # no documents table yet (old schema in tests)
            return []
        if not rows:
            return []
        worst = max(-r["rank"] for r in rows) or 1.0
        weights = self._term_weights(qterms, "doc_chunk_fts")
        out = []
        for r in rows:
            words = set(terms(f"{r['content']} {r['path']} {r['doc_name']}", limit=10_000))
            stems = {stem(t) for t in words}
            matched = sum(weights[q] for q in qterms if covered(q, words, stems))
            coverage = matched / sum(weights.values()) if qterms else 0.0
            bm = -r["rank"] / worst if worst > 0 else 0.0
            score = round((0.6 * coverage + 0.4 * bm) * 0.75 + 0.15 * 0.4 + 0.1 - 0.02, 3)
            if r["kind"] == "row" and qterms:
                # a query word that *starts* a cell names this row ("node2" → the
                # Node2C7 row), not one that merely mentions it ("Snapshot Node2")
                cells = [fold(c.strip()) for c in r["content"].splitlines()[-1].split("|")]
                if any(c.startswith(q) for q in qterms for c in cells if c):
                    score = round(score + 0.15, 3)
            out.append(DocHit(r["doc_name"], r["anchor"], r["path"], r["kind"], r["content"],
                              score, {"coverage": round(coverage, 2), "bm25": round(bm, 2),
                                      "rel": round(coverage, 2)}))
        return sorted(out, key=lambda c: c.score, reverse=True)

    def _promote_successors(self, hits: list[Hit]) -> list[Hit]:
        """Replace superseded hits by the current version of the fact (one hit per fact)."""
        best: dict[int, Hit] = {}
        for h in hits:
            m, replaced = h.memory, None
            seen = set()
            while m["superseded_by"] and m["id"] not in seen:
                seen.add(m["id"])
                replaced = replaced or h.memory["id"]
                m = self._row(m["superseded_by"])
            if m["deleted_at"] or (m["expires_at"] and m["expires_at"] <= now()):
                continue
            if replaced:
                h = Hit(memory=m, entity=m["entity_name"], score=h.score,
                        parts={**h.parts, "replaces": replaced})
            keep = best.get(m["id"])
            if keep is None or h.score > keep.score:
                best[m["id"]] = h
        return list(best.values())

    def _term_weights(self, qterms: list[str], table: str = "memory_fts") -> dict[str, float]:
        """How much each query word says, from how many entries contain it (BM25's IDF):
        in any language, words that appear everywhere ("server", a speaker's name, filler)
        count little and rare ones ("mailgun") a lot. A word found nowhere (often just an
        unusual inflection: "appku") counts like an average word of the query, not like the
        rarest one: it still lowers confidence, so a query about something memory doesn't
        hold stays low-confidence, but one odd word doesn't sink a good match."""
        n = self.conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        idf: dict[str, float] = {}
        for q in qterms:
            match = " OR ".join(f'"{a}"*' for a in alternatives(q))
            df = self.conn.execute(f"SELECT count(*) FROM {table} WHERE {table} MATCH ?",
                                   (match,)).fetchone()[0]
            if df:
                idf[q] = math.log(1 + (n - df + 0.5) / (df + 0.5))
        unseen = sum(idf.values()) / len(idf) if idf else 1.0
        return {q: idf.get(q, unseen) for q in qterms}

    def _score(self, row: sqlite3.Row, qterms: list[str],
               weights: dict[str, float] | None = None) -> Hit:
        text_terms = set(terms(
            f"{row['content']} {row['entity_name'] or ''} {' '.join(json.loads(row['tags']))}",
            limit=10_000))
        stems = {stem(t) for t in text_terms}
        if qterms:
            w = weights or {}
            matched = sum(w.get(q, 1.0) for q in qterms if covered(q, text_terms, stems))
            coverage = matched / sum(w.get(q, 1.0) for q in qterms)
        else:
            coverage = 0.0
        ts = row["updated_at"] or row["recorded_at"]
        age_days = (datetime.now(UTC) - datetime.fromisoformat(ts)).total_seconds() / 86400
        recency = math.exp(-max(age_days, 0) / 180)
        parts = {"coverage": round(coverage, 2), "importance": row["importance"],
                 "recency": round(recency, 2)}
        score = 0.75 * row["importance"] + 0.25 * recency + (0.5 if row["pinned"] else 0)
        return Hit(memory=row, entity=row["entity_name"], score=round(score, 3), parts=parts)

    def context(self, scope: str | None = None, limit: int = 40,
                per_entity: int = 2, max_todos: int | None = None) -> list[Hit]:
        """What an agent should see at session start, best first.

        Pinned facts first, then open todos, then the rest ranked by type, importance,
        use and *activity-based* age: a fact ages only when its entity receives newer
        writes, so a project left alone for a year doesn't fade (idea from engram-mcp).
        At most `per_entity` unpinned facts per entity, so one busy topic can't fill it.
        With `max_todos`, only the most recently touched open todos are listed; how many
        were left out is in `self.todos_left`.
        """
        chain = self.scope_chain(scope)
        rows = self.conn.execute(
            f"""SELECT m.*, e.name AS entity_name, s.key AS scope_key FROM memory m
                LEFT JOIN entity e ON e.id = m.entity_id JOIN scope s ON s.id = m.scope_id
                WHERE m.scope_id IN ({','.join('?' * len(chain))}) AND m.deleted_at IS NULL
                AND m.superseded_by IS NULL AND (m.expires_at IS NULL OR m.expires_at > ?)
                AND (m.status IS NULL OR m.status = 'open')""",
            (*chain, now()),
        ).fetchall()
        active_days = self._entity_active_days()
        hits = []
        for r in rows:
            h = self._score(r, [])
            elapsed = self._active_age(r)
            usage = 1 - math.exp(-(r["access_count"] + 2 * r["useful_count"]) / 5)
            prior = TYPE_PRIOR.get(r["type"], 0.5)
            h.parts.update(prior=prior, usage=round(usage, 2), active_age=elapsed)
            h.score = round(0.35 * prior + 0.30 * r["importance"] + 0.15 * usage
                            + 0.20 * math.exp(-0.05 * elapsed)
                            - (0.3 if r["wrong_count"] > r["useful_count"] else 0.0), 3)
            hits.append(h)

        def rank(h: Hit):
            m = h.memory
            # ties (common right after an import) go to the most recently active entity
            last_active = (active_days.get(m["entity_id"]) or [""])[-1]
            return (not m["pinned"], m["type"] != "todo", -h.score, _desc(last_active))

        hits.sort(key=rank)
        picked, seen = [], {}
        todos = sorted((h for h in hits if h.memory["type"] == "todo" and not h.memory["pinned"]),
                       key=lambda h: h.memory["updated_at"] or "", reverse=True)
        skip = {id(h) for h in todos[max_todos:]} if max_todos is not None else set()
        self.todos_left = len(skip)
        for h in hits:
            if id(h) in skip:
                continue
            ent = h.memory["entity_id"]
            if not h.memory["pinned"] and ent is not None:
                if seen.get(ent, 0) >= per_entity:
                    continue
                seen[ent] = seen.get(ent, 0) + 1
            picked.append(h)
            if len(picked) >= limit:
                break
        return picked

    def _entity_active_days(self) -> dict[int, list[str]]:
        """Sorted distinct days on which each entity received a write (cached until a write)."""
        if self._active_days is not None:
            return self._active_days
        days: dict[int, set[str]] = {}
        for eid, day in self.conn.execute(
                """SELECT entity_id, substr(recorded_at, 1, 10) FROM memory
                   WHERE entity_id IS NOT NULL
                   UNION SELECT entity_id, substr(updated_at, 1, 10) FROM memory
                   WHERE entity_id IS NOT NULL"""):
            days.setdefault(eid, set()).add(day)
        self._active_days = {eid: sorted(d) for eid, d in days.items()}
        return self._active_days

    def _active_age(self, row: sqlite3.Row) -> int:
        """Days with newer writes on the same entity since this memory was last changed."""
        days = self._entity_active_days().get(row["entity_id"], [])
        return len(days) - bisect.bisect_right(days, (row["updated_at"] or row["recorded_at"])[:10])

    def entities(self, scope: str | None = None) -> list[sqlite3.Row]:
        chain = self.scope_chain(scope)
        return self.conn.execute(
            f"""SELECT e.*, (SELECT count(*) FROM memory m WHERE m.entity_id = e.id
                AND m.deleted_at IS NULL AND m.superseded_by IS NULL) AS facts
                FROM entity e WHERE e.deleted_at IS NULL
                AND e.scope_id IN ({','.join('?' * len(chain))}) ORDER BY e.name""",
            chain,
        ).fetchall()

    def entity_memories(self, entity_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT m.*, e.name AS entity_name, s.key AS scope_key FROM memory m
               LEFT JOIN entity e ON e.id = m.entity_id JOIN scope s ON s.id = m.scope_id
               WHERE m.entity_id = ? AND m.deleted_at IS NULL AND m.superseded_by IS NULL
               ORDER BY m.pinned DESC, m.importance DESC, m.id""",
            (entity_id,),
        ).fetchall()

    def log_call(self, *, client: str | None, tool: str, query: str | None, tokens: int,
                 hits: int | None, ms: float, error: bool = False) -> None:
        """Record one tool call. Query text is kept unless MEMCAIRN_LOG_QUERIES=0."""
        if os.environ.get("MEMCAIRN_LOG_QUERIES", "1") == "0":
            query = None
        with self.lock:
            self.conn.execute(
                """INSERT INTO call_log (ts, client, tool, query, tokens, hits, ms, error)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (now(), client, tool, (query or None) and query[:300], tokens, hits,
                 round(ms, 1), int(error)))
            self._calls = getattr(self, "_calls", 0) + 1
            if self._calls % 500 == 1:
                self.conn.execute("DELETE FROM call_log WHERE ts < datetime('now', '-30 days')")

    def usage(self, days: int = 7) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT tool, count(*) AS calls, round(avg(tokens)) AS avg_tokens,
                      max(tokens) AS max_tokens, round(avg(hits), 1) AS avg_hits,
                      sum(hits = 0) AS empty, round(avg(ms), 1) AS avg_ms, sum(error) AS errors
               FROM call_log WHERE ts >= datetime('now', ?) GROUP BY tool ORDER BY calls DESC""",
            (f"-{days} days",)).fetchall()

    def stats(self) -> dict[str, int]:
        def q(sql):
            return self.conn.execute(sql).fetchone()[0]
        return {
            "memories": q("SELECT count(*) FROM memory WHERE deleted_at IS NULL "
                          "AND superseded_by IS NULL"),
            "superseded": q("SELECT count(*) FROM memory WHERE superseded_by IS NOT NULL"),
            "forgotten": q("SELECT count(*) FROM memory WHERE deleted_at IS NOT NULL"),
            "entities": q("SELECT count(*) FROM entity WHERE deleted_at IS NULL"),
            "relations": q("SELECT count(*) FROM relation WHERE deleted_at IS NULL"),
            "scopes": q("SELECT count(*) FROM scope"),
            "clients": q("SELECT count(*) FROM client WHERE revoked_at IS NULL"),
        }
