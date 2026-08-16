# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""No-model tests for the generic staged task lifecycle rail."""

from __future__ import annotations

import json
from types import SimpleNamespace

from openjiuwen.core.single_agent.rail.base import (
    AgentCallbackContext,
    RunContext,
)

from jiuwenswarm.agents.harness.common.rails.staged_task_lifecycle_rail import (
    StagedTaskLifecycleRail,
)
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
)
from jiuwenswarm.common import config as swarm_config


class _Session:
    def __init__(self, session_id: str) -> None:
        self._session_id = session_id
        self._state: dict = {}

    def get_session_id(self) -> str:
        return self._session_id

    def get_state(self, key=None):
        return self._state.get(key)

    def update_state(self, data: dict) -> None:
        self._state.update(data)


def _context(
    session: _Session,
    *,
    inputs=None,
    extra: dict | None = None,
    exception: Exception | None = None,
    retry_attempt: int = 0,
) -> AgentCallbackContext:
    return AgentCallbackContext(
        agent=object(),
        inputs=inputs if inputs is not None else {},
        session=session,
        extra=extra or {},
        exception=exception,
        retry_attempt=retry_attempt,
    )


async def _run_success(
    rail: StagedTaskLifecycleRail, session: _Session, stage_data: dict
) -> None:
    await rail.before_invoke(
        _context(session, extra={"staged_task": {"task_id": "task-1"}})
    )
    inputs = SimpleNamespace(
        iteration=1,
        result={"result_type": "success"},
        run_context={"staged_task": stage_data},
    )
    await rail.before_task_iteration(_context(session, inputs=inputs))
    await rail.after_task_iteration(_context(session, inputs=inputs))
    await rail.after_invoke(_context(session))


def _adapter(monkeypatch):
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._instance_overrides = {}
    adapter._filesystem_rail = None
    adapter._vision_model_config = None
    adapter._config_cache = {}
    adapter._model = None
    monkeypatch.setattr(
        adapter, "_instantiate_rails", lambda rail_infos, config_base: rail_infos
    )
    return adapter


def test_disabled_by_default(monkeypatch) -> None:
    adapter = _adapter(monkeypatch)
    rail_infos = adapter._build_agent_rails({}, {})
    assert not any(
        info.attr_name == "_staged_task_lifecycle_rail" for info in rail_infos
    )


def test_enabled_creates_exactly_one(monkeypatch) -> None:
    adapter = _adapter(monkeypatch)
    rail_infos = adapter._build_agent_rails(
        {"staged_task_lifecycle": True}, {}
    )
    assert sum(
        info.attr_name == "_staged_task_lifecycle_rail" for info in rail_infos
    ) == 1


async def test_success_snapshot_and_artifacts() -> None:
    rail = StagedTaskLifecycleRail()
    session = _Session("session-A")
    stage_data = {
        "stage_id": "alpha",
        "stage_name": "Generic Alpha",
        "artifact_refs": [{"uri": "file:///tmp/report.md", "name": "report.md"}],
        "checkpoint_ref": "checkpoint-1",
        "metadata": {"owner": "agent"},
    }
    await _run_success(rail, session, stage_data)
    snapshot = rail.get_snapshot(session)
    assert snapshot["status"] == "COMPLETED"
    assert snapshot["task"]["session_id"] == "session-A"
    assert snapshot["stages"][0]["stage_status"] == "COMPLETED"
    artifact = snapshot["stages"][0]["artifact_refs"][0]
    assert artifact["uri"] == stage_data["artifact_refs"][0]["uri"]
    assert artifact["name"] == stage_data["artifact_refs"][0]["name"]
    assert artifact["artifact_id"].startswith("artifact-")
    assert artifact["evidence_id"] == artifact["artifact_id"]
    assert snapshot["stages"][0]["checkpoint_ref"] == "checkpoint-1"
    json.dumps(snapshot, allow_nan=False)


