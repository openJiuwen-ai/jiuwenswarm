# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import os

import pytest

from jiuwenswarm.agents.harness.common.tools.command_tools import (
    _looks_like_posix,
    _resolve_execution_plan,
    _split_shell_segments,
    _translate_mkdir_p_to_powershell,
    _translate_posix_for_powershell,
    _translate_posix_segment_for_powershell,
    _available_bash,
    _available_powershell,
)


# ── _translate_mkdir_p_to_powershell ────────────────────────────────


class TestTranslateMkdirP:
    @pytest.mark.parametrize(
        "command, expected",
        [
            ("mkdir -p dir", "New-Item -ItemType Directory -Path 'dir' -Force"),
            ("mkdir -p a/b/c", "New-Item -ItemType Directory -Path 'a/b/c' -Force"),
            ("mkdir -p 'path with spaces'", "New-Item -ItemType Directory -Path 'path with spaces' -Force"),
            ("mkdir -p a b c", "New-Item -ItemType Directory -Path 'a' -Force; New-Item -ItemType Directory -Path 'b' -Force; New-Item -ItemType Directory -Path 'c' -Force"),
            ("mkdir --parents dir", "New-Item -ItemType Directory -Path 'dir' -Force"),
            ("mkdir -p dir/subdir", "New-Item -ItemType Directory -Path 'dir/subdir' -Force"),
        ],
    )
    def test_translates_mkdir_p(self, command: str, expected: str) -> None:
        assert _translate_mkdir_p_to_powershell(command) == expected

    @pytest.mark.parametrize(
        "command",
        [
            "mkdir dir",
            "mkdir -p",
            "mkdir -p dir -v",
            "mkdir -p dir -m 755",
            "ls -la",
            "cat file.txt",
            "",
        ],
    )
    def test_returns_none_for_untranslatable(self, command: str) -> None:
        assert _translate_mkdir_p_to_powershell(command) is None

    @pytest.mark.parametrize(
        "command, expected",
        [
            ("mkdir -p 'path with \"quotes\"'", "New-Item -ItemType Directory -Path 'path with \"quotes\"' -Force"),
            ('mkdir -p "$(Start-Process calc)"', "New-Item -ItemType Directory -Path '$(Start-Process calc)' -Force"),
            ('mkdir -p "$env:TEMP"', "New-Item -ItemType Directory -Path '$env:TEMP' -Force"),
            ("mkdir -p \"it's here\"", "New-Item -ItemType Directory -Path 'it''s here' -Force"),
        ],
    )
    def test_injection_vectors_neutralized(self, command: str, expected: str) -> None:
        assert _translate_mkdir_p_to_powershell(command) == expected


# ── _translate_posix_segment_for_powershell ─────────────────────────


class TestTranslatePosixSegment:
    def test_translates_mkdir_p_segment(self) -> None:
        assert _translate_posix_segment_for_powershell("mkdir -p dir") is not None

    def test_returns_none_for_ls(self) -> None:
        assert _translate_posix_segment_for_powershell("ls -la") is None

    def test_returns_none_for_empty(self) -> None:
        assert _translate_posix_segment_for_powershell("") is None

    def test_returns_none_for_mkdir_without_p(self) -> None:
        assert _translate_posix_segment_for_powershell("mkdir dir") is None


# ── _translate_posix_for_powershell ─────────────────────────────────


class TestTranslatePosixForPowershell:
    def test_translates_single_mkdir_p(self) -> None:
        result = _translate_posix_for_powershell("mkdir -p dir")
        assert result == "New-Item -ItemType Directory -Path 'dir' -Force"

    @pytest.mark.parametrize(
        "command",
        [
            "mkdir -p dir && echo done",
            "mkdir -p dir; ls",
            "ls -la",
            "echo hello",
        ],
    )
    def test_returns_none_for_compound_or_untranslatable(self, command: str) -> None:
        assert _translate_posix_for_powershell(command) is None


# ── _looks_like_posix ───────────────────────────────────────────────


