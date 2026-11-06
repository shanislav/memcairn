"""Recall in many languages: a synthetic, public test set (bench/multilingual.json).

    uv run python bench/multilingual.py [--k 5] [--by lang|cat]

~100 short facts about a fictional company, noted in English, German, Spanish, French,
Russian, Chinese, Japanese, Slovak and Czech, and ~100 queries: keywords, questions,
inflected forms, typos, missing diacritics, queries in another language than the fact
(cross-language), a fact that was later superseded (the query must get the current one)
and questions memory has no answer to (negative).

Systems on the same store: plain FTS5 BM25, plain vector search, Memcairn recall
(normal, deep) and "agent": normal recall, then deep when it reports low confidence,
which is what the MCP instructions tell agents to do. A query is found when an expected
fact is among the first k results. A negative query passes when the final result is
weak (what the agent is told as "low confidence"); BM25 and vectors always return
something, so they have no negative score.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from locomo import fts_baseline, vec_baseline  # noqa: E402
from memcairn.db import connect  # noqa: E402
from memcairn.store import Store  # noqa: E402
from memcairn.vectors import from_env  # noqa: E402

WEAK = 0.5  # format.recall: below this coverage the agent is told "low confidence"
DATA = Path(__file__).with_name("multilingual.json")


def load(store: Store, facts: list[dict]) -> dict[int, str]:
    ids: dict[str, int] = {}
    for f in facts:
        if f.get("replaces"):
            row = store.update(ids[f["replaces"]], content=f["content"], supersede=True)
        else:
            row, _ = store.remember(f["content"], entity=f["entity"], check_duplicates=False,
                                    recorded_at=f.get("recorded_at"))
        ids[f["id"]] = row["id"]
    return {mid: fid for fid, mid in ids.items()}


def run(k: int) -> tuple[list[str], list[dict]]:
    data = json.loads(DATA.read_text(encoding="utf-8"))
    embedder = from_env()
    store = Store(connect(":memory:"), embedder=embedder)
    names = load(store, data["facts"])
    systems = ["bm25", "memcairn"] + (["vectors", "memcairn deep", "agent"] if embedder else [])
    rows = []
    for q in data["queries"]:
        expect, out = set(q["expect"]), {}
        for s in systems:
            weak = None
            if s == "bm25":
                ids = fts_baseline(store, q["q"], k)
            elif s == "vectors":
                ids = vec_baseline(store, q["q"], k)
            else:
                res = store.recall(q["q"], limit=k, deep=s == "memcairn deep")
                if s == "agent" and res.coverage < WEAK:
                    res = store.recall(q["q"], limit=k, deep=True)
                ids, weak = [h.memory["id"] for h in res.hits], res.coverage < WEAK
            got = [names.get(i) for i in ids[:k]]
            if expect:
                out[s] = bool(expect & set(got))
            else:
                out[s] = None if weak is None else (weak or not got)
        rows.append({**q, "found": out})
    return systems, rows


def table(systems: list[str], rows: list[dict], by: str) -> str:
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        groups[r[by]].append(r)
    lines = [f"| {by} | n | " + " | ".join(systems) + " |", "|---" * (len(systems) + 2) + "|"]

    def cell(rs, s):
        vals = [r["found"][s] for r in rs if r["found"][s] is not None]
        return f"{100 * sum(vals) / len(vals):.0f}%" if vals else "-"
    for g in sorted(groups):
        lines.append(f"| {g} | {len(groups[g])} | " +
                     " | ".join(cell(groups[g], s) for s in systems) + " |")
    pos = [r for r in rows if r["expect"]]
    lines.append(f"| **all with an answer** | {len(pos)} | " +
                 " | ".join(f"**{cell(pos, s)}**" for s in systems) + " |")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--by", choices=["lang", "cat"], default="cat")
    ap.add_argument("--misses", action="store_true", help="list the queries agent missed")
    a = ap.parse_args()
    systems, rows = run(a.k)
    print(table(systems, rows, a.by))
    if a.misses:
        last = systems[-1]
        for r in rows:
            if r["found"][last] is False:
                print(f"  miss [{r['lang']}/{r['cat']}] {r['q']}  -> expect {r['expect']}")


if __name__ == "__main__":
    main()
