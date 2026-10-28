# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
from __future__ import annotations

import asyncio
import json

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse, AgentResponseChunk
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.common.ws_limits import AGENT_WS_SEND_BUDGET_BYTES

# conftest 有个 autouse fixture 会把 ``pipeline.dispatch_parsed_request`` 换成 stub。
# 模块导入发生在任何 fixture 之前，这里抓住真函数的引用，供需要打真链路的用例使用。
from jiuwenswarm.server.pipeline import (
    dispatch_parsed_request as _REAL_DISPATCH_PARSED_REQUEST,
)


# ============================================================ 桩


class FakeWs:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, text: str) -> None:
        self.sent.append(json.loads(text))


class TrackingLock(asyncio.Lock):
    """统计**成功获取**次数的发送锁。

    计数必须放在 ``await super().acquire()`` **之后** —— 放在之前的话，
    等锁期间被取消的调用（收尾时取消心跳任务就会这样）也会被计入，
    于是「加锁次数」凭空多出一次而帧没发出去，断言随机失败。
    """

    def __init__(self) -> None:
        super().__init__()
        self.acquisitions = 0

    async def acquire(self) -> bool:  # type: ignore[override]
        acquired = await super().acquire()
        self.acquisitions += 1
        return acquired


class StubAgent:
    def __init__(self, chunks: list[AgentResponseChunk] | None = None,
                 unary: AgentResponse | None = None) -> None:
        self._chunks = chunks or []
        self._unary = unary

    async def process_message_stream(self, request):  # noqa: ANN001
        for chunk in self._chunks:
            yield chunk

    async def process_message(self, request):  # noqa: ANN001
        return self._unary


def _chunk(request_id: str, text: str, *, complete: bool = False) -> AgentResponseChunk:
    return AgentResponseChunk(
        request_id=request_id,
        channel_id="web",
        payload={"event_type": "chat.delta", "content": text},
        is_complete=complete,
    )


def _request(*, stream: bool, method: ReqMethod = ReqMethod.CHAT_SEND,
             session_id: str = "s1", request_id: str = "req-1") -> AgentRequest:
    return AgentRequest(
        request_id=request_id,
        channel_id="web",
        session_id=session_id,
        req_method=method,
        params={"query": "hi", "mode": "agent"},
        is_stream=stream,
    )


@pytest.fixture()
def dp():
    from jiuwenswarm.server.handlers import _default as mod

    return mod


@pytest.fixture()
def server(monkeypatch):
    from jiuwenswarm.server import agent_ws_server as mod
    from jiuwenswarm.server.runtime.agent_adapter import interface_deep

    async def _noop_checkpointer() -> None:
        return None

    monkeypatch.setattr(interface_deep, "ensure_persistent_checkpointer", _noop_checkpointer)
    return mod.AgentWebSocketServer()


def _ctx(server, ws, send_lock, request):
    from jiuwenswarm.server.context import AgentServerServices, RequestContext
    from jiuwenswarm.server.transports.sink import WSSink

    return RequestContext(
        request=request,
        sink=WSSink(ws, send_lock),
        connection_id=str(id(ws)),
        services=AgentServerServices(server),
    )


def _install_agent(dp, monkeypatch, agent, *, restored_plan: bool = False) -> None:
    async def _prepare(ctx, request, channel_id, sync_metadata=True):  # noqa: ANN001
        return ("agent", None, agent)

    async def _ensure_state(*_args, **_kwargs):  # noqa: ANN002
        return restored_plan

    monkeypatch.setattr(dp, "_prepare_code_mode_chat_turn", _prepare)
    monkeypatch.setattr(dp, "_ensure_code_mode_state", _ensure_state)


def _run_stream(dp, server, request, *, lock=None) -> FakeWs:
    ws = FakeWs()
    lock = lock if lock is not None else asyncio.Lock()
    asyncio.run(dp._handle_stream_impl(_ctx(server, ws, lock, request), request))
    return ws


def _run_unary(dp, server, request, *, lock=None) -> FakeWs:
    ws = FakeWs()
    lock = lock if lock is not None else asyncio.Lock()
    asyncio.run(dp._handle_unary_impl(_ctx(server, ws, lock, request), request))
    return ws


