# Memcairn

Self-hosted long-term memory for AI agents, served over MCP.

*A cairn is the pile of stones hikers leave to mark a trail. Every agent session adds a
stone; the next one finds the way.*

One server, many clients: several Claude Code instances, server-side agents and
claude.ai can share the same memory without overwriting each other.

> Status: pre-alpha, phase 2 of the [roadmap](DESIGN.md#roadmap): core store, full-text
> recall, local semantic search in deep mode, HTTP with tokens, import from the official
> memory server.

## Why

- **Small context cost** — the whole tool list is ~900 tokens; a recall returns 5 one-line
  hits (~250 tokens) and full text only for what the model asks for.
- **Instant writes** — storing a memory never waits for an LLM.
- **Safe for concurrent clients** — one SQLite writer, versioning, idempotency keys.
- **Nothing lost silently** — facts are superseded, not overwritten; full history and undo.
- **Transparent** — every result can show its score breakdown and provenance.
- **No lock-in** — importer for `@modelcontextprotocol/server-memory`, JSON export.

## Benchmark

On 998 real facts with 50 queries, compared with the official `server-memory`
([details and caveats](BENCHMARK.md), [feature comparison](COMPARISON.md)):

| | official | Memcairn |
|---|---|---|
| hit@5 | 24% | **98%** |
| tokens per query (mean / max) | 2,055 / 35,765 | **198 / 449** |
| tool list (every turn) | 1,247 | **919** |

## Quick start

The full guide covers server deployment, connecting machines, multiple instances, scopes,
agent instructions, migration and troubleshooting: **[docs/setup.md](docs/setup.md)**.

```bash
uv tool install git+https://github.com/shanislav/memcairn
memcairn import server-memory ~/.claude/memory.json   # optional: migrate old memory
```

### Local (stdio)

```bash
claude mcp add memcairn -- memcairn serve --client my-laptop
```

### Central server (HTTP, many clients)

```bash
memcairn token add laptop          # prints a token once
memcairn serve --transport http --host 0.0.0.0 --port 8765 \
    --public-url https://memory.example.com
```

Put it behind TLS (nginx/caddy) and keep it on a private network. On each client, install
the Claude Code plugin. It asks for the URL and token and keeps the token in the keychain:

```
/plugin marketplace add shanislav/memcairn
/plugin install memcairn@memcairn
```

It adds the tools and a short memory overview at session start, and changes nothing else.
`memcairn setup` offers the optional extras (turn off built-in auto memory, file sync),
each with a diff and a backup; `memcairn unsetup` reverts them. Without the plugin:

```bash
claude mcp add --transport http memcairn https://memory.example.com/mcp \
    --header "Authorization: Bearer <token>"
```

## Tools

| Tool | Purpose |
|---|---|
| `context` | pinned facts, open todos and the most important facts, for session start (≤ 500 tokens; also as plain text at `GET /context` for a SessionStart hook) |
| `recall` | ranked search; `mode=deep` when nothing is found; `explain=true` shows scores; time words in the query ("last week", "v októbri", "2026-10") filter by date |
| `get` | full text of selected ids, optionally with version history |
| `remember` | store one short atomic fact; returns possible duplicates |
| `update` | edit (versioned); `supersede=true` when the fact changed in reality; `status=done` or `dropped` closes a todo |
| `forget` | soft delete, `undo=true` restores |
| `link` | relation between entities |

`--compat` additionally exposes the tool names of the official memory server
(`create_entities`, `search_nodes`, `open_nodes`, …) for existing prompts; it roughly
doubles the tool-list cost.

Writes that look like credentials (API keys, tokens, `password=…`) are refused. New facts
that look like an existing one (same words, or same meaning on the same topic) come back as
candidates to update instead.

**Shared documents** (server inventory, the shared part of `CLAUDE.md`, long plans) are
versioned on the server and synced to the paths you choose with `memcairn sync`. Edits on
different machines are merged three-way, and overlapping edits are never overwritten.

`memcairn chronicle "topic"` writes everything memory knows about a topic as a dated
markdown story, including earlier versions that were replaced. It's documentation
generated on demand, with nothing to maintain. `memcairn doctor` checks a client's setup
and the server's database. It is a CLI
command, not an MCP tool, so it costs agents nothing.

## Development

```bash
uv venv && uv pip install -e ".[dev]"
uv run pytest
```

## License

MIT
