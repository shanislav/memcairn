# Engrammar

Self-hosted long-term memory for AI agents, served over MCP.

One server, many clients: several Claude Code instances, server-side agents and
claude.ai can share the same memory without overwriting each other.

> Status: pre-alpha, phase 1 of the [roadmap](DESIGN.md#roadmap) (core store, full-text
> recall, HTTP with tokens, import from the official memory server).

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

| | official | Engrammar |
|---|---|---|
| hit@5 | 24% | **98%** |
| tokens per query (mean / max) | 2,055 / 35,765 | **221 / 449** |
| tool list (every turn) | 1,247 | **919** |

## Quick start

The full guide covers server deployment, connecting machines, multiple instances, scopes,
agent instructions, migration and troubleshooting: **[docs/setup.md](docs/setup.md)**.

```bash
uv tool install git+https://github.com/shanislav/engrammar
engrammar import server-memory ~/.claude/memory.json   # optional: migrate old memory
```

### Local (stdio)

```bash
claude mcp add engrammar -- engrammar serve --client my-laptop
```

### Central server (HTTP, many clients)

```bash
engrammar token add laptop          # prints a token once
engrammar serve --transport http --host 0.0.0.0 --port 8765 \
    --public-url https://memory.example.com
```

Put it behind TLS (nginx/caddy) and keep it on a private network. On each client:

```bash
claude mcp add --transport http engrammar https://memory.example.com/mcp \
    --header "Authorization: Bearer <token>"
```

## Tools

| Tool | Purpose |
|---|---|
| `context` | pinned and most important facts, for session start (≤ 500 tokens) |
| `recall` | ranked search; `mode=deep` when nothing is found; `explain=true` shows scores |
| `get` | full text of selected ids, optionally with version history |
| `remember` | store one short atomic fact; returns possible duplicates |
| `update` | edit (versioned); `supersede=true` when the fact changed in reality |
| `forget` | soft delete, `undo=true` restores |
| `link` | relation between entities |

`--compat` additionally exposes the tool names of the official memory server
(`create_entities`, `search_nodes`, `open_nodes`, …) for existing prompts; it roughly
doubles the tool-list cost.

Writes that look like credentials (API keys, tokens, `password=…`) are refused.

## Development

```bash
uv venv && uv pip install -e ".[dev]"
uv run pytest
```

## License

MIT
