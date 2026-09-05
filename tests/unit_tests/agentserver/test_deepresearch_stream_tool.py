# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Contract tests for the isolated ``deepresearch_stream`` runtime client."""

from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import stat
import sys
import zipfile
from contextlib import ExitStack, asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest

from jiuwenswarm.agents.harness.common.tools.deepresearch import tools as dt
from jiuwenswarm.agents.harness.common.tools.deepresearch.runtime import (
    DeepResearchRuntimeError,
)
from jiuwenswarm.agents.harness.common.tools.deepresearch.stream_router import (
    MAX_CHUNK_TEXT_CHARS,
)
from jiuwenswarm.common.local_env_config import (
    bind_task_env_overlay,
    clear_agent_env_ns,
    replace_active_env,
    reset_task_env_overlay,
)
from jiuwenswarm.server.runtime.session import session_history


class _Stdin:
    def __init__(self, *, drain_error: BaseException | None = None):
        self.data = bytearray()
        self.closed = False
        self.drain_error = drain_error

    def write(self, data: bytes) -> None:
        self.data.extend(data)

    async def drain(self) -> None:
        if self.drain_error is not None:
            raise self.drain_error

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


class _Reader:
    def __init__(self, payload: bytes = b""):
        self.payload = payload
        self.reads = 0

    async def read(self, _size: int = -1) -> bytes:
        if self.reads:
            return b""
        self.reads += 1
        return self.payload


class _DelayedFinalReportReader:
    def __init__(
        self,
        initial: bytes,
        terminal: bytes,
        delay: float,
        *,
        initial_delay: float = 0.0,
    ):
        self.initial = initial
        self.terminal = terminal
        self.delay = delay
        self.initial_delay = initial_delay
        self.reads = 0
        self.cancelled = False

    async def read(self, _size: int = -1) -> bytes:
        self.reads += 1
        if self.reads == 1:
            if self.initial_delay:
                try:
                    await asyncio.sleep(self.initial_delay)
                except asyncio.CancelledError:
                    self.cancelled = True
                    raise
            return self.initial
        if self.reads == 2:
            try:
                await asyncio.sleep(self.delay)
            except asyncio.CancelledError:
                self.cancelled = True
                raise
            return self.terminal
        return b""


class _Proc:
    """Small asyncio subprocess double with observable lifecycle."""

    def __init__(
        self,
        lines: list[str] | None = None,
        *,
        stderr: bytes = b"",
        stdin: _Stdin | None = None,
        running: bool = False,
    ):
        payload = b"".join(line.encode("utf-8") + b"\n" for line in (lines or []))
        self.stdin = stdin or _Stdin()
        self.stdout = _Reader(payload)
        self.stderr = _Reader(stderr)
        self.returncode = None if running else 0
        self.pid = 1234
        self.terminated = 0
        self.killed = 0
        self.waited = 0

    async def wait(self) -> int:
        self.waited += 1
        if self.returncode is None:
            self.returncode = -15 if self.terminated else 0
        return self.returncode

    def terminate(self) -> None:
        self.terminated += 1

    def kill(self) -> None:
        self.killed += 1
        self.returncode = -9


def _valid_config(**overrides: str) -> dict[str, str]:
    config = {
        "LLM_API_KEY": "llm-secret",
        "LLM_MODEL_NAME": "model",
        "LLM_BASE_URL": "https://llm.invalid/v1",
        "LLM_MODEL_TYPE": "openai",
        "WEB_SEARCH_API_KEY": "search-secret",
        "WEB_SEARCH_ENGINE_NAME": "bocha",
        "LLM_SSL_VERIFY": "false",
        "TOOL_SSL_VERIFY": "false",
        "DEEPSEARCH_HITL": "true",
    }
    config.update(overrides)
    return config


def test_deepresearch_stream_defers_timeout_to_its_bounded_runtime() -> None:
    assert dt.deepresearch_stream.card.properties["resilience"]["timeout_s"] is None


def _stream_patches(proc: _Proc, *, route: dict[str, str] | None = None):
    return (
        patch.object(dt, "resolve_python_executable", return_value=Path("/runtime/bin/python")),
        patch.object(dt, "_resolve_run_script", return_value="/skills/deepresearch/scripts/run_deepsearch.py"),
        patch.object(dt, "_build_deepresearch_request_config", return_value=_valid_config()),
        patch.object(
            dt,
            "_build_deepresearch_child_env",
            return_value={"PATH": "/runtime/bin", "PYTHONUNBUFFERED": "1", "PYTHONUTF8": "1"},
        ),
        patch.object(
            dt,
            "_get_route",
            return_value=route
            or {
                "request_id": "",
                "channel_id": "",
                "session_id": "",
                "service_id": "default",
                "agent_id": "default",
            },
        ),
        patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)),
    )


def test_config_frame_accepts_exact_limit_and_rejects_one_byte_over():
    base = _valid_config()
    fixed_size = len(dt._encode_deepresearch_config({**base, "padding": ""}))
    exact = {**base, "padding": "x" * (dt.DEEPRESEARCH_CONFIG_MAX_BYTES - fixed_size)}
    assert len(dt._encode_deepresearch_config(exact)) == dt.DEEPRESEARCH_CONFIG_MAX_BYTES

    with pytest.raises(ValueError, match="64 KiB"):
        dt._encode_deepresearch_config({**exact, "padding": exact["padding"] + "x"})


def test_child_env_delegates_to_isolated_runtime_and_contains_no_secrets():
    executable = Path("/isolated/bin/python")
    base_env = {"PATH": "/isolated/bin", "HTTP_PROXY": "http://proxy"}
    with patch.object(dt, "resolve_python_executable", return_value=executable), patch.object(
        dt, "build_child_env", return_value=base_env
    ) as build:
        env = dt._build_deepresearch_child_env(
            interactive_ask=True,
            runtime_config=_valid_config(),
        )

    build.assert_called_once_with(executable)
    assert env["PYTHONUNBUFFERED"] == "1"
    assert env["PYTHONUTF8"] == "1"
    for forbidden in (
        "LLM_API_KEY",
        "WEB_SEARCH_API_KEY",
        "PYTHONHOME",
        "PYTHONPATH",
        "ALL_PROXY",
        "all_proxy",
    ):
        assert forbidden not in env


def test_skill_resolution_uses_only_shared_roots(tmp_path: Path, monkeypatch):
    shared = tmp_path / "shared"
    runner = shared / "deepresearch" / "scripts" / "run_deepsearch.py"
    runner.parent.mkdir(parents=True)
    runner.write_text("# runner\n", encoding="utf-8")
    cwd_runner = tmp_path / "deepresearch" / "scripts" / "run_deepsearch.py"
    cwd_runner.parent.mkdir(parents=True)
    cwd_runner.write_text("# must not win\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("JIUWENCLAW_SHARED_SKILLS_DIRS", str(tmp_path / "ambient"))
    overlay_token = bind_task_env_overlay(
        {"JIUWENSWARM_SHARED_SKILLS_DIRS": str(shared)}
    )
    try:
        assert dt._resolve_skill_root() == str(shared / "deepresearch")
        assert dt._resolve_run_script() == str(runner)
    finally:
        reset_task_env_overlay(overlay_token)


def test_skill_resolution_uses_route_scoped_tenant_tip_when_overlay_unbound(
    tmp_path: Path, monkeypatch
):
    service_id = "deepresearch-test-service"
    agent_id = "office"
    shared = tmp_path / "shared"
    runner = shared / "deepresearch" / "scripts" / "run_deepsearch.py"
    runner.parent.mkdir(parents=True)
    runner.write_text("# runner\n", encoding="utf-8")
    monkeypatch.delenv("JIUWENSWARM_SHARED_SKILLS_DIRS", raising=False)
    monkeypatch.delenv("JIUWENCLAW_SHARED_SKILLS_DIRS", raising=False)
    replace_active_env(
        {"JIUWENSWARM_SHARED_SKILLS_DIRS": str(shared)},
        service_id=service_id,
        agent_id=agent_id,
    )
    route_token = dt.push_deepresearch_route(
        "request", "channel", "session", service_id=service_id, agent_id=agent_id
    )
    try:
        assert dt._resolve_skill_root() == str(shared / "deepresearch")
    finally:
        dt.reset_deepresearch_route(route_token)
        clear_agent_env_ns(service_id, agent_id)


def test_route_scoped_output_dir_overrides_core_cwd(tmp_path: Path):
    output_dir = tmp_path / "agent-workspace" / "projects"
    route_token = dt.push_deepresearch_route(
        "request",
        "channel",
        "session",
        output_dir=str(output_dir),
    )
    try:
        assert dt._get_effective_request_output_dir() == output_dir.resolve()
    finally:
        dt.reset_deepresearch_route(route_token)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message", "code"),
    [
        ("runtime_python_missing", "runtime_python_missing"),
        ("runtime_python_invalid", "runtime_python_invalid"),
    ],
)
async def test_missing_or_invalid_runtime_fails_closed_without_spawn(message: str, code: str):
    spawn = AsyncMock()
    with patch.object(
        dt, "resolve_python_executable", side_effect=DeepResearchRuntimeError(message)
    ), patch.object(dt, "_resolve_run_script", return_value="/runner"), patch(
        "asyncio.create_subprocess_exec", new=spawn
    ), patch.object(sys, "executable", "/must/not/fallback"):
        result = json.loads(await dt.deepresearch_stream._func(action="start", query="q"))

    assert result["status"] == "error"
    assert result["error_code"] == code
    spawn.assert_not_awaited()


@pytest.mark.asyncio
async def test_stream_sends_versioned_config_over_stdin_only():
    proc = _Proc([
        json.dumps({"__deepsearch_status__": "started", "conversation_id": "C1"}),
        json.dumps(
            {
                "agent": "intent_recognition",
                "event": "start",
                "section_idx": "0",
            }
        ),
        json.dumps(
            {
                "__deepsearch_status__": "interrupted",
                "agent": "feedback_handler",
                "conversation_id": "C1",
                "timing": {
                    "schema_version": 2,
                    "runner_total_ms": 25,
                    "runner_bootstrap_ms": 5,
                    "sdk_execution_ms": 20,
                    "sdk_first_node_ms": 3,
                    "sdk_node_spans": [],
                    "sdk_node_summary": [],
                },
            }
        ),
    ])
    patches = _stream_patches(proc)
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5] as spawn:
        outcome = await dt.deepresearch_stream._func(action="start", query="q", file_name="r")

    assert spawn.await_args.args[0] == "/runtime/bin/python"
    assert spawn.await_args.args[1] == "/skills/deepresearch/scripts/run_deepsearch.py"
    assert "--config-stdin" in spawn.await_args.args
    assert "LLM_API_KEY" not in spawn.await_args.kwargs["env"]
    frame = json.loads(bytes(proc.stdin.data))
    assert frame["version"] == 1
    assert frame["config"] == _valid_config()
    assert frame["tls"] == {"LLM_SSL_VERIFY": False, "TOOL_SSL_VERIFY": False}
    parsed_outcome = json.loads(outcome)
    assert parsed_outcome["status"] == "interrupted"
    assert parsed_outcome["timing"]["sdk_first_node_ms"] == 3
    assert parsed_outcome["timing"]["skill_to_sdk_first_node_ms"] >= 0
    assert parsed_outcome["skill_execution_ms"] >= 0


