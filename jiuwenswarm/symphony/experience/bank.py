from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import faiss
import numpy as np

from .embed import EmbeddingClient
from .models import ExperienceItem

LOGGER = logging.getLogger(__name__)

_META_FILE = "meta.json"
_SCALAR_DIR = "scalar"
_SCALAR_FILE = "metadata.jsonl"
_VECTOR_DIR = "vector"
_FAISS_FILE = "faiss_index.bin"
_EMBED_FILE = "embeddings.npy"
_HASH_CHUNK_SIZE = 8192


@dataclass
class _KnowledgeMeta:
    version: int = 1
    vector_count: int = 0
    vector_algorithm: str = "Flat"
    vector_dimension: int = 0
    vector_sha256: str = ""
    scalar_sha256: str = ""
    embeddings_sha256: str = ""


def _win_retry(func, retries: int = 3, delay: float = 0.1):
    """Retry a callable on Windows file-lock errors."""
    for i in range(retries):
        try:
            return func()
        except OSError:
            if i == retries - 1:
                raise
            time.sleep(delay)
    return None  # Unreachable, satisfies static analysis


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with _win_retry(lambda: open(path, "rb")) as f:
        for chunk in iter(lambda: f.read(_HASH_CHUNK_SIZE), b""):
            h.update(chunk)
    return h.hexdigest()




def _build_faiss_index(algorithm: str, dim: int) -> tuple[faiss.Index, str]:
    actual_algorithm = algorithm
    try:
        index = faiss.index_factory(dim, algorithm, faiss.METRIC_INNER_PRODUCT)
    except (RuntimeError, ValueError) as exc:
        LOGGER.warning(
            "ExperienceBank: index_factory failed for '%s' (dim=%d): %s",
            algorithm, dim, exc,
            exc_info=True,
        )
        raise

    # IVF-style indices need training before adding vectors
    if isinstance(index, faiss.IndexIVF):
        index.nprobe = 10  # reasonable default for search recall

    return index, actual_algorithm


def _write_atomic(source: Path, target: Path) -> None:
    """Atomically replace *target* with *source* (Windows-safe)."""
    if not source.exists():
        return
    try:
        _win_retry(lambda: os.replace(str(source), str(target)), retries=5, delay=0.2)
    except FileNotFoundError:
        LOGGER.warning("_write_atomic: source, target file vanished/locked during replace")
        raise
    except Exception:
        LOGGER.warning("_write_atomic failed", exc_info=True)
        raise


