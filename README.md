# Engrammar

Self-hosted long-term memory for AI agents, served over MCP.

One server, many clients: several Claude Code instances, server-side agents and
claude.ai can share the same memory without overwriting each other.

> Status: early design / pre-alpha. See [DESIGN.md](DESIGN.md).

## Why

- **Instant writes** — storing a memory never waits for an LLM.
- **Ranked recall within a token budget** — hybrid full-text + vector + entity + time search.
- **Safe for concurrent clients** — single SQLite writer, versioning, idempotency.
- **Nothing lost silently** — facts are superseded, not overwritten; full history and undo.
- **Transparent** — every result shows score breakdown and provenance.
- **No lock-in** — markdown mirror, importers for the official MCP memory server and
  Claude Code memory files.

## License

MIT
