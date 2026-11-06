"""Shared documents: whole files kept on the server, versioned, synced to chosen paths.

For things that should stay a document rather than become facts: a server inventory, the
shared part of CLAUDE.md, a long project plan. Each write names the version it was based
on; a stale base is a conflict, so two machines can never overwrite each other blindly.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3

from . import secretscan
from .db import now
from .store import Conflict, NotFound, Store, split_unspaced

NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,79}$")
MAX_BYTES = 1_000_000


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def check_name(name: str) -> str:
    if not NAME.match(name):
        raise ValueError("document name: lowercase letters, digits, . _ - (max 80)")
    return name


def list_docs(store: Store) -> list[sqlite3.Row]:
    return store.conn.execute(
        "SELECT name, version, sha, updated_at, client, length(content) AS size "
        "FROM document ORDER BY name").fetchall()


def get_doc(store: Store, name: str, version: int | None = None) -> sqlite3.Row:
    if version is None:
        row = store.conn.execute("SELECT * FROM document WHERE name = ?", (name,)).fetchone()
    else:
        row = store.conn.execute("SELECT * FROM document_version WHERE name = ? AND version = ?",
                                 (name, version)).fetchone()
    if not row:
        raise NotFound(f"document {name}" + (f" v{version}" if version else "") + " not found")
    return row


def put_doc(store: Store, name: str, content: str, *, base_version: int,
            client: str | None = None) -> sqlite3.Row:
    """Store a new version. `base_version` is the version the writer started from (0 = new).

    Raises Conflict when someone else wrote in between; the caller merges and retries.
    Writing identical content is a no-op that returns the current row.
    """
    check_name(name)
    if len(content.encode("utf-8")) > MAX_BYTES:
        raise ValueError(f"document over {MAX_BYTES} bytes")
    secretscan.check(content)
    digest = sha(content)
    with store._tx():
        cur = store.conn.execute("SELECT * FROM document WHERE name = ?", (name,)).fetchone()
        current = cur["version"] if cur else 0
        if cur and cur["sha"] == digest:
            return cur
        if base_version != current:
            raise Conflict(f"document {name} is at v{current}, not v{base_version}")
        version, t = current + 1, now()
        store.conn.execute(
            """INSERT INTO document (name, version, sha, content, updated_at, client)
               VALUES (?,?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET version = excluded.version,
               sha = excluded.sha, content = excluded.content,
               updated_at = excluded.updated_at, client = excluded.client""",
            (name, version, digest, content, t, client))
        store.conn.execute(
            """INSERT INTO document_version (name, version, sha, content, updated_at, client)
               VALUES (?,?,?,?,?,?)""", (name, version, digest, content, t, client))
    index_doc(store, name)
    return get_doc(store, name)


# ---------- parts index: documents searchable section by section ----------

def index_doc(store: Store, name: str) -> dict[str, int]:
    """(Re)index a document's parts; unchanged parts (same anchor and hash) are kept."""
    from .chunks import chunk_markdown

    doc = get_doc(store, name)
    chunks = chunk_markdown(doc["content"])
    stats = {"kept": 0, "added": 0, "changed": 0, "removed": 0}
    with store._tx():
        old = {r["anchor"]: r for r in store.conn.execute(
            "SELECT id, anchor, sha FROM doc_chunk WHERE doc_name = ?", (name,))}
        for n, c in enumerate(chunks):
            prev = old.pop(c.anchor, None)
            if prev and prev["sha"] == c.sha:
                store.conn.execute("UPDATE doc_chunk SET doc_version = ?, ord = ? WHERE id = ?",
                                   (doc["version"], n, prev["id"]))
                stats["kept"] += 1
                continue
            if prev:
                store.conn.execute(
                    """UPDATE doc_chunk SET path = ?, kind = ?, content = ?, sha = ?,
                       doc_version = ?, ord = ? WHERE id = ?""",
                    (c.path, c.kind, c.content, c.sha, doc["version"], n, prev["id"]))
                cid = prev["id"]
                store.conn.execute("DELETE FROM doc_chunk_fts WHERE rowid = ?", (cid,))
                stats["changed"] += 1
            else:
                cid = store.conn.execute(
                    """INSERT INTO doc_chunk (doc_name, anchor, path, kind, content, sha,
                       doc_version, ord) VALUES (?,?,?,?,?,?,?,?)""",
                    (name, c.anchor, c.path, c.kind, c.content, c.sha, doc["version"],
                     n)).lastrowid
                stats["added"] += 1
            store.conn.execute(
                "INSERT INTO doc_chunk_fts (rowid, content, path, doc) VALUES (?,?,?,?)",
                (cid, split_unspaced(c.content), split_unspaced(c.path), name.replace("_", " ")))
        for r in old.values():
            store.conn.execute("DELETE FROM doc_chunk WHERE id = ?", (r["id"],))
            store.conn.execute("DELETE FROM doc_chunk_fts WHERE rowid = ?", (r["id"],))
            stats["removed"] += 1
    return stats


def index_all(store: Store) -> int:
    """Index documents whose parts are missing or from an older version. Returns how many."""
    stale = store.conn.execute(
        """SELECT d.name FROM document d WHERE NOT EXISTS (SELECT 1 FROM doc_chunk c
           WHERE c.doc_name = d.name AND c.doc_version = d.version)""").fetchall()
    for r in stale:
        index_doc(store, r["name"])
    return len(stale)


def get_chunk(store: Store, name: str, anchor: str) -> sqlite3.Row:
    row = store.conn.execute("SELECT * FROM doc_chunk WHERE doc_name = ? AND anchor = ?",
                             (name, anchor)).fetchone()
    if not row:
        raise NotFound(f"doc:{name}#{anchor} not found (document changed? see doc:{name}?toc)")
    return row


def delete_doc(store: Store, name: str) -> int:
    """Remove a document with all its versions and parts. Returns versions removed.

    Admin only (CLI), not an MCP tool. Remove it from every machine's sync.toml first,
    or the next `memcairn sync` there uploads it again.
    """
    get_doc(store, name)  # raises if missing
    with store._tx():
        ids = [r["id"] for r in store.conn.execute(
            "SELECT id FROM doc_chunk WHERE doc_name = ?", (name,))]
        for cid in ids:
            store.conn.execute("DELETE FROM doc_chunk_fts WHERE rowid = ?", (cid,))
        store.conn.execute("DELETE FROM doc_chunk WHERE doc_name = ?", (name,))
        n = store.conn.execute("DELETE FROM document_version WHERE name = ?", (name,)).rowcount
        store.conn.execute("DELETE FROM document WHERE name = ?", (name,))
    return n
