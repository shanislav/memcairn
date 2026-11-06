"""Retrieval on LoCoMo (Maharana et al., 2024): does recall find the evidence?

LoCoMo is a public long-conversation benchmark: 10 conversations (~600 turns each, over
months) and ~2,000 questions, each labelled with the turns that hold the answer.
Data (2.8 MB, not in this repository):

    curl -fsSLO https://raw.githubusercontent.com/snap-research/locomo/main/data/locomo10.json
    uv run python bench/locomo.py locomo10.json [--units turns|observations] [--k 5]

Each conversation gets its own fresh memory. `turns` stores every utterance as a fact
("Speaker: text", entity = speaker, recorded at the session's date); `observations`
stores the dataset's per-session extracted facts instead, which is closer to what an
agent writes with `remember`. A question counts as found when one of its evidence turns
is among the first k results (hit@k); recall@k is the share of all its evidence turns
found. Category 5 (adversarial: no answer exists) is left out, as in other reports.

Compared on the same data: plain FTS5 BM25 over the same text, plain vector search with
the same model, Memcairn recall (normal, deep, and "agent": normal, then deep when it
reports low confidence, as the MCP instructions tell agents). Only retrieval is measured:
no LLM reads the results, so these numbers are not answer accuracy.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from memcairn.db import connect  # noqa: E402
from memcairn.store import Store, query_terms  # noqa: E402
from memcairn.vectors import from_env  # noqa: E402

WEAK = 0.5  # below this, recall tells the agent "low confidence — try mode=deep"
CATEGORIES = {1: "multi-hop", 2: "temporal", 3: "open-domain", 4: "single-hop"}


def session_date(text: str) -> str:
    """'1:56 pm on 8 May, 2023' -> '2023-05-08T13:56:00Z'."""
    dt = datetime.strptime(text.strip(), "%I:%M %p on %d %B, %Y")
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def units(sample: dict, kind: str) -> list[tuple[str, str, str, list[str]]]:
    """(content, entity, recorded_at, evidence ids) for one conversation."""
    conv, out = sample["conversation"], []
    for key, turns in conv.items():
        if not (key.startswith("session_") and isinstance(turns, list)):
            continue
        when = session_date(conv[f"{key}_date_time"])
        if kind == "turns":
            for t in turns:
                text = t["text"] + (f" [shares a photo: {t['blip_caption']}]"
                                    if t.get("blip_caption") else "")
                out.append((f"{t['speaker']}: {text}", t["speaker"], when, [t["dia_id"]]))
        else:
            obs = sample["observation"].get(f"{key}_observation", {})
            for speaker, items in obs.items():
                for text, ev in items:
                    ids = ev if isinstance(ev, list) else [ev]
                    out.append((text, speaker, when, [i for i in ids if isinstance(i, str)]))
    return out


def fts_baseline(store: Store, q: str, k: int) -> list[int]:
    words = query_terms(q)
    if not words:
        return []
    rows = store.conn.execute(
        "SELECT rowid FROM memory_fts WHERE memory_fts MATCH ? ORDER BY bm25(memory_fts) "
        "LIMIT ?", (" OR ".join(f'"{w}"' for w in words), k)).fetchall()
    return [r[0] for r in rows]


def vec_baseline(store: Store, q: str, k: int) -> list[int]:
    return [mid for mid, _ in store.vindex.search(store.embedder.query(q), k)]


def run(path: Path, kind: str, k: int) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    embedder = from_env()
    systems = ["bm25", "memcairn"] + (["vectors", "memcairn deep", "agent"] if embedder else [])
    res: dict[str, dict[int, list[tuple[float, float]]]] = {s: defaultdict(list) for s in systems}
    times: dict[str, list[float]] = defaultdict(list)
    stored = 0
    for sample in data:
        store = Store(connect(":memory:"), embedder=embedder)
        evidence: dict[int, list[str]] = {}
        for content, entity, when, ev in units(sample, kind):
            row, _ = store.remember(content, entity=entity, recorded_at=when, valid_from=when,
                                    check_duplicates=False)
            evidence[row["id"]] = ev
        stored += len(evidence)
        for qa in sample["qa"]:
            cat = qa.get("category")
            gold = set(qa.get("evidence") or [])
            if cat not in CATEGORIES or not gold:
                continue
            for system in systems:
                t = time.perf_counter()
                if system == "bm25":
                    ids = fts_baseline(store, qa["question"], k)
                elif system == "vectors":
                    ids = vec_baseline(store, qa["question"], k)
                else:
                    got = store.recall(qa["question"], limit=k, deep=system.endswith("deep"))
                    if system == "agent" and got.coverage < WEAK:  # as the MCP instructions say
                        got = store.recall(qa["question"], limit=k, deep=True)
                    hits = got.hits
                    ids = [h.memory["id"] for h in hits]
                times[system].append(time.perf_counter() - t)
                found = {e for i in ids[:k] for e in evidence.get(i, [])} & gold
                res[system][cat].append((1.0 if found else 0.0, len(found) / len(gold)))
    return {"units": kind, "k": k, "stored": stored, "systems": systems, "results": res,
            "ms": {s: 1000 * statistics.mean(v) for s, v in times.items()}}


def report(r: dict) -> str:
    k, cats = r["k"], list(CATEGORIES)
    lines = [f"LoCoMo, {r['units']} ({r['stored']} stored), hit@{k} / recall@{k}, "
             f"categories 1-4", "",
             "| system | " + " | ".join(CATEGORIES[c] for c in cats) + " | all | ms/query |",
             "|---" * (len(cats) + 3) + "|"]
    for s in r["systems"]:
        per, every = r["results"][s], [x for c in cats for x in r["results"][s][c]]

        def cell(xs):
            return (f"{100 * statistics.mean(x[0] for x in xs):.0f} / "
                    f"{100 * statistics.mean(x[1] for x in xs):.0f}") if xs else "-"
        lines.append(f"| {s} | " + " | ".join(cell(per[c]) for c in cats) +
                     f" | **{cell(every)}** | {r['ms'][s]:.1f} |")
    n = {c: len(r["results"][r["systems"][0]][c]) for c in cats}
    lines += ["", "questions: " + ", ".join(f"{CATEGORIES[c]} {n[c]}" for c in cats)]
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("data", type=Path)
    ap.add_argument("--units", choices=["turns", "observations"], default="turns")
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--json", type=Path, help="also write the raw numbers here")
    a = ap.parse_args()
    r = run(a.data, a.units, a.k)
    print(report(r))
    if a.json:
        a.json.write_text(json.dumps(r, indent=1, default=str), encoding="utf-8")


if __name__ == "__main__":
    main()
