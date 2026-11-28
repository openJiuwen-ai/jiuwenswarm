# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""request-scoped MCP worker 初始化失败的「可诊断 + 不空转」行为。

现场背景：Excel 轮次里同一个连接器连续 16 次报
``MCP worker initialization interrupted: server=xlsx-mcp``，
间隔 6–8s，日志里只有 ``error=``（CancelledError 的 str() 恒为空），
模型无法判断是凭证失效还是传输抖动，只能反复重试直到轮次耗尽。

这里锁住三条行为：
1. 失败日志带类型 / repr / close_reason / 是否被取消；
2. 401/403 与「连续失败达上限」换成带可执行指引的失败，而不是原始异常；
3. 判定不可恢复后，同一请求内再次 acquire 立即失败，不再起 worker、不再空转。
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.common import mcp_config

REQUEST_ID = "req-excel"
SERVER = "xlsx-mcp"


@pytest.fixture(autouse=True)
def _clean_pool_state():
    """这组状态是模块级的，测试之间必须清干净。"""

    mcp_config._request_scoped_mcp_sessions.clear()
    mcp_config._request_scoped_mcp_init_failures.clear()
    mcp_config._request_scoped_mcp_dead.clear()
    yield
    mcp_config._request_scoped_mcp_sessions.clear()
    mcp_config._request_scoped_mcp_init_failures.clear()
    mcp_config._request_scoped_mcp_dead.clear()


async def _fail_init(monkeypatch, exc: BaseException):
    """起一个 worker，让 init 抛指定异常，并把一个排队 caller 挂上去。"""

    monkeypatch.setattr(
        mcp_config, "_enter_remote_mcp_session", AsyncMock(side_effect=exc)
    )
    worker = mcp_config._PooledMcpWorker(SERVER)
    mcp_config._request_scoped_mcp_sessions[(REQUEST_ID, SERVER)] = worker
    request = mcp_config._McpCallRequest("get_workbook_context", {})
    worker.queue.put_nowait(request)
    worker.task = asyncio.create_task(
        mcp_config._run_mcp_worker({"_mcp_client_type": "sse"}, worker)
    )
    result = (await asyncio.gather(request.future, return_exceptions=True))[0]
    return worker, result


@pytest.mark.asyncio
async def test_auth_failure_tells_the_model_to_stop_and_ask_for_a_resend(monkeypatch):
    """403 是确定性失败：第一次就要说清「重试无用」，并登记为不可恢复。"""

    worker, result = await _fail_init(
        monkeypatch,
        RuntimeError("Error POSTing to endpoint (HTTP 403): XLSX_CAPABILITY_INVALID"),
    )
    assert isinstance(result, mcp_config.McpWorkerStoppedError)
    message = str(result)
    assert "xlsx-mcp" in message
    assert "401/403" in message
    assert "Do not retry" in message
    assert "resend" in message
    assert (REQUEST_ID, SERVER) in mcp_config._request_scoped_mcp_dead
    await asyncio.wait_for(worker.task, 1)


def test_observed_http_403_wins_over_an_opaque_cancellation():
    """真实 SDK 会把 initialize 的 403 变成取消；HTTP 层记录的状态必须能救回来。"""

    from types import SimpleNamespace

    from jiuwenswarm.common import mcp_call_timeout_patch as patch

    patch.begin_mcp_http_observation()
    try:
        patch._record_mcp_http_response(
            SimpleNamespace(
                status_code=403,
                request=SimpleNamespace(method="POST", url="http://host/mcp"),
            )
        )
        assert patch.observed_mcp_http_failure() == (403, "POST", "http://host/mcp")
        assert mcp_config._observed_http_failure_status() == 403
    finally:
        patch.clear_mcp_http_observation()


