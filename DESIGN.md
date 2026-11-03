# Memcairn — design

Long-term memory server for AI agents over MCP. One self-hosted server, many clients
(several Claude Code instances on one PC, a server-side agent, claude.ai web/mobile).
Single SQLite file, no external services required.

## Goals

1. **Cheap, instant writes.** Storing a memory never needs an LLM call. Enrichment and
   consolidation are optional background jobs.
2. **Good recall with small context cost.** Hybrid ranked retrieval with a token budget,
   never "dump the whole graph". Token economy is a first-class requirement, see below.
3. **Safe concurrency.** Many clients write at once; nothing is silently lost.
4. **Nothing is destroyed silently.** Facts are superseded or invalidated, every change is
   versioned, everything can be undone.
5. **Transparent.** Every recall result says why it was returned and where it came from.
6. **No lock-in.** Human-readable markdown mirror, import/export of other formats.
7. **Small ops footprint.** `uvx memcairn serve` or one Docker container. SQLite + local
   embedding model on CPU.

Non-goals (for now): multi-tenant SaaS, distributed storage, LLM-mandatory pipelines.

## What we take from whom

| Idea | Source |
|---|---|
| Bi-temporal facts (`valid_from/valid_to` + `recorded_at`), invalidate instead of delete | Graphiti / Zep |
| Supersede chain, contradicted facts hidden from default recall but retrievable | mcp-memory-service, supermemory |
| Hybrid retrieval (BM25 + vector + entity + temporal), RRF fusion, optional cross-encoder rerank, token-budget cutoff | Hindsight, Graphiti, Basic Memory |
| Ranking by recency × importance × access frequency, per-type decay/retention, TTL facts | mcp-memory-service, supermemory |
| Episodes = provenance for every fact | Graphiti |
| Background "dream" consolidation | Letta, mcp-memory-service, Claude Code |
| Small always-loaded core context + on-demand detail | Letta blocks, supermemory `context`, MEMORY.md |
| Memory types (user / feedback / project / reference …) | Claude Code auto-memory |
| Markdown mirror (Obsidian-compatible), two-way | Basic Memory, Letta MemFS |
| Scope per endpoint / header | Hindsight, mcp-memory-service |
| Tool annotations so clients auto-approve reads | Basic Memory |
| Importers from other systems | Basic Memory |
| Entity + relation graph, tool names compatible with the official server | @modelcontextprotocol/server-memory |

## Where we try to be better (gaps nobody solves well)

- **Cheap writes *and* structure** — raw write path, LLM only in optional async jobs.
- **Concurrency as a feature** — single writer, WAL, idempotency keys, optimistic
  versioning (`expected_version`), explicit conflict reporting.
- **Dedup the agent can see** — on write, near-duplicates are returned as candidates;
  consolidation produces *proposals* in a review queue instead of silent merges.
- **Quality signal** — track recall hits, explicit "useful / wrong" feedback, stale detection.
- **Scopes built for coding agents** — user → project (git remote as key, shared across
  worktrees and machines) → machine → session, with inheritance at recall time.
- **Claude Code memory-dir sync** — mirror `~/.claude/projects/*/memory/*.md` into the
  server so file-based auto-memory is shared between machines.
- **Prompt-injection hygiene** — recalled text is wrapped and marked as data; secret
  scanner rejects API keys/passwords on write.
- **Transparency** — scores, provenance, history and undo for every memory.

## Token economy

Memory must help without eating the context window. Every token the server puts into
context has to earn its place. Rules:

**Fixed cost (paid every turn)**
- Few tools: 6–7 core tools with one-line descriptions. Target < 1 000 tokens for the
  whole tool list. Compatibility tools are off by default (they would roughly double it).
- Stable, deterministic tool list and descriptions, so prompt caching keeps working.
- Usage guidance lives in tool descriptions, not in long CLAUDE.md instructions.

**Session start**
- `get_context` returns only pinned + highest-value facts for the current scope,
  hard cap ~500 tokens (configurable). No automatic dump of anything else.

