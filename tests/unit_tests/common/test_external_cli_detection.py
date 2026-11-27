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
    # reference_version 桩为非空低版本：SDK 门控要求其非空（!10678 讨论），
    # 且低于探测版本不触发 warning，保持身份校验断言的确定性。
    monkeypatch.setattr(handlers, "external_cli_reference_version", lambda _: "0.1.0")
    # SDK 门控桩：清单模块视为齐备，结果不依赖运行环境是否装了 SDK
    # （!10678 第二轮检视）。
    monkeypatch.setattr(handlers, "missing_external_cli_sdk_modules", lambda _agent: [])
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
    # reference_version 桩为非空低版本：SDK 门控要求其非空（!10678 讨论），
    # 且低于探测版本不触发 warning，保持身份校验断言的确定性。
    monkeypatch.setattr(handlers, "external_cli_reference_version", lambda _: "0.1.0")
    # SDK 门控桩：清单模块视为齐备，结果不依赖运行环境是否装了 SDK
    # （!10678 第二轮检视）。
    monkeypatch.setattr(handlers, "missing_external_cli_sdk_modules", lambda _agent: [])
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


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_manifest_unavailable_degrades_to_missing(tmp_path, monkeypatch, agent):
    """清单不可读是环境级错误：探测降级为 missing，不向接口抛 RuntimeError（!10678 检视）。"""
    executable = tmp_path / "real-cli"
    executable.touch()
    monkeypatch.setattr(handlers, "external_cli_reference_version", lambda _: "0.1.0")

    def _unreadable_manifest(_agent):
        raise RuntimeError("failed to load external CLI runtime manifest: broken")

    monkeypatch.setattr(handlers, "missing_external_cli_sdk_modules", _unreadable_manifest)
    monkeypatch.setattr(handlers, "run_external_cli_version_command", lambda *_: ("2.1.999", ""))
    result = handlers.detect_external_cli_agent(agent, str(executable))
    assert result["status"] == "missing"
    assert result["reason"] == "builtin_not_installed"
    assert "manifest unavailable" in result["message"]


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_saving_with_unreadable_manifest_reports_validation_error(tmp_path, monkeypatch, agent):
    """保存链路对清单不可读得到常规 ValueError 校验失败，而非未处理的 RuntimeError。"""
    executable = tmp_path / "real-cli"
    executable.touch()
    monkeypatch.setattr(handlers, "external_cli_reference_version", lambda _: "0.1.0")

    def _unreadable_manifest(_agent):
        raise RuntimeError("failed to load external CLI runtime manifest: broken")

    monkeypatch.setattr(handlers, "missing_external_cli_sdk_modules", _unreadable_manifest)
    monkeypatch.setattr(handlers, "run_external_cli_version_command", lambda *_: ("2.1.999", ""))
    raw = {"modes": {"team": {"jiuwen_team": {"external_cli_agents": []}}}}
    params = {
        f"external_cli_agent_{agent}_enabled": "true",
        f"external_cli_agent_{agent}_use_builtin": "false",
        f"external_cli_agent_{agent}_cli_path": str(executable),
    }
    with pytest.raises(ValueError, match="cli_path is not available"):
        handlers.external_cli_agents_from_switches(raw, params)
    assert raw["modes"]["team"]["jiuwen_team"]["external_cli_agents"] == []