async def test_failure_is_sanitized_and_serializable() -> None:
    rail = StagedTaskLifecycleRail()
    session = _Session("session-failure")
    await rail.before_invoke(_context(session))
    inputs = SimpleNamespace(
        iteration=1,
        result={"result_type": "error", "error": "failed"},
        run_context={"staged_task": {"stage_id": "failure"}},
    )
    await rail.before_task_iteration(_context(session, inputs=inputs))
    await rail.after_task_iteration(
        _context(
            session,
            inputs=inputs,
            exception=RuntimeError(
                "Authorization: Bearer test-secret-do-not-use "
                "api_key=test-api-key"
            ),
            retry_attempt=2,
        )
    )
    snapshot = rail.get_snapshot(session)
    failure = snapshot["stages"][0]["failure"]
    assert snapshot["status"] == "FAILED"
    assert failure["type"] == "RuntimeError"
    assert failure["retry_attempt"] == 2
    assert "test-secret-do-not-use" not in failure["message"]
    assert "test-api-key" not in failure["message"]
    json.dumps(snapshot, allow_nan=False)


async def test_fallback_id_is_deterministic_and_sessions_are_isolated() -> None:
    rail = StagedTaskLifecycleRail()
    session_a = _Session("session-A")
    session_b = _Session("session-B")
    for session in (session_a, session_a, session_b):
        await rail.before_task_iteration(
            _context(
                session,
                inputs=SimpleNamespace(iteration=3, run_context={}),
            )
        )
    snapshot_a = rail.get_snapshot(session_a)
    snapshot_b = rail.get_snapshot(session_b)
    assert [stage["stage_id"] for stage in snapshot_a["stages"]] == ["iteration-3"]
    assert [stage["stage_id"] for stage in snapshot_b["stages"]] == ["iteration-3"]
    assert snapshot_a["task"]["session_id"] != snapshot_b["task"]["session_id"]


async def test_artifact_refs_do_not_read_files() -> None:
    rail = StagedTaskLifecycleRail()
    session = _Session("session-artifact")
    inputs = SimpleNamespace(
        iteration=1,
        run_context={
            "staged_task": {
                "stage_id": "artifact-stage",
                "artifact_refs": [
                    {"path": "/path/that/is/not/read", "hash": "sha256:test"}
                ],
            }
        },
    )
    await rail.before_task_iteration(_context(session, inputs=inputs))
    snapshot = rail.get_snapshot(session)
    assert snapshot["stages"][0]["artifact_refs"][0]["path"] == (
        "/path/that/is/not/read"
    )


async def test_iteration_completion_keeps_task_running_until_invoke() -> None:
    rail = StagedTaskLifecycleRail()
    session = _Session("session-running")
    await rail.before_invoke(
        _context(session, extra={"staged_task": {"task_id": "task-running"}})
    )
    inputs = SimpleNamespace(
        iteration=1,
        result={"result_type": "success"},
        run_context={"staged_task": {"stage_id": "stage-1"}},
    )
    await rail.before_task_iteration(_context(session, inputs=inputs))
    await rail.after_task_iteration(_context(session, inputs=inputs))

    snapshot = rail.get_snapshot(session)
    assert snapshot["status"] == "RUNNING"
    assert snapshot["task"]["status"] == "RUNNING"
    assert snapshot["stages"][0]["stage_status"] == "COMPLETED"
    assert snapshot["iterations"][0]["status"] == "COMPLETED"

    await rail.after_invoke(_context(session))
    snapshot = rail.get_snapshot(session)
    assert snapshot["status"] == "COMPLETED"
    assert snapshot["task"]["status"] == "COMPLETED"


