"""Source-contract tests; use external pinned checkouts, never vendor datasets."""
import json
import os
from pathlib import Path

import pytest

from jiuwenswarm.common.duplex_public_benchmark import (
    agentradio_manifest, interruptbench_manifest, load_official_interrupt,
    resolve_official_trigger,
)


@pytest.fixture
def interrupt_repo():
    source = os.environ.get("JIUWEN_INTERRUPT_BENCH_ROOT")
    if not source:
        pytest.skip("set JIUWEN_INTERRUPT_BENCH_ROOT to the pinned official checkout")
    return Path(source)


def test_manifest_preserves_real_agentradio_tasks_and_rubrics():
    source = os.environ.get("JIUWEN_AGENTRADIO_ROOT")
    if not source:
        pytest.skip("set JIUWEN_AGENTRADIO_ROOT to the pinned official checkout")
    result = agentradio_manifest(Path(source))
    assert len(result["tasks"]) == 124
    assert sum(task["rubric_count"] for task in result["tasks"]) == 1306
    assert all(task["metadata"]["base_commit"] for task in result["tasks"])
    assert all("docker_image" in task["environment"] for task in result["tasks"])


def test_all_official_interrupt_suites_are_accounted_for(interrupt_repo):
    result = interruptbench_manifest(interrupt_repo)
    assert set(result["suites"]) == {"1update", "2update", "2modification", "1retraction", "2retraction", "3mixed"}
    for suite in result["suites"].values():
        assert suite["task_ids"]
        assert suite["update_count"] >= len(suite["task_ids"])


@pytest.mark.parametrize("spec,count,expected", [
    ({"interrupt_at_pct": "20%"}, 7, 1),
    ({"interrupt_at_pct": 20}, 7, 1),
    ({"interrupt_at_pct": 0.6}, 7, 4),
    ({"interrupt_at_action": 30}, 7, 7),
    ({"interrupt_at_action": -1}, 7, 0),
])
def test_pinned_upstream_trigger_semantics(interrupt_repo, spec, count, expected):
    assert resolve_official_trigger(interrupt_repo, spec, count) == expected


def test_import_rejects_changed_message_task_and_initial_intent(interrupt_repo, tmp_path):
    config = interrupt_repo / "Eval/config_files/wa/test_webarena_lite_transformed_1update/0.json"
    spec = interrupt_repo / "Eval/interrupt_config/process/interrupt_spec_1update_opus_02.json"
    trajectory = tmp_path / "trajectory.json"
    # Structural fixture only; never reported as an executed WebArena trajectory.
    trajectory.write_text(json.dumps({"task_id": 0, "actions": [{"action_type": 0}] * 7}))
    kwargs = {"suite": "1update", "task_id": "0", "config_file": config,
              "spec_file": spec, "trajectory_file": trajectory}
    case = load_official_interrupt(interrupt_repo, **kwargs)
    assert case.initial_intent == "What are the top-3 best-selling products?"
    assert case.update == "I meant for Jan 2023 specifically"
    assert case.action_boundary == 1
    assert "reference_answers" not in case.__dict__
    changed_spec = tmp_path / "spec.json"
    changed_spec.write_text(json.dumps({"tasks": {"0": {"update_intent": "invented"}}}))
    with pytest.raises(ValueError, match="unchanged official update"):
        load_official_interrupt(interrupt_repo, **{**kwargs, "spec_file": changed_spec})
    trajectory.write_text(json.dumps({"task_id": 1, "actions": []}))
    with pytest.raises(ValueError, match="task_id/actions mismatch"):
        load_official_interrupt(interrupt_repo, **kwargs)
    trajectory.write_text(json.dumps({"task_id": 0, "actions": []}))
    changed_config = tmp_path / "config.json"
    changed_config.write_text(json.dumps({"task_id": 0, "intent": "Different task"}))
    with pytest.raises(ValueError, match="initial-stage"):
        load_official_interrupt(interrupt_repo, **{**kwargs, "config_file": changed_config})