def test_tenant_stream_stops_when_flush_send_fails(dp, server, monkeypatch, caplog):
    """尾包写出失败时不得再打「流式响应完成」，与 agent 路径一致。"""
    req = _request(stream=True, session_id="s-flush-fail")
    checked = {"exit": False}

    class _Pool:
        async def process_message_stream(self, request):  # noqa: ANN001
            yield _chunk(request.request_id, "头")
            yield _chunk(request.request_id, "尾")

    async def _tenant_prepare(*_a, **_k):  # noqa: ANN002
        return ("agent", None, object())

    async def _check_exit(*_a, **_k):  # noqa: ANN002
        checked["exit"] = True

    from jiuwenswarm.server.transports.sink import WSSink

    original = WSSink.send_chunk

    async def _send(self, chunk, *, sequence, response_id=None):  # noqa: ANN001
        payload = chunk.payload or {}
        if payload.get("content") == "尾":
            return False
        return await original(self, chunk, sequence=sequence, response_id=response_id)

    monkeypatch.setattr(dp, "_uses_tenant_pool", lambda _r: True)
    monkeypatch.setattr(server, "_tenant_pool", lambda: _Pool())
    monkeypatch.setattr(dp, "_prepare_tenant_code_mode_chat_turn", _tenant_prepare)
    monkeypatch.setattr(dp, "_check_post_process_plan_exit", _check_exit)
    monkeypatch.setattr(WSSink, "send_chunk", _send)

    with caplog.at_level("INFO", logger="jiuwenswarm.server.handlers._default"):
        ws = _run_stream(dp, server, req)

    dumped = json.dumps(ws.sent, ensure_ascii=False)
    assert "头" in dumped
    assert "尾" not in dumped
    assert "流式响应完成" not in caplog.text
    assert checked["exit"] is True


def test_stream_emits_ordered_chunks_and_completes(dp, server, monkeypatch):
    req = _request(stream=True)
    _install_agent(dp, monkeypatch, StubAgent([
        _chunk(req.request_id, "第一段"),
        _chunk(req.request_id, "第二段"),
        _chunk(req.request_id, "", complete=True),
    ]))

    ws = _run_stream(dp, server, req)

    assert len(ws.sent) == 3, f"应发 3 帧，实际 {len(ws.sent)}"
    assert [f["sequence"] for f in ws.sent] == [0, 1, 2], "sequence 必须从 0 连续递增"
    assert ws.sent[-1]["response_kind"] == "e2a.complete", "最后一帧必须是 complete"
    assert all(f["response_kind"] == "e2a.chunk" for f in ws.sent[:-1])
    assert ws.sent[0]["body"]["delta"] == "第一段"


def test_stream_cleans_up_session_task_registry(dp, server, monkeypatch):
    req = _request(stream=True, session_id="s-cleanup")
    _install_agent(dp, monkeypatch, StubAgent([_chunk(req.request_id, "x", complete=True)]))

    _run_stream(dp, server, req)

    leftover = server._session_stream_tasks.get("s-cleanup") or {}
    assert not leftover, f"session 流式任务表应清空，实际残留 {len(leftover)} 项"


def test_stream_aborts_after_oversized_chunk(dp, server, monkeypatch):
    req = _request(stream=True)
    huge = "x" * (AGENT_WS_SEND_BUDGET_BYTES + 1000)
    _install_agent(dp, monkeypatch, StubAgent([
        _chunk(req.request_id, "正常"),
        _chunk(req.request_id, huge),          # 超预算 → 应在此中止
        _chunk(req.request_id, "不该发出", complete=True),
    ]))

    ws = _run_stream(dp, server, req)

    assert len(ws.sent) == 2, f"应在超限帧处停止（共 2 帧），实际 {len(ws.sent)}"
    assert ws.sent[0]["body"]["delta"] == "正常"
    # 第二帧必须是降级后的错误帧，不是 6MB 原文
    dumped = json.dumps(ws.sent[1], ensure_ascii=False)
    assert len(dumped.encode()) < AGENT_WS_SEND_BUDGET_BYTES, "超限帧必须被替换，不能原样发出"
    assert "不该发出" not in json.dumps(ws.sent, ensure_ascii=False), "中止后不得继续发送"


def test_stream_task_is_cancellable_and_leaves_no_residue(dp, server, monkeypatch):
    req = _request(stream=True, session_id="s-cancel")

    started = asyncio.Event()
    blocked = asyncio.Event()   # 测试永不 set，agent 就一直卡在这

    class BlockingAgent:
        async def process_message_stream(self, request):  # noqa: ANN001
            yield _chunk(request.request_id, "首片")
            started.set()
            await blocked.wait()      # 卡住，直到被 cancel
            yield _chunk(request.request_id, "不该发出", complete=True)

    _install_agent(dp, monkeypatch, BlockingAgent())

    async def _run():
        ws = FakeWs()
        lock = asyncio.Lock()
        task = asyncio.create_task(
            dp._handle_stream_impl(_ctx(server, ws, lock, req), req)
        )
        await asyncio.wait_for(started.wait(), timeout=5)   # 确保已进入流式
        entries = server._session_stream_tasks.get("s-cancel") or {}
        assert entries, "流式进行中，任务表里应有登记"
        for stream_task, stop_event in list(entries.items()):
            stop_event.set()
            stream_task.cancel()
        cancelled = False
        try:
            await task
        except asyncio.CancelledError:
            cancelled = True
        return cancelled, server._session_stream_tasks.get("s-cancel") or {}, ws.sent

    cancelled, leftover, sent = asyncio.run(_run())
    assert cancelled, "cancel 后任务应以 CancelledError 结束"
    assert not leftover, f"取消后任务表应清空，实际残留 {len(leftover)} 项"
    assert "不该发出" not in json.dumps(sent, ensure_ascii=False), "取消后不得继续发送"


