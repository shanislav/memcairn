"""Several agents writing at once: nothing lost, nothing doubled, no half-written state.

Each Store here has its own SQLite connection to one file, as separate processes would
(the server plus a CLI command, or two servers on one database by mistake).
"""
import threading

import pytest

from memcairn.db import connect
from memcairn.store import Conflict, Store


@pytest.fixture
def path(tmp_path):
    return tmp_path / "m.db"


def together(n, fn):
    """Run fn(i) in n threads started at the same moment; return results or exceptions."""
    start, out = threading.Barrier(n), [None] * n

    def run(i):
        start.wait()
        try:
            out[i] = fn(i)
        except Exception as e:  # noqa: BLE001 - collected for the assertions
            out[i] = e
    threads = [threading.Thread(target=run, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return out


def test_two_edits_of_one_version_one_wins(path):
    row, _ = Store(connect(path)).remember("hermes has 8 GB RAM")
    stores = [Store(connect(path)) for _ in range(2)]
    out = together(2, lambda i: stores[i].update(row["id"], content=f"hermes has {16 * (i + 1)} GB",
                                                 expected_version=row["version"]))
    assert sum(isinstance(o, Conflict) for o in out) == 1
    final = Store(connect(path)).get([row["id"]])[0]
    assert final["version"] == row["version"] + 1
    assert final["content"] in {"hermes has 16 GB", "hermes has 32 GB"}


def test_two_supersedes_of_one_fact_one_wins(path):
    row, _ = Store(connect(path)).remember("backups go to the NAS")
    stores = [Store(connect(path)) for _ in range(2)]
    out = together(2, lambda i: stores[i].update(row["id"], content=f"backups go to B{i}",
                                                 supersede=True))
    assert sum(isinstance(o, Conflict) for o in out) == 1
    s = Store(connect(path))
    live = s.conn.execute("SELECT count(*) FROM memory WHERE superseded_by IS NULL").fetchone()[0]
    assert live == 1  # one successor, not two branches


def test_a_retried_write_is_stored_once(path):
    stores = [Store(connect(path)) for _ in range(4)]
    out = together(4, lambda i: stores[i].remember("deploy done", client="laptop",
                                                   idempotency_key="k1")[0]["id"])
    assert len(set(out)) == 1 and all(isinstance(o, int) for o in out)
    n = stores[0].conn.execute("SELECT count(*) FROM memory").fetchone()[0]
    assert n == 1


def test_many_writers_lose_nothing(path):
    stores = [Store(connect(path)) for _ in range(4)]

    def write(i):
        for j in range(25):
            stores[i].remember(f"writer {i} fact {j}", entity=f"e{i}", check_duplicates=False)
    errors = [o for o in together(4, write) if isinstance(o, Exception)]
    assert not errors
    s = Store(connect(path))
    q = s.conn.execute
    assert q("SELECT count(*) FROM memory").fetchone()[0] == 100
    assert q("SELECT count(*) FROM memory_fts").fetchone()[0] == 100
    assert q("SELECT count(*) FROM memory_history").fetchone()[0] == 100
    assert len(s.recall("writer fact", entity="e3", limit=200).hits) == 25


def test_a_failure_mid_write_leaves_nothing_behind(path, monkeypatch):
    s = Store(connect(path))
    row, _ = s.remember("atlas runs PostgreSQL 15")

    def boom(*a, **k):
        raise RuntimeError("crash after the row was written, before its history")
    monkeypatch.setattr(s, "_snapshot", boom)
    with pytest.raises(RuntimeError):
        s.remember("half written")
    with pytest.raises(RuntimeError):
        s.update(row["id"], content="atlas runs PostgreSQL 16", supersede=True)
    monkeypatch.undo()
    q = s.conn.execute
    assert q("SELECT count(*) FROM memory").fetchone()[0] == 1
    assert q("SELECT content, version, superseded_by FROM memory").fetchone()[:] == (
        "atlas runs PostgreSQL 15", 1, None)
    assert q("SELECT count(*) FROM memory_fts").fetchone()[0] == 1
    assert s.recall("half written").hits == []


def test_one_store_serves_many_threads_at_once(path):
    """The server: one Store, every MCP tool call in its own thread."""
    s = Store(connect(path))
    ids = [s.remember(f"server s{i} runs postgres and nginx", entity=f"s{i}",
                      check_duplicates=False)[0]["id"] for i in range(40)]

    def work(i):
        for j in range(30):
            if i % 4 == 0:
                s.remember(f"note {i}-{j} about postgres", check_duplicates=False)
            hits = s.recall("postgres nginx server", limit=5).hits
            assert hits
            assert s.get([ids[(i + j) % len(ids)]])[0]["content"].startswith("server")
        return True
    out = together(8, work)
    assert out == [True] * 8, [o for o in out if o is not True][:2]
