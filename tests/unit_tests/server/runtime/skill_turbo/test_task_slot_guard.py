# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""taskProgress 槽位泄漏登记簿契约测试（B_03）。

锁四类行为：
1. claim / peek / release 基础语义（覆盖后写胜、幂等注销、空输入忽略）；
2. 上限淘汰：超过 _MAX_TRACKED_SESSIONS 按插入序淘汰最旧；
3. 外层 rail 集成：TaskExecutionRail._emit_task_update_event 写流成功即注销
   （槽位被外层 todo 夺回）；写失败保留登记；
4. 键口径：登记簿键 = 外层父会话 session_id（turbo 的 parent_session 与
   rail 的 ctx.session 同一对象）。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.server.runtime.skill_turbo import task_slot_guard
from jiuwenswarm.server.runtime.skill_turbo.task_slot_guard import (
    _MAX_TRACKED_SESSIONS,
    claim_task_slot,
    peek_leaked_task_slot,
    release_task_slot,
    reset_task_slot_registry,
)


@pytest.fixture(autouse=True)
def _clean_registry():
    reset_task_slot_registry()
    yield
    reset_task_slot_registry()


def test_claim_and_peek_roundtrip() -> None:
    claim_task_slot("sess-1", ["task_a", "task_b"])

    leaked = peek_leaked_task_slot("sess-1")

    assert leaked == frozenset({"task_a", "task_b"})
    # 只读不删：清算方在快照实际进流后才 release
    assert peek_leaked_task_slot("sess-1") == leaked


def test_release_is_idempotent_and_peeks_none_after() -> None:
    claim_task_slot("sess-1", ["task_a"])

    release_task_slot("sess-1")
    release_task_slot("sess-1")  # 重复注销静默
    release_task_slot("sess-unknown")  # 无登记静默（外层 rail 每次快照都调）

    assert peek_leaked_task_slot("sess-1") is None


def test_claim_overwrites_previous_owner() -> None:
    """同会话后写胜：并发两条 turbo 流转发时登记跟随后进快照（退化态不仲裁）。"""
    claim_task_slot("sess-1", ["task_a"])
    claim_task_slot("sess-1", ["task_b", "task_c"])

    assert peek_leaked_task_slot("sess-1") == frozenset({"task_b", "task_c"})


def test_empty_inputs_are_ignored() -> None:
    claim_task_slot("", ["task_a"])
    claim_task_slot("   ", ["task_a"])
    claim_task_slot("sess-1", [])
    claim_task_slot("sess-1", ["", "  "])

    assert peek_leaked_task_slot("") is None
    assert peek_leaked_task_slot("sess-1") is None


def test_cap_evicts_oldest_entry() -> None:
    for i in range(_MAX_TRACKED_SESSIONS):
        claim_task_slot(f"sess-{i}", [f"task_{i}"])

    claim_task_slot("sess-new", ["task_new"])

    assert peek_leaked_task_slot("sess-0") is None  # 最旧被淘汰
    assert peek_leaked_task_slot("sess-1") == frozenset({"task_1"})
    assert peek_leaked_task_slot("sess-new") == frozenset({"task_new"})


def test_cap_keeps_existing_key_on_reclaim() -> None:
    """已登记会话重复 claim 不触发淘汰（键已存在，只覆盖值）。"""
    for i in range(_MAX_TRACKED_SESSIONS):
        claim_task_slot(f"sess-{i}", [f"task_{i}"])

    claim_task_slot("sess-0", ["task_0_v2"])

    assert peek_leaked_task_slot("sess-0") == frozenset({"task_0_v2"})
    assert peek_leaked_task_slot("sess-1") == frozenset({"task_1"})


@pytest.mark.asyncio
async def test_rail_task_update_releases_slot(monkeypatch) -> None:
    """外层 rail 发自身 task.update（写流成功）= 槽位被外层夺回，注销登记。"""
    from jiuwenswarm.agents.harness.common.rails.task_execution_rail import (
        TaskExecutionRail,
    )

    claim_task_slot("sess-rail", ["task_leaked"])

    rail = TaskExecutionRail()
    monkeypatch.setattr(rail, "_load_todo_from_json", lambda _sid: [])
    session = SimpleNamespace(
        get_session_id=lambda: "sess-rail",
        write_stream=AsyncMock(),
    )

    await rail._emit_task_update_event(session)

    session.write_stream.assert_awaited_once()
    assert peek_leaked_task_slot("sess-rail") is None


@pytest.mark.asyncio
async def test_rail_task_update_failure_keeps_slot(monkeypatch) -> None:
    """写流失败 = 槽位未被夺回，登记保留（turbo 侧终止性出口仍可关账）。"""
    from jiuwenswarm.agents.harness.common.rails.task_execution_rail import (
        TaskExecutionRail,
    )

    claim_task_slot("sess-rail", ["task_leaked"])

    rail = TaskExecutionRail()
    monkeypatch.setattr(rail, "_load_todo_from_json", lambda _sid: [])
    session = SimpleNamespace(
        get_session_id=lambda: "sess-rail",
        write_stream=AsyncMock(side_effect=RuntimeError("stream down")),
    )

    await rail._emit_task_update_event(session)

    assert peek_leaked_task_slot("sess-rail") == frozenset({"task_leaked"})
