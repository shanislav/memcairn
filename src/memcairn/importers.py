"""Importers from other memory formats and lossless JSON export."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

from .secretscan import SecretError
from .store import NotFound, Store


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


ESTIMATED = "date:estimated"  # tag: the text gave no date, recorded_at is a guess
# A record date opens the fact ("2026-10-01 switched…") or heads it, followed by a colon
# or dash ("FIX Chimera1C7 (.211) 2026-07-01: …"), within the first 40 characters.
RECORD = re.compile(r"^\W*(20\d\d-[01]\d-[0-3]\d)\b|^.{0,40}?\b(20\d\d-[01]\d-[0-3]\d)\s*[:—–-]")


def record_date(obs: str) -> str | None:
    """The date a fact was written, if it says. A date inside the text ("battery made
    2005-10-06, …") is about something else and says nothing about when it was noted."""
    m = RECORD.search(obs)
    day = (m.group(1) or m.group(2)) if m else None
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    return day if day and day <= today else None


def fact_dates(observations: list[str], fallback: str | None = None
               ) -> list[tuple[str | None, bool]]:
    """(recorded_at, estimated) per fact, as far as the text says.

    server-memory keeps no timestamps, but agents often start facts with a date
    ("2026-10-01: switched prod to the fork"). Facts without one inherit the newest date
    of their entity. An entity with no dates at all gets `fallback` (the oldest record
    date of the whole import: it existed by then at the latest). Inherited and fallback
    dates are marked as estimated.
    """
    found = [record_date(o) for o in observations]
    latest = max((d for d in found if d), default=fallback)
    return [(f"{d or latest}T00:00:00Z" if (d or latest) else None, d is None)
            for d in found]


def import_server_memory(store: Store, path: str | Path, *, scope: str = "global",
                         client: str = "import") -> dict[str, int]:
    entities, relations = read_server_memory(path)
    episode = store.episode(scope, client, raw=f"import from {Path(path).name}")
    stats = {"entities": 0, "facts": 0, "relations": 0, "skipped_secrets": 0}
    store.defer_vectors = True  # embed everything in batches at the end
    oldest = min((d for e in entities for o in e.get("observations", [])
                  if (d := record_date(o))), default=None)
    for ent in entities:
        name = ent["name"]
        with store._tx():
            store.entity_id(name, scope, ent.get("entityType"))
        stats["entities"] += 1
        observations = ent.get("observations", [])
        for obs, (recorded, guess) in zip(observations, fact_dates(observations, oldest)):
            try:
                store.remember(obs, entity=name, scope=scope, client=client, episode_id=episode,
                               recorded_at=recorded, check_duplicates=False,
                               tags=[ESTIMATED] if guess and recorded else None)
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


# ---------- Claude Code file memory (~/.claude/projects/<project>/memory/*.md) ----------

FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.S)
LINK = re.compile(r"\[\[([^\]]+)\]\]")
NAME_PREFIX = re.compile(r"^(project|feedback|reference|user)[-_ ]+", re.I)


def parse_memory_file(text: str) -> tuple[dict, str]:
    """Frontmatter (flat keys plus one level of `metadata:`) and body; no YAML dependency."""
    m = FRONTMATTER.match(text)
    if not m:
        return {}, text.strip()
    meta: dict = {}
    for line in m.group(1).splitlines():
        if not line.strip() or ":" not in line:
            continue
        key, _, value = line.strip().partition(":")
        value = value.strip().strip('"').strip("'")
        if value:
            meta.setdefault(key.strip(), value)  # nested keys land flat; first one wins
    return meta, text[m.end():].strip()


def topic(name: str) -> str:
    """'project-lumina' → 'lumina' (matches an existing 'Lumina' entity, case-insensitive)."""
    return NAME_PREFIX.sub("", name).replace("-", " ").replace("_", " ").strip() or name


DOC_THRESHOLD = 1500  # chars: longer files are documents, not facts


def doc_name(stem: str) -> str:
    return re.sub(r"[^a-z0-9._-]+", "-", stem.lower()).strip("-.")[:80] or "doc"


def import_claude_memory(store: Store, folder: str | Path, *, scope: str = "global",
                         client: str = "import", skip: list[str] | None = None,
                         doc_threshold: int = DOC_THRESHOLD) -> dict[str, int]:
    """Short notes become one memory each (description + body); long files become shared
    documents (whole file, versioned) plus a `reference` memory pointing at them, so
    recall finds them and `get("doc:<name>")` or `memcairn sync` opens them. [[links]]
    become relations.

    Re-running is safe: memories are keyed by file name and content hash, documents by
    content hash. MEMORY.md (the index) and files matching `skip` are left out.
    """
    import fnmatch
    import hashlib

    from . import docs

    folder = Path(folder)
    stats = {"files": 0, "facts": 0, "documents": 0, "relations": 0, "skipped": 0,
             "skipped_secrets": 0}
    store.defer_vectors = True
    links = []
    for path in sorted(folder.glob("*.md")):
        if path.name == "MEMORY.md" or any(fnmatch.fnmatch(path.name, p) for p in skip or []):
            stats["skipped"] += 1
            continue
        text = path.read_text(encoding="utf-8")
        meta, body = parse_memory_file(text)
        stats["files"] += 1
        name = meta.get("name") or path.stem
        entity = topic(name)
        desc = meta.get("description", "")
        content = f"{desc}\n{body}".strip() if desc and desc not in body else body or desc
        recorded = meta.get("modified") or datetime.fromtimestamp(
            path.stat().st_mtime, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        key = f"claude-memory:{path.name}:{hashlib.sha1(text.encode()).hexdigest()[:12]}"
        mtype = (meta.get("type") or "fact").lower()
        try:
            if len(content) > doc_threshold:
                name = doc_name(path.stem)
                try:
                    current = docs.get_doc(store, name)["version"]
                except NotFound:
                    current = 0
                docs.put_doc(store, name, text, base_version=current, client=client)
                stats["documents"] += 1
                content = (f"{desc or entity} — full text in shared document '{name}' "
                           f"(get ids=[\"doc:{name}\"], or memcairn sync).")
                mtype = "reference"
            store.remember(content, type=mtype, entity=entity, scope=scope, client=client,
                           recorded_at=recorded[:19] + "Z", idempotency_key=key,
                           check_duplicates=False)
            stats["facts"] += 1
        except SecretError:
            stats["skipped_secrets"] += 1
        links += [(entity, topic(t.strip())) for t in LINK.findall(body)]
    for a, b in links:
        if a != b and store.link(a, b, "related_to", scope=scope, client=client):
            stats["relations"] += 1
    store.defer_vectors = False
    stats["embedded"] = store.backfill_vectors()
    return stats