class TestLooksLikePosix:
    @pytest.mark.parametrize(
        "command",
        [
            "mkdir -p dir",
            "ls -la",
            "cat file.txt",
            "grep pattern file",
            "find . -name '*.py'",
            "rm -rf dir",
            "touch file.txt",
            "chmod 755 script.sh",
            "ls && cat file.txt",
            "mkdir dir; ls",
        ],
    )
    def test_detects_posix_commands(self, command: str) -> None:
        assert _looks_like_posix(command) is True

    @pytest.mark.parametrize(
        "command",
        [
            "echo hello",
            "python script.py",
            "git status",
            "npm install",
            "dir /s pattern",
            "powershell Get-ChildItem",
        ],
    )
    def test_rejects_non_posix_commands(self, command: str) -> None:
        assert _looks_like_posix(command) is False

    def test_empty_command(self) -> None:
        assert _looks_like_posix("") is False


# ── _resolve_execution_plan on Windows ──────────────────────────────


class TestResolveExecutionPlanWindows:
    @pytest.fixture(autouse=True)
    def _require_windows(self) -> None:
        if os.name != "nt":
            pytest.skip("Windows-only tests")

    @pytest.fixture()
    def _no_bash(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "jiuwenswarm.agents.harness.common.tools.command_tools._available_bash",
            lambda **kw: None,
        )

    @pytest.fixture()
    def _with_bash(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "jiuwenswarm.agents.harness.common.tools.command_tools._available_bash",
            lambda **kw: "C:\\Program Files\\Git\\bin\\bash.exe",
        )

    def test_mkdir_p_translated_to_powershell_when_no_bash(self, _no_bash) -> None:
        plan, use_shell, resolved_shell = _resolve_execution_plan("mkdir -p dir", "auto")
        assert resolved_shell == "powershell"
        assert use_shell is False
        assert "New-Item" in plan[-1]
        assert "-ItemType Directory" in plan[-1]

    def test_mkdir_p_uses_bash_when_available(self, _with_bash) -> None:
        plan, use_shell, resolved_shell = _resolve_execution_plan("mkdir -p dir", "auto")
        assert resolved_shell == "bash"
        assert use_shell is False

    def test_ls_routes_to_powershell_when_no_bash(self, _no_bash) -> None:
        plan, use_shell, resolved_shell = _resolve_execution_plan("ls", "auto")
        assert resolved_shell == "powershell"
        assert use_shell is False

    def test_non_posix_stays_on_cmd(self, _no_bash) -> None:
        plan, use_shell, resolved_shell = _resolve_execution_plan("echo hello", "auto")
        assert resolved_shell == "cmd"
        assert use_shell is True

    def test_compound_posix_routes_to_powershell_raw_when_no_bash(self, _no_bash) -> None:
        plan, use_shell, resolved_shell = _resolve_execution_plan("mkdir -p dir && echo done", "auto")
        assert resolved_shell == "powershell"
        assert use_shell is False

    def test_mkdir_without_p_routes_to_powershell_when_no_bash(self, _no_bash) -> None:
        plan, use_shell, resolved_shell = _resolve_execution_plan("mkdir dir", "auto")
        assert resolved_shell == "powershell"
        assert use_shell is False

    def test_mkdir_p_nested_path_translated(self, _no_bash) -> None:
        plan, use_shell, resolved_shell = _resolve_execution_plan("mkdir -p output/slides", "auto")
        assert resolved_shell == "powershell"
        assert "New-Item" in plan[-1]
        assert "output/slides" in plan[-1]

    def test_mkdir_p_with_spaces_translated(self, _no_bash) -> None:
        plan, use_shell, resolved_shell = _resolve_execution_plan("mkdir -p \"path with spaces\"", "auto")
        assert resolved_shell == "powershell"
        assert "New-Item" in plan[-1]
        assert "path with spaces" in plan[-1]


class TestResolveExecutionPlanNonWindows:
    @pytest.fixture(autouse=True)
    def _require_non_windows(self) -> None:
        if os.name == "nt":
            pytest.skip("Non-Windows-only tests")

    def test_auto_routes_to_bash(self) -> None:
        plan, use_shell, resolved_shell = _resolve_execution_plan("mkdir -p dir", "auto")
        assert resolved_shell in ("bash", "sh")
