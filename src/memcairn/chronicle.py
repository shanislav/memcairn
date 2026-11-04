"""`memcairn chronicle`: everything memory knows about one topic, as a dated markdown story.

Documentation generated on demand instead of written by hand: facts, decisions and
todos in date order, including the long original texts that cleanup replaced. Costs no
tokens until someone reads it.
"""

from __future__ import annotations

import json
import sqlite3

from .store import NotFound, Store


def find_entities(store: Store, name: str) -> list[sqlite3.Row]:
    exact = store.conn.execute(
        "SELECT * FROM entity WHERE name = ? COLLATE NOCASE AND deleted_at IS NULL",
        (name,)).fetchall()
    if exact:
        return exact
    return store.conn.execute(
        "SELECT * FROM entity WHERE name LIKE ? AND deleted_at IS NULL ORDER BY name",
        (f"%{name}%",)).fetchall()


def chronicle(store: Store, name: str, *, current_only: bool = False) -> str:
    entities = find_entities(store, name)
    if not entities:
        raise NotFound(f"no entity matching {name!r}")
    out = []
    for ent in entities:
        out += _one(store, ent, current_only)
    return "\n".join(out)


def _first_wording(store: Store, row: sqlite3.Row) -> str | None:
    """The original text of a memory rewritten in place (e.g. by cleanup), if different."""
    snap = store.conn.execute(
        "SELECT snapshot FROM memory_history WHERE memory_id = ? ORDER BY id LIMIT 1",
        (row["id"],)).fetchone()
    if not snap:
        return None
    text = json.loads(snap["snapshot"])["content"]
    return text if text != row["content"] else None


def _one(store: Store, ent: sqlite3.Row, current_only: bool) -> list[str]:
    rows = store.conn.execute(
        """SELECT * FROM memory WHERE entity_id = ? AND deleted_at IS NULL
           ORDER BY recorded_at, id""", (ent["id"],)).fetchall()
    if current_only:
        rows = [r for r in rows if not r["superseded_by"]]
    live = [r for r in rows if not r["superseded_by"]]
    todos = [r for r in live if r["type"] == "todo" and r["status"] == "open"]
    out = [f"# {ent['name']}", "",
           f"*{ent['type']} · {len(live)} current facts"
           + (f", {len(rows) - len(live)} earlier versions" if len(rows) > len(live) else "")
           + (f" · {len(todos)} open todos" if todos else "") + "*", ""]
    rels = store.relations(ent["id"])
    if rels:
        out += ["**Relations:** " + "; ".join(
            f"{r['from_name']} → {r['type']} → {r['to_name']}" for r in rels), ""]
    if todos:
        out += ["## Open todos", ""] + [f"- m{r['id']} {r['content']}" for r in todos] + [""]
    out += ["## Timeline", ""]
    day = None
    for r in rows:
        d = r["recorded_at"][:10]
        if d != day:
            out += ["", f"### {d}", ""]
            day = d
        marks = []
        if r["superseded_by"]:
            marks.append(f"replaced by m{r['superseded_by']}")
        if r["type"] == "todo":
            marks.append(r["status"] or "open")
        if r["pinned"]:
            marks.append("pinned")
        tags = json.loads(r["tags"])
        if tags:
            marks.append("#" + " #".join(tags))
        mark = f" ({', '.join(marks)})" if marks else ""
        text = r["content"]
        if r["superseded_by"]:
            out.append(f"- ~~m{r['id']}~~ *{r['type']}*{mark}")
            out += [f"  > {line}" for line in text.splitlines()]
        else:
            out.append(f"- **m{r['id']}** *{r['type']}*{mark} — {text}")
            first = _first_wording(store, r) if r["version"] > 1 else None
            if first and not current_only:
                out.append("  - earlier wording:")
                out += [f"    > {line}" for line in first.splitlines()]
    out.append("")
    return out
