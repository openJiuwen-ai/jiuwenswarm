# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Wiring regression tests for the A2UI finalize repair/retry adapter.

repair_model_response 与 retry_without_a2ui 的方法体依赖 session-scoped adapter 上
由 create_instance 建立的 _model/_instance；_ensure_adapter 返回的 root facade 按
设计不构建实例（热更新仅级联 session adapter），两者挂在 root 上时 repair 全部被
skip、降级重答在 _instance 守卫处静默返回 None，a2ui 块最终被整块剥离、落库只剩
纯文本。finalize 接线必须先解析出真正执行本轮的 session adapter。
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

