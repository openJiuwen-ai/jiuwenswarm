# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""B 类 request_id 绑定单测（诊断日志隔离收尾，见
``docs/diagnosis_log_isolation_followup_design.md``）。

B 类：AgentServer wire_parse._maybe_bind_diagnosis_request_id 前缀守卫。
不依赖真实 openjiuwen 原语：monkeypatch sys.modules 注入 stub。
"""
from __future__ import annotations

import sys
from types import SimpleNamespace


# ---------------------------------------------------------------------------
# B 类：_maybe_bind_diagnosis_request_id 前缀守卫
# ---------------------------------------------------------------------------


def _patch_request_id_ctx(monkeypatch) -> list[str]:
    """注入 stub span_context，捕获 set_current_request_id 调用。"""
    set_calls: list[str] = []
    fake = SimpleNamespace(set_current_request_id=lambda r: set_calls.append(r))
    monkeypatch.setitem(
        sys.modules,
        "openjiuwen.extensions.observability.span_context",
        fake,
    )
    return set_calls


def test_maybe_bind_diagnosis_request_id_diagnosis_prefix(monkeypatch) -> None:
    """B 类：diagnosis- 前缀 → set_current_request_id 透传。"""
    set_calls = _patch_request_id_ctx(monkeypatch)
    from jiuwenswarm.server.wire_parse import _maybe_bind_diagnosis_request_id

    _maybe_bind_diagnosis_request_id({"request_id": "diagnosis-sess_abc"})
    assert set_calls == ["diagnosis-sess_abc"]


def test_maybe_bind_diagnosis_request_id_skips_non_diagnosis(monkeypatch) -> None:
    """B 类：普通 request_id 不绑定（保守，零主流程影响）。"""
    set_calls = _patch_request_id_ctx(monkeypatch)
    from jiuwenswarm.server.wire_parse import _maybe_bind_diagnosis_request_id

    _maybe_bind_diagnosis_request_id({"request_id": "chat-123"})
    _maybe_bind_diagnosis_request_id({"request_id": "diagnosis_noprefix"})  # 下划线非连字符
    assert set_calls == []


def test_maybe_bind_diagnosis_request_id_tolerates_bad_input(monkeypatch) -> None:
    """B 类：非 dict / 缺字段 / 类型错 不抛、不绑定。"""
    set_calls = _patch_request_id_ctx(monkeypatch)
    from jiuwenswarm.server.wire_parse import _maybe_bind_diagnosis_request_id

    _maybe_bind_diagnosis_request_id(None)
    _maybe_bind_diagnosis_request_id("not-a-dict")
    _maybe_bind_diagnosis_request_id({})  # 无 request_id
    _maybe_bind_diagnosis_request_id({"request_id": 123})  # 非 str
    assert set_calls == []
