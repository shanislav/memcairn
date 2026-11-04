"""SQLite connection and schema migrations."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

FTS_TOKENIZER = "unicode61 remove_diacritics 2"

MIGRATIONS: list[str] = [
    # 1 — initial schema
    f"""
    CREATE TABLE scope (
        id        INTEGER PRIMARY KEY,
        key       TEXT NOT NULL UNIQUE,
        kind      TEXT NOT NULL DEFAULT 'custom',
        parent_id INTEGER REFERENCES scope(id),
        name      TEXT
    );
    INSERT INTO scope (key, kind, name) VALUES ('global', 'user', 'Global');

    CREATE TABLE client (
        id            INTEGER PRIMARY KEY,
        name          TEXT NOT NULL UNIQUE,
        token_hash    TEXT UNIQUE,
        default_scope TEXT NOT NULL DEFAULT 'global',
        created_at    TEXT NOT NULL,
        last_seen_at  TEXT,
        revoked_at    TEXT
    );

    CREATE TABLE episode (
        id          INTEGER PRIMARY KEY,
        scope_id    INTEGER NOT NULL REFERENCES scope(id),
        client      TEXT,
        session_ref TEXT,
        created_at  TEXT NOT NULL,
        raw         TEXT
    );

    CREATE TABLE entity (
        id         INTEGER PRIMARY KEY,
        scope_id   INTEGER NOT NULL REFERENCES scope(id),
        name       TEXT NOT NULL COLLATE NOCASE,
        type       TEXT NOT NULL DEFAULT 'thing',
        aliases    TEXT NOT NULL DEFAULT '[]',
        summary    TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        deleted_at TEXT
    );
    CREATE UNIQUE INDEX entity_name ON entity (scope_id, name) WHERE deleted_at IS NULL;

    CREATE TABLE memory (
        id               INTEGER PRIMARY KEY,
        scope_id         INTEGER NOT NULL REFERENCES scope(id),
        entity_id        INTEGER REFERENCES entity(id),
        type             TEXT NOT NULL DEFAULT 'fact',
        content          TEXT NOT NULL,
        tags             TEXT NOT NULL DEFAULT '[]',
        importance       REAL NOT NULL DEFAULT 0.5,
        pinned           INTEGER NOT NULL DEFAULT 0,
        valid_from       TEXT,
        valid_to         TEXT,
        recorded_at      TEXT NOT NULL,
        updated_at       TEXT NOT NULL,
        expires_at       TEXT,
        superseded_by    INTEGER REFERENCES memory(id),
        deleted_at       TEXT,
        version          INTEGER NOT NULL DEFAULT 1,
        episode_id       INTEGER REFERENCES episode(id),
        client           TEXT,
        idempotency_key  TEXT,
        access_count     INTEGER NOT NULL DEFAULT 0,
        last_accessed_at TEXT,
        useful_count     INTEGER NOT NULL DEFAULT 0,
        wrong_count      INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX memory_scope ON memory (scope_id);
    CREATE INDEX memory_entity ON memory (entity_id);
    CREATE UNIQUE INDEX memory_idem ON memory (client, idempotency_key)
        WHERE idempotency_key IS NOT NULL;

    CREATE TABLE relation (
        id            INTEGER PRIMARY KEY,
        scope_id      INTEGER NOT NULL REFERENCES scope(id),
        from_entity   INTEGER NOT NULL REFERENCES entity(id),
        to_entity     INTEGER NOT NULL REFERENCES entity(id),
        type          TEXT NOT NULL,
        valid_from    TEXT,
        valid_to      TEXT,
        recorded_at   TEXT NOT NULL,
        deleted_at    TEXT,
        episode_id    INTEGER REFERENCES episode(id)
    );
    CREATE UNIQUE INDEX relation_unique ON relation (from_entity, to_entity, type)
        WHERE deleted_at IS NULL;

    CREATE TABLE memory_history (
        id         INTEGER PRIMARY KEY,
        memory_id  INTEGER NOT NULL REFERENCES memory(id),
        version    INTEGER NOT NULL,
        changed_at TEXT NOT NULL,
        client     TEXT,
        op         TEXT NOT NULL,
        snapshot   TEXT NOT NULL
    );
    CREATE INDEX memory_history_mem ON memory_history (memory_id);

    CREATE VIRTUAL TABLE memory_fts USING fts5 (
        content, tags, entity, tokenize = '{FTS_TOKENIZER}'
    );
    """,
    # 2 — embeddings for semantic search (float32 blobs, one model at a time)
    """
    CREATE TABLE memory_vec (
        memory_id INTEGER PRIMARY KEY REFERENCES memory(id),
        model     TEXT NOT NULL,
        vec       BLOB NOT NULL
    );
    """,
    # 3 — explicit state for todos, so "finished" and "forgotten" are not the same thing
    """
    ALTER TABLE memory ADD COLUMN status TEXT;
    UPDATE memory SET status = 'open' WHERE type = 'todo';
    """,
    # 4 — usage log: what agents ask and what it costs them (kept 30 days)
    """
    CREATE TABLE call_log (
        id     INTEGER PRIMARY KEY,
        ts     TEXT NOT NULL,
        client TEXT,
        tool   TEXT NOT NULL,
        query  TEXT,
        tokens INTEGER NOT NULL,
        hits   INTEGER,
        ms     REAL NOT NULL,
        error  INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX call_log_ts ON call_log (ts);
    """,
    # 5 — shared documents (synced files): every version kept, the merge base for clients
    """
    CREATE TABLE document (
        name       TEXT PRIMARY KEY,
        version    INTEGER NOT NULL,
        sha        TEXT NOT NULL,
        content    TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        client     TEXT
    );
    CREATE TABLE document_version (
        name       TEXT NOT NULL,
        version    INTEGER NOT NULL,
        sha        TEXT NOT NULL,
        content    TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        client     TEXT,
        PRIMARY KEY (name, version)
    );
    """,
    # 6 — documents indexed in parts (sections, table rows) so recall can return one part
    f"""
    CREATE TABLE doc_chunk (
        id          INTEGER PRIMARY KEY,
        doc_name    TEXT NOT NULL,
        anchor      TEXT NOT NULL,
        path        TEXT NOT NULL,
        kind        TEXT NOT NULL,
        content     TEXT NOT NULL,
        sha         TEXT NOT NULL,
        doc_version INTEGER NOT NULL,
        ord         INTEGER NOT NULL,
        UNIQUE (doc_name, anchor)
    );
    CREATE VIRTUAL TABLE doc_chunk_fts USING fts5 (
        content, path, doc, tokenize = '{FTS_TOKENIZER}'
    );
    """,
]


def now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(path: str | Path) -> sqlite3.Connection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    for version, sql in enumerate(MIGRATIONS[current:], start=current + 1):
        conn.execute("BEGIN")
        try:
            _run_script(conn, sql)
            conn.execute(f"PRAGMA user_version = {version}")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise


def _run_script(conn: sqlite3.Connection, sql: str) -> None:
    # executescript() would commit our open transaction; run statements one by one instead.
    for stmt in _split_sql(sql):
        conn.execute(stmt)


def _split_sql(sql: str) -> list[str]:
    stmts, buf = [], ""
    for line in sql.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            if buf.strip():
                stmts.append(buf.strip())
            buf = ""
    if buf.strip():
        stmts.append(buf.strip())
    return stmts