**Recall — progressive disclosure**
- `recall` returns compact hits by default: one line per hit
  (`id · type · entity · ~120-char snippet`), top 5, default budget ~600 tokens.
- Full text only on demand: `get(ids)` for the few hits the agent actually needs.
- Score breakdown and provenance only with `explain=true`.
- Plain-text output, not verbose JSON (keys and quotes cost tokens).
- The server does the ranking/filtering/dedup of results, so the model reads 5 good
  lines instead of 50 mediocre ones.
- Superseded / expired facts are excluded unless explicitly requested.

**Writes**
- Response is one line: `ok <id>` (+ at most 3 duplicate-candidate snippets).
- Facts are short and atomic; content over ~500 chars gets a warning suggesting a split.

**Keeping the store lean**
- Consolidation merges duplicates and summarizes overgrown entities, so recall hits
  fewer, better facts over time.
- No `read_graph`-style dump; the compat `read_graph` is capped and paginated.

**Measured, not assumed**
- Every response size is logged; `memcairn stats` shows tokens per call type.
- The eval suite tracks recall quality *and* tokens returned per query; a change that
  improves quality but blows the budget fails the check.

## Data model (SQLite)

```
scope        id, kind(user|project|machine|session|custom), key, parent_id, name
episode      id, scope_id, client_id, session_ref, created_at, raw (optional)
entity       id, scope_id, name, type, aliases[json], summary, created_at, updated_at
memory       id (ULID), scope_id, type, content, importance(0-1), confidence(0-1),
             pinned, tags[json], valid_from, valid_to, recorded_at, expires_at,
             superseded_by, deleted_at, version, episode_id,
             access_count, last_accessed_at, useful_count, wrong_count
memory_entity   memory_id, entity_id, role
relation     id, scope_id, from_entity, to_entity, type, valid_from, valid_to,
             recorded_at, superseded_by, episode_id
memory_history  memory_id, version, changed_at, client_id, op, snapshot[json]
memory_fts   FTS5(content, tags, entity names)        — BM25, unicode61 remove_diacritics
memory_vec   sqlite-vec(embedding)                     — optional extra
proposal     id, kind(merge|split|retype|expire|summarize), payload[json], status, created_at
client       id, name, token_hash, default_scope_id, created_at, last_seen_at
```

- One memory = one atomic fact (short). Entities group facts; an "entity summary" is a
  derived, regenerable field — never the source of truth.
- Entity types are normalized (`project`/`projekt` → `project`) via an alias table.
- `deleted_at` = soft delete; hard purge only via explicit admin command.

## Retrieval pipeline

1. Parse query: free text, optional filters (scope, types, tags, entity, time range
   like "last week" / `as_of`).
2. Candidates in parallel: FTS5 BM25, vector kNN (if enabled), entity-name match,
   temporal filter.
3. Reciprocal Rank Fusion → optional cross-encoder rerank.
4. Final score blends relevance with importance, recency decay (half-life per type),
   access/feedback signal; pinned items boosted.
5. Default view hides superseded / invalidated / expired facts (`include_history=true`
   to see them, `as_of=<date>` for point-in-time).
6. Cut to `max_tokens` budget; each hit returns id, content, scope, entity, score
   breakdown, provenance.

### Deep search (fallback when normal recall finds nothing)

Same `recall` tool with `mode="deep"` — no extra tool, so no extra fixed token cost.
Normal recall answers with a one-line hint (`low confidence — try mode=deep`) when its
best score is weak. Deep mode widens the net instead of the output:
- query expansion: synonyms/aliases, entity aliases, cross-language match via the
  multilingual embedding model; optional LLM query rewrite if an LLM is configured;
- fuzzy / trigram matching for typos and diacritics variants;
- graph traversal 1–2 hops from matched entities and relations;
- includes superseded, invalidated and expired facts (marked as such) and raw episode text;
- larger candidate pool, but output stays compact (default budget ~1 500 tokens).

