# Memcairn

Long-term memory for Claude Code (and other MCP clients) that you host yourself, shared by
all your machines.

*A cairn is the pile of stones hikers leave to mark a trail. Every session adds a stone;
the next one finds the way.*

Claude Code already remembers how you like to work (`CLAUDE.md`, auto memory), but only
on one machine, and only what fits into every session. Memcairn adds the rest: facts
about your projects, servers, decisions and todos, searched when needed, the same on
your laptop, your server and the Claude phone app.

## Install

Paste this into Claude Code:

> Install Memcairn for me, following https://github.com/shanislav/memcairn/blob/main/INSTALL.md

Claude asks whether the memory should stay on this computer or be shared through a
server, sets it up and checks that it works. You never copy a token: machines connect
with a one-time code. [INSTALL.md](INSTALL.md) is what Claude follows; you can follow it
yourself just as well.

The short version:

```bash
# only this computer
uv tool install "memcairn[vector] @ git+https://github.com/shanislav/memcairn"
memcairn connect --local

# several computers: run the server (Docker, see docs/setup.md), then connect each machine
docker compose up -d && docker compose exec memcairn memcairn login-link   # web UI
memcairn pair https://memory.example.com K7QM-42XD    # code from "Add machine"
```

## What you get

- **A short overview at every session start** (pinned facts, open todos, the most used
  facts; ~900 tokens at most) and a `recall` tool for the rest.
- **Small context cost.** The tool list is ~900 tokens; a recall returns five one-line
  hits (~200 tokens), full text only for what the model asks for.
- **Writes are instant**: no LLM call when storing.
- **Nothing is lost silently**: every change is versioned, outdated facts are superseded
  rather than overwritten, todos are closed as done or dropped.
- **Shared documents** (a server inventory, plans) synced to the files you choose, with
  three-way merge; optionally Claude Code's own auto memory carried between your
  machines.
- **A web UI** to browse, search, edit and clean up memory, add machines and revoke them.
- **claude.ai and the Claude apps** through a built-in OAuth server (opt-in).
- **Credentials are refused** on write; `memcairn purge` removes a leaked one for good.
- **One SQLite file.** Semantic search runs locally on the CPU; nothing goes to an
  external service.

## How well it finds things

On 998 real facts with 50 queries, against the official
`@modelcontextprotocol/server-memory` ([method and caveats](BENCHMARK.md)):

| | official | Memcairn |
|---|---|---|
| right answer in the top 5 | 24% | **98%** |
| tokens per query (mean / max) | 2,055 / 35,765 | **198 / 449** |

## Documentation

- [INSTALL.md](INSTALL.md): installation, step by step
- [docs/setup.md](docs/setup.md): the full guide (server, clients, web UI, documents and
  sync, security, migration, troubleshooting)
- [DESIGN.md](DESIGN.md): how it works and why
- [COMPARISON.md](COMPARISON.md): other memory systems
- [CHANGELOG.md](CHANGELOG.md): what changed

## Development

```bash
uv venv && uv pip install -e ".[dev]"
uv run pytest
```

MIT license.
