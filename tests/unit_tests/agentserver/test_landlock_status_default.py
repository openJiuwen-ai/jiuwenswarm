"""The command.sandbox capability view uses the same deployment default."""

import pytest

from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer


@pytest.mark.parametrize("contents", [None, "{}", "landlock: {}", "landlock: [invalid yaml"])
def test_missing_or_invalid_policy_reports_mandatory_default(tmp_path, contents):
    path = tmp_path / "policy.yaml"
    if contents is not None:
        path.write_text(contents, encoding="utf-8")
    assert AgentWebSocketServer._read_landlock_compatibility(path) == "hard_requirement"
    assert AgentWebSocketServer._read_landlock_compatibility(None) == "hard_requirement"


@pytest.mark.parametrize("mode", ["hard_requirement", "best_effort", "disabled"])
def test_explicit_deployment_choice_is_reported(tmp_path, mode):
    path = tmp_path / "policy.yaml"
    path.write_text(f"landlock:\n  compatibility: {mode}\n", encoding="utf-8")
    assert AgentWebSocketServer._read_landlock_compatibility(path) == mode
