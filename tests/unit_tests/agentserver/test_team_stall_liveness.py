# coding: utf-8
"""停摆看门狗的 leader 活性判定。

预算耗尽后成员侧各自停轮、leader 内核仍停在 running；旧的
``state == "running"`` 判定让看门狗永远看不到“死”。收窄为：账本耗尽且
leader 无在途轮 = 无产出能力，其余形态保持原判定。
"""
from __future__ import annotations

from types import SimpleNamespace

from jiuwenswarm.server.runtime.agent_adapter.team_stall_watchdog import (
    _runtime_is_alive,
)


def _agent(*, exhausted: bool, in_flight: bool):
    return SimpleNamespace(
        is_round_budget_exhausted=lambda: exhausted,
        has_in_flight_round=lambda: in_flight,
    )


def test_non_running_state_is_not_alive():
    assert _runtime_is_alive("paused", _agent(exhausted=False, in_flight=False)) is False
    assert _runtime_is_alive("stopped", None) is False


def test_running_state_without_capability_keeps_old_behaviour():
    assert _runtime_is_alive("running", None) is True
    assert _runtime_is_alive("running", SimpleNamespace()) is True


def test_running_and_not_exhausted_is_alive():
    assert _runtime_is_alive(
        "running", _agent(exhausted=False, in_flight=False),
    ) is True


def test_running_exhausted_with_in_flight_round_is_alive():
    """收尾轮仍在跑：不能判死。"""
    assert _runtime_is_alive(
        "running", _agent(exhausted=True, in_flight=True),
    ) is True


def test_running_exhausted_without_in_flight_round_is_dead():
    """成员已全部停轮、leader 空闲：本回合确定无预算再推进。"""
    assert _runtime_is_alive(
        "running", _agent(exhausted=True, in_flight=False),
    ) is False


def test_probe_failure_keeps_old_behaviour():
    def _boom():
        raise RuntimeError("probe exploded")

    agent = SimpleNamespace(
        is_round_budget_exhausted=_boom,
        has_in_flight_round=lambda: False,
    )
    assert _runtime_is_alive("running", agent) is True
