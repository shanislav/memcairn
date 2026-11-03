"""Importers from other memory formats and lossless JSON export."""

from __future__ import annotations

import json
from pathlib import Path

from .secretscan import SecretError
from .store import Store


def read_server_memory(path: str | Path) -> tuple[list[dict], list[dict]]:
    """Parse @modelcontextprotocol/server-memory files: JSONL or a single {entities, relations}."""
    text = Path(path).read_text(encoding="utf-8")
    try:
        doc = json.loads(text)
        if isinstance(doc, dict) and "entities" in doc:
            return doc.get("entities", []), doc.get("relations", [])
    except json.JSONDecodeError:
        pass
    entities, relations = [], []
    for line in text.splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        (entities if item.get("type") == "entity" else relations).append(item)
    return entities, relations


def import_server_memory(store: Store, path: str | Path, *, scope: str = "global",
                         client: str = "import") -> dict[str, int]:
    entities, relations = read_server_memory(path)
    episode = store.episode(scope, client, raw=f"import from {Path(path).name}")
    stats = {"entities": 0, "facts": 0, "relations": 0, "skipped_secrets": 0}
    for ent in entities:
        name = ent["name"]
        with store._tx():
            store.entity_id(name, scope, ent.get("entityType"))
        stats["entities"] += 1
        for obs in ent.get("observations", []):
            try:
                store.remember(obs, entity=name, scope=scope, client=client, episode_id=episode,
                               check_duplicates=False)
                stats["facts"] += 1
            except SecretError:
                stats["skipped_secrets"] += 1
    for rel in relations:
        if store.link(rel["from"], rel["to"], rel["relationType"], scope=scope, client=client):
            stats["relations"] += 1
    return stats


TABLES = ("scope", "episode", "entity", "memory", "relation", "memory_history")


def export_json(store: Store) -> dict:
    """Everything except client tokens. Restoring = copy the SQLite file; this is for humans/tools."""
    return {t: [dict(r) for r in store.conn.execute(f"SELECT * FROM {t}")] for t in TABLES}
