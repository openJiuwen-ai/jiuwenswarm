"""Tests for ``ExperienceBaseBuilder._merge_similar_patterns``.

Covers the pattern-deduplication behavior introduced to collapse
near-duplicate distilled patterns (embedding cosine similarity >=
``pattern_merge_threshold``) into a single experience item, sampling
``merged_query_examples_count`` queries from the pooled success traces.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest

from openjiuwen.symphony.experience.bank import ExperienceBank
from openjiuwen.symphony.experience.cluster import ClusteredQuery, cluster_traces
from openjiuwen.symphony.experience.collector import ExperienceBaseBuilder
from openjiuwen.symphony.experience.retriever import ExperienceRetriever
from openjiuwen.symphony.experience.models import (
    DistilledPattern,
    ExperienceBankBuildConfig,
    ExperienceItem,
    TraceRecord,
)

class FakeEmbedder:
    def __init__(self, vectors: dict[str, list[float]]) -> None:
        self._vectors = {k: list(v) for k, v in vectors.items()}
        self.embed_calls: list[str] = []

    def _unit(self, vec: list[float]) -> list[float]:
        arr = np.asarray(vec, dtype=np.float32)
        n = float(np.linalg.norm(arr))
        if n == 0.0:
            return list(arr)
        return (arr / n).tolist()

    def embed(self, text: str) -> list[float]:
        self.embed_calls.append(text)
        return self._unit(self._vectors[text])

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [self.embed(t) for t in texts]


class FakeBank:
    """Minimal stand-in for ``ExperienceBank`` recording created items."""

    def __init__(self) -> None:
        self.items: list[ExperienceItem] = []
        self._counter = 0

    @property
    def count(self) -> int:
        return len(self.items)

    def create_item(
        self,
        query_pattern: str,
        query_examples: list[str],
        skill_ids: list[str],
        success_count: int = 1,
    ) -> ExperienceItem:
        self._counter += 1
        return ExperienceItem(
            id=f"exp_{self._counter:04d}",
            query_pattern=query_pattern,
            query_examples=list(query_examples),
            skill_ids=list(skill_ids),
            success_count=success_count,
            created_at=0.0,
            last_hit_at=0.0,
        )

    def add_batch(self, items: list[ExperienceItem]) -> None:
        self.items.extend(items)


class _FakeBankForRetriever:
    """ExperienceBank stand-in exposing ``search_by_embedding`` for the
    retriever. Returns a fixed, ordered list of (score, item) so the test can
    assert the dedup + order-preservation behavior of ``ExperienceRetriever``.
    """

    def __init__(self, results: list[tuple[float, ExperienceItem]]) -> None:
        self._results = results
        self.search_calls: list[str] = []

    def search_by_embedding(
            self,
            query: str,
            top_k: int = 1,
            threshold: float = 0.80,
    ) -> list[tuple[float, ExperienceItem]]:
        self.search_calls.append(query)
        return list(self._results)


def _item(item_id: str, skill_ids: list[str]) -> ExperienceItem:
    return ExperienceItem(
        id=item_id,
        query_pattern="p",
        query_examples=[],
        skill_ids=list(skill_ids),
        success_count=1,
        created_at=0.0,
        last_hit_at=0.0,
    )


def _pattern(
    cluster_id: int,
    description: str,
    *,
    skills: list[str] | None = None,
) -> DistilledPattern:
    return DistilledPattern(
        cluster_id=cluster_id,
        effective_skills=list(skills) if skills else [],
        pattern_description=description,
    )


def _cluster(cluster_id: int, success_queries: list[str]) -> ClusteredQuery:
    traces = [
        TraceRecord(
            trace_id=f"t{cluster_id}_{i}",
            query=q,
            skills=[],
            messages=[],
            result="",
            success=True,
        )
        for i, q in enumerate(success_queries)
    ]
    return ClusteredQuery(
        cluster_id=cluster_id,
        centroid_query=success_queries[0] if success_queries else "",
        member_traces=list(traces),
        success_traces=list(traces),
        failure_traces=[],
    )


def _builder(
    embedder: FakeEmbedder,
    *,
    merge_threshold: float = 0.9,
    sample_count: int = 5,
    kb: Any = None,
) -> ExperienceBaseBuilder:
    # Bypass __init__ — we only exercise the merge / create paths.
    import random
    b = ExperienceBaseBuilder.__new__(ExperienceBaseBuilder)
    b._embedder = embedder
    b._pattern_merge_threshold = merge_threshold
    b._query_examples_count = sample_count
    b._kb = kb
    b._rng = random.Random(42)
    return b

class _RecordingDistiller:
    """Distiller stub returning exactly the patterns handed to it, so we can
    isolate the merge + write stages of ``build`` without an LLM call."""

    def __init__(self, patterns: list[DistilledPattern]) -> None:
        self._patterns = patterns

    def run(self, _clusters: list[Any]) -> list[DistilledPattern]:
        return list(self._patterns)


def test_build_drops_traces_with_success_false(monkeypatch) -> None:
    """build() filters out success=False traces before they reach cluster_traces."""
    embedder = FakeEmbedder({"p": [1.0, 0.0]})
    bank = FakeBank()

    captured: list[list[TraceRecord]] = []

    def fake_cluster_traces(traces, *_a, **_k):
        captured.append(list(traces))
        return [ClusteredQuery(0, "ok", [], [], [])]

    import openjiuwen.symphony.experience.collector as collector_mod
    monkeypatch.setattr(collector_mod, "cluster_traces", fake_cluster_traces)
    monkeypatch.setattr(
        collector_mod, "TraceDistiller",
        lambda *a, **kw: _RecordingDistiller([_pattern(0, "p", skills=["s"])]),
    )

    builder = ExperienceBaseBuilder(
        kb=bank,
        embedding_client=embedder,
        llm_client=object(),
        llm_model="any",
        build_config=ExperienceBankBuildConfig(),
    )

    traces = [
        TraceRecord(trace_id="ok1", query="q1", skills=["s"], success=True),
        TraceRecord(trace_id="bad1", query="q2", skills=["s"], success=False),
        TraceRecord(trace_id="bad2", query="q3", skills=["s"], success=False),
    ]
    builder.build(traces)

    assert len(captured) == 1
    assert [t.trace_id for t in captured[0]] == ["ok1"]


def test_build_drops_traces_with_empty_skills(monkeypatch) -> None:
    """build() filters out traces with empty skills list before cluster_traces."""
    embedder = FakeEmbedder({"p": [1.0, 0.0]})
    bank = FakeBank()

    captured: list[list[TraceRecord]] = []

    def fake_cluster_traces(traces, *_a, **_k):
        captured.append(list(traces))
        return [ClusteredQuery(0, "ok", [], [], [])]

    import openjiuwen.symphony.experience.collector as collector_mod
    monkeypatch.setattr(collector_mod, "cluster_traces", fake_cluster_traces)
    monkeypatch.setattr(
        collector_mod, "TraceDistiller",
        lambda *a, **kw: _RecordingDistiller([_pattern(0, "p", skills=["s"])]),
    )

    builder = ExperienceBaseBuilder(
        kb=bank,
        embedding_client=embedder,
        llm_client=object(),
        llm_model="any",
        build_config=ExperienceBankBuildConfig(),
    )

    traces = [
        TraceRecord(trace_id="ok1", query="q1", skills=["s"], success=True),
        TraceRecord(trace_id="bad1", query="q2", skills=[], success=True),
        TraceRecord(trace_id="bad2", query="q3", skills=[], success=True),
    ]
    builder.build(traces)

    assert len(captured) == 1
    assert [t.trace_id for t in captured[0]] == ["ok1"]


def test_build_returns_zero_when_all_traces_invalid(monkeypatch) -> None:
    """All traces invalid -> build() returns 0 without calling cluster_traces."""
    embedder = FakeEmbedder({})
    bank = FakeBank()

    called = {"cluster": False}

    def fake_cluster_traces(*_a, **_k):
        called["cluster"] = True
        return []

    import openjiuwen.symphony.experience.collector as collector_mod
    monkeypatch.setattr(collector_mod, "cluster_traces", fake_cluster_traces)

    builder = ExperienceBaseBuilder(
        kb=bank,
        embedding_client=embedder,
        llm_client=object(),
        llm_model="any",
        build_config=ExperienceBankBuildConfig(),
    )

    traces = [
        TraceRecord(trace_id="bad1", query="q1", skills=["s"], success=False),
        TraceRecord(trace_id="bad2", query="q2", skills=[], success=True),
    ]
    created = builder.build(traces)

    assert created == 0
    assert called["cluster"] is False


def test_build_logs_dropped_count(monkeypatch) -> None:
    """build() logs how many invalid traces were dropped."""
    import logging
    embedder = FakeEmbedder({"p": [1.0, 0.0]})
    bank = FakeBank()

    captured: list[list[TraceRecord]] = []

    def fake_cluster_traces(traces, *_a, **_k):
        captured.append(list(traces))
        return [ClusteredQuery(0, "ok", [], [], [])]

    import openjiuwen.symphony.experience.collector as collector_mod
    monkeypatch.setattr(collector_mod, "cluster_traces", fake_cluster_traces)
    monkeypatch.setattr(
        collector_mod, "TraceDistiller",
        lambda *a, **kw: _RecordingDistiller([_pattern(0, "p", skills=["s"])]),
    )

    builder = ExperienceBaseBuilder(
        kb=bank,
        embedding_client=embedder,
        llm_client=object(),
        llm_model="any",
        build_config=ExperienceBankBuildConfig(),
    )

    traces = [
        TraceRecord(trace_id="ok1", query="q1", skills=["s"], success=True),
        TraceRecord(trace_id="bad1", query="q2", skills=["s"], success=False),
        TraceRecord(trace_id="bad2", query="q3", skills=[], success=True),
    ]

    records: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = records.append
    logger = logging.getLogger("openjiuwen.symphony.experience.collector")
    prev_level = logger.level
    logger.setLevel(logging.WARNING)
    logger.addHandler(handler)
    try:
        count = builder.build(traces)
        assert count == 1
    finally:
        logger.removeHandler(handler)
        logger.setLevel(prev_level)

    msgs = [r.getMessage() for r in records]
    assert any("dropped 2" in m and "3 provided" in m for m in msgs)


def _add_builder(*, flush_threshold: int = 100) -> ExperienceBaseBuilder:
    """Minimal builder for add()-only tests: just the fields add() touches."""
    import threading
    b = ExperienceBaseBuilder.__new__(ExperienceBaseBuilder)
    b._pending = []
    b._flush_threshold = flush_threshold
    b._lock = threading.Lock()
    return b



def test_add_accepts_valid_trace() -> None:
    builder = _add_builder()

    trace = TraceRecord(trace_id="t1", query="q", skills=["s"], success=True)
    builder.add(trace)
    assert len(builder._pending) == 1
    assert builder._pending[0].trace_id == "t1"

    bad_trace = TraceRecord(trace_id="bad", query="q", skills=[], success=True)
    builder.add(bad_trace)
    assert len(builder._pending) == 1


def test_trace_record_query_defaults_to_empty_string() -> None:
    """TraceRecord must be constructible with only trace_id — query defaults to "".

    Regression: ``query`` used to be a required positional field, so callers
    that built a record from a partially-known session (e.g. only the
    request_id available) had to pass a placeholder. Making it default to
    ``""`` aligns the dataclass with the already-tolerant ``from_dict``,
    which reads ``data.get("query", "")``.
    """
    record = TraceRecord(trace_id="t1")
    assert record.query == ""
    assert record.skills == []
    assert record.messages == []
    assert record.result == ""
    assert record.success is False


def test_trace_record_to_dict_preserves_default_query() -> None:
    """to_dict must emit the default ``query`` as ``""`` for round-trip parity."""
    record = TraceRecord(trace_id="t1")
    assert record.to_dict()["query"] == ""


def test_trace_record_from_dict_tolerates_missing_query() -> None:
    """from_dict must not raise when the persisted record lacks ``query``.

    Regression: the persistence layer (records.jsonl) may carry legacy rows
    written before ``query`` was added; ``from_dict`` already used
    ``data.get("query", "")`` and must keep accepting such rows.
    """
    record = TraceRecord.from_dict({"trace_id": "t_legacy", "skills": ["s"]})
    assert record.trace_id == "t_legacy"
    assert record.query == ""
    assert record.skills == ["s"]


def test_trace_record_round_trip_preserves_empty_and_populated_query() -> None:
    """to_dict -> from_dict round-trip must preserve ``query`` in both states."""
    empty = TraceRecord(trace_id="t1")
    assert TraceRecord.from_dict(empty.to_dict()).query == ""

    populated = TraceRecord(trace_id="t2", query="how do I x", skills=["s"])
    assert TraceRecord.from_dict(populated.to_dict()).query == "how do I x"


def test_add_propagates_flush_exception_instead_of_dropping_pending(monkeypatch) -> None:
    """When the LLM is down, distiller.run() must surface the error from
    add()'s auto-flush rather than silently clearing the pending buffer.

    Regression: distiller.run used to log-and-drop per-cluster exceptions,
    return [], and let flush_snapshot report created=0 — the pending
    snapshot had already been cleared by add(), so the data was lost
    without any signal to the caller.
    """
    import threading

    class _BoomDistiller:
        def run(self, _clusters: list[Any]) -> list[DistilledPattern]:
            raise RuntimeError("LLM unavailable")

    builder = ExperienceBaseBuilder.__new__(ExperienceBaseBuilder)
    builder._pending = []
    builder._flush_threshold = 1
    builder._lock = threading.Lock()
    builder._min_hits = 1
    builder._skill_cluster_num = None
    builder._min_cluster_size = 1
    builder._embedder = FakeEmbedder({"q": [1.0, 0.0]})
    builder._kb = FakeBank()
    builder._skills_info = None
    builder._max_workers = 1
    builder._max_success_examples = 1
    builder._pattern_merge_threshold = 1.0
    builder._query_examples_count = 0

    import openjiuwen.symphony.experience.collector as collector_mod
    monkeypatch.setattr(
        collector_mod, "TraceDistiller",
        lambda *a, **kw: _BoomDistiller(),
    )

    trace = TraceRecord(trace_id="t1", query="q", skills=["s"], success=True)
    with pytest.raises(Exception):
        builder.add(trace)
    assert builder._pending == [], "auto-flush failure should still leave pending cleared by add()"


def test_retriever_dedups_shared_skills_preserving_similarity_order() -> None:
    # Two items, both referencing skill "s1"; the higher-score item also
    # carries "s2". Without dedup the flat comprehension would yield
    # ["s1", "s2", "s1"].
    results = [
        (0.95, _item("exp_0001", ["s1", "s2"])),
        (0.88, _item("exp_0002", ["s1"])),
    ]
    kb = _FakeBankForRetriever(results)
    retriever = ExperienceRetriever(kb=kb, threshold=0.5, top_k=2)

    out = retriever.search("q")

    assert out == ["s1", "s2"]
    # First occurrence wins: s1 comes from the higher-score item (exp_0001),
    # not the lower one (exp_0002), and is not repeated.
    assert out[0] == "s1"
    assert out.count("s1") == 1


def test_retriever_returns_empty_when_no_results() -> None:
    kb = _FakeBankForRetriever([])
    retriever = ExperienceRetriever(kb=kb, threshold=0.5, top_k=2)

    assert retriever.search("q") == []


def test_retriever_keeps_disjoint_skills_in_order() -> None:
    # No overlap between items — order must still follow FAISS similarity rank.
    results = [
        (0.9, _item("exp_0001", ["alpha"])),
        (0.8, _item("exp_0002", ["beta"])),
        (0.7, _item("exp_0003", ["gamma"])),
    ]
    kb = _FakeBankForRetriever(results)
    retriever = ExperienceRetriever(kb=kb, threshold=0.5, top_k=3)

    assert retriever.search("q") == ["alpha", "beta", "gamma"]


def test_retriever_dedups_repeated_skills_within_a_single_item() -> None:
    """The dedup runs on the flattened generator, so duplicates *within* a
    single item's ``skill_ids`` list are also collapsed — not just repeats
    across items. First-occurrence order is preserved.

    Against the pre-dedup comprehension the input below would yield
    ``["s1", "s2", "s1", "s3", "s2"]``; the fix produces ``["s1", "s2", "s3"]``.
    """
    results = [
        (0.95, _item("exp_0001", ["s1", "s2", "s1", "s3", "s2"])),
    ]
    kb = _FakeBankForRetriever(results)
    retriever = ExperienceRetriever(kb=kb, threshold=0.5, top_k=1)

    out = retriever.search("q")

    assert out == ["s1", "s2", "s3"]
    for skill in ("s1", "s2", "s3"):
        assert out.count(skill) == 1


def test_load_raises_on_corrupt_meta_json(tmp_path) -> None:
    """When meta.json cannot be parsed, ExperienceBank._load must surface
    the error rather than swallow it.

    Regression: the integrity-check try/except used to log "failed to parse
    manifest, skipping check" and fall through with ``meta`` unbound, so
    the downstream ``meta.vector_count`` reference raised
    ``UnboundLocalError`` — masked by the data-load try/except as
    "failed to load data: cannot access local variable 'meta'". Both
    except blocks now log and re-raise; construction fails fast.
    """
    import json

    (tmp_path / "meta.json").write_text("{ not valid json", encoding="utf-8")
    scalar_dir = tmp_path / "scalar"
    scalar_dir.mkdir(parents=True, exist_ok=True)
    (scalar_dir / "metadata.jsonl").write_text(
        json.dumps({
            "id": "exp_0001",
            "query_pattern": "p",
            "query_examples": [],
            "skill_ids": ["s"],
            "success_count": 1,
            "created_at": 0.0,
            "last_hit_at": 0.0,
            "embedding": [],
        }) + "\n",
        encoding="utf-8",
    )

    embedder = FakeEmbedder({"p": [1.0, 0.0]})

    with pytest.raises(Exception):
        ExperienceBank(tmp_path, embedder)


def test_load_starts_empty_when_all_index_files_absent(tmp_path) -> None:
    """Empty-start contract: when meta.json, scalar/, and vector/ are all
    absent, the bank starts empty instead of raising."""
    embedder = FakeEmbedder({"p": [1.0, 0.0]})

    kb = ExperienceBank(tmp_path, embedder)

    assert kb.count == 0
    assert kb.items == []


def test_load_succeeds_with_valid_manifest_and_data(tmp_path) -> None:
    """Valid manifest + scalar/vector data with matching SHA256 loads cleanly."""
    import hashlib
    import json

    import faiss
    import numpy as np

    scalar_dir = tmp_path / "scalar"
    scalar_dir.mkdir(parents=True, exist_ok=True)
    scalar_path = scalar_dir / "metadata.jsonl"
    (scalar_path).write_text(
        json.dumps({
            "id": "exp_0001",
            "query_pattern": "p",
            "query_examples": [],
            "skill_ids": ["s"],
            "success_count": 1,
            "created_at": 0.0,
            "last_hit_at": 0.0,
            "embedding": [1.0, 0.0],
        }) + "\n",
        encoding="utf-8",
    )

    vector_dir = tmp_path / "vector"
    vector_dir.mkdir(parents=True, exist_ok=True)
    emb_path = vector_dir / "embeddings.npy"
    faiss_path = vector_dir / "faiss_index.bin"

    arr = np.array([[1.0, 0.0]], dtype=np.float32)
    np.save(str(emb_path.with_suffix("")), arr)
    index = faiss.index_factory(2, "Flat", faiss.METRIC_INNER_PRODUCT)
    index.add(arr)
    faiss.write_index(index, str(faiss_path))

    scalar_sha = hashlib.sha256(scalar_path.read_bytes()).hexdigest()
    emb_sha = hashlib.sha256(emb_path.read_bytes()).hexdigest()
    faiss_sha = hashlib.sha256(faiss_path.read_bytes()).hexdigest()
    (tmp_path / "meta.json").write_text(
        json.dumps({
            "version": 1,
            "vector_count": 1,
            "vector_algorithm": "Flat",
            "vector_dimension": 2,
            "vector_sha256": faiss_sha,
            "scalar_sha256": scalar_sha,
            "embeddings_sha256": emb_sha,
        }),
        encoding="utf-8",
    )
    embedder = FakeEmbedder({"p": [1.0, 0.0]})

    kb = ExperienceBank(tmp_path, embedder)

    assert kb.count == 1


@pytest.mark.parametrize(
    "write_meta, write_scalar, write_faiss, write_embeddings, expected_exc",
    [
        (True, False, False, False, Exception),
        (False, True, False, False, Exception),
        (True, True, False, True, FileNotFoundError),
        (True, True, True, False, FileNotFoundError),
    ],
)
def test_load_raises_when_partial_state_present(
    tmp_path, write_meta, write_scalar, write_faiss, write_embeddings, expected_exc,
) -> None:
    """Any partial on-disk state (some index files present, others absent)
    must raise rather than silently load incomplete data.

    Parametrized over the four "which file is missing" precheck branches.
    """
    import json

    if write_scalar:
        scalar_dir = tmp_path / "scalar"
        scalar_dir.mkdir(parents=True, exist_ok=True)
        (scalar_dir / "metadata.jsonl").write_text(
            json.dumps({
                "id": "exp_0001",
                "query_pattern": "p",
                "query_examples": [],
                "skill_ids": ["s"],
                "success_count": 1,
                "created_at": 0.0,
                "last_hit_at": 0.0,
                "embedding": [1.0, 0.0],
            }) + "\n",
            encoding="utf-8",
        )
    if write_faiss or write_embeddings:
        vector_dir = tmp_path / "vector"
        vector_dir.mkdir(parents=True, exist_ok=True)
        if write_embeddings:
            import numpy as np
            np.save(
                str(vector_dir / "embeddings.npy"),
                np.array([[1.0, 0.0]], dtype=np.float32),
            )
        if write_faiss:
            import faiss
            import numpy as np
            arr = np.array([[1.0, 0.0]], dtype=np.float32)
            index = faiss.index_factory(2, "Flat", faiss.METRIC_INNER_PRODUCT)
            index.add(arr)
            faiss.write_index(index, str(vector_dir / "faiss_index.bin"))
    if write_meta:
        (tmp_path / "meta.json").write_text(
            json.dumps({
                "version": 1,
                "vector_count": 1,
                "vector_algorithm": "Flat",
                "vector_dimension": 2,
                "vector_sha256": "",
                "scalar_sha256": "",
                "embeddings_sha256": "",
            }),
            encoding="utf-8",
        )

    embedder = FakeEmbedder({"p": [1.0, 0.0]})

    with pytest.raises(expected_exc):
        ExperienceBank(tmp_path, embedder)


@pytest.mark.parametrize(
    "manifest_overrides, expected_exc",
    [
        ({"scalar_sha256": "deadbeef" * 8}, Exception),
        ({"embeddings_sha256": "deadbeef" * 8}, Exception),
        ({"scalar_sha256": ""}, Exception),
        ({"vector_count": 1}, Exception),
    ],
)
def test_load_raises_when_manifest_field_invalid(
    tmp_path, manifest_overrides, expected_exc,
) -> None:
    """A full valid on-disk index whose manifest declares one corrupted
    sha256 / count field must raise. Parametrized over the four integrity
    failures: scalar hash mismatch, embeddings hash mismatch, empty
    scalar_sha256, and embedding row count != vector_count.
    """
    import hashlib
    import json

    import faiss
    import numpy as np

    scalar_dir = tmp_path / "scalar"
    scalar_dir.mkdir(parents=True, exist_ok=True)
    scalar_path = scalar_dir / "metadata.jsonl"
    (scalar_path).write_text(
        json.dumps({
            "id": "exp_0001",
            "query_pattern": "p",
            "query_examples": [],
            "skill_ids": ["s"],
            "success_count": 1,
            "created_at": 0.0,
            "last_hit_at": 0.0,
            "embedding": [1.0, 0.0],
        }) + "\n",
        encoding="utf-8",
    )

    vector_dir = tmp_path / "vector"
    vector_dir.mkdir(parents=True, exist_ok=True)
    emb_path = vector_dir / "embeddings.npy"
    faiss_path = vector_dir / "faiss_index.bin"

    # Two-row embedding matrix — used by the row-count-mismatch scenario;
    # other scenarios override vector_count to match.
    arr = np.array([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32)
    np.save(str(emb_path), arr)
    index = faiss.index_factory(2, "Flat", faiss.METRIC_INNER_PRODUCT)
    index.add(arr)
    faiss.write_index(index, str(faiss_path))

    scalar_sha = hashlib.sha256(scalar_path.read_bytes()).hexdigest()
    emb_sha = hashlib.sha256(emb_path.read_bytes()).hexdigest()
    faiss_sha = hashlib.sha256(faiss_path.read_bytes()).hexdigest()
    base_manifest = {
        "version": 1,
        "vector_count": 2,
        "vector_algorithm": "Flat",
        "vector_dimension": 2,
        "vector_sha256": faiss_sha,
        "scalar_sha256": scalar_sha,
        "embeddings_sha256": emb_sha,
    }
    base_manifest.update(manifest_overrides)
    (tmp_path / "meta.json").write_text(
        json.dumps(base_manifest),
        encoding="utf-8",
    )
    embedder = FakeEmbedder({"p": [1.0, 0.0]})

    with pytest.raises(expected_exc):
        ExperienceBank(tmp_path, embedder)


def test_load_raises_on_scalar_sha256_mismatch(tmp_path) -> None:
    """Scalar file SHA256 mismatch must raise.

    Dedicated regression for the ``_validate_hashes`` scalar branch
    (bank.py: scalar_actual != meta.scalar_sha256). Builds a fully valid
    on-disk index where ``vector_sha256`` and ``embeddings_sha256`` are
    correct so the validator reaches the scalar comparison rather than
    short-circuiting on an empty/invalid vector hash first — and only the
    scalar hash is corrupted.
    """
    import hashlib
    import json

    import faiss
    import numpy as np

    scalar_dir = tmp_path / "scalar"
    scalar_dir.mkdir(parents=True, exist_ok=True)
    scalar_path = scalar_dir / "metadata.jsonl"
    (scalar_path).write_text(
        json.dumps({
            "id": "exp_0001",
            "query_pattern": "p",
            "query_examples": [],
            "skill_ids": ["s"],
            "success_count": 1,
            "created_at": 0.0,
            "last_hit_at": 0.0,
            "embedding": [1.0, 0.0],
        }) + "\n",
        encoding="utf-8",
    )

    vector_dir = tmp_path / "vector"
    vector_dir.mkdir(parents=True, exist_ok=True)
    emb_path = vector_dir / "embeddings.npy"
    faiss_path = vector_dir / "faiss_index.bin"

    arr = np.array([[1.0, 0.0]], dtype=np.float32)
    np.save(str(emb_path), arr)
    index = faiss.index_factory(2, "Flat", faiss.METRIC_INNER_PRODUCT)
    index.add(arr)
    faiss.write_index(index, str(faiss_path))

    emb_sha = hashlib.sha256(emb_path.read_bytes()).hexdigest()
    faiss_sha = hashlib.sha256(faiss_path.read_bytes()).hexdigest()
    (tmp_path / "meta.json").write_text(
        json.dumps({
            "version": 1,
            "vector_count": 1,
            "vector_algorithm": "Flat",
            "vector_dimension": 2,
            # Correct — must not short-circuit the validator on the vector branch.
            "vector_sha256": faiss_sha,
            # Deliberately wrong — the scalar mismatch path under test.
            "scalar_sha256": "deadbeef" * 8,
            # Correct — must not short-circuit the validator on the embeddings branch.
            "embeddings_sha256": emb_sha,
        }),
        encoding="utf-8",
    )
    embedder = FakeEmbedder({"p": [1.0, 0.0]})

    with pytest.raises(Exception):
        ExperienceBank(tmp_path, embedder)