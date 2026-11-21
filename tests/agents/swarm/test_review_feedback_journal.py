# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for the reviewer-feedback evolution journal.

The journal is the durable audit surface for reviewer-feedback evolution:
every settled attribution lands in ``<team_ws>/workspace/evolutions.json``
as a schema-frozen entry with an incremental ``E-NNN`` id, evanescent
attributions deduplicate per (source, task, round) while actionable ones
always record, and every failure mode (no team root, unreadable or corrupt
file, I/O errors) degrades to advisory logging without ever raising into
the evolution path.

The attribution sink keyword is an openjiuwen core extension; the wiring
must therefore signature-check the mounted rail and stay inert on cores
that predate the extension.

Sections:
  A. mapping — attribution -> schema-frozen entry normalization,
  B. mechanics — append, ids, dedup, advisory returns,
  C. fault tolerance — corrupt document, blocked writes,
  D. wiring — the core-extension signature guard and the sink adapter.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from openjiuwen.agent_evolving.signal import ReviewFeedbackAction
from openjiuwen.harness.rails import TeamSkillEvolutionRail

from jiuwenswarm.agents.swarm.review_feedback_journal import (
    ReviewFeedbackEvolutionJournal,
    _stringify_attribution_value,
    attribution_to_evolution_entry,
    journal_review_feedback_attribution,
    supports_attribution_sink,
)

_SCHEMA_KEYS = {
    "source",
    "context",
    "change_section",
    "change_action",
    "change_content",
    "expected_effect",
}


