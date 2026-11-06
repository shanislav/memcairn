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
  Slovak. The small model doesn't link the Slovak verb *nasadiť* to *deploy*. Later
  synonym groups in plain recall (`nasad deploy`, …) fixed this one; the tables above are
  from the original run.
- **This is not a comparison with Mem0, Graphiti, Hindsight and similar systems.** Those
  need an LLM on every write; see [COMPARISON.md](COMPARISON.md) for a feature comparison.

## Public data: LoCoMo (English)

The test above uses the author's own data and queries. As an independent check, retrieval
was also measured on [LoCoMo](https://github.com/snap-research/locomo) (Maharana et al.,
2024). It has 10 long conversations between two people (~600 turns each, spread over
months) and 1,540 questions in categories 1–4, each labelled with the turns that hold
the answer. Each conversation gets its own fresh memory. Every turn is stored as a fact
(`Speaker: text`, entity = speaker, recorded on the session date). A question counts as
found when an evidence turn is among the first 5 results (hit@5); recall@5 is the share
of all its evidence turns found.

hit@5 / recall@5:

| system | multi-hop | temporal | open-domain | single-hop | all |
|---|---|---|---|---|---|
| FTS5 BM25 alone | 33 / 15 | 58 / 54 | 26 / 19 | 58 / 57 | 51 / 47 |
| vector search alone (same model) | 32 / 14 | 45 / 42 | 22 / 16 | 38 / 37 | 38 / 33 |
| Memcairn recall | 37 / 18 | **65 / 62** | 29 / 20 | **66 / 65** | **58 / 53** |
| Memcairn recall, `mode=deep` | **44 / 21** | 60 / 57 | 28 / 21 | 60 / 59 | 55 / 49 |
| agent: recall, then deep on low confidence | 40 / 21 | 64 / 61 | **30 / 23** | 62 / 61 | 57 / 51 |

The last row is what an agent does when it follows the MCP instructions.

What this shows, and what it doesn't:
- **Only retrieval is measured.** No LLM reads the results, so these are not answer
  accuracy and can't be compared with the "LLM judge" scores that Mem0, Zep and others
  report on LoCoMo.
- **Raw turns are a hard case for Memcairn.** It is built for short facts an agent
  writes, not for chat lines like "Sounds great! What kind of books you got?". When the
  dataset's own extracted facts ("observations") are stored instead, plain recall gets
  58 / 52, `mode=deep` 63 / 55 and the agent 61 / 54; vectors help more there
  (multi-hop: deep 62 / 31, BM25 alone 43 / 19).
- **The first run found two bugs, both fixed.** Dates with a day ("3 June, 2023",
  "April 10, 2023") were read as only the month, and of the wrong year. A date with
  nothing written on it returned nothing; now recall looks a week either side, then at
  any time. Together they took Memcairn from 53 to 58 hit@5. The author's set above went
  from 98% to 100%.
- **Deep mode is not better on every question.** It helps multi-hop questions, where
  words don't match, and costs single-hop ones. This matches the author's set: vectors
  only when plain recall is unsure.

## Many languages (synthetic set)

[`bench/multilingual.json`](bench/multilingual.json) is a public test set written for
this: 92 short facts about a fictional company, noted in English, German, Spanish,
French, Russian, Chinese, Japanese, Slovak and Czech, and 101 queries (92 with an
answer, 9 without). Queries are keywords, questions, inflected forms, typos, text without
diacritics, a fact that was later superseded (the current one must come back), and 14
queries in another language than the fact. It is small and written by the author, so it
shows where each part breaks rather than a score to quote.

hit@5 by kind of query:

| | n | BM25 alone | Memcairn | vectors alone | Memcairn deep | agent |
|---|---|---|---|---|---|---|
| keyword | 17 | 100% | 100% | 100% | 100% | 100% |
| question | 40 | 98% | 100% | 98% | 100% | 100% |
| inflection | 10 | 80% | 100% | 100% | 100% | 100% |
| no diacritics | 5 | 100% | 100% | 100% | 100% | 100% |
| typo | 3 | 100% | 100% | 100% | 100% | 100% |
| superseded (current) | 3 | 100% | 100% | 100% | 100% | 100% |
| cross-language | 14 | 43% | 50% | **79%** | 64% | 64% |
| **all with an answer** | 92 | 88% | 92% | **96%** | 95% | 95% |
| no answer: reported as low confidence | 9 | – | 100% | – | 100% | 100% |

Every language reaches 100% with the agent, except English (83%) and French (90%),
where the misses are cross-language queries.

What changed because of this set:
- **Chinese and Japanese.** They have no spaces between words, so the full-text index saw
  each sentence as one word: plain recall found 22% (Chinese) and 30% (Japanese) of
  their queries. Text in such scripts is now indexed and queried as overlapping character
  pairs, and both are at 100% (the plain recall column above; overall 76% → 92%).
- **Word weights from the data.** Coverage (how much of the query a fact matches) used
  to count every word the same, so a fact matching only a frequent word ("server", a
  speaker's name) looked as good as one matching the rare word that matters. Words now
  weigh by how many entries contain them (IDF), in any language, without stop-word
  lists.
- **Similarity in deep mode** also counts relative to the best one in the result, as
  bm25 does: across languages a small model's similarities stay low, and on an absolute
  scale they couldn't outrank a partial word match. Cross-language with the agent 57% →
  64%.

Vector search alone wins on this set (96%). It doesn't on the other two: on LoCoMo it
gets 38 against Memcairn's 58, and on the author's set vectors in every recall made
results worse. On 92 facts on distinct topics the nearest vector is usually right; on
thousands of similar ones it often isn't. Cross-language recall is the weak spot: a
small static model links languages less well than a large one.

## Reproduce

```bash
uv pip install -e ".[dev]"
python bench/bench.py --memory-json path/to/memory.json --queries my-queries.json
curl -fsSLO https://raw.githubusercontent.com/snap-research/locomo/main/data/locomo10.json
python bench/locomo.py locomo10.json                      # --units observations, --k 10
python bench/multilingual.py                              # --by lang, --misses
```

`my-queries.json`: `[{"q": "...", "cat": "keyword", "expect": ["Entity name"]}, ...]`.
The harness works on a temporary copy and never modifies the input file.
