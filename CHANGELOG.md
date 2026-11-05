# Changelog

## 0.1.0 — 2026-11-05

First release. Self-hosted long-term memory for Claude Code, claude.ai and other MCP
clients: one SQLite file, one process, light on tokens (tool list ~900 tokens, a recall
~200).

**Install and connect**
- [INSTALL.md](INSTALL.md): tell Claude Code to install it; it asks whether the memory
  stays on this computer or is shared through a server, and sets it up.
- No token copying: machines connect with one-time pairing codes (`memcairn pair`,
  "Add machine" in the web UI, `memcairn add-machine`); `memcairn connect --local` for one
  computer without a server.
- Claude Code plugin (memory tools, session-start overview), or the manual entry that
  `pair` writes, which also keeps a claude.ai connector to the same server from loading
  twice.
- OAuth 2.1 (opt-in) for claude.ai and the Claude apps; the approval page takes a
  pairing code.

**Memory**
- Recall: full-text search with stemming, sk/cs/en stop words, synonym groups, time words
  in the query ("last week", "v októbri"), weak hits cut off; `mode=deep` adds local
  semantic search (no external API).
- Writes: short atomic facts, every version kept, supersede chains, duplicate candidates,
  todos with done/dropped, credentials refused.
- Session start: a ranked overview (pinned facts, open todos, most useful facts).
- Shared documents, searchable part by part, synced to files with three-way merge;
  optional sharing of Claude Code's auto memory between your machines.

**Web UI** (`/ui`)
- Overview, search, edit with history, forget/restore/delete, documents, cleanup review,
  topics rename/merge, add and revoke machines.
- Password login (sessions renew on use), one-time login links, `memcairn ui` locally.

**Maintenance**
- Daily review with a self-closing cleanup todo and the `cleanup` prompt; `doctor`,
  `stats`, `chronicle`, `backup`, `purge` (removes a leaked string for good), importers for
  the official memory server and Claude Code memory folders.
- Docker image `ghcr.io/shanislav/memcairn` for amd64 and arm64.
