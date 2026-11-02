# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Wiring regression tests for the A2UI finalize repair/retry adapter.

repair_model_response 与 retry_without_a2ui 的方法体依赖 session-scoped adapter 上
由 create_instance 建立的 _model/_instance；_ensure_adapter 返回的 root facade 按
设计不构建实例（热更新仅级联 session adapter），两者挂在 root 上时 repair 全部被
skip、降级重答在 _instance 守卫处静默返回 None，a2ui 块最终被整块剥离、落库只剩
纯文本。finalize 接线必须先解析出真正执行本轮的 session adapter。

用例分两层：
- helper 级（解析分支/门控/链式契约）——覆盖 ``_resolve_turn_adapter_for_finalize``
  自身语义；
- 生产接线点（``process_message_stream`` / ``process_message``）——驱动真实入口，
  断言 repair/retry 两个回调确实绑定到 session adapter。缺了这层，接线回退成
  ``getattr(adapter, ...)`` 时全套件仍全绿，静默内容降级无法被检测。
"""

from types import SimpleNamespace

import pytest

from jiuwenswarm.server.runtime.agent_adapter.interface import JiuWenSwarm

VALID_A2UI_RESPONSE = """<a2ui-json>
[
  {
    "beginRendering": {
      "surfaceId": "wiring-test",
      "root": "root"
    }
  },
  {
    "surfaceUpdate": {
      "surfaceId": "wiring-test",
      "components": [
        {
          "id": "root",
          "component": {
            "Text": {
              "text": {
                "literalString": "Repaired form"
              }
            }
          }
        }
      ]
    }
  }
]
</a2ui-json>"""

INVALID_A2UI_RESPONSE = """Here is the form.
<a2ui-json>
[
  {
    "type": "beginRendering",
    "version": "0.8"
  }
]
</a2ui-json>"""


class _FakeRootAdapter:
    def __init__(self, session_adapter=None, error: BaseException | None = None):
        self._session_adapter = session_adapter
        self._error = error
        self.calls: list[tuple] = []

    async def _get_or_create_session_adapter(self, session_id, request=None):
        self.calls.append((session_id, request))
        if self._error is not None:
            raise self._error
        return self._session_adapter


async def _broken_repair(prompt: str):
    return None


async def _working_repair(prompt: str):
    return SimpleNamespace(content=VALID_A2UI_RESPONSE)


@pytest.mark.asyncio
async def test_resolve_turn_adapter_returns_session_scoped_adapter():
    session = _FakeRootAdapter()  # any object identity works as the session adapter
    root = _FakeRootAdapter(session_adapter=session)
    request = SimpleNamespace(request_id="req-1", channel_id="web")

    resolved = await JiuWenSwarm._resolve_turn_adapter_for_finalize(
        root, request, "sess-1"
    )

    assert resolved is session
    assert root.calls == [("sess-1", request)]


@pytest.mark.asyncio
async def test_resolve_turn_adapter_falls_back_to_root_when_lookup_raises():
    root = _FakeRootAdapter(
        session_adapter=SimpleNamespace(), error=RuntimeError("boom")
    )

    resolved = await JiuWenSwarm._resolve_turn_adapter_for_finalize(
        root,
        SimpleNamespace(request_id="req-2", channel_id="web"),
        "sess-2",
    )

    assert resolved is root


@pytest.mark.asyncio
async def test_resolve_turn_adapter_falls_back_without_session_id():
    root = _FakeRootAdapter(session_adapter=SimpleNamespace())

    resolved = await JiuWenSwarm._resolve_turn_adapter_for_finalize(
        root,
        SimpleNamespace(request_id="req-3", channel_id="web"),
        None,
    )

    assert resolved is root
    assert root.calls == []


@pytest.mark.asyncio
async def test_resolve_turn_adapter_returns_adapter_without_getter():
    adapter = SimpleNamespace()

    resolved = await JiuWenSwarm._resolve_turn_adapter_for_finalize(
        adapter,
        SimpleNamespace(request_id="req-4", channel_id="web"),
        "sess-4",
    )

    assert resolved is adapter


@pytest.mark.asyncio
async def test_finalize_repairs_invalid_block_via_resolved_session_adapter(monkeypatch):
    """链式契约：root 修不动（repair 恒 None），解析出的 session adapter 能修 → 终稿保留有效块。"""
    from jiuwenswarm.server.runtime.a2ui.runtime.response_finalization import (
        finalize_a2ui_assistant_content,
    )

    monkeypatch.setenv("JIUWENSWARM_A2UI_ENABLED", "true")

    session = SimpleNamespace(repair_model_response=_working_repair)
    root = SimpleNamespace(
        repair_model_response=_broken_repair,
        _get_or_create_session_adapter=_FakeRootAdapter(
            session_adapter=session
        )._get_or_create_session_adapter,
    )
    turn = await JiuWenSwarm._resolve_turn_adapter_for_finalize(
        root,
        SimpleNamespace(request_id="req-5", channel_id="web"),
        "sess-5",
    )

    result = await finalize_a2ui_assistant_content(
        INVALID_A2UI_RESPONSE,
        user_query="设备报修登记表单",
        request_id="req-5",
        repair_call=turn.repair_model_response,
        a2ui_enabled=True,
    )

    assert result == VALID_A2UI_RESPONSE


@pytest.mark.asyncio
async def test_resolve_turn_adapter_skips_non_a2ui_channel():
    """非 a2ui 渠道 finalize 会早退：不得解析 adapter，避免无意义的缓存查询与冷建。"""
    root = _FakeRootAdapter(session_adapter=SimpleNamespace())

    resolved = await JiuWenSwarm._resolve_turn_adapter_for_finalize(
        root,
        SimpleNamespace(request_id="req-6", channel_id="feishu"),
        "sess-6",
    )

    assert resolved is root
    assert root.calls == []


@pytest.mark.asyncio
async def test_resolve_turn_adapter_skips_channel_without_repair(monkeypatch):
    """a2ui 渠道但未启用服务端 repair 时同样早退，避免为一个不会使用的 repair 冷建。"""
    from jiuwenswarm.server.runtime.agent_adapter import interface as interface_module

    monkeypatch.setattr(interface_module, "should_finalize_repair", lambda _channel: False)
    root = _FakeRootAdapter(session_adapter=SimpleNamespace())

    resolved = await JiuWenSwarm._resolve_turn_adapter_for_finalize(
        root,
        SimpleNamespace(request_id="req-7", channel_id="web"),
        "sess-7",
    )

    assert resolved is root
    assert root.calls == []


# ── 生产接线点锚定（检视意见：helper 级用例无法发现接线回退）────────────────────
# 上面 7 个用例只验证 ``_resolve_turn_adapter_for_finalize`` 本身或手工接线后的
# finalize 契约；把 interface.py 里两处生产接线回退成 ``getattr(adapter, ...)``
# 后全套件仍全绿——真实 bug（root facade 无实例，repair 恒 skip、retry 静默返回
# None，a2ui 块被整块剥皮）无法被检测。以下用例驱动真实入口
# （``process_message_stream`` / ``process_message``），断言 repair/retry 两个回调
# 都绑定到解析出的 session adapter：
#   - repair：``__self__`` 必须是 session adapter（root 的 repair 恒不可修）
#   - retry：真实调用一次，root 因 ``_instance is None`` 会静默返回 None，
#     能返回内容即证明闭包捕获的是带实例的 session adapter

_RETRY_CONTENT = "RETRY_WITHOUT_A2UI_OK"


class _SessionAdapterProbe:
    """执行本轮的 session adapter：有实例，repair 与 retry 都可用。"""

    def __init__(self) -> None:
        self._instance = object()
        self.retried_queries: list[str] = []

    async def repair_model_response(self, _prompt: str):
        return SimpleNamespace(content=VALID_A2UI_RESPONSE)

    async def process_message_impl(self, _request, _inputs):
        self.retried_queries.append(_request.params.get("query"))
        return SimpleNamespace(ok=True, payload={"content": _RETRY_CONTENT})


class _RootFacadeProbe:
    """``_ensure_adapter`` 返回的 root facade：按设计不自建实例。"""

    def __init__(self, session_adapter, frames: list[dict] | None = None) -> None:
        self._instance = None
        self._session_adapter = session_adapter
        self._frames = frames or []
        self.resolved_session_ids: list[str] = []

    async def _get_or_create_session_adapter(self, session_id, request=None, **kwargs):
        self.resolved_session_ids.append(session_id)
        return self._session_adapter

    async def repair_model_response(self, _prompt: str):
        return None

    async def handle_heartbeat(self, _request):
        return None

    async def process_message_stream_impl(self, request, _inputs):
        from jiuwenswarm.common.schema.agent import AgentResponseChunk

        for payload in self._frames:
            yield AgentResponseChunk(
                request_id=request.request_id,
                channel_id=request.channel_id,
                payload=dict(payload),
                is_complete=False,
            )

    async def process_message_impl(self, _request, _inputs):
        return SimpleNamespace(ok=True, payload={"content": "ROOT_RETRY_MUST_NOT_RUN"})


def _web_request(*, is_stream: bool):
    from jiuwenswarm.common.schema.agent import AgentRequest
    from jiuwenswarm.common.schema.message import ReqMethod

    return AgentRequest(
        request_id="req-wiring",
        req_method=ReqMethod.CHAT_SEND,
        channel_id="web",
        session_id="session-wiring",
        params={"query": "生成表单", "mode": "agent"},
        is_stream=is_stream,
    )


def _build_swarm(monkeypatch, root):
    from jiuwenswarm.server.runtime.agent_adapter import interface as interface_module

    swarm = interface_module.JiuWenSwarm()
    swarm._adapter = root
    swarm._sdk_name = "harness"
    monkeypatch.setattr(swarm, "_build_inputs", lambda request: ({}, "local", "生成表单"))
    monkeypatch.setattr(interface_module, "get_config", lambda: {"preferred_language": "zh"})
    monkeypatch.setattr(interface_module, "is_auto_memory_enabled", lambda *_a, **_k: False)
    monkeypatch.setattr(interface_module, "is_memory_enabled", lambda *_a, **_k: False)
    monkeypatch.setattr(interface_module, "_schedule_symphony_session_feedback", lambda *_a: None)
    monkeypatch.setattr(interface_module, "append_history_record", lambda **_k: None)
    monkeypatch.setattr(swarm, "_append_history_record", lambda **_k: None)
    return swarm


def _capture_finalize(monkeypatch):
    """截获真实 finalize 调用，保留 repair/retry 回调供断言。"""
    from jiuwenswarm.server.runtime.agent_adapter import interface as interface_module

    captured: dict[str, object] = {}

    async def fake_finalize(content, **kwargs):
        captured.setdefault("repair_call", kwargs.get("repair_call"))
        captured.setdefault(
            "retry_without_a2ui_call", kwargs.get("retry_without_a2ui_call")
        )
        return content

    monkeypatch.setattr(
        interface_module, "finalize_assistant_response_if_a2ui", fake_finalize
    )
    return captured


async def _assert_both_calls_bound_to_session(
    captured: dict[str, object], session, root
) -> None:
    repair_call = captured["repair_call"]
    retry_call = captured["retry_without_a2ui_call"]

    assert getattr(repair_call, "__self__", None) is session, (
        "repair 回调必须绑定到执行本轮的 session adapter（root facade 无实例，"
        f"repair 会恒 skip）：实际 {getattr(repair_call, '__self__', None)!r}"
    )

    # root 的 _instance 为 None → retry 闭包会静默返回 None（静默内容降级）
    assert await retry_call("重答一次") == _RETRY_CONTENT, (
        "retry 回调必须走 session adapter；拿 root 时 _instance 守卫会静默返回 None"
    )
    assert session.retried_queries == ["重答一次"], (
        "retry 必须真正落到 session adapter 的 process_message_impl"
    )


@pytest.mark.asyncio
async def test_stream_entrypoint_wires_turn_adapter_for_repair_and_retry(monkeypatch):
    """生产接线点 1：``process_message_stream`` 收尾必须用 session adapter 组装回调。"""
    session = _SessionAdapterProbe()
    root = _RootFacadeProbe(
        session,
        frames=[
            {"event_type": "chat.delta", "content": "表单正文"},
            {"event_type": "chat.final", "content": "终稿"},
        ],
    )
    swarm = _build_swarm(monkeypatch, root)
    captured = _capture_finalize(monkeypatch)

    try:
        async for _ in swarm.process_message_stream(_web_request(is_stream=True)):
            pass
    finally:
        await swarm._session_manager.close_all_sessions()

    assert "session-wiring" in root.resolved_session_ids, (
        "finalize 时必须解析出本轮 session adapter"
    )
    await _assert_both_calls_bound_to_session(captured, session, root)


@pytest.mark.asyncio
async def test_unary_entrypoint_wires_turn_adapter_for_repair_and_retry(monkeypatch):
    """生产接线点 2：``process_message`` 非流式收尾同样必须用 session adapter。"""
    from jiuwenswarm.server.runtime.a2ui import integration as integration_module

    session = _SessionAdapterProbe()
    root = _RootFacadeProbe(session)
    swarm = _build_swarm(monkeypatch, root)
    captured = _capture_finalize(monkeypatch)

    # 绕过非流式路径上与本接线无关的前置（技能/插件/租户/配额/人脉附件）
    async def _noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(swarm, "_handle_skills_request", _noop)
    monkeypatch.setattr(swarm, "_handle_plugins_request", _noop)
    monkeypatch.setattr(swarm, "_materialize_enterprise_attachments", _noop)
    monkeypatch.setattr(swarm, "_append_ask_user_answered_history", lambda **_k: None)
    monkeypatch.setattr(swarm, "_bind_tenant_request_context", lambda *_a, **_k: (None, None))
    monkeypatch.setattr(
        swarm, "_reset_tenant_request_context", staticmethod(lambda *_a: None)
    )
    monkeypatch.setattr(
        integration_module, "reload_quota_policies_from_gateway_db", _noop, raising=False
    )

    async def fake_submit_and_wait(_session_id, _task):
        return SimpleNamespace(ok=True, payload={"content": "终稿"})

    monkeypatch.setattr(swarm._session_manager, "submit_and_wait", fake_submit_and_wait)

    try:
        await swarm.process_message(_web_request(is_stream=False))
    finally:
        await swarm._session_manager.close_all_sessions()

    assert "session-wiring" in root.resolved_session_ids, (
        "非流式 finalize 同样必须解析出本轮 session adapter"
    )
    await _assert_both_calls_bound_to_session(captured, session, root)

