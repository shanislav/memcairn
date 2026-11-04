import numpy as np

from memcairn import review as rv
from memcairn.db import connect
from memcairn.store import Store


class OneAxis:
    """Every text embeds to the same vector: all same-entity pairs look identical."""
    model = "one-axis"

    def docs(self, texts):
        return np.ones((len(texts), 4), dtype=np.float32) / 2

    def query(self, text):
        return np.ones(4, dtype=np.float32) / 2


def test_finds_each_kind_and_ranks_by_saving():
    store = Store(connect(":memory:"))
    store.remember("same text", entity="A")
    store.remember("same text", entity="A")
    store.remember("x" * 900, entity="B")
    store.remember("deployed c34baa5 to prod; the doctor command, successor promotion and "
                   "semantic duplicates are live now, 41 tests pass, nothing else changed today, "
                   "the benchmark is unchanged",
                   entity="C")
    items = rv.review(store)
    assert {i.kind for i in items} == {"dup", "long", "status"}
    assert items[0].kind == "long"  # biggest saving first


def test_similar_pairs_use_stored_vectors():
    store = Store(connect(":memory:"), embedder=OneAxis())
    store.remember("Plex runs natively", entity="Plex")
    store.remember("Plex is a systemd service", entity="Plex")
    store.remember("unrelated", entity="Other")
    items = rv.similar_pairs(store)
    assert len(items) == 1 and items[0].entity == "Plex"


def test_fix_exact_duplicates_is_reversible():
    store = Store(connect(":memory:"))
    a, _ = store.remember("same text", entity="A")
    b, _ = store.remember("same text", entity="A")
    assert rv.fix_exact_duplicates(store) == 1
    assert store.get([b["id"]])[0]["deleted_at"]
    assert not store.get([a["id"]])[0]["deleted_at"]
    store.forget(b["id"], undo=True)
    assert not store.get([b["id"]])[0]["deleted_at"]


def test_template_rows_for_different_hosts_are_not_pairs():
    store = Store(connect(":memory:"), embedder=OneAxis())
    store.remember("Chimera1C7: 192.168.20.211 | MySQL replikátor", entity="Chimera")
    store.remember("Chimera2C7: 192.168.20.212 | MySQL replikátor", entity="Chimera")
    assert rv.similar_pairs(store) == []


def test_daily_check_keeps_one_todo_without_churn_and_closes_it_when_clean():
    store = Store(connect(":memory:"))
    ids = [store.remember("y" * 700 + str(i), entity=f"E{i}")[0]["id"] for i in range(10)]
    assert rv.suggest_cleanup(store, minimum=10, origin="Daily memory check")[0] == 10
    todo = next(h.memory for h in store.context() if h.memory["type"] == "todo")
    rv.suggest_cleanup(store, minimum=10, origin="Daily memory check")
    assert store.get([todo["id"]])[0]["version"] == todo["version"]  # same count: no new version
    for mid in ids:
        store.update(mid, content="short conclusion")
    rv.suggest_cleanup(store, minimum=10, origin="Daily memory check")
    assert store.get([todo["id"]])[0]["status"] == "done"
