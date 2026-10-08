"""Semantic (vector) search over the ``embeddings`` table — PRD §21C, §51.

**Backend outcome (AGENTS.md §2):** ``faiss-cpu`` was installed once with
``pip install faiss-cpu`` on this machine and imports + runs correctly on
**Python 3.14.8** (``faiss`` 1.15.1, ``IndexFlatIP`` verified), so this module
ships both implementations and :func:`create_semantic_index` prefers
:class:`FaissSemanticIndex`; ``faiss-cpu`` is therefore listed in
``requirements.txt``. :class:`NumpySemanticIndex` is the dependency-free
pure-NumPy cosine fallback behind the same :class:`SemanticIndex` protocol —
removing ``faiss-cpu`` from the environment needs no call-site change, it only
swaps which index :func:`create_semantic_index` builds.

Both indexes load rows from the database at search time (the library is small
and always local — PRD §51), L2-normalize the vectors once, and score by
**cosine similarity** (inner product of unit vectors), returned in ``[-1, 1]``.

**Exclusions.** A cosine score is only meaningful between vectors from the
same model and dimension. Rows are therefore **excluded** when their
``embeddings.embedding_model`` differs from the requested model, when their
BLOB is malformed, or (at search time) when their dimension differs from the
query vector's — :func:`NumpySemanticIndex.from_db` /
:class:`FaissSemanticIndex.from_db` filter by model, and each implementation
groups vectors by dimension so a mismatched query simply finds nothing for
that group instead of returning nonsense.

Vector bytes use the little-endian float32 layout documented by
:mod:`backend.ai.queue` — always (de)serialize with
:func:`~backend.ai.queue.serialize_embedding` /
:func:`~backend.ai.queue.deserialize_embedding`.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Sequence
from typing import Protocol, runtime_checkable

import numpy as np

from backend.ai.queue import deserialize_embedding

logger = logging.getLogger(__name__)

try:  # pragma: no cover - exercised implicitly by whichever backend is present
    import faiss
except ImportError:  # pragma: no cover
    faiss = None  # type: ignore[assignment]


@runtime_checkable
class SemanticIndex(Protocol):
    """Vector index contract used by the hybrid ranker (PRD §16-style seam)."""

    def __len__(self) -> int:
        """Number of usable embeddings loaded (``0`` → no semantic component)."""
        ...

    def search(self, vector: Sequence[float], k: int) -> list[tuple[int, float]]:
        """Return the ``k`` closest ``(media_id, cosine_similarity)`` pairs, best first."""
        ...


def faiss_available() -> bool:
    """True when the FAISS backend can be used on this interpreter."""
    return faiss is not None


def create_semantic_index(
    conn: sqlite3.Connection, *, embedding_model: str | None = None
) -> SemanticIndex:
    """Load the embeddings into the best available backend.

    FAISS when ``faiss-cpu`` is importable (this machine: yes — see the module
    docstring), otherwise the NumPy fallback; both honour the same model/dim
    exclusion rules and return identical rankings (both are exact search).
    """
    if faiss is not None:
        return FaissSemanticIndex.from_db(conn, embedding_model=embedding_model)
    return NumpySemanticIndex.from_db(conn, embedding_model=embedding_model)


def _load_vectors(
    conn: sqlite3.Connection, embedding_model: str | None
) -> dict[int, tuple[list[int], np.ndarray]]:
    """Read usable embeddings grouped by dimension: ``dim → (ids, unit matrix)``.

    Rows whose ``embedding_model`` differs from ``embedding_model`` (when given)
    or whose BLOB fails to deserialize are skipped with a warning — they can
    never be scored against a query vector from another model (see the module
    docstring).
    """
    rows = conn.execute(
        "SELECT media_id, embedding, embedding_model FROM embeddings ORDER BY media_id"
    ).fetchall()
    grouped: dict[int, tuple[list[int], np.ndarray]] = {}
    ids: dict[int, list[int]] = {}
    vectors: dict[int, list[np.ndarray]] = {}
    skipped_model = skipped_blob = 0
    for row in rows:
        if embedding_model is not None and str(row["embedding_model"]) != embedding_model:
            skipped_model += 1
            continue
        try:
            vector = deserialize_embedding(row["embedding"])
        except ValueError:
            skipped_blob += 1
            logger.warning("skipping malformed embedding media_id=%s", row["media_id"])
            continue
        dimension = int(vector.size)
        ids.setdefault(dimension, []).append(int(row["media_id"]))
        vectors.setdefault(dimension, []).append(vector)
    for dimension, dimension_ids in ids.items():
        matrix = np.vstack(vectors[dimension]).astype(np.float32, copy=False)
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        matrix = np.divide(matrix, norms, out=np.zeros_like(matrix), where=norms > 0)
        grouped[dimension] = (dimension_ids, matrix)
    if skipped_model or skipped_blob:
        logger.info(
            "semantic index skipped rows model_mismatch=%d malformed=%d",
            skipped_model, skipped_blob,
        )
    return grouped


def _unit_query(vector: Sequence[float]) -> np.ndarray:
    """L2-normalize a query vector; an empty/zero vector yields an empty array."""
    query = np.asarray(vector, dtype=np.float32).reshape(-1)
    if query.size == 0:
        return query
    norm = float(np.linalg.norm(query))
    if norm == 0.0:
        return np.empty(0, dtype=np.float32)
    return query / norm


def _top_k(ids: list[int], scores: np.ndarray, k: int) -> list[tuple[int, float]]:
    """Best ``k`` ``(media_id, score)`` pairs; ties break by ascending media id."""
    if len(ids) == 0 or k <= 0:
        return []
    order = sorted(range(len(ids)), key=lambda index: (-float(scores[index]), ids[index]))
    return [(ids[index], float(scores[index])) for index in order[: min(k, len(ids))]]


class NumpySemanticIndex:
    """Pure-NumPy cosine index (AGENTS.md §2 fallback; no FAISS required)."""

    def __init__(self, ids_by_dim: dict[int, tuple[list[int], np.ndarray]]) -> None:
        self._groups = ids_by_dim

    @classmethod
    def from_db(
        cls, conn: sqlite3.Connection, *, embedding_model: str | None = None
    ) -> NumpySemanticIndex:
        """Load usable embeddings (model/dim/malformed exclusions apply)."""
        return cls(_load_vectors(conn, embedding_model))

    def __len__(self) -> int:
        return sum(len(ids) for ids, _ in self._groups.values())

    def search(self, vector: Sequence[float], k: int) -> list[tuple[int, float]]:
        """Cosine top-``k``; empty when no stored row shares the query's dimension."""
        query = _unit_query(vector)
        group = self._groups.get(int(query.size)) if query.size else None
        if group is None:
            return []
        ids, matrix = group
        return _top_k(ids, matrix @ query, k)


