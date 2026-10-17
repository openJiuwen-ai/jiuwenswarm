# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for the Scientific Artifact Memory Interface (search_claims).

Claim governance needs exact metadata filtering (tag/level/status) over the
structured claim ledger artifacts (claims/reviews/experiments YAML,
evolutions JSON) that live outside the markdown memory index. The query
layer scans the workspace directly: name-based ledger discovery, bounded and
defensive (caps on file count/size, malformed files skipped), normalizing
every record to a common shape with workspace-relative provenance, then
filters deterministically (case-insensitive exact matches, optional
substring query). No embeddings, no index state, no network — identical
queries return bit-identical records.

Sections:
  A. normalization — document shapes and record normalization,
  B. loading — ledger discovery, bounds, and degradation,
  C. filtering — exact/substring filters, ordering, limits,
  D. manager — the search_claims instance method on the real class.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import yaml

from jiuwenswarm.agents.harness.common.memory import manager as memory_manager
from jiuwenswarm.agents.harness.common.memory.config import MemorySettings
from jiuwenswarm.agents.harness.common.memory.manager import (
    MemoryIndexManager,
    _claim_list_from_doc,
    filter_claim_records,
    load_claim_records,
    normalize_claim_record,
)


def _write_ws(root: Path, rel: str, content: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _record(**overrides):
    record = {
        "id": "C-001",
        "status": "supported",
        "tags": ["evolution"],
        "level": "high",
        "text": "mutation improves robustness",
        "file": "claims.yml",
        "raw": {},
    }
    record.update(overrides)
    return record


# ---------------------------------------------------------------------------
# A. normalization
# ---------------------------------------------------------------------------


def test_claim_list_from_bare_list_filters_non_dicts() -> None:
    data = [{"id": "C-1"}, "junk", {"id": "C-2"}, None, 42]
    assert _claim_list_from_doc(data) == [{"id": "C-1"}, {"id": "C-2"}]


@pytest.mark.parametrize("key", ["claims", "entries", "items"])
def test_claim_list_from_mapping_known_keys(key: str) -> None:
    data = {key: [{"id": "C-1"}], "unrelated": [{"id": "C-9"}]}
    assert _claim_list_from_doc(data) == [{"id": "C-1"}]


def test_claim_list_from_unknown_shapes_yields_nothing() -> None:
    assert _claim_list_from_doc(None) == []
    assert _claim_list_from_doc("text") == []
    assert _claim_list_from_doc(7) == []
    assert _claim_list_from_doc({"unknown": [{"id": "C-1"}]}) == []
    assert _claim_list_from_doc({"claims": "not-a-list"}) == []


def test_normalize_claim_record_folds_fields() -> None:
    record = normalize_claim_record(
        {
            "id": 7,
            "status": "Frozen",
            "tag": "evolution",
            "tags": ["drift", "evolution", None],
            "confidence": "0.9",
            "statement": "The policy drifted",
            "context": "under mutation",
        },
        "claims.yml",
    )

    assert record is not None
    assert record["id"] == "7"  # ids stringify
    assert record["status"] == "Frozen"  # values pass through verbatim
    assert record["tags"] == ["drift", "evolution"]  # scalar tag + list, deduped, sorted
    assert record["level"] == "0.9"  # confidence fills the level slot
    assert record["text"] == "The policy drifted under mutation"
    assert record["file"] == "claims.yml"
    assert record["raw"]["confidence"] == "0.9"  # provenance keeps the original record


def test_normalize_claim_record_level_beats_confidence() -> None:
    record = normalize_claim_record(
        {"id": "C-1", "level": "L2", "confidence": "0.9"}, "claims.yml"
    )
    assert record is not None
    assert record["level"] == "L2"


def test_normalize_claim_record_rejects_non_claim_shapes() -> None:
    # A record carrying none of the claim marker fields is not a claim.
    assert normalize_claim_record({}, "x.yml") is None
    assert normalize_claim_record({"title": "plain notes"}, "x.yml") is None
    assert normalize_claim_record("not a dict", "x.yml") is None


# ---------------------------------------------------------------------------
# B. loading
# ---------------------------------------------------------------------------


def test_load_claim_records_discovers_ledgers_with_provenance(tmp_path: Path) -> None:
    _write_ws(tmp_path, "claims.yml", yaml.safe_dump({"claims": [{"id": "C-001", "status": "supported"}]}))
    _write_ws(tmp_path, "sub/reviews.yaml", yaml.safe_dump({"entries": [{"id": "R-001", "level": "high"}]}))
    _write_ws(tmp_path, "sub/deep/experiments.yml", yaml.safe_dump([{"id": "E-001", "text": "ablation"}]))
    _write_ws(tmp_path, "evolutions.json", json.dumps({"items": [{"id": "V-001", "status": "frozen"}]}))
    _write_ws(tmp_path, "extra.claims.yml", yaml.safe_dump({"claims": [{"id": "X-001", "tag": "side"}]}))
    _write_ws(tmp_path, "notes.md", "# not a ledger\n")
    _write_ws(tmp_path, "config.yaml", "unrelated: true\n")  # not a ledger name

    records = load_claim_records(str(tmp_path))

    by_id = {r["id"]: r for r in records}
    assert set(by_id) == {"C-001", "R-001", "E-001", "V-001", "X-001"}
    # Provenance is workspace-relative, platform-correct.
    assert by_id["R-001"]["file"] == os.path.join("sub", "reviews.yaml")
    assert by_id["V-001"]["file"] == "evolutions.json"


def test_load_claim_records_skips_malformed_files(tmp_path: Path) -> None:
    _write_ws(tmp_path, "claims.yml", "{{{ not yaml")
    _write_ws(tmp_path, "evolutions.json", "{broken json")
    _write_ws(tmp_path, "reviews.yaml", yaml.safe_dump({"entries": [{"id": "R-001", "status": "ok"}]}))

    records = load_claim_records(str(tmp_path))

    assert [r["id"] for r in records] == ["R-001"]


def test_load_claim_records_skips_oversized_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(memory_manager, "CLAIMS_MAX_FILE_BYTES", 32)
    _write_ws(tmp_path, "claims.yml", yaml.safe_dump({"claims": [{"id": "BIG-1", "tag": "x"}]}) + " " * 200)
    _write_ws(tmp_path, "reviews.yaml", yaml.safe_dump({"entries": [{"id": "OK-1", "tag": "x"}]}))

    records = load_claim_records(str(tmp_path))

    assert [r["id"] for r in records] == ["OK-1"]


def test_load_claim_records_caps_file_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(memory_manager, "CLAIMS_MAX_FILES", 2)
    for i in range(3):
        _write_ws(
            tmp_path,
            f"team{i}.claims.yml",
            yaml.safe_dump({"claims": [{"id": f"T-{i}", "tag": "x"}]}),
        )

    records = load_claim_records(str(tmp_path))

    assert len(records) == 2


# ---------------------------------------------------------------------------
# C. filtering
# ---------------------------------------------------------------------------


def test_filter_by_tag_matches_any_tag_case_insensitively() -> None:
    records = [
        _record(id="C-1", tags=["Evolution", "Drift"]),
        _record(id="C-2", tags=["planning"]),
        _record(id="C-3", tags=[]),
    ]
    assert [r["id"] for r in filter_claim_records(records, tag="EVOLUTION")] == ["C-1"]
    assert filter_claim_records(records, tag="absent") == []


def test_filter_by_level_and_status_exact_case_insensitive() -> None:
    records = [
        _record(id="C-1", level="High", status="Supported"),
        _record(id="C-2", level="low", status="proposed"),
    ]
    assert [r["id"] for r in filter_claim_records(records, level="high")] == ["C-1"]
    assert [r["id"] for r in filter_claim_records(records, status="PROPOSED")] == ["C-2"]


def test_filter_query_substring_covers_all_fields() -> None:
    records = [
        _record(id="C-1", text="mutation improves robustness"),
        _record(id="C-2", status="contested", text="nothing relevant"),
        _record(id="C-3", tags=["ablation-study"], text="x"),
        _record(id="C-9", level="pilot", text="x"),
    ]
    assert [r["id"] for r in filter_claim_records(records, query="MUTATION")] == ["C-1"]
    assert [r["id"] for r in filter_claim_records(records, query="Contested")] == ["C-2"]
    assert [r["id"] for r in filter_claim_records(records, query="ablation")] == ["C-3"]
    assert [r["id"] for r in filter_claim_records(records, query="PILOT")] == ["C-9"]
    assert filter_claim_records(records, query="absent") == []


def test_filter_preserves_ledger_order_and_caps_limit() -> None:
    records = [_record(id=f"C-{i:03d}") for i in range(5)]

    assert [r["id"] for r in filter_claim_records(records, limit=2)] == ["C-000", "C-001"]
    assert len(filter_claim_records(records)) == 5  # default limit (50) not hit
    assert len(filter_claim_records(records[:3], limit=0)) == 1  # clamped to >= 1


def test_filter_combines_constraints() -> None:
    records = [
        _record(id="C-1", tags=["evolution"], level="high", status="supported"),
        _record(id="C-2", tags=["evolution"], level="low", status="supported"),
        _record(id="C-3", tags=["planning"], level="high", status="supported"),
    ]
    out = filter_claim_records(records, tag="evolution", level="high", status="supported")
    assert [r["id"] for r in out] == ["C-1"]


# ---------------------------------------------------------------------------
# D. manager
# ---------------------------------------------------------------------------


def _make_manager(workspace: Path) -> MemoryIndexManager:
    return MemoryIndexManager(
        agent_id="unit-test",
        workspace_dir=str(workspace),
        settings=MemorySettings(),
    )


@pytest.mark.asyncio
async def test_search_claims_round_trip_on_real_manager(tmp_path: Path) -> None:
    _write_ws(
        tmp_path,
        "claims.yml",
        yaml.safe_dump(
            {
                "claims": [
                    {"id": "C-001", "status": "supported", "tags": ["evolution"], "statement": "drift is measurable"},
                    {"id": "C-002", "status": "proposed", "tags": ["planning"], "statement": "future work"},
                ]
            }
        ),
    )
    manager = _make_manager(tmp_path)

    by_tag = await manager.search_claims(tag="evolution")
    assert [r["id"] for r in by_tag] == ["C-001"]
    assert by_tag[0]["file"] == "claims.yml"
    assert by_tag[0]["status"] == "supported"

    by_status = await manager.search_claims(status="proposed")
    assert [r["id"] for r in by_status] == ["C-002"]

    by_query = await manager.search_claims(query="DRIFT")
    assert [r["id"] for r in by_query] == ["C-001"]


@pytest.mark.asyncio
async def test_search_claims_is_bit_identical_across_calls(tmp_path: Path) -> None:
    _write_ws(
        tmp_path,
        "claims.yml",
        yaml.safe_dump({"claims": [{"id": "C-001", "status": "supported", "tags": ["evolution"]}]}),
    )
    manager = _make_manager(tmp_path)

    first = await manager.search_claims()
    second = await manager.search_claims()

    assert first == second  # same records, same order, same provenance


@pytest.mark.asyncio
async def test_search_claims_empty_workspace_returns_nothing(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)

    assert await manager.search_claims() == []
    assert await manager.search_claims(tag="evolution", query="anything") == []


@pytest.mark.asyncio
async def test_search_claims_needs_no_index_initialization(tmp_path: Path) -> None:
    # The interface reads workspace files directly: constructing the manager
    # is enough — no sqlite DB, no embedding provider, no index warm-up.
    manager = _make_manager(tmp_path)
    _write_ws(tmp_path, "claims.yml", yaml.safe_dump({"claims": [{"id": "C-001", "tag": "x"}]}))

    out = await manager.search_claims()

    assert [r["id"] for r in out] == ["C-001"]
    assert manager.db is None
    assert manager.provider is None
