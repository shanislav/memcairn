# How Memcairn compares

A feature comparison with other long-term memory systems for agents, as of November 2026.
Measured numbers (against the official MCP memory server, on LoCoMo and in nine
languages) are in [BENCHMARK.md](BENCHMARK.md).

| | Memcairn | server-memory (official) | Mem0 / OpenMemory | Graphiti / Zep | Basic Memory | mcp-memory-service | Hindsight | Letta |
|---|---|---|---|---|---|---|---|---|
| License | MIT | MIT | Apache-2.0 | Apache-2.0 | AGPL-3.0 | Apache-2.0 | MIT | Apache-2.0 |
| Storage | SQLite file | JSONL file | Postgres + vector DB | Neo4j / FalkorDB | Markdown + SQLite | SQLite-vec / Cloudflare | Postgres + pgvector | Postgres / git files |
| LLM needed on write | **no** | no | yes | yes | no | no (optional) | yes | yes (agent) |
| Retrieval | FTS (BM25) + coverage + importance + activity; local embeddings in deep mode | substring, unranked | vector + BM25 + entity | BM25 + vector + graph + rerank | FTS + vector | vector + BM25 | 4-way hybrid + rerank | agent search |
| Token budget on results | **yes** | no (dumps full entities) | top-k | top-k | top-k | top-k | yes | – |
| Concurrent writers | single writer, versions, conflict report | file lock | DB | DB | file sync | WAL | DB | DB |
| History / undo | **every version** | no | no | invalidated edges | git | partial | no | git |
| Temporal facts | valid_from/to, supersede, TTL | no | dates | **bi-temporal** | no | decay | timestamps | no |
| Duplicate handling | candidates shown to the agent on write | name only | LLM merge | LLM entity resolution | – | heuristic | LLM | agent |
| Consolidation | daily review, cleanup prompt (originals kept) | no | – | – | – | scheduled "dream" | reflect | sleep-time agents |
| Several machines | per-machine tokens, pairing codes, OAuth for claude.ai | stdio only | API key | none in reference server | cloud | OAuth | bearer | API |
| Secret scanning on write | **yes** | no | no | no | no | no | no | no || Self-hosting footprint | one Python process | one Node process | several containers | graph DB + LLM | one process | one process | Postgres + LLM | server |
| Human-editable files | shared markdown documents synced to files | JSON | no | no | **native markdown** | no | no | MemFS markdown |

## What Memcairn is for

One person's memory shared by all their agents, machines and instances. Writes are free
and instant, recall doesn't eat the context window, and nothing is lost silently.

## What it isn't

- **Automatic knowledge extraction.** Memcairn stores what the agent decides to store.
  Systems that run an LLM on every message (Mem0, Graphiti, supermemory) extract facts on
  their own, at the cost of LLM calls, latency and the occasional invented fact.
- **Strong semantic recall by default.** The default embedding model is small and fast
  and is used only in deep mode; Mem0, Hindsight and Graphiti lean much harder on
  embeddings and LLMs. A larger model can be configured.
- **A team tool, yet.** There are no per-user access rules; any token can read and
  change every fact. Teams are on the [roadmap](ROADMAP.md).
