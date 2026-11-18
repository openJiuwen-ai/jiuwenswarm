# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import json
import os
import io
import subprocess
import threading
import time
from pathlib import Path

import pytest

from openjiuwen.core.sys_operation.shell_process_registry import (
    kill_shell_processes_for_session,
)

from jiuwenswarm.agents.harness.common.tools import command_tools
from jiuwenswarm.agents.harness.common.tools.command_tools import (
    CommandCancelled,
    _decode_subprocess_output,
    _looks_like_posix,
    _resolve_execution_plan,
    _run_command_sync,
    _split_shell_segments,
    _translate_mkdir_p_to_powershell,
    _translate_posix_for_powershell,
    _translate_posix_segment_for_powershell,
    _available_bash,
    _available_powershell,
)

# 200KB on a single line: larger than the ~64KB pipe buffer, and its first
# 1000 characters are exactly 1000 "x" (no newlines) for clip assertions.
_LARGE_SINGLE_LINE_OUTPUT = "head -c 200000 /dev/zero | tr '\\0' 'x'"


class TestDecodeSubprocessOutput:
    @pytest.mark.parametrize(
        "data_encoding",
        [
            # UTF-8 bytes win over the Windows locale encoding.
            pytest.param("utf-8", id="utf8-preferred-over-locale"),
            # Non-UTF-8 bytes fall back to the system locale encoding.
            pytest.param("gbk", id="system-encoding-fallback"),
        ],
    )
    def test_decodes_utf8_and_system_encoded_output(self, monkeypatch, data_encoding) -> None:
        monkeypatch.setattr(
            "jiuwenswarm.agents.harness.common.tools.command_tools.locale.getpreferredencoding",
            lambda _do_setlocale: "gbk",
        )

        assert _decode_subprocess_output("中文 output".encode(data_encoding)) == "中文 output"

    def test_sync_command_decodes_stdout_and_stderr_from_bytes(self, monkeypatch) -> None:
        popen_kwargs = {}

        class FakeProcess:
            returncode = 0
            pid = 4242
            stdin = None

            def __init__(self):
                self.stdout = io.BytesIO("标准输出".encode())
                self.stderr = io.BytesIO("错误输出".encode())

            def poll(self):
                return self.returncode

        def fake_popen(*_args, **kwargs):
            popen_kwargs.update(kwargs)
            return FakeProcess()

        monkeypatch.setattr(
            "jiuwenswarm.agents.harness.common.tools.command_tools._resolve_execution_plan",
            lambda *_args: (["utf8-cli"], False, "cmd"),
        )
        monkeypatch.setattr(
            "jiuwenswarm.agents.harness.common.tools.command_tools.subprocess.Popen",
            fake_popen,
        )

        completed, _resolved_shell = _run_command_sync("utf8-cli", 5, Path("."), "cmd")

        assert completed.stdout == "标准输出"
        assert completed.stderr == "错误输出"
        assert "text" not in popen_kwargs
        assert "encoding" not in popen_kwargs


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


