"""Hybrid recall with a tiny deterministic embedder (no model download)."""

import numpy as np
import pytest

from memcairn import format
from memcairn.db import connect
from memcairn.importers import import_server_memory
from memcairn.store import Store, fold

# Words that mean the same thing land on the same axis, across languages.
CONCEPTS = {
    "lunch": 0, "obed": 0, "obedov": 0, "jedlo": 0,
    "deploy": 1, "nasadit": 1, "nasadenie": 1, "rollout": 1,
    "battery": 2, "bateria": 2, "baterie": 2,
    "firewall": 3, "foomuuri": 3,
}


class FakeEmbedder:
    model = "fake-concepts"

    def _vec(self, text):
        v = np.zeros(8, dtype=np.float32)
        for word in fold(text).split():
            word = word.strip(".,:;!?")
            if word in CONCEPTS:
                v[CONCEPTS[word]] += 1
        v[7] = 0.05  # keep every vector non-zero
        return v / np.linalg.norm(v)

    def docs(self, texts):
        return np.stack([self._vec(t) for t in texts])

    def query(self, text):
        return self._vec(text)


@pytest.fixture
def store():
    return Store(connect(":memory:"), embedder=FakeEmbedder())


def test_semantic_hit_without_shared_words(store):
    store.remember("App na objednávanie obedov z reštaurácie", entity="OBED")
    store.remember("Plex beží natívne cez systemd", entity="Plex")
    assert not store.recall("lunch ordering").hits  # normal mode: full text only
    res = store.recall("lunch ordering", deep=True)
    assert res.hits[0].entity == "OBED"
    assert res.hits[0].parts["sim"] > 0.9


def test_vectors_follow_updates_and_forget(store):
    row, _ = store.remember("rollout cez docker compose", entity="X")
    assert len(store.vindex) == 1
    new = store.update(row["id"], content="firewall je foomuuri", supersede=True)
    assert len(store.vindex) == 2
    assert store.recall("firewall").hits[0].memory["id"] == new["id"]
    store.forget(new["id"])
    assert new["id"] not in store.vindex.pos
    store.forget(new["id"], undo=True)
    assert new["id"] in store.vindex.pos


def test_index_survives_restart(tmp_path):
    db = tmp_path / "m.db"
    s1 = Store(connect(db), embedder=FakeEmbedder())
    s1.remember("výmena batérie v UPS", entity="UPS")
    s1.conn.close()
    s2 = Store(connect(db), embedder=FakeEmbedder())
    assert len(s2.vindex) == 1
    assert s2.recall("battery", deep=True).hits[0].entity == "UPS"


def test_import_embeds_in_one_batch(store, tmp_path):
    p = tmp_path / "memory.json"
    p.write_text('{"type":"entity","name":"OBED","entityType":"project",'
                 '"observations":["obed app","deploy postup"]}', encoding="utf-8")
    stats = import_server_memory(store, p)
    assert stats["embedded"] == 2
    assert len(store.vindex) == 2


def test_unrelated_query_is_low_confidence(store):
    store.remember("App na objednávanie obedov", entity="OBED")
    res = store.recall("kubernetes autoscaling", deep=True)
    text = format.recall(res, max_tokens=1500, limit=12, explain=False, deep=True)
    assert "likely not in memory" in text
