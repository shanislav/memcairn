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
