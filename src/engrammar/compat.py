"""Tools with the names of @modelcontextprotocol/server-memory, so existing prompts keep working.

Off by default: they roughly double the tool-list token cost.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from . import format
from .server import DESTRUCTIVE, READ, WRITE
from .store import Store


class EntityIn(BaseModel):
    name: str
    entityType: str = "thing"
    observations: list[str] = Field(default_factory=list)


class RelationIn(BaseModel):
    from_: str = Field(alias="from")
    to: str
    relationType: str


class ObservationIn(BaseModel):
    entityName: str
    contents: list[str]


class DeletionIn(BaseModel):
    entityName: str
    observations: list[str]


def register(mcp, store: Store, *, client, default_scope, safe) -> None:
    def add_facts(name: str, facts: list[str], scope: str) -> list[str]:
        out = []
        for fact in facts:
            row, _ = store.remember(fact, entity=name, scope=scope, client=client(),
                                    check_duplicates=False)
            out.append(f"m{row['id']}")
        return out

    @mcp.tool(structured_output=False, annotations=WRITE)
    @safe
    def create_entities(entities: list[EntityIn]) -> str:
        """Create entities with observations (legacy API)."""
        scope = default_scope(None)
        out = []
        for e in entities:
            with store._tx():
                store.entity_id(e.name, scope, e.entityType)
            ids = add_facts(e.name, e.observations, scope)
            out.append(f"{e.name}: {len(ids)} facts")
        return "ok\n" + "\n".join(out)

    @mcp.tool(structured_output=False, annotations=WRITE)
    @safe
    def create_relations(relations: list[RelationIn]) -> str:
        """Create relations between entities (legacy API)."""
        scope = default_scope(None)
        n = sum(store.link(r.from_, r.to, r.relationType, scope=scope, client=client())
                for r in relations)
        return f"ok, {n} new"

    @mcp.tool(structured_output=False, annotations=WRITE)
    @safe
    def add_observations(observations: list[ObservationIn]) -> str:
        """Add observations to entities (legacy API)."""
        scope = default_scope(None)
        return "ok\n" + "\n".join(
            f"{o.entityName}: {', '.join(add_facts(o.entityName, o.contents, scope))}"
            for o in observations)

    @mcp.tool(structured_output=False, annotations=DESTRUCTIVE)
    @safe
    def delete_entities(entityNames: list[str]) -> str:
        """Delete entities and their observations (legacy API, recoverable)."""
        scope = default_scope(None)
        n = sum(store.forget_entity(name, scope=scope, client=client()) for name in entityNames)
        return f"ok, {n} facts forgotten"

    @mcp.tool(structured_output=False, annotations=DESTRUCTIVE)
    @safe
    def delete_observations(deletions: list[DeletionIn]) -> str:
        """Delete specific observations (legacy API, recoverable)."""
        scope = default_scope(None)
        n = 0
        for d in deletions:
            ent = store.find_entity(d.entityName, scope)
            if not ent:
                continue
            wanted = {o.strip() for o in d.observations}
            for m in store.entity_memories(ent["id"]):
                if m["content"] in wanted:
                    store.forget(m["id"], reason="delete_observations", client=client())
                    n += 1
        return f"ok, {n} forgotten"

    @mcp.tool(structured_output=False, annotations=DESTRUCTIVE)
    @safe
    def delete_relations(relations: list[RelationIn]) -> str:
        """Delete relations (legacy API)."""
        scope = default_scope(None)
        n = sum(store.link(r.from_, r.to, r.relationType, scope=scope, remove=True,
                           client=client()) for r in relations)
        return f"ok, {n} removed"

    @mcp.tool(structured_output=False, annotations=READ)
    @safe
    def read_graph(offset: int = 0, max_tokens: int = 1500) -> str:
        """List entities with fact counts (paged; use open_nodes for details)."""
        rows = store.entities(default_scope(None))[offset:]
        lines = [f"{r['name']} ({r['type']}, {r['facts']} facts)" for r in rows]
        return format.budgeted(lines, max_tokens, f"(next page: offset={offset + len(lines)})")

    @mcp.tool(structured_output=False, annotations=READ)
    @safe
    def search_nodes(query: str, max_tokens: int = 800) -> str:
        """Search entities and observations (legacy API; prefer recall)."""
        res = store.recall(query, scope=default_scope(None))
        return format.recall(res, max_tokens=max_tokens, limit=10, explain=False, deep=False)

    @mcp.tool(structured_output=False, annotations=READ)
    @safe
    def open_nodes(names: list[str], max_tokens: int = 2000) -> str:
        """Entities with their observations and relations (legacy API)."""
        scope = default_scope(None)
        lines = []
        for name in names:
            ent = store.find_entity(name, scope)
            if not ent:
                lines.append(f"{name}: not found")
                continue
            lines.append(f"## {ent['name']} ({ent['type']})")
            lines += [f"m{m['id']} · {format.oneline(m['content'], 300)}"
                      for m in store.entity_memories(ent["id"])]
            lines += [f"rel: {r['from_name']} --{r['type']}--> {r['to_name']}"
                      for r in store.relations(ent["id"])]
        return format.budgeted(lines, max_tokens, "(use get or recall for the rest)")
