# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Unit tests for the unified coding engine abstraction."""

from __future__ import annotations

from pathlib import Path

import pytest

from jiuwenavatar.server.runtime.coding import (
    CODING_ENGINE_CLAUDE_CODE,
    CODING_ENGINE_CODEX,
    CODING_ENGINE_JIUWEN,
    get_coding_engine,
    list_engine_kinds,
)
from jiuwenavatar.server.runtime.coding.engines import (
    ClaudeCodeEngine,
    CliCodingEngine,
    CodexEngine,
    JiuwenEngine,
    _non_interactive_task_prompt,
    assert_coding_engine_selectable,
    coding_engine_selectability,
    list_coding_engine_selectability,
)


def test_registry_returns_expected_engines():
    kinds = list_engine_kinds()
    assert {CODING_ENGINE_JIUWEN, CODING_ENGINE_CLAUDE_CODE, CODING_ENGINE_CODEX} <= set(kinds)
    assert isinstance(get_coding_engine(CODING_ENGINE_JIUWEN), JiuwenEngine)
    assert isinstance(get_coding_engine(CODING_ENGINE_CLAUDE_CODE), ClaudeCodeEngine)
    assert isinstance(get_coding_engine(CODING_ENGINE_CODEX), CodexEngine)


def test_non_interactive_task_prompt_mentions_repo_review_standards():
    """Review tasks delegated to a CLI engine must load repo-local standards."""
    prompt = _non_interactive_task_prompt(
        engine_name="Claude Code",
        message="检视 PR: https://gitcode.com/openJiuwen/jiuwenswarm/pull/7464",
        workspace=Path("/tmp/ws"),
        skills_root=Path("/tmp/skills"),
    )
    assert "repo_review_standards" in prompt
    assert "仓内检视标准" in prompt
    assert "base 版本" in prompt


def test_unknown_and_empty_engine_fall_back_to_jiuwen():
    assert isinstance(get_coding_engine(None), JiuwenEngine)
    assert isinstance(get_coding_engine(""), JiuwenEngine)
    assert isinstance(get_coding_engine("does-not-exist"), JiuwenEngine)


def test_jiuwen_engine_is_native_and_provides_no_tool():
    engine = JiuwenEngine()
    assert engine.is_cli is False
    assert engine.provides_tool() is False
    assert engine.is_available() is True
    section = engine.prompt_section(skills_root="/tmp/skills", language="cn")
    assert "jiuwen-coding" in section
    assert "coding_task" in section  # tells leader NOT to call it


def test_claude_code_credentials_require_platform_api_key(monkeypatch):
    engine = ClaudeCodeEngine()
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert engine.is_credentials_configured() is False

    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/tmp/claude-login-is-ignored")
    assert engine.is_credentials_configured() is False

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert engine.is_credentials_configured() is True


