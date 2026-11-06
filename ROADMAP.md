# Roadmap

Where Memcairn is going. Nothing here has a date; the order says what comes first.
Ideas and use cases are welcome as GitHub issues.

## Next

- **Consolidation proposals.** The daily review already finds long and status-like
  memories. Next: pairs of facts that say the same thing, todos nobody touched for
  months, topics that grew past ~30 facts. Each comes as a proposal the agent or the user
  accepts; nothing merges on its own, and originals stay in history.
- **Project scopes from the git remote.** A session in a repository reads and writes that
  project's scope by default, keyed by the normalized remote URL, so the same project on
  two machines shares memory without configuration.
- **"What was true then".** History already keeps every version with its dates. Recall
  should answer "which IP did atlas have in March" from it, not only "which IP does it
  have now".
- **Better recall across languages.** A query in one language finding a fact in another
  is the weakest case today ([BENCHMARK.md](BENCHMARK.md#3-many-languages-synthetic-set)).
  Options: an optional larger embedding model for machines that can afford it, measured
  on all three benchmarks before it becomes a recommendation.
- **One more public benchmark**, LongMemEval, next to LoCoMo.
- **Faster first embedding of a large import.** Embedding 50,000 existing facts in the
  background takes ~17 minutes today; recall works by words meanwhile.

## Later

- **Other MCP clients, tested and documented**: Codex CLI, Gemini CLI, Cursor, and the
  instructions each needs where it ignores MCP instructions.
- **Importers** from other memory systems (Mem0 exports, Basic Memory folders).
- **Web UI**: a timeline per topic (what `memcairn chronicle` prints), bulk cleanup.

## Some day: teams

Today Memcairn is one person's memory on several machines. A scope organizes recall but
is not a permission: any valid token can read and change every fact. Sharing one server
between several people needs, at least:

- **people, not only machines**: an account per person, each with their own machines,
  web UI login and OAuth connections;
- **access per scope**: read or read-write, checked on every tool, `get` and `update`
  included; a private scope per person plus shared team scopes;
- **pins per person**, approved by whoever owns the scope;
- **an audit view**: who changed what, across people;
- auto memory sharing kept personal (it holds one person's preferences).

The data model already records the writing client on every version, so this is mostly
authorization work, not a new storage design. It comes after the single-user path is
solid.

## Not planned

- **An LLM on every write.** Writes stay instant and free; an optional LLM may help with
  consolidation proposals, never silently.
- **A hosted service.** Memcairn is meant to run on your own machine or server.
- **An external database or vector store.** One SQLite file is enough for one person's
  memory: with 50,000 synthetic facts (on a laptop, Intel Core i7-12700H) the file is
  135 MB, a write takes ~2 ms and a recall ~120 ms (deep ~140 ms). It stays the default
  for teams too.
