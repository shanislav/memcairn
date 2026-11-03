# How Memcairn compares

A feature comparison with other long-term memory systems for agents, as of November 2026.
For measured numbers against the official MCP memory server, see
[BENCHMARK.md](BENCHMARK.md). Star counts are approximate.

Memcairn's own column shows the current state as of phase 1; items marked *(planned)*
come from the [roadmap](DESIGN.md#roadmap).

## At a glance

| | Memcairn | server-memory (official) | Mem0 / OpenMemory | Graphiti / Zep | Basic Memory | mcp-memory-service | Hindsight | Letta |
|---|---|---|---|---|---|---|---|---|
| License | MIT | MIT | Apache-2.0 | Apache-2.0 | AGPL-3.0 | Apache-2.0 | MIT | Apache-2.0 |
| Storage | SQLite file | JSONL file | Postgres + vector DB | Neo4j / FalkorDB | Markdown + SQLite | SQLite-vec / Cloudflare | Postgres + pgvector | Postgres / git files |
| LLM needed on write | **no** | no | yes | yes | no | no (optional) | yes | yes (agent) |
| Retrieval | FTS (BM25) + coverage + importance + recency; hybrid vector *(planned)* | substring, unranked | vector + BM25 + entity | BM25 + vector + graph + rerank | FTS + vector | vector + BM25 | 4-way hybrid + rerank | agent search |
| Token budget on results | **yes** | no (dumps full entities) | top-k | top-k | top-k | top-k | yes | – |
| Concurrent writers | single writer, versions, conflict report | file lock | DB | DB | file sync | WAL | DB | DB |
| History / undo | **every version** | no | no | invalidated edges | git | partial | no | git |
| Temporal facts | valid_from/to, supersede, TTL | no | dates | **bi-temporal** | no | decay | timestamps | no |
| Duplicate handling | candidates shown to agent on write | name only | LLM merge | LLM entity resolution | – | heuristic | LLM | agent |
| Consolidation | *(planned, as review proposals)* | no | – | – | – | scheduled "dream" | reflect | sleep-time agents |
| Scopes | hierarchical (global → project → …) | none | user / agent / run | group_id | projects | tags / agent header | banks | per agent |
| Multi-client HTTP + auth | per-client tokens, provenance | stdio only | API key | none in reference server | cloud | OAuth | bearer | API |
| Secret scanning on write | **yes** | no | no | no | no | no | no | no |
| Self-hosting footprint | one Python process | one Node process | several containers | graph DB + LLM | one process | one process | Postgres + LLM | server |
| Human-editable mirror | *(planned)* | JSON | no | no | **native markdown** | no | no | MemFS markdown |

## What Memcairn is for

You want one memory shared by several agents or instances. Writes should be free and
instant. Recall must not eat the context window. Nothing should ever be lost silently.

## What it isn't (yet)

- **Semantic recall.** Without the planned vector index, a query with no words in common
  with the stored text ("deploy" vs a fact that says "rollout") can miss. Mem0, Hindsight
  and Graphiti handle this today.
- **Automatic knowledge extraction.** Memcairn stores what the agent decides to store.
  Systems that run an LLM on every message (Mem0, Graphiti, supermemory) extract facts on
  their own, at the cost of LLM calls, latency and occasional hallucinated facts.
- **Polished UI.** Basic Memory (Obsidian) and mcp-memory-service (dashboard) are ahead.

## Ideas taken from the others

The bi-temporal model and invalidation come from Graphiti. The supersede chain comes
from mcp-memory-service and supermemory. Hybrid ranking with a token budget comes from
Hindsight and Graphiti. Memory types and the always-loaded core context come from
Claude Code and Letta. Tool annotations come from Basic Memory. The full list is in
[DESIGN.md](DESIGN.md#what-we-take-from-whom).