def test_coding_engine_selectability_flags_cli_credentials(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    status = list_coding_engine_selectability()
    assert status[CODING_ENGINE_JIUWEN]["selectable"] is True
    assert status[CODING_ENGINE_CLAUDE_CODE]["selectable"] is False
    assert status[CODING_ENGINE_CLAUDE_CODE]["reason"] == "anthropic_not_configured"
    assert status[CODING_ENGINE_CODEX]["selectable"] is False

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert coding_engine_selectability(CODING_ENGINE_CLAUDE_CODE)["selectable"] is True


def test_assert_coding_engine_selectable_rejects_unconfigured_claude(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
        assert_coding_engine_selectable(CODING_ENGINE_CLAUDE_CODE)


def test_background_cli_install_does_not_block(monkeypatch):
    from jiuwenavatar.server.runtime.coding import bootstrap

    calls: list[str] = []
    monkeypatch.setattr(bootstrap, "ensure_cli_installed", lambda kind: calls.append(kind) or "ok")

    detail = bootstrap.start_cli_install_background(CODING_ENGINE_CLAUDE_CODE)
    assert detail in {"install started in background", "install already running"}


def test_cli_engines_provide_tool_and_inject_prompt():
    for engine in (ClaudeCodeEngine(), CodexEngine()):
        assert engine.is_cli is True
        assert engine.provides_tool() is True
        section = engine.prompt_section(skills_root="/tmp/skills", language="cn")
        assert "coding_task" in section
        assert "GITCODE_TOKEN" in section
        assert "不要预读 diff" in section
        assert engine.display_name in section


def test_claude_code_workspace_links_skills_and_copies_agents(tmp_path, monkeypatch):
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    (skills_dir / "dev-reviewer").mkdir()

    package_assets = tmp_path / "pkg" / "avatar-skills"
    agents_src = package_assets / "claude-agents"
    agents_src.mkdir(parents=True)
    (agents_src / "dev-reviewer.md").write_text("---\nname: dev-reviewer\n---\n", encoding="utf-8")
    (package_assets / "claude-settings.json").write_text("{}", encoding="utf-8")

    cc_ws = tmp_path / "workspace" / "aidlc-cc"

    import jiuwenavatar.common.utils as utils

    monkeypatch.setattr(utils, "get_agent_workspace_dir", lambda: cc_ws.parent)
    monkeypatch.setattr(utils, "get_builtin_skills_dir", lambda: package_assets)

    engine = ClaudeCodeEngine()
    # disable auto-install attempts in CI
    monkeypatch.setenv("JIUWEN_AUTO_INSTALL_CODING_CLI", "0")
    status = engine.ensure_ready(skills_dir, auto_install=False)

    assert status.kind == CODING_ENGINE_CLAUDE_CODE
    assert (cc_ws / "skills").is_symlink()
    assert (cc_ws / "skills").resolve() == skills_dir.resolve()
    assert (cc_ws / ".claude" / "agents" / "dev-reviewer.md").is_file()
    assert (cc_ws / ".claude" / "settings.json").is_file()


def test_codex_workspace_writes_agents_md(tmp_path, monkeypatch):
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    ws_parent = tmp_path / "workspace"

    import jiuwenavatar.common.utils as utils

    monkeypatch.setattr(utils, "get_agent_workspace_dir", lambda: ws_parent)
    monkeypatch.setattr(utils, "get_builtin_skills_dir", lambda: tmp_path / "pkg" / "avatar-skills")

    engine = CodexEngine()
    status = engine.ensure_ready(skills_dir, auto_install=False)

    codex_ws = ws_parent / "aidlc-codex"
    assert status.kind == CODING_ENGINE_CODEX
    assert (codex_ws / "skills").is_symlink()
    assert (codex_ws / "AGENTS.md").is_file()


def test_claude_code_build_command_uses_stdin_not_argv():
    engine = ClaudeCodeEngine()
    args = engine._build_command("/usr/bin/claude", "task body", skip_permissions=True)
    assert args == ["/usr/bin/claude", "-p", "--dangerously-skip-permissions"]
    assert "task body" not in args
    assert engine.prompt_via_stdin is True


def test_claude_code_resolve_executable_prefers_native_exe_on_windows(monkeypatch, tmp_path):
    npm_root = tmp_path / "npm"
    wrapper = npm_root / "claude.CMD"
    wrapper.parent.mkdir(parents=True)
    wrapper.write_text("@echo off", encoding="utf-8")
    native = (
        npm_root
        / "node_modules"
        / "@anthropic-ai"
        / "claude-code"
        / "bin"
        / "claude.exe"
    )
    native.parent.mkdir(parents=True)
    native.write_bytes(b"MZ")

    monkeypatch.setattr(CliCodingEngine, "resolve_executable", lambda self: str(wrapper))
    monkeypatch.setattr("jiuwenavatar.server.runtime.coding.engines.os.name", "nt")
    resolved = ClaudeCodeEngine().resolve_executable()
    assert resolved == str(native)


@pytest.mark.asyncio
async def test_coding_task_without_active_engine_is_safe():
    from jiuwenavatar.server.runtime.coding import (
        clear_active_coding_engine,
        coding_task,
        set_active_coding_engine,
    )

    clear_active_coding_engine()
    out = await coding_task._func(message="do something")  # type: ignore[attr-defined]
    assert "原生" in out or "jiuwen" in out.lower()

    # native engine active -> still no external CLI
    set_active_coding_engine(get_coding_engine(CODING_ENGINE_JIUWEN))
    out2 = await coding_task._func(message="do something")  # type: ignore[attr-defined]
    assert "coding_task" in out2 or "jiuwen" in out2.lower()
    clear_active_coding_engine()
