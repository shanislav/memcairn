"""Edits that go beyond one fact: permanent delete, renaming or merging entities.

Used by the web UI (and usable from scripts). Agents don't get these as MCP tools: they
forget (reversible) and update; destroying history is a person's decision.
"""

from __future__ import annotations

from .store import NotFound, Store, parse_id


def hard_delete(store: Store, ref: str | int) -> int:
    """Delete a fact for good: its versions, the earlier facts it replaced, indexes.

    Returns how many rows went. Unlike forget, this cannot be undone (backups aside).
    """
    mid = parse_id(ref)
    with store._tx():
        if not store.conn.execute("SELECT 1 FROM memory WHERE id = ?", (mid,)).fetchone():
            raise NotFound(f"m{mid} not found")
        ids, frontier = {mid}, [mid]
        while frontier:  # the supersede chain behind it: its own history
            rows = store.conn.execute(
                f"SELECT id FROM memory WHERE superseded_by IN ({','.join('?' * len(frontier))})",
                frontier).fetchall()
            frontier = [r["id"] for r in rows if r["id"] not in ids]
            ids.update(frontier)
        marks = ",".join("?" * len(ids))
        args = tuple(ids)
        store.conn.execute(f"UPDATE memory SET superseded_by = NULL WHERE superseded_by IN "
                           f"({marks}) AND id NOT IN ({marks})", args + args)
        store.conn.execute(f"DELETE FROM memory_history WHERE memory_id IN ({marks})", args)
        store.conn.execute(f"DELETE FROM memory_fts WHERE rowid IN ({marks})", args)
        store.conn.execute(f"DELETE FROM memory_vec WHERE memory_id IN ({marks})", args)
        store.conn.execute(f"DELETE FROM memory WHERE id IN ({marks})", args)
        store._active_days = None
    if store.vindex is not None:
        for i in ids:
            store.vindex.remove(i)
    return len(ids)


def rename_entity(store: Store, old: str, new: str, *, client: str | None = None) -> int:
    """Rename an entity, or merge it into an existing one. Returns facts moved."""
    new = new.strip()
    if not new:
        raise ValueError("new name is empty")
    src = store.find_entity(old)
    if not src:
        raise NotFound(f"entity {old!r} not found")
    target = store.find_entity(new)
    if target and target["id"] == src["id"]:  # only the case changes
        with store._tx():
            store.conn.execute("UPDATE entity SET name = ? WHERE id = ?", (new, src["id"]))
        return 0
    ids = [r["id"] for r in store.conn.execute(
        "SELECT id FROM memory WHERE entity_id = ? AND deleted_at IS NULL "
        "AND superseded_by IS NULL", (src["id"],))]
    for mid in ids:  # versioned, reindexed, recorded as this client
        store.update(mid, entity=new, client=client)
    dst = store.find_entity(new)
    with store._tx():
        if dst:
            for col in ("from_entity", "to_entity"):
                store.conn.execute(f"UPDATE relation SET {col} = ? WHERE {col} = ?",
                                   (dst["id"], src["id"]))
        left = store.conn.execute("SELECT count(*) FROM memory WHERE entity_id = ? AND "
                                  "deleted_at IS NULL AND superseded_by IS NULL",
                                  (src["id"],)).fetchone()[0]
        if not left:
            store.conn.execute("UPDATE entity SET deleted_at = datetime('now') WHERE id = ?",
                               (src["id"],))
    return len(ids)


def overview(store: Store) -> dict:
    """Numbers for the web UI's home page: what is in memory and how it is used."""
    q = store.conn.execute
    live = "m.deleted_at IS NULL AND m.superseded_by IS NULL"
    types = {r[0]: r[1] for r in q(f"SELECT type, count(*) FROM memory m WHERE {live} "
                                    "GROUP BY type ORDER BY 2 DESC")}
    top = [dict(r) for r in q(
        f"""SELECT e.name, count(*) AS facts, max(m.updated_at) AS last FROM memory m
            JOIN entity e ON e.id = m.entity_id WHERE {live} AND e.deleted_at IS NULL
            GROUP BY e.id ORDER BY facts DESC LIMIT 12""")]
    days = [dict(r) for r in q(
        """SELECT substr(changed_at, 1, 10) AS day, count(*) AS n FROM memory_history
           WHERE changed_at >= datetime('now', '-14 days') GROUP BY day ORDER BY day""")]
    clients = [dict(r) for r in q(
        """SELECT c.name, c.last_seen_at,
                  (SELECT count(*) FROM memory_history h WHERE h.client = c.name
                   AND h.changed_at >= datetime('now', '-30 days')) AS writes_30d
           FROM client c WHERE c.revoked_at IS NULL ORDER BY c.last_seen_at DESC""")]
    todos = [dict(r) for r in q(
        f"""SELECT m.id, m.content, e.name AS entity_name, m.updated_at FROM memory m
            LEFT JOIN entity e ON e.id = m.entity_id WHERE {live} AND m.type = 'todo'
            AND m.status = 'open' ORDER BY m.updated_at DESC LIMIT 6""")]
    recent = [dict(r) for r in q(
        f"""SELECT m.id, m.type, m.content, e.name AS entity_name, m.updated_at, m.client
            FROM memory m LEFT JOIN entity e ON e.id = m.entity_id WHERE {live}
            ORDER BY m.updated_at DESC LIMIT 6""")]
    pinned = [dict(r) for r in q(
        f"""SELECT m.id, m.type, m.content, e.name AS entity_name FROM memory m
            LEFT JOIN entity e ON e.id = m.entity_id WHERE {live} AND m.pinned = 1
            ORDER BY m.id""")]
    usage = {r["tool"]: dict(r) for r in store.usage(7)}
    docs = q("SELECT count(*), coalesce(sum(length(content)), 0) FROM document").fetchone()
    open_todos = q(f"SELECT count(*) FROM memory m WHERE {live} AND m.type = 'todo' "
                   "AND m.status = 'open'").fetchone()[0]
    return {"stats": store.stats(), "types": types, "top_entities": top, "activity": days,
            "clients": clients, "open_todos": open_todos, "todos": todos, "recent": recent,
            "pinned": pinned,
            "usage": usage, "documents": {"count": docs[0], "chars": docs[1]}}