def test_only_six_standard_proxy_names_are_forwarded(tmp_path: Path, monkeypatch):
    venv = tmp_path / "runtime"
    python = venv / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("#!/bin/sh\n", encoding="utf-8")
    python.chmod(0o755)
    (venv / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")
    for key in (
        "HTTP_PROXY",
        "http_proxy",
        "HTTPS_PROXY",
        "https_proxy",
        "NO_PROXY",
        "no_proxy",
        "ALL_PROXY",
    ):
        monkeypatch.setenv(key, key)
    env = dt._build_deepresearch_child_env(
        interactive_ask=True,
        runtime_config=_valid_config(),
    )
    assert set(env).issuperset(
        {"HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "NO_PROXY", "no_proxy"}
    )
    assert "ALL_PROXY" not in env


def test_module_does_not_restore_legacy_six_interfaces():
    legacy = {
        "deepresearch_create_task",
        "deepresearch_get_status",
        "deepresearch_list_tasks",
        "deepresearch_cancel_task",
        "deepresearch_get_result",
        "deepresearch_run_task",
        "get_deepresearch_tools",
    }
    assert legacy.isdisjoint(vars(dt))


def _zip_payload(entries: list[tuple[str, bytes, int | None]]) -> str:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        for name, payload, external_attr in entries:
            info = zipfile.ZipInfo(name)
            if external_attr is not None:
                info.create_system = 3
                info.external_attr = external_attr
            archive.writestr(info, payload)
    return __import__("base64").b64encode(stream.getvalue()).decode("ascii")


@pytest.mark.parametrize(
    "member",
    ["../evil", "/absolute", "C:/windows", "C:\\windows", "a/../../evil"],
)
def test_styled_zip_rejects_traversal_and_absolute_members(tmp_path: Path, member: str):
    payload = _zip_payload(
        [
            ("report_bundle/report.html", b"ok", None),
            (member, b"bad", None),
        ]
    )
    with pytest.raises(ValueError, match="unsafe ZIP"):
        dt._extract_styled_bundle(payload, tmp_path)


def test_styled_zip_rejects_symlink_member(tmp_path: Path):
    payload = _zip_payload(
        [
            ("report_bundle/report.html", b"ok", None),
            ("report_bundle/link", b"target", 0o120777 << 16),
        ]
    )
    with pytest.raises(ValueError, match="unsafe ZIP"):
        dt._extract_styled_bundle(payload, tmp_path)


def test_styled_zip_rejects_duplicate_normalized_member(tmp_path: Path):
    payload = _zip_payload(
        [
            ("report_bundle/report.html", b"ok", None),
            ("report_bundle/a.txt", b"a", None),
            ("report_bundle\\a.txt", b"b", None),
        ]
    )
    with pytest.raises(ValueError, match="duplicate ZIP"):
        dt._extract_styled_bundle(payload, tmp_path)


def test_package_exports_stream_route_and_formal_registration():
    from jiuwenswarm.agents.harness.common.tools import deepresearch

    assert "deepresearch_stream" in deepresearch.__all__
    assert "push_deepresearch_route" in deepresearch.__all__
    assert "reset_deepresearch_route" in deepresearch.__all__
    assert "get_deepresearch_tools" in deepresearch.__all__


@pytest.mark.asyncio
async def test_track_and_untrack_cover_terminal_error_exit():
    proc = _Proc(
        [
            json.dumps(
                {
                    "__deepsearch_status__": "error",
                    "error_code": "WEB_SEARCH_PROXY_ERROR",
                    "error": "proxy failed",
                }
            )
        ]
    )
    manager = Mock()
    patches = _stream_patches(proc)
    with ExitStack() as stack:
        for item in patches:
            stack.enter_context(item)
        stack.enter_context(patch.object(dt, "get_deepresearch_manager", return_value=manager))
        result = json.loads(await dt.deepresearch_stream._func(action="start", query="q"))

    assert result["error_code"] == "WEB_SEARCH_PROXY_ERROR"
    manager.track_process.assert_called_once_with("", proc)
    manager.untrack_process.assert_called_once_with("", proc)
    assert proc.waited >= 1


@pytest.mark.asyncio
async def test_track_failure_reaps_locally_without_untrack():
    proc = _Proc([], running=True)
    manager = Mock()
    manager.track_process.side_effect = RuntimeError("manager closed")
    patches = _stream_patches(proc)
    with ExitStack() as stack:
        for item in patches:
            stack.enter_context(item)
        stack.enter_context(patch.object(dt, "get_deepresearch_manager", return_value=manager))
        result = json.loads(await dt.deepresearch_stream._func(action="start", query="q"))

    assert result["status"] == "error"
    manager.untrack_process.assert_not_called()
    assert proc.terminated >= 1
    assert proc.waited >= 1


@pytest.mark.asyncio
async def test_config_pipe_failure_reaps_and_untracks():
    proc = _Proc([], stdin=_Stdin(drain_error=BrokenPipeError()), running=True)
    manager = Mock()
    patches = _stream_patches(proc)
    with ExitStack() as stack:
        for item in patches:
            stack.enter_context(item)
        stack.enter_context(patch.object(dt, "get_deepresearch_manager", return_value=manager))
        result = json.loads(await dt.deepresearch_stream._func(action="start", query="q"))

    assert result["status"] == "error"
    assert proc.stdin.closed is True
    assert proc.terminated >= 1
    assert proc.waited >= 1
    manager.untrack_process.assert_called_once_with("", proc)


class _BackpressureStdin(_Stdin):
    def __init__(self, stderr_started: asyncio.Event):
        super().__init__()
        self.stderr_started = stderr_started

    async def drain(self) -> None:
        await asyncio.wait_for(self.stderr_started.wait(), timeout=1)


class _SignalReader(_Reader):
    def __init__(self, event: asyncio.Event):
        super().__init__(b"diagnostics")
        self.event = event

    async def read(self, size: int = -1) -> bytes:
        self.event.set()
        return await super().read(size)


@pytest.mark.asyncio
async def test_stderr_drain_starts_before_config_stdin_can_backpressure():
    stderr_started = asyncio.Event()
    proc = _Proc(
        [json.dumps({"__deepsearch_status__": "error", "error": "done"})],
        stdin=_BackpressureStdin(stderr_started),
    )
    proc.stderr = _SignalReader(stderr_started)
    patches = _stream_patches(proc)
    with ExitStack() as stack:
        for item in patches:
            stack.enter_context(item)
        result = json.loads(await dt.deepresearch_stream._func(action="start", query="q"))

    assert result["status"] == "error"
    assert result["stderr_tail"] == "diagnostics"


class _BlockingReader:
    async def read(self, _size: int = -1) -> bytes:
        await asyncio.Event().wait()
        return b""


class _CancellationProc(_Proc):
    def __init__(self):
        super().__init__([], running=True)
        self.stdout = _BlockingReader()
        self.release_wait = asyncio.Event()

    async def wait(self) -> int:
        self.waited += 1
        await self.release_wait.wait()
        self.returncode = -15 if self.terminated else -9
        return self.returncode


@pytest.mark.asyncio
async def test_repeated_cancellation_finishes_reap_and_untrack_before_reraising():
    proc = _CancellationProc()
    manager = Mock()
    patches = _stream_patches(proc)

    async def invoke():
        with ExitStack() as stack:
            for item in patches:
                stack.enter_context(item)
            stack.enter_context(
                patch.object(dt, "get_deepresearch_manager", return_value=manager)
            )
            await dt.deepresearch_stream._func(action="start", query="q")

    task = asyncio.create_task(invoke())
    for _ in range(100):
        if manager.track_process.called:
            break
        await asyncio.sleep(0)
    task.cancel()
    for _ in range(100):
        if proc.terminated:
            break
        await asyncio.sleep(0)
    task.cancel()
    proc.release_wait.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert proc.waited >= 1
    manager.untrack_process.assert_called_once_with("", proc)


@pytest.mark.asyncio
async def test_stdout_pending_buffer_enforces_legacy_limit():
    legacy_limit = 16 * 1024 * 1024
    stream = _Reader(b"x" * (legacy_limit + 1))

    with patch.object(dt.logger, "error") as error:
        with pytest.raises(ValueError, match="deepresearch_stdout_limit_exceeded"):
            async for _line in dt._iter_ndjson_lines(stream):
                pass

    assert error.call_args.args[1:3] == (
        legacy_limit + 1,
        legacy_limit,
    )


@pytest.mark.asyncio
async def test_stdout_pending_buffer_is_bounded_and_logs_dimensions():
    stream = _Reader(b"x" * (dt.DEEPRESEARCH_STDOUT_PENDING_MAX_BYTES + 1))
    with patch.object(dt.logger, "error") as error:
        with pytest.raises(ValueError, match="deepresearch_stdout_limit_exceeded"):
            async for _line in dt._iter_ndjson_lines(stream):
                pass

    assert error.call_args.args[1:3] == (
        dt.DEEPRESEARCH_STDOUT_PENDING_MAX_BYTES + 1,
        dt.DEEPRESEARCH_STDOUT_PENDING_MAX_BYTES,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exception_message", "expected_reason", "expected_exc_info"),
    [
        (
            "deepresearch_stdout_limit_exceeded",
            "deepresearch_stdout_limit_exceeded",
            True,
        ),
        (
            "deepresearch_router_limit_exceeded",
            "deepresearch_router_limit_exceeded",
            True,
        ),
        ("exception-secret-that-must-not-be-logged", "unclassified", False),
    ],
)
async def test_stream_failure_logs_safe_reason_and_correlation(
    exception_message: str, expected_reason: str, expected_exc_info: bool
):
    proc = _Proc([])
    route = {
        "request_id": "REQ-123",
        "channel_id": "CHANNEL-123",
        "session_id": "SESSION-123",
        "service_id": "default",
        "agent_id": "default",
    }
    patches = _stream_patches(proc, route=route)

    with ExitStack() as stack:
        for item in patches:
            stack.enter_context(item)
        logged_error = stack.enter_context(patch.object(dt.logger, "error"))
        stack.enter_context(
            patch.object(
                dt,
                "_consume_stream",
                new=AsyncMock(side_effect=ValueError(exception_message)),
            )
        )
        outcome = json.loads(
            await dt.deepresearch_stream._func(
                action="resume",
                conversation_id="CONVERSATION-123",
                node="user_feedback_processor",
                query="query-secret-that-must-not-be-logged",
            )
        )

    assert outcome["error_code"] == "stream_failed"
    assert outcome["error"] == "DeepResearch stream failed: ValueError"
    assert logged_error.call_args.args[1:] == (
        "ValueError",
        expected_reason,
        "REQ-123",
        "SESSION-123",
        "CONVERSATION-123",
    )
    assert logged_error.call_args.kwargs == {"exc_info": expected_exc_info}
    logged_call = repr(logged_error.call_args)
    assert "query-secret-that-must-not-be-logged" not in logged_call
    assert "exception-secret-that-must-not-be-logged" not in logged_call


@pytest.mark.asyncio
async def test_resume_conversation_id_never_enters_progress_path():
    proc = _Proc([json.dumps({"__deepsearch_status__": "error", "error": "done"})])
    spawn = AsyncMock(return_value=proc)
    route = {
        "request_id": "",
        "channel_id": "",
        "session_id": "",
        "service_id": "default",
        "agent_id": "default",
    }
    with patch.object(dt, "resolve_python_executable", return_value=Path("/runtime/bin/python")), patch.object(
        dt, "_resolve_run_script", return_value="/runner"
    ), patch.object(dt, "_build_deepresearch_request_config", return_value=_valid_config()), patch.object(
        dt, "_build_deepresearch_child_env", return_value={"PATH": "/runtime/bin"}
    ), patch.object(dt, "_get_route", return_value=route), patch(
        "asyncio.create_subprocess_exec", new=spawn
    ):
        await dt.deepresearch_stream._func(
            action="resume",
            conversation_id="../../escape",
            node="feedback_handler",
        )

    args = spawn.await_args.args
    progress = Path(args[args.index("--progress-file") + 1])
    assert progress.parent.parent == Path(__import__("tempfile").gettempdir())
    assert "escape" not in progress.name


@pytest.mark.parametrize(
    ("interaction_result", "expected"),
    [
        (
            json.dumps({"status": "skipped", "answers": []}),
            '{"feedback":"","interaction_status":"skipped"}',
        ),
        (
            json.dumps(
                {
                    "status": "answered",
                    "answers": [{"selected_options": [], "custom_input": "  "}],
                }
            ),
            '{"feedback":"","interaction_status":"skipped"}',
        ),
        (
            json.dumps(
                {
                    "status": "answered",
                    "answers": [{"selected_options": ["market"], "custom_input": ""}],
                }
            ),
            "native feedback",
        ),
    ],
)
def test_feedback_resume_preserves_answered_and_skipped_machine_state(
    interaction_result: str, expected: str
):
    assert (
        dt._normalize_feedback_handler_resume_feedback(
            "native feedback", interaction_result
        )
        == expected
    )


@pytest.mark.parametrize(
    "interaction_result",
    [
        "not-json",
        "[]",
        json.dumps({"status": "cancelled", "answers": []}),
        json.dumps({"status": "skipped", "answers": [{"custom_input": "answer"}]}),
    ],
)
def test_feedback_resume_rejects_ambiguous_or_contradictory_state(
    interaction_result: str,
):
    with pytest.raises(ValueError):
        dt._normalize_feedback_handler_resume_feedback("feedback", interaction_result)


@pytest.mark.asyncio
async def test_invalid_feedback_resume_does_not_spawn():
    spawn = AsyncMock()
    with patch.object(dt, "resolve_python_executable", return_value=Path("/runtime/bin/python")), patch.object(
        dt, "_resolve_run_script", return_value="/runner"
    ), patch("asyncio.create_subprocess_exec", new=spawn):
        outcome = json.loads(
            await dt.deepresearch_stream._func(
                action="resume",
                conversation_id="C1",
                node="feedback_handler",
                interaction_result='{"status":"cancelled","answers":[]}',
            )
        )

    assert outcome["error_code"] == "interaction_result_invalid"
    spawn.assert_not_awaited()


@pytest.mark.asyncio
async def test_stream_forces_hitl_on_request_config():
    proc = _Proc([json.dumps({"__deepsearch_status__": "error", "error": "done"})])
    config_builder = Mock(return_value=_valid_config())
    with patch.object(dt, "resolve_python_executable", return_value=Path("/runtime/bin/python")), patch.object(
        dt, "_resolve_run_script", return_value="/runner"
    ), patch.object(dt, "_build_deepresearch_request_config", config_builder), patch.object(
        dt, "_build_deepresearch_child_env", return_value={"PATH": "/runtime/bin"}
    ), patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
        await dt.deepresearch_stream._func(action="start", query="q")

    assert config_builder.call_args.kwargs["interactive_ask"] is True


@pytest.mark.asyncio
async def test_feedback_interrupt_injects_accumulated_questions():
    proc = _Proc(
        [
            json.dumps({"__deepsearch_status__": "started", "conversation_id": "C1"}),
            json.dumps(
                {
                    "agent": "question_generator",
                    "message_type": "message_chunk",
                    "message_id": "Q1",
                    "content": "1. 市场？\n2. 时间？",
                },
                ensure_ascii=False,
            ),
            json.dumps(
                {
                    "__deepsearch_status__": "interrupted",
                    "agent": "feedback_handler",
                    "conversation_id": "C1",
                },
                ensure_ascii=False,
            ),
        ]
    )
    patches = _stream_patches(proc)
    with ExitStack() as stack:
        for item in patches:
            stack.enter_context(item)
        outcome = json.loads(await dt.deepresearch_stream._func(action="start", query="q"))

    assert outcome["status"] == "interrupted"
    assert outcome["marker"]["questions"] == "1. 市场？\n2. 时间？"


@pytest.mark.asyncio
async def test_outline_interrupt_returns_interrupted_without_auto_resume(tmp_path: Path):
    """outline_interaction interrupt now surfaces to caller instead of auto-resuming with accepted feedback."""
    outline = json.dumps(
        {"title": "AI Agent", "sections": [{"id": "1", "title": "架构"}]},
        ensure_ascii=False,
    )
    proc = _Proc(
        [
            json.dumps({"__deepsearch_status__": "started", "conversation_id": "C1"}),
            json.dumps({"agent": "outline", "content": outline}, ensure_ascii=False),
            json.dumps(
                {
                    "__deepsearch_status__": "interrupted",
                    "agent": "outline_interaction",
                    "conversation_id": "C1",
                    "outline": outline,
                },
                ensure_ascii=False,
            ),
        ]
    )
    route = {
        "request_id": "R1",
        "channel_id": "CH1",
        "session_id": "S1",
        "service_id": "default",
        "agent_id": "default",
    }
    artifact = dt._ProgressArtifact(Path("/private/progress"), (1, 2), (3, 4))
    remove = Mock()
    manager = Mock()
    patches = _stream_patches(proc, route=route)
    with ExitStack() as stack:
        for item in patches:
            stack.enter_context(item)
        stack.enter_context(patch.object(dt, "WebSocketGatewayPushTransport", return_value=AsyncMock()))
        stack.enter_context(patch.object(dt, "get_deepresearch_manager", return_value=manager))
        stack.enter_context(patch.object(dt, "_create_progress_artifact", return_value=artifact))
        stack.enter_context(patch.object(dt, "_remove_progress_artifact", remove))
        outcome = json.loads(
            await dt.deepresearch_stream._func(action="start", query="q", file_name="report")
        )

    assert outcome["status"] == "interrupted"
    assert outcome["node_id"] == "outline_interaction"
    assert "interaction_policy" not in outcome


@pytest.mark.asyncio
async def test_outline_interrupt_marker_uses_card_markdown_and_caches_json(tmp_path: Path):
    """When interrupted marker lacks outline, card markdown is built from state.outline_parts and full JSON is cached."""
    outline_json = {
        "title": "AI Agent 架构",
        "thought": "从框架到部署",
        "sections": [
            {"id": "1", "title": "架构设计", "is_core_section": True, "description": "核心架构"},
            {"id": "2", "title": "部署方案", "format_requirements": "markdown"},
        ],
    }
    outline_str = json.dumps(outline_json, ensure_ascii=False)
    proc = _Proc(
        [
            json.dumps({"__deepsearch_status__": "started", "conversation_id": "C1"}),
            json.dumps({"agent": "outline", "content": outline_str}, ensure_ascii=False),
            json.dumps(
                {
                    "__deepsearch_status__": "interrupted",
                    "agent": "outline_interaction",
                    "conversation_id": "C1",
                },
                ensure_ascii=False,
            ),
        ]
    )
    route = {
        "request_id": "R1",
        "channel_id": "CH1",
        "session_id": "S1",
        "service_id": "default",
        "agent_id": "default",
    }
    artifact = dt._ProgressArtifact(Path("/private/progress"), (1, 2), (3, 4))
    remove = Mock()
    manager = Mock()
    patches = _stream_patches(proc, route=route)
    try:
        with ExitStack() as stack:
            for item in patches:
                stack.enter_context(item)
            stack.enter_context(patch.object(dt, "WebSocketGatewayPushTransport", return_value=AsyncMock()))
            stack.enter_context(patch.object(dt, "get_deepresearch_manager", return_value=manager))
            stack.enter_context(patch.object(dt, "_create_progress_artifact", return_value=artifact))
            stack.enter_context(patch.object(dt, "_remove_progress_artifact", remove))
            outcome = json.loads(
                await dt.deepresearch_stream._func(action="start", query="q", file_name="report")
            )

        assert outcome["status"] == "interrupted"
        assert outcome["node_id"] == "outline_interaction"
        assert "interaction_policy" not in outcome
        marker = outcome["marker"]
        assert "## 页面规划" in marker["outline"]
        assert "### P1: 架构设计（重点）" in marker["outline"]
        assert "### P2: 部署方案" in marker["outline"]
        assert marker["preview"]["text"] == marker["outline"]
        cached = dt._get_cached_outline_json(route, "C1")
        assert cached["title"] == "AI Agent 架构"
        assert len(cached["sections"]) == 2
        assert cached["sections"][0]["is_core_section"] is True
        assert cached["sections"][0]["description"] == "核心架构"
        assert cached["sections"][1]["format_requirements"] == "markdown"
    finally:
        with dt._OUTLINE_JSON_CACHES_GUARD:
            dt._OUTLINE_JSON_CACHES.clear()
        with dt._OUTLINE_TITLE_CACHES_GUARD:
            dt._OUTLINE_TITLE_CACHES.clear()


@pytest.mark.asyncio
async def test_outline_interrupt_handles_unparseable_outline_parts(tmp_path: Path):
    """Unparseable state.outline_parts produces empty marker.outline and empty preview, still interrupted."""
    proc = _Proc(
        [
            json.dumps({"__deepsearch_status__": "started", "conversation_id": "C1"}),
            json.dumps({"agent": "outline", "content": "not valid json"}, ensure_ascii=False),
            json.dumps(
                {
                    "__deepsearch_status__": "interrupted",
                    "agent": "outline_interaction",
                    "conversation_id": "C1",
                },
                ensure_ascii=False,
            ),
        ]
    )
    route = {
        "request_id": "R1",
        "channel_id": "CH1",
        "session_id": "S1",
        "service_id": "default",
        "agent_id": "default",
    }
    artifact = dt._ProgressArtifact(Path("/private/progress"), (1, 2), (3, 4))
    remove = Mock()
    manager = Mock()
    patches = _stream_patches(proc, route=route)
    try:
        with ExitStack() as stack:
            for item in patches:
                stack.enter_context(item)
            stack.enter_context(patch.object(dt, "WebSocketGatewayPushTransport", return_value=AsyncMock()))
            stack.enter_context(patch.object(dt, "get_deepresearch_manager", return_value=manager))
            stack.enter_context(patch.object(dt, "_create_progress_artifact", return_value=artifact))
            stack.enter_context(patch.object(dt, "_remove_progress_artifact", remove))
            outcome = json.loads(
                await dt.deepresearch_stream._func(action="start", query="q", file_name="report")
            )

        assert outcome["status"] == "interrupted"
        assert outcome["node_id"] == "outline_interaction"
        assert "interaction_policy" not in outcome
        marker = outcome["marker"]
        assert marker["outline"] == ""
        assert marker["preview"] == {"text": ""}
        cached = dt._get_cached_outline_json(route, "C1")
        assert cached == {}
    finally:
        with dt._OUTLINE_JSON_CACHES_GUARD:
            dt._OUTLINE_JSON_CACHES.clear()
        with dt._OUTLINE_TITLE_CACHES_GUARD:
            dt._OUTLINE_TITLE_CACHES.clear()


@pytest.mark.asyncio
async def test_feedback_handler_interrupt_behavior_unchanged(tmp_path: Path):
    """feedback_handler interrupt still returns interrupted outcome with questions in marker — regression check."""
    proc = _Proc(
        [
            json.dumps({"__deepsearch_status__": "started", "conversation_id": "C1"}),
            json.dumps(
                {
                    "__deepsearch_status__": "interrupted",
                    "agent": "feedback_handler",
                    "conversation_id": "C1",
                    "questions": [{"id": "q1", "question": "什么?"}],
                },
                ensure_ascii=False,
            ),
        ]
    )
    route = {
        "request_id": "R1",
        "channel_id": "CH1",
        "session_id": "S1",
        "service_id": "default",
        "agent_id": "default",
    }
    artifact = dt._ProgressArtifact(Path("/private/progress"), (1, 2), (3, 4))
    remove = Mock()
    manager = Mock()
    patches = _stream_patches(proc, route=route)
    with ExitStack() as stack:
        for item in patches:
            stack.enter_context(item)
        stack.enter_context(patch.object(dt, "WebSocketGatewayPushTransport", return_value=AsyncMock()))
        stack.enter_context(patch.object(dt, "get_deepresearch_manager", return_value=manager))
        stack.enter_context(patch.object(dt, "_create_progress_artifact", return_value=artifact))
        stack.enter_context(patch.object(dt, "_remove_progress_artifact", remove))
        outcome = json.loads(
            await dt.deepresearch_stream._func(action="start", query="q", file_name="report")
        )

    assert outcome["status"] == "interrupted"
    assert outcome["node_id"] == "feedback_handler"
    assert "interaction_policy" not in outcome
    marker = outcome["marker"]
    assert marker["questions"] == [{"id": "q1", "question": "什么?"}]
    assert "preview" not in marker


@pytest.mark.asyncio
async def test_repeated_outline_interrupt_returns_loop_error():
    proc = _Proc(
        [
            json.dumps({"__deepsearch_status__": "resuming", "conversation_id": "C1"}),
            json.dumps(
                {
                    "__deepsearch_status__": "interrupted",
                    "agent": "outline_interaction",
                    "conversation_id": "C1",
                }
            ),
        ]
    )
    artifact = dt._ProgressArtifact(Path("/private/progress"), (1, 2), (3, 4))
    remove = Mock()
    manager = Mock()
    patches = _stream_patches(proc)
    with ExitStack() as stack:
        for item in patches:
            stack.enter_context(item)
        stack.enter_context(patch.object(dt, "WebSocketGatewayPushTransport", return_value=AsyncMock()))
        stack.enter_context(patch.object(dt, "get_deepresearch_manager", return_value=manager))
        stack.enter_context(patch.object(dt, "_create_progress_artifact", return_value=artifact))
        stack.enter_context(patch.object(dt, "_remove_progress_artifact", remove))
        outcome = json.loads(
            await dt.deepresearch_stream._func(
                action="resume",
                conversation_id="C1",
                node="outline_interaction",
                interaction_result=(
                    '{"status":"answered","answers":[{"question":"q",'
                    '"selected_options":["outline_confirm"],"custom_input":null}]}'
                ),
            )
        )

    assert outcome["error_code"] == "outline_auto_resume_loop"


@pytest.mark.asyncio
async def test_completed_marker_requires_nonempty_report():
    proc = _Proc(
        [
            json.dumps(
                {
                    "__deepsearch_status__": "completed",
                    "conversation_id": "C1",
                    "final_result": {"response_content": ""},
                }
            )
        ]
    )
    patches = _stream_patches(proc)
    with ExitStack() as stack:
        for item in patches:
            stack.enter_context(item)
        outcome = json.loads(await dt.deepresearch_stream._func(action="start", query="q"))

    assert outcome["error_code"] == "empty_report"


@pytest.mark.asyncio
async def test_completed_marker_requires_all_section_progress():
    proc = _Proc(
        [
            json.dumps(
                {
                    "__deepsearch_status__": "completed",
                    "conversation_id": "C1",
                    "final_result": {"response_content": "report"},
                }
            )
        ]
    )
    patches = _stream_patches(proc)
    with ExitStack() as stack:
        for item in patches:
            stack.enter_context(item)
        outcome = json.loads(await dt.deepresearch_stream._func(action="start", query="q"))

    assert outcome["error_code"] == "incomplete_section_progress"


@pytest.mark.asyncio
async def test_completed_marker_accepts_p_numbered_sections_beneath_wrapper_heading(
    tmp_path: Path,
):
    sdk_usage = {
        "input_tokens": 120,
        "output_tokens": 30,
        "total_tokens": 150,
        "llm_call_count": 2,
        "agent_name_token_usage": [],
    }
    outline = (
        "# 大纲：用户需求洞察报告\n"
        "## 页面规划\n"
        "### P1: 用户画像构建（重点）\n"
        "### P2: 行为习惯分析（重点）\n"
        "### P3: 真实痛点挖掘（重点）\n"
        "### P4: 消费决策逻辑剖析（重点）\n"
        "### P5: 潜在需求识别与产品设计建议（重点）"
    )
    lines = [
        json.dumps({"__deepsearch_status__": "started", "conversation_id": "C1"}),
        json.dumps({"agent": "outline", "content": outline}),
        *[
            json.dumps(
                {
                    "agent": "sub_reporter",
                    "section_idx": str(index),
                    "section_total": 5,
                    "event": "done",
                }
            )
            for index in range(1, 6)
        ],
        json.dumps(
            {
                "__deepsearch_status__": "completed",
                "conversation_id": "C1",
                "final_result": {
                    "response_content": "# Final",
                    "workflow_llm_token_usage": sdk_usage,
                },
            }
        ),
    ]
    proc = _Proc(lines)
    route = {
        "request_id": "R1",
        "channel_id": "CH1",
        "session_id": "S1",
        "service_id": "default",
        "agent_id": "default",
    }
    patches = _stream_patches(proc, route=route)
    with ExitStack() as stack:
        for item in patches:
            stack.enter_context(item)
        stack.enter_context(
            patch.object(dt, "WebSocketGatewayPushTransport", return_value=AsyncMock())
        )
        stack.enter_context(
            patch.object(
                dt,
                "_write_report_artifacts_stream",
                new=AsyncMock(return_value={"md": str(tmp_path / "r.md")}),
            )
        )
        outcome = json.loads(
            await dt.deepresearch_stream._func(action="start", query="q")
        )

    assert outcome == {
        "status": "completed",
        "conversation_id": "C1",
        "report_delivered": True,
        "report_chars": len("# Final"),
        "workflow_llm_token_usage": sdk_usage,
    }


@pytest.mark.asyncio
async def test_completed_marker_accepts_sdk_final_report_after_degraded_section(
    tmp_path: Path,
):
    proc = _Proc(
        [
            json.dumps({"__deepsearch_status__": "started", "conversation_id": "C1"}),
            json.dumps(
                {
                    "agent": "sub_reporter",
                    "section_idx": "1",
                    "section_total": 2,
                    "event": "done",
                }
            ),
            json.dumps(
                {
                    "agent": "source_tracer",
                    "section_idx": "0",
                    "event": "done",
                    "content": "validating citations",
                }
            ),
            json.dumps(
                {
                    "__deepsearch_status__": "completed",
                    "conversation_id": "C1",
                    "final_result": {
                        "response_content": "# Final",
                        "warning_info": "one section degraded",
                    },
                }
            ),
        ]
    )
    route = {
        "request_id": "R1",
        "channel_id": "CH1",
        "session_id": "S1",
        "service_id": "default",
        "agent_id": "default",
    }
    patches = _stream_patches(proc, route=route)
    with ExitStack() as stack:
        for item in patches:
            stack.enter_context(item)
        stack.enter_context(
            patch.object(dt, "WebSocketGatewayPushTransport", return_value=AsyncMock())
        )
        stack.enter_context(
            patch.object(
                dt,
                "_write_report_artifacts_stream",
                new=AsyncMock(return_value={"md": str(tmp_path / "r.md")}),
            )
        )
        outcome = json.loads(
            await dt.deepresearch_stream._func(action="start", query="q")
        )

    assert outcome == {
        "status": "completed",
        "conversation_id": "C1",
        "report_delivered": True,
        "report_chars": len("# Final"),
    }


@pytest.mark.asyncio
async def test_completed_marker_accepts_formal_sdk_end_result_when_section_done_is_missing(
    tmp_path: Path,
):
    """The SDK's successful EndNode result is the authoritative workflow boundary."""
    final_result = {"response_content": "# Final"}
    proc = _Proc(
        [
            json.dumps({"__deepsearch_status__": "started", "conversation_id": "C1"}),
            json.dumps(
                {
                    "agent": "sub_reporter",
                    "section_idx": "1",
                    "section_total": 2,
                    "event": "done",
                }
            ),
            json.dumps(
                {
                    "agent": "reporter",
                    "section_idx": "0",
                    "event": "start",
                    "content": "assembling report",
                }
            ),
            json.dumps(
                {
                    "agent": "end",
                    "section_idx": "0",
                    "event": "summary_response",
                    "content": json.dumps(final_result),
                }
            ),
            json.dumps(
                {
                    "__deepsearch_status__": "completed",
                    "conversation_id": "C1",
                    "final_result": final_result,
                }
            ),
        ]
    )
    route = {
        "request_id": "R1",
        "channel_id": "CH1",
        "session_id": "S1",
        "service_id": "default",
        "agent_id": "default",
    }
    patches = _stream_patches(proc, route=route)
    with ExitStack() as stack:
        for item in patches:
            stack.enter_context(item)
        stack.enter_context(
            patch.object(dt, "WebSocketGatewayPushTransport", return_value=AsyncMock())
        )
        stack.enter_context(
            patch.object(
                dt,
                "_write_report_artifacts_stream",
                new=AsyncMock(return_value={"md": str(tmp_path / "r.md")}),
            )
        )
        outcome = json.loads(
            await dt.deepresearch_stream._func(action="start", query="q")
        )

    assert outcome == {
        "status": "completed",
        "conversation_id": "C1",
        "report_delivered": True,
        "report_chars": len("# Final"),
    }


@pytest.mark.asyncio
async def test_completed_marker_delivers_large_sdk_end_result(tmp_path: Path):
    """Large final results are terminal artifacts, not process-display text."""
    final_result = {
        "response_content": "# Final",
        "content": "x" * MAX_CHUNK_TEXT_CHARS,
    }
    end_content = json.dumps(final_result)
    proc = _Proc(
        [
            json.dumps({"__deepsearch_status__": "started", "conversation_id": "C1"}),
            json.dumps(
                {
                    "agent": "end",
                    "section_idx": "0",
                    "event": "summary_response",
                    "content": end_content,
                }
            ),
            json.dumps(
                {
                    "__deepsearch_status__": "completed",
                    "conversation_id": "C1",
                    "final_result": final_result,
                }
            ),
        ]
    )
    route = {
        "request_id": "R1",
        "channel_id": "CH1",
        "session_id": "S1",
        "service_id": "default",
        "agent_id": "default",
    }
    patches = _stream_patches(proc, route=route)

    with ExitStack() as stack:
        for item in patches:
            stack.enter_context(item)
        stack.enter_context(
            patch.object(dt, "WebSocketGatewayPushTransport", return_value=AsyncMock())
        )
        write_artifacts = stack.enter_context(
            patch.object(
                dt,
                "_write_report_artifacts_stream",
                new=AsyncMock(return_value={"md": str(tmp_path / "r.md")}),
            )
        )
        outcome = json.loads(
            await dt.deepresearch_stream._func(action="start", query="q")
        )

    assert len(end_content) > MAX_CHUNK_TEXT_CHARS
    assert outcome == {
        "status": "completed",
        "conversation_id": "C1",
        "report_delivered": True,
        "report_chars": len("# Final"),
    }
    write_artifacts.assert_awaited_once()


@pytest.mark.asyncio
async def test_completed_marker_rejects_sdk_end_result_with_exception() -> None:
    final_result = {
        "response_content": "# Partial",
        "exception_info": "report workflow failed",
    }
    proc = _Proc(
        [
            json.dumps({"__deepsearch_status__": "started", "conversation_id": "C1"}),
            json.dumps(
                {
                    "agent": "reporter",
                    "section_idx": "0",
                    "event": "start",
                    "content": "assembling report",
                }
            ),
            json.dumps(
                {
                    "agent": "end",
                    "section_idx": "0",
                    "event": "summary_response",
                    "content": json.dumps(final_result),
                }
            ),
            json.dumps(
                {
                    "__deepsearch_status__": "completed",
                    "conversation_id": "C1",
                    "final_result": final_result,
                }
            ),
        ]
    )
    patches = _stream_patches(proc)
    with ExitStack() as stack:
        for item in patches:
            stack.enter_context(item)
        outcome = json.loads(
            await dt.deepresearch_stream._func(action="start", query="q")
        )

    assert outcome["error_code"] == "incomplete_section_progress"


@pytest.mark.asyncio
async def test_completed_marker_rejects_final_pipeline_that_only_started(tmp_path: Path):
    proc = _Proc(
        [
            json.dumps({"__deepsearch_status__": "started", "conversation_id": "C1"}),
            json.dumps(
                {
                    "agent": "sub_reporter",
                    "section_idx": "1",
                    "section_total": 2,
                    "event": "done",
                }
            ),
            json.dumps(
                {
                    "agent": "source_tracer",
                    "section_idx": "0",
                    "event": "start",
                    "content": "validating citations",
                }
            ),
            json.dumps(
                {
                    "__deepsearch_status__": "completed",
                    "conversation_id": "C1",
                    "final_result": {"response_content": "# Final"},
                }
            ),
        ]
    )
    patches = _stream_patches(proc)
    with ExitStack() as stack:
        for item in patches:
            stack.enter_context(item)
        outcome = json.loads(
            await dt.deepresearch_stream._func(action="start", query="q")
        )

    assert outcome["error_code"] == "incomplete_section_progress"


@pytest.mark.asyncio
async def test_completed_report_delivers_markdown_html_and_hidden_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        session_history,
        "get_agent_sessions_dir",
        lambda: tmp_path / "sessions",
    )
    final_result = {"response_content": "# Final", "infer_messages": [], "chart_messages": []}
    proc = _Proc(
        [
            json.dumps({"__deepsearch_status__": "started", "conversation_id": "C1"}),
            json.dumps(
                {
                    "agent": "sub_reporter",
                    "section_idx": "1",
                    "section_total": 1,
                    "event": "done",
                }
            ),
            json.dumps(
                {
                    "__deepsearch_status__": "completed",
                    "conversation_id": "C1",
                    "final_result": final_result,
                    "raw_report_path": "/hidden/raw.md",
                    "citations_preview_path": "/hidden/citations.preview.json",
                    "citations_path": "/must/not/expose.json",
                }
            ),
        ]
    )
    route = {
        "request_id": "R1",
        "channel_id": "CH1",
        "session_id": "S1",
        "service_id": "default",
        "agent_id": "default",
    }
    push = AsyncMock()
    write_report = AsyncMock(
        return_value=(
            {"md": str(tmp_path / "r.md"), "html": str(tmp_path / "r.html")},
            "fallback",
            "invoke_llm",
            "llm_call_failed",
        )
    )
    patches = _stream_patches(proc, route=route)
    with ExitStack() as stack:
        for item in patches:
            stack.enter_context(item)
        stack.enter_context(patch.object(dt, "WebSocketGatewayPushTransport", return_value=push))
        stack.enter_context(
            patch.object(dt, "_write_report_artifacts_stream", new=write_report)
        )
        outcome = json.loads(
            await dt.deepresearch_stream._func(action="start", query="q", file_name="r")
        )

    assert outcome == {
        "status": "completed",
        "conversation_id": "C1",
        "report_delivered": True,
        "report_chars": len("# Final"),
        "html_style_status": "fallback",
        "html_style_phase": "invoke_llm",
        "html_style_reason_code": "llm_call_failed",
    }
    file_payload = next(
        call.args[0]["payload"]
        for call in push.send_push.await_args_list
        if call.args[0]["payload"].get("event_type") == "chat.file"
    )
    assert [item["name"] for item in file_payload["files"]] == ["r.md", "r.html"]
    assert file_payload["metadata"]["htmlStyleStatus"] == "fallback"
    assert file_payload["metadata"]["htmlStylePhase"] == "invoke_llm"
    assert file_payload["metadata"]["htmlStyleReasonCode"] == "llm_call_failed"
    serialized = json.dumps(file_payload)
    assert "/hidden/raw.md" in serialized
    assert "/hidden/citations.preview.json" in serialized
    assert "/must/not/expose.json" not in serialized

    file_records = [
        item
        for item in session_history.load_history_records("S1")
        if item.get("event_type") == "chat.file"
    ]
    assert len(file_records) == 1
    assert file_records[0]["request_id"] == "R1"
    assert file_records[0]["channel_id"] == "CH1"
    assert file_records[0]["files"] == file_payload["files"]
    assert file_records[0]["metadata"] == file_payload["metadata"]


@pytest.mark.asyncio
async def test_final_report_idle_stream_emits_processing_heartbeat_without_cancelling_reader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    initial = b"".join(
        (
            json.dumps(
                {"__deepsearch_status__": "started", "conversation_id": "C1"}
            ).encode("utf-8")
            + b"\n",
            json.dumps(
                {
                    "agent": "sub_reporter",
                    "section_idx": "1",
                    "section_total": 1,
                    "event": "done",
                }
            ).encode("utf-8")
            + b"\n",
        )
    )
    terminal = (
        json.dumps(
            {
                "__deepsearch_status__": "completed",
                "conversation_id": "C1",
                "final_result": {"response_content": "# Final"},
            }
        ).encode("utf-8")
        + b"\n"
    )
    reader = _DelayedFinalReportReader(
        initial,
        terminal,
        delay=0.08,
        initial_delay=0.06,
    )
    proc = _Proc()
    proc.stdout = reader
    route = {
        "request_id": "R1",
        "channel_id": "CH1",
        "session_id": "S1",
        "service_id": "default",
        "agent_id": "default",
    }
    push = AsyncMock()
    heartbeat_observations: list[tuple[str, int]] = []
    phase = "stream"

    async def record_push(envelope: dict[str, Any]) -> None:
        payload = envelope["payload"]
        if (
            payload.get("event_type") == "chat.processing_status"
            and payload.get("current_task") == "in_progress"
        ):
            heartbeat_observations.append((phase, reader.reads))

    push.send_push.side_effect = record_push

    async def delayed_write(*_args: Any, **_kwargs: Any) -> dict[str, str]:
        nonlocal phase
        phase = "artifact"
        await asyncio.sleep(0.08)
        return {"md": str(tmp_path / "r.md")}

    monkeypatch.setattr(
        dt,
        "DEEPRESEARCH_FINAL_REPORT_PROGRESS_INTERVAL_SECONDS",
        0.02,
        raising=False,
    )
    patches = _stream_patches(proc, route=route)
    with ExitStack() as stack:
        for item in patches:
            stack.enter_context(item)
        stack.enter_context(
            patch.object(dt, "WebSocketGatewayPushTransport", return_value=push)
        )
        stack.enter_context(
            patch.object(
                dt,
                "_write_report_artifacts_stream",
                new=delayed_write,
            )
        )
        outcome = json.loads(
            await dt.deepresearch_stream._func(action="start", query="q")
        )

    heartbeats = [
        call.args[0]["payload"]
        for call in push.send_push.await_args_list
        if call.args[0]["payload"].get("event_type") == "chat.processing_status"
        and call.args[0]["payload"].get("current_task") == "in_progress"
    ]
    assert len(heartbeats) >= 4
    stream_heartbeat_reads = {
        read_number
        for observed_phase, read_number in heartbeat_observations
        if observed_phase == "stream"
    }
    assert stream_heartbeat_reads == {2}
    assert any(
        observed_phase == "artifact"
        for observed_phase, _read_number in heartbeat_observations
    )
    assert reader.cancelled is False
    assert outcome["status"] == "completed"
    assert outcome["report_delivered"] is True


def test_offline_html_converter_sanitizes_active_content(tmp_path: Path):
    from jiuwenswarm.agents.harness.common.tools.deepresearch_plugin.convert_html_offline import (
        convert_md_to_html,
    )

    source = tmp_path / "report.md"
    target = tmp_path / "report.html"
    source.write_text(
        '# Report\n\n<script>alert(1)</script>\n\n<a href="javascript:alert(2)" onclick="x()">bad</a>',
        encoding="utf-8",
    )
    convert_md_to_html(source, target)
    rendered = target.read_text(encoding="utf-8").lower()
    assert "<script>alert(1)</script>" not in rendered
    assert "javascript:alert(2)" not in rendered
    assert "onclick=" not in rendered


@pytest.mark.parametrize(
    "attack",
    [
        '<a href="&#x6a;avascript:alert(1)">entity</a>',
        '<a href="java&#10;script:alert(2)">control</a>',
        '<svg><a xlink:href="javascript:alert(3)">svg-secret</a></svg>',
        '<math><mtext href="javascript:alert(4)">math-secret</mtext></math>',
        '<iframe srcdoc="<script>alert(5)</script>">frame-secret</iframe>',
        '<img src="https://safe.invalid/x" style="x" onerror="alert(6)" '
        'srcdoc="bad"><script><b>malformed-secret',
    ],
)
def test_offline_html_allowlist_blocks_parser_bypass_payloads(
    tmp_path: Path, attack: str
):
    from jiuwenswarm.agents.harness.common.tools.deepresearch_plugin.convert_html_offline import (
        convert_md_to_html,
    )

    source = tmp_path / "report.md"
    target = tmp_path / "report.html"
    source.write_text(f"# Report\n\n{attack}\n", encoding="utf-8")
    convert_md_to_html(source, target)
    body = target.read_text(encoding="utf-8").split("<body>", 1)[1]
    lowered = body.lower()
    for forbidden in (
        "javascript:",
        "xlink:",
        "srcdoc=",
        "style=",
        "onerror=",
        "<svg",
        "<math",
        "<iframe",
        "alert(",
        "svg-secret",
        "math-secret",
        "frame-secret",
        "malformed-secret",
    ):
        assert forbidden not in lowered


def test_offline_html_allowlist_preserves_normal_markdown_structures(
    tmp_path: Path,
):
    from jiuwenswarm.agents.harness.common.tools.deepresearch_plugin.convert_html_offline import (
        convert_md_to_html,
    )

    source = tmp_path / "report.md"
    target = tmp_path / "report.html"
    source.write_text(
        "# Report\n\n[link](https://example.invalid/path)\n\n"
        "![alt](/images/chart.png)\n\n| A | B |\n|---|---|\n| 1 | 2 |\n",
        encoding="utf-8",
    )
    convert_md_to_html(source, target)
    body = target.read_text(encoding="utf-8").split("<body>", 1)[1]
    assert '<a href="https://example.invalid/path"' in body
    assert '<img alt="alt" src="/images/chart.png">' in body
    assert "<table>" in body and "<th>A</th>" in body and "<td>1</td>" in body


def test_offline_html_converter_rejects_symlink_input(tmp_path: Path):
    from jiuwenswarm.agents.harness.common.tools.deepresearch_plugin.convert_html_offline import (
        convert_md_to_html,
    )

    real = tmp_path / "real.md"
    real.write_text("secret", encoding="utf-8")
    source = tmp_path / "report.md"
    source.symlink_to(real)
    with pytest.raises(OSError, match="unsafe Markdown input"):
        convert_md_to_html(source, tmp_path / "report.html")


def test_offline_html_converter_never_overwrites_existing_or_symlink_output(
    tmp_path: Path,
):
    from jiuwenswarm.agents.harness.common.tools.deepresearch_plugin.convert_html_offline import (
        convert_md_to_html,
    )

    source = tmp_path / "report.md"
    source.write_text("# report", encoding="utf-8")
    protected = tmp_path / "protected.html"
    protected.write_text("keep", encoding="utf-8")
    output = tmp_path / "report.html"
    output.symlink_to(protected)
    with pytest.raises(FileExistsError):
        convert_md_to_html(source, output)
    assert protected.read_text(encoding="utf-8") == "keep"


def test_offline_converter_has_no_sdk_import():
    import inspect

    from jiuwenswarm.agents.harness.common.tools.deepresearch_plugin import (
        convert_html_offline,
    )

    assert "openjiuwen_deepsearch" not in inspect.getsource(convert_html_offline)


def test_styled_zip_member_count_and_size_are_bounded(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(dt, "DEEPRESEARCH_ZIP_MAX_MEMBERS", 1)
    payload = _zip_payload(
        [
            ("report_bundle/report.html", b"ok", None),
            ("report_bundle/a.txt", b"a", None),
        ]
    )
    with pytest.raises(ValueError, match="member limit"):
        dt._extract_styled_bundle(payload, tmp_path / "count")

    monkeypatch.setattr(dt, "DEEPRESEARCH_ZIP_MAX_MEMBERS", 10)
    monkeypatch.setattr(dt, "DEEPRESEARCH_ZIP_MEMBER_MAX_BYTES", 1)
    with pytest.raises(ValueError, match="member limit"):
        dt._extract_styled_bundle(
            _zip_payload([("report_bundle/report.html", b"too large", None)]),
            tmp_path / "size",
        )


@pytest.mark.asyncio
async def test_report_publication_writes_markdown_html_snapshot_and_provenance(
    tmp_path: Path,
):
    final_result = {
        "response_content": "# Report\n\nClaim [1].",
        "citations": [{"id": 1, "url": "https://example.invalid/source"}],
        "infer_messages": [],
        "chart_messages": [],
    }
    citation_artifacts = {
        "raw_report_path": "/hidden/raw.md",
        "citations_preview_path": "/hidden/citations.preview.json",
    }
    with patch.object(dt, "get_cwd", return_value=str(tmp_path)):
        (
            artifacts,
            html_style_status,
            html_style_phase,
            html_style_reason_code,
        ) = await dt._write_report_artifacts_stream(
            final_result, "Research", "C1", citation_artifacts
        )

    assert set(artifacts) == {"md", "html"}
    assert html_style_status == "fallback"
    assert html_style_phase is None
    assert html_style_reason_code is None
    markdown = Path(artifacts["md"])
    assert markdown.read_text(encoding="utf-8").startswith("# Report")
    assert Path(artifacts["html"]).read_text(encoding="utf-8").startswith("<!DOCTYPE html>")
    provenance = json.loads(markdown.with_suffix(".provenance.json").read_text())
    snapshot = json.loads(markdown.with_suffix(".final-result.json").read_text())
    assert provenance["conversation_id"] == "C1"
    assert provenance["citation_artifacts"] == citation_artifacts
    assert provenance["content_sha256"]
    assert snapshot["response_content"] == final_result["response_content"]


@pytest.mark.asyncio
async def test_html_failure_keeps_published_markdown(tmp_path: Path):
    final_result = {
        "response_content": "# Report",
        "infer_messages": [],
        "chart_messages": [],
    }
    with patch.object(dt, "get_cwd", return_value=str(tmp_path)), patch.object(
        dt, "_generate_report_html", new=AsyncMock(return_value=None)
    ):
        (
            artifacts,
            html_style_status,
            html_style_phase,
            html_style_reason_code,
        ) = await dt._write_report_artifacts_stream(
            final_result, "Research", "C1"
        )

    assert set(artifacts) == {"md"}
    assert html_style_status is None
    assert html_style_phase is None
    assert html_style_reason_code is None
    assert Path(artifacts["md"]).is_file()


@pytest.mark.asyncio
async def test_progress_file_is_precreated_in_private_owned_directory():
    proc = _Proc([json.dumps({"__deepsearch_status__": "error", "error": "done"})])
    observations: list[tuple[Path, os.stat_result, os.stat_result]] = []

    async def inspect_spawn(*args, **kwargs):
        del kwargs
        progress = Path(args[args.index("--progress-file") + 1])
        observations.append((progress, progress.parent.lstat(), progress.lstat()))
        return proc

    import stat

    with patch.object(dt, "resolve_python_executable", return_value=Path("/runtime/bin/python")), patch.object(
        dt, "_resolve_run_script", return_value="/runner"
    ), patch.object(dt, "_build_deepresearch_request_config", return_value=_valid_config()), patch.object(
        dt, "_build_deepresearch_child_env", return_value={"PATH": "/runtime/bin"}
    ), patch("asyncio.create_subprocess_exec", new=AsyncMock(side_effect=inspect_spawn)):
        await dt.deepresearch_stream._func(action="start", query="q")

    assert len(observations) == 1
    progress, parent, leaf = observations[0]
    assert progress.parent.parent == Path(__import__("tempfile").gettempdir())
    assert stat.S_ISDIR(parent.st_mode)
    assert stat.S_IMODE(parent.st_mode) == 0o700
    assert stat.S_ISREG(leaf.st_mode)
    assert leaf.st_nlink == 1
    assert stat.S_IMODE(leaf.st_mode) == 0o600


@pytest.mark.asyncio
async def test_concurrent_same_title_report_publication_allocates_distinct_outputs(
    tmp_path: Path,
):
    final_result = {
        "response_content": "# Report",
        "infer_messages": [],
        "chart_messages": [],
    }
    with patch.object(dt, "get_cwd", return_value=str(tmp_path)):
        first_result, second_result = await asyncio.gather(
            dt._write_report_artifacts_stream(final_result, "Same", "C1"),
            dt._write_report_artifacts_stream(final_result, "Same", "C2"),
        )

    first, _, _, _ = first_result
    second, _, _, _ = second_result
    assert first["md"] != second["md"]
    assert Path(first["md"]).is_file()
    assert Path(second["md"]).is_file()
    assert dt._REPORT_OUTPUT_LOCKS == {}


def test_report_publication_fails_closed_when_output_root_is_swapped(
    tmp_path: Path,
):
    from jiuwenswarm.agents.harness.common.tools.deepresearch_plugin import report_bundle

    workspace = tmp_path / "workspace"
    moved = tmp_path / "moved-workspace"
    attacker = tmp_path / "attacker"
    original = report_bundle.build_report_bundle

    def build_then_swap(*args, **kwargs):
        bundle = original(*args, **kwargs)
        workspace.rename(moved)
        attacker.mkdir()
        workspace.symlink_to(attacker, target_is_directory=True)
        return bundle

    final_result = {
        "response_content": "# Report",
        "infer_messages": [],
        "chart_messages": [],
    }
    with patch.object(dt, "get_cwd", return_value=str(workspace)), patch.object(
        report_bundle, "build_report_bundle", side_effect=build_then_swap
    ):
        with pytest.raises(OSError, match="output root"):
            dt._write_report_markdown(final_result, "Report", "C1")

    assert list(attacker.iterdir()) == []


def test_route_output_root_symlink_is_rejected_without_outside_write(
    tmp_path: Path,
):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    outside.mkdir()
    workspace.symlink_to(outside, target_is_directory=True)
    route_token = dt.push_deepresearch_route(
        "request",
        "channel",
        "session",
        output_dir=str(workspace),
    )
    final_result = {
        "response_content": "# Report",
        "infer_messages": [],
        "chart_messages": [],
    }
    try:
        with pytest.raises(OSError, match="output root"):
            dt._write_report_markdown(final_result, "Report", "C1")
    finally:
        dt.reset_deepresearch_route(route_token)

    assert list(outside.iterdir()) == []


def test_windows_output_root_reparse_point_is_rejected(tmp_path: Path):
    metadata = SimpleNamespace(
        st_mode=stat.S_IFDIR | 0o700,
        st_dev=1,
        st_ino=2,
        st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT,
    )
    with patch.object(dt, "_uses_windows_path_publication", return_value=True), patch.object(
        Path, "lstat", return_value=metadata
    ):
        with pytest.raises(OSError, match="output root"):
            dt._open_output_root(tmp_path)


def test_progress_artifact_accepts_windows_synthetic_mode_bits(
    tmp_path: Path, monkeypatch
):
    directory = tmp_path / "deepresearch-progress"
    directory.mkdir(mode=0o700)
    original_lstat = Path.lstat
    original_fstat = os.fstat

    def windows_lstat(path: Path):
        metadata = original_lstat(path)
        if path == directory:
            return SimpleNamespace(
                st_mode=stat.S_IFDIR | 0o777,
                st_dev=metadata.st_dev,
                st_ino=metadata.st_ino,
                st_file_attributes=0,
            )
        return metadata

    def windows_fstat(descriptor: int):
        metadata = original_fstat(descriptor)
        return SimpleNamespace(
            st_mode=stat.S_IFREG | 0o666,
            st_dev=metadata.st_dev,
            st_ino=metadata.st_ino,
            st_nlink=1,
            st_file_attributes=0,
        )

    monkeypatch.setattr(dt.tempfile, "mkdtemp", lambda **_kwargs: str(directory))
    monkeypatch.setattr(Path, "lstat", windows_lstat)
    monkeypatch.setattr(dt.os, "fstat", windows_fstat)

    artifact = dt._create_progress_artifact()
    try:
        assert artifact.path == directory / "progress.jsonl"
    finally:
        dt._remove_progress_artifact(artifact)


def test_build_deepresearch_config_maps_only_required_values():
    source = {
        "MODEL_NAME": "model",
        "MODEL_PROVIDER": "OpenRouter",
        "API_BASE": "https://llm.invalid/v1",
        "API_KEY": "llm-key",
        "BOCHA_API_KEY": "search-key",
        "UNRELATED_SECRET": "must-not-copy",
        "TOOL_SSL_VERIFY": "true",
    }
    config = dt._build_deepresearch_config(source)
    assert config["LLM_MODEL_NAME"] == "model"
    assert config["LLM_MODEL_TYPE"] == "openai"
    assert config["LLM_BASE_URL"] == "https://llm.invalid/v1"
    assert config["LLM_API_KEY"] == "llm-key"
    assert config["WEB_SEARCH_ENGINE_NAME"] == "bocha"
    assert config["WEB_SEARCH_API_KEY"] == "search-key"
    assert config["TOOL_SSL_VERIFY"] == "true"
    assert "UNRELATED_SECRET" not in config


@pytest.mark.parametrize(
    "source",
    [
        {"WEB_SEARCH_ENGINE_NAME": "petal", "WEB_SEARCH_API_KEY": "key"},
        {"WEB_SEARCH_ENGINE_NAME": "petal", "WEB_SEARCH_URL": "https://search"},
        {"WEB_SEARCH_ENGINE_NAME": "bocha"},
    ],
)
def test_build_deepresearch_config_rejects_partial_search_by_omission(source):
    config = dt._build_deepresearch_config(source)
    assert dt._validate_deepresearch_search_config(config) is not None


def test_build_deepresearch_config_empty_values_are_not_forwarded():
    config = dt._build_deepresearch_config(
        {"LLM_API_KEY": "", "BOCHA_API_KEY": "search-key"}
    )
    assert "LLM_API_KEY" not in config
    assert all(value != "" for value in config.values())


def test_styled_export_auth_carries_only_maas_authorization():
    authorization = "Basic c3R5bGUtcmVxdWVzdA=="
    auth = dt._build_styled_export_llm_auth(
        {
            "default_headers": json.dumps(
                {"authorization": authorization, "X-Unrelated": "must-not-cross"}
            )
        },
        {"general": {"api_key": "huawei-maas-session"}},
    )

    assert json.loads(auth["default_headers"]) == {
        "Authorization": authorization
    }
    assert "X-Unrelated" not in auth["default_headers"]


def test_styled_export_auth_does_not_apply_to_ordinary_api_keys():
    assert dt._build_styled_export_llm_auth(
        {"default_headers": "not-json"},
        {"general": {"api_key": "ordinary-api-key"}},
    ) == {}


@pytest.mark.parametrize(
    "raw_headers",
    [
        "",
        "{}",
        '{"X-Other":"1"}',
        '{"Authorization":"Basic one","authorization":"Basic two"}',
    ],
)
def test_styled_export_auth_fails_closed_without_one_maas_authorization(
    raw_headers: str,
):
    with pytest.raises(ValueError, match="MaaS Authorization is unavailable"):
        dt._build_styled_export_llm_auth(
            {"default_headers": raw_headers},
            {"general": {"api_key": "huawei-maas-session"}},
        )


def test_resolve_runner_rejects_symlink_and_hardlink(tmp_path: Path):
    shared = tmp_path / "shared"
    scripts = shared / "deepresearch" / "scripts"
    scripts.mkdir(parents=True)
    real = tmp_path / "real-runner.py"
    real.write_text("# runner", encoding="utf-8")
    runner = scripts / "run_deepsearch.py"
    runner.symlink_to(real)
    with patch.object(dt, "get_shared_agent_skills_dirs", return_value=[shared]):
        assert dt._resolve_run_script() == ""
    runner.unlink()
    os.link(real, runner)
    with patch.object(dt, "get_shared_agent_skills_dirs", return_value=[shared]):
        assert dt._resolve_run_script() == ""


@pytest.mark.asyncio
async def test_missing_runner_and_search_config_do_not_spawn():
    spawn = AsyncMock()
    with patch.object(dt, "resolve_python_executable", return_value=Path("/runtime/bin/python")), patch.object(
        dt, "_resolve_run_script", return_value=""
    ), patch("asyncio.create_subprocess_exec", new=spawn):
        missing_runner = json.loads(await dt.deepresearch_stream._func(action="start", query="q"))
    assert missing_runner["error_code"] == "runner_missing"
    spawn.assert_not_awaited()

    with patch.object(dt, "resolve_python_executable", return_value=Path("/runtime/bin/python")), patch.object(
        dt, "_resolve_run_script", return_value="/runner"
    ), patch.object(
        dt,
        "_build_deepresearch_request_config",
        return_value={"LLM_SSL_VERIFY": "false", "TOOL_SSL_VERIFY": "false"},
    ), patch.object(dt, "_build_deepresearch_child_env", return_value={}), patch(
        "asyncio.create_subprocess_exec", new=spawn
    ):
        missing_search = json.loads(await dt.deepresearch_stream._func(action="start", query="q"))
    assert missing_search["error_code"] == "search_config_missing"
    spawn.assert_not_awaited()


@pytest.mark.asyncio
async def test_native_feedback_questions_are_not_replaced():
    proc = _Proc(
        [
            json.dumps(
                {
                    "agent": "question_generator",
                    "message_type": "message_chunk",
                    "message_id": "Q1",
                    "content": "cached",
                }
            ),
            json.dumps(
                {
                    "__deepsearch_status__": "interrupted",
                    "agent": "feedback_handler",
                    "conversation_id": "C1",
                    "questions": ["native"],
                }
            ),
        ]
    )
    patches = _stream_patches(proc)
    with ExitStack() as stack:
        for item in patches:
            stack.enter_context(item)
        outcome = json.loads(await dt.deepresearch_stream._func(action="start", query="q"))
    assert outcome["marker"]["questions"] == ["native"]


@pytest.mark.asyncio
async def test_user_feedback_interrupt_injects_accumulated_report():
    proc = _Proc(
        [
            json.dumps({"agent": "reporter", "event": "start", "content": "report body"}),
            json.dumps(
                {
                    "__deepsearch_status__": "interrupted",
                    "agent": "user_feedback_processor",
                    "conversation_id": "C1",
                }
            ),
        ]
    )
    patches = _stream_patches(proc)
    with ExitStack() as stack:
        for item in patches:
            stack.enter_context(item)
        outcome = json.loads(await dt.deepresearch_stream._func(action="start", query="q"))
    assert outcome["marker"]["report"] == "report body"


@pytest.mark.asyncio
async def test_file_delivery_failure_never_falls_back_to_report_chat(tmp_path: Path):
    proc = _Proc(
        [
            json.dumps({"__deepsearch_status__": "started", "conversation_id": "C1"}),
            json.dumps(
                {"agent": "sub_reporter", "section_idx": "1", "section_total": 1, "event": "done"}
            ),
            json.dumps(
                {
                    "__deepsearch_status__": "completed",
                    "conversation_id": "C1",
                    "final_result": {"response_content": "secret final report"},
                }
            ),
        ]
    )
    route = {
        "request_id": "R",
        "channel_id": "CH",
        "session_id": "S",
        "service_id": "default",
        "agent_id": "default",
    }
    push = AsyncMock()

    async def fail_file(message):
        if message["payload"].get("event_type") == "chat.file":
            raise RuntimeError("delivery failed")

    push.send_push.side_effect = fail_file
    persist_history = Mock()
    patches = _stream_patches(proc, route=route)
    with ExitStack() as stack:
        for item in patches:
            stack.enter_context(item)
        stack.enter_context(patch.object(dt, "WebSocketGatewayPushTransport", return_value=push))
        stack.enter_context(patch.object(dt, "append_history_record", persist_history))
        stack.enter_context(
            patch.object(
                dt,
                "_write_report_artifacts_stream",
                new=AsyncMock(return_value={"md": str(tmp_path / "r.md")}),
            )
        )
        outcome = json.loads(await dt.deepresearch_stream._func(action="start", query="q"))
    assert outcome["error_code"] == "report_file_delivery_failed"
    assert "secret final report" not in json.dumps(
        [call.args[0] for call in push.send_push.await_args_list]
    )
    persist_history.assert_not_called()


@pytest.mark.asyncio
async def test_stage_three_remains_in_progress_when_research_fails():
    proc = _Proc(
        [
            json.dumps({"__deepsearch_status__": "started", "conversation_id": "C1"}),
            json.dumps(
                {
                    "agent": "collector_info_retrieval",
                    "section_idx": "1",
                    "section_total": 1,
                    "event": "start",
                    "content": "searching",
                }
            ),
            json.dumps({"__deepsearch_status__": "error", "error": "failed"}),
        ]
    )
    route = {
        "request_id": "R",
        "channel_id": "CH",
        "session_id": "S",
        "service_id": "default",
        "agent_id": "default",
    }
    push = AsyncMock()
    patches = _stream_patches(proc, route=route)
    with ExitStack() as stack:
        for item in patches:
            stack.enter_context(item)
        stack.enter_context(patch.object(dt, "WebSocketGatewayPushTransport", return_value=push))
        outcome = json.loads(await dt.deepresearch_stream._func(action="start", query="q"))
    assert outcome["status"] == "error"
    updates = [
        call.args[0]["payload"]
        for call in push.send_push.await_args_list
        if call.args[0]["payload"].get("event_type") == "task.update"
    ]
    assert updates
    assert any(
        task["task_id"] == "deepresearch_stage_3" and task["status"] == "in_progress"
        for task in updates[-1]["tasks"]
    )


@pytest.mark.asyncio
async def test_brief_process_content_reaches_gateway_reasoning():
    detail = "证据详情：Redis 适合共享状态，SQLite 适合本地持久化。"
    proc = _Proc(
        [
            json.dumps({"__deepsearch_status__": "started", "conversation_id": "C1"}),
            json.dumps(
                {
                    "agent": "brief_info_collector",
                    "event": "message",
                    "reasoning_content": "正在判断证据覆盖范围",
                    "content": detail,
                },
                ensure_ascii=False,
            ),
            json.dumps({"__deepsearch_status__": "error", "error": "failed"}),
        ]
    )
    route = {
        "request_id": "R",
        "channel_id": "CH",
        "session_id": "S",
        "service_id": "default",
        "agent_id": "default",
    }
    push = AsyncMock()
    patches = _stream_patches(proc, route=route)
    with ExitStack() as stack:
        for item in patches:
            stack.enter_context(item)
        stack.enter_context(
            patch.object(dt, "WebSocketGatewayPushTransport", return_value=push)
        )
        outcome = json.loads(
            await dt.deepresearch_stream._func(action="start", query="q")
        )

    assert outcome["status"] == "error"
    reasoning_payloads = [
        call.args[0]["payload"]
        for call in push.send_push.await_args_list
        if call.args[0]["payload"].get("event_type") == "chat.reasoning"
    ]
    assert any(
        payload.get("task_id") == "deepresearch_stage_3"
        and payload.get("stream_source_id") == "dr_brief_info_collector"
        and payload.get("content") == detail
        for payload in reasoning_payloads
    )


@pytest.mark.asyncio
async def test_large_completed_marker_above_streamreader_limit_is_supported(tmp_path: Path):
    report = "R" * (70 * 1024)
    proc = _Proc(
        [
            json.dumps(
                {"agent": "sub_reporter", "section_idx": "1", "section_total": 1, "event": "done"}
            ),
            json.dumps(
                {
                    "__deepsearch_status__": "completed",
                    "conversation_id": "C1",
                    "final_result": {"response_content": report},
                }
            ),
        ]
    )
    route = {
        "request_id": "R",
        "channel_id": "CH",
        "session_id": "S",
        "service_id": "default",
        "agent_id": "default",
    }
    patches = _stream_patches(proc, route=route)
    with ExitStack() as stack:
        for item in patches:
            stack.enter_context(item)
        stack.enter_context(
            patch.object(dt, "WebSocketGatewayPushTransport", return_value=AsyncMock())
        )
        stack.enter_context(
            patch.object(
                dt,
                "_write_report_artifacts_stream",
                new=AsyncMock(return_value={"md": str(tmp_path / "r.md")}),
            )
        )
        outcome = json.loads(await dt.deepresearch_stream._func(action="start", query="q"))
    assert outcome["status"] == "completed"
    assert outcome["report_chars"] == len(report)


@pytest.mark.asyncio
async def test_no_terminal_marker_returns_bounded_stderr_tail():
    proc = _Proc([], stderr=b"diagnostic tail")
    patches = _stream_patches(proc)
    with ExitStack() as stack:
        for item in patches:
            stack.enter_context(item)
        outcome = json.loads(await dt.deepresearch_stream._func(action="start", query="q"))
    assert outcome["error_code"] == "terminal_marker_missing"
    assert outcome["stderr_tail"] == "diagnostic tail"


@pytest.mark.asyncio
async def test_long_child_stderr_preserves_the_final_exception():
    stderr = ("startup noise\n" * 3_000) + "Traceback: final root cause"
    proc = _Proc([], stderr=stderr.encode())
    patches = _stream_patches(proc)
    with ExitStack() as stack:
        for item in patches:
            stack.enter_context(item)
        outcome = json.loads(await dt.deepresearch_stream._func(action="start", query="q"))

    assert outcome["error_code"] == "terminal_marker_missing"
    assert len(outcome["stderr_tail"]) <= dt.DEEPRESEARCH_STDERR_OUTCOME_MAX_CHARS
    assert outcome["stderr_tail"].endswith("Traceback: final root cause")


@pytest.mark.asyncio
async def test_resume_positional_argument_order_is_preserved():
    proc = _Proc([json.dumps({"__deepsearch_status__": "error", "error": "done"})])
    patches = _stream_patches(proc)
    with ExitStack() as stack:
        for item in patches:
            stack.enter_context(item)
        await dt.deepresearch_stream._func(
            "resume", "ignored", "C1", "feedback", "feedback_handler", "report", ""
        )
    assert b"feedback" not in proc.stdin.data


def _styled_bundle(root: Path) -> Path:
    bundle = root / "report_bundle"
    (bundle / "infer").mkdir(parents=True)
    (bundle / "charts").mkdir()
    (bundle / "report.html").write_text(
        '<a href="infer/a.html">infer</a><img src="charts/c.png">',
        encoding="utf-8",
    )
    (bundle / "infer" / "a.html").write_text("infer", encoding="utf-8")
    (bundle / "charts" / "c.png").write_bytes(b"png")
    return bundle


def test_install_styled_bundle_uses_dedicated_asset_directories(tmp_path: Path):
    bundle = _styled_bundle(tmp_path / "staging")
    html = tmp_path / "output" / "report.html"
    html.parent.mkdir()
    dt._install_styled_bundle(bundle, html)
    rendered = html.read_text(encoding="utf-8")
    assert 'href="report_html_infer/a.html"' in rendered
    assert 'src="report_html_charts/c.png"' in rendered
    assert (html.parent / "report_html_infer" / "a.html").is_file()
    assert (html.parent / "report_html_charts" / "c.png").is_file()


@pytest.mark.parametrize("target_kind", ["directory", "symlink"])
def test_install_styled_bundle_preserves_occupied_asset_target(
    tmp_path: Path, target_kind: str
):
    bundle = _styled_bundle(tmp_path / "staging")
    output = tmp_path / "output"
    output.mkdir()
    occupied = output / "report_html_infer"
    protected = tmp_path / "protected"
    if target_kind == "directory":
        occupied.mkdir()
        (occupied / "keep").write_text("keep", encoding="utf-8")
    else:
        protected.mkdir()
        (protected / "keep").write_text("keep", encoding="utf-8")
        occupied.symlink_to(protected, target_is_directory=True)
    with pytest.raises((FileExistsError, OSError)):
        dt._install_styled_bundle(bundle, output / "report.html")
    assert not (output / "report.html").exists()
    if target_kind == "directory":
        assert (occupied / "keep").read_text() == "keep"
    else:
        assert (protected / "keep").read_text() == "keep"


def test_install_styled_bundle_rolls_back_owned_assets_after_html_collision(
    tmp_path: Path,
):
    bundle = _styled_bundle(tmp_path / "staging")
    output = tmp_path / "output"
    output.mkdir()
    html = output / "report.html"
    html.write_text("keep", encoding="utf-8")
    with pytest.raises(FileExistsError):
        dt._install_styled_bundle(bundle, html)
    assert html.read_text(encoding="utf-8") == "keep"
    assert not (output / "report_html_infer").exists()
    assert not (output / "report_html_charts").exists()


def test_install_styled_bundle_rejects_symlink_source_member(tmp_path: Path):
    bundle = _styled_bundle(tmp_path / "staging")
    (bundle / "infer" / "a.html").unlink()
    (bundle / "infer" / "a.html").symlink_to(bundle / "report.html")
    output = tmp_path / "output"
    output.mkdir()
    with pytest.raises(OSError, match="unsafe report asset"):
        dt._install_styled_bundle(bundle, output / "report.html")
    assert list(output.iterdir()) == []


class _NaturalExitReader(_Reader):
    def __init__(self, proc: "_NaturalExitProc", payload: bytes):
        super().__init__(payload)
        self.proc = proc

    async def read(self, size: int = -1) -> bytes:
        chunk = await super().read(size)
        if not chunk:
            self.proc.returncode = 0
        return chunk


class _NaturalExitProc(_Proc):
    def __init__(self, lines: list[str]):
        super().__init__([], running=True)
        payload = b"".join(line.encode() + b"\n" for line in lines)
        self.stdout = _NaturalExitReader(self, payload)


@pytest.mark.asyncio
async def test_interrupted_marker_waits_for_natural_runner_exit():
    proc = _NaturalExitProc(
        [
            json.dumps(
                {
                    "__deepsearch_status__": "interrupted",
                    "agent": "feedback_handler",
                    "conversation_id": "C1",
                }
            )
        ]
    )
    patches = _stream_patches(proc)
    with ExitStack() as stack:
        for item in patches:
            stack.enter_context(item)
        outcome = json.loads(await dt.deepresearch_stream._func(action="start", query="q"))
    assert outcome["status"] == "interrupted"
    assert proc.terminated == 0
    assert proc.waited >= 1


class _IgnoreTerminateProc(_Proc):
    def __init__(self):
        super().__init__([], running=True)

    async def wait(self) -> int:
        self.waited += 1
        if not self.killed:
            await asyncio.Event().wait()
        self.returncode = -9
        return self.returncode


@pytest.mark.asyncio
async def test_stop_process_kills_and_reaps_child_ignoring_terminate():
    proc = _IgnoreTerminateProc()
    await dt._stop_deepresearch_process(proc, timeout=0.01)
    assert proc.terminated == 1
    assert proc.killed == 1
    assert proc.waited == 2
    assert proc.returncode == -9


@pytest.mark.asyncio
async def test_feedback_resume_does_not_repeat_stage_one_transition():
    proc = _Proc(
        [
            json.dumps({"__deepsearch_status__": "resuming", "conversation_id": "C1"}),
            json.dumps({"__deepsearch_status__": "error", "error": "stop"}),
        ]
    )
    route = {
        "request_id": "R",
        "channel_id": "CH",
        "session_id": "S",
        "service_id": "default",
        "agent_id": "default",
    }
    push = AsyncMock()
    patches = _stream_patches(proc, route=route)
    with ExitStack() as stack:
        for item in patches:
            stack.enter_context(item)
        stack.enter_context(patch.object(dt, "WebSocketGatewayPushTransport", return_value=push))
        await dt.deepresearch_stream._func(
            action="resume", conversation_id="C1", node="feedback_handler"
        )
    stage_one_updates = [
        call
        for call in push.send_push.await_args_list
        if any(
            task.get("task_id") == "deepresearch_stage_1"
            and task.get("status") == "in_progress"
            for task in call.args[0]["payload"].get("tasks", [])
        )
    ]
    assert stage_one_updates == []


@pytest.mark.asyncio
async def test_outline_resume_starts_from_stage_three_without_replaying_prior_stages():
    proc = _Proc(
        [
            json.dumps({"__deepsearch_status__": "resuming", "conversation_id": "C1"}),
            json.dumps(
                {
                    "agent": "collector_info_retrieval",
                    "section_idx": "1",
                    "section_total": 1,
                    "event": "start",
                    "content": "searching",
                }
            ),
            json.dumps({"__deepsearch_status__": "error", "error": "stop"}),
        ]
    )
    route = {
        "request_id": "R",
        "channel_id": "CH",
        "session_id": "S",
        "service_id": "default",
        "agent_id": "default",
    }
    push = AsyncMock()
    patches = _stream_patches(proc, route=route)
    with ExitStack() as stack:
        for item in patches:
            stack.enter_context(item)
        stack.enter_context(
            patch.object(dt, "WebSocketGatewayPushTransport", return_value=push)
        )
        await dt.deepresearch_stream._func(
            action="resume",
            conversation_id="C1",
            node="outline_interaction",
            interaction_result=(
                '{"status":"answered","answers":[{"question":"q",'
                '"selected_options":["outline_confirm"],"custom_input":null}]}'
            ),
        )

    payloads = [
        call.args[0]["payload"]
        for call in push.send_push.await_args_list
    ]
    assert not any(
        payload.get("event_type") == "chat.delta"
        and "[DeepResearch 阶段" in payload.get("content", "")
        for payload in payloads
    )
    assert any(
        any(
            task.get("task_id") == "deepresearch_stage_3"
            and task.get("status") == "in_progress"
            for task in payload.get("tasks", [])
        )
        for payload in payloads
        if payload.get("event_type") == "task.update"
    )


@pytest.mark.asyncio
async def test_user_feedback_resume_can_complete_final_report_without_section_replay(
    tmp_path: Path,
):
    proc = _Proc(
        [
            json.dumps({"__deepsearch_status__": "resuming", "conversation_id": "C1"}),
            json.dumps(
                {
                    "__deepsearch_status__": "completed",
                    "conversation_id": "C1",
                    "final_result": {"response_content": "final"},
                }
            ),
        ]
    )
    route = {
        "request_id": "R",
        "channel_id": "CH",
        "session_id": "S",
        "service_id": "default",
        "agent_id": "default",
    }
    push = AsyncMock()
    patches = _stream_patches(proc, route=route)
    with ExitStack() as stack:
        for item in patches:
            stack.enter_context(item)
        stack.enter_context(
            patch.object(dt, "WebSocketGatewayPushTransport", return_value=push)
        )
        stack.enter_context(
            patch.object(
                dt,
                "_write_report_artifacts_stream",
                new=AsyncMock(return_value={"md": str(tmp_path / "r.md")}),
            )
        )
        outcome = json.loads(
            await dt.deepresearch_stream._func(
                action="resume",
                conversation_id="C1",
                node="user_feedback_processor",
            )
        )
    assert outcome["status"] == "completed"
    payloads = [
        call.args[0]["payload"]
        for call in push.send_push.await_args_list
    ]
    assert not any(
        payload.get("event_type") == "chat.delta"
        and "[DeepResearch 阶段" in payload.get("content", "")
        for payload in payloads
    )
    assert any(
        any(
            task.get("task_id") == "deepresearch_stage_4"
            and task.get("status") == "in_progress"
            for task in payload.get("tasks", [])
        )
        for payload in payloads
        if payload.get("event_type") == "task.update"
    )


def test_artifact_bundle_ignores_blank_and_unapproved_companions():
    bundle = dt._build_related_artifact_bundle(
        {
            "raw_report_path": " ",
            "citations_preview_path": "/hidden/preview.json",
            "citations_path": "/hidden/audit.json",
        },
        2,
    )
    assert bundle == {
        "schemaVersion": "1.0",
        "relatedArtifacts": [
            {
                "type": "citations_preview",
                "path": "/hidden/preview.json",
                "contentType": "application/json",
                "schemaVersion": "1.1",
                "relatedToPathIndex": 2,
            }
        ],
    }


@pytest.mark.asyncio
async def test_offline_html_uses_verified_snapshot_and_preserves_occupied_output(
    tmp_path: Path,
):
    markdown = tmp_path / "report.md"
    markdown.write_text("mutated source", encoding="utf-8")
    generated = await dt._generate_report_html({}, markdown, "verified snapshot")
    assert generated is not None
    result, html_style_status, html_style_phase, html_style_reason_code = generated
    assert html_style_status == "fallback"
    assert html_style_phase is None
    assert html_style_reason_code is None
    rendered = result.read_text(encoding="utf-8")
    assert "verified snapshot" in rendered
    assert "mutated source" not in rendered

    result.write_text("protected", encoding="utf-8")
    second = await dt._generate_report_html({}, markdown, "new content")
    assert second is None
    assert result.read_text(encoding="utf-8") == "protected"


@pytest.mark.asyncio
async def test_styled_html_uses_isolated_bridge_as_primary(tmp_path: Path):
    markdown = tmp_path / "report.md"
    archive = tmp_path / "styled.zip"
    archive.write_bytes(
        base64.b64decode(
            _zip_payload([
                ("report_bundle/report.html", b"<h1>styled</h1>", None),
            ]),
            validate=True,
        )
    )
    observed = {}

    @asynccontextmanager
    async def bridge(**kwargs):
        observed.update(kwargs)
        yield SimpleNamespace(
            path=archive,
            style_status="fallback",
            style_phase="invoke_llm",
            style_reason_code="llm_call_failed",
        )

    route = {
        "session_id": "S1",
        "service_id": "svc",
        "agent_id": "agent",
    }
    authorization = "Basic c3R5bGUtY2hpbGQ="
    runtime_config = {
        "LLM_SSL_VERIFY": "false",
        "TOOL_SSL_VERIFY": "true",
        "default_headers": json.dumps(
            {"Authorization": authorization, "X-Unrelated": "do-not-forward"}
        ),
    }
    with patch.object(dt, "_get_route", return_value=route), patch.object(
        dt, "_build_deepresearch_request_config",
        return_value=runtime_config,
    ), patch.object(
        dt, "_build_styled_export_llm_config",
        return_value={"general": {"api_key": "huawei-maas-session"}},
    ) as build_llm, patch.object(
        dt, "get_deepresearch_manager", return_value=Mock()
    ), patch.object(
        dt, "stylize_report_archive", bridge
    ):
        result = await dt._generate_report_html(
            {"response_content": "report"}, markdown, "offline"
        )

    assert result == (
        markdown.with_suffix(".html"),
        "fallback",
        "invoke_llm",
        "llm_call_failed",
    )
    assert result[0].read_text(encoding="utf-8") == "<h1>styled</h1>"
    assert observed["tls"] == {
        "LLM_SSL_VERIFY": False,
        "TOOL_SSL_VERIFY": True,
    }
    assert observed["session_id"] == "S1"
    assert observed["llm_auth"] == {
        "default_headers": json.dumps(
            {"Authorization": authorization},
            ensure_ascii=False,
            separators=(",", ":"),
        )
    }
    build_llm.assert_called_once_with(runtime_config)


@pytest.mark.asyncio
async def test_styled_html_primary_does_not_require_offline_markdown(tmp_path: Path):
    markdown = tmp_path / "report.md"
    archive = tmp_path / "styled.zip"
    archive.write_bytes(
        base64.b64decode(
            _zip_payload([
                ("report_bundle/report.html", b"<h1>styled</h1>", None),
            ]),
            validate=True,
        )
    )
    bridge_calls = 0

    @asynccontextmanager
    async def bridge(**_kwargs):
        nonlocal bridge_calls
        bridge_calls += 1
        yield SimpleNamespace(path=archive, style_status="applied")

    route = {"session_id": "S1", "service_id": "svc", "agent_id": "agent"}
    with patch.object(dt, "_get_route", return_value=route), patch.object(
        dt,
        "_build_deepresearch_request_config",
        return_value={"LLM_SSL_VERIFY": "false", "TOOL_SSL_VERIFY": "true"},
    ), patch.object(dt, "_build_styled_export_llm_config", return_value={}), patch.object(
        dt, "get_deepresearch_manager", return_value=Mock()
    ), patch.object(dt, "stylize_report_archive", bridge):
        result = await dt._generate_report_html(
            {"response_content": "report"}, markdown, None
        )

    assert bridge_calls == 1
    assert result == (markdown.with_suffix(".html"), "applied", None, None)
    assert result[0].read_text(encoding="utf-8") == "<h1>styled</h1>"


@pytest.mark.asyncio
async def test_styled_html_missing_maas_auth_falls_back_without_starting_bridge(
    tmp_path: Path,
):
    markdown = tmp_path / "report.md"
    bridge = Mock()
    with patch.object(
        dt,
        "_build_deepresearch_request_config",
        return_value={"LLM_SSL_VERIFY": "false", "TOOL_SSL_VERIFY": "false"},
    ), patch.object(
        dt,
        "_build_styled_export_llm_config",
        return_value={"general": {"api_key": "huawei-maas-session"}},
    ), patch.object(
        dt, "get_deepresearch_manager", return_value=Mock()
    ), patch.object(
        dt, "stylize_report_archive", bridge
    ):
        result = await dt._generate_report_html({}, markdown, "offline report")

    assert result == (markdown.with_suffix(".html"), "fallback", None, None)
    assert "offline report" in result[0].read_text(encoding="utf-8")
    bridge.assert_not_called()


@pytest.mark.asyncio
async def test_styled_html_cancellation_never_falls_back(tmp_path: Path):
    markdown = tmp_path / "report.md"

    @asynccontextmanager
    async def cancelled(**_kwargs):
        raise asyncio.CancelledError
        yield

    with patch.object(
        dt, "_build_deepresearch_request_config",
        return_value={"LLM_SSL_VERIFY": "false", "TOOL_SSL_VERIFY": "false"},
    ), patch.object(
        dt, "_build_styled_export_llm_config", return_value={"general": {}}
    ), patch.object(dt, "get_deepresearch_manager", return_value=Mock()), patch.object(
        dt, "stylize_report_archive", cancelled
    ), pytest.raises(asyncio.CancelledError):
        await dt._generate_report_html({}, markdown, "must not render")
    assert not markdown.with_suffix(".html").exists()


@pytest.mark.asyncio
async def test_ordered_stage_gateway_updates_reach_completed_todo_snapshot(tmp_path: Path):
    proc = _Proc(
        [
            json.dumps({"__deepsearch_status__": "started", "conversation_id": "C1"}),
            json.dumps({"agent": "outline", "content": "# 1. Scope"}),
            json.dumps(
                {"agent": "sub_reporter", "section_idx": "1", "section_total": 1, "event": "done"}
            ),
            json.dumps(
                {
                    "__deepsearch_status__": "completed",
                    "conversation_id": "C1",
                    "final_result": {"response_content": "final"},
                }
            ),
        ]
    )
    route = {
        "request_id": "R",
        "channel_id": "CH",
        "session_id": "S",
        "service_id": "default",
        "agent_id": "default",
    }
    push = AsyncMock()
    persist = Mock(return_value=True)
    patches = _stream_patches(proc, route=route)
    with ExitStack() as stack:
        for item in patches:
            stack.enter_context(item)
        stack.enter_context(patch.object(dt, "WebSocketGatewayPushTransport", return_value=push))
        stack.enter_context(patch.object(dt, "deepresearch_todo_path", return_value=tmp_path / "todo.json"))
        stack.enter_context(patch.object(dt, "persist_deepresearch_task_update", persist))
        stack.enter_context(
            patch.object(
                dt,
                "_write_report_artifacts_stream",
                new=AsyncMock(return_value={"md": str(tmp_path / "r.md")}),
            )
        )
        outcome = json.loads(await dt.deepresearch_stream._func(action="start", query="q"))
    assert outcome["status"] == "completed"
    snapshots = [
        call.args[0]["payload"]
        for call in push.send_push.await_args_list
        if call.args[0]["payload"].get("event_type") == "task.update"
    ]
    active = []
    for snapshot in snapshots:
        active.append(
            next(
                (
                    int(task["task_id"].rsplit("_", 1)[-1])
                    for task in snapshot["tasks"]
                    if task["status"] == "in_progress"
                ),
                None,
            )
        )
    assert active == sorted(active, key=lambda value: 99 if value is None else value)
    assert active[-1] is None
    assert persist.called


def test_windows_path_publication_backend_avoids_directory_descriptors(
    tmp_path: Path,
):
    final_result = {
        "response_content": "# Windows Report",
        "infer_messages": [],
        "chart_messages": [],
    }
    with patch.object(dt, "get_cwd", return_value=str(tmp_path)), patch.object(
        dt, "_uses_windows_path_publication", return_value=True
    ), patch.object(os, "supports_dir_fd", set()):
        report = Path(dt._write_report_markdown(final_result, "Windows", "C1"))
    assert report.is_file()
    assert report.with_suffix(".provenance.json").is_file()
    assert report.with_suffix(".final-result.json").is_file()


def test_windows_path_publication_never_overwrites_existing_html_or_assets(
    tmp_path: Path,
):
    bundle = _styled_bundle(tmp_path / "staging")
    output = tmp_path / "output"
    output.mkdir()
    html = output / "report.html"
    html.write_text("keep", encoding="utf-8")
    with patch.object(dt, "_uses_windows_path_publication", return_value=True), patch.object(
        os, "supports_dir_fd", set()
    ):
        with pytest.raises(FileExistsError):
            dt._install_styled_bundle(bundle, html)
    assert html.read_text(encoding="utf-8") == "keep"
    assert not (output / "report_html_infer").exists()
    assert not (output / "report_html_charts").exists()


def test_report_publication_reallocates_after_asset_namespace_collision(
    tmp_path: Path,
):
    occupied = tmp_path / "Report-v1_infer"
    occupied.mkdir()
    (occupied / "keep").write_text("keep", encoding="utf-8")
    final_result = {
        "response_content": "# Report\n\n[analysis](#inference:1)",
        "infer_messages": [
            {
                "id": "1",
                "html_base64": __import__("base64").b64encode(b"<p>analysis</p>").decode(),
            }
        ],
        "chart_messages": [],
    }
    with patch.object(dt, "get_cwd", return_value=str(tmp_path)):
        report = Path(dt._write_report_markdown(final_result, "Report", "C1"))
    assert report.name == "Report-2-v1.md"
    assert (occupied / "keep").read_text(encoding="utf-8") == "keep"


def test_exclusive_writes_remove_owned_partial_after_fsync_failure(
    tmp_path: Path,
):
    direct = tmp_path / "direct.bin"
    with patch.object(dt.os, "fsync", side_effect=OSError("disk failed")):
        with pytest.raises(OSError, match="disk failed"):
            dt._exclusive_write(direct, b"partial")
    assert not direct.exists()

    root_fd, _ = dt._open_output_root(tmp_path)
    assert root_fd is not None
    try:
        with patch.object(dt.os, "fsync", side_effect=OSError("disk failed")):
            with pytest.raises(OSError, match="disk failed"):
                dt._exclusive_write_at(root_fd, "at.bin", b"partial")
    finally:
        os.close(root_fd)
    assert not (tmp_path / "at.bin").exists()


def test_zip_validation_failure_does_not_create_destination(tmp_path: Path):
    destination = tmp_path / "destination"
    payload = _zip_payload(
        [
            ("report_bundle/report.html", b"ok", None),
            ("../escape", b"bad", None),
        ]
    )
    with pytest.raises(ValueError, match="unsafe ZIP"):
        dt._extract_styled_bundle(payload, destination)
    assert not destination.exists()


@pytest.mark.parametrize(
    "entries",
    [
        [
            ("report_bundle/report.html", b"ok", None),
            ("report_bundle/Caf\u00e9.txt", b"a", None),
            ("report_bundle/Cafe\u0301.txt", b"b", None),
        ],
        [
            ("report_bundle/report.html", b"ok", None),
            ("report_bundle/A.txt", b"a", None),
            ("report_bundle/a.TXT", b"b", None),
        ],
        [
            ("report_bundle/report.html", b"ok", None),
            ("report_bundle/node", b"file", None),
            ("report_bundle/node/child.txt", b"child", None),
        ],
    ],
)
def test_zip_namespace_conflicts_fail_before_destination_creation(
    tmp_path: Path,
    entries: list[tuple[str, bytes, int | None]],
):
    destination = tmp_path / "destination"
    with pytest.raises(ValueError):
        dt._extract_styled_bundle(_zip_payload(entries), destination)
    assert not destination.exists()


def test_zip_extraction_retries_short_writes(tmp_path: Path):
    destination = tmp_path / "destination"
    payload = _zip_payload(
        [("report_bundle/report.html", b"abcdefghij", None)]
    )
    original_write = os.write
    calls = 0

    def short_write(descriptor: int, data: bytes) -> int:
        nonlocal calls
        calls += 1
        amount = max(1, len(data) // 2)
        return original_write(descriptor, data[:amount])

    with patch.object(dt.os, "write", side_effect=short_write):
        bundle = dt._extract_styled_bundle(payload, destination)
    assert calls > 1
    assert (bundle / "report.html").read_bytes() == b"abcdefghij"


def test_zip_zero_write_rolls_back_owned_destination(tmp_path: Path):
    destination = tmp_path / "destination"
    payload = _zip_payload(
        [("report_bundle/report.html", b"content", None)]
    )
    with patch.object(dt.os, "write", return_value=0):
        with pytest.raises(OSError, match="ZIP output write failed"):
            dt._extract_styled_bundle(payload, destination)
    assert not destination.exists()


def test_zip_second_member_read_failure_rolls_back_entire_destination(
    tmp_path: Path,
):
    destination = tmp_path / "destination"
    payload = _zip_payload(
        [
            ("report_bundle/report.html", b"ok", None),
            ("report_bundle/second.txt", b"second", None),
        ]
    )
    original_read = zipfile.ZipExtFile.read

    def fail_second(source, *args, **kwargs):
        if str(getattr(source, "name", "")).endswith("second.txt"):
            raise zipfile.BadZipFile("CRC mismatch")
        return original_read(source, *args, **kwargs)

    with patch.object(zipfile.ZipExtFile, "read", fail_second):
        with pytest.raises(zipfile.BadZipFile, match="CRC mismatch"):
            dt._extract_styled_bundle(payload, destination)
    assert not destination.exists()


def test_zip_rollback_preserves_concurrent_destination_replacement(
    tmp_path: Path,
):
    destination = tmp_path / "destination"
    moved_owned = tmp_path / "moved-owned"
    payload = _zip_payload(
        [("report_bundle/report.html", b"content", None)]
    )

    def replace_then_fail(_descriptor: int, _data: bytes) -> int:
        destination.rename(moved_owned)
        destination.mkdir()
        (destination / "foreign").write_text("keep", encoding="utf-8")
        raise OSError("write failed")

    with patch.object(dt.os, "write", side_effect=replace_then_fail):
        with pytest.raises(OSError, match="write failed"):
            dt._extract_styled_bundle(payload, destination)
    assert (destination / "foreign").read_text(encoding="utf-8") == "keep"


def test_zip_rollback_preserves_swap_after_owned_tree_cleanup(tmp_path: Path):
    destination = tmp_path / "destination"
    moved_owned = tmp_path / "moved-owned"
    payload = _zip_payload(
        [("report_bundle/report.html", b"content", None)]
    )
    original_clear = dt._clear_owned_directory_fd
    swapped = False

    def clear_then_swap(descriptor: int) -> None:
        nonlocal swapped
        original_clear(descriptor)
        if not swapped and destination.exists():
            swapped = True
            destination.rename(moved_owned)
            destination.mkdir()
            (destination / "foreign").write_text("keep", encoding="utf-8")

    with patch.object(dt.os, "write", side_effect=OSError("write failed")), patch.object(
        dt, "_clear_owned_directory_fd", side_effect=clear_then_swap
    ):
        with pytest.raises(OSError, match="write failed"):
            dt._extract_styled_bundle(payload, destination)
    assert (destination / "foreign").read_text(encoding="utf-8") == "keep"


def test_zip_windows_fallback_extracts_and_rolls_back_by_identity(tmp_path: Path):
    destination = tmp_path / "destination"
    payload = _zip_payload(
        [("report_bundle/report.html", b"content", None)]
    )
    original_open = os.open

    def windows_open(path, flags, mode=0o777, *, dir_fd=None):
        if dir_fd is not None:
            raise NotImplementedError("dir_fd unavailable")
        return original_open(path, flags, mode)

    with patch.object(dt, "_uses_windows_path_publication", return_value=True), patch.object(
        dt.os, "open", side_effect=windows_open
    ):
        bundle = dt._extract_styled_bundle(payload, destination)
    assert (bundle / "report.html").read_bytes() == b"content"

    failed = tmp_path / "failed"
    with patch.object(dt, "_uses_windows_path_publication", return_value=True), patch.object(
        dt.os, "open", side_effect=windows_open
    ), patch.object(dt.os, "write", side_effect=OSError("write failed")):
        with pytest.raises(OSError, match="write failed"):
            dt._extract_styled_bundle(payload, failed)
    assert not failed.exists()


def test_zip_windows_fallback_writes_members_in_binary_mode(
    tmp_path: Path, monkeypatch
):
    destination = tmp_path / "destination"
    member_payload = b"line-1\nline-2\x1a"
    payload = _zip_payload(
        [("report_bundle/report.html", member_payload, None)]
    )
    binary_flag = 1 << 29
    opened_flags: dict[int, int] = {}
    original_open = os.open
    original_write = os.write

    def windows_open(path, flags, mode=0o777, *, dir_fd=None):
        if dir_fd is not None:
            raise NotImplementedError("dir_fd unavailable")
        descriptor = original_open(path, flags & ~binary_flag, mode)
        opened_flags[descriptor] = flags
        return descriptor

    def windows_write(descriptor: int, data: bytes) -> int:
        raw = bytes(data)
        if opened_flags[descriptor] & binary_flag:
            return original_write(descriptor, raw)
        translated = raw.replace(b"\n", b"\r\n")
        assert original_write(descriptor, translated) == len(translated)
        return len(raw)

    monkeypatch.setattr(dt.os, "O_BINARY", binary_flag, raising=False)
    monkeypatch.setattr(dt.os, "open", windows_open)
    monkeypatch.setattr(dt.os, "write", windows_write)

    with patch.object(dt, "_uses_windows_path_publication", return_value=True):
        bundle = dt._extract_styled_bundle(payload, destination)

    assert (bundle / "report.html").read_bytes() == member_payload


def test_regular_file_reader_uses_binary_mode_on_windows(
    tmp_path: Path, monkeypatch
):
    source = tmp_path / "styled.zip"
    payload = b"prefix\r\nsuffix\x1atail"
    source.write_bytes(payload)
    binary_flag = 1 << 29
    opened_flags: dict[int, int] = {}
    original_open = os.open
    original_read = os.read

    def windows_open(path, flags, mode=0o777, **kwargs):
        descriptor = original_open(path, flags & ~binary_flag, mode, **kwargs)
        opened_flags[descriptor] = flags
        return descriptor

    def windows_read(descriptor: int, size: int) -> bytes:
        raw = original_read(descriptor, size)
        if opened_flags[descriptor] & binary_flag:
            return raw
        return raw.split(b"\x1a", 1)[0].replace(b"\r\n", b"\n")

    monkeypatch.setattr(dt.os, "O_BINARY", binary_flag, raising=False)
    monkeypatch.setattr(dt.os, "open", windows_open)
    monkeypatch.setattr(dt.os, "read", windows_read)

    assert dt._read_regular_file(
        source, limit=len(payload), label="styled report archive"
    ) == payload


@pytest.mark.asyncio
async def test_spawn_and_child_env_use_same_resolved_interpreter():
    proc = _Proc([json.dumps({"__deepsearch_status__": "error", "error": "done"})])
    executable = Path("/runtime/bin/python")
    env_builder = Mock(return_value={"PATH": "/runtime/bin"})
    spawn = AsyncMock(return_value=proc)
    with patch.object(dt, "resolve_python_executable", return_value=executable) as resolve, patch.object(
        dt, "_resolve_run_script", return_value="/runner"
    ), patch.object(dt, "_build_deepresearch_request_config", return_value=_valid_config()), patch.object(
        dt, "_build_deepresearch_child_env", env_builder
    ), patch("asyncio.create_subprocess_exec", new=spawn):
        await dt.deepresearch_stream._func(action="start", query="q")
    assert resolve.call_count == 1
    assert env_builder.call_args.kwargs["executable"] == executable
    assert spawn.await_args.args[0] == str(executable)


@pytest.mark.asyncio
async def test_child_stderr_and_error_are_bounded_and_secret_redacted(caplog):
    secrets = {
        "LLM_API_KEY": "llm-super-secret",
        "WEB_SEARCH_API_KEY": "search-super-secret",
        "default_headers": "header-super-secret",
    }
    config = _valid_config(**secrets)
    proc = _Proc(
        [
            json.dumps(
                {
                    "__deepsearch_status__": "error",
                    "error_code": "BAD/CODE\n" + "X" * 200,
                    "error": (
                        "provider failed "
                        + secrets["LLM_API_KEY"]
                        + " "
                        + secrets["WEB_SEARCH_API_KEY"]
                        + " "
                        + secrets["default_headers"]
                        + "Y" * 5000
                    ),
                }
            )
        ],
        stderr=("stderr " + " ".join(secrets.values())).encode(),
    )
    patches = list(_stream_patches(proc))
    patches[2] = patch.object(dt, "_build_deepresearch_request_config", return_value=config)
    with ExitStack() as stack:
        for item in patches:
            stack.enter_context(item)
        outcome_text = await dt.deepresearch_stream._func(action="start", query="q")
    outcome = json.loads(outcome_text)
    combined = outcome_text + "\n" + caplog.text
    assert all(secret not in combined for secret in secrets.values())
    assert outcome["error_code"] == "workflow_error"
    assert len(outcome["error"]) <= dt.DEEPRESEARCH_ERROR_TEXT_MAX_CHARS
    assert len(outcome["stderr_tail"]) <= dt.DEEPRESEARCH_STDERR_OUTCOME_MAX_CHARS


@pytest.mark.asyncio
async def test_action_and_interaction_errors_do_not_echo_untrusted_input():
    action = "secret-action-token"
    with patch.object(dt, "resolve_python_executable", return_value=Path("/runtime/bin/python")), patch.object(
        dt, "_resolve_run_script", return_value="/runner"
    ):
        action_result = await dt.deepresearch_stream._func(action=action)
    assert action not in action_result

    interaction = '{"status":"secret-status-token","answers":[]}'
    with patch.object(dt, "resolve_python_executable", return_value=Path("/runtime/bin/python")), patch.object(
        dt, "_resolve_run_script", return_value="/runner"
    ):
        interaction_result = await dt.deepresearch_stream._func(
            action="resume",
            conversation_id="C1",
            node="feedback_handler",
            interaction_result=interaction,
        )
    assert "secret-status-token" not in interaction_result


@pytest.mark.asyncio
async def test_deep_or_invalid_utf8_stdout_returns_fixed_protocol_error():
    deep = "[" * 200 + "0" + "]" * 200
    for payload in (deep.encode() + b"\n", b'{"bad":"\xff"}\n'):
        proc = _Proc([])
        proc.stdout = _Reader(payload)
        patches = _stream_patches(proc)
        with ExitStack() as stack:
            for item in patches:
                stack.enter_context(item)
            outcome_text = await dt.deepresearch_stream._func(action="start", query="q")
        outcome = json.loads(outcome_text)
        assert outcome["error_code"] == "stream_protocol_invalid"
        assert outcome["error"] == "DeepResearch stream protocol is invalid"
        assert "xff" not in outcome_text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("marker", "expected_status"),
    [
        ({"__deepsearch_status__": "error", "error": "failed"}, "error"),
        (
            {
                "__deepsearch_status__": "interrupted",
                "agent": "feedback_handler",
                "conversation_id": "C1",
            },
            "interrupted",
        ),
    ],
)
async def test_untrack_failure_preserves_terminal_outcome_and_progress_cleanup(
    marker: dict[str, object], expected_status: str, caplog
):
    proc = _Proc([json.dumps(marker)])
    manager = Mock()
    manager.untrack_process.side_effect = RuntimeError("untrack secret payload")
    artifact = dt._ProgressArtifact(Path("/private/progress"), (1, 2), (3, 4))
    remove = Mock()
    patches = _stream_patches(proc)
    with ExitStack() as stack:
        for item in patches:
            stack.enter_context(item)
        stack.enter_context(patch.object(dt, "get_deepresearch_manager", return_value=manager))
        stack.enter_context(patch.object(dt, "_create_progress_artifact", return_value=artifact))
        stack.enter_context(patch.object(dt, "_remove_progress_artifact", remove))
        outcome = json.loads(
            await dt.deepresearch_stream._func(action="start", query="q")
        )
    assert outcome["status"] == expected_status
    remove.assert_called_once_with(artifact)
    assert "untrack secret payload" not in caplog.text


@pytest.mark.asyncio
async def test_untrack_failure_cannot_override_cancel_or_progress_cleanup():
    proc = _CancellationProc()
    manager = Mock()
    manager.untrack_process.side_effect = RuntimeError("untrack failed")
    artifact = dt._ProgressArtifact(Path("/private/progress"), (1, 2), (3, 4))
    remove = Mock()
    patches = _stream_patches(proc)

    async def invoke():
        with ExitStack() as stack:
            for item in patches:
                stack.enter_context(item)
            stack.enter_context(
                patch.object(dt, "get_deepresearch_manager", return_value=manager)
            )
            stack.enter_context(
                patch.object(dt, "_create_progress_artifact", return_value=artifact)
            )
            stack.enter_context(patch.object(dt, "_remove_progress_artifact", remove))
            await dt.deepresearch_stream._func(action="start", query="q")

    task = asyncio.create_task(invoke())
    for _ in range(100):
        if manager.track_process.called:
            break
        await asyncio.sleep(0)
    task.cancel()
    for _ in range(100):
        if proc.terminated:
            break
        await asyncio.sleep(0)
    proc.release_wait.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    remove.assert_called_once_with(artifact)


@pytest.mark.asyncio
async def test_child_secrets_are_redacted_before_gateway_todo_state_and_prompt(
    tmp_path: Path, caplog
):
    secret = "request-scoped-super-secret"
    config = _valid_config(
        LLM_API_KEY=secret,
        WEB_SEARCH_API_KEY="search-scoped-super-secret",
        default_headers="header-scoped-super-secret",
    )
    proc = _Proc(
        [
            json.dumps({"__deepsearch_status__": "started", "conversation_id": "C1"}),
            json.dumps(
                {
                    "agent": "outline",
                    "content": {
                        "title": f"outline {secret}",
                        "sections": [{"title": secret, secret: "nested outline"}],
                    },
                    "reasoning_content": f"reasoning {secret}",
                }
            ),
            json.dumps(
                {
                    "agent": "sub_reporter",
                    "section_idx": "1",
                    "section_total": 1,
                    "section_title": f"section {secret}",
                    "content": f"section reasoning {secret}",
                    "event": "start",
                }
            ),
            json.dumps(
                {
                    "agent": "question_generator",
                    "message_type": "message_chunk",
                    "message_id": "Q1",
                    "content": f"native question {secret}",
                }
            ),
            json.dumps(
                {
                    "__deepsearch_status__": "interrupted",
                    "agent": "feedback_handler",
                    "conversation_id": "C1",
                    "questions": [
                        {secret: f"native {secret}", "nested": {"value": secret}}
                    ],
                }
            ),
        ]
    )
    route = {
        "request_id": "R",
        "channel_id": "CH",
        "session_id": "S",
        "service_id": "default",
        "agent_id": "default",
    }
    push = AsyncMock()
    persist = Mock(return_value=True)
    patches = list(_stream_patches(proc, route=route))
    patches[2] = patch.object(
        dt, "_build_deepresearch_request_config", return_value=config
    )
    with ExitStack() as stack:
        for item in patches:
            stack.enter_context(item)
        stack.enter_context(
            patch.object(dt, "WebSocketGatewayPushTransport", return_value=push)
        )
        stack.enter_context(
            patch.object(
                dt, "deepresearch_todo_path", return_value=tmp_path / "todo.json"
            )
        )
        stack.enter_context(
            patch.object(dt, "persist_deepresearch_task_update", persist)
        )
        outcome_text = await dt.deepresearch_stream._func(
            action="start", query=f"query {secret}"
        )

    gateway_text = json.dumps(
        [call.args[0] for call in push.send_push.await_args_list],
        ensure_ascii=False,
    )
    todo_text = json.dumps(
        [call.args[0] for call in persist.call_args_list], ensure_ascii=False
    )
    combined = "\n".join((gateway_text, todo_text, outcome_text, caplog.text))
    for value in dt._config_secret_values(config):
        assert value not in combined
    outcome = json.loads(outcome_text)
    assert outcome["status"] == "interrupted"
    assert "[REDACTED]" in combined
    assert any(
        call.args[0]["payload"].get("event_type") == "chat.reasoning"
        for call in push.send_push.await_args_list
    )


@pytest.mark.asyncio
async def test_secret_redaction_key_collision_fails_closed_before_gateway():
    secret = "request-secret-key"
    config = _valid_config(LLM_API_KEY=secret)
    proc = _Proc(
        [
            json.dumps(
                {
                    "agent": "outline",
                    secret: "first",
                    "[REDACTED]": "second",
                }
            )
        ]
    )
    route = {
        "request_id": "R",
        "channel_id": "CH",
        "session_id": "S",
        "service_id": "default",
        "agent_id": "default",
    }
    push = AsyncMock()
    patches = list(_stream_patches(proc, route=route))
    patches[2] = patch.object(
        dt, "_build_deepresearch_request_config", return_value=config
    )
    with ExitStack() as stack:
        for item in patches:
            stack.enter_context(item)
        stack.enter_context(
            patch.object(dt, "WebSocketGatewayPushTransport", return_value=push)
        )
        outcome = json.loads(
            await dt.deepresearch_stream._func(action="start", query="q")
        )
    assert outcome == {
        "status": "error",
        "error_code": "stream_protocol_invalid",
        "error": "DeepResearch stream protocol is invalid",
        "returncode": 0,
    }
    push.send_push.assert_not_awaited()


def test_outline_title_cache_is_bounded_for_many_tenants_and_conversations():
    with dt._OUTLINE_TITLE_CACHES_GUARD:
        dt._OUTLINE_TITLE_CACHES.clear()
    try:
        route = {
            "service_id": "service",
            "agent_id": "agent",
            "session_id": "session",
        }
        for index in range(dt.DEEPRESEARCH_OUTLINE_CACHE_CONVERSATIONS + 25):
            dt._cache_outline_titles(
                route,
                f"conversation-{index}",
                {"1": f"section-{index}"},
            )
        key = ("service", "agent", "session")
        with dt._OUTLINE_TITLE_CACHES_GUARD:
            assert len(dt._OUTLINE_TITLE_CACHES[key]) == (
                dt.DEEPRESEARCH_OUTLINE_CACHE_CONVERSATIONS
            )

        for index in range(dt.DEEPRESEARCH_OUTLINE_CACHE_TENANTS + 25):
            dt._cache_outline_titles(
                {
                    "service_id": f"service-{index}",
                    "agent_id": "agent",
                    "session_id": "session",
                },
                "conversation",
                {"1": "section"},
            )
        with dt._OUTLINE_TITLE_CACHES_GUARD:
            assert len(dt._OUTLINE_TITLE_CACHES) <= (
                dt.DEEPRESEARCH_OUTLINE_CACHE_TENANTS
            )
    finally:
        with dt._OUTLINE_TITLE_CACHES_GUARD:
            dt._OUTLINE_TITLE_CACHES.clear()


def test_outline_title_cache_rejects_oversized_titles_and_terminal_clear():
    with dt._OUTLINE_TITLE_CACHES_GUARD:
        dt._OUTLINE_TITLE_CACHES.clear()
    route = {
        "service_id": "service",
        "agent_id": "agent",
        "session_id": "session",
    }
    try:
        dt._cache_outline_titles(
            route,
            "oversized",
            {
                str(index): "x"
                for index in range(dt.DEEPRESEARCH_OUTLINE_CACHE_TITLES + 1)
            },
        )
        key = ("service", "agent", "session")
        with dt._OUTLINE_TITLE_CACHES_GUARD:
            assert "oversized" not in dt._OUTLINE_TITLE_CACHES.get(key, {})

        dt._cache_outline_titles(route, "terminal", {"1": "section"})
        assert dt._get_cached_outline_titles(route, "terminal") == {
            "1": "section"
        }
        dt._clear_outline_title_cache(route, "terminal")
        assert dt._get_cached_outline_titles(route, "terminal") == {}
    finally:
        with dt._OUTLINE_TITLE_CACHES_GUARD:
            dt._OUTLINE_TITLE_CACHES.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("secret", "terminal_status"),
    [
        ("error", "error"),
        ("status", "error"),
        ("completed", "completed"),
        ("interrupted", "interrupted"),
        ("agent", "interrupted"),
    ],
)
async def test_protocol_control_keys_and_values_survive_secret_redaction(
    tmp_path: Path, secret: str, terminal_status: str
):
    free_text = f"free-prefix-{secret}-free-suffix"
    extension_key = f"extension-{secret}-field"
    config = _valid_config(LLM_API_KEY=secret)
    if terminal_status == "completed":
        lines = [
            json.dumps(
                {
                    "agent": "sub_reporter",
                    "section_idx": "1",
                    "section_total": 1,
                    "event": "done",
                    "content": free_text,
                }
            ),
            json.dumps(
                {
                    "__deepsearch_status__": "completed",
                    "conversation_id": "C1",
                    "final_result": {"response_content": free_text},
                    extension_key: free_text,
                }
            ),
        ]
    elif terminal_status == "interrupted":
        lines = [
            json.dumps(
                {
                    "__deepsearch_status__": "interrupted",
                    "agent": "feedback_handler",
                    "conversation_id": "C1",
                    "questions": [free_text],
                    extension_key: free_text,
                }
            )
        ]
    else:
        lines = [
            json.dumps(
                {
                    "__deepsearch_status__": "error",
                    "error_code": "workflow_error",
                    "error": free_text,
                    extension_key: free_text,
                }
            )
        ]

    route = {
        "request_id": "R",
        "channel_id": "CH",
        "session_id": "S",
        "service_id": "default",
        "agent_id": "default",
    }
    proc = _Proc(lines)
    push = AsyncMock()
    write_report = AsyncMock(
        return_value={"md": str(tmp_path / "report.md")}
    )
    patches = list(_stream_patches(proc, route=route))
    patches[2] = patch.object(
        dt, "_build_deepresearch_request_config", return_value=config
    )
    with ExitStack() as stack:
        for item in patches:
            stack.enter_context(item)
        stack.enter_context(
            patch.object(dt, "WebSocketGatewayPushTransport", return_value=push)
        )
        stack.enter_context(
            patch.object(dt, "_write_report_artifacts_stream", new=write_report)
        )
        outcome_text = await dt.deepresearch_stream._func(
            action="start", query="q"
        )

    outcome = json.loads(outcome_text)
    assert outcome["status"] == terminal_status
    if terminal_status == "error":
        assert outcome["error_code"] == "workflow_error"
        assert "error" in outcome
    if terminal_status == "interrupted":
        assert outcome["node_id"] == "feedback_handler"
    assert free_text not in outcome_text
    assert extension_key not in outcome_text
    assert "[REDACTED]" in outcome_text or terminal_status == "completed"
    gateway_text = json.dumps(
        [call.args[0] for call in push.send_push.await_args_list],
        ensure_ascii=False,
    )
    assert free_text not in gateway_text
    assert extension_key not in gateway_text
    if terminal_status == "completed":
        written = json.dumps(write_report.await_args.args[0], ensure_ascii=False)
        assert free_text not in written
        assert "[REDACTED]" in written


@pytest.mark.parametrize("swap_point", ["after_root_open", "after_first_write"])
def test_posix_zip_extraction_stays_on_held_destination_when_name_is_swapped(
    tmp_path: Path, swap_point: str
):
    destination = tmp_path / "destination"
    moved_owned = tmp_path / "moved-owned"
    outside = tmp_path / "outside"
    outside.mkdir()
    payload = _zip_payload(
        [
            ("report_bundle/report.html", b"report", None),
            ("report_bundle/second.txt", b"second", None),
        ]
    )
    original_open = os.open
    original_write_all = dt._write_all
    swapped = False

    def swap_destination() -> None:
        nonlocal swapped
        if swapped:
            return
        swapped = True
        destination.rename(moved_owned)
        destination.symlink_to(outside, target_is_directory=True)

    def observed_open(path, flags, mode=0o777, *, dir_fd=None):
        descriptor = original_open(path, flags, mode, dir_fd=dir_fd)
        if (
            swap_point == "after_root_open"
            and not swapped
            and path == destination.name
            and dir_fd is not None
            and stat.S_ISDIR(os.fstat(descriptor).st_mode)
        ):
            swap_destination()
        return descriptor

    def observed_write_all(descriptor: int, data: bytes) -> int:
        written = original_write_all(descriptor, data)
        if swap_point == "after_first_write" and not swapped:
            swap_destination()
        return written

    with patch.object(dt, "_uses_windows_path_publication", return_value=False), patch.object(
        dt.os, "open", side_effect=observed_open
    ), patch.object(dt, "_write_all", side_effect=observed_write_all):
        with pytest.raises(OSError, match="ZIP destination changed"):
            dt._extract_styled_bundle(payload, destination)

    assert swapped
    assert destination.is_symlink()
    assert destination.resolve() == outside.resolve()
    assert list(outside.iterdir()) == []
    assert moved_owned.is_dir()
    assert list(moved_owned.iterdir()) == []

# --- Todo 3: outline_interaction resume resolver tests ---

def _outline_route() -> dict[str, str]:
    return {
        "request_id": "R1",
        "channel_id": "CH1",
        "session_id": "S1",
        "service_id": "default",
        "agent_id": "default",
    }


def _three_section_outline() -> dict[str, Any]:
    return {
        "title": "AI Agent 架构",
        "thought": "从框架到部署",
        "sections": [
            {"id": "1", "title": "架构设计", "is_core_section": True, "description": "核心架构"},
            {"id": "2", "title": "部署方案", "format_requirements": "markdown"},
            {"id": "3", "title": "性能评估", "is_core_section": False, "description": "基准测试"},
        ],
    }


def _outline_interaction_result(selected: str, custom_input: str | None = None) -> str:
    payload: dict[str, Any] = {
        "request_id": "R1",
        "source": "deepresearch_stream",
        "answers": [
            {
                "question": "请审阅研究报告大纲",
                "selected_options": [selected],
                "custom_input": custom_input,
            }
        ],
    }
    return json.dumps(payload, ensure_ascii=False)


def test_resolve_outline_confirm_branch_returns_accepted_feedback() -> None:
    """outline_confirm -> interrupt_feedback=accepted, empty feedback string."""
    feedback = dt._resolve_outline_interaction_feedback(
        _outline_interaction_result("outline_confirm"),
        conversation_id="C1",
        route=_outline_route(),
    )
    parsed = json.loads(feedback)
    assert parsed["interrupt_feedback"] == "accepted"
    assert parsed["feedback"] == ""


def test_resolve_outline_confirm_accepts_live_ask_user_label() -> None:
    """The generic AskUser card returns its display label, not the UI-only id."""
    interaction_result = json.dumps(
        {
            "status": "answered",
            "answers": [
                {
                    "question": "请审阅生成的研究报告大纲，确认后将继续执行深度研究。",
                    "selected_options": ["确认大纲，继续研究"],
                    "custom_input": None,
                }
            ],
        },
        ensure_ascii=False,
    )

    feedback = dt._resolve_outline_interaction_feedback(
        interaction_result,
        conversation_id="C1",
        route=_outline_route(),
    )

    assert json.loads(feedback) == {
        "interrupt_feedback": "accepted",
        "feedback": "",
    }


def test_resolve_outline_skipped_result_defaults_to_accepted() -> None:
    """Outline timeout/skip follows the skill contract and continues research."""
    interaction_result = json.dumps(
        {
            "status": "skipped",
            "answers": [
                {
                    "question": "请审阅生成的研究报告大纲，确认后将继续执行深度研究。",
                    "selected_options": [],
                    "custom_input": None,
                }
            ],
        },
        ensure_ascii=False,
    )

    feedback = dt._resolve_outline_interaction_feedback(
        interaction_result,
        conversation_id="C1",
        route=_outline_route(),
    )

    assert json.loads(feedback) == {
        "interrupt_feedback": "accepted",
        "feedback": "",
    }


def test_resolve_outline_use_edited_updates_all_titles_and_preserves_other_fields() -> None:
    """outline_use_edited with matching page count -> revise_outline, all titles updated, other fields unchanged."""
    route = _outline_route()
    dt._cache_outline_json(route, "C1", _three_section_outline())
    try:
        edited_md = "### P1: NewA\n### P2: NewB\n### P3: NewC"
        feedback = dt._resolve_outline_interaction_feedback(
            _outline_interaction_result("outline_use_edited", custom_input=edited_md),
            conversation_id="C1",
            route=route,
        )
        parsed = json.loads(feedback)
        assert parsed["interrupt_feedback"] == "revise_outline"
        inner = json.loads(parsed["feedback"])
        assert inner["title"] == "AI Agent 架构"
        assert inner["sections"][0]["title"] == "NewA"
        assert inner["sections"][1]["title"] == "NewB"
        assert inner["sections"][2]["title"] == "NewC"
        # Other fields preserved
        assert inner["sections"][0]["is_core_section"] is True
        assert inner["sections"][0]["description"] == "核心架构"
        assert inner["sections"][1]["format_requirements"] == "markdown"
        assert inner["sections"][2]["is_core_section"] is False
        # Cache not mutated (shallow copy returned by getter is the working surface)
        cached = dt._get_cached_outline_json(route, "C1")
        assert cached["sections"][0]["title"] == "架构设计"
    finally:
        with dt._OUTLINE_JSON_CACHES_GUARD:
            dt._OUTLINE_JSON_CACHES.clear()
        with dt._OUTLINE_TITLE_CACHES_GUARD:
            dt._OUTLINE_TITLE_CACHES.clear()


def test_resolve_outline_use_edited_strips_core_section_suffix() -> None:
    """（重点） suffix in edited title is stripped before mapping to Section.title."""
    route = _outline_route()
    dt._cache_outline_json(route, "C1", _three_section_outline())
    try:
        edited_md = "### P1: 全新架构（重点）\n### P2: 部署方案"
        feedback = dt._resolve_outline_interaction_feedback(
            _outline_interaction_result("outline_use_edited", custom_input=edited_md),
            conversation_id="C1",
            route=route,
        )
        parsed = json.loads(feedback)
        inner = json.loads(parsed["feedback"])
        assert inner["sections"][0]["title"] == "全新架构"
        assert inner["sections"][1]["title"] == "部署方案"
    finally:
        with dt._OUTLINE_JSON_CACHES_GUARD:
            dt._OUTLINE_JSON_CACHES.clear()
        with dt._OUTLINE_TITLE_CACHES_GUARD:
            dt._OUTLINE_TITLE_CACHES.clear()


def test_resolve_outline_use_edited_cache_miss_raises_value_error() -> None:
    """No cached Outline JSON -> ValueError (fail closed)."""
    route = _outline_route()
    try:
        with pytest.raises(ValueError, match="cached"):
            dt._resolve_outline_interaction_feedback(
                _outline_interaction_result("outline_use_edited", custom_input="### P1: NewA"),
                conversation_id="C1",
                route=route,
            )
    finally:
        with dt._OUTLINE_JSON_CACHES_GUARD:
            dt._OUTLINE_JSON_CACHES.clear()
        with dt._OUTLINE_TITLE_CACHES_GUARD:
            dt._OUTLINE_TITLE_CACHES.clear()


def test_resolve_outline_use_edited_empty_cache_raises_value_error() -> None:
    """Empty dict cached (Todo 2 parse-failure path) -> ValueError (treat as cache miss)."""
    route = _outline_route()
    dt._cache_outline_json(route, "C1", {})
    try:
        with pytest.raises(ValueError, match="cached"):
            dt._resolve_outline_interaction_feedback(
                _outline_interaction_result("outline_use_edited", custom_input="### P1: NewA"),
                conversation_id="C1",
                route=route,
            )
    finally:
        with dt._OUTLINE_JSON_CACHES_GUARD:
            dt._OUTLINE_JSON_CACHES.clear()
        with dt._OUTLINE_TITLE_CACHES_GUARD:
            dt._OUTLINE_TITLE_CACHES.clear()


def test_resolve_outline_page_count_mismatch_updates_only_matching_indices() -> None:
    """Edited has fewer pages than cached -> only matching indices updated, unmatched sections untouched."""
    route = _outline_route()
    dt._cache_outline_json(route, "C1", _three_section_outline())
    try:
        edited_md = "### P1: NewA\n### P2: NewB"
        feedback = dt._resolve_outline_interaction_feedback(
            _outline_interaction_result("outline_use_edited", custom_input=edited_md),
            conversation_id="C1",
            route=route,
        )
        parsed = json.loads(feedback)
        inner = json.loads(parsed["feedback"])
        assert inner["sections"][0]["title"] == "NewA"
        assert inner["sections"][1]["title"] == "NewB"
        # Third section keeps original title (no add/remove)
        assert inner["sections"][2]["title"] == "性能评估"
        assert len(inner["sections"]) == 3
    finally:
        with dt._OUTLINE_JSON_CACHES_GUARD:
            dt._OUTLINE_JSON_CACHES.clear()
        with dt._OUTLINE_TITLE_CACHES_GUARD:
            dt._OUTLINE_TITLE_CACHES.clear()


def test_resolve_outline_page_count_more_than_cached_skips_out_of_range() -> None:
    """Edited has more pages than cached -> out-of-range pages skipped, no new sections added."""
    route = _outline_route()
    dt._cache_outline_json(route, "C1", _three_section_outline())
    try:
        edited_md = "### P1: NewA\n### P2: NewB\n### P3: NewC\n### P4: ShouldBeSkipped"
        feedback = dt._resolve_outline_interaction_feedback(
            _outline_interaction_result("outline_use_edited", custom_input=edited_md),
            conversation_id="C1",
            route=route,
        )
        parsed = json.loads(feedback)
        inner = json.loads(parsed["feedback"])
        assert len(inner["sections"]) == 3
        assert inner["sections"][0]["title"] == "NewA"
        assert inner["sections"][1]["title"] == "NewB"
        assert inner["sections"][2]["title"] == "NewC"
    finally:
        with dt._OUTLINE_JSON_CACHES_GUARD:
            dt._OUTLINE_JSON_CACHES.clear()
        with dt._OUTLINE_TITLE_CACHES_GUARD:
            dt._OUTLINE_TITLE_CACHES.clear()


@pytest.mark.parametrize(
    "interaction_result",
    [
        "",
        "not-json",
        "[]",
        json.dumps({"status": "answered"}),  # missing answers
        json.dumps({"status": "answered", "answers": "not-a-list"}),
        json.dumps({"status": "answered", "answers": []}),
        json.dumps({"status": "answered", "answers": [{"selected_options": "not-a-list"}]}),
        json.dumps({"status": "answered", "answers": [{"selected_options": []}]}),
        json.dumps({"status": "answered", "answers": [{"selected_options": ["unknown_choice"]}]}),
        json.dumps({"status": "answered", "answers": [{"selected_options": ["outline_use_edited"], "custom_input": "no heading lines here"}]}),
    ],
)
def test_resolve_outline_malformed_result_raises_value_error(interaction_result: str) -> None:
    """All malformed interaction_result variants -> ValueError (fail closed, no auto-accept)."""
    route = _outline_route()
    dt._cache_outline_json(route, "C1", _three_section_outline())
    try:
        with pytest.raises(ValueError):
            dt._resolve_outline_interaction_feedback(
                interaction_result,
                conversation_id="C1",
                route=route,
            )
    finally:
        with dt._OUTLINE_JSON_CACHES_GUARD:
            dt._OUTLINE_JSON_CACHES.clear()
        with dt._OUTLINE_TITLE_CACHES_GUARD:
            dt._OUTLINE_TITLE_CACHES.clear()


def test_resolve_outline_cancel_status_returns_cancel_feedback() -> None:
    """AskUserQuestion status=cancelled -> interrupt_feedback=cancel, empty feedback."""
    feedback = dt._resolve_outline_interaction_feedback(
        json.dumps({"status": "cancelled", "answers": []}, ensure_ascii=False),
        conversation_id="C1",
        route=_outline_route(),
    )
    parsed = json.loads(feedback)
    assert parsed["interrupt_feedback"] == "cancel"
    assert parsed["feedback"] == ""


def test_resolve_outline_error_status_returns_cancel_feedback() -> None:
    """AskUserQuestion status=error -> interrupt_feedback=cancel (treat errors as user-initiated cancellation)."""
    feedback = dt._resolve_outline_interaction_feedback(
        json.dumps({"status": "error", "error": "ask_user_question timeout"}, ensure_ascii=False),
        conversation_id="C1",
        route=_outline_route(),
    )
    parsed = json.loads(feedback)
    assert parsed["interrupt_feedback"] == "cancel"
    assert parsed["feedback"] == ""


@pytest.mark.asyncio
async def test_outline_interaction_resume_invalid_result_does_not_spawn():
    """outline_interaction resume with malformed interaction_result returns outline_interaction_invalid_result and never spawns the subprocess."""
    spawn = AsyncMock()
    with patch.object(dt, "resolve_python_executable", return_value=Path("/runtime/bin/python")), patch.object(
        dt, "_resolve_run_script", return_value="/runner"
    ), patch("asyncio.create_subprocess_exec", new=spawn):
        outcome = json.loads(
            await dt.deepresearch_stream._func(
                action="resume",
                conversation_id="C1",
                node="outline_interaction",
                interaction_result="not-json",
            )
        )

    assert outcome["error_code"] == "outline_interaction_invalid_result"
    assert outcome["status"] == "error"
    spawn.assert_not_awaited()


def test_brief_final_pipeline_completion_is_recognized():
    state = dt.RouterState(
        active_nodes={
            "brief_reporter:0": {
                "agent_name": "brief_reporter",
                "done": True,
            }
        }
    )

    assert dt._has_completed_final_pipeline_node(state) is True
