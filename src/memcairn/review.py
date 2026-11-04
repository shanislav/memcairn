"""`memcairn review`: what to clean up, most token savings first.

The server only finds and ranks problems; rewriting is left to an agent (on request,
with the normal tools: get / update / supersede / forget) or a human. Nothing is merged
automatically. The one exception, `fix_exact_duplicates`, soft-deletes byte-identical
copies, which `forget(undo=true)` can restore.

Uses stored vectors directly, so it needs no embedding model.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from .format import STATUS_LIKE, oneline, tokens
from .store import Store, different_hosts

LIVE = "m.deleted_at IS NULL AND m.superseded_by IS NULL"


@dataclass
class Item:
    kind: str
    ids: list[int]
    entity: str | None
    saving: int  # rough tokens saved per time it is read
    action: str
    text: str

    def line(self) -> str:
        ids = ",".join(f"m{i}" for i in self.ids)
        ent = f" · {self.entity}" if self.entity else ""
        return f"{self.kind:<8} {ids}{ent} · ~{self.saving} tok · {self.action}: {self.text}"


def _rows(store: Store, where: str = "", args: tuple = ()) -> list[sqlite3.Row]:
    return store.conn.execute(
        f"""SELECT m.*, e.name AS entity_name FROM memory m
            LEFT JOIN entity e ON e.id = m.entity_id WHERE {LIVE} {where}""", args).fetchall()


def exact_duplicates(store: Store) -> list[Item]:
    groups: dict[tuple, list[sqlite3.Row]] = {}
    for r in _rows(store):
        groups.setdefault((r["entity_id"], r["content"].strip()), []).append(r)
    return [Item("dup", [r["id"] for r in rs], rs[0]["entity_name"],
                 tokens(rs[0]["content"]) * (len(rs) - 1), "forget the extra copies",
                 oneline(rs[0]["content"], 80))
            for rs in groups.values() if len(rs) > 1]


def similar_pairs(store: Store, threshold: float = 0.9) -> list[Item]:
    """Same-entity pairs with near-identical meaning: one may replace the other."""
    try:
        import numpy as np
    except ImportError:
        return []
    rows = store.conn.execute(
        f"""SELECT m.id, m.entity_id, m.content, e.name AS entity_name, v.vec FROM memory m
            JOIN memory_vec v ON v.memory_id = m.id LEFT JOIN entity e ON e.id = m.entity_id
            WHERE {LIVE} AND m.entity_id IS NOT NULL""").fetchall()
    by_entity: dict[int, list[sqlite3.Row]] = {}
    for r in rows:
        by_entity.setdefault(r["entity_id"], []).append(r)
    out = []
    for group in by_entity.values():
        if len(group) < 2:
            continue
        mat = np.stack([np.frombuffer(r["vec"], dtype=np.float32) for r in group])
        sims = mat @ mat.T
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                if (sims[i, j] >= threshold and group[i]["content"] != group[j]["content"]
                        and not different_hosts(group[i]["content"], group[j]["content"])):
                    a, b = group[i], group[j]
                    out.append(Item("similar", [a["id"], b["id"]], a["entity_name"],
                                    tokens(min(a["content"], b["content"], key=len)),
                                    f"same fact? keep one, supersede the other ({sims[i, j]:.2f})",
                                    f"{oneline(a['content'], 60)} | {oneline(b['content'], 60)}"))
    return out


def long_facts(store: Store, limit: int = 500) -> list[Item]:
    return [Item("long", [r["id"]], r["entity_name"], tokens(r["content"]) - 60,
                 "split into a short conclusion + separate facts", oneline(r["content"], 80))
            for r in _rows(store, "AND length(m.content) > ?", (limit,))]


def status_reports(store: Store) -> list[Item]:
    return [Item("status", [r["id"]], r["entity_name"], tokens(r["content"]) // 2,
                 "rewrite as the lasting fact (what/where/why), drop hashes and versions",
                 oneline(r["content"], 80))
            for r in _rows(store, "AND length(m.content) BETWEEN 151 AND 500")
            if STATUS_LIKE.search(r["content"])]


def stale_todos(store: Store, days: int = 30) -> list[Item]:
    return [Item("todo", [r["id"]], r["entity_name"], tokens(r["content"]),
                 'close: update(status="done"|"dropped")', oneline(r["content"], 80))
            for r in _rows(store, "AND m.type = 'todo' AND m.status = 'open' "
                                  "AND m.updated_at < datetime('now', ?)", (f"-{days} days",))]


KINDS = {"dup": exact_duplicates, "similar": similar_pairs, "long": long_facts,
         "status": status_reports, "todo": stale_todos}


def review(store: Store, kinds: list[str] | None = None) -> list[Item]:
    items = [i for k in (kinds or KINDS) for i in KINDS[k](store)]
    return sorted(items, key=lambda i: -i.saving)


def fix_exact_duplicates(store: Store, client: str = "review") -> int:
    """Soft-delete all but the oldest copy of identical facts. Reversible."""
    n = 0
    for item in exact_duplicates(store):
        for mid in sorted(item.ids)[1:]:
            store.forget(mid, reason=f"exact duplicate of m{min(item.ids)}", client=client)
            n += 1
    return n


CLEANUP_TODO = "Memcairn cleanup"


def suggest_cleanup(store: Store, *, scope: str = "global", minimum: int = 5,
                    origin: str = "After the import", client: str = "import") -> tuple[int, int]:
    """If enough memories need cleanup, keep one open todo saying so; close it when clean.

    The todo shows up in the session-start context, so the next agent session offers the
    cleanup on its own. Runs after an import and, on the server, once a day. Returns
    (items, tokens to save); (0, 0) when not worth it.
    """
    items = review(store, ["long", "status"])
    open_todo = store.conn.execute(
        """SELECT m.id, m.content FROM memory m JOIN entity e ON e.id = m.entity_id
           WHERE e.name = ? AND m.type = 'todo' AND m.status = 'open'
           AND m.deleted_at IS NULL AND m.superseded_by IS NULL""", (CLEANUP_TODO,)).fetchone()
    if len(items) < minimum:
        if open_todo and not items:  # nothing left: the todo would only cost context
            store.update(open_todo["id"], status="done", client=client)
        return 0, 0
    saving = sum(i.saving for i in items)
    text = (f"{origin}, {len(items)} memories are long or read like status logs "
            f"(~{saving} tokens). Offer the user a cleanup: the MCP prompt 'cleanup' lists them "
            "and the rules (a short conclusion plus separate facts; originals stay in history).")
    if open_todo:
        if f", {len(items)} memories " not in open_todo["content"]:  # no version churn
            store.update(open_todo["id"], content=text, client=client)
    else:
        store.remember(text, type="todo", entity=CLEANUP_TODO, importance=0.9, scope=scope,
                       client=client, check_duplicates=False)
    return len(items), saving