Embeddings: fastembed with a multilingual model (memories are not English-only),
lazily computed on write in a background worker; FTS works without them.

## Write path

- `remember(content, type, entity?, tags?, importance?, valid_from?, expires_at?,
  idempotency_key?)` → stored immediately, returns id + up to N near-duplicate
  candidates so the agent can decide to `update`/`supersede` instead.
- `update(id, content, expected_version)` → new version in history; on version mismatch
  returns a conflict with the current state.
- `supersede(old_id, new_content)` → new fact, old one gets `valid_to` + `superseded_by`.
- `forget(id, reason)` → soft delete (destructiveHint).
- Secret scanner on every write.

## Consolidation ("dream")

Scheduled or on demand, as an MCP task / CLI command:
- find duplicate clusters, overgrown entities (e.g. > 30 facts), type drift, expired TTLs,
  never-recalled stale facts;
- without an LLM: heuristics + vector clustering; with an LLM (any OpenAI-compatible or
  Anthropic API, optional): summaries, merges, splits;
- output is **proposals** in a review queue; auto-apply is opt-in per proposal kind.

## MCP surface

Tools (deterministic order, annotated):

| Tool | Annotation |
|---|---|
| `recall` (query, filters, mode=normal\|deep, max_tokens, explain) — compact hits | readOnly |
| `get` (ids) — full text of selected hits | readOnly |
| `get_context` (scope) — pinned + top facts within budget | readOnly |
| `remember` | idempotent with key |
| `update`, `supersede`, `link` (relation), `feedback` (useful/wrong) | — |
| `forget` | destructive |
| `history` (id) | readOnly |
| `review` (list / apply / reject proposals) | — |

Compatibility mode (flag): official server-memory tool names (`create_entities`,
`add_observations`, `search_nodes`, `open_nodes`, `create_relations`, `delete_*`,
`read_graph` with a size cap) so existing prompts keep working.

Resources: `memcairn://context/{scope}`, `memcairn://entity/{name}`.
Prompts: `recall-for-task`, `save-session`.

Transports: stdio and Streamable HTTP (built on the official `mcp` Python SDK so spec
changes are handled upstream).
Auth: static bearer token per client (name used for provenance); OAuth 2.1 for
claude.ai custom connectors in a later phase.

## Interfaces besides MCP

- CLI: `memcairn serve | import | export | dream | review | token add/revoke | doctor`
- Markdown mirror: export/import folder, one file per entity with facts as list items;
  Claude Code memory-dir sync.
- Web dashboard (later): search, browse, graph, history/undo, review queue, stats.

## Importers / exporters

- official `server-memory` JSONL (`memory.json`)
- Claude Code memory dir (`MEMORY.md` + topic files)
- Basic Memory markdown
- full JSON export / import (lossless backup)

## Stack

Python 3.11+, `mcp` SDK (FastMCP), stdlib `sqlite3` with FTS5, `sqlite-vec`,
`fastembed` (optional extra), Starlette/uvicorn for HTTP, Typer CLI, pytest.
Packaging: PyPI (`uvx memcairn`), Docker image.

## Roadmap

| Phase | Content |
|---|---|
| 0 | Repo skeleton, schema + migrations, CI, tests |
| 1 ✅ | Core store, FTS recall, remember/update/supersede/forget/history, stdio + HTTP with bearer tokens, import `memory.json`, compatibility tools → deploy for own use |
| 2 (partly ✅) | Done: local embeddings (fastembed, brute-force cosine), hybrid scoring in deep mode, relative cutoff of weak hits. Open: vector-based near-duplicates, temporal queries ("last week", `as_of`) |
| 3 | Markdown mirror + Claude Code memory-dir sync |
| 4 | Consolidation jobs + review queue, optional LLM backend |
| 5 | Web dashboard, OAuth for claude.ai connectors |
| 6 | Retrieval eval suite, PyPI + Docker release, docs |