class TestBlockingCommandOutputDrain:
    @pytest.fixture(autouse=True)
    def _require_posix(self) -> None:
        if os.name == "nt":
            pytest.skip("POSIX shell commands only")

    def test_output_larger_than_pipe_buffer_is_collected(self, tmp_path: Path) -> None:
        result, resolved_shell = _run_command_sync(
            _LARGE_SINGLE_LINE_OUTPUT, 10, tmp_path, "bash"
        )

        assert resolved_shell == "bash"
        assert result.returncode == 0
        assert len(result.stdout) == 200000

    @pytest.mark.asyncio
    async def test_large_output_is_clipped_to_max_output_chars(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        workspace = tmp_path.resolve()
        monkeypatch.setattr(command_tools, "_context_cwd", lambda: workspace)
        monkeypatch.setattr(command_tools, "_context_project_root", lambda: workspace)
        monkeypatch.setattr(command_tools, "_context_workspace_root", lambda: workspace)

        raw = await command_tools.mcp_exec_command._func(
            command=_LARGE_SINGLE_LINE_OUTPUT,
            timeout_seconds=10,
            workdir=str(workspace),
            max_output_chars=1000,
            shell_type="bash",
        )

        payload = json.loads(raw)
        assert payload["exit_code"] == 0
        assert payload["stdout"] == "x" * 1000 + "\n...[truncated]"

    def test_cancel_still_wins_while_output_streams(self) -> None:
        outcome: dict[str, str] = {}

        def _run() -> None:
            try:
                # ~100KB/s: keeps the pipe draining without buffering gigabytes.
                _run_command_sync(
                    "while true; do printf '%02000d' 0; sleep 0.02; done",
                    60,
                    Path("."),
                    "bash",
                    session_id="sess_drain_cancel",
                )
                outcome["result"] = "completed"
            except CommandCancelled:
                outcome["result"] = "cancelled"

        thread = threading.Thread(target=_run)
        thread.start()
        try:
            time.sleep(0.3)
            kill_shell_processes_for_session("sess_drain_cancel")
        finally:
            thread.join(timeout=10)

        assert not thread.is_alive()
        assert outcome == {"result": "cancelled"}

    def test_output_beyond_drain_limit_keeps_prefix_and_completes(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(command_tools, "_DRAIN_STREAM_LIMIT_BYTES", 4096)

        result, _ = _run_command_sync(_LARGE_SINGLE_LINE_OUTPUT, 10, tmp_path, "bash")

        # The command runs to completion rather than being killed at the cap,
        # and the retained prefix is exactly the byte budget (ASCII here).
        assert result.returncode == 0
        assert result.stdout == "x" * 4096 + "\n...[output truncated: output limit reached]"
        assert result.stderr == ""

    def test_output_exactly_at_drain_limit_is_not_marked(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(command_tools, "_DRAIN_STREAM_LIMIT_BYTES", 4096)

        exact, _ = _run_command_sync(
            "head -c 4096 /dev/zero | tr '\\0' 'x'", 10, tmp_path, "bash"
        )
        assert exact.returncode == 0
        assert exact.stdout == "x" * 4096

        over, _ = _run_command_sync(
            "head -c 4097 /dev/zero | tr '\\0' 'x'", 10, tmp_path, "bash"
        )
        assert over.returncode == 0
        assert over.stdout == "x" * 4096 + "\n...[output truncated: output limit reached]"

    def test_timeout_still_applies_while_output_streams(self, tmp_path: Path) -> None:
        started = time.monotonic()
        with pytest.raises(subprocess.TimeoutExpired):
            _run_command_sync(
                "while true; do printf '%02000d' 0; sleep 0.02; done",
                1,
                tmp_path,
                "bash",
            )
        assert time.monotonic() - started < 5

    def test_cancel_kills_grandchild_holding_the_pipes(self) -> None:
        session_id = "sess_cancel_drain_stuck"
        outcome: dict[str, str] = {}

        def _run() -> None:
            try:
                # The shell takes a second to honour SIGTERM, so it is still
                # alive when the cancel branch runs; the inner shell ignores
                # SIGTERM and keeps both pipes open long after the shell is
                # gone.  The drain threads can only finish if the rest of the
                # process group is killed, not just the direct child.
                _run_command_sync(
                    "trap 'sleep 1; exit 0' TERM;"
                    " bash -c 'trap \"\" TERM; for i in $(seq 1 40); do sleep 1; done'"
                    " & sleep 30",
                    60,
                    Path("."),
                    "bash",
                    session_id=session_id,
                )
                outcome["result"] = "completed"
            except CommandCancelled:
                outcome["result"] = "cancelled"

        thread = threading.Thread(target=_run)
        thread.start()
        try:
            time.sleep(0.5)
            kill_shell_processes_for_session(session_id)
        finally:
            thread.join(timeout=15)

        assert not thread.is_alive()
        assert outcome == {"result": "cancelled"}
        leftovers = [t for t in threading.enumerate() if t.name == "command-output-drain"]
        assert leftovers == []

    def test_grandchild_holding_pipe_is_killed_on_timeout(self, tmp_path: Path) -> None:
        started = time.monotonic()
        with pytest.raises(subprocess.TimeoutExpired):
            _run_command_sync("sleep 5 &", 2, tmp_path, "bash")
        # The shell exits at once but `sleep` keeps the pipes open; the drain
        # must not wait for it, so the call returns around the deadline.
        assert time.monotonic() - started < 4

    def test_drained_stdout_is_decoded_as_utf8(self, tmp_path: Path) -> None:
        result, _ = _run_command_sync("printf '中文输出'", 10, tmp_path, "bash")

        assert result.returncode == 0
        assert result.stdout == "中文输出"

    def test_truncation_does_not_split_utf8_sequence(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(command_tools, "_DRAIN_STREAM_LIMIT_BYTES", 1000)

        # 400 * 3 bytes = 1200 bytes; 1000 is not on a character boundary.
        result, _ = _run_command_sync("printf '中%.0s' {1..400}", 10, tmp_path, "bash")

        assert result.returncode == 0
        assert result.stdout == "中" * 333 + "\n...[output truncated: output limit reached]"
        assert "\ufffd" not in result.stdout

    def test_full_chunk_at_limit_then_more_data_is_not_split(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The limit equals one read chunk, so the retained buffer is filled by
        # the ``len(chunk) <= room`` branch and the cut-off character is only
        # seen at the end of the buffer, not by _prefix_within.
        monkeypatch.setattr(command_tools, "_DRAIN_STREAM_LIMIT_BYTES", 64 * 1024)

        result, _ = _run_command_sync("printf '中%.0s' {1..30000}", 20, tmp_path, "bash")

        assert result.returncode == 0
        assert result.stdout == "中" * (65536 // 3) + "\n...[output truncated: output limit reached]"
        assert "\ufffd" not in result.stdout
    def test_drain_closes_its_pipe_when_it_reaches_eof(self) -> None:
        # Without a process group to kill, the reader owns its fd: it has to be
        # freed by the drain itself once the pipe finally drains, even if the
        # caller already gave up waiting.
        read_fd, write_fd = os.pipe()
        stream = os.fdopen(read_fd, "rb")
        drain = command_tools._BoundedPipeDrain(stream, 4096)
        drain.start()
        os.write(write_fd, b"hello")
        os.close(write_fd)
        drain.join(timeout=5)

        assert not drain.is_alive()
        assert command_tools._decode_subprocess_output(drain.data()) == "hello"
        assert stream.closed

    def test_without_process_group_the_call_returns_and_frees_pipes(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # JW_START_NEW_SESSION=0 leaves no group of our own to kill, exactly as
        # on Windows: the shell exits at once while a grandchild keeps the pipes
        # open past the join budget.  The call must still come back, and the
        # drains must release their pipes as soon as the grandchild goes away.
        monkeypatch.setenv("JW_START_NEW_SESSION", "0")
        streams: list = []
        original_init = command_tools._BoundedPipeDrain.__init__

        def _record(drain: object, stream: object, limit_bytes: int) -> None:
            streams.append(stream)
            original_init(drain, stream, limit_bytes)

        monkeypatch.setattr(command_tools._BoundedPipeDrain, "__init__", _record)
        started = time.monotonic()
        with pytest.raises(subprocess.TimeoutExpired):
            _run_command_sync("sleep 5 &", 1, tmp_path, "bash")
        assert time.monotonic() - started < 4

        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if not [t for t in threading.enumerate() if t.name == "command-output-drain"]:
                break
            time.sleep(0.1)
        assert [t for t in threading.enumerate() if t.name == "command-output-drain"] == []
        assert len(streams) == 2
        assert all(stream.closed for stream in streams)
