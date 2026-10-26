# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the per-session diagnosis record store."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from jiuwenswarm.diagnosis.models import DiagnosisStage, DiagnosisStatus, StageStatus
from jiuwenswarm.diagnosis.store import (
    DiagnosisStore,
    is_valid_diagnosis_id,
    new_diagnosis_id,
)
from tests.unit_tests.diagnosis.diagnosis_fixtures import SESSION_ID, make_record


def test_new_diagnosis_id_is_filename_safe() -> None:
    value = new_diagnosis_id()

    assert is_valid_diagnosis_id(value)
    assert not is_valid_diagnosis_id("../escape")
    assert not is_valid_diagnosis_id("")


def test_save_and_load_round_trip(tmp_path: Path) -> None:
    store = DiagnosisStore(tmp_path)
    record = make_record()

    store.save(record)
    loaded = store.load(SESSION_ID, record.diagnosis_id)

    assert loaded is not None
    assert loaded.diagnosis_id == record.diagnosis_id
    assert loaded.input.mode_profile == record.input.mode_profile
    assert not list((tmp_path / SESSION_ID / "diagnosis").glob("*.tmp"))


def test_load_reads_records_written_before_signal_and_task_fields(tmp_path: Path) -> None:
    import json

    store = DiagnosisStore(tmp_path)
    record = make_record()
    store.save(record)
    path = next((tmp_path / SESSION_ID / "diagnosis").glob("*.json"))
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.pop("task_description", None)
    for cause in payload.get("root_causes", []):
        cause.pop("signal", None)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    loaded = store.load(SESSION_ID, record.diagnosis_id)

    assert loaded is not None
    assert loaded.task_description is None
    assert loaded.reward is None
    assert all(cause.signal is None for cause in loaded.root_causes)


def test_load_retries_a_read_that_overlaps_an_atomic_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = DiagnosisStore(tmp_path)
    record = make_record()
    store.save(record)
    original = Path.read_text
    failures = {"left": 2}

    def flaky(self: Path, *args: object, **kwargs: object) -> str:
        if self.name == f"{record.diagnosis_id}.json" and failures["left"] > 0:
            failures["left"] -= 1
            raise PermissionError("file is being replaced")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", flaky)

    assert store.load(SESSION_ID, record.diagnosis_id) is not None
    assert failures["left"] == 0


def test_save_retries_a_replace_blocked_by_an_open_reader(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = DiagnosisStore(tmp_path)
    record = make_record()
    original = os.replace
    failures = {"left": 3}

    def flaky(source: object, destination: object) -> None:
        if failures["left"] > 0:
            failures["left"] -= 1
            raise PermissionError("file is open")
        original(source, destination)

    monkeypatch.setattr(os, "replace", flaky)

    store.save(record)

    assert failures["left"] == 0
    assert store.load(SESSION_ID, record.diagnosis_id) is not None


def test_load_gives_up_on_a_persistently_unreadable_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = DiagnosisStore(tmp_path)
    record = make_record()
    store.save(record)

    def locked(self: Path, *args: object, **kwargs: object) -> str:
        raise PermissionError("locked")

    monkeypatch.setattr(Path, "read_text", locked)

    assert store.load(SESSION_ID, record.diagnosis_id) is None


def test_load_returns_none_for_missing_or_corrupt_records(tmp_path: Path) -> None:
    store = DiagnosisStore(tmp_path)
    record = make_record()
    store.save(record)
    (tmp_path / SESSION_ID / "diagnosis" / f"{record.diagnosis_id}.json").write_text("{broken", encoding="utf-8")

    assert store.load(SESSION_ID, record.diagnosis_id) is None
    assert store.load(SESSION_ID, "dg_ffffffffffffffff") is None
    assert store.load(SESSION_ID, "../../etc/passwd") is None


def test_ordinals_increase_and_are_never_reused_after_delete(tmp_path: Path) -> None:
    store = DiagnosisStore(tmp_path)

    first = store.allocate_ordinal(SESSION_ID)
    store.save(make_record(ordinal=first, diagnosis_id="dg_00000000000000a1"))
    second = store.allocate_ordinal(SESSION_ID)
    assert store.delete(SESSION_ID, "dg_00000000000000a1") is True
    third = store.allocate_ordinal(SESSION_ID)

    assert (first, second, third) == (1, 2, 3)


def test_ordinal_recovers_from_a_missing_index(tmp_path: Path) -> None:
    store = DiagnosisStore(tmp_path)
    store.save(make_record(ordinal=7, diagnosis_id="dg_00000000000000b1"))

    assert store.allocate_ordinal(SESSION_ID) == 8


def test_list_summaries_is_newest_first(tmp_path: Path) -> None:
    store = DiagnosisStore(tmp_path)
    store.save(make_record(ordinal=1, diagnosis_id="dg_00000000000000c1"))
    store.save(make_record(ordinal=3, diagnosis_id="dg_00000000000000c3"))
    store.save(make_record(ordinal=2, diagnosis_id="dg_00000000000000c2"))

    assert [item.ordinal for item in store.list_summaries(SESSION_ID)] == [3, 2, 1]
    assert store.list_summaries("session-without-records") == []


def test_delete_reports_whether_a_record_was_removed(tmp_path: Path) -> None:
    store = DiagnosisStore(tmp_path)
    store.save(make_record())

    assert store.delete(SESSION_ID, "dg_0123456789abcdef") is True
    assert store.delete(SESSION_ID, "dg_0123456789abcdef") is False
    assert store.delete(SESSION_ID, "bad-id") is False


def test_invalid_session_id_is_rejected(tmp_path: Path) -> None:
    store = DiagnosisStore(tmp_path)

    with pytest.raises(ValueError):
        store.allocate_ordinal("../outside")


def test_mark_running_interrupted_rewrites_only_running_records(tmp_path: Path) -> None:
    store = DiagnosisStore(tmp_path)
    running = make_record(ordinal=1, diagnosis_id="dg_00000000000000d1", status=DiagnosisStatus.RUNNING)
    running.stages.append(DiagnosisStage(stage_id="evidence", status=StageStatus.RUNNING))
    done = make_record(ordinal=2, diagnosis_id="dg_00000000000000d2")
    store.save(running)
    store.save(done)

    changed = store.mark_running_interrupted()

    reloaded = store.load(SESSION_ID, running.diagnosis_id)
    assert changed == 1
    assert reloaded is not None
    assert reloaded.status is DiagnosisStatus.INTERRUPTED
    assert reloaded.finished_at is not None
    assert reloaded.stages[0].status is StageStatus.FAILED
    untouched = store.load(SESSION_ID, done.diagnosis_id)
    assert untouched is not None and untouched.status is DiagnosisStatus.COMPLETED