def test_heartbeat_sends_through_ctx_sink_with_sequence_minus_one() -> None:
    """心跳必须经 ``ctx.sink`` 出去，并用 ``sequence=-1``。

    **为什么用 AST 断言，而不是跑真实时序。**

    这是一条**结构性**不变量：心跳与主循环都经 ``ctx.sink``，因此共用连接级
    ``send_lock``；谁给心跳单开一条出口，帧就会在线上交错。它跟"跑多久、触发几次"
    没有关系。

    早先的写法是让 agent 睡一会儿、把心跳间隔压到几十毫秒，赌这期间会触发几次
    心跳，再数加锁次数。那样做有两个代价，而且在 Linux CI 上都兑现了：
    触发次数与取消时机随机漂移导致断言不稳；更糟的是一旦哪一步卡住，
    整条流水线要等到 pytest-timeout 的 60 秒才报一个没有信息量的超时
    （本地 Windows 单跑 20 次、整文件 8 次、transport 全量、乃至全量 6659 passed
    均无法复现，根因始终没定位）。

    用 AST 检查同一条不变量：零时序、零 async、不可能挂，而且**更严格** ——
    它连"心跳恰好没被触发"的情况都能覆盖，而跑时序的版本在那种情况下只会
    误报成失败。
    """
    import ast
    import inspect

    from jiuwenswarm.server.handlers import _default as dp_mod

    tree = ast.parse(inspect.getsource(dp_mod))
    loops = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_heartbeat_loop"
    ]
    assert len(loops) == 1, f"期望恰好一个 _heartbeat_loop，找到 {len(loops)} 个"
    heartbeat = loops[0]

    def _is_ctx_sink(node: ast.AST) -> bool:
        return (
            isinstance(node, ast.Attribute)
            and node.attr == "sink"
            and isinstance(node.value, ast.Name)
            and node.value.id == "ctx"
        )

    sends = [
        node for node in ast.walk(heartbeat)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr.startswith("send_")
    ]
    assert sends, "心跳循环里没有任何发送调用 —— keepalive 还发得出去吗？"

    for call in sends:
        assert _is_ctx_sink(call.func.value), (
            f"心跳用 `{ast.unparse(call.func.value)}.{call.func.attr}` 发送，"
            f"而不是 ctx.sink —— 单开出口意味着不共用连接级 send_lock，帧会在线上交错。"
        )

    seqs = [
        kw.value for call in sends for kw in call.keywords if kw.arg == "sequence"
    ]
    assert seqs, "心跳发送未显式指定 sequence"
    for seq in seqs:
        assert (
            isinstance(seq, ast.UnaryOp)
            and isinstance(seq.op, ast.USub)
            and getattr(seq.operand, "value", None) == 1
        ), f"心跳的 sequence 应为 -1，实际 `{ast.unparse(seq)}`"


def test_restored_plan_pushes_mode_exited(dp, server, monkeypatch):
    pushed: list[dict] = []

    async def _capture_push(msg):  # noqa: ANN001
        pushed.append(msg)

    monkeypatch.setattr(server, "send_push", _capture_push)

    req = _request(stream=True)
    _install_agent(dp, monkeypatch,
                   StubAgent([_chunk(req.request_id, "x", complete=True)]),
                   restored_plan=True)

    _run_stream(dp, server, req)

    assert pushed, "restored_plan 为 True 时应推送 plan 退出事件"
    payload = pushed[0].get("payload") or {}
    assert payload.get("event_type"), "推送必须带 event_type"
    assert pushed[0].get("session_id") == req.session_id


def test_unary_normal_branch_sends_single_response(dp, server, monkeypatch):
    req = _request(stream=False)
    resp = AgentResponse(request_id=req.request_id, channel_id="web", ok=True,
                         payload={"content": "答复"})
    _install_agent(dp, monkeypatch, StubAgent(unary=resp))

    ws = _run_unary(dp, server, req)

    assert len(ws.sent) == 1, f"非流式应只发 1 帧，实际 {len(ws.sent)}"
    assert ws.sent[0]["request_id"] == req.request_id