def test_non_failure_responses_are_not_recorded():
    """2xx 不能写成失败观测，否则重连成功的连接也会被误判。"""

    from types import SimpleNamespace

    from jiuwenswarm.common import mcp_call_timeout_patch as patch

    patch.begin_mcp_http_observation()
    try:
        patch._record_mcp_http_response(SimpleNamespace(status_code=200, request=None))
        assert patch.observed_mcp_http_failure() is None
        assert mcp_config._observed_http_failure_status() is None
    finally:
        patch.clear_mcp_http_observation()


@pytest.mark.asyncio
async def test_http_403_makes_an_opaque_cancel_actionable(monkeypatch):
    """没有可用文案的取消 + HTTP 403 → 必须给出「凭证失效，别重试」而不是 interrupted。"""

    from types import SimpleNamespace

    from jiuwenswarm.common import mcp_call_timeout_patch as patch

    async def fake_enter(stack, params, client_type):
        patch._record_mcp_http_response(
            SimpleNamespace(
                status_code=403,
                request=SimpleNamespace(method="POST", url="http://host/mcp"),
            )
        )
        raise asyncio.CancelledError()

    monkeypatch.setattr(mcp_config, "_enter_remote_mcp_session", fake_enter)
    worker = mcp_config._PooledMcpWorker(SERVER)
    mcp_config._request_scoped_mcp_sessions[(REQUEST_ID, SERVER)] = worker
    request = mcp_config._McpCallRequest("read_cells", {})
    worker.queue.put_nowait(request)
    worker.task = asyncio.create_task(
        mcp_config._run_mcp_worker({"_mcp_client_type": "streamable-http"}, worker)
    )
    result = (await asyncio.gather(request.future, return_exceptions=True))[0]
    with pytest.raises(asyncio.CancelledError):
        await worker.task
    message = str(result)
    assert "HTTP 403 Unauthorized" in message
    assert "Do not retry" in message
    assert (REQUEST_ID, SERVER) in mcp_config._request_scoped_mcp_dead


def test_auth_detection_ignores_bare_numbers():
    """``4013 ms`` 之类的数字串不能当成 403；误判会让整轮连接器被判定死亡。"""

    assert (
        mcp_config._mcp_auth_failure(RuntimeError("connect failed after 4013 ms"))
        is False
    )
    assert mcp_config._mcp_auth_failure(RuntimeError("retry in 4030 ms")) is False
    assert (
        mcp_config._mcp_auth_failure(
            RuntimeError("Error POSTing to endpoint (HTTP 403)")
        )
        is True
    )
    assert (
        mcp_config._mcp_auth_failure(RuntimeError("ClientResponseError(status=401)"))
        is True
    )
    assert (
        mcp_config._mcp_auth_failure(
            RuntimeError("invalid_token: XLSX_CAPABILITY_INVALID")
        )
        is True
    )


@pytest.mark.asyncio
async def test_dead_connector_fails_fast_without_spawning_another_worker(monkeypatch):
    """已判定不可恢复后，再 acquire 必须立即失败，不再起 worker 等 5s 强杀。"""

    marker = "MCP worker initialization failed: credential expired"
    mcp_config._request_scoped_mcp_dead[(REQUEST_ID, SERVER)] = marker
    with pytest.raises(mcp_config.McpWorkerStoppedError) as info:
        await mcp_config.acquire_request_scoped_mcp_session(
            REQUEST_ID,
            SERVER,
            {"_mcp_client_type": "sse"},
        )
    assert str(info.value) == marker
    assert (REQUEST_ID, SERVER) not in mcp_config._request_scoped_mcp_sessions