async def test_iteration_failure_snapshot_consistency() -> None:
    rail = StagedTaskLifecycleRail()
    session = _Session("session-failure-consistent")
    await rail.before_invoke(_context(session))
    inputs = SimpleNamespace(
        iteration=1,
        result={"result_type": "error", "error": "iteration failed"},
        run_context={"staged_task": {"stage_id": "failed-stage"}},
    )
    await rail.before_task_iteration(_context(session, inputs=inputs))
    await rail.after_task_iteration(
        _context(
            session,
            inputs=inputs,
            exception=RuntimeError("iteration failed"),
        )
    )

    snapshot = rail.get_snapshot(session)
    assert snapshot["status"] == "FAILED"
    assert snapshot["task"]["status"] == "FAILED"
    assert snapshot["task"]["failure"] is not None
    assert snapshot["stages"][0]["stage_status"] == "FAILED"
    assert snapshot["iterations"][0]["status"] == "FAILED"


async def test_task_id_change_resets_state_in_same_session() -> None:
    rail = StagedTaskLifecycleRail()
    session = _Session("session-task-switch")
    await rail.before_invoke(
        _context(session, extra={"staged_task": {"task_id": "task-A"}})
    )
    inputs = SimpleNamespace(
        iteration=1,
        run_context={"staged_task": {"stage_id": "stage-A"}},
    )
    await rail.before_task_iteration(_context(session, inputs=inputs))

    await rail.before_invoke(
        _context(session, extra={"staged_task": {"task_id": "task-B"}})
    )
    snapshot = rail.get_snapshot(session)
    assert snapshot["task"]["task_id"] == "task-B"
    assert snapshot["stages"] == []
    assert snapshot["iterations"] == []
    assert snapshot["current_stage"] is None


def test_strict_config_types(monkeypatch) -> None:
    cases = [
        (True, 1),
        (False, 0),
        ("true", 0),
        ("false", 0),
        (1, 0),
        (0, 0),
        ([], 0),
        ({}, 0),
        ({"enabled": True}, 1),
        ({"enabled": False}, 0),
        ({"enabled": "false"}, 0),
    ]
    for raw, expected_count in cases:
        adapter = _adapter(monkeypatch)
        rail_infos = adapter._build_agent_rails(
            {"staged_task_lifecycle": raw}, {}
        )
        assert sum(
            info.attr_name == "_staged_task_lifecycle_rail"
            for info in rail_infos
        ) == expected_count


async def test_generic_metadata_is_ignored_without_staged_namespace() -> None:
    rail = StagedTaskLifecycleRail()
    session = _Session("session-generic-metadata")
    await rail.before_invoke(
        _context(session, extra={"metadata": {"owner": "abc"}})
    )
    inputs = SimpleNamespace(
        iteration=1,
        run_context={"metadata": {"owner": "abc"}},
    )
    await rail.before_task_iteration(_context(session, inputs=inputs))

    snapshot = rail.get_snapshot(session)
    assert snapshot["task"]["metadata"] == {}
    assert snapshot["stages"][0]["metadata"] == {}


async def test_explicit_staged_namespace_precedes_direct_metadata() -> None:
    rail = StagedTaskLifecycleRail()
    session = _Session("session-namespace-priority")
    inputs = SimpleNamespace(
        iteration=1,
        run_context={
            "staged_task": {"stage_id": "explicit-stage"},
        },
    )
    await rail.before_task_iteration(
        _context(
            session,
            inputs=inputs,
            extra={"task_id": "generic-task", "stage_id": "generic-stage"},
        )
    )

    snapshot = rail.get_snapshot(session)
    assert snapshot["task"]["task_id"] == "session-namespace-priority"
    assert snapshot["stages"][0]["stage_id"] == "explicit-stage"


async def test_structured_run_context_extra_is_supported() -> None:
    rail = StagedTaskLifecycleRail()
    session = _Session("session-structured-context")
    inputs = SimpleNamespace(
        iteration=1,
        run_context=RunContext(
            extra={"staged_task": {"stage_id": "structured-stage"}}
        ),
    )
    await rail.before_task_iteration(_context(session, inputs=inputs))

    snapshot = rail.get_snapshot(session)
    assert snapshot["stages"][0]["stage_id"] == "structured-stage"