class FaissSemanticIndex:
    """Exact FAISS index (inner product over unit vectors == cosine), one per dimension."""

    def __init__(
        self,
        ids_by_dim: dict[int, tuple[list[int], "faiss.Index"]],
    ) -> None:
        self._groups = ids_by_dim

    @classmethod
    def from_db(
        cls, conn: sqlite3.Connection, *, embedding_model: str | None = None
    ) -> FaissSemanticIndex:
        """Load usable embeddings into per-dimension ``IndexFlatIP`` indexes."""
        if faiss is None:  # pragma: no cover - guarded by create_semantic_index
            raise RuntimeError("faiss is not importable in this environment")
        groups: dict[int, tuple[list[int], "faiss.Index"]] = {}
        for dimension, (ids, matrix) in _load_vectors(conn, embedding_model).items():
            index = faiss.IndexFlatIP(dimension)
            index.add(np.ascontiguousarray(matrix, dtype=np.float32))
            groups[dimension] = (ids, index)
        return cls(groups)

    def __len__(self) -> int:
        return sum(index.ntotal for _, index in self._groups.values())

    def search(self, vector: Sequence[float], k: int) -> list[tuple[int, float]]:
        """Cosine top-``k``; empty when no stored row shares the query's dimension."""
        query = _unit_query(vector)
        if query.size == 0 or k <= 0:
            return []
        group = self._groups.get(int(query.size))
        if group is None:
            return []
        ids, index = group
        count = min(k, len(ids))
        scores, positions = index.search(
            np.ascontiguousarray(query.reshape(1, -1), dtype=np.float32), count
        )
        pairs = [
            (ids[int(position)], float(score))
            for score, position in zip(scores[0], positions[0], strict=True)
            if int(position) >= 0
        ]
        pairs.sort(key=lambda pair: (-pair[1], pair[0]))
        return pairs
