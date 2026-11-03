# Benchmark: Memcairn vs the official MCP memory server

The test compares Memcairn with
[`@modelcontextprotocol/server-memory`](https://github.com/modelcontextprotocol/servers/tree/main/src/memory)
(version 2025.9.25) on real data. That server is the official reference memory server
and what many Claude Code setups use. The harness is
[`bench/bench.py`](bench/bench.py) and you can run it on your own `memory.json`.

## Setup

- **Data:** one person's real memory after five months of daily Claude Code use: 73
  entities, 998 facts and 20 relations. The text is mostly Slovak with some English and Czech.
  The data is private, so this file publishes only aggregate numbers.
- **Both servers** run as real MCP subprocesses over stdio, each on its own copy of the file.
  Memcairn gets the same file through its importer, with no manual cleanup.
- **Queries:** 50 queries in 7 categories, each with the expected entities. The queries were
  written to look like what an agent asks while it works:

| Category | n | Example shape |
|---|---|---|
| keyword | 10 | a single word, as the official server's usage advice recommends |
| question | 10 | "how do I deploy the X app", "which port is SSH on for host Y" |
| multi-keyword | 14 | "storage nas drbd", "postgres vip" |
| no-diacritics | 4 | a Slovak query typed without accents |
| inflection | 4 | a different grammatical case than the stored text |
| cross-language | 4 | an English query for facts stored in Slovak |
| negative | 4 | topics that are not in memory at all |

- **Official server:** one `search_nodes` call per query.
- **Memcairn:** one `recall` call. When the reply carries the `low confidence — try
  mode=deep` hint, a second call follows with `mode=deep`, as an agent would do. Tokens for
  both calls are counted.
- **Token count:** characters / 3.5, applied identically to both systems. The absolute
  numbers are estimates; the ratios are what matter.

## Results

"Phase 1" is full-text search only. "Phase 2" adds semantic search with a local
embedding model ([`potion-multilingual-128M`](https://huggingface.co/minishlab/potion-multilingual-128M))
in `mode=deep`.

| | server-memory (official) | Memcairn phase 1 | Memcairn phase 2 |
|---|---|---|---|
| Tool list (paid on every turn) | 1,247 tok | 919 tok | 919 tok |
| hit@1 | 15% | **89%** | **89%** |
| hit@5 | 24% | **98%** | **98%** |
| MRR | 0.19 | **0.93** | **0.93** |
| Relevant queries that returned nothing | 34 of 46 | **0 of 46** | **0 of 46** |
| Tokens per query: mean | 2,055 | 221 | **198** |
| Tokens per query: max | 35,765 | **449** | **449** |
| Tokens across all 50 queries | 102,736 | 11,055 | **9,896** |
| Tokens per query, single-keyword queries only | 10,017 | **225** | – |
| Tokens on negative queries: mean | **12** | 160 | 138 |
| Median latency | **2.7 ms** | 7.9 ms | 8.5 ms |

hit@5 by category:

| | official | Memcairn |
|---|---|---|
| keyword | 8/10 | 10/10 |
| question | 0/10 | 9/10 |
| multi-keyword | 2/14 | 14/14 |
| no-diacritics | 0/4 | 4/4 |
| inflection | 1/4 | 4/4 |
| cross-language | 0/4 | 4/4 |

## What the numbers mean

**The official server mostly finds nothing or floods the context.** It matches the whole
query as one substring. Any query of two or more words that doesn't appear verbatim
returns nothing; that covers 34 of the 46 queries with a known answer. When it does
find something, it returns every matching entity with all of its facts, in file order and
unranked. That averaged 8,500 tokens for those queries; one query for a three-letter
keyword returned 35,765 tokens. The "12 tokens" median is an empty result.

**Memcairn answers with five ranked lines**, about 220 tokens. The model fetches full
text only for the hits it needs. Diacritic folding, crude stemming and stop-word removal
let a Slovak question find a fact written in another grammatical case or without accents.

**Where the official server is better:**
- On queries about things that are not in memory it returns an empty result (12 tokens).
  Memcairn returns its few closest weak matches marked `low confidence` (160 tokens on
  average).
- It is a few milliseconds faster. A single-digit millisecond difference doesn't matter
  for an LLM tool call.

## Caveats (read before quoting)

- **The queries were written by Memcairn's author, who knows the data.** The categories
  show where each system breaks, but the set is small (50) and not adversarial.
- **Two general fixes came out of the first run:**
  - stop words (sk/cs/en) are dropped from queries;
  - weak results are trimmed to 3 lines.

  Before them, Memcairn had 96% hit@5 and spent 324 tokens on negative queries. One
  expected answer in the query set was also corrected: the "memory leak" fact lives in a
  different entity than first assumed.
- **Cross-language hits in phase 1 were partly luck.** The stored text has some English
  words mixed in. This query set is weak at showing what embeddings add: most queries share
  at least one word with the answer. Its cross-language part has only 4 queries.
- **Phase 2: where semantic search goes.** Adding vector candidates to *every* recall made
  results slightly worse on this set: 96% then 94% hit@5, plus more tokens. Semantic search
  always finds *something*, and that padded the lists. Vectors are therefore used only in
  `mode=deep`, which an agent calls when full-text recall reports low confidence, i.e. when
  words didn't match. Phase 2 also drops hits that score far below the best one.
- **Which model.** Five multilingual models were compared on the same data, vector search
  only, hit@5:

  | Model | hit@5 | Embedding 998 facts |
  |---|---|---|
  | `potion-multilingual-128M` | 93% | 0.5 s |
  | `embeddinggemma-300m` | 91% | 686 s |
  | `paraphrase-multilingual-mpnet-base-v2` | 87% | 395 s |
  | `paraphrase-multilingual-MiniLM-L12-v2` | 76% | 46 s |

  Embedding times are on 3 server CPU cores. `Qwen3-Embedding-0.6B` didn't fit in a 6 GB
  memory limit. The static potion model was both the best and by far the cheapest.
- **Still missed:** "how to deploy the app" against a fact titled "deploy procedure" in
  Slovak. The small model doesn't link the Slovak verb *nasadiť* to *deploy*.
- **This is not a comparison with Mem0, Graphiti, Hindsight and similar systems.** Those
  need an LLM on every write; see [COMPARISON.md](COMPARISON.md) for a feature comparison.

## Reproduce

```bash
uv pip install -e ".[dev]"
python bench/bench.py --memory-json path/to/memory.json --queries my-queries.json
```

`my-queries.json`: `[{"q": "...", "cat": "keyword", "expect": ["Entity name"]}, ...]`.
The harness works on a temporary copy and never modifies the input file.