class ExperienceBank:
    """Persistent knowledge base for experience items.

    Storage layout (directory-based)::

        experience_kb/
        ├── meta.json            # integrity manifest
        ├── scalar/
        │   └── metadata.jsonl   # item metadata (no embeddings)
        └── vector/
            ├── faiss_index.bin  # FAISS index
            └── embeddings.npy   # embedding matrix

    All items are loaded into memory; FAISS provides fast search.
    """

    def __init__(
        self,
        index_dir: str | Path,
        embedding_client: EmbeddingClient,
        *,
        vector_algorithm: str = "Flat",
    ) -> None:
        self._dir = Path(index_dir)
        self._embedder = embedding_client
        self._vector_algorithm = vector_algorithm
        self._items: list[ExperienceItem] = []
        self._id_index: dict[str, ExperienceItem] = {}
        self._skills: set[frozenset] = set()
        self._embedding_matrix: np.ndarray | None = None
        self._faiss_index: faiss.Index | None = None
        self._write_lock = threading.Lock()
        self._next_id_counter = 0
        self._load()

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    @property
    def items(self) -> list[ExperienceItem]:
        return list(self._items)

    @property
    def count(self) -> int:
        return len(self._items)

    def exist(self, skills: list[str]) -> bool:
        return frozenset(skills) in self._skills

    def add(self, item: ExperienceItem) -> None:
        """Add a new experience item to the KB and persist."""
        with self._write_lock:
            self._items.append(item)
            self._id_index[item.id] = item
            self._skills.add(frozenset(item.skill_ids))
            self._rebuild_index()
            self._persist()

    def add_batch(self, items: list[ExperienceItem]) -> None:
        """Add multiple items at once, rebuilding index and persisting only once."""
        if not items:
            return
        with self._write_lock:
            for item in items:
                self._items.append(item)
                self._id_index[item.id] = item
                self._skills.add(frozenset(item.skill_ids))
            self._rebuild_index()
            self._persist()

    def remove(self, item_id: str) -> bool:
        """Remove an item by id. Returns True if found and removed."""
        with self._write_lock:
            item = self._id_index.pop(item_id, None)
            if item is None:
                return False
            self._items = [i for i in self._items if i.id != item_id]
            self._skills.discard(frozenset(item.skill_ids))
            self._rebuild_index()
            self._persist()
            return True

    # ------------------------------------------------------------------
    # FAISS search
    # ------------------------------------------------------------------

    def search_by_embedding(
        self,
        query: str,
        top_k: int = 1,
        threshold: float = 0.80,
    ) -> list[tuple[float, ExperienceItem]]:
        """Search the KB by embedding similarity using FAISS.

        Returns a list of (similarity_score, item) sorted descending.
        Items below threshold are excluded.
        """
        if self._faiss_index is None or not self._items:
            return []

        query_emb = self._embedder.embed(query)
        query_vec = np.asarray([query_emb], dtype=np.float32)

        k = min(top_k, len(self._items))
        distances, indices = self._faiss_index.search(query_vec, k)

        results = []
        for i in range(k):
            if i >= len(indices[0]):
                break
            idx = int(indices[0][i])
            score = float(distances[0][i])
            if score >= threshold and idx < len(self._items):
                results.append((score, self._items[idx]))

        return results

    def search_with_skill_ids(
        self,
        query: str,
        top_k: int = 1,
        threshold: float = 0.80,
    ) -> list[str]:
        """Convenience: return just the skill_ids of the best match."""
        results = self.search_by_embedding(query, top_k=top_k, threshold=threshold)
        skills: list[str] = []
        for _sim, item in results:
            for sid in item.skill_ids:
                if sid not in skills:
                    skills.append(sid)
        return skills

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self) -> None:
        meta_path = self._dir / _META_FILE
        scalar_path = self._dir / _SCALAR_DIR / _SCALAR_FILE
        vector_dir = self._dir / _VECTOR_DIR
        faiss_path = vector_dir / _FAISS_FILE
        emb_path = vector_dir / _EMBED_FILE

        meta_exists = meta_path.exists()
        scalar_exists = scalar_path.exists()
        faiss_exist = faiss_path.exists()
        emb_exists = emb_path.exists()

        if not any([meta_exists, scalar_exists, faiss_exist, emb_exists]):
            LOGGER.info("ExperienceBank: index directory is empty, starting empty")
            return

        if not meta_exists:
            raise FileNotFoundError(
                f"ExperienceBank: meta.json missing but other index files present"
            )
        if not scalar_exists:
            raise FileNotFoundError(
                f"ExperienceBank: scalar/metadata.jsonl missing but other index files present "
            )
        if not faiss_path.exists():
            raise FileNotFoundError(
                f"ExperienceBank: vector/{_FAISS_FILE} missing but other index files present"
            )
        if not emb_path.exists():
            raise FileNotFoundError(
                f"ExperienceBank: vector/{_EMBED_FILE} missing but other index files present"
            )

        try:
            meta_data = json.loads(meta_path.read_text(encoding="utf-8"))
            meta = _KnowledgeMeta(**meta_data)
        except Exception as exc:
            LOGGER.error("ExperienceBank: failed to parse manifest: %s", exc, exc_info=True)
            raise

        if not self._validate_hashes(meta, scalar_path, faiss_path, emb_path):
            raise ValueError(
                "ExperienceBank: integrity check failed — manifest does not match on-disk files"
            )

        try:
            # 1. Load scalar metadata
            with open(scalar_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    data = json.loads(line)
                    item = ExperienceItem.from_dict(data)
                    self._items.append(item)
                    self._id_index[item.id] = item
                    self._skills.add(frozenset(item.skill_ids))

            # 2. Load embeddings matrix and re-hydrate in-memory items
            self._embedding_matrix = np.load(str(emb_path))
            # Re-normalize to ensure IndexFlatIP inner product = cosine similarity
            if self._embedding_matrix is not None and len(self._embedding_matrix) > 0:
                norms = np.linalg.norm(self._embedding_matrix, axis=1, keepdims=True)
                norms[norms == 0] = 1.0
                self._embedding_matrix = self._embedding_matrix / norms
            for i, item in enumerate(self._items):
                if i < len(self._embedding_matrix):
                    item.embedding = self._embedding_matrix[i].tolist()

            # 3. Load FAISS index
            self._faiss_index = faiss.read_index(str(faiss_path))

            if not self._validate_counts(meta):
                raise ValueError(
                    "ExperienceBank: integrity check failed — manifest counts do not match loaded data"
                )

            # 4. Initialize ID counter from max existing ID
            if self._items:
                max_id = 0
                for item in self._items:
                    if item.id.startswith("exp_"):
                        try:
                            num = int(item.id.split("_")[1])
                            max_id = max(max_id, num)
                        except (IndexError, ValueError) as exc:
                            LOGGER.warning(
                                "ExperienceBank: malformed item id %r while initializing id counter: %s",
                                item.id, exc,
                                exc_info=True,
                            )
                            raise
                self._next_id_counter = max_id + 1

            LOGGER.info("ExperienceBank: loaded %d items", len(self._items))
        except Exception as exc:
            LOGGER.error("ExperienceBank: failed to load data: %s", exc, exc_info=True)
            raise


    @staticmethod
    def _validate_hashes(
        meta: _KnowledgeMeta,
        scalar_path: Path,
        faiss_path: Path,
        emb_path: Path,
    ) -> bool:
        """Return True iff the manifest declares all three sha256 values
        and each matches the corresponding on-disk file.

        All three fields (scalar_sha256, vector_sha256, embeddings_sha256)
        MUST be non-empty and MUST hash-match their files. An empty field
        is treated as a corrupt/missing manifest entry — return False.
        File existence is already enforced by ``_load``'s pre-check.
        """
        if not meta.scalar_sha256:
            LOGGER.error("ExperienceBank: manifest missing scalar_sha256")
            return False
        scalar_actual = _sha256_file(scalar_path)
        if scalar_actual != meta.scalar_sha256:
            LOGGER.error("ExperienceBank: scalar sha256 mismatch")
            return False

        if not meta.vector_sha256:
            LOGGER.error("ExperienceBank: manifest missing vector_sha256")
            return False
        faiss_actual = _sha256_file(faiss_path)
        if faiss_actual != meta.vector_sha256:
            LOGGER.error("ExperienceBank: faiss sha256 mismatch")
            return False

        if not meta.embeddings_sha256:
            LOGGER.error("ExperienceBank: manifest missing embeddings_sha256")
            return False
        emb_actual = _sha256_file(emb_path)
        if emb_actual != meta.embeddings_sha256:
            LOGGER.error("ExperienceBank: embeddings sha256 mismatch")
            return False

        return True

    def _validate_counts(self, meta: _KnowledgeMeta) -> bool:
        """Return True iff loaded in-memory data matches the manifest counts.

        Runs after scalar + embedding data is loaded — compares against
        ``self._items`` and ``self._embedding_matrix``. The manifest's
        ``vector_count`` must equal BOTH the number of loaded items AND
        the number of embedding rows; a mismatch between items and
        embedding rows (even if both are non-zero) is also a failure.
        """
        if meta.vector_count != len(self._items):
            LOGGER.error(
                "ExperienceBank: vector_count mismatch — manifest=%d, items=%d",
                meta.vector_count, len(self._items),
            )
            return False
        if self._embedding_matrix is None:
            LOGGER.error("ExperienceBank: embeddings matrix not loaded")
            return False
        if len(self._embedding_matrix) != meta.vector_count:
            LOGGER.error(
                "ExperienceBank: embedding row count mismatch — manifest=%d, rows=%d",
                meta.vector_count, len(self._embedding_matrix),
            )
            return False

        if meta.vector_dimension and self._embedding_matrix is not None:
            actual_dim = self._embedding_matrix.shape[1]
            if meta.vector_dimension != actual_dim:
                LOGGER.error(
                    "ExperienceBank: vector_dimension mismatch — manifest=%d, actual=%d",
                    meta.vector_dimension, actual_dim,
                )
                return False

        return True


    def _persist(self) -> None:
        """Persist to index_dir atomically with an integrity manifest."""
        # Ensure root directory exists first

        if self._faiss_index is None or self._embedding_matrix is None:
            LOGGER.error("faiss index or embedding matrix is None")
            return
        self._dir.mkdir(parents=True, exist_ok=True)

        scalar_dir = self._dir / _SCALAR_DIR
        vector_dir = self._dir / _VECTOR_DIR
        scalar_dir.mkdir(parents=True, exist_ok=True)
        vector_dir.mkdir(parents=True, exist_ok=True)

        scalar_path = scalar_dir / _SCALAR_FILE
        faiss_path = vector_dir / _FAISS_FILE
        emb_path = vector_dir / _EMBED_FILE

        tmp_scalar = scalar_dir / (_SCALAR_FILE + ".tmp")
        tmp_faiss = vector_dir / "faiss_tmp"

        tmp_emb_src = vector_dir / "embed_tmp"
        tmp_emb = tmp_emb_src.with_suffix(".npy")
        tmp_meta = self._dir / (_META_FILE + ".tmp")

        written_tmps: list[Path] = []
        try:
            with open(tmp_scalar, "w", encoding="utf-8") as f:
                for item in self._items:
                    f.write(json.dumps(item.to_dict(), ensure_ascii=False) + "\n")
            written_tmps.append(tmp_scalar)


            faiss.write_index(self._faiss_index, str(tmp_faiss))
            written_tmps.append(tmp_faiss)


            np.save(str(tmp_emb_src), self._embedding_matrix)
            written_tmps.append(tmp_emb)

            dim = self._embedding_matrix.shape[1] if self._embedding_matrix is not None else 0
            meta = _KnowledgeMeta(
                vector_count=len(self._items),
                vector_algorithm=self._vector_algorithm,
                vector_dimension=dim,
                vector_sha256=_sha256_file(tmp_faiss),
                scalar_sha256=_sha256_file(tmp_scalar),
                embeddings_sha256=_sha256_file(tmp_emb),
            )
            tmp_meta.write_text(
                json.dumps(meta.__dict__, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            written_tmps.append(tmp_meta)

            _write_atomic(tmp_faiss, faiss_path)
            _write_atomic(tmp_emb, emb_path)
            _write_atomic(tmp_scalar, scalar_path)
            _write_atomic(tmp_meta, self._dir / _META_FILE)
        except Exception:
            LOGGER.error(
                "ExperienceBank: persist failed, cleaning up temp files",
                exc_info=True,
            )
            for p in written_tmps:
                try:
                    p.unlink(missing_ok=True)
                except OSError:
                    LOGGER.warning("ExperienceBank: failed to clean up temp file", exc_info=True)
            raise

    # ------------------------------------------------------------------
    # Index management
    # ------------------------------------------------------------------

    def _rebuild_index(self) -> None:
        """Rebuild FAISS index and embedding matrix from current _items."""
        if not self._items:
            self._faiss_index = None
            self._embedding_matrix = None
            return

        embeddings = []
        for item in self._items:
            if item.embedding:
                embeddings.append(item.embedding)

        if not embeddings:
            self._faiss_index = None
            self._embedding_matrix = None
            return

        arr = np.array(embeddings, dtype=np.float32)
        dim = arr.shape[1]

        index, actual_algorithm = _build_faiss_index(self._vector_algorithm, dim)
        self._vector_algorithm = actual_algorithm

        # IVF-style indices require training on the dataset first
        if isinstance(index, faiss.IndexIVF):
            if len(arr) >= index.nlist:
                index.train(arr)
            else:
                # Too few vectors for IVF; fall back to Flat
                LOGGER.info(
                    "ExperienceBank: too few vectors (%d) for IVF nlist=%d, falling back to Flat",
                    len(arr), index.nlist,
                )
                index = faiss.index_factory(dim, "Flat", faiss.METRIC_INNER_PRODUCT)
                self._vector_algorithm = "Flat"
        index.add(arr)

        self._faiss_index = index
        self._embedding_matrix = arr

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def generate_id(self) -> str:
        cid = self._next_id_counter
        self._next_id_counter += 1
        return f"exp_{cid:04d}"

    def create_item(
        self,
        query_pattern: str,
        query_examples: list[str],
        skill_ids: list[str],
        success_count: int = 1,
    ) -> ExperienceItem:
        """Create an ExperienceItem with auto-generated embedding and id.

        The embedding is derived from ``query_pattern`` only — a generalized
        template carries the skill's intent cleanly, while concatenating raw
        query_examples would dilute it with concrete entities and let
        same-domain (but wrong) skills cross the threshold. ``query_examples``
        is still stored on the item for display / debugging, just not embedded.
        """
        text_for_embedding = query_pattern or ""
        embedding = self._embedder.embed(text_for_embedding)

        return ExperienceItem(
            id=self.generate_id(),
            query_pattern=query_pattern,
            query_examples=query_examples,
            skill_ids=skill_ids,
            success_count=success_count,
            embedding=embedding,
            created_at=_now(),
            last_hit_at=_now(),
        )


def _now() -> float:
    return time.time()


__all__ = ["ExperienceBank"]
