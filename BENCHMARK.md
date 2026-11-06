# Benchmarks

Three tests of how well `recall` finds things, each on different data:

| | data | Memcairn | compared with |
|---|---|---|---|
| [Real memory](#1-real-memory-vs-the-official-memory-server) | 997 facts of one person's memory, 50 queries | **100%** hit@5, ~190 tokens per query | official `server-memory`: 24%, ~2,055 tokens |
| [LoCoMo](#2-public-data-locomo-english) | 10 long public conversations, 1,540 questions | **58%** hit@5 | plain BM25: 51%, plain vectors: 38% |
| [Nine languages](#3-many-languages-synthetic-set) | 92 synthetic facts, 101 queries | **95%** hit@5 | plain BM25: 88% |

hit@5: the right fact is among the five lines recall returns. All numbers are from the
current code; the harnesses are in [`bench/`](bench/).

## 1. Real memory vs the official memory server

The test compares Memcairn with
[`@modelcontextprotocol/server-memory`](https://github.com/modelcontextprotocol/servers/tree/main/src/memory)
(version 2025.9.25), the official reference memory server and what many Claude Code
setups use, on the same data. The harness is [`bench/bench.py`](bench/bench.py); you can
run it on your own `memory.json`.

- **Data:** one person's real memory after five months of daily Claude Code use: 73
  entities, 997 facts and 20 relations, mostly Slovak with some English and Czech. The
  data is private, so only aggregate numbers are published.
- **Both servers** run as real MCP subprocesses over stdio, each on its own copy of the
  file. Memcairn gets the same file through its importer, with no manual cleanup.
- **Queries:** 50, each with the expected entities, written to look like what an agent
  asks while it works:

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
- **Memcairn:** one `recall` call; when the reply says `low confidence — try mode=deep`, a
  second call with `mode=deep`, as an agent would do. Tokens of both calls are counted.
- **Token count:** characters / 3.5 for both systems. Absolute numbers are estimates;
  the ratios are what matter.

| | server-memory (official) | Memcairn |
|---|---|---|
| Tool list (paid on every turn) | 1,247 tok | 978 tok |
| hit@1 | 15% | **89%** |
| hit@5 | 24% | **100%** |
| MRR | 0.19 | **0.94** |
| Relevant queries that returned nothing | 34 of 46 | **0 of 46** |
| Tokens per query: mean | 2,055 | **187** |
| Tokens per query: max | 35,765 | **453** |
| Tokens across all 50 queries | 102,736 | **9,352** |
| Tokens on negative queries: mean | **12** | 115 |
| Median latency | **2.1 ms** | 7.0 ms |

hit@5 by category:

| | official | Memcairn |
|---|---|---|
| keyword | 8/10 | 10/10 |
| question | 0/10 | 10/10 |
| multi-keyword | 2/14 | 14/14 |
| no-diacritics | 0/4 | 4/4 |
| inflection | 1/4 | 4/4 |
| cross-language | 0/4 | 4/4 |

**The official server mostly finds nothing or floods the context.** It matches the whole
query as one substring, so any query of two or more words that doesn't appear verbatim
returns nothing (34 of the 46 queries with an answer). When it does find something, it
returns every matching entity with all of its facts, unranked: 8,500 tokens on average
for those queries, 35,765 for one three-letter keyword. Its "12 tokens" is an empty
result, which is also why it is cheaper on the negative queries: Memcairn returns its
closest weak matches marked `low confidence`.

**Memcairn answers with five ranked lines**, about 190 tokens, and the model fetches full
text only for the hits it needs.

Caveats:
- **The queries were written by Memcairn's author, who knows the data.** The set is
  small and not adversarial; the categories show where each system breaks. The other two
  tests exist for this reason.
- **The set improved Memcairn along the way:** stop words and trimming weak results
  (first run: 96% hit@5, 324 tokens on negative queries), then the fixes that LoCoMo and
  the language set found (98% → 100%). One expected answer in the query set was also
  corrected.
- **Vectors in every recall made results worse here** (96% → 94% when tried): semantic
  search always finds *something*. That is why they are used only in `mode=deep`, which
  an agent calls when plain recall reports low confidence.
- **Which embedding model.** Five multilingual models were compared on this data,
  vector search only:

  | Model | hit@5 | Embedding 998 facts |
  |---|---|---|
  | `potion-multilingual-128M` (default) | 93% | 0.5 s |
  | `embeddinggemma-300m` | 91% | 686 s |
  | `paraphrase-multilingual-mpnet-base-v2` | 87% | 395 s |
  | `paraphrase-multilingual-MiniLM-L12-v2` | 76% | 46 s |

  Times are on the author's home server, a 2017 desktop CPU (Intel Core i3-7100, 2 cores
  / 4 threads; the test container had 3 of them and 6 GB of memory, in which
  `Qwen3-Embedding-0.6B` didn't fit). Any recent machine is faster; the order stays. The
  static potion model was both the best and by far the cheapest.
- **Not a comparison with Mem0, Graphiti, Hindsight and similar systems**, which run an
  LLM on every write; see [COMPARISON.md](COMPARISON.md).

## 2. Public data: LoCoMo (English)

Test 1 uses the author's own data and queries. As an independent check, retrieval was
also measured on [LoCoMo](https://github.com/snap-research/locomo) (Maharana et al.,
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
  any time. Together they took Memcairn from 53 to 58 hit@5 (test 1: 98% → 100%).
- **Deep mode is not better on every question.** It helps multi-hop questions, where
  words don't match, and costs single-hop ones. This matches test 1: vectors only when
  plain recall is unsure.

## 3. Many languages (synthetic set)

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
gets 38 against Memcairn's 58, and in test 1 vectors in every recall made results
worse. On 92 facts on distinct topics the nearest vector is usually right; on
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
