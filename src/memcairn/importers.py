"""Importers from other memory formats and lossless JSON export."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
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


DATE = re.compile(r"\b(20\d\d-[01]\d-[0-3]\d)\b")


def fact_dates(observations: list[str], fallback: str | None = None) -> list[str | None]:
    """When each fact was written, as far as the text says.

    server-memory keeps no timestamps, but agents often start facts with a date
    ("2026-10-01: switched prod to the fork"). Facts without one inherit the newest date
    of their entity. An entity with no dates at all gets `fallback` (the oldest date of
    the whole import), so it doesn't look like the most recent work.
    """
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    found = []
    for obs in observations:
        m = DATE.search(obs)
        found.append(m.group(1) if m and m.group(1) <= today else None)
    latest = max((d for d in found if d), default=fallback)
    return [f"{d or latest}T00:00:00Z" if (d or latest) else None for d in found]


def import_server_memory(store: Store, path: str | Path, *, scope: str = "global",
                         client: str = "import") -> dict[str, int]:
    entities, relations = read_server_memory(path)
    episode = store.episode(scope, client, raw=f"import from {Path(path).name}")
    stats = {"entities": 0, "facts": 0, "relations": 0, "skipped_secrets": 0}
    store.defer_vectors = True  # embed everything in batches at the end
    oldest = min((d for e in entities for d in fact_dates(e.get("observations", [])) if d),
                 default=None)
    oldest = oldest[:10] if oldest else None
    for ent in entities:
        name = ent["name"]
        with store._tx():
            store.entity_id(name, scope, ent.get("entityType"))
        stats["entities"] += 1
        observations = ent.get("observations", [])
        for obs, recorded in zip(observations, fact_dates(observations, oldest)):
            try:
                store.remember(obs, entity=name, scope=scope, client=client, episode_id=episode,
                               recorded_at=recorded, check_duplicates=False)
                stats["facts"] += 1
            except SecretError:
                stats["skipped_secrets"] += 1
    for rel in relations:
        if store.link(rel["from"], rel["to"], rel["relationType"], scope=scope, client=client):
            stats["relations"] += 1
    store.defer_vectors = False
    stats["embedded"] = store.backfill_vectors()
    return stats


TABLES = ("scope", "episode", "entity", "memory", "relation", "memory_history")


def export_json(store: Store) -> dict:
    """Everything except client tokens. Restoring = copy the SQLite file; this is for humans/tools."""
    return {t: [dict(r) for r in store.conn.execute(f"SELECT * FROM {t}")] for t in TABLES}
