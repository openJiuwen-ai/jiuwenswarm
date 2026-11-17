"""CLI detection must verify the selected program, not just its version number."""

import pytest

from jiuwenswarm.common.config_panel import config_set_handlers as handlers


@pytest.mark.parametrize(
    "agent,output,expected",
    [
        ("claude", "codex-cli 0.114.0", "unavailable"),
        ("codex", "2.1.999 (Claude Code)", "unavailable"),
        ("claude", "2.1.999", "unavailable"),
        ("claude", "2.1.999 (Claude Code)", "ok"),
        ("codex", "codex-cli 0.114.0", "ok"),
    ],
)
def test_detect_checks_program_identity(tmp_path, monkeypatch, agent, output, expected):
    executable = tmp_path / "renamed-program"
    executable.touch()
    monkeypatch.setattr(handlers, "external_cli_reference_version", lambda _: "")
    monkeypatch.setattr(handlers, "run_external_cli_version_command", lambda *_: (output, ""))
    result = handlers.detect_external_cli_agent(agent, str(executable))
    assert result["status"] == expected
    assert result["path"] == str(executable)
    if expected == "unavailable":
        assert agent in result["message"]


@pytest.mark.parametrize("agent,output", [("claude", "codex-cli 0.114.0"), ("codex", "2.1.999 (Claude Code)")])
def test_saving_rejects_another_agents_executable(tmp_path, monkeypatch, agent, output):
    executable = tmp_path / "wrong-program"
    executable.touch()
    monkeypatch.setattr(handlers, "external_cli_reference_version", lambda _: "")
    monkeypatch.setattr(handlers, "run_external_cli_version_command", lambda *_: (output, ""))
    raw = {"modes": {"team": {"jiuwen_team": {"external_cli_agents": []}}}}
    params = {
        f"external_cli_agent_{agent}_enabled": "true",
        f"external_cli_agent_{agent}_use_builtin": "false",
        f"external_cli_agent_{agent}_cli_path": str(executable),
    }
    with pytest.raises(ValueError, match="cli_path is not available"):
        handlers.external_cli_agents_from_switches(raw, params)
    assert raw["modes"]["team"]["jiuwen_team"]["external_cli_agents"] == []
