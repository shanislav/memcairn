"""Benchmark: official @modelcontextprotocol/server-memory vs Memcairn on the same data.

Both servers run as real MCP subprocesses over stdio, on a *copy* of the memory file.

    python bench/bench.py --memory-json ~/.claude/memory.json --queries bench/queries.json

queries.json: [{"q": "...", "expect": ["Entity name", ...], "cat": "keyword"}, ...]
An empty "expect" means nothing relevant exists (negative query).
Writes a JSON report and prints a markdown summary.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path

import anyio
from mcp import Client, StdioServerParameters

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from memcairn.db import connect  # noqa: E402
from memcairn.importers import import_server_memory  # noqa: E402
from memcairn.store import Store  # noqa: E402

HIT = re.compile(r"^m\d+ · ")
OFFICIAL_PKG = "@modelcontextprotocol/server-memory@2025.9.25"


def est(text: str) -> int:
    # Same estimate for both systems (chars / 3.5); see BENCHMARK.md for the caveat.
    return int(len(text) / 3.5 + 0.999)


def text_of(result) -> str:
    return "".join(getattr(c, "text", "") for c in result.content)


def npx() -> tuple[str, list[str]]:
    if os.name == "nt":
        return "cmd.exe", ["/d", "/c", "npx", "-y", OFFICIAL_PKG]
    return "npx", ["-y", OFFICIAL_PKG]


def official_rank(text: str, expect: list[str]) -> int | None:
    """server-memory returns {"entities": [...]} in file order; rank = position of first match."""
    try:
        names = [e["name"] for e in json.loads(text).get("entities", [])]
    except (json.JSONDecodeError, AttributeError):
        return None
    for i, name in enumerate(names, 1):
        if name in expect:
            return i
    return None


def memcairn_rank(text: str, expect: list[str]) -> int | None:
    hits = [line for line in text.splitlines() if HIT.match(line)]
    for i, line in enumerate(hits, 1):
        parts = line.split(" · ")
        if len(parts) > 2 and parts[2] in expect:
            return i
    return None


def memcairn_count(text: str) -> int:
    return sum(1 for line in text.splitlines() if HIT.match(line))


def official_count(text: str) -> int:
    try:
        return len(json.loads(text).get("entities", []))
    except (json.JSONDecodeError, AttributeError):
        return 0


async def tool_list_cost(client) -> int:
    tools = await client.list_tools()
    items = getattr(tools, "tools", tools)
    return est(json.dumps([t.model_dump(exclude_none=True, by_alias=True) for t in items]))


async def run_official(memory_copy: Path, queries: list[dict]) -> dict:
    cmd, args = npx()
    params = StdioServerParameters(command=cmd, args=args,
                                   env={"MEMORY_FILE_PATH": str(memory_copy)})
    out = []
    async with Client(params, read_timeout_seconds=120) as client:
        cost = await tool_list_cost(client)
        for q in queries:
            t0 = time.perf_counter()
            res = await client.call_tool("search_nodes", {"query": q["q"]})
            ms = (time.perf_counter() - t0) * 1000
            text = text_of(res)
            out.append({"tokens": est(text), "ms": ms, "rank": official_rank(text, q["expect"]),
                        "results": official_count(text)})
    return {"tool_list_tokens": cost, "queries": out, "session_start_tokens": None}


async def run_memcairn(db: Path, queries: list[dict]) -> dict:
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "memcairn.cli", "serve", "--db", str(db)],
        env={"PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
             "PYTHONIOENCODING": "utf-8"})
    out = []
    async with Client(params, read_timeout_seconds=120) as client:
        cost = await tool_list_cost(client)
        ctx = text_of(await client.call_tool("context", {}))
        for q in queries:
            t0 = time.perf_counter()
            text = text_of(await client.call_tool("recall", {"query": q["q"]}))
            rank = memcairn_rank(text, q["expect"])
            total, deep_used = est(text), False
            # The realistic flow: an agent follows the low-confidence hint once.
            if "try mode=deep" in text:
                deep = text_of(await client.call_tool("recall", {"query": q["q"], "mode": "deep"}))
                total += est(deep)
                deep_used = True
                if rank is None:
                    rank = memcairn_rank(deep, q["expect"])
                text = deep
            ms = (time.perf_counter() - t0) * 1000
            out.append({"tokens": total, "ms": ms, "rank": rank, "deep": deep_used,
                        "results": memcairn_count(text)})
    return {"tool_list_tokens": cost, "queries": out, "session_start_tokens": est(ctx)}


def summarize(name: str, run: dict, queries: list[dict]) -> dict:
    pos = [(q, r) for q, r in zip(queries, run["queries"]) if q["expect"]]
    neg = [(q, r) for q, r in zip(queries, run["queries"]) if not q["expect"]]
    toks = [r["tokens"] for r in run["queries"]]
    hit = lambda k: sum(1 for _, r in pos if r["rank"] and r["rank"] <= k) / len(pos)  # noqa: E731
    by_cat: dict[str, list] = {}
    for q, r in pos:
        by_cat.setdefault(q.get("cat", "other"), []).append(bool(r["rank"] and r["rank"] <= 5))
    return {
        "system": name,
        "tool_list_tokens": run["tool_list_tokens"],
        "session_start_tokens": run["session_start_tokens"],
        "hit@1": round(hit(1), 3),
        "hit@5": round(hit(5), 3),
        "mrr": round(sum(1 / r["rank"] for _, r in pos if r["rank"]) / len(pos), 3),
        "zero_results": sum(1 for _, r in pos if r["results"] == 0),
        "tokens_mean": round(statistics.mean(toks)),
        "tokens_median": round(statistics.median(toks)),
        "tokens_max": max(toks),
        "tokens_total": sum(toks),
        "negative_tokens_mean": round(statistics.mean([r["tokens"] for _, r in neg])) if neg else 0,
        "latency_ms_median": round(statistics.median(r["ms"] for r in run["queries"]), 1),
        "hit@5_by_category": {c: f"{sum(v)}/{len(v)}" for c, v in sorted(by_cat.items())},
    }


def markdown(summaries: list[dict], n_queries: int, data: dict) -> str:
    rows = [
        ("Tool list (paid every turn)", "tool_list_tokens", "{} tok"),
        ("Session start", "session_start_tokens", "{} tok"),
        ("hit@1", "hit@1", "{:.0%}"),
        ("hit@5", "hit@5", "{:.0%}"),
        ("MRR", "mrr", "{:.2f}"),
        ("Relevant queries with zero results", "zero_results", "{}"),
        ("Tokens per query — mean", "tokens_mean", "{:,}"),
        ("Tokens per query — median", "tokens_median", "{:,}"),
        ("Tokens per query — max", "tokens_max", "{:,}"),
        ("Tokens, all queries", "tokens_total", "{:,}"),
        ("Tokens on negative queries — mean", "negative_tokens_mean", "{:,}"),
        ("Latency median", "latency_ms_median", "{} ms"),
    ]
    head = "| | " + " | ".join(s["system"] for s in summaries) + " |"
    title = f"Dataset: {data['entities']} entities, {data['facts']} facts; {n_queries} queries."
    lines = [title, "", head, "|---" * (len(summaries) + 1) + "|"]
    for label, key, fmt in rows:
        vals = ["–" if s[key] is None else fmt.format(s[key]) for s in summaries]
        lines.append(f"| {label} | " + " | ".join(vals) + " |")
    cats = sorted({c for s in summaries for c in s["hit@5_by_category"]})
    lines += ["", "hit@5 by query category:", "", head, "|---" * (len(summaries) + 1) + "|"]
    for c in cats:
        lines.append(f"| {c} | " + " | ".join(s["hit@5_by_category"].get(c, "–")
                                             for s in summaries) + " |")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--memory-json", type=Path, required=True)
    ap.add_argument("--queries", type=Path, required=True)
    ap.add_argument("--out", type=Path, default=Path("bench-results.json"))
    a = ap.parse_args()
    queries = json.loads(a.queries.read_text(encoding="utf-8"))

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        tmp = Path(tmp)
        copy = tmp / "memory.json"
        shutil.copyfile(a.memory_json, copy)  # never touch the original
        db = tmp / "memcairn.db"
        conn = connect(db)
        data = import_server_memory(Store(conn), copy)
        conn.close()
        official = anyio.run(run_official, copy, queries)
        engr = anyio.run(run_memcairn, db, queries)

    summaries = [summarize("server-memory (official)", official, queries),
                 summarize("Memcairn", engr, queries)]
    report = {"data": data, "summaries": summaries,
              "per_query": [{"q": q["q"], "cat": q.get("cat"), "expect": q["expect"],
                             "official": o, "memcairn": e}
                            for q, o, e in zip(queries, official["queries"], engr["queries"])]}
    a.out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(markdown(summaries, len(queries), data))


if __name__ == "__main__":
    main()
