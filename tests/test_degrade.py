"""A broken or missing embedding model must never break writes or recall."""

import numpy as np

from memcairn.db import connect
from memcairn.store import Store


class BrokenEmbedder:
    model = "broken"

    def docs(self, texts):
        raise ValueError("Could not load model broken from any source.")

    def query(self, text):
        raise ValueError("Could not load model broken from any source.")


def test_write_succeeds_when_embedding_fails():
    store = Store(connect(":memory:"), embedder=BrokenEmbedder())
    row, _ = store.remember("Plex beží natívne cez systemd", entity="Plex")
    assert row["id"] == 1
    assert len(store.vindex) == 0


def test_deep_recall_falls_back_to_full_text():
    store = Store(connect(":memory:"), embedder=BrokenEmbedder())
    store.remember("Plex beží natívne cez systemd", entity="Plex")
    # put something in the index so the vector path is attempted
    store.vindex.upsert(1, np.ones(4, dtype=np.float32) / 2)
    hits = store.recall("plex systemd", deep=True).hits
    assert hits[0].entity == "Plex"
