# Changelog

## Unreleased

- **Fixed: several tool calls at once could fail** ("Error executing tool recall", or a
  "not found" for an id that exists). The server shares one database connection between
  the threads that run tool calls; reads now take the same lock as writes.
- `type=` and `entity=` filters in recall now leave document parts out (they have
  neither), instead of letting them through.
- When a date in the query had to be widened or dropped, the answer says so.
- `recall` with `type=todo` and no topic words ("todo", "open todos") lists the open
  todos, as many as fit; closed ones only in deep mode.
- Forms of "to be" (sk/cs/en) are stop words: "why was X removed" no longer ranks facts
  that only share "was".
- The `cleanup` prompt checks old logs against newer facts: details that were replaced
  later go into the conclusion as history, and only still-open steps become todos.
- **Only you pin.** Pinned facts reach every session on every machine, so an agent can
  only ask: `pinned=true` from an agent (or a machine token) stores the fact unpinned and
  adds a pin request, and changing the text of a pinned fact unpins it until approved
  again. Approve or reject requests at the top of the web UI overview; the session
  overview shows only how many wait.
- Dates with a day in a query ("3 June, 2023", "April 10, 2023", "3. júna 2023",
  "3.6.2023") are understood; before, only the month was read, and of the wrong year.
- A query date with nothing noted on it no longer returns nothing: recall looks a week
  either side, then at any time (the date said when something happened, not when it was
  written down).
- **Chinese, Japanese, Thai and Korean** are searched without vectors too: text in
  scripts without spaces is indexed as overlapping character pairs (before, a whole
  sentence was one word). Existing databases are re-indexed once on start.
- **Word weights from your data** (IDF): a word in many facts counts less than a rare
  one, in any language, instead of every word counting the same.
- Deep mode ranks facts in another language better (similarity also counts relative to
  the best match).
- Public benchmarks: retrieval on LoCoMo (`bench/locomo.py`) and a synthetic
  nine-language set (`bench/multilingual.py`); results in BENCHMARK.md.
- Tests for several writers on one database at once and for a failure halfway through
  a write.
- Docs: [ROADMAP.md](ROADMAP.md) (including what teams would need), current numbers
  everywhere, a section on search and languages, clearer notes that scopes are not
  permissions.

## 0.1.2 — 2026-11-06

- The Docker image installs the exact dependency versions from `uv.lock`: a build of a
  given commit is reproducible.
- `compose.yml` runs a fixed release (`MEMCAIRN_VERSION` in `.env`) instead of `latest`;
  updating is a step you choose (back up, set the version, pull).
- One-time codes (pairing, login links, the OAuth approval page) are limited to 20 wrong
  attempts per 15 minutes, like passwords.
- `pair`, `connect` and `setup` write `~/.claude.json` and `settings.json` atomically and
  re-read them just before writing, so a running Claude Code never sees half a file and
  its own changes are kept.

## 0.1.1 — 2026-11-05

- On PyPI: `uv tool install memcairn` (add `[vector]` for search by meaning), updates with
  `uv tool upgrade memcairn`. Installing from git still works.
- Package metadata (links, classifiers); the source package leaves out images and CI files.

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
  semantic search (no LLM, no external API; ~0.6 GB of memory, optional).
- Writes: short atomic facts, every version kept, supersede chains, duplicate candidates,
  todos with done/dropped, credentials refused.
- Session start: a brief overview (~200 tokens: topics and documents memory covers,
  pinned facts, open todo count); `mode=full` adds ranked facts.
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
