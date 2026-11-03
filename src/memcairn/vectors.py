"""Optional semantic search: local embeddings (fastembed) + brute-force cosine in numpy.

Brute force is deliberate: a personal or team memory holds thousands of facts, not
millions, and a dot product over 10k x 384 floats takes about a millisecond. This
also avoids depending on a SQLite extension being loadable.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

DEFAULT_MODEL = "minishlab/potion-multilingual-128M"


def available() -> bool:
    try:
        import fastembed  # noqa: F401
        import numpy  # noqa: F401
    except ImportError:
        return False
    return True


class Embedder:
    """Lazily loaded embedding model; safe to share between threads."""

    def __init__(self, model: str = DEFAULT_MODEL, cache_dir: str | Path | None = None):
        self.model = model
        self.cache_dir = str(cache_dir) if cache_dir else None
        self._impl = None
        self._lock = threading.Lock()

    def _load(self):
        if self._impl is None:
            from fastembed import TextEmbedding

            self._impl = TextEmbedding(self.model, cache_dir=self.cache_dir)
        return self._impl

    def docs(self, texts: list[str]):
        import numpy as np

        with self._lock:
            vecs = np.array(list(self._load().embed(texts, batch_size=64)), dtype=np.float32)
        return _normalize(vecs)

    def query(self, text: str):
        import numpy as np

        with self._lock:
            vec = np.array(list(self._load().query_embed([text]))[0], dtype=np.float32)
        return _normalize(vec[None, :])[0]


class VectorIndex:
    """In-memory matrix of normalized vectors keyed by memory id."""

    def __init__(self):
        import numpy as np

        self._np = np
        self.ids: list[int] = []
        self.pos: dict[int, int] = {}
        self.matrix = np.zeros((0, 0), dtype=np.float32)
        self.lock = threading.Lock()

    def load(self, rows) -> None:
        np = self._np
        with self.lock:
            self.ids = [r[0] for r in rows]
            self.pos = {mid: i for i, mid in enumerate(self.ids)}
            self.matrix = (np.stack([np.frombuffer(r[1], dtype=np.float32) for r in rows])
                           if rows else np.zeros((0, 0), dtype=np.float32))

    def upsert(self, mid: int, vec) -> None:
        np = self._np
        with self.lock:
            if mid in self.pos:
                self.matrix[self.pos[mid]] = vec
                return
            self.pos[mid] = len(self.ids)
            self.ids.append(mid)
            self.matrix = vec[None, :] if self.matrix.size == 0 else np.vstack([self.matrix, vec])

    def remove(self, mid: int) -> None:
        np = self._np
        with self.lock:
            i = self.pos.pop(mid, None)
            if i is None:
                return
            self.ids.pop(i)
            self.matrix = np.delete(self.matrix, i, axis=0)
            self.pos = {m: j for j, m in enumerate(self.ids)}

    def similarities(self, qvec) -> dict[int, float]:
        with self.lock:
            if self.matrix.size == 0:
                return {}
            sims = self.matrix @ qvec
            return dict(zip(self.ids, sims.tolist()))

    def search(self, qvec, k: int) -> list[tuple[int, float]]:
        np = self._np
        with self.lock:
            if self.matrix.size == 0:
                return []
            sims = self.matrix @ qvec
            k = min(k, len(self.ids))
            top = np.argpartition(-sims, k - 1)[:k]
            top = top[np.argsort(-sims[top])]
            return [(self.ids[i], float(sims[i])) for i in top]

    def __len__(self) -> int:
        return len(self.ids)


def _normalize(vecs):
    import numpy as np

    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return vecs / norms


def from_env() -> Embedder | None:
    """MEMCAIRN_EMBED_MODEL=none disables semantic search; unset uses the default model."""
    model = os.environ.get("MEMCAIRN_EMBED_MODEL", DEFAULT_MODEL)
    if model.lower() in ("", "none", "off") or not available():
        return None
    cache = os.environ.get("MEMCAIRN_MODEL_DIR", str(Path.home() / ".memcairn" / "models"))
    return Embedder(model, cache_dir=cache)
