# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""deepresearch_stream tool 集成单测(mock subprocess + 凭据桥接单测)。

测试用 `deepresearch_stream._func(...)` 直接 await 原始 async 函数,绕过 LocalFunction
的 schema/trigger 机制,聚焦 spawn+route+outcome 逻辑。
"""
import asyncio
import base64
import hashlib
import inspect
import io
import json
import logging
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from types import SimpleNamespace

import os
import sys

import pytest
from unittest.mock import AsyncMock, Mock, patch

from jiuwenclaw.agentserver.tools import deepresearch_tools as dt
from jiuwenclaw.agentserver.tools.deepresearch import todo_progress
from jiuwenclaw.local_env_config import (
    bind_task_env_overlay,
    reset_task_env_overlay,
)


@pytest.fixture(autouse=True)
def _stream_search_config(monkeypatch):
    original_build = dt.build_effective_env_overlay

    def build_with_search(*args, **kwargs):
        env = original_build(*args, **kwargs)
        env.setdefault("BOCHA_API_KEY", "test-bocha-key")
        return env

    monkeypatch.setattr(dt, "build_effective_env_overlay", build_with_search)


class _Proc:
    """假 subprocess:stdout 是 async generator,returncode/terminate/kill/wait 齐全。"""

    def __init__(self, lines, stderr_lines=None):
        self._lines = [l.encode() for l in lines]
        self._stderr = b"".join(s.encode() for s in (stderr_lines or []))
        self.returncode = 0
        self.stdin = _Stdin()

    @property
    def stdout(self):
        async def gen():
            for b in self._lines:
                yield b

        return gen()

    @property
    def stderr(self):
        data = self._stderr

        class _SR:
            def __init__(self):
                self._done = False

            async def read(self, _size=-1):
                if self._done:
                    return b""
                self._done = True
                return data

        return _SR()

    async def wait(self):
        return 0

    def terminate(self):
        pass

    def kill(self):
        pass


class _Stdin:
    def __init__(self):
        self.data = bytearray()
        self.closed = False

    def write(self, data):
        self.data.extend(data)

    async def drain(self):
        return None

    def close(self):
        self.closed = True

    async def wait_closed(self):
        return None


class _RunningProc(_Proc):
    """Process stays running until stdout is consumed to EOF."""

    def __init__(self, lines):
        super().__init__(lines)
        self.returncode = None
        self.stdout_exhausted = False
        self.terminated = False

    @property
    def stdout(self):
        async def gen():
            for b in self._lines:
                yield b
            self.stdout_exhausted = True
            self.returncode = 0

        return gen()

    def terminate(self):
        self.terminated = True
        self.returncode = -15


class _StderrBackpressureProc(_Proc):
    """stdout cannot finish until the parent starts draining stderr."""

    def __init__(self):
        super().__init__([])
        self.returncode = None
        self.stderr_drained = asyncio.Event()

    @property
    def stdout(self):
        async def gen():
            await self.stderr_drained.wait()
            yield json.dumps({
                "agent": "sub_reporter",
                "section_idx": "1",
                "section_total": 1,
                "event": "done",
            }).encode()
            yield json.dumps({
                "__deepsearch_status__": "completed",
                "conversation_id": "C1",
                "final_result": {"response_content": "done"},
            }).encode()
            self.returncode = 0

        return gen()

    @property
    def stderr(self):
        event = self.stderr_drained

        class _SR:
            def __init__(self):
                self._done = False

            async def read(self, _size=-1):
                if self._done:
                    return b""
                self._done = True
                event.set()
                return b"collector diagnostics"

        return _SR()


class _LargeStdoutLineProc(_Proc):
    """Expose a real StreamReader with an NDJSON line above its 64 KiB limit."""

    def __init__(self, report_content):
        super().__init__([])
        self._stdout = asyncio.StreamReader()
        self._stdout.feed_data((json.dumps({
            "agent": "sub_reporter",
            "section_idx": "1",
            "section_total": 1,
            "event": "done",
        }) + "\n").encode())
        self._stdout.feed_data((json.dumps({
            "__deepsearch_status__": "completed",
            "conversation_id": "C1",
            "final_result": {"response_content": report_content},
        }) + "\n").encode())
        self._stdout.feed_eof()

    @property
    def stdout(self):
        return self._stdout


def test_styled_export_llm_config_uses_request_overlay_instead_of_process_env(
    monkeypatch,
):
    monkeypatch.setenv("MODEL_NAME", "static-model")
    monkeypatch.setenv("MODEL_PROVIDER", "OpenAI")
    monkeypatch.setenv("API_BASE", "https://example.com/compatible-mode/v1")
    monkeypatch.setenv("API_KEY", "static-key")
    token = bind_task_env_overlay({
        "MODEL_NAME": "glm-5.2",
        "MODEL_PROVIDER": "OpenAI",
        "API_BASE": "https://client-claw.example/v2",
        "API_KEY": "request-key",
    })
    try:
        config = dt._build_styled_export_llm_config()
    finally:
        reset_task_env_overlay(token)

    assert config["general"]["model_name"] == "glm-5.2"
    assert config["general"]["model_type"] == "openai"
    assert config["general"]["base_url"] == "https://client-claw.example/v2"
    assert config["general"]["api_key"] == bytearray(b"request-key")


@pytest.mark.parametrize(
    "overlay",
    [
        {
            "MODEL_PROVIDER": "OpenAI",
            "API_BASE": "https://llm.example/v1",
            "API_KEY": "key",
        },
        {
            "MODEL_NAME": "model",
            "MODEL_PROVIDER": "OpenAI",
            "API_KEY": "key",
        },
        {
            "MODEL_NAME": "model",
            "MODEL_PROVIDER": "OpenAI",
            "API_BASE": "https://llm.example/v1",
        },
        {
            "MODEL_NAME": "model",
            "MODEL_PROVIDER": "OpenAI",
            "API_BASE": "https://example.com/compatible-mode/v1",
            "API_KEY": "key",
        },
    ],
)
def test_styled_export_llm_config_rejects_invalid_config_before_client_creation(
    overlay,
):
    token = bind_task_env_overlay(overlay)
    try:
        with pytest.raises(
            ValueError,
            match="styled HTML LLM configuration is invalid",
        ):
            dt._build_styled_export_llm_config()
    finally:
        reset_task_env_overlay(token)


@pytest.mark.asyncio
@pytest.mark.parametrize("raise_during_use", [False, True])
async def test_styled_report_llm_context_restores_explicit_tls_value(
    monkeypatch, raise_during_use
):
    monkeypatch.setenv("LLM_SSL_VERIFY", "true")
    monkeypatch.setattr(dt, "read_env", lambda _name, _default: "true")
    observed = {}

    @asynccontextmanager
    async def fake_context_factory(llm_config):
        observed["config"] = llm_config
        observed["entry"] = os.environ.get("LLM_SSL_VERIFY")
        try:
            yield "runtime-llm"
        finally:
            observed["exit"] = os.environ.get("LLM_SSL_VERIFY")

    async def use_context():
        async with dt._scoped_report_style_llm_context(
            fake_context_factory, {"general": {"model_name": "test"}}
        ) as llm:
            assert llm == "runtime-llm"
            observed["yielded"] = os.environ.get("LLM_SSL_VERIFY")
            if raise_during_use:
                raise RuntimeError("stylization failed")

    if raise_during_use:
        with pytest.raises(RuntimeError, match="stylization failed"):
            await use_context()
    else:
        await use_context()

    assert observed == {
        "config": {"general": {"model_name": "test"}},
        "entry": "true",
        "yielded": "true",
        "exit": "true",
    }
    assert os.environ.get("LLM_SSL_VERIFY") == "true"


@pytest.mark.asyncio
async def test_styled_report_llm_context_uses_overlay_tls_only_for_entry(monkeypatch):
    monkeypatch.setenv("LLM_SSL_VERIFY", "ambient")
    read_inputs = []
    observed = {}

    def fake_read_env(name, default):
        read_inputs.append((name, default))
        return "resolved-by-overlay"

    @asynccontextmanager
    async def fake_context_factory(_llm_config):
        observed["entry"] = os.environ.get("LLM_SSL_VERIFY")
        yield "runtime-llm"

    monkeypatch.setattr(dt, "read_env", fake_read_env)

    async with dt._scoped_report_style_llm_context(
        fake_context_factory, {"general": {}}
    ):
        observed["yielded"] = os.environ.get("LLM_SSL_VERIFY")

    assert read_inputs == [("LLM_SSL_VERIFY", "false")]
    assert observed == {
        "entry": "resolved-by-overlay",
        "yielded": "ambient",
    }
    assert os.environ.get("LLM_SSL_VERIFY") == "ambient"


def test_styled_report_llm_context_serializes_concurrent_event_loops(monkeypatch):
    monkeypatch.setenv("LLM_SSL_VERIFY", "ambient")
    monkeypatch.setattr(
        dt,
        "read_env",
        lambda _name, _default: "resolved-by-overlay",
    )
    start_barrier = threading.Barrier(3)
    observation_lock = threading.Lock()
    observed = []
    active_entries = 0
    max_active_entries = 0
    errors = []

    @asynccontextmanager
    async def fake_context_factory(_llm_config):
        nonlocal active_entries, max_active_entries
        with observation_lock:
            active_entries += 1
            max_active_entries = max(max_active_entries, active_entries)
            observed.append(("entry", os.environ.get("LLM_SSL_VERIFY")))
        await asyncio.sleep(0.02)
        with observation_lock:
            active_entries -= 1
        yield "runtime-llm"

    async def use_context():
        async with dt._scoped_report_style_llm_context(
            fake_context_factory, {"general": {}}
        ):
            with observation_lock:
                observed.append(("yielded", os.environ.get("LLM_SSL_VERIFY")))

    def run_in_thread():
        try:
            start_barrier.wait(timeout=1)
            asyncio.run(use_context())
        except BaseException as exc:  # pragma: no cover - assertion reports details
            errors.append(exc)

    threads = [threading.Thread(target=run_in_thread) for _ in range(2)]
    for thread in threads:
        thread.start()
    start_barrier.wait(timeout=1)
    for thread in threads:
        thread.join(timeout=1)

    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    assert max_active_entries == 1
    assert len(observed) == 4
    assert observed.count(("entry", "resolved-by-overlay")) == 2
    assert observed.count(("yielded", "ambient")) == 2
    assert os.environ.get("LLM_SSL_VERIFY") == "ambient"


def test_styled_report_llm_context_wait_cancellation_allows_loop_shutdown():
    lock = dt._REPORT_STYLE_LLM_INIT_LOCK
    lock.acquire()
    cancellation_started = threading.Event()
    runner_errors = []

    @asynccontextmanager
    async def fake_context_factory(_llm_config):
        yield "runtime-llm"  # pragma: no cover - lock stays held by the test

    def run_cancelled_waiter():
        async def scenario():
            context = dt._scoped_report_style_llm_context(
                fake_context_factory, {"general": {}}
            )
            task = asyncio.create_task(context.__aenter__())
            await asyncio.sleep(0.02)
            task.cancel()
            cancellation_started.set()
            with suppress(asyncio.CancelledError):
                await task

        try:
            asyncio.run(scenario())
        except BaseException as exc:  # pragma: no cover - assertion reports details
            runner_errors.append(exc)

    thread = threading.Thread(target=run_cancelled_waiter)
    thread.start()
    try:
        assert cancellation_started.wait(timeout=1)
        thread.join(timeout=0.2)
        shutdown_completed_while_locked = not thread.is_alive()
    finally:
        lock.release()
        thread.join(timeout=1)

    assert shutdown_completed_while_locked is True
    assert not thread.is_alive()
    assert runner_errors == []
    assert lock.acquire(blocking=False)
    lock.release()


def test_styled_report_llm_context_does_not_starve_limited_default_executor():
    lock = dt._REPORT_STYLE_LLM_INIT_LOCK
    lock.acquire()

    @asynccontextmanager
    async def fake_context_factory(_llm_config):
        yield "runtime-llm"  # pragma: no cover - lock stays held by the test

    async def scenario():
        asyncio.get_running_loop().set_default_executor(
            ThreadPoolExecutor(max_workers=1)
        )
        context = dt._scoped_report_style_llm_context(
            fake_context_factory, {"general": {}}
        )
        waiter = asyncio.create_task(context.__aenter__())
        await asyncio.sleep(0.02)
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(lambda: "executor-available"),
                timeout=0.1,
            )
        finally:
            waiter.cancel()
            lock.release()
            with suppress(asyncio.CancelledError):
                await waiter

    previous_loop = asyncio.get_event_loop_policy().get_event_loop()
    try:
        result = asyncio.run(scenario())
    finally:
        asyncio.set_event_loop(previous_loop)

    assert result == "executor-available"
    assert lock.acquire(blocking=False)
    lock.release()


@pytest.mark.asyncio
async def test_styled_report_llm_context_restores_unset_tls_after_entry_failure(
    monkeypatch,
):
    monkeypatch.delenv("LLM_SSL_VERIFY", raising=False)

    @asynccontextmanager
    async def failing_context_factory(_llm_config):
        assert os.environ.get("LLM_SSL_VERIFY") == "false"
        raise RuntimeError("context entry failed")
        yield  # pragma: no cover

    with pytest.raises(RuntimeError, match="context entry failed"):
        async with dt._scoped_report_style_llm_context(
            failing_context_factory, {"general": {}}
        ):
            pass

    assert "LLM_SSL_VERIFY" not in os.environ

    @asynccontextmanager
    async def succeeding_context_factory(_llm_config):
        yield "runtime-llm"

    async def reenter():
        async with dt._scoped_report_style_llm_context(
            succeeding_context_factory, {"general": {}}
        ) as llm:
            assert llm == "runtime-llm"

    await asyncio.wait_for(reenter(), timeout=0.1)
    assert "LLM_SSL_VERIFY" not in os.environ


@pytest.mark.asyncio
async def test_styled_report_llm_context_releases_after_repeated_entry_cancellation(
    monkeypatch,
):
    monkeypatch.setenv("LLM_SSL_VERIFY", "ambient")
    monkeypatch.setattr(
        dt,
        "read_env",
        lambda _name, _default: "resolved-by-overlay",
    )
    entry_started = asyncio.Event()

    @asynccontextmanager
    async def blocked_context_factory(_llm_config):
        assert os.environ.get("LLM_SSL_VERIFY") == "resolved-by-overlay"
        entry_started.set()
        await asyncio.Event().wait()
        yield "runtime-llm"  # pragma: no cover

    context = dt._scoped_report_style_llm_context(
        blocked_context_factory, {"general": {}}
    )
    task = asyncio.create_task(context.__aenter__())
    await asyncio.wait_for(entry_started.wait(), timeout=0.1)
    task.cancel()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=0.1)

    assert os.environ.get("LLM_SSL_VERIFY") == "ambient"

    @asynccontextmanager
    async def succeeding_context_factory(_llm_config):
        yield "runtime-llm"

    async def reenter():
        async with dt._scoped_report_style_llm_context(
            succeeding_context_factory, {"general": {}}
        ):
            pass

    await asyncio.wait_for(reenter(), timeout=0.1)


def _patch_env(tool_lines, push=None):
    """统一 patch:Python/script 解析、route、subprocess、transport。"""
    route = (
        {"request_id": "R1", "channel_id": "CH1", "session_id": "S1"}
        if push is not None
        else {"request_id": "", "channel_id": "", "session_id": ""}
    )
    return [
        patch.object(dt, "_resolve_jiuwenclaw_python", return_value="/p"),
        patch.object(dt, "_resolve_run_script", return_value="/s"),
        patch.object(dt, "_get_route", return_value=route),
        patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=_Proc(tool_lines))),
        patch(
            "jiuwenclaw.agentserver.gateway_push.transport.WebSocketGatewayPushTransport",
            return_value=push,
        ),
    ]


def _task_updates(payloads):
    return [payload for payload in payloads if payload.get("event_type") == "task.update"]


def _active_stage(update):
    active = [
        index for index, task in enumerate(update["tasks"], start=1)
        if task["status"] == "in_progress"
    ]
    return active[0] if active else None


@pytest.mark.asyncio
async def test_tool_sends_ordered_stage_surfaces_and_retains_completed_snapshot(tmp_path):
    raw_process = "原始检索过程第一行\n\n原始检索过程第二行" + "完整内容" * 40
    lines = [
        json.dumps({"__deepsearch_status__": "started", "conversation_id": "C1"}),
        json.dumps({
            "agent": "collector_info_retrieval",
            "section_idx": "1",
            "section_title": "真实标题",
            "section_total": 1,
            "event": "start",
            "content": raw_process,
        }),
        json.dumps({
            "agent": "sub_reporter",
            "section_idx": "1",
            "section_title": "真实章节标题",
            "section_total": 1,
            "event": "done",
            "content": "SUCCESS",
        }),
        json.dumps({
            "__deepsearch_status__": "completed",
            "conversation_id": "C1",
            "final_result": {"response_content": "done"},
        }),
    ]
    report_path = tmp_path / "r.md"
    report_path.write_text("done", encoding="utf-8")
    push = AsyncMock()
    with patch.object(
             todo_progress,
             "resolve_tenant_agent_workspace_dir",
             return_value=tmp_path,
         ), \
         patch.object(dt, "_resolve_jiuwenclaw_python", return_value="/p"), \
         patch.object(dt, "_resolve_run_script", return_value="/s"), \
         patch.object(dt, "_get_route", return_value={
             "request_id": "R1", "channel_id": "CH1", "session_id": "S1"
         }), \
         patch.object(dt, "_write_report_markdown", return_value=str(report_path)), \
         patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=_Proc(lines))), \
         patch(
             "jiuwenclaw.agentserver.gateway_push.transport.WebSocketGatewayPushTransport",
             return_value=push,
         ):
        result = await dt.deepresearch_stream._func(action="start", query="X", file_name="r")

    payloads = [call.args[0]["payload"] for call in push.send_push.await_args_list]
    reasoning = [
        payload for payload in payloads if payload.get("event_type") == "chat.reasoning"
    ]
    stage_reasoning = [
        payload for payload in reasoning if not payload.get("stream_source_id")
    ]
    process_reasoning = [
        payload for payload in reasoning if payload.get("stream_source_id")
    ]
    stage_deltas = [
        payload for payload in payloads
        if payload.get("event_type") == "chat.delta"
        and payload.get("task_id", "").startswith("deepresearch_stage_")
    ]
    task_updates = _task_updates(payloads)
    assert json.loads(result)["status"] == "completed"
    assert process_reasoning == [
        {
            "event_type": "chat.reasoning",
            "task_id": "deepresearch_stage_3",
            "task_content": "真实标题",
            "task_index": 1,
            "total_tasks": 1,
            "stream_source_id": "deepresearch_section_1",
            "content": "资料检索开始\n",
        },
        {
            "event_type": "chat.reasoning",
            "task_id": "deepresearch_stage_3",
            "task_content": "真实标题",
            "task_index": 1,
            "total_tasks": 1,
            "stream_source_id": "deepresearch_section_1",
            "content": raw_process,
        },
        {
            "event_type": "chat.reasoning",
            "task_id": "deepresearch_stage_3",
            "task_content": "真实章节标题",
            "task_index": 1,
            "total_tasks": 1,
            "stream_source_id": "deepresearch_section_1",
            "content": "章节撰写完成\n",
        },
    ]
    assert [payload["content"] for payload in stage_reasoning] == [
        "[DeepResearch 阶段切换] 开始 Stage 1：研究主题澄清\n",
        "[DeepResearch 阶段切换] 开始 Stage 2：大纲生成\n",
        "[DeepResearch 阶段切换] 开始 Stage 3：并行调研与章节撰写\n",
        "[DeepResearch 阶段切换] 开始 Stage 4：报告交付\n",
        "[DeepResearch 阶段完成] Stage 4：报告交付\n",
    ]
    assert [payload["content"] for payload in stage_deltas] == [
        "[DeepResearch 阶段切换] 开始 Stage 1：研究主题澄清\n",
        "[DeepResearch 阶段切换] 开始 Stage 2：大纲生成\n",
        "[DeepResearch 阶段切换] 开始 Stage 3：并行调研与章节撰写\n",
        "[DeepResearch 阶段切换] 开始 Stage 4：报告交付\n",
        "[DeepResearch 阶段完成] Stage 4：报告交付\n",
    ]
    assert [_active_stage(update) for update in task_updates] == [
        1, 2, 3, 4, None,
    ]
    completed_update = task_updates[-1]
    assert [task["task_id"] for task in completed_update["tasks"]] == [
        f"deepresearch_stage_{index}" for index in range(1, 5)
    ]
    assert all(task["status"] == "completed" for task in completed_update["tasks"])
    assert not any(
        payload.get("event_type") == "task.update" and not payload.get("tasks")
        for payload in payloads
    )
    persisted_todos = json.loads(
        (tmp_path / "todo" / "S1" / "todo.json").read_text(encoding="utf-8")
    )
    assert [item["id"] for item in persisted_todos] == [
        f"deepresearch_stage_{index}" for index in range(1, 5)
    ]
    assert all(item["status"] == "completed" for item in persisted_todos)
    completed_update_index = payloads.index(completed_update)
    file_index = next(
        index for index, payload in enumerate(payloads)
        if payload.get("event_type") == "chat.file"
    )
    completion_reasoning_index = next(
        index for index, payload in enumerate(payloads)
        if payload.get("event_type") == "chat.reasoning"
        and payload.get("content") == "[DeepResearch 阶段完成] Stage 4：报告交付\n"
    )
    completion_delta_index = next(
        index for index, payload in enumerate(payloads)
        if payload.get("event_type") == "chat.delta"
        and payload.get("content") == "[DeepResearch 阶段完成] Stage 4：报告交付\n"
    )
    assert file_index < completed_update_index < completion_reasoning_index < completion_delta_index
    nested_boundaries = [
        (payload["event_type"], payload.get("stream_source_id"))
        for payload in payloads
        if payload.get("event_type") in {"task.start", "task.complete"}
    ]
    assert nested_boundaries == [
        ("task.start", "deepresearch_section_1"),
        ("task.complete", "deepresearch_section_1"),
        ("task.start", "deepresearch_final_report"),
        ("task.complete", "deepresearch_final_report"),
    ]
    aggregate_complete_index = next(
        index for index, payload in enumerate(payloads)
        if payload.get("event_type") == "task.complete"
        and payload.get("stream_source_id") == "deepresearch_final_report"
    )
    aggregate_start_index = next(
        index for index, payload in enumerate(payloads)
        if payload.get("event_type") == "task.start"
        and payload.get("stream_source_id") == "deepresearch_final_report"
    )
    stage_four_update_index = payloads.index(task_updates[-2])
    assert (
        stage_four_update_index
        < aggregate_start_index
        < aggregate_complete_index
        < file_index
    )
    aggregate_payloads = [
        payload for payload in payloads
        if payload.get("stream_source_id") == "deepresearch_final_report"
    ]
    assert aggregate_payloads
    assert all(
        payload.get("task_id") == "deepresearch_stage_4"
        for payload in aggregate_payloads
    )

    for update in task_updates[:-1]:
        active_stage = _active_stage(update)
        title = update["tasks"][active_stage - 1]["task_content"]
        content = f"[DeepResearch 阶段切换] 开始 Stage {active_stage}：{title}\n"
        update_index = payloads.index(update)
        reasoning_index = next(
            index for index, payload in enumerate(payloads)
            if payload.get("event_type") == "chat.reasoning"
            and payload.get("content") == content
        )
        delta_index = next(
            index for index, payload in enumerate(payloads)
            if payload.get("event_type") == "chat.delta"
            and payload.get("content") == content
        )
        assert update_index < reasoning_index < delta_index
    assert any(
        payload.get("event_type") == "chat.processing_status"
        and payload.get("is_processing") is True
        for payload in payloads
    )


def _styled_report_archive(
    html: str,
    assets: dict[str, bytes | str] | None = None,
) -> str:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("report_bundle/report.html", html)
        for name, content in (assets or {}).items():
            archive.writestr(f"report_bundle/{name}", content)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


@asynccontextmanager
async def _async_context(value):
    yield value


def _valid_styled_export_llm_config():
    return {"general": {"model_name": "test-model"}}


@pytest.mark.asyncio
async def test_generate_report_html_installs_styled_report(tmp_path):
    report_path_md = tmp_path / "report.md"
    report_path_md.write_text("# Report", encoding="utf-8")
    styled_result = SimpleNamespace(
        convert_content=_styled_report_archive(
            '<html><a href="infer/new.html">styled</a>'
            '<img src="charts/new.png"></html>',
            {
                "infer/new.html": "<html>new inference</html>",
                "charts/new.png": b"new chart",
            },
        )
    )
    llm = object()
    infer_dir = tmp_path / "report_infer"
    chart_dir = tmp_path / "report_charts"
    infer_dir.mkdir()
    chart_dir.mkdir()
    (infer_dir / "existing.html").write_text("existing inference", encoding="utf-8")
    (chart_dir / "existing.png").write_bytes(b"existing chart")

    with patch.object(
        dt,
        "_build_styled_export_llm_config",
        return_value=_valid_styled_export_llm_config(),
    ), patch.object(
        dt,
        "_scoped_report_style_llm_context",
        return_value=_async_context(llm),
    ), patch(
        "openjiuwen_deepsearch.algorithm.report_style.service.stylize_report",
        new=AsyncMock(return_value=styled_result),
    ) as stylize:
        html_path = await dt._generate_report_html(
            {"response_content": "# Report"}, report_path_md, "# Report"
        )

    assert html_path == tmp_path / "report.html"
    generated_infer_dir = tmp_path / "report_html_infer"
    generated_chart_dir = tmp_path / "report_html_charts"
    assert html_path.read_text(encoding="utf-8") == (
        f'<html><a href="{generated_infer_dir.name}/new.html">styled</a>'
        f'<img src="{generated_chart_dir.name}/new.png"></html>'
    )
    assert (generated_infer_dir / "new.html").read_text(encoding="utf-8") == (
        "<html>new inference</html>"
    )
    assert (generated_chart_dir / "new.png").read_bytes() == b"new chart"
    assert (infer_dir / "existing.html").read_text(encoding="utf-8") == (
        "existing inference"
    )
    assert (chart_dir / "existing.png").read_bytes() == b"existing chart"
    assert sorted(path.name for path in infer_dir.iterdir()) == ["existing.html"]
    assert sorted(path.name for path in chart_dir.iterdir()) == ["existing.png"]
    stylize.assert_awaited_once_with({"response_content": "# Report"}, llm)


@pytest.mark.asyncio
async def test_generate_report_html_does_not_depend_on_supports_dir_fd(tmp_path):
    report_path_md = tmp_path / "report.md"
    report_path_md.write_text("# Report", encoding="utf-8")
    fixed_infer_dir = tmp_path / "report_infer"
    fixed_chart_dir = tmp_path / "report_charts"
    fixed_infer_dir.mkdir()
    fixed_chart_dir.mkdir()
    (fixed_infer_dir / "existing.html").write_text("existing", encoding="utf-8")
    (fixed_chart_dir / "existing.png").write_bytes(b"existing")
    styled_result = SimpleNamespace(
        convert_content=_styled_report_archive(
            '<html><a href="infer/new.html">styled</a>'
            '<img src="charts/new.png"></html>',
            {
                "infer/new.html": "<html>new inference</html>",
                "charts/new.png": b"new chart",
            },
        )
    )
    dir_fd_calls = []

    def _without_dir_fd(function):
        def _wrapped(*args, **kwargs):
            caller = inspect.currentframe().f_back
            if (
                caller is not None
                and Path(caller.f_code.co_filename) == Path(dt.__file__)
                and any(
                    kwargs.get(name) is not None
                    for name in ("dir_fd", "src_dir_fd", "dst_dir_fd")
                )
            ):
                dir_fd_calls.append(function.__name__)
                raise NotImplementedError("dir_fd is unavailable")
            return function(*args, **kwargs)

        return _wrapped

    with patch.object(
        dt,
        "_build_styled_export_llm_config",
        return_value=_valid_styled_export_llm_config(),
    ), patch.object(
        dt,
        "_scoped_report_style_llm_context",
        return_value=_async_context(object()),
    ), patch(
        "openjiuwen_deepsearch.algorithm.report_style.service.stylize_report",
        new=AsyncMock(return_value=styled_result),
    ), patch.object(
        dt.os, "open", side_effect=_without_dir_fd(dt.os.open)
    ), patch.object(
        dt.os, "stat", side_effect=_without_dir_fd(dt.os.stat)
    ), patch.object(
        dt.os, "mkdir", side_effect=_without_dir_fd(dt.os.mkdir)
    ), patch.object(
        dt.os, "replace", side_effect=_without_dir_fd(dt.os.replace)
    ), patch.object(
        dt.os, "unlink", side_effect=_without_dir_fd(dt.os.unlink)
    ), patch.object(
        dt.os, "supports_dir_fd", set()
    ):
        html_path = await dt._generate_report_html(
            {"response_content": "# Verified report"},
            report_path_md,
            "# Verified report",
        )

    assert dir_fd_calls == []
    assert html_path == tmp_path / "report.html"
    html = html_path.read_text(encoding="utf-8")
    assert "styled" in html
    generated_infer_dir = tmp_path / "report_html_infer"
    generated_chart_dir = tmp_path / "report_html_charts"
    assert f'href="{generated_infer_dir.name}/new.html"' in html
    assert f'src="{generated_chart_dir.name}/new.png"' in html
    assert (generated_infer_dir / "new.html").is_file()
    assert (generated_chart_dir / "new.png").read_bytes() == b"new chart"
    assert sorted(path.name for path in fixed_infer_dir.iterdir()) == [
        "existing.html"
    ]
    assert sorted(path.name for path in fixed_chart_dir.iterdir()) == [
        "existing.png"
    ]


@pytest.mark.asyncio
async def test_generate_report_html_falls_back_to_offline_conversion(tmp_path):
    report_path_md = tmp_path / "report.md"
    report_path_md.write_text("# Report", encoding="utf-8")

    def _convert(_markdown_path, html_path):
        with open(html_path, "w", encoding="utf-8") as stream:
            stream.write("<html>offline report</html>")

    with patch.object(
        dt,
        "_build_styled_export_llm_config",
        side_effect=RuntimeError("styled export unavailable"),
    ), patch(
        "jiuwenclaw.agentserver.tools.deepresearch_plugin.convert_html_offline.convert_md_to_html",
        side_effect=_convert,
    ):
        html_path = await dt._generate_report_html(
            {"response_content": "# Report"}, report_path_md, "# Report"
        )

    assert html_path == tmp_path / "report.html"
    assert html_path.read_text(encoding="utf-8") == "<html>offline report</html>"


@pytest.mark.asyncio
async def test_generate_report_html_fallback_uses_verified_content_after_source_mutation(
    tmp_path,
):
    report_path_md = tmp_path / "report.md"
    report_path_md.write_text("# Verified report", encoding="utf-8")

    def _mutate_then_fail():
        report_path_md.write_text("# SECRET mutation", encoding="utf-8")
        raise RuntimeError("styled export unavailable")

    with patch.object(
        dt,
        "_build_styled_export_llm_config",
        side_effect=_mutate_then_fail,
    ):
        html_path = await dt._generate_report_html(
            {"response_content": "# Verified report"},
            report_path_md,
            "# Verified report",
        )

    assert html_path == tmp_path / "report.html"
    html = html_path.read_text(encoding="utf-8")
    assert "Verified report" in html
    assert "SECRET mutation" not in html


@pytest.mark.asyncio
async def test_generate_report_html_rejects_html_symlink_without_overwriting_target(
    tmp_path,
):
    report_path_md = tmp_path / "report.md"
    report_path_md.write_text("# Report", encoding="utf-8")
    outside = tmp_path / "outside.html"
    outside.write_text("outside sentinel", encoding="utf-8")
    (tmp_path / "report.html").symlink_to(outside)

    with patch.object(
        dt,
        "_build_styled_export_llm_config",
        side_effect=RuntimeError("styled export unavailable"),
    ):
        html_path = await dt._generate_report_html(
            {"response_content": "# Verified report"},
            report_path_md,
            "# Verified report",
        )

    assert html_path is None
    assert outside.read_text(encoding="utf-8") == "outside sentinel"
    assert (tmp_path / "report.html").is_symlink()


@pytest.mark.asyncio
async def test_generate_report_html_does_not_follow_legacy_fixed_temp_symlink(tmp_path):
    report_path_md = tmp_path / "report.md"
    report_path_md.write_text("# Report", encoding="utf-8")
    outside = tmp_path / "outside.tmp"
    outside.write_text("outside sentinel", encoding="utf-8")
    legacy_temp = tmp_path / "report.html.tmp"
    legacy_temp.symlink_to(outside)
    styled_result = SimpleNamespace(
        convert_content=_styled_report_archive("<html>styled report</html>")
    )

    with patch.object(
        dt,
        "_build_styled_export_llm_config",
        return_value=_valid_styled_export_llm_config(),
    ), patch.object(
        dt,
        "_scoped_report_style_llm_context",
        return_value=_async_context(object()),
    ), patch(
        "openjiuwen_deepsearch.algorithm.report_style.service.stylize_report",
        new=AsyncMock(return_value=styled_result),
    ):
        html_path = await dt._generate_report_html(
            {"response_content": "# Report"}, report_path_md, "# Report"
        )

    assert html_path == tmp_path / "report.html"
    assert html_path.read_text(encoding="utf-8") == "<html>styled report</html>"
    assert outside.read_text(encoding="utf-8") == "outside sentinel"
    assert legacy_temp.is_symlink()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("asset_root_name", "asset_name"),
    [
        ("report_html_infer", "infer/new.html"),
        ("report_html_charts", "charts/new.png"),
    ],
)
async def test_generate_report_html_does_not_follow_asset_root_symlink(
    tmp_path, asset_root_name, asset_name
):
    report_path_md = tmp_path / "report.md"
    report_path_md.write_text("# Report", encoding="utf-8")
    outside_dir = tmp_path / f"outside_{asset_root_name}"
    outside_dir.mkdir()
    sentinel = outside_dir / "sentinel"
    sentinel.write_text("outside sentinel", encoding="utf-8")
    (tmp_path / asset_root_name).symlink_to(outside_dir, target_is_directory=True)
    styled_result = SimpleNamespace(
        convert_content=_styled_report_archive(
            (
                '<html><a href="infer/new.html">styled report</a></html>'
                if asset_root_name == "report_html_infer"
                else '<html><img src="charts/new.png">styled report</html>'
            ),
            {asset_name: b"untrusted asset"},
        )
    )

    with patch.object(
        dt,
        "_build_styled_export_llm_config",
        return_value=_valid_styled_export_llm_config(),
    ), patch.object(
        dt,
        "_scoped_report_style_llm_context",
        return_value=_async_context(object()),
    ), patch(
        "openjiuwen_deepsearch.algorithm.report_style.service.stylize_report",
        new=AsyncMock(return_value=styled_result),
    ):
        html_path = await dt._generate_report_html(
            {"response_content": "# Verified report"},
            report_path_md,
            "# Verified report",
        )

    assert html_path == tmp_path / "report.html"
    html = html_path.read_text(encoding="utf-8")
    assert "Verified report" in html
    assert (tmp_path / asset_root_name).is_symlink()
    assert sentinel.read_text(encoding="utf-8") == "outside sentinel"
    assert sorted(path.name for path in outside_dir.iterdir()) == ["sentinel"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("asset_root_name", "asset_name"),
    [
        ("report_html_infer", "infer/new.html"),
        ("report_html_charts", "charts/new.png"),
    ],
)
async def test_generate_report_html_ignores_fixed_asset_root_swapped_to_symlink(
    tmp_path, asset_root_name, asset_name
):
    report_path_md = tmp_path / "report.md"
    report_path_md.write_text("# Report", encoding="utf-8")
    asset_root = tmp_path / asset_root_name
    asset_root.mkdir()
    (asset_root / "existing").write_text("existing asset", encoding="utf-8")
    held_root = tmp_path / f"held_{asset_root_name}"
    outside_dir = tmp_path / f"outside_{asset_root_name}"
    outside_dir.mkdir()
    sentinel = outside_dir / "sentinel"
    sentinel.write_text("outside sentinel", encoding="utf-8")
    asset_root.rename(held_root)
    asset_root.symlink_to(outside_dir, target_is_directory=True)
    styled_result = SimpleNamespace(
        convert_content=_styled_report_archive(
            (
                '<html><a href="infer/new.html">styled report</a></html>'
                if asset_root_name == "report_html_infer"
                else '<html><img src="charts/new.png">styled report</html>'
            ),
            {asset_name: b"untrusted asset"},
        )
    )

    with patch.object(
        dt,
        "_build_styled_export_llm_config",
        return_value=_valid_styled_export_llm_config(),
    ), patch.object(
        dt,
        "_scoped_report_style_llm_context",
        return_value=_async_context(object()),
    ), patch(
        "openjiuwen_deepsearch.algorithm.report_style.service.stylize_report",
        new=AsyncMock(return_value=styled_result),
    ):
        html_path = await dt._generate_report_html(
            {"response_content": "# Verified report"},
            report_path_md,
            "# Verified report",
        )

    assert html_path == tmp_path / "report.html"
    html = html_path.read_text(encoding="utf-8")
    assert "Verified report" in html
    assert asset_root.is_symlink()
    assert (held_root / "existing").read_text(encoding="utf-8") == "existing asset"
    assert sentinel.read_text(encoding="utf-8") == "outside sentinel"
    assert sorted(path.name for path in outside_dir.iterdir()) == ["sentinel"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_stage", ["asset_copy", "html_install"])
async def test_generate_report_html_rolls_back_owned_assets_when_install_fails(
    tmp_path, failure_stage
):
    report_path_md = tmp_path / "report.md"
    report_path_md.write_text("# Report", encoding="utf-8")
    fixed_infer_dir = tmp_path / "report_infer"
    fixed_chart_dir = tmp_path / "report_charts"
    fixed_infer_dir.mkdir()
    fixed_chart_dir.mkdir()
    (fixed_infer_dir / "existing").write_text("existing", encoding="utf-8")
    (fixed_chart_dir / "existing").write_text("existing", encoding="utf-8")
    styled_result = SimpleNamespace(
        convert_content=_styled_report_archive(
            '<html><a href="infer/new.html">styled report</a>'
            '<img src="charts/new.png"></html>',
            {
                "infer/new.html": b"new inference",
                "charts/new.png": b"new chart",
            },
        )
    )
    real_atomic_create = dt._atomic_create_bytes
    real_atomic_create_at = dt._atomic_create_bytes_at

    def _fail_asset_copy(directory_fd, name, payload):
        if failure_stage == "asset_copy" and payload == b"new chart":
            raise OSError("styled asset copy failed")
        return real_atomic_create_at(directory_fd, name, payload)

    def _fail_html_install(path, payload):
        if (
            failure_stage == "html_install"
            and path == tmp_path / "report.html"
            and b"styled report" in payload
        ):
            raise OSError("styled HTML install failed")
        return real_atomic_create(path, payload)

    with patch.object(
        dt,
        "_build_styled_export_llm_config",
        return_value=_valid_styled_export_llm_config(),
    ), patch.object(
        dt,
        "_scoped_report_style_llm_context",
        return_value=_async_context(object()),
    ), patch(
        "openjiuwen_deepsearch.algorithm.report_style.service.stylize_report",
        new=AsyncMock(return_value=styled_result),
    ), patch.object(
        dt,
        "_atomic_create_bytes_at",
        side_effect=_fail_asset_copy,
    ), patch.object(
        dt,
        "_atomic_create_bytes",
        side_effect=_fail_html_install,
    ):
        html_path = await dt._generate_report_html(
            {"response_content": "# Verified report"},
            report_path_md,
            "# Verified report",
        )

    assert html_path == tmp_path / "report.html"
    assert "Verified report" in html_path.read_text(encoding="utf-8")
    assert not (tmp_path / "report_html_infer").exists()
    assert not (tmp_path / "report_html_charts").exists()
    assert sorted(path.name for path in fixed_infer_dir.iterdir()) == ["existing"]
    assert sorted(path.name for path in fixed_chart_dir.iterdir()) == ["existing"]


@pytest.mark.asyncio
async def test_generate_report_html_preserves_replaced_published_asset_dir(
    tmp_path,
):
    report_path_md = tmp_path / "report.md"
    report_path_md.write_text("# Report", encoding="utf-8")
    styled_result = SimpleNamespace(
        convert_content=_styled_report_archive(
            '<html><a href="infer/new.html">styled report</a></html>',
            {"infer/new.html": b"published asset"},
        )
    )
    real_atomic_create = dt._atomic_create_bytes
    held_published = tmp_path / "held_published_assets"
    replacement_dir = None

    def _replace_final_then_fail_html(path, payload):
        nonlocal replacement_dir
        if path == tmp_path / "report.html" and b"styled report" in payload:
            published_dir = tmp_path / "report_html_infer"
            published_dir.rename(held_published)
            published_dir.mkdir()
            (published_dir / "replacement-sentinel").write_text(
                "replacement sentinel", encoding="utf-8"
            )
            replacement_dir = published_dir
            raise OSError("styled HTML install failed")
        return real_atomic_create(path, payload)

    with patch.object(
        dt,
        "_build_styled_export_llm_config",
        return_value=_valid_styled_export_llm_config(),
    ), patch.object(
        dt,
        "_scoped_report_style_llm_context",
        return_value=_async_context(object()),
    ), patch(
        "openjiuwen_deepsearch.algorithm.report_style.service.stylize_report",
        new=AsyncMock(return_value=styled_result),
    ), patch.object(
        dt,
        "_atomic_create_bytes",
        side_effect=_replace_final_then_fail_html,
    ):
        html_path = await dt._generate_report_html(
            {"response_content": "# Verified report"},
            report_path_md,
            "# Verified report",
        )

    assert html_path == tmp_path / "report.html"
    assert "Verified report" in html_path.read_text(encoding="utf-8")
    assert replacement_dir is not None
    assert (replacement_dir / "replacement-sentinel").read_text(
        encoding="utf-8"
    ) == "replacement sentinel"
    assert list(held_published.iterdir()) == []


@pytest.mark.asyncio
async def test_generate_report_html_logs_only_stable_exception_types(tmp_path, caplog):
    report_path_md = tmp_path / "report.md"
    report_path_md.write_text("# Report", encoding="utf-8")
    caplog.set_level(logging.WARNING, logger=dt.__name__)
    dt.logger.addHandler(caplog.handler)
    try:
        with patch.object(
            dt,
            "_build_styled_export_llm_config",
            side_effect=RuntimeError("SECRET /internal/styled"),
        ), patch(
            "jiuwenclaw.agentserver.tools.deepresearch_plugin.convert_html_offline.convert_md_to_html",
            side_effect=OSError("SECRET /internal/fallback"),
        ):
            html_path = await dt._generate_report_html(
                {"response_content": "# Verified report"},
                report_path_md,
                "# Verified report",
            )
    finally:
        dt.logger.removeHandler(caplog.handler)

    assert html_path is None
    assert "RuntimeError" in caplog.text
    assert "OSError" in caplog.text
    assert "SECRET" not in caplog.text
    assert "/internal" not in caplog.text


@pytest.mark.asyncio
async def test_generate_report_html_propagates_cancellation(tmp_path):
    report_path_md = tmp_path / "report.md"
    report_path_md.write_text("# Report", encoding="utf-8")

    @asynccontextmanager
    async def _cancelled_context(_context_factory, _llm_config):
        raise asyncio.CancelledError
        yield  # pragma: no cover

    with patch.object(
        dt,
        "_build_styled_export_llm_config",
        return_value=_valid_styled_export_llm_config(),
    ), patch.object(
        dt,
        "_scoped_report_style_llm_context",
        side_effect=_cancelled_context,
    ):
        with pytest.raises(asyncio.CancelledError):
            await dt._generate_report_html(
                {"response_content": "# Verified report"},
                report_path_md,
                "# Verified report",
            )


@pytest.mark.asyncio
async def test_generate_report_html_preserves_existing_output_when_both_paths_fail(
    tmp_path,
):
    report_path_md = tmp_path / "report.md"
    report_path_md.write_text("# Report", encoding="utf-8")
    report_path_html = tmp_path / "report.html"
    report_path_html.write_text("<html>partial</html>", encoding="utf-8")

    with patch.object(
        dt,
        "_build_styled_export_llm_config",
        side_effect=RuntimeError("styled export unavailable"),
    ), patch(
        "jiuwenclaw.agentserver.tools.deepresearch_plugin.convert_html_offline.convert_md_to_html",
        side_effect=RuntimeError("offline conversion unavailable"),
    ):
        html_path = await dt._generate_report_html(
            {"response_content": "# Report"}, report_path_md, "# Report"
        )

    assert html_path is None
    assert report_path_html.read_text(encoding="utf-8") == "<html>partial</html>"


def test_write_report_markdown_builds_inference_bundle_and_strips_internal_markers(tmp_path):
    final_result = {
        "response_content": (
            "# 报告\n\n"
            "[观点](#inference:7)"
            "[checked_citation:3][[1]](https://example.com/source)\n"
        ),
        "infer_messages": [{
            "id": "7",
            "html_base64": base64.b64encode(b"<html>trace</html>").decode("ascii"),
        }],
        "chart_messages": [{
            "chart_id": "chart-1",
            "chart_title": "趋势图",
            "base64": base64.b64encode(b"png-bytes").decode("ascii"),
        }],
        "request_metadata": {"trace_id": "trace-1"},
        "citation_messages": {
            "code": 0,
            "msg": "success",
            "data": [{
                "id": 3,
                "reference_index": 1,
                "url": "https://example.com/source",
                "title": "Source",
                "content": "evidence",
                "chunk": "evidence chunk",
                "source": "web",
                "publish_time": "2026-07-15",
                "score": 0.9,
            }],
        },
    }
    with patch(
        "jiuwenclaw.agentserver.tools.subagent_executor.context_vars.get_effective_request_output_dir",
        return_value=str(tmp_path),
    ):
        report_path = dt._write_report_markdown(final_result, "研究报告.md", "C1")

    assert report_path == str(tmp_path / "研究报告-v1.md")
    report = (tmp_path / "研究报告-v1.md").read_text(encoding="utf-8")
    assert "checked_citation" not in report
    assert "[观点](研究报告-v1_infer/inference_7.html)" in report
    assert "[[1]](https://example.com/source)" in report
    assert (tmp_path / "研究报告-v1_infer" / "inference_7.html").read_bytes() == b"<html>trace</html>"
    provenance = json.loads((tmp_path / "研究报告-v1.provenance.json").read_text(encoding="utf-8"))
    snapshot_path = tmp_path / "研究报告-v1.final-result.json"
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    assert snapshot["response_content"] == final_result["response_content"]
    assert snapshot["citation_messages"] == final_result["citation_messages"]
    assert snapshot["request_metadata"] == {"trace_id": "trace-1"}
    assert "html_base64" not in snapshot["infer_messages"][0]
    assert snapshot["infer_messages"][0]["artifact_path"] == "研究报告-v1_infer/inference_7.html"
    assert "base64" not in snapshot["chart_messages"][0]
    assert snapshot["chart_messages"][0]["artifact_path"] == "研究报告-v1_charts/chart-1.png"
    assert provenance["schema_version"] == 2
    assert provenance["version_number"] == 1
    assert provenance["version_base_stem"] == "研究报告"
    assert provenance["document_id"].startswith("doc_")
    assert provenance["revision_id"].startswith("rev_")
    assert provenance["parent_revision_id"] is None
    assert provenance["conversation_id"] == "C1"
    assert provenance["content_sha256"] == hashlib.sha256(report.encode("utf-8")).hexdigest()
    assert provenance["final_result_path"] == snapshot_path.name
    assert provenance["final_result_sha256"] == hashlib.sha256(snapshot_path.read_bytes()).hexdigest()
    assert provenance["citations"] == final_result["citation_messages"]["data"]
    assert provenance["inference_manifest"][0]["id"] == "7"
    assert "html_base64" not in json.dumps(provenance)


@pytest.mark.asyncio
async def test_write_report_artifacts_keeps_rewrite_sidecars_hidden(tmp_path):
    final_result = {
        "response_content": "# 报告\n\n正文",
        "infer_messages": [],
        "chart_messages": [],
    }

    def _convert(_markdown_path, html_path):
        with open(html_path, "w", encoding="utf-8") as stream:
            stream.write("<html>report</html>")

    with patch(
        "jiuwenclaw.agentserver.tools.subagent_executor.context_vars.get_effective_request_output_dir",
        return_value=str(tmp_path),
    ), patch(
        "jiuwenclaw.agentserver.tools.deepresearch_plugin.convert_html_offline.convert_md_to_html",
        side_effect=_convert,
    ):
        artifacts = await dt._write_report_artifacts_stream(
            final_result,
            "研究报告.md",
            "C1",
            {
                "raw_report_path": "/skill/data/C1.raw_report.md",
                "citations_preview_path": "/skill/data/C1.citations.preview.json",
                "citations_path": "/skill/data/C1.citations.json",
            },
        )

    assert artifacts == {
        "md": str(tmp_path / "研究报告-v1.md"),
        "html": str(tmp_path / "研究报告-v1.html"),
    }
    assert (tmp_path / "研究报告-v1.final-result.json").is_file()
    provenance = json.loads(
        (tmp_path / "研究报告-v1.provenance.json").read_text(encoding="utf-8")
    )
    assert provenance["citation_artifacts"] == {
        "raw_report_path": "/skill/data/C1.raw_report.md",
        "citations_preview_path": "/skill/data/C1.citations.preview.json",
    }
    assert "citations_path" not in provenance["citation_artifacts"]


@pytest.mark.asyncio
async def test_write_report_artifacts_keeps_html_when_fallback_markdown_read_fails(
    tmp_path, caplog
):
    final_result = {"response_content": "# Report"}
    report_path = tmp_path / "report.md"
    html_path = tmp_path / "report.html"
    generator = AsyncMock(return_value=html_path)
    caplog.set_level(logging.WARNING, logger=dt.__name__)
    dt.logger.addHandler(caplog.handler)
    try:
        with patch.object(
            dt,
            "_write_report_markdown",
            return_value=str(report_path),
        ), patch.object(
            dt.Path,
            "read_text",
            side_effect=OSError("SECRET /internal/report.md"),
        ), patch.object(
            dt,
            "_generate_report_html",
            generator,
        ):
            artifacts = await dt._write_report_artifacts_stream(
                final_result, "report.md", "C1"
            )
    finally:
        dt.logger.removeHandler(caplog.handler)

    assert artifacts == {"md": str(report_path), "html": str(html_path)}
    generator.assert_awaited_once_with(final_result, report_path, None)
    assert "OSError" in caplog.text
    assert "SECRET" not in caplog.text
    assert "/internal" not in caplog.text


@pytest.mark.asyncio
async def test_write_report_artifacts_keeps_markdown_when_read_and_styled_fail(
    tmp_path, caplog
):
    final_result = {"response_content": "# Report"}
    report_path = tmp_path / "report.md"
    generator = AsyncMock(return_value=None)
    read_error = UnicodeDecodeError(
        "utf-8", b"SECRET", 0, 1, "SECRET /internal/report.md"
    )
    caplog.set_level(logging.WARNING, logger=dt.__name__)
    dt.logger.addHandler(caplog.handler)
    try:
        with patch.object(
            dt,
            "_write_report_markdown",
            return_value=str(report_path),
        ), patch.object(
            dt.Path,
            "read_text",
            side_effect=read_error,
        ), patch.object(
            dt,
            "_generate_report_html",
            generator,
        ):
            artifacts = await dt._write_report_artifacts_stream(
                final_result, "report.md", "C1"
            )
    finally:
        dt.logger.removeHandler(caplog.handler)

    assert artifacts == {"md": str(report_path)}
    generator.assert_awaited_once_with(final_result, report_path, None)
    assert "UnicodeDecodeError" in caplog.text
    assert "SECRET" not in caplog.text
    assert "/internal" not in caplog.text


@pytest.mark.asyncio
async def test_write_report_artifacts_fallback_html_matches_delivered_markdown(tmp_path):
    final_result = {
        "response_content": (
            "# 报告\n\n"
            "[观点](#inference:7)"
            "[checked_citation:3][[1]](https://example.com/source)\n\n"
            "(#insertChart:chart-1)\n"
        ),
        "infer_messages": [{
            "id": "7",
            "html_base64": base64.b64encode(b"<html>trace</html>").decode("ascii"),
        }],
        "chart_messages": [{
            "chart_id": "chart-1",
            "chart_title": "趋势图",
            "base64": base64.b64encode(b"png-bytes").decode("ascii"),
        }],
    }

    with patch(
        "jiuwenclaw.agentserver.tools.subagent_executor.context_vars.get_effective_request_output_dir",
        return_value=str(tmp_path),
    ), patch.object(
        dt,
        "_build_styled_export_llm_config",
        side_effect=RuntimeError("styled unavailable"),
    ):
        artifacts = await dt._write_report_artifacts_stream(
            final_result, "研究报告.md", "C1"
        )

    markdown = Path(artifacts["md"]).read_text(encoding="utf-8")
    html = Path(artifacts["html"]).read_text(encoding="utf-8")
    assert "checked_citation" not in markdown
    assert "#inference:7" not in markdown
    assert "#insertChart:chart-1" not in markdown
    assert "[观点](研究报告-v1_infer/inference_7.html)" in markdown
    assert "![趋势图](研究报告-v1_charts/chart-1.png)" in markdown
    assert "checked_citation" not in html
    assert "#inference:7" not in html
    assert "#insertChart:chart-1" not in html
    assert 'href="研究报告-v1_infer/inference_7.html"' in html
    assert 'src="研究报告-v1_charts/chart-1.png"' in html


@pytest.mark.asyncio
@pytest.mark.parametrize("target_kind", ["file", "symlink"])
async def test_write_report_artifacts_fallback_omits_occupied_html_without_overwrite(
    tmp_path, target_kind
):
    html_path = tmp_path / "研究报告-v1.html"
    protected_path = tmp_path / "protected.html"
    protected_path.write_bytes(b"protected")
    if target_kind == "file":
        html_path.write_bytes(b"existing")
    else:
        html_path.symlink_to(protected_path)
    converter_outputs = []

    def _convert(_markdown_path, output_path):
        converter_outputs.append(Path(output_path))
        Path(output_path).write_bytes(b"<html>fallback</html>")

    with patch(
        "jiuwenclaw.agentserver.tools.subagent_executor.context_vars.get_effective_request_output_dir",
        return_value=str(tmp_path),
    ), patch(
        "jiuwenclaw.agentserver.tools.deepresearch_plugin.convert_html_offline.convert_md_to_html",
        side_effect=_convert,
    ), patch.object(
        dt,
        "_build_styled_export_llm_config",
        side_effect=RuntimeError("styled unavailable"),
    ):
        artifacts = await dt._write_report_artifacts_stream(
            _minimal_report_result(), "研究报告.md", "C1"
        )

    assert artifacts == {"md": str(tmp_path / "研究报告-v1.md")}
    assert converter_outputs and converter_outputs[0] != html_path
    assert protected_path.read_bytes() == b"protected"
    if target_kind == "file":
        assert html_path.read_bytes() == b"existing"
    else:
        assert html_path.is_symlink()


@pytest.mark.asyncio
async def test_write_report_artifacts_keeps_rewrite_sidecars_when_html_fails(tmp_path):
    final_result = {
        "response_content": "# 报告\n\n正文",
        "infer_messages": [],
        "chart_messages": [],
    }

    with patch(
        "jiuwenclaw.agentserver.tools.subagent_executor.context_vars.get_effective_request_output_dir",
        return_value=str(tmp_path),
    ), patch(
        "jiuwenclaw.agentserver.tools.deepresearch_plugin.convert_html_offline.convert_md_to_html",
        side_effect=RuntimeError("converter unavailable"),
    ):
        artifacts = await dt._write_report_artifacts_stream(
            final_result, "研究报告.md", "C1"
        )

    assert artifacts == {"md": str(tmp_path / "研究报告-v1.md")}
    assert (tmp_path / "研究报告-v1.final-result.json").is_file()
    assert (tmp_path / "研究报告-v1.provenance.json").is_file()


def _minimal_report_result():
    return {
        "response_content": "# 报告\n\n正文",
        "infer_messages": [],
        "chart_messages": [],
    }


def _report_result_with_assets():
    final_result = _minimal_report_result()
    final_result["infer_messages"] = [{
        "id": "7",
        "html_base64": base64.b64encode(b"<html>trace</html>").decode("ascii"),
    }]
    final_result["chart_messages"] = [{
        "chart_id": "chart-1",
        "base64": base64.b64encode(b"png-bytes").decode("ascii"),
    }]
    return final_result


def _write_report_in(tmp_path, file_name="研究报告.md", final_result=None):
    with patch(
        "jiuwenclaw.agentserver.tools.subagent_executor.context_vars.get_effective_request_output_dir",
        return_value=str(tmp_path),
    ):
        return dt._write_report_markdown(
            final_result or _minimal_report_result(), file_name, "C1"
        )


def test_write_report_markdown_allocates_same_title_ordinal(tmp_path):
    first = _write_report_in(tmp_path)
    second = _write_report_in(tmp_path)

    assert first == str(tmp_path / "研究报告-v1.md")
    assert second == str(tmp_path / "研究报告-2-v1.md")


@pytest.mark.parametrize("unsafe_name", ["", "../..", "***"])
def test_write_report_markdown_uses_default_for_empty_or_unsafe_title(
    tmp_path, unsafe_name
):
    report_path = _write_report_in(tmp_path, unsafe_name)

    assert report_path == str(tmp_path / "深度研究报告-v1.md")


def test_write_report_markdown_serializes_same_title_allocation_across_threads(
    tmp_path,
):
    with patch(
        "jiuwenclaw.agentserver.tools.subagent_executor.context_vars.get_effective_request_output_dir",
        return_value=str(tmp_path),
    ):
        with ThreadPoolExecutor(max_workers=2) as executor:
            paths = list(executor.map(
                lambda _: dt._write_report_markdown(
                    _minimal_report_result(), "研究报告.md", "C1"
                ),
                range(2),
            ))

    assert set(paths) == {
        str(tmp_path / "研究报告-v1.md"),
        str(tmp_path / "研究报告-2-v1.md"),
    }
    assert dt._REPORT_OUTPUT_LOCKS == {}


def test_report_output_lock_registry_releases_sequential_unique_outputs(tmp_path):
    for index in range(20):
        output_path = (tmp_path / f"output-{index}").resolve()
        with dt._report_output_lock(output_path):
            assert output_path in dt._REPORT_OUTPUT_LOCKS

    assert dt._REPORT_OUTPUT_LOCKS == {}


def test_report_output_lock_serializes_waiters_and_releases_registry(tmp_path):
    output_path = tmp_path.resolve()
    release_first = threading.Event()
    first_entered = threading.Event()
    second_entered = threading.Event()
    order = []

    def worker(label):
        with dt._report_output_lock(output_path):
            order.append(f"{label}:enter")
            if label == "first":
                first_entered.set()
                assert release_first.wait(timeout=2)
            else:
                second_entered.set()
            order.append(f"{label}:exit")

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(worker, "first")
        assert first_entered.wait(timeout=2)
        second = executor.submit(worker, "second")
        assert not second_entered.wait(timeout=0.05)
        release_first.set()
        first.result(timeout=2)
        second.result(timeout=2)

    assert order == [
        "first:enter",
        "first:exit",
        "second:enter",
        "second:exit",
    ]
    assert dt._REPORT_OUTPUT_LOCKS == {}


def test_windows_publication_backend_does_not_require_posix_constants(
    tmp_path, monkeypatch
):
    target = tmp_path / "report.md"
    monkeypatch.setattr(dt, "_uses_windows_path_publication", lambda: True)

    def reject_link(*_args, **_kwargs):
        raise AssertionError("Windows publication must not call os.link")

    monkeypatch.setattr(dt.os, "link", reject_link)
    metadata = dt._atomic_create_bytes(target, b"complete")

    assert target.read_bytes() == b"complete"
    assert dt._same_identity(metadata, os.lstat(target))


def test_windows_file_publication_never_overwrites_existing_target(
    tmp_path, monkeypatch
):
    target = tmp_path / "report.md"
    target.write_bytes(b"protected")
    monkeypatch.setattr(dt, "_uses_windows_path_publication", lambda: True)

    with pytest.raises(FileExistsError):
        dt._atomic_create_bytes(target, b"replacement")

    assert target.read_bytes() == b"protected"
    assert not list(tmp_path.glob(".report.md.*"))


def test_windows_asset_publication_avoids_directory_descriptors(
    tmp_path, monkeypatch
):
    staged = tmp_path / "staged"
    staged.mkdir()
    (staged / "chart.png").write_bytes(b"chart")
    final = tmp_path / "report_charts"
    created = []
    monkeypatch.setattr(dt, "_uses_windows_path_publication", lambda: True)

    dt._publish_staged_asset_directory(staged, final, created)

    assert (final / "chart.png").read_bytes() == b"chart"
    assert len(created) == 1
    assert created[0].path == final
    assert created[0].directory_fd is None


def test_windows_asset_publication_preserves_existing_directory(
    tmp_path, monkeypatch
):
    staged = tmp_path / "staged"
    staged.mkdir()
    (staged / "chart.png").write_bytes(b"replacement")
    final = tmp_path / "report_charts"
    final.mkdir()
    (final / "chart.png").write_bytes(b"protected")
    monkeypatch.setattr(dt, "_uses_windows_path_publication", lambda: True)

    with pytest.raises(FileExistsError):
        dt._publish_staged_asset_directory(staged, final, [])

    assert (final / "chart.png").read_bytes() == b"protected"


def test_windows_directory_verification_rejects_identity_change(
    tmp_path, monkeypatch
):
    public = tmp_path / "assets"
    public.mkdir()
    artifact = dt._CreatedArtifact(public, os.lstat(public))
    public.rename(tmp_path / "owned")
    public.mkdir()
    monkeypatch.setattr(dt, "_uses_windows_path_publication", lambda: True)

    with pytest.raises(RuntimeError, match="namespace changed"):
        dt._verify_created_directories([artifact])


def test_windows_rollback_removes_only_matching_owned_artifact(
    tmp_path, monkeypatch
):
    public = tmp_path / "report.md"
    public.write_bytes(b"owned")
    artifact = dt._CreatedArtifact(public, os.lstat(public))
    monkeypatch.setattr(dt, "_uses_windows_path_publication", lambda: True)
    monkeypatch.setattr(
        dt,
        "_quarantine_created_artifact",
        lambda _artifact: (_ for _ in ()).throw(
            AssertionError("Windows rollback must not use POSIX quarantine")
        ),
    )

    dt._remove_created_artifacts([artifact])

    assert not public.exists()
    assert not list(tmp_path.glob(".report.md.quarantine-*"))


def test_windows_rollback_restores_replaced_artifact(
    tmp_path, monkeypatch
):
    public = tmp_path / "report.md"
    public.write_bytes(b"owned")
    artifact = dt._CreatedArtifact(public, os.lstat(public))
    public.rename(tmp_path / "owned.md")
    public.write_bytes(b"replacement")
    monkeypatch.setattr(dt, "_uses_windows_path_publication", lambda: True)

    dt._remove_created_artifacts([artifact])

    assert public.read_bytes() == b"replacement"
    assert not list(tmp_path.glob(".report.md.quarantine-*"))


def test_write_report_markdown_uses_windows_publication_backend(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(dt, "_uses_windows_path_publication", lambda: True)

    report_path = _write_report_in(
        tmp_path, final_result=_report_result_with_assets()
    )

    assert Path(report_path).read_text(encoding="utf-8")
    assert (tmp_path / "研究报告-v1_infer" / "inference_7.html").exists()
    assert (tmp_path / "研究报告-v1_charts" / "chart-1.png").exists()


@pytest.mark.parametrize("target_kind", ["file", "symlink"])
def test_write_report_markdown_never_overwrites_preexisting_target(
    tmp_path, target_kind
):
    target = tmp_path / "研究报告-v1.md"
    protected = tmp_path / "protected.md"
    protected.write_text("protected", encoding="utf-8")
    if target_kind == "file":
        target.write_text("existing", encoding="utf-8")
    else:
        target.symlink_to(protected)

    report_path = _write_report_in(tmp_path)

    assert report_path == str(tmp_path / "研究报告-2-v1.md")
    assert protected.read_text(encoding="utf-8") == "protected"
    if target_kind == "file":
        assert target.read_text(encoding="utf-8") == "existing"
    else:
        assert target.is_symlink()


@pytest.mark.parametrize(
    ("asset_suffix", "asset_name", "expected_bytes"),
    [
        ("_infer", "inference_7.html", b"<html>trace</html>"),
        ("_charts", "chart-1.png", b"png-bytes"),
    ],
)
def test_write_report_markdown_reallocates_without_overwriting_preexisting_asset(
    tmp_path, asset_suffix, asset_name, expected_bytes
):
    protected_dir = tmp_path / f"研究报告-v1{asset_suffix}"
    protected_dir.mkdir()
    protected_file = protected_dir / asset_name
    protected_file.write_bytes(b"protected")

    report_path = _write_report_in(
        tmp_path, final_result=_report_result_with_assets()
    )

    assert report_path == str(tmp_path / "研究报告-2-v1.md")
    assert protected_file.read_bytes() == b"protected"
    assert (
        tmp_path / f"研究报告-2-v1{asset_suffix}" / asset_name
    ).read_bytes() == expected_bytes
    assert not (tmp_path / "研究报告-v1.final-result.json").exists()
    assert not (tmp_path / "研究报告-v1.provenance.json").exists()
    assert not (tmp_path / "研究报告-v1.md").exists()


@pytest.mark.parametrize(
    ("asset_suffix", "asset_name"),
    [
        ("_infer", "inference_7.html"),
        ("_charts", "chart-1.png"),
    ],
)
def test_write_report_markdown_does_not_follow_symlinked_asset_directory(
    tmp_path, asset_suffix, asset_name
):
    external_dir = tmp_path / "external"
    external_dir.mkdir()
    external_file = external_dir / asset_name
    external_file.write_bytes(b"external")
    (tmp_path / f"研究报告-v1{asset_suffix}").symlink_to(
        external_dir, target_is_directory=True
    )

    report_path = _write_report_in(
        tmp_path, final_result=_report_result_with_assets()
    )

    assert report_path == str(tmp_path / "研究报告-2-v1.md")
    assert external_file.read_bytes() == b"external"
    assert (tmp_path / f"研究报告-v1{asset_suffix}").is_symlink()
    assert not (tmp_path / "研究报告-v1.final-result.json").exists()
    assert not (tmp_path / "研究报告-v1.provenance.json").exists()
    assert not (tmp_path / "研究报告-v1.md").exists()


def test_write_report_markdown_rejects_asset_directory_namespace_swap(
    tmp_path, monkeypatch
):
    asset_dir = tmp_path / "研究报告-v1_infer"
    displaced_dir = tmp_path / "displaced-owned-infer"
    external_dir = tmp_path / "external"
    external_dir.mkdir()
    external_file = external_dir / "inference_7.html"
    external_file.write_bytes(b"external")
    directory_opened = threading.Event()
    namespace_swapped = threading.Event()
    real_open = dt.os.open

    def synchronizing_open(path, flags, *args, **kwargs):
        descriptor = real_open(path, flags, *args, **kwargs)
        if (
            os.path.basename(os.fspath(path)) == asset_dir.name
            and flags & os.O_DIRECTORY
            and not directory_opened.is_set()
        ):
            directory_opened.set()
            assert namespace_swapped.wait(timeout=2)
        return descriptor

    monkeypatch.setattr(dt.os, "open", synchronizing_open)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            _write_report_in,
            tmp_path,
            "研究报告.md",
            _report_result_with_assets(),
        )
        assert directory_opened.wait(timeout=2)
        os.rename(asset_dir, displaced_dir)
        asset_dir.symlink_to(external_dir, target_is_directory=True)
        namespace_swapped.set()
        with pytest.raises(RuntimeError, match="namespace"):
            future.result(timeout=2)

    assert external_file.read_bytes() == b"external"
    assert asset_dir.is_symlink()
    assert not (tmp_path / "研究报告-v1.md").exists()


def test_write_report_markdown_cleans_asset_directory_when_open_fails(
    tmp_path, monkeypatch
):
    asset_dir = tmp_path / "研究报告-v1_infer"
    real_open = dt.os.open

    def failing_open(path, flags, *args, **kwargs):
        if (
            os.path.basename(os.fspath(path)) == asset_dir.name
            and flags & os.O_DIRECTORY
        ):
            raise OSError("asset directory open failed")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(dt.os, "open", failing_open)
    with pytest.raises(OSError, match="asset directory open failed"):
        _write_report_in(
            tmp_path, final_result=_report_result_with_assets()
        )

    assert not asset_dir.exists()
    assert not (tmp_path / "研究报告-v1.md").exists()


def test_write_report_markdown_quarantines_owned_directory_after_fstat_failure(
    tmp_path, monkeypatch
):
    asset_dir = tmp_path / "研究报告-v1_infer"
    asset_descriptor = None
    entry_quarantined = threading.Event()
    replacement_created = threading.Event()
    real_open = dt.os.open
    real_fstat = dt.os.fstat
    real_rename = dt.os.rename

    def recording_open(path, flags, *args, **kwargs):
        nonlocal asset_descriptor
        descriptor = real_open(path, flags, *args, **kwargs)
        if (
            os.path.basename(os.fspath(path)) == asset_dir.name
            and flags & os.O_DIRECTORY
        ):
            asset_descriptor = descriptor
        return descriptor

    def failing_fstat(descriptor):
        nonlocal asset_descriptor
        if descriptor == asset_descriptor:
            asset_descriptor = None
            raise OSError("asset directory fstat failed")
        return real_fstat(descriptor)

    def synchronizing_rename(source, destination, *args, **kwargs):
        result = real_rename(source, destination, *args, **kwargs)
        if os.path.basename(os.fspath(source)) == asset_dir.name:
            entry_quarantined.set()
            assert replacement_created.wait(timeout=2)
        return result

    def replace_public_entry():
        if not entry_quarantined.wait(timeout=2):
            return
        asset_dir.mkdir()
        (asset_dir / "writer.bin").write_bytes(b"replacement")
        replacement_created.set()

    monkeypatch.setattr(dt.os, "open", recording_open)
    monkeypatch.setattr(dt.os, "fstat", failing_fstat)
    monkeypatch.setattr(dt.os, "rename", synchronizing_rename)
    replacement = threading.Thread(target=replace_public_entry)
    replacement.start()
    with pytest.raises(OSError, match="asset directory fstat failed"):
        _write_report_in(
            tmp_path, final_result=_report_result_with_assets()
        )
    replacement.join(timeout=2)

    assert entry_quarantined.is_set()
    assert not replacement.is_alive()
    assert (asset_dir / "writer.bin").read_bytes() == b"replacement"
    assert not (tmp_path / "研究报告-v1.md").exists()


def test_write_report_markdown_publishes_snapshot_then_provenance_then_markdown(
    tmp_path, monkeypatch
):
    publication_order = []
    atomic_create = dt._atomic_create_bytes

    def recording_create(path, payload):
        publication_order.append(path.name)
        return atomic_create(path, payload)

    monkeypatch.setattr(dt, "_atomic_create_bytes", recording_create)

    report_path = _write_report_in(tmp_path)

    assert report_path == str(tmp_path / "研究报告-v1.md")
    assert publication_order == [
        "研究报告-v1.final-result.json",
        "研究报告-v1.provenance.json",
        "研究报告-v1.md",
    ]


def test_write_report_markdown_reallocates_after_publication_collision(
    tmp_path, monkeypatch
):
    atomic_create = dt._atomic_create_bytes
    collision_path = tmp_path / "研究报告-v1.provenance.json"
    collision_injected = False

    def racing_create(path, payload):
        nonlocal collision_injected
        if path == collision_path and not collision_injected:
            collision_injected = True
            path.write_text("external", encoding="utf-8")
        return atomic_create(path, payload)

    monkeypatch.setattr(dt, "_atomic_create_bytes", racing_create)

    report_path = _write_report_in(tmp_path)

    assert report_path == str(tmp_path / "研究报告-2-v1.md")
    assert collision_path.read_text(encoding="utf-8") == "external"
    assert not (tmp_path / "研究报告-v1.final-result.json").exists()
    assert not (tmp_path / "研究报告-v1.md").exists()


def test_write_report_markdown_cleans_current_partial_files_on_write_failure(
    tmp_path, monkeypatch
):
    preserved = tmp_path / "preserved.txt"
    preserved.write_text("keep", encoding="utf-8")
    atomic_create = dt._atomic_create_bytes

    def failing_create(path, payload):
        if path.name.endswith(".provenance.json"):
            raise OSError("disk unavailable")
        return atomic_create(path, payload)

    monkeypatch.setattr(dt, "_atomic_create_bytes", failing_create)
    with pytest.raises(OSError, match="disk unavailable"):
        _write_report_in(
            tmp_path, final_result=_report_result_with_assets()
        )

    assert preserved.read_text(encoding="utf-8") == "keep"
    assert not (tmp_path / "研究报告-v1.final-result.json").exists()
    assert not (tmp_path / "研究报告-v1.provenance.json").exists()
    assert not (tmp_path / "研究报告-v1.md").exists()
    assert not (tmp_path / "研究报告-v1_infer").exists()
    assert not (tmp_path / "研究报告-v1_charts").exists()


def test_write_report_markdown_cleans_staging_when_snapshot_serialization_fails(
    tmp_path,
):
    with patch(
        "jiuwenclaw.agentserver.tools.deepresearch_plugin.report_bundle.serialize_final_result_snapshot",
        side_effect=RuntimeError("snapshot serialization failed"),
    ):
        with pytest.raises(RuntimeError, match="snapshot serialization failed"):
            _write_report_in(
                tmp_path, final_result=_report_result_with_assets()
            )

    assert list(tmp_path.iterdir()) == []


def test_remove_created_artifacts_preserves_replacement_created_during_cleanup(
    tmp_path, monkeypatch
):
    owned_path = tmp_path / "owned.bin"
    owned_path.write_bytes(b"owned")
    owned_metadata = os.lstat(owned_path)
    entry_quarantined = threading.Event()
    replacement_created = threading.Event()
    real_rename = dt.os.rename

    def synchronizing_rename(source, destination, *args, **kwargs):
        result = real_rename(source, destination, *args, **kwargs)
        if os.path.basename(os.fspath(source)) == owned_path.name:
            entry_quarantined.set()
            assert replacement_created.wait(timeout=2)
        return result

    def replace_public_entry():
        assert entry_quarantined.wait(timeout=2)
        owned_path.write_bytes(b"replacement")
        replacement_created.set()

    monkeypatch.setattr(dt.os, "rename", synchronizing_rename)
    replacement = threading.Thread(target=replace_public_entry)
    replacement.start()
    dt._remove_created_artifacts([(owned_path, owned_metadata)])
    replacement.join(timeout=2)

    assert not replacement.is_alive()
    assert owned_path.read_bytes() == b"replacement"


def test_remove_created_artifacts_restores_replacement_directory(tmp_path):
    public_dir = tmp_path / "assets"
    displaced_owned_dir = tmp_path / "displaced-owned-assets"
    public_dir.mkdir()
    owned_metadata = os.lstat(public_dir)
    os.rename(public_dir, displaced_owned_dir)
    public_dir.mkdir()
    replacement_file = public_dir / "writer.bin"
    replacement_file.write_bytes(b"replacement")

    dt._remove_created_artifacts([(public_dir, owned_metadata)])

    assert replacement_file.read_bytes() == b"replacement"
    assert displaced_owned_dir.is_dir()


def _styled_bundle(tmp_path):
    bundle_root = tmp_path / "styled-bundle"
    (bundle_root / "infer").mkdir(parents=True)
    (bundle_root / "charts").mkdir()
    (bundle_root / "infer" / "inference_7.html").write_bytes(b"styled-infer")
    (bundle_root / "charts" / "chart-1.png").write_bytes(b"styled-chart")
    (bundle_root / "report.html").write_text(
        '<link href="infer/inference_7.html">'
        '<img src="charts/chart-1.png">',
        encoding="utf-8",
    )
    return bundle_root


def test_install_styled_bundle_uses_windows_publication_backend(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(dt, "_uses_windows_path_publication", lambda: True)
    html_path = tmp_path / "研究报告-v1.html"

    dt._install_styled_bundle(_styled_bundle(tmp_path), html_path)

    assert html_path.exists()
    assert (
        tmp_path / "研究报告-v1_html_infer" / "inference_7.html"
    ).exists()
    assert (
        tmp_path / "研究报告-v1_html_charts" / "chart-1.png"
    ).exists()


def test_install_styled_bundle_uses_dedicated_assets_without_mutating_provenance(
    tmp_path,
):
    html_path = tmp_path / "研究报告-v1.html"
    original_infer = tmp_path / "研究报告-v1_infer"
    original_charts = tmp_path / "研究报告-v1_charts"
    original_infer.mkdir()
    original_charts.mkdir()
    infer_file = original_infer / "inference_7.html"
    chart_file = original_charts / "chart-1.png"
    infer_file.write_bytes(b"provenance-infer")
    chart_file.write_bytes(b"provenance-chart")
    before_hashes = {
        infer_file: hashlib.sha256(infer_file.read_bytes()).hexdigest(),
        chart_file: hashlib.sha256(chart_file.read_bytes()).hexdigest(),
    }
    provenance_path = tmp_path / "研究报告-v1.provenance.json"
    provenance_path.write_text(json.dumps({
        "inference_manifest": [{"sha256": before_hashes[infer_file]}],
        "chart_manifest": [{"sha256": before_hashes[chart_file]}],
    }), encoding="utf-8")
    provenance_bytes = provenance_path.read_bytes()

    dt._install_styled_bundle(_styled_bundle(tmp_path), html_path)

    assert {
        path: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in before_hashes
    } == before_hashes
    assert provenance_path.read_bytes() == provenance_bytes
    manifests = json.loads(provenance_path.read_text(encoding="utf-8"))
    assert manifests["inference_manifest"][0]["sha256"] == before_hashes[infer_file]
    assert manifests["chart_manifest"][0]["sha256"] == before_hashes[chart_file]
    assert (
        tmp_path / "研究报告-v1_html_infer" / "inference_7.html"
    ).read_bytes() == b"styled-infer"
    assert (
        tmp_path / "研究报告-v1_html_charts" / "chart-1.png"
    ).read_bytes() == b"styled-chart"
    html = html_path.read_text(encoding="utf-8")
    assert 'href="研究报告-v1_html_infer/' in html
    assert 'src="研究报告-v1_html_charts/' in html


def test_install_styled_bundle_rolls_back_owned_assets_after_collision(tmp_path):
    html_path = tmp_path / "研究报告-v1.html"
    protected_chart_dir = tmp_path / "研究报告-v1_html_charts"
    protected_chart_dir.mkdir()
    protected_file = protected_chart_dir / "chart-1.png"
    protected_file.write_bytes(b"protected")

    with pytest.raises(FileExistsError):
        dt._install_styled_bundle(_styled_bundle(tmp_path), html_path)

    assert protected_file.read_bytes() == b"protected"
    assert not (tmp_path / "研究报告-v1_html_infer").exists()
    assert not html_path.exists()


def test_install_styled_bundle_does_not_follow_symlinked_asset_target(tmp_path):
    html_path = tmp_path / "研究报告-v1.html"
    external_dir = tmp_path / "external"
    external_dir.mkdir()
    external_file = external_dir / "inference_7.html"
    external_file.write_bytes(b"external")
    (tmp_path / "研究报告-v1_html_infer").symlink_to(
        external_dir, target_is_directory=True
    )

    with pytest.raises(FileExistsError):
        dt._install_styled_bundle(_styled_bundle(tmp_path), html_path)

    assert external_file.read_bytes() == b"external"
    assert not html_path.exists()


def test_install_styled_bundle_rejects_asset_directory_namespace_swap(
    tmp_path, monkeypatch
):
    html_path = tmp_path / "研究报告-v1.html"
    asset_dir = tmp_path / "研究报告-v1_html_infer"
    displaced_dir = tmp_path / "displaced-styled-infer"
    external_dir = tmp_path / "external"
    external_dir.mkdir()
    external_file = external_dir / "inference_7.html"
    external_file.write_bytes(b"external")
    directory_opened = threading.Event()
    namespace_swapped = threading.Event()
    real_open = dt.os.open

    def synchronizing_open(path, flags, *args, **kwargs):
        descriptor = real_open(path, flags, *args, **kwargs)
        if (
            os.path.basename(os.fspath(path)) == asset_dir.name
            and flags & os.O_DIRECTORY
            and not directory_opened.is_set()
        ):
            directory_opened.set()
            assert namespace_swapped.wait(timeout=2)
        return descriptor

    monkeypatch.setattr(dt.os, "open", synchronizing_open)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            dt._install_styled_bundle, _styled_bundle(tmp_path), html_path
        )
        assert directory_opened.wait(timeout=2)
        os.rename(asset_dir, displaced_dir)
        asset_dir.symlink_to(external_dir, target_is_directory=True)
        namespace_swapped.set()
        with pytest.raises(RuntimeError, match="namespace"):
            future.result(timeout=2)

    assert external_file.read_bytes() == b"external"
    assert asset_dir.is_symlink()
    assert not html_path.exists()


def test_build_related_artifact_bundle_exposes_only_hidden_preview_companions():
    marker = {
        "raw_report_path": "/skill/data/C1.raw_report.md",
        "citations_path": "/skill/data/C1.citations.json",
        "citations_preview_path": "/skill/data/C1.citations.preview.json",
    }

    assert dt._build_related_artifact_bundle(marker, 0) == {
        "schemaVersion": "1.0",
        "relatedArtifacts": [
            {
                "type": "raw_report",
                "path": "/skill/data/C1.raw_report.md",
                "contentType": "text/markdown",
                "relatedToPathIndex": 0,
            },
            {
                "type": "citations_preview",
                "path": "/skill/data/C1.citations.preview.json",
                "contentType": "application/json",
                "schemaVersion": "1.1",
                "relatedToPathIndex": 0,
            },
        ],
    }


def test_build_related_artifact_bundle_ignores_blank_companion_paths():
    assert dt._build_related_artifact_bundle(
        {"raw_report_path": " ", "citations_preview_path": None},
        0,
    ) is None


@pytest.mark.asyncio
async def test_completed_report_is_delivered_as_markdown_file_without_entering_tool_outcome():
    report_content = "# 最终报告\n\n完整正文"
    final_result = {"response_content": report_content, "infer_messages": [], "chart_messages": []}
    lines = [
        json.dumps({"__deepsearch_status__": "started", "conversation_id": "C1"}),
        json.dumps({
            "agent": "sub_reporter",
            "section_idx": "1",
            "section_total": 1,
            "event": "done",
        }),
        json.dumps({
            "__deepsearch_status__": "completed",
            "conversation_id": "C1",
            "final_result": final_result,
            "raw_report_path": "/skill/data/C1.raw_report.md",
            "citations_path": "/skill/data/C1.citations.json",
            "citations_preview_path": "/skill/data/C1.citations.preview.json",
        }),
    ]
    push = AsyncMock()
    with patch.object(dt, "_resolve_jiuwenclaw_python", return_value="/p"), \
         patch.object(dt, "_resolve_run_script", return_value="/s"), \
         patch.object(dt, "_get_route", return_value={
             "request_id": "R1", "channel_id": "CH1", "session_id": "S1"
         }), \
         patch.object(
             dt,
             "_write_report_artifacts_stream",
             return_value={"md": "/tmp/r.md", "html": "/tmp/r.html"},
         ) as write_report, \
         patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=_Proc(lines))), \
         patch(
             "jiuwenclaw.agentserver.gateway_push.transport.WebSocketGatewayPushTransport",
             return_value=push,
         ):
        result = await dt.deepresearch_stream._func(action="start", query="X", file_name="r")

    payloads = [call.args[0]["payload"] for call in push.send_push.await_args_list]
    report_frames = [payload for payload in payloads if payload.get("event_type") == "chat.delta"]
    assert report_frames
    assert all(frame.get("content") != report_content for frame in report_frames)
    assert all(
        frame.get("task_id", "").startswith("deepresearch_stage_")
        for frame in report_frames
    )
    write_report.assert_called_once_with(
        final_result,
        "r",
        "C1",
        {
            "raw_report_path": "/skill/data/C1.raw_report.md",
            "citations_preview_path": "/skill/data/C1.citations.preview.json",
        },
    )
    file_payload = next(payload for payload in payloads if payload.get("event_type") == "chat.file")
    assert file_payload == {
        "event_type": "chat.file",
        "files": [
            {"path": "/tmp/r.md", "name": "r.md"},
            {"path": "/tmp/r.html", "name": "r.html"},
        ],
        "metadata": {
            "artifactBundle": {
                "schemaVersion": "1.0",
                "relatedArtifacts": [
                    {
                        "type": "raw_report",
                        "path": "/skill/data/C1.raw_report.md",
                        "contentType": "text/markdown",
                        "relatedToPathIndex": 0,
                    },
                    {
                        "type": "citations_preview",
                        "path": "/skill/data/C1.citations.preview.json",
                        "contentType": "application/json",
                        "schemaVersion": "1.1",
                        "relatedToPathIndex": 0,
                    },
                ],
            },
        },
    }
    assert "C1.citations.json" not in json.dumps(file_payload)
    assert [_active_stage(update) for update in _task_updates(payloads)] == [
        1, 2, 3, 4, None,
    ]
    assert json.loads(result) == {
        "status": "completed",
        "conversation_id": "C1",
        "report_delivered": True,
        "report_chars": len(report_content),
    }


@pytest.mark.asyncio
async def test_completed_report_does_not_fall_back_to_chat_when_file_delivery_fails(
    tmp_path,
):
    lines = [
        json.dumps({"__deepsearch_status__": "started", "conversation_id": "C1"}),
        json.dumps({
            "agent": "sub_reporter",
            "section_idx": "1",
            "section_total": 1,
            "event": "done",
        }),
        json.dumps({
            "__deepsearch_status__": "completed",
            "conversation_id": "C1",
            "final_result": {"response_content": "完整报告"},
        }),
    ]
    report_path = tmp_path / "r.md"
    report_path.write_text("完整报告", encoding="utf-8")
    push = AsyncMock()

    async def _fail_file(message):
        if message["payload"].get("event_type") == "chat.file":
            raise RuntimeError("push failed")

    push.send_push.side_effect = _fail_file
    with patch.object(dt, "_resolve_jiuwenclaw_python", return_value="/p"), \
         patch.object(dt, "_resolve_run_script", return_value="/s"), \
         patch.object(dt, "_get_route", return_value={
             "request_id": "R1", "channel_id": "CH1", "session_id": "S1"
         }), \
         patch.object(dt, "_write_report_markdown", return_value=str(report_path)), \
         patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=_Proc(lines))), \
         patch(
             "jiuwenclaw.agentserver.gateway_push.transport.WebSocketGatewayPushTransport",
             return_value=push,
         ):
        result = await dt.deepresearch_stream._func(action="start", query="X", file_name="r")

    outcome = json.loads(result)
    assert outcome["status"] == "error"
    assert outcome["error_code"] == "report_file_delivery_failed"
    assert "report_content" not in outcome
    payloads = [call.args[0]["payload"] for call in push.send_push.await_args_list]
    assert [_active_stage(update) for update in _task_updates(payloads)] == [
        1, 2, 3, 4,
    ]


@pytest.mark.asyncio
async def test_tool_keeps_current_workflow_stage_in_progress_when_research_fails():
    lines = [
        json.dumps({"__deepsearch_status__": "started", "conversation_id": "C1"}),
        json.dumps({
            "agent": "collector_info_retrieval",
            "section_idx": "1",
            "section_title": "真实章节标题",
            "section_total": 1,
        }),
        json.dumps({
            "__deepsearch_status__": "error",
            "conversation_id": "C1",
            "error": "search failed",
        }),
    ]
    push = AsyncMock()
    with patch.object(dt, "_resolve_jiuwenclaw_python", return_value="/p"), \
         patch.object(dt, "_resolve_run_script", return_value="/s"), \
         patch.object(dt, "_get_route", return_value={
             "request_id": "R1", "channel_id": "CH1", "session_id": "S1"
         }), \
         patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=_Proc(lines))), \
         patch(
             "jiuwenclaw.agentserver.gateway_push.transport.WebSocketGatewayPushTransport",
             return_value=push,
         ):
        result = await dt.deepresearch_stream._func(
            action="start",
            query="X",
            file_name="r",
        )

    outcome = json.loads(result)
    assert outcome["status"] == "error"
    assert outcome["error_code"] == "workflow_error"
    assert outcome["error"] == "search failed"
    payloads = [call.args[0]["payload"] for call in push.send_push.await_args_list]
    assert [_active_stage(update) for update in _task_updates(payloads)] == [
        1, 2, 3,
    ]
    assert all(payload.get("event_type") != "chat.error" for payload in payloads)


@pytest.mark.asyncio
async def test_outline_interaction_is_not_returned_to_the_model():
    # A repeated fake outline interruption exercises the automatic-resume loop guard.
    lines = [
        json.dumps({"__deepsearch_status__": "started", "conversation_id": "C1"}),
        json.dumps({"agent": "outline", "content": "累积的旧大纲"}),
        json.dumps({"agent": "outline_interaction", "message_type": "interrupt",
                    "content": "请审批大纲", "conversation_id": "C1"}),
        json.dumps({"__deepsearch_status__": "interrupted", "agent": "outline_interaction",
                    "conversation_id": "C1", "content": "第一章 来自marker\n第二章 来自marker",
                    "prompt": "请审阅大纲"}),
    ]
    push = AsyncMock()
    with patch.object(dt, "_resolve_jiuwenclaw_python", return_value="/p"), \
         patch.object(dt, "_resolve_run_script", return_value="/s"), \
         patch.object(dt, "_get_route", return_value={
             "request_id": "R1", "channel_id": "CH1", "session_id": "S1"
         }), \
         patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=_Proc(lines))), \
         patch(
             "jiuwenclaw.agentserver.gateway_push.transport.WebSocketGatewayPushTransport",
             return_value=push,
         ):
        result = await dt.deepresearch_stream._func(action="start", query="X", file_name="r")
    out = json.loads(result)
    assert out["status"] == "error"
    assert out["conversation_id"] == "C1"
    assert out["error_code"] == "outline_auto_resume_loop"


@pytest.mark.asyncio
async def test_feedback_interrupt_injects_cached_questions_when_marker_has_none():
    lines = [
        json.dumps({"__deepsearch_status__": "started", "conversation_id": "C1"}),
        json.dumps({
            "agent": "question_generator",
            "message_type": "message_chunk",
            "message_id": "Q1",
            "content": "1. 市场？\n2. 时间？",
        }, ensure_ascii=False),
        json.dumps({
            "__deepsearch_status__": "interrupted",
            "agent": "feedback_handler",
            "conversation_id": "C1",
            "content": "Enter your feedback:",
        }),
    ]
    patches = _patch_env(lines)
    for active_patch in patches:
        active_patch.start()
    try:
        result = await dt.deepresearch_stream._func(
            action="start", query="X", file_name="r",
        )
    finally:
        for active_patch in patches:
            active_patch.stop()

    assert json.loads(result)["marker"]["questions"] == "1. 市场？\n2. 时间？"


@pytest.mark.asyncio
async def test_feedback_interrupt_preserves_native_questions():
    lines = [
        json.dumps({"__deepsearch_status__": "started", "conversation_id": "C1"}),
        json.dumps({
            "agent": "question_generator",
            "message_type": "message_chunk",
            "message_id": "Q1",
            "content": "缓存问题",
        }, ensure_ascii=False),
        json.dumps({
            "__deepsearch_status__": "interrupted",
            "agent": "feedback_handler",
            "conversation_id": "C1",
            "questions": ["原生问题"],
        }, ensure_ascii=False),
    ]
    patches = _patch_env(lines)
    for active_patch in patches:
        active_patch.start()
    try:
        result = await dt.deepresearch_stream._func(
            action="start", query="X", file_name="r",
        )
    finally:
        for active_patch in patches:
            active_patch.stop()

    assert json.loads(result)["marker"]["questions"] == ["原生问题"]


@pytest.mark.asyncio
async def test_repeated_outline_interaction_after_auto_accept_returns_error():
    lines = [
        json.dumps({"__deepsearch_status__": "started", "conversation_id": "C1"}),
        json.dumps({"agent": "outline", "content": "第一章 累积大纲"}),
        json.dumps({"agent": "outline_interaction", "message_type": "interrupt",
                    "content": "请审批大纲", "conversation_id": "C1"}),
        json.dumps({"__deepsearch_status__": "interrupted", "agent": "outline_interaction",
                    "conversation_id": "C1"}),
    ]
    push = AsyncMock()
    patches = _patch_env(lines, push)
    for p in patches:
        p.start()
    try:
        result = await dt.deepresearch_stream._func(action="start", query="X", file_name="r")
    finally:
        for p in patches:
            p.stop()

    out = json.loads(result)
    assert out["status"] == "error"
    assert out["error_code"] == "outline_auto_resume_loop"
    payloads = [call.args[0]["payload"] for call in push.send_push.await_args_list]
    assert all(payload.get("event_type") != "chat.error" for payload in payloads)


@pytest.mark.asyncio
async def test_outline_status_placeholder_does_not_escape_auto_resume_loop():
    lines = [
        json.dumps({"__deepsearch_status__": "started", "conversation_id": "C1"}),
        json.dumps({"agent": "outline", "content": "# 第一章\n累积大纲正文"}),
        json.dumps({"agent": "outline_interaction", "message_type": "interrupt",
                    "content": "Round 1: waiting for user feedback.", "conversation_id": "C1"}),
        json.dumps({"__deepsearch_status__": "interrupted", "agent": "outline_interaction",
                    "conversation_id": "C1", "content": "Round 1: waiting for user feedback."}),
    ]
    push = AsyncMock()
    patches = _patch_env(lines, push)
    for p in patches:
        p.start()
    try:
        result = await dt.deepresearch_stream._func(action="start", query="X", file_name="r")
    finally:
        for p in patches:
            p.stop()

    out = json.loads(result)
    assert out["status"] == "error"
    assert out["error_code"] == "outline_auto_resume_loop"
    payloads = [call.args[0]["payload"] for call in push.send_push.await_args_list]
    assert all(payload.get("event_type") != "chat.error" for payload in payloads)


@pytest.mark.asyncio
async def test_outline_interaction_is_resumed_inside_the_tool_without_returning_control_to_model(
    tmp_path,
):
    outline = json.dumps({
        "title": "AI Agent 入门",
        "sections": [{"id": "1", "title": "核心架构"}],
    }, ensure_ascii=False)
    start_lines = [
        json.dumps({"__deepsearch_status__": "started", "conversation_id": "C1"}),
        json.dumps({"agent": "outline", "content": outline}),
        json.dumps({
            "__deepsearch_status__": "interrupted",
            "agent": "outline_interaction",
            "conversation_id": "C1",
            "outline": outline,
        }),
    ]
    resume_lines = [
        json.dumps({"__deepsearch_status__": "resuming", "conversation_id": "C1"}),
        json.dumps({
            "agent": "sub_reporter",
            "section_idx": "1",
            "section_total": 1,
            "event": "done",
        }),
        json.dumps({
            "__deepsearch_status__": "completed",
            "conversation_id": "C1",
            "final_result": {"response_content": "done"},
        }),
    ]
    route = {"request_id": "R1", "channel_id": "CH1", "session_id": "S1"}
    push = AsyncMock()
    spawn = AsyncMock(side_effect=[_Proc(start_lines), _Proc(resume_lines)])
    report_path = tmp_path / "r.md"
    report_path.write_text("done", encoding="utf-8")

    with patch.object(dt, "_resolve_jiuwenclaw_python", return_value="/p"), \
         patch.object(dt, "_resolve_run_script", return_value="/s"), \
         patch.object(dt, "_get_route", side_effect=[dict(route), dict(route)]), \
         patch.object(
             dt,
             "_write_report_artifacts_stream",
             new=AsyncMock(return_value={"md": str(report_path)}),
         ), \
         patch("asyncio.create_subprocess_exec", new=spawn), \
         patch(
             "jiuwenclaw.agentserver.gateway_push.transport.WebSocketGatewayPushTransport",
             return_value=push,
         ):
        result = await dt.deepresearch_stream._func(
            action="start", query="X", file_name="r",
        )

    assert json.loads(result)["status"] == "completed"
    assert spawn.await_count == 2
    resume_argv = spawn.await_args_list[1].args
    assert resume_argv[1:3] == ("/s", "resume")
    assert "--config-stdin" in resume_argv
    assert "--conversation-id" in resume_argv
    assert resume_argv[resume_argv.index("--conversation-id") + 1] == "C1"
    assert '{"interrupt_feedback":"accepted","feedback":""}' in resume_argv
    payloads = [call.args[0]["payload"] for call in push.send_push.await_args_list]
    stage_2_updates = [
        payload for payload in _task_updates(payloads)
        if _active_stage(payload) == 2
    ]
    stage_2_messages = [
        payload for payload in payloads
        if payload.get("task_id") == "deepresearch_stage_2"
        and payload.get("event_type") in {"chat.reasoning", "chat.delta"}
        and payload.get("content", "").startswith("[DeepResearch 阶段切换]")
    ]
    assert len(stage_2_updates) == 1
    assert [payload["event_type"] for payload in stage_2_messages] == [
        "chat.reasoning",
        "chat.delta",
    ]


@pytest.mark.asyncio
async def test_outline_titles_are_reused_by_section_stream_after_resume(tmp_path):
    outline = json.dumps({
        "title": "主流 RAG 框架深度对比",
        "sections": [
            {"title": "RAG技术演进与主流框架全景概览"},
            {"title": "核心架构设计与检索增强能力深度对比"},
        ],
    }, ensure_ascii=False)
    start_lines = [
        json.dumps({"__deepsearch_status__": "started", "conversation_id": "C1"}),
        json.dumps({"agent": "outline", "content": outline}),
        json.dumps({"agent": "outline_interaction", "message_type": "interrupt",
                    "content": "请审批大纲", "conversation_id": "C1"}),
        json.dumps({"__deepsearch_status__": "interrupted", "agent": "outline_interaction",
                    "conversation_id": "C1", "outline": outline}),
    ]
    resume_lines = [
        json.dumps({"__deepsearch_status__": "resuming", "conversation_id": "C1"}),
        json.dumps({
            "agent": "plan_reasoning",
            "section_idx": "1",
            "event": "message",
            "content": json.dumps({
                "title": "RAG技术演进阶段与里程碑及十大框架全景画像信息采集",
                "thought": "完整规划过程",
            }, ensure_ascii=False),
        }),
        json.dumps({"__deepsearch_status__": "completed", "conversation_id": "C1",
                    "final_result": {"response_content": "done"}}),
    ]
    route = {"request_id": "R1", "channel_id": "CH1", "session_id": "S1"}
    push = AsyncMock()
    spawn = AsyncMock(side_effect=[_Proc(start_lines), _Proc(resume_lines)])
    report_path = tmp_path / "r.md"
    report_path.write_text("done", encoding="utf-8")
    with patch.object(dt, "_resolve_jiuwenclaw_python", return_value="/p"), \
         patch.object(dt, "_resolve_run_script", return_value="/s"), \
         patch.object(dt, "_get_route", side_effect=[dict(route), dict(route)]), \
         patch.object(
             dt,
             "_write_report_artifacts_stream",
             new=AsyncMock(return_value={"md": str(report_path)}),
         ), \
         patch("asyncio.create_subprocess_exec", new=spawn), \
         patch(
             "jiuwenclaw.agentserver.gateway_push.transport.WebSocketGatewayPushTransport",
             return_value=push,
         ):
        completed = await dt.deepresearch_stream._func(
            action="start", query="X", file_name="r",
        )

    outcome = json.loads(completed)
    assert outcome["status"] == "error"
    assert outcome["error_code"] == "incomplete_section_progress"
    section_payloads = [
        call.args[0]["payload"]
        for call in push.send_push.await_args_list
        if call.args[0]["payload"].get("task_id") == "deepresearch_stage_3"
        and call.args[0]["payload"].get("stream_source_id") == "deepresearch_section_1"
    ]
    assert section_payloads
    assert all(
        payload["task_content"] == "RAG技术演进与主流框架全景概览"
        for payload in section_payloads
    )
    payloads = [call.args[0]["payload"] for call in push.send_push.await_args_list]
    assert [_active_stage(update) for update in _task_updates(payloads)] == [1, 2, 3]
    assert not any(payload.get("event_type") == "chat.file" for payload in payloads)


@pytest.mark.asyncio
async def test_user_feedback_resume_can_complete_existing_final_report_node(tmp_path):
    lines = [
        json.dumps({"__deepsearch_status__": "resuming", "conversation_id": "C1"}),
        json.dumps({
            "__deepsearch_status__": "completed",
            "conversation_id": "C1",
            "final_result": {"response_content": "done"},
        }),
    ]
    report_path = tmp_path / "r.md"
    report_path.write_text("done", encoding="utf-8")
    push = AsyncMock()
    with patch.object(dt, "_resolve_jiuwenclaw_python", return_value="/p"), \
         patch.object(dt, "_resolve_run_script", return_value="/s"), \
         patch.object(dt, "_get_route", return_value={
             "request_id": "R1", "channel_id": "CH1", "session_id": "S1"
         }), \
         patch.object(
             dt,
             "_write_report_artifacts_stream",
             new=AsyncMock(return_value={"md": str(report_path)}),
         ), \
         patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=_Proc(lines))), \
         patch(
             "jiuwenclaw.agentserver.gateway_push.transport.WebSocketGatewayPushTransport",
             return_value=push,
         ):
        completed = await dt.deepresearch_stream._func(
            action="resume",
            conversation_id="C1",
            node="user_feedback_processor",
            feedback='{"feedback":"accepted"}',
            file_name="r",
        )

    assert json.loads(completed)["status"] == "completed"
    payloads = [call.args[0]["payload"] for call in push.send_push.await_args_list]
    assert (
        "task.complete",
        "deepresearch_final_report",
    ) in [
        (payload.get("event_type"), payload.get("stream_source_id"))
        for payload in payloads
    ]
    final_report_complete_index = next(
        index for index, payload in enumerate(payloads)
        if payload.get("event_type") == "task.complete"
        and payload.get("stream_source_id") == "deepresearch_final_report"
    )
    stage_four_update_index = next(
        index for index, payload in enumerate(payloads)
        if payload.get("event_type") == "task.update"
        and payload["tasks"][3]["status"] == "in_progress"
    )
    assert stage_four_update_index < final_report_complete_index
    assert payloads[final_report_complete_index]["task_id"] == "deepresearch_stage_4"


@pytest.mark.asyncio
async def test_outline_titles_are_reused_when_workflow_continues_without_interrupt():
    outline = json.dumps({
        "title": "AI Agent 入门",
        "sections": [
            {"id": "1", "title": "AI Agent 概念定义与核心区分"},
            {"id": "2", "title": "AI Agent 技术架构与工作原理"},
        ],
    }, ensure_ascii=False)
    lines = [
        json.dumps({"__deepsearch_status__": "started", "conversation_id": "C1"}),
        json.dumps({"agent": "outline", "content": outline}),
        json.dumps({
            "agent": "plan_reasoning",
            "section_idx": "2",
            "event": "message",
            "content": "章节规划过程",
        }),
        json.dumps({"__deepsearch_status__": "error", "conversation_id": "C1",
                    "error": "stop after section evidence"}),
    ]
    push = AsyncMock()
    with patch.object(dt, "_resolve_jiuwenclaw_python", return_value="/p"), \
         patch.object(dt, "_resolve_run_script", return_value="/s"), \
         patch.object(dt, "_get_route", return_value={
             "request_id": "R1", "channel_id": "CH1", "session_id": "S1"
         }), \
         patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=_Proc(lines))), \
         patch(
             "jiuwenclaw.agentserver.gateway_push.transport.WebSocketGatewayPushTransport",
             return_value=push,
         ):
        await dt.deepresearch_stream._func(action="start", query="X", file_name="r")

    section_payloads = [
        call.args[0]["payload"]
        for call in push.send_push.await_args_list
        if call.args[0]["payload"].get("stream_source_id") == "deepresearch_section_2"
    ]
    assert section_payloads
    assert all(
        payload["task_content"] == "AI Agent 技术架构与工作原理"
        for payload in section_payloads
    )


@pytest.mark.asyncio
async def test_interrupted_marker_waits_for_runner_to_exit_naturally():
    proc = _RunningProc([
        json.dumps({"__deepsearch_status__": "started", "conversation_id": "C1"}),
        json.dumps({"__deepsearch_status__": "interrupted", "agent": "feedback_handler",
                    "conversation_id": "C1", "content": "请输入反馈"}),
        "runner cleanup complete",
    ])
    with patch.object(dt, "_resolve_jiuwenclaw_python", return_value="/p"), \
         patch.object(dt, "_resolve_run_script", return_value="/s"), \
         patch.object(dt, "_get_route", return_value={"request_id": "", "channel_id": "", "session_id": ""}), \
         patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)), \
         patch("jiuwenclaw.agentserver.gateway_push.transport.WebSocketGatewayPushTransport"):
        result = await dt.deepresearch_stream._func(action="start", query="X", file_name="r")

    assert json.loads(result)["status"] == "interrupted"
    assert proc.stdout_exhausted is True
    assert proc.terminated is False


@pytest.mark.asyncio
async def test_stderr_is_drained_while_subprocess_is_running(tmp_path):
    proc = _StderrBackpressureProc()
    push = AsyncMock()
    report_path = tmp_path / "r.md"
    report_path.write_text("done", encoding="utf-8")
    with patch.object(dt, "_resolve_jiuwenclaw_python", return_value="/p"), \
         patch.object(dt, "_resolve_run_script", return_value="/s"), \
         patch.object(dt, "_get_route", return_value={"request_id": "R1", "channel_id": "CH1", "session_id": "S1"}), \
         patch.object(dt, "_write_report_markdown", return_value=str(report_path)), \
         patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)), \
         patch("jiuwenclaw.agentserver.gateway_push.transport.WebSocketGatewayPushTransport", return_value=push):
        result = await asyncio.wait_for(
            dt.deepresearch_stream._func(action="start", query="X", file_name="r"),
            timeout=0.2,
        )

    assert json.loads(result)["status"] == "completed"
    assert proc.stderr_drained.is_set()


@pytest.mark.asyncio
async def test_completed_marker_can_exceed_asyncio_stream_line_limit(tmp_path):
    report_content = "报告正文" * 20000
    proc = _LargeStdoutLineProc(report_content)
    push = AsyncMock()
    report_path = tmp_path / "r.md"
    report_path.write_text(report_content, encoding="utf-8")
    with patch.object(dt, "_resolve_jiuwenclaw_python", return_value="/p"), \
         patch.object(dt, "_resolve_run_script", return_value="/s"), \
         patch.object(dt, "_get_route", return_value={"request_id": "R1", "channel_id": "CH1", "session_id": "S1"}), \
         patch.object(dt, "_write_report_markdown", return_value=str(report_path)), \
         patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)), \
         patch("jiuwenclaw.agentserver.gateway_push.transport.WebSocketGatewayPushTransport", return_value=push):
        result = await dt.deepresearch_stream._func(action="start", query="X", file_name="r")

    outcome = json.loads(result)
    assert outcome["status"] == "completed"
    assert outcome["report_chars"] == len(report_content)


@pytest.mark.asyncio
async def test_start_returns_completed_outcome(tmp_path):
    final_result = {"response_content": "最终报告正文"}
    lines = [
        json.dumps({"__deepsearch_status__": "started", "conversation_id": "C1"}),
        json.dumps({"agent": "reporter", "content": "最终报告正文"}),
        json.dumps({
            "agent": "sub_reporter",
            "section_idx": "1",
            "section_total": 1,
            "event": "done",
        }),
        json.dumps({"__deepsearch_status__": "completed", "conversation_id": "C1",
                    "final_result": final_result}),
    ]
    push = AsyncMock()
    report_path = tmp_path / "r.md"
    report_path.write_text(final_result["response_content"], encoding="utf-8")
    with patch.object(dt, "_resolve_jiuwenclaw_python", return_value="/p"), \
         patch.object(dt, "_resolve_run_script", return_value="/s"), \
         patch.object(dt, "_get_route", return_value={"request_id": "R1", "channel_id": "CH1", "session_id": "S1"}), \
         patch.object(dt, "_write_report_markdown", return_value=str(report_path)), \
         patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=_Proc(lines))), \
         patch("jiuwenclaw.agentserver.gateway_push.transport.WebSocketGatewayPushTransport", return_value=push):
        result = await dt.deepresearch_stream._func(action="start", query="X", file_name="r")
    out = json.loads(result)
    assert out["status"] == "completed"
    assert out["report_chars"] == len("最终报告正文")
    assert "report_content" not in out


@pytest.mark.asyncio
async def test_completed_marker_rejects_legacy_report_content():
    lines = [
        json.dumps({"__deepsearch_status__": "started", "conversation_id": "C1"}),
        json.dumps({
            "__deepsearch_status__": "completed",
            "conversation_id": "C1",
            "report_content": "legacy report",
        }),
    ]
    write_report = AsyncMock()
    with patch.object(dt, "_resolve_jiuwenclaw_python", return_value="/p"), \
         patch.object(dt, "_resolve_run_script", return_value="/s"), \
         patch.object(dt, "_get_route", return_value={"request_id": "R1", "channel_id": "CH1", "session_id": "S1"}), \
         patch.object(dt, "_write_report_markdown", write_report), \
         patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=_Proc(lines))), \
         patch("jiuwenclaw.agentserver.gateway_push.transport.WebSocketGatewayPushTransport", return_value=AsyncMock()):
        result = await dt.deepresearch_stream._func(action="start", query="X", file_name="r")

    out = json.loads(result)
    assert out["status"] == "error"
    assert out["error_code"] == "empty_report"
    write_report.assert_not_awaited()


@pytest.mark.asyncio
async def test_start_returns_explicit_error_marker():
    lines = [
        json.dumps({"__deepsearch_status__": "started", "conversation_id": "C1"}),
        json.dumps({
            "__deepsearch_status__": "error",
            "conversation_id": "C1",
            "error": "workflow ended without report content",
        }),
    ]
    patches = _patch_env(lines)
    for p in patches:
        p.start()
    try:
        result = await dt.deepresearch_stream._func(action="start", query="X", file_name="r")
    finally:
        for p in patches:
            p.stop()
    out = json.loads(result)
    assert out["status"] == "error"
    assert out["conversation_id"] == "C1"
    assert out["error"] == "workflow ended without report content"


@pytest.mark.asyncio
async def test_start_preserves_web_search_proxy_error_marker():
    lines = [
        json.dumps({"__deepsearch_status__": "started", "conversation_id": "C1"}),
        json.dumps({
            "__deepsearch_status__": "error",
            "conversation_id": "C1",
            "error_code": "web_search_proxy_error",
            "error": "Web Search could not connect through the configured proxy",
        }),
    ]
    patches = _patch_env(lines)
    for p in patches:
        p.start()
    try:
        result = await dt.deepresearch_stream._func(action="start", query="X", file_name="r")
    finally:
        for p in patches:
            p.stop()

    out = json.loads(result)
    assert out["status"] == "error"
    assert out["error_code"] == "web_search_proxy_error"
    assert out["error"] == "Web Search could not connect through the configured proxy"


@pytest.mark.asyncio
async def test_ufp_marker_injects_accumulated_report():
    # UFP 中断:marker 不带 report(report 不在 key 列表),tool 注入累积 report_parts[:6000]
    lines = [
        json.dumps({"__deepsearch_status__": "started", "conversation_id": "C1"}),
        json.dumps({"agent": "reporter", "content": "R" * 7000}),
        json.dumps({"__deepsearch_status__": "interrupted", "agent": "user_feedback_processor",
                    "conversation_id": "C1", "prompt": "请选择后续操作"}),
    ]
    patches = _patch_env(lines)
    for p in patches:
        p.start()
    try:
        result = await dt.deepresearch_stream._func(action="start", query="X", file_name="r")
    finally:
        for p in patches:
            p.stop()
    out = json.loads(result)
    assert out["status"] == "interrupted"
    assert out["node_id"] == "user_feedback_processor"
    assert "完整报告见最终产物" in out["marker"]["report"]
    assert len(out["marker"]["report"]) < 6200


@pytest.mark.asyncio
async def test_resume_requires_conversation_id_and_node():
    patches = _patch_env([])
    for p in patches:
        p.start()
    try:
        result = await dt.deepresearch_stream._func(action="resume", query="X")
    finally:
        for p in patches:
            p.stop()
    out = json.loads(result)
    assert out["status"] == "error"
    assert "conversation_id and node" in out["error"]


@pytest.mark.asyncio
async def test_feedback_resume_does_not_repeat_stage_1_transition():
    start_lines = [
        json.dumps({"__deepsearch_status__": "started", "conversation_id": "C1"}),
        json.dumps({
            "__deepsearch_status__": "interrupted",
            "agent": "feedback_handler",
            "conversation_id": "C1",
        }),
    ]
    resume_lines = [
        json.dumps({"__deepsearch_status__": "resuming", "conversation_id": "C1"}),
        json.dumps({"agent": "outline", "content": "# 第一章"}),
        json.dumps({
            "__deepsearch_status__": "error",
            "conversation_id": "C1",
            "error": "stop after outline",
        }),
    ]
    push = AsyncMock()
    spawn = AsyncMock(side_effect=[_Proc(start_lines), _Proc(resume_lines)])

    with patch.object(dt, "_resolve_jiuwenclaw_python", return_value="/p"), \
         patch.object(dt, "_resolve_run_script", return_value="/s"), \
         patch.object(dt, "_get_route", return_value={
             "request_id": "R1", "channel_id": "CH1", "session_id": "S1",
         }), \
         patch("asyncio.create_subprocess_exec", new=spawn), \
         patch(
             "jiuwenclaw.agentserver.gateway_push.transport.WebSocketGatewayPushTransport",
             return_value=push,
         ):
        interrupted = await dt.deepresearch_stream._func(
            action="start", query="X", file_name="r",
        )
        resumed = await dt.deepresearch_stream._func(
            action="resume",
            conversation_id="C1",
            node="feedback_handler",
            feedback='{"feedback":"补充范围"}',
            file_name="r",
        )

    assert json.loads(interrupted)["status"] == "interrupted"
    assert json.loads(resumed)["status"] == "error"
    payloads = [call.args[0]["payload"] for call in push.send_push.await_args_list]
    transitions = [
        (payload["event_type"], payload["task_id"])
        for payload in payloads
        if payload.get("event_type") in {"chat.reasoning", "chat.delta"}
        and payload.get("content", "").startswith("[DeepResearch 阶段切换]")
    ]
    assert transitions == [
        ("chat.reasoning", "deepresearch_stage_1"),
        ("chat.delta", "deepresearch_stage_1"),
        ("chat.reasoning", "deepresearch_stage_2"),
        ("chat.delta", "deepresearch_stage_2"),
    ]
    assert [_active_stage(update) for update in _task_updates(payloads)] == [1, 2]


@pytest.mark.parametrize(
    "interaction_result",
    [
        {"status": "answered", "answers": []},
        {"status": "skipped", "answers": []},
    ],
)
@pytest.mark.asyncio
async def test_feedback_resume_normalizes_empty_result_to_structured_skipped(
    interaction_result,
):
    lines = [
        json.dumps({"__deepsearch_status__": "resuming", "conversation_id": "C1"}),
        json.dumps({
            "__deepsearch_status__": "interrupted",
            "agent": "feedback_handler",
            "conversation_id": "C1",
        }),
    ]
    spawn = AsyncMock(return_value=_Proc(lines))
    with patch.object(dt, "_resolve_jiuwenclaw_python", return_value="/p"), \
         patch.object(dt, "_resolve_run_script", return_value="/s"), \
         patch.object(dt, "_get_route", return_value={
             "request_id": "", "channel_id": "", "session_id": ""
         }), \
         patch("asyncio.create_subprocess_exec", new=spawn), \
         patch(
             "jiuwenclaw.agentserver.gateway_push.transport.WebSocketGatewayPushTransport"
         ):
        await dt.deepresearch_stream._func(
            action="resume",
            conversation_id="C1",
            node="feedback_handler",
            feedback='{"feedback":"不应使用"}',
            interaction_result=json.dumps(
                interaction_result,
                ensure_ascii=False,
            ),
        )

    argv = list(spawn.await_args.args)
    assert argv[argv.index("--feedback") + 1] == (
        '{"feedback":"","interaction_status":"skipped"}'
    )


@pytest.mark.parametrize(
    ("interaction_result", "expected"),
    [
        (
            {"status": "answered", "answers": [
                {"selected_options": [], "custom_input": "  "},
                {"selected_options": [], "custom_input": None},
            ]},
            '{"feedback":"","interaction_status":"skipped"}',
        ),
        (
            {"status": "skipped", "answers": []},
            '{"feedback":"","interaction_status":"skipped"}',
        ),
        (
            {"status": "answered", "answers": [
                {"selected_options": ["market_scope"], "custom_input": ""},
            ]},
            '{"feedback":"关注市场范围"}',
        ),
        (
            {"status": "answered", "answers": [
                {"selected_options": [], "custom_input": "补充竞品分析"},
            ]},
            '{"feedback":"补充竞品分析"}',
        ),
    ],
)
def test_normalize_feedback_interaction_result(
    interaction_result, expected
):
    assert dt._normalize_feedback_handler_resume_feedback(
        '{"feedback":"关注市场范围"}'
        if "market_scope" in json.dumps(interaction_result)
        else '{"feedback":"补充竞品分析"}',
        json.dumps(interaction_result, ensure_ascii=False),
    ) == expected


@pytest.mark.parametrize(
    ("interaction_result", "error"),
    [
        ("not-json", "合法 JSON"),
        (json.dumps([]), "JSON 对象"),
        (json.dumps({"status": "cancelled", "answers": []}), "cancelled"),
        (json.dumps({"status": "error", "answers": []}), "error"),
        (json.dumps({"status": "unknown", "answers": []}), "unknown"),
        (
            json.dumps({
                "status": "skipped",
                "answers": [{"selected_options": ["market_scope"]}],
            }),
            "不能包含有效回答",
        ),
        (
            json.dumps({
                "status": "skipped",
                "answers": [{"selected_options": [], "custom_input": "补充竞品"}],
            }),
            "不能包含有效回答",
        ),
    ],
)
def test_normalize_feedback_interaction_result_rejects_invalid_states(
    interaction_result, error
):
    with pytest.raises(ValueError, match=error):
        dt._normalize_feedback_handler_resume_feedback(
            '{"feedback":"原反馈"}',
            interaction_result,
        )


@pytest.mark.asyncio
async def test_feedback_resume_rejects_cancelled_without_spawning():
    spawn = AsyncMock()
    with patch.object(dt, "_resolve_jiuwenclaw_python", return_value="/p"), \
         patch.object(dt, "_resolve_run_script", return_value="/s"), \
         patch("asyncio.create_subprocess_exec", new=spawn):
        result = await dt.deepresearch_stream._func(
            action="resume",
            conversation_id="C1",
            node="feedback_handler",
            feedback='{"feedback":"不应使用"}',
            interaction_result=json.dumps({"status": "cancelled", "answers": []}),
        )

    assert json.loads(result) == {
        "status": "error",
        "error": "feedback_handler interaction_result status=cancelled",
    }
    spawn.assert_not_awaited()


@pytest.mark.asyncio
async def test_feedback_resume_rejects_contradictory_skip_without_spawning():
    spawn = AsyncMock()
    with patch.object(dt, "_resolve_jiuwenclaw_python", return_value="/p"), \
         patch.object(dt, "_resolve_run_script", return_value="/s"), \
         patch("asyncio.create_subprocess_exec", new=spawn):
        result = await dt.deepresearch_stream._func(
            action="resume",
            conversation_id="C1",
            node="feedback_handler",
            feedback='{"feedback":"不应使用"}',
            interaction_result=json.dumps({
                "status": "skipped",
                "answers": [
                    {"selected_options": [], "custom_input": "补充竞品"},
                ],
            }),
        )

    assert json.loads(result) == {
        "status": "error",
        "error": "skipped interaction_result 不能包含有效回答",
    }
    spawn.assert_not_awaited()


@pytest.mark.asyncio
async def test_resume_preserves_legacy_positional_argument_order():
    lines = [
        json.dumps({"__deepsearch_status__": "resuming", "conversation_id": "C1"}),
        json.dumps({
            "__deepsearch_status__": "interrupted",
            "agent": "feedback_handler",
            "conversation_id": "C1",
        }),
    ]
    spawn = AsyncMock(return_value=_Proc(lines))
    with patch.object(dt, "_resolve_jiuwenclaw_python", return_value="/p"), \
         patch.object(dt, "_resolve_run_script", return_value="/s"), \
         patch.object(dt, "_get_route", return_value={
             "request_id": "", "channel_id": "", "session_id": ""
         }), \
         patch("asyncio.create_subprocess_exec", new=spawn), \
         patch(
             "jiuwenclaw.agentserver.gateway_push.transport.WebSocketGatewayPushTransport"
         ):
        await dt.deepresearch_stream._func(
            "resume",
            "",
            "C1",
            '{"feedback":"原反馈"}',
            "feedback_handler",
            "report-name",
        )

    argv = list(spawn.await_args.args)
    assert argv[argv.index("--feedback") + 1] == '{"feedback":"原反馈"}'
    assert argv[argv.index("--node") + 1] == "feedback_handler"


@pytest.mark.asyncio
async def test_missing_run_script_returns_error():
    # runner 脚本解析失败 → 早返 error(不 spawn)
    with patch.object(dt, "_resolve_run_script", return_value=""):
        result = await dt.deepresearch_stream._func(action="start", query="X", file_name="r")
    out = json.loads(result)
    assert out["status"] == "error"
    assert "run_deepsearch.py" in out["error"]


@pytest.mark.asyncio
async def test_missing_search_config_returns_error_without_spawning():
    spawn = AsyncMock(return_value=_Proc([]))
    missing_config = {
        "LLM_MODEL_NAME": "model",
        "LLM_MODEL_TYPE": "openai",
        "LLM_BASE_URL": "https://llm.example/v1",
        "LLM_API_KEY": "llm-key",
    }

    with patch.object(dt, "_resolve_run_script", return_value="/s"), \
         patch.object(
             dt, "_build_deepresearch_request_config", return_value=missing_config,
         ), \
         patch("asyncio.create_subprocess_exec", new=spawn), \
         patch.object(dt, "_get_route", return_value={
             "request_id": "R1", "channel_id": "CH1", "session_id": "S1",
         }):
        result = await dt.deepresearch_stream._func(
            action="start",
            query="X",
            file_name="r",
        )

    out = json.loads(result)
    assert out["status"] == "error"
    assert out["error_code"] == "search_config_missing"
    assert "Petal" in out["error"]
    assert "Bocha" in out["error"]
    spawn.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_terminal_marker_captures_stderr():
    # 子进程 stdout 无 status marker → "no terminal marker" + stderr 尾部进 outcome
    lines = [
        json.dumps({"agent": "info_collector", "content": "部分进度"}),  # 非 marker,被路由
        # 没有 started/interrupted/completed marker → loop 结束,默认 error
    ]
    proc = _Proc(lines, stderr_lines=["KeyError: 'LLM_API_KEY'", "Traceback (most recent call last)"])
    with patch.object(dt, "_resolve_jiuwenclaw_python", return_value="/p"), \
         patch.object(dt, "_resolve_run_script", return_value="/s"), \
         patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)), \
         patch.object(dt, "_get_route", return_value={"request_id": "", "channel_id": "", "session_id": ""}):
        result = await dt.deepresearch_stream._func(action="start", query="X", file_name="r")
    out = json.loads(result)
    assert out["status"] == "error"
    assert out["error"] == "no terminal marker"
    assert out["returncode"] == 0
    assert "stderr_tail" in out
    assert "LLM_API_KEY" in out["stderr_tail"]
    assert "Traceback" in out["stderr_tail"]


def test_deepresearch_config_maps_only_required_values():
    source = {
        "API_KEY": "sk-b354", "MODEL_NAME": "glm-5.2", "API_BASE": "https://api.example/v2",
        "MODEL_PROVIDER": "dashscope", "BOCHA_API_KEY": "bkey",
        "GITHUB_TOKEN": "must-not-cross-process-boundary",
        "default_headers": '{"Authorization":"Basic session"}',
    }

    env = dt._build_deepresearch_config(source)

    assert "GITHUB_TOKEN" not in env
    assert env["default_headers"] == '{"Authorization":"Basic session"}'
    assert env["LLM_API_KEY"] == "sk-b354"
    assert env["WEB_SEARCH_API_KEY"] == "bkey"


def test_build_deepresearch_config_maps_global_to_deepsearch_names():
    env = dt._build_deepresearch_config({
        "API_KEY": "sk-b354", "MODEL_NAME": "glm-5.2", "API_BASE": "https://api.example/v2",
        "MODEL_PROVIDER": "dashscope", "BOCHA_API_KEY": "bkey",
    })
    assert env["LLM_API_KEY"] == "sk-b354"
    assert env["LLM_MODEL_NAME"] == "glm-5.2"
    assert env["LLM_BASE_URL"] == "https://api.example/v2"
    assert env["LLM_MODEL_TYPE"] == "qwen"  # dashscope → qwen
    assert env["WEB_SEARCH_API_KEY"] == "bkey"
    assert env["WEB_SEARCH_ENGINE_NAME"] == "bocha"
    assert "WEB_SEARCH_URL" not in env  # bocha 无显式 url,不设(有值才设),脚本侧管默认
    assert env["LLM_SSL_VERIFY"] == "false"  # 子进程默认 true 会触发 ssl_cert required
    assert env["TOOL_SSL_VERIFY"] == "false"  # Petal 同样默认 true 且要求 TOOL_SSL_CERT


def test_build_deepresearch_config_respects_explicit_tool_ssl_verify():
    env = dt._build_deepresearch_config({"MODEL_NAME": "m", "TOOL_SSL_VERIFY": "true"})

    assert env["TOOL_SSL_VERIFY"] == "true"


def test_build_deepresearch_config_petal_requires_explicit_url_and_search_key():
    headers = '{"Authorization":"Basic session"}'
    env = dt._build_deepresearch_config({
        "API_KEY": "sk-x",
        "MODEL_NAME": "m",
        "API_BASE": "https://dashscope.example/v1",
        "WEB_SEARCH_ENGINE_NAME": "petal",
        "WEB_SEARCH_URL": "https://petal.example/v1/ai-tools/web-search",
        "WEB_SEARCH_API_KEY": headers,
    })
    assert env["WEB_SEARCH_ENGINE_NAME"] == "petal"
    assert env["WEB_SEARCH_URL"] == "https://petal.example/v1/ai-tools/web-search"
    assert env["WEB_SEARCH_API_KEY"] == headers


@pytest.mark.parametrize(
    ("invalid_name", "invalid_value"),
    [
        ("WEB_SEARCH_URL", None),
        ("WEB_SEARCH_API_KEY", None),
        ("WEB_SEARCH_URL", "   "),
        ("WEB_SEARCH_API_KEY", "   "),
    ],
)
def test_build_deepresearch_config_rejects_partial_petal_config(invalid_name, invalid_value):
    source = {
        "WEB_SEARCH_ENGINE_NAME": "petal",
        "WEB_SEARCH_URL": "https://petal.example/v1/ai-tools/web-search",
        "WEB_SEARCH_API_KEY": '{"Authorization":"Basic session"}',
    }
    if invalid_value is None:
        source.pop(invalid_name)
    else:
        source[invalid_name] = invalid_value
    env = dt._build_deepresearch_config(source)
    assert "WEB_SEARCH_ENGINE_NAME" not in env
    assert "WEB_SEARCH_API_KEY" not in env
    assert "WEB_SEARCH_URL" not in env


def test_build_deepresearch_config_accepts_provider_specific_petal_key():
    env = dt._build_deepresearch_config({
        "WEB_SEARCH_ENGINE_NAME": "petal",
        "WEB_SEARCH_URL": "https://petal.example/v1/ai-tools/web-search",
        "PETAL_API_KEY": "petal-key",
    })
    assert env["WEB_SEARCH_ENGINE_NAME"] == "petal"
    assert env["WEB_SEARCH_API_KEY"] == "petal-key"


def test_build_deepresearch_config_uses_independent_petal_url_with_custom_llm():
    headers = '{"Authorization":"Basic search-session"}'
    env = dt._build_deepresearch_config({
        "API_KEY": "custom-llm-key",
        "MODEL_NAME": "glm-5.2",
        "API_BASE": "https://dashscope.example/compatible-mode/v1",
        "default_headers": '{"Authorization":"Bearer custom-llm-key"}',
        "PETAL_API_KEY": headers,
        "PETAL_API_URL": "https://client-claw.example/v1/ai-tools/web-search",
    })

    assert env["LLM_BASE_URL"] == "https://dashscope.example/compatible-mode/v1"
    assert env["WEB_SEARCH_ENGINE_NAME"] == "petal"
    assert env["WEB_SEARCH_API_KEY"] == headers
    assert env["WEB_SEARCH_URL"] == "https://client-claw.example/v1/ai-tools/web-search"


def test_build_deepresearch_config_reuses_run_task_petal_fallback():
    source = {
        "API_KEY": "sk-x",
        "MODEL_NAME": "m",
        "API_BASE": "https://client-claw.example/v2",
        "default_headers": '{"Authorization":"Basic session"}',
    }

    resolved = dt._get_task_manager_cls()._load_config(source)
    env = dt._build_deepresearch_config(source)

    assert env["LLM_API_KEY"] == resolved["LLM_API_KEY"]
    assert env["LLM_MODEL_NAME"] == resolved["LLM_MODEL_NAME"]
    assert env["LLM_BASE_URL"] == resolved["LLM_BASE_URL"]
    assert env["WEB_SEARCH_ENGINE_NAME"] == resolved["WEB_SEARCH_ENGINE_NAME"] == "petal"
    assert env["WEB_SEARCH_API_KEY"] == resolved["WEB_SEARCH_API_KEY"]
    assert env["WEB_SEARCH_URL"] == resolved["WEB_SEARCH_URL"]
    assert env["WEB_SEARCH_URL"] == "https://client-claw.example/v1/ai-tools/web-search"


def test_general_llm_configs_normalize_only_styled_report_provider():
    config = {
        "LLM_MODEL_NAME": "qwen-plus",
        "LLM_MODEL_TYPE": "qwen",
        "LLM_BASE_URL": "https://dashscope.example/compatible-mode/v1",
        "LLM_API_KEY": "sk-test",
    }
    extension = {"extra_body": {"thinking": {"type": "disabled"}}}

    workflow_config, report_style_config = (
        dt._get_task_manager_cls()._build_general_llm_configs(config, extension)
    )

    assert workflow_config["model_type"] == "qwen"
    assert report_style_config["model_type"] == "openai"
    assert workflow_config is not report_style_config
    assert workflow_config["api_key"] is not report_style_config["api_key"]
    assert workflow_config["api_key"] == report_style_config["api_key"] == bytearray(b"sk-test")


def test_build_deepresearch_config_empty_value_not_set():
    # 无 API_KEY → 不设 LLM_API_KEY,让 .env 兜底
    env = dt._build_deepresearch_config({"MODEL_NAME": "m"})
    assert "LLM_API_KEY" not in env
    assert env["LLM_MODEL_NAME"] == "m"


def test_child_env_enables_hitl_for_interactive_request(monkeypatch):
    for key in (
        "HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy",
        "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(dt, "export_spawn_environ", lambda: {"PATH": "/usr/bin"})

    assert dt._build_deepresearch_child_env(interactive_ask=True) == {
        "PATH": "/usr/bin",
        "DEEPSEARCH_HITL": "true",
        "LLM_SSL_VERIFY": "false",
        "TOOL_SSL_VERIFY": "false",
        "PYTHONUNBUFFERED": "1",
        "PYTHONUTF8": "1",
    }


def test_child_env_copies_standard_proxy_variables_but_ignores_all_proxy(monkeypatch):
    proxy_env = {
        "HTTP_PROXY": "http://upper-http.proxy:8080",
        "http_proxy": "http://lower-http.proxy:8080",
        "HTTPS_PROXY": "http://upper-https.proxy:8443",
        "https_proxy": "http://lower-https.proxy:8443",
        "NO_PROXY": "localhost,.internal.example",
        "no_proxy": "127.0.0.1,.lower.internal.example",
    }
    for key, value in proxy_env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("ALL_PROXY", "socks5://upper-all.proxy:1080")
    monkeypatch.setenv("all_proxy", "socks5://lower-all.proxy:1080")
    monkeypatch.setenv("UNRELATED_SECRET", "must-not-enter-child")
    monkeypatch.setattr(dt, "export_spawn_environ", lambda: {"PATH": "/usr/bin"})

    env = dt._build_deepresearch_child_env(interactive_ask=False)

    assert {key: env[key] for key in proxy_env} == proxy_env
    assert "ALL_PROXY" not in env
    assert "all_proxy" not in env
    assert "UNRELATED_SECRET" not in env


def test_child_env_omits_empty_deepresearch_proxy_variables(monkeypatch):
    for key in (
        "HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy",
        "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy",
    ):
        monkeypatch.setenv(key, "")
    monkeypatch.setattr(dt, "export_spawn_environ", lambda: {})

    env = dt._build_deepresearch_child_env(interactive_ask=False)

    assert not any(key.lower().endswith("proxy") for key in env)


def test_child_env_disables_hitl_and_overrides_stale_parent(monkeypatch):
    monkeypatch.setattr(dt, "export_spawn_environ", lambda: {})

    env = dt._build_deepresearch_child_env(interactive_ask=False)

    assert env["DEEPSEARCH_HITL"] == "false"
    assert env["PYTHONUNBUFFERED"] == "1"


def test_request_config_resolves_current_tenant_in_memory(monkeypatch):
    observed = {}

    def fake_overlay(*parts, service_id, agent_id):
        observed["tenant"] = (service_id, agent_id)
        observed["parts"] = parts
        return {
            "API_KEY": "huawei-maas-session",
            "default_headers": '{"Authorization":"Basic session"}',
        }

    def fake_build_config(source):
        observed["source"] = source
        return dict(source)

    monkeypatch.setattr(dt, "get_task_env_overlay", lambda: None)
    monkeypatch.setattr(dt, "build_effective_env_overlay", fake_overlay)
    monkeypatch.setattr(dt, "_build_deepresearch_config", fake_build_config)

    config = dt._build_deepresearch_request_config(
        interactive_ask=True,
        service_id="service-1",
        agent_id="office",
    )

    assert observed == {
        "tenant": ("service-1", "office"),
        "parts": (),
        "source": {
            "API_KEY": "huawei-maas-session",
            "default_headers": '{"Authorization":"Basic session"}',
        },
    }
    assert config["default_headers"] == '{"Authorization":"Basic session"}'
    assert config["DEEPSEARCH_HITL"] == "true"


def test_request_config_does_not_fall_through_bound_empty_overlay(monkeypatch):
    monkeypatch.setattr(dt, "get_task_env_overlay", lambda: {})

    def fail_live_tip_fallback(*_args, **_kwargs):
        raise AssertionError("bound task overlay must seal the live tenant tip")

    monkeypatch.setattr(dt, "build_effective_env_overlay", fail_live_tip_fallback)

    config = dt._build_deepresearch_request_config(interactive_ask=True)

    assert "LLM_API_KEY" not in config
    assert "WEB_SEARCH_API_KEY" not in config


@pytest.mark.asyncio
async def test_stream_writes_secret_config_to_stdin_not_child_env():
    lines = [
        json.dumps({"__deepsearch_status__": "started", "conversation_id": "C1"}),
        json.dumps({
            "__deepsearch_status__": "interrupted",
            "agent": "feedback_handler",
            "conversation_id": "C1",
        }),
    ]
    proc = _Proc(lines)
    spawn = AsyncMock(return_value=proc)
    config = {
        "LLM_MODEL_NAME": "model",
        "LLM_MODEL_TYPE": "openai",
        "LLM_BASE_URL": "https://llm.example/v1",
        "LLM_API_KEY": "llm-secret",
        "WEB_SEARCH_ENGINE_NAME": "bocha",
        "WEB_SEARCH_API_KEY": "search-secret",
    }
    child_env = {"PATH": "/usr/bin", "PYTHONUNBUFFERED": "1", "PYTHONUTF8": "1"}

    with patch.object(dt, "_resolve_jiuwenclaw_python", return_value="/p"), \
         patch.object(dt, "_resolve_run_script", return_value="/s"), \
         patch.object(dt, "_build_deepresearch_request_config", return_value=config), \
         patch.object(dt, "_build_deepresearch_child_env", return_value=child_env), \
         patch.object(dt, "_get_route", return_value={
             "request_id": "", "channel_id": "", "session_id": "",
         }), \
         patch("asyncio.create_subprocess_exec", new=spawn), \
         patch(
             "jiuwenclaw.agentserver.gateway_push.transport.WebSocketGatewayPushTransport"
         ):
        result = await dt.deepresearch_stream._func(
            action="start", query="X", file_name="r",
        )

    assert json.loads(result)["status"] == "interrupted"
    assert spawn.await_args.kwargs["stdin"] is asyncio.subprocess.PIPE
    assert spawn.await_args.kwargs["env"] == child_env
    assert "--config-stdin" in spawn.await_args.args
    frame = json.loads(bytes(proc.stdin.data).decode("utf-8"))
    assert frame == {"version": 1, "config": config}
    assert proc.stdin.closed is True
    assert "llm-secret" not in json.dumps(child_env)
    assert "search-secret" not in json.dumps(child_env)


@pytest.mark.asyncio
async def test_config_pipe_is_closed_when_delivery_fails():
    class BrokenStdin(_Stdin):
        async def drain(self):
            raise BrokenPipeError("child exited")

    stdin = BrokenStdin()

    with pytest.raises(BrokenPipeError, match="child exited"):
        await dt._write_deepresearch_config(SimpleNamespace(stdin=stdin), b"frame\n")

    assert stdin.closed is True


@pytest.mark.asyncio
async def test_stop_process_kills_and_reaps_child_that_ignores_terminate():
    class StubbornProcess:
        def __init__(self):
            self.returncode = None
            self.terminate = Mock()
            self.kill = Mock()
            self.wait_calls = 0

        async def wait(self):
            self.wait_calls += 1
            if self.wait_calls == 1:
                await asyncio.Event().wait()
            self.returncode = -9
            return self.returncode

    proc = StubbornProcess()

    await dt._stop_deepresearch_process(proc, timeout=0.01)

    proc.terminate.assert_called_once_with()
    proc.kill.assert_called_once_with()
    assert proc.wait_calls == 2
    assert proc.returncode == -9


@pytest.mark.asyncio
async def test_stream_cancellation_during_config_delivery_stops_child():
    proc = _Proc([])
    proc.returncode = None
    proc.terminate = Mock()
    proc.kill = Mock()

    async def wait():
        proc.returncode = -15
        return proc.returncode

    proc.wait = AsyncMock(side_effect=wait)
    config = {
        "LLM_MODEL_NAME": "model",
        "LLM_MODEL_TYPE": "openai",
        "LLM_BASE_URL": "https://llm.example/v1",
        "LLM_API_KEY": "llm-secret",
        "WEB_SEARCH_ENGINE_NAME": "bocha",
        "WEB_SEARCH_API_KEY": "search-secret",
    }

    with patch.object(dt, "_resolve_jiuwenclaw_python", return_value="/p"), \
         patch.object(dt, "_resolve_run_script", return_value="/s"), \
         patch.object(dt, "_build_deepresearch_request_config", return_value=config), \
         patch.object(dt, "_build_deepresearch_child_env", return_value={"PATH": "/usr/bin"}), \
         patch.object(dt, "_write_deepresearch_config", side_effect=asyncio.CancelledError), \
         patch.object(dt, "_get_route", return_value={
             "request_id": "", "channel_id": "", "session_id": "",
         }), \
         patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)), \
         patch(
             "jiuwenclaw.agentserver.gateway_push.transport.WebSocketGatewayPushTransport"
         ), \
         pytest.raises(asyncio.CancelledError):
        await dt.deepresearch_stream._func(action="start", query="X", file_name="r")

    proc.terminate.assert_called_once_with()
    proc.wait.assert_awaited_once_with()


def _make_fake_skill(parent: str) -> str:
    """在 parent/deepresearch/scripts/run_deepsearch.py 建假 skill,返回 skill dir。"""
    import os
    skill_dir = os.path.join(parent, "deepresearch")
    os.makedirs(os.path.join(skill_dir, "scripts"), exist_ok=True)
    with open(os.path.join(skill_dir, "scripts", "run_deepsearch.py"), "w", encoding="utf-8") as f:
        f.write("# fake")
    return skill_dir


def test_resolve_skill_root_from_env(tmp_path, monkeypatch):
    # sidecar cwd 不含 office-claw-skills → 必须靠 tip 中的 JIUWENCLAW_SHARED_SKILLS_DIRS 命中
    skill_parent = str(tmp_path / "shared-skills")
    os.makedirs(skill_parent)
    skill_dir = _make_fake_skill(skill_parent)
    elsewhere = str(tmp_path / "elsewhere")
    os.makedirs(elsewhere)
    monkeypatch.chdir(elsewhere)  # cwd 不含 skill
    token = bind_task_env_overlay({"JIUWENCLAW_SHARED_SKILLS_DIRS": skill_parent})
    try:
        assert dt._resolve_skill_root() == skill_dir
        assert os.path.basename(dt._resolve_run_script()) == "run_deepsearch.py"
    finally:
        reset_task_env_overlay(token)


def test_deepresearch_python_uses_current_jiuwenclaw_interpreter():
    assert dt._resolve_jiuwenclaw_python() == sys.executable


def test_get_deepresearch_tools_exposes_stream_and_rewrite_tools(monkeypatch):
    monkeypatch.setattr(dt, "enable_deepresearch", lambda: True)
    monkeypatch.setattr(dt, "_deepresearch_dependency_available", lambda: True)

    from jiuwenclaw.agentserver.tools.deepresearch import rewrite_tools as rt

    assert dt.get_deepresearch_tools() == [
        dt.deepresearch_stream,
        rt.deepresearch_prepare_rewrite,
        rt.deepresearch_commit_rewrite,
        rt.deepresearch_generate_rewrite_html,
    ]


def test_resolve_skill_root_env_uses_platform_path_separator(tmp_path, monkeypatch):
    p1 = str(tmp_path / "d1"); os.makedirs(p1); sd1 = _make_fake_skill(p1)
    p2 = str(tmp_path / "d2"); os.makedirs(p2)
    monkeypatch.chdir(tmp_path)
    token = bind_task_env_overlay({
        "JIUWENCLAW_SHARED_SKILLS_DIRS": os.pathsep.join((p1, p2)),
    })
    try:
        assert dt._resolve_skill_root() == sd1  # 命中第一个含 skill 的
    finally:
        reset_task_env_overlay(token)


def test_resolve_skill_root_preserves_windows_drive_letter(tmp_path, monkeypatch):
    windows_parent = r"C:\shared-skills"
    skill_dir = _make_fake_skill(str(tmp_path / windows_parent))
    monkeypatch.setattr(dt.os, "pathsep", ";")
    monkeypatch.chdir(tmp_path)
    token = bind_task_env_overlay({
        "JIUWENCLAW_SHARED_SKILLS_DIRS": rf"{windows_parent};D:\other-skills",
    })
    try:
        assert dt._resolve_skill_root() == os.path.join(windows_parent, "deepresearch")
        assert os.path.samefile(dt._resolve_skill_root(), skill_dir)
    finally:
        reset_task_env_overlay(token)


def test_resolve_skill_root_falls_back_to_cwd(tmp_path, monkeypatch):
    # 无 tip/env → cwd/office-claw-skills/deepresearch
    skill_dir = _make_fake_skill(str(tmp_path / "office-claw-skills"))
    monkeypatch.chdir(tmp_path)
    token = bind_task_env_overlay({})  # seal: tip miss → unset
    try:
        assert dt._resolve_skill_root() == skill_dir
    finally:
        reset_task_env_overlay(token)


def test_resolve_skill_root_empty_when_not_found(tmp_path, monkeypatch):
    monkeypatch.chdir(str(tmp_path))  # 无 office-claw-skills
    token = bind_task_env_overlay({})  # seal: tip miss → unset
    try:
        assert dt._resolve_skill_root() == ""
        assert dt._resolve_run_script() == ""
    finally:
        reset_task_env_overlay(token)