@pytest.mark.asyncio
async def test_interrupted_init_keeps_cause_and_close_reason(monkeypatch):
    """CancelledError 的 str() 是空的：错误消息与日志都必须补上类型和原因。"""

    monkeypatch.setattr(
        mcp_config,
        "_enter_remote_mcp_session",
        AsyncMock(side_effect=asyncio.CancelledError()),
    )
    worker = mcp_config._PooledMcpWorker(SERVER)
    worker.close_reason = "shutdown"
    mcp_config._request_scoped_mcp_sessions[(REQUEST_ID, SERVER)] = worker
    request = mcp_config._McpCallRequest("read_cells", {})
    worker.queue.put_nowait(request)
    worker.task = asyncio.create_task(
        mcp_config._run_mcp_worker({"_mcp_client_type": "sse"}, worker)
    )
    with pytest.raises(asyncio.CancelledError):
        await worker.task
    result = (await asyncio.gather(request.future, return_exceptions=True))[0]
    assert isinstance(result, mcp_config.McpWorkerStoppedError)
    message = str(result)
    assert "MCP worker initialization interrupted" in message
    assert "CancelledError" in message
    assert "close_reason=shutdown" in message
    assert message != "MCP worker initialization interrupted: server=xlsx-mcp"


@pytest.mark.asyncio
async def test_init_failure_log_carries_type_repr_and_close_reason(monkeypatch, caplog):
    """日志必须能独立定位：类型、repr、关闭原因、是否被取消。"""

    caplog.set_level(logging.WARNING)
    worker, _ = await _fail_init(monkeypatch, RuntimeError("boom during connect"))
    records = [
        r.getMessage() for r in caplog.records if "init failed" in r.getMessage()
    ]
    assert records, "init failure must be logged"
    line = records[-1]
    assert "error_type=RuntimeError" in line
    assert "error=RuntimeError('boom during connect')" in line
    assert "close_reason=" in line
    assert "cancelling=" in line
    await asyncio.wait_for(worker.task, 1)


@pytest.mark.asyncio
async def test_repeated_generic_init_failures_stop_at_the_limit(monkeypatch):
    """非鉴权失败允许一次重试，但同一请求连续三次后必须停止重建并给出指引。"""

    monkeypatch.setattr(
        mcp_config,
        "_enter_remote_mcp_session",
        AsyncMock(side_effect=RuntimeError("host unreachable")),
    )
    messages: list[str] = []
    for _ in range(mcp_config._MCP_INIT_FAILURE_LIMIT):
        worker = mcp_config._PooledMcpWorker(SERVER)
        mcp_config._request_scoped_mcp_sessions[(REQUEST_ID, SERVER)] = worker
        request = mcp_config._McpCallRequest("read_cells", {})
        worker.queue.put_nowait(request)
        worker.task = asyncio.create_task(
            mcp_config._run_mcp_worker({"_mcp_client_type": "sse"}, worker)
        )
        result = (await asyncio.gather(request.future, return_exceptions=True))[0]
        messages.append(str(result))
        await asyncio.wait_for(worker.task, 1)

    # 前两次保留原始异常（仍可被上层按原类型处理），第三次换成带指引的停止信号。
    assert messages[0] == "host unreachable"
    assert "3 times" in messages[-1]
    assert "stop retrying" in messages[-1]
    assert (REQUEST_ID, SERVER) in mcp_config._request_scoped_mcp_dead


@pytest.mark.asyncio
async def test_release_clears_failure_markers_for_the_request(monkeypatch):
    """请求结束必须把失败计数 / 不可恢复标记一起回收，否则进程里会一直残留。"""

    mcp_config._request_scoped_mcp_dead[(REQUEST_ID, SERVER)] = "credential expired"
    mcp_config._request_scoped_mcp_init_failures[(REQUEST_ID, SERVER)] = 3
    mcp_config._request_scoped_mcp_dead[("other-request", SERVER)] = "keep me"

    await mcp_config.release_request_scoped_mcp_sessions(REQUEST_ID)

    assert (REQUEST_ID, SERVER) not in mcp_config._request_scoped_mcp_dead
    assert (REQUEST_ID, SERVER) not in mcp_config._request_scoped_mcp_init_failures
    assert mcp_config._request_scoped_mcp_dead[("other-request", SERVER)] == "keep me"
