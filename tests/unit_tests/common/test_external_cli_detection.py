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


def test_disabled_modes_record_the_mode_at_disable_time():
    raw = {"modes": {"team": {"jiuwen_team": {"external_cli_agents": []}}}}
    # 本地路径模式下禁用：记录自定义模式，重开时恢复本地路径
    disable_local = {
        "external_cli_agent_claude_enabled": "false",
        "external_cli_agent_claude_use_builtin": "false",
        "external_cli_agent_claude_cli_path": "/local/claude",
    }
    assert handlers.external_cli_disabled_modes_from_switches(raw, disable_local, []) == {"claude": "false"}
    # 内置模式下禁用：记录内置模式，重开时恢复内置而不是被历史回退拉回
    # 旧的本地路径（!10561 检视）
    disable_builtin = {
        "external_cli_agent_claude_enabled": "false",
        "external_cli_agent_claude_use_builtin": "true",
        "external_cli_agent_claude_cli_path": "",
    }
    assert handlers.external_cli_disabled_modes_from_switches(raw, disable_builtin, []) == {"claude": "true"}
    # 重新启用且 agent 真正落盘进列表：记录清除（列表条目重新承载模式）
    enable_builtin = {
        "external_cli_agent_claude_enabled": "true",
        "external_cli_agent_claude_use_builtin": "true",
        "external_cli_agent_claude_cli_path": "",
    }
    assert handlers.external_cli_disabled_modes_from_switches(
        raw, enable_builtin, [{"cli_agent": "claude"}]
    ) == {}


def test_disabled_modes_refresh_when_install_deferral_keeps_the_agent_out_of_the_list():
    """一键安装触发依赖准备时 agent 被剔除出列表，但 keys 仍是启用态——
    记录必须按本次保存的模式刷新而不是被清除，否则重新读取后卡片从内置
    退回自定义空态（!10561 第二轮检视）。"""
    raw = {"modes": {"team": {"jiuwen_team": {"external_cli_agents": []}}}}
    enable_builtin = {
        "external_cli_agent_claude_enabled": "true",
        "external_cli_agent_claude_use_builtin": "true",
        "external_cli_agent_claude_cli_path": "",
    }
    # 列表为空（依赖安装待完成把 agent 剔除）：记录按本次模式刷新
    assert handlers.external_cli_disabled_modes_from_switches(raw, enable_builtin, []) == {"claude": "true"}


def test_disabled_modes_keep_untouched_agents_and_survive_history_only_saves():
    raw = {"modes": {"team": {"jiuwen_team": {
        "external_cli_disabled_modes": {"claude": "true", "codex": "false"},
    }}}}
    # 只写路径历史的清理保存：记录原样保留
    history_only = {"external_cli_agent_codex_path_histories": "[]"}
    assert handlers.external_cli_disabled_modes_from_switches(raw, history_only, []) == {
        "claude": "true",
        "codex": "false",
    }
    # 未触碰的 agent 保留既有记录，触碰的 agent 按本次保存刷新
    touch_codex = {"external_cli_agent_codex_enabled": "false"}
    assert handlers.external_cli_disabled_modes_from_switches(raw, touch_codex, []) == {
        "claude": "true",
        "codex": "false",
    }