def test_unary_stateless_branch_uses_stateless_agent(dp, server, monkeypatch):
    req = _request(stream=False)
    resp = AgentResponse(request_id=req.request_id, channel_id="web", ok=True,
                         payload={"content": "stateless"})

    used = {"stateless": False, "prepare": False}

    async def _get_stateless(ctx, channel_id):  # noqa: ANN001
        used["stateless"] = True
        return StubAgent(unary=resp)

    async def _prepare(*_a, **_k):  # noqa: ANN002
        used["prepare"] = True
        return ("agent", None, StubAgent(unary=resp))

    monkeypatch.setattr(dp, "_is_stateless_method_request", lambda _r: True)
    monkeypatch.setattr(dp, "_get_stateless_agent", _get_stateless)
    monkeypatch.setattr(dp, "_prepare_code_mode_chat_turn", _prepare)

    ws = _run_unary(dp, server, req)

    assert used["stateless"], "无状态分支应走 _get_stateless_agent"
    assert not used["prepare"], "无状态分支不应触发 code-mode 准备"
    assert len(ws.sent) == 1


def test_unary_tenant_pool_branch_short_circuits(dp, server, monkeypatch):
    req = _request(stream=False)
    resp = AgentResponse(request_id=req.request_id, channel_id="web", ok=True,
                         payload={"content": "tenant"})

    used = {"pool": False, "prepare": False, "tenant_prepare": False}

    class _Pool:
        async def process_message(self, request):  # noqa: ANN001
            used["pool"] = True
            return resp

    async def _prepare(*_a, **_k):  # noqa: ANN002
        used["prepare"] = True
        return ("agent", None, StubAgent(unary=resp))

    async def _tenant_prepare(*_a, **_k):  # noqa: ANN002
        used["tenant_prepare"] = True
        return None

    monkeypatch.setattr(dp, "_uses_tenant_pool", lambda _r: True)
    monkeypatch.setattr(server, "_tenant_pool", lambda: _Pool())
    monkeypatch.setattr(dp, "_prepare_code_mode_chat_turn", _prepare)
    monkeypatch.setattr(dp, "_prepare_tenant_code_mode_chat_turn", _tenant_prepare)

    ws = _run_unary(dp, server, req)

    assert used["pool"], "应走租户池分支"
    assert used["tenant_prepare"], "chat.send 租户池应先做 code.plan 同步"
    assert not used["prepare"], "不应再走默认 agent_manager 的 code-mode 准备"
    assert len(ws.sent) == 1


def test_pipeline_reraises_cancellation() -> None:
    """汇合点必须**重抛** ``CancelledError``，不能吞掉。

    吞掉会让被中断的任务以「正常返回」告终：``task.cancelled()`` 为 False、
    ``asyncio.gather(..., return_exceptions=True)`` 拿到 ``None`` 而非
    ``CancelledError``，上层再也分不清「跑完了」和「被中断了」——
    而 ``handlers/chat.py`` 的 cancel 分支正是按「gather 会收到 CancelledError」写的。

    更实际的后果：吞掉之后协程继续跑后续 ``finally`` 里的 await（如
    ``SSESink.finish``），而此时取消请求已被消费，那些 await 若阻塞就再无人能打断。
    """
    import asyncio

    from jiuwenswarm.common.schema.agent import AgentRequest
    from jiuwenswarm.common.schema.message import ReqMethod
    from jiuwenswarm.server import pipeline as pipeline_mod
    from jiuwenswarm.server.context import RequestContext
    from jiuwenswarm.server.transports.sink import UnaryHTTPSink

    async def _cancelled_dispatch(_ctx, _request) -> bool:
        raise asyncio.CancelledError()

    async def main() -> str:
        request = AgentRequest(
            request_id="cancel-me",
            channel_id="web",
            session_id="s1",
            req_method=ReqMethod.SESSION_LIST,
            params={},
        )
        ctx = RequestContext(
            request=request, sink=UnaryHTTPSink(), connection_id="c1", services=None
        )
        original = pipeline_mod.dispatch_with_context
        pipeline_mod.dispatch_with_context = _cancelled_dispatch  # type: ignore[assignment]
        try:
            await _REAL_DISPATCH_PARSED_REQUEST(ctx, request)
            return "swallowed"
        except asyncio.CancelledError:
            return "propagated"
        finally:
            pipeline_mod.dispatch_with_context = original  # type: ignore[assignment]

    assert asyncio.run(main()) == "propagated", (
        "取消被汇合点吞掉了 —— 调用方会把「被中断」误判为「正常结束」。"
    )
