"""`memcairn purge`: remove text for real, when soft delete is not enough (a leaked secret).

`forget` and `update` keep the old text on purpose (history, undo). For a password that got
in, that is the problem: it sits in the fact, in its version history, in old versions of a
document, in the search index, maybe in a logged query. Purge replaces the given strings
everywhere, rebuilds the derived indexes from the cleaned text, and VACUUMs, so no copy
stays in the file's free pages either (with secure_delete on, freed pages are zeroed).

Values come from a file or stdin, never from the command line (shell history, `ps`).
Backups made before the purge still hold the text; delete them separately.
"""

from __future__ import annotations

import json

from . import docs
from .store import Store

# (table, column, key column) holding stored text; derived tables are rebuilt instead.
SOURCES = [
    ("memory", "content", "id"),
    ("memory_history", "snapshot", "id"),
    ("document", "content", "name"),
    ("document_version", "content", "rowid"),
    ("episode", "raw", "id"),
    ("call_log", "query", "id"),
]


def _forms(value: str) -> list[str]:
    """The value, plus how it looks inside a JSON snapshot (escaped quotes, \\uXXXX)."""
    out = [value]
    for ascii_ in (True, False):
        enc = json.dumps(value, ensure_ascii=ascii_)[1:-1]
        if enc not in out:
            out.append(enc)
    return out


def purge_text(store: Store, values: list[str], *, replacement: str = "[removed]",
               dry_run: bool = False, vacuum: bool = True) -> dict[str, int]:
    """Replace each value everywhere; returns rows changed per table."""
    values = sorted({v for v in values if len(v) >= 4}, key=len, reverse=True)  # longest first
    counts: dict[str, int] = {}
    memories: set[int] = set()
    documents: set[str] = set()
    conn = store.conn
    with store._tx():
        for table, col, key in SOURCES:
            hit: set = set()
            for v in values:
                for form in _forms(v):
                    rows = conn.execute(f"SELECT {key} FROM {table} WHERE instr({col}, ?) > 0",
                                        (form,)).fetchall()
                    hit |= {r[0] for r in rows}
                    if rows and not dry_run:
                        conn.execute(f"UPDATE {table} SET {col} = replace({col}, ?, ?) "
                                     f"WHERE instr({col}, ?) > 0", (form, replacement, form))
            counts[table] = len(hit)
            if table == "memory":
                memories = hit
            elif table == "document":
                documents = hit
        if not dry_run:
            for mid in memories:
                store._index(mid)  # search index and vector from the cleaned text
    if not dry_run:
        for name in documents:
            docs.index_doc(store, name)
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        if vacuum:
            conn.execute("VACUUM")
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return counts


def leftovers(store: Store, values: list[str]) -> int:
    """How many stored rows (any table, derived included) still contain a value."""
    tables = [(t, c) for t, c, _ in SOURCES] + [("memory_fts", "content"),
                                                  ("doc_chunk", "content"),
                                                  ("doc_chunk_fts", "content")]
    n = 0
    for v in values:
        for form in _forms(v):
            for t, c in tables:
                n += store.conn.execute(f"SELECT count(*) FROM {t} WHERE instr({c}, ?) > 0",
                                        (form,)).fetchone()[0]
    return n