def _attribution(**overrides: Any) -> SimpleNamespace:
    base = {
        "skill_name": "literature_scan",
        "target": "prompt",
        "action": ReviewFeedbackAction.EVOLVE_EXISTING_SKILL,
        "classification": SimpleNamespace(value="correction"),
        "confidence": 0.91,
        "reason": "reviewer flagged stale citation policy",
        "reusable_guidance": "always cite the primary source",
        "feedback_excerpt": "the scan step missed the 2025 survey",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _read_document(root: Path) -> dict[str, Any]:
    return json.loads((root / "workspace" / "evolutions.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# A. mapping
# ---------------------------------------------------------------------------


def test_stringify_attribution_value_renders_deterministically() -> None:
    assert _stringify_attribution_value(None) == ""
    assert _stringify_attribution_value(0.91) == "0.91"
    assert _stringify_attribution_value(ReviewFeedbackAction.EVOLVE_EXISTING_SKILL) == (
        "evolve_existing_skill"
    )
    assert _stringify_attribution_value(["a", None, "b"]) == "a; b"
    assert _stringify_attribution_value(7) == "7"


def test_evolve_action_maps_to_reviewer_feedback_entry() -> None:
    entry = attribution_to_evolution_entry(
        _attribution(), task_id="T-1", review_round=2
    )

    assert set(entry) == _SCHEMA_KEYS
    assert entry["source"] == "reviewer_feedback"
    assert entry["change_action"] == "evolve"
    assert entry["change_section"] == "literature_scan:prompt"
    assert entry["change_content"] == "always cite the primary source"
    assert "Evolve Team Skill literature_scan (prompt)" in entry["expected_effect"]
    assert "task=T-1" in entry["context"]
    assert "round=2" in entry["context"]
    assert "classification=correction" in entry["context"]
    assert "confidence=0.91" in entry["context"]
    assert "skill=literature_scan" in entry["context"]


def test_suggest_new_skill_maps_to_create_entry() -> None:
    entry = attribution_to_evolution_entry(
        _attribution(
            skill_name="",
            target="",
            action=ReviewFeedbackAction.SUGGEST_NEW_SKILL,
            reusable_guidance="",
        )
    )

    assert entry["source"] == "reviewer_feedback_new_skill"
    assert entry["change_action"] == "create"
    assert entry["change_section"] == "team_skills"
    assert "new Team Skill" in entry["expected_effect"]


def test_record_task_failure_maps_to_history_entry() -> None:
    entry = attribution_to_evolution_entry(
        _attribution(action=ReviewFeedbackAction.RECORD_TASK_FAILURE)
    )

    assert entry["source"] == "reviewer_feedback_task_failure"
    assert entry["change_action"] == "record"
    assert entry["change_section"] == "task_failure_history"
    assert "experience replay" in entry["expected_effect"]


def test_skip_or_unknown_action_maps_to_evanescent_entry() -> None:
    entry = attribution_to_evolution_entry(
        _attribution(action=ReviewFeedbackAction.SKIP_UNATTRIBUTED)
    )

    assert entry["source"] == "reviewer_feedback_evanescent"
    assert entry["change_action"] == "record"
    assert entry["change_section"] == "review_feedback_attribution"
    assert entry["expected_effect"].startswith("none")


def test_empty_attribution_shape_yields_generic_record_not_none() -> None:
    entry = attribution_to_evolution_entry(
        SimpleNamespace(), task_id="T-9", review_round=3
    )

    assert set(entry) == _SCHEMA_KEYS
    assert entry["source"] == "reviewer_feedback_evanescent"
    assert "unattributable" in entry["context"]
    assert "task=T-9" in entry["context"]


# ---------------------------------------------------------------------------
# B. mechanics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_appends_with_monotonic_entry_ids(tmp_path: Path) -> None:
    journal = ReviewFeedbackEvolutionJournal(str(tmp_path))

    first = await journal.record(_attribution(), task_id="T-1", review_round=1)
    second = await journal.record(_attribution(), task_id="T-2", review_round=1)

    assert first is not None and first["entry_id"] == "E-001"
    assert second is not None and second["entry_id"] == "E-002"
    document = _read_document(tmp_path)
    assert document["version"] == 1
    assert [e["entry_id"] for e in document["entries"]] == ["E-001", "E-002"]
    assert all(set(e) == _SCHEMA_KEYS | {"entry_id"} for e in document["entries"])


@pytest.mark.asyncio
async def test_entry_id_resumes_from_existing_highest(tmp_path: Path) -> None:
    (tmp_path / "workspace").mkdir(parents=True)
    existing = {
        "version": 1,
        "entries": [
            {"entry_id": "E-007", "source": "s"},
            {"id": "E-003", "source": "legacy-id-key"},  # legacy key still counts
            {"entry_id": "not-an-id", "source": "s"},
            "not-a-dict",
        ],
    }
    (tmp_path / "workspace" / "evolutions.json").write_text(
        json.dumps(existing), encoding="utf-8"
    )
    journal = ReviewFeedbackEvolutionJournal(str(tmp_path))

    entry = await journal.record(_attribution(), task_id="T-1", review_round=1)

    assert entry is not None and entry["entry_id"] == "E-008"


@pytest.mark.asyncio
async def test_evanescent_attribution_deduplicates_per_task_round(
    tmp_path: Path,
) -> None:
    journal = ReviewFeedbackEvolutionJournal(str(tmp_path))
    evanescent = _attribution(action=ReviewFeedbackAction.SKIP_UNATTRIBUTED)

    first = await journal.record(evanescent, task_id="T-1", review_round=1)
    retry = await journal.record(evanescent, task_id="T-1", review_round=1)
    other_round = await journal.record(evanescent, task_id="T-1", review_round=2)

    assert first is not None
    assert retry is None  # duplicate (source, task, round) dropped
    assert other_round is not None  # different round is a new decision
    document = _read_document(tmp_path)
    assert len(document["entries"]) == 2


@pytest.mark.asyncio
async def test_actionable_attribution_always_records(tmp_path: Path) -> None:
    journal = ReviewFeedbackEvolutionJournal(str(tmp_path))

    first = await journal.record(_attribution(), task_id="T-1", review_round=1)
    duplicate = await journal.record(_attribution(), task_id="T-1", review_round=1)

    assert first is not None and duplicate is not None
    assert duplicate["entry_id"] == "E-002"


@pytest.mark.asyncio
async def test_record_without_team_root_is_advisory_none() -> None:
    journal = ReviewFeedbackEvolutionJournal(None)

    assert await journal.record(_attribution(), task_id="T-1", review_round=1) is None


@pytest.mark.asyncio
async def test_record_without_team_root_never_raises_and_touches_nothing(
    tmp_path: Path,
) -> None:
    for root in ("", "   ", None):
        journal = ReviewFeedbackEvolutionJournal(root)  # type: ignore[arg-type]
        out = await journal.record(_attribution(), task_id="T-1", review_round=1)
        assert out is None
    assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------------------
# C. fault tolerance
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_corrupt_document_falls_back_to_fresh_document(tmp_path: Path) -> None:
    (tmp_path / "workspace").mkdir(parents=True)
    (tmp_path / "workspace" / "evolutions.json").write_text("{broken json", encoding="utf-8")
    journal = ReviewFeedbackEvolutionJournal(str(tmp_path))

    entry = await journal.record(_attribution(), task_id="T-1", review_round=1)

    assert entry is not None and entry["entry_id"] == "E-001"
    document = _read_document(tmp_path)
    assert document["version"] == 1
    assert len(document["entries"]) == 1


@pytest.mark.asyncio
async def test_non_dict_document_falls_back_to_fresh_document(tmp_path: Path) -> None:
    (tmp_path / "workspace").mkdir(parents=True)
    (tmp_path / "workspace" / "evolutions.json").write_text(
        json.dumps(["not", "a", "dict"]), encoding="utf-8"
    )
    journal = ReviewFeedbackEvolutionJournal(str(tmp_path))

    entry = await journal.record(_attribution(), task_id="T-1", review_round=1)

    assert entry is not None
    assert _read_document(tmp_path)["version"] == 1


@pytest.mark.asyncio
async def test_write_failure_is_advisory_and_returns_none(tmp_path: Path) -> None:
    # A directory parked at the journal path blocks the replace(): the read
    # sees no file, the write fails — record() must swallow it and report None.
    (tmp_path / "workspace" / "evolutions.json").mkdir(parents=True)
    journal = ReviewFeedbackEvolutionJournal(str(tmp_path))

    assert await journal.record(_attribution(), task_id="T-1", review_round=1) is None


# ---------------------------------------------------------------------------
# D. wiring
# ---------------------------------------------------------------------------


def test_supports_attribution_sink_accepts_extension_signature() -> None:
    class NewCoreRail:
        def configure_review_feedback_evolution(
            self, *, session_id: str, team_id: str, attribution_sink: Any
        ) -> None:
            del session_id, team_id, attribution_sink

    assert supports_attribution_sink(NewCoreRail()) is True


def test_supports_attribution_sink_rejects_pinned_core_signature() -> None:
    class PinnedCoreRail:
        def configure_review_feedback_evolution(
            self, *, session_id: str, team_id: str, min_confidence: float = 0.7
        ) -> None:
            del session_id, team_id, min_confidence

    assert supports_attribution_sink(PinnedCoreRail()) is False


def test_supports_attribution_sink_handles_missing_or_opaque_callables() -> None:
    assert supports_attribution_sink(object()) is False
    # Builtins have no introspectable signature (ValueError path).
    assert (
        supports_attribution_sink(
            type("X", (), {"configure_review_feedback_evolution": len})()
        )
        is False
    )


def test_pinned_core_rail_class_has_no_attribution_sink() -> None:
    # Encodes the pinned openjiuwen core contract (uv.lock rev): the journal
    # stays inert until the core gains the sink. Flips intentionally when the
    # core pin is bumped with the extension.
    assert supports_attribution_sink(TeamSkillEvolutionRail) is False


@pytest.mark.asyncio
async def test_sink_adapter_round_trips_into_journal(tmp_path: Path) -> None:
    journal = ReviewFeedbackEvolutionJournal(str(tmp_path))

    await journal_review_feedback_attribution(journal, _attribution(), "T-1", 2)

    document = _read_document(tmp_path)
    assert len(document["entries"]) == 1
    entry = document["entries"][0]
    assert entry["entry_id"] == "E-001"
    assert entry["change_action"] == "evolve"
    assert "task=T-1" in entry["context"]
    assert "round=2" in entry["context"]


@pytest.mark.asyncio
async def test_sink_adapter_survives_coordinator_style_str_types(
    tmp_path: Path,
) -> None:
    journal = ReviewFeedbackEvolutionJournal(str(tmp_path))

    await journal_review_feedback_attribution(journal, _attribution(), None, None)  # type: ignore[arg-type]

    entry = _read_document(tmp_path)["entries"][0]
    assert "task=unknown" in entry["context"]
    assert "round=0" in entry["context"]