async def test_strict_json_converts_non_finite_floats() -> None:
    rail = StagedTaskLifecycleRail()
    session = _Session("session-json")
    inputs = SimpleNamespace(
        iteration=1,
        run_context={
            "staged_task": {
                "stage_id": "json-stage",
                "metadata": {
                    "nan": float("nan"),
                    "positive_inf": float("inf"),
                    "negative_inf": float("-inf"),
                },
            }
        },
    )
    await rail.before_task_iteration(_context(session, inputs=inputs))
    snapshot = rail.get_snapshot(session)

    json.dumps(snapshot, allow_nan=False)
    assert snapshot["stages"][0]["metadata"] == {
        "nan": "nan",
        "positive_inf": "inf",
        "negative_inf": "-inf",
    }


async def test_malformed_metadata_does_not_raise() -> None:
    rail = StagedTaskLifecycleRail()
    session = _Session("session-malformed-metadata")
    await rail.before_invoke(
        _context(
            session,
            extra={
                "staged_task": {
                    "task_id": "task-malformed",
                    "metadata": "not-a-dict",
                }
            },
        )
    )
    inputs = SimpleNamespace(
        iteration=1,
        run_context={
            "staged_task": {
                "stage_id": "malformed-stage",
                "metadata": "not-a-dict",
            }
        },
    )
    await rail.before_task_iteration(_context(session, inputs=inputs))
    snapshot = rail.get_snapshot(session)

    assert snapshot["task"]["metadata"] == {}
    assert snapshot["stages"][0]["metadata"] == {}


async def test_old_artifact_ref_remains_compatible() -> None:
    rail = StagedTaskLifecycleRail()
    session = _Session("session-old-artifact")

    await _run_success(
        rail,
        session,
        {"stage_id": "legacy-stage", "artifact_refs": ["file://legacy.txt"]},
    )

    artifact = rail.get_snapshot(session)["stages"][0]["artifact_refs"][0]
    assert artifact["uri"] == "file://legacy.txt"
    assert artifact["artifact_id"].startswith("artifact-")
    assert artifact["evidence_id"] == artifact["artifact_id"]


async def test_new_artifact_provenance_ref_is_normalized_in_snapshot() -> None:
    rail = StagedTaskLifecycleRail()
    session = _Session("session-new-artifact")

    await _run_success(
        rail,
        session,
        {
            "stage_id": "provenance-stage",
            "artifact_refs": [
                {
                    "uri": "file://result.csv",
                    "artifact_provenance": {
                        "artifact_id": "artifact-result",
                        "evidence_id": "evidence-result",
                        "source": {"type": "generated", "identifier": "run-1"},
                    },
                }
            ],
        },
    )

    artifact = rail.get_snapshot(session)["stages"][0]["artifact_refs"][0]
    assert artifact["artifact_id"] == "artifact-result"
    assert artifact["evidence_id"] == "evidence-result"
    assert artifact["source"]["identifier"] == "run-1"


def test_real_config_loader_path(monkeypatch, tmp_path) -> None:
    cases = [
        ("enabled.yaml", "react:\n  staged_task_lifecycle: true\n", 1),
        ("default.yaml", "react: {}\n", 0),
    ]
    for filename, yaml_text, expected_count in cases:
        config_path = tmp_path / filename
        config_path.write_text(yaml_text, encoding="utf-8")
        monkeypatch.setattr(
            swarm_config,
            "get_config_file",
            lambda config_path=config_path: config_path,
        )
        loaded = swarm_config.get_config()
        react_config = loaded.get("react", {})
        adapter = _adapter(monkeypatch)
        rail_infos = adapter._build_agent_rails(react_config, loaded)
        assert sum(
            info.attr_name == "_staged_task_lifecycle_rail"
            for info in rail_infos
        ) == expected_count
