"""Optional semantic search: local embeddings + brute-force cosine in numpy.

Brute force is deliberate: a personal memory holds thousands of facts, not millions, and
a dot product over 10k x 256 floats takes about a millisecond. This also avoids depending
on a SQLite extension being loadable.

Static models (model2vec, like the default potion-multilingual-128M) are a table of one
vector per token: a text's vector is the mean of its tokens' rows. Memcairn reads that
table memory-mapped from the downloaded file instead of loading it through fastembed, so
only the rows actually used take memory (about 0.6 GB in all instead of 1.2 GB, most of
it the tokenizer) and the vectors are the same. Other models go through fastembed.
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
            self._impl = StaticModel.open(self.model, self.cache_dir)
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


class StaticModel:
    """A model2vec model read straight from its safetensors file (memory-mapped)."""

    MAX_TOKENS = 512  # as model2vec's encode

    def __init__(self, table, tokenizer, unk: int | None):
        self.table, self.tokenizer, self.unk = table, tokenizer, unk

    @classmethod
    def open(cls, model: str, cache_dir: str | None) -> StaticModel | None:
        """The model, or None when it isn't a model2vec model (fastembed handles those)."""
        import json
        import struct

        import numpy as np

        try:
            from huggingface_hub import snapshot_download
            from tokenizers import Tokenizer

            files = ["config.json", "model.safetensors", "tokenizer.json"]
            try:  # offline first: the model is usually there already
                folder = snapshot_download(model, cache_dir=cache_dir, allow_patterns=files,
                                           local_files_only=True)
            except Exception:
                folder = snapshot_download(model, cache_dir=cache_dir, allow_patterns=files)
            folder = Path(folder)
            config = json.loads((folder / "config.json").read_text(encoding="utf-8"))
            if config.get("model_type") != "model2vec" or not config.get("normalize", True):
                return None
            path = folder / "model.safetensors"
            with open(path, "rb") as f:
                size = struct.unpack("<Q", f.read(8))[0]
                header = json.loads(f.read(size))
            meta = header["embeddings"]
            dtype = {"F32": np.float32, "F16": np.float16}[meta["dtype"]]
            table = np.memmap(path, dtype=dtype, mode="r", shape=tuple(meta["shape"]),
                              offset=8 + size + meta["data_offsets"][0])
            tokenizer = Tokenizer.from_file(str(folder / "tokenizer.json"))
        except Exception:  # unusual layout, no network, missing package: use fastembed
            return None
        return cls(table, tokenizer, tokenizer.token_to_id("[UNK]"))

    def embed(self, texts: list[str], batch_size: int = 64):
        import numpy as np

        dim = self.table.shape[1]
        for enc in self.tokenizer.encode_batch(list(texts), add_special_tokens=False):
            ids = [i for i in enc.ids if i != self.unk][: self.MAX_TOKENS]
            yield (self.table[ids].astype(np.float32).mean(axis=0) if ids
                   else np.zeros(dim, dtype=np.float32))

    def query_embed(self, texts: list[str]):
        return self.embed(texts)


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
