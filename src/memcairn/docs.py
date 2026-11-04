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
from .store import Conflict, NotFound, Store

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
    return get_doc(store, name)
