# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Loop-detection tests for develop's CircuitBreakerRail.

These cover repeat / ping-pong / unknown-tool / no-progress interrupts.
They are not the issue 3975 OPEN/HALF_OPEN recovery breaker.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

from openjiuwen.core.single_agent.rail.base import (
    AgentCallbackContext,
    ToolCallInputs,
)

from jiuwenswarm.agents.harness.common.rails.execution_guard.circuit_breaker_rail import (
    CircuitBreakerConfig,
    CircuitBreakerRail,
    ToolCallRecord,
)
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
    _agent_ras_kwargs_from_config,
)


def _record(
    tool_name: str,
    args_hash: str,
    result_hash: str | None = "r1",
    *,
    has_error: bool = False,
) -> ToolCallRecord:
    return ToolCallRecord(
        tool_name=tool_name,
        args_hash=args_hash,
        result_hash=result_hash,
        timestamp=time.time(),
        has_error=has_error,
    )


def _rail(**overrides: int) -> CircuitBreakerRail:
    return CircuitBreakerRail(
        CircuitBreakerConfig(
            warning_threshold=overrides.get("warning_threshold", 3),
            critical_threshold=overrides.get("critical_threshold", 4),
            global_breaker_threshold=overrides.get("global_breaker_threshold", 5),
            unknown_tool_threshold=overrides.get("unknown_tool_threshold", 3),
        )
    )


def test_generic_repeat_warns_without_force_finish() -> None:
    rail = _rail(warning_threshold=3, global_breaker_threshold=10)
    history = [
        _record("read_file", "a", f"out-{index}")
        for index in range(3)
    ]
    result = rail._detect(history, "read_file", "a")
    assert result.stuck is True
    assert result.level == "warning"
    assert result.detector == "generic_repeat"


def test_global_breaker_interrupts_no_progress() -> None:
    rail = _rail(global_breaker_threshold=5)
    history = [_record("bash", "cmd", "same") for _ in range(5)]
    result = rail._detect(history, "bash", "cmd")
    assert result.stuck is True
    assert result.level == "critical"
    assert result.detector == "global_circuit_breaker"


def test_unknown_tool_repeat_interrupts_error_streak() -> None:
    rail = _rail(unknown_tool_threshold=3, global_breaker_threshold=10)
    history = [
        _record("missing_tool", "x", f"err-{index}", has_error=True)
        for index in range(3)
    ]
    result = rail._detect(history, "missing_tool", "x")
    assert result.stuck is True
    assert result.level == "critical"
    assert result.detector == "unknown_tool_repeat"


def test_ping_pong_critical_when_both_sides_stall() -> None:
    rail = _rail(critical_threshold=3, warning_threshold=2, global_breaker_threshold=20)
    history: list[ToolCallRecord] = []
    for _ in range(3):
        history.append(_record("read_file", "hash-a", "same-a"))
        history.append(_record("write_file", "hash-b", "same-b"))
    result = rail._detect(history, "write_file", "hash-b")
    assert result.stuck is True
    assert result.level == "critical"
    assert result.detector == "ping_pong"


def test_cleanup_session_drops_history() -> None:
    rail = _rail()
    rail._histories["s1"] = [_record("bash", "a")]
    rail._histories["s2"] = [_record("bash", "b")]
    rail.cleanup_session("s1")
    assert "s1" not in rail._histories
    assert "s2" in rail._histories


def test_detect_is_isolated_per_session_history() -> None:
    rail = _rail(warning_threshold=3, global_breaker_threshold=10)
    session_a = [_record("read_file", "a", f"a-{index}") for index in range(3)]
    session_b = [_record("read_file", "a", "once")]
    hot = rail._detect(session_a, "read_file", "a")
    cold = rail._detect(session_b, "read_file", "a")
    assert hot.detector == "generic_repeat"
    assert cold.stuck is False


async def test_after_tool_call_force_finishes_unknown_tool() -> None:
    rail = _rail(unknown_tool_threshold=2, global_breaker_threshold=10)
    rail._histories["s1"] = [_record("ghost", "g", "e1", has_error=True)]
    ctx = AgentCallbackContext(
        agent=object(),
        inputs=ToolCallInputs(
            tool_call=SimpleNamespace(name="ghost", arguments={"k": 1}),
            tool_result={"success": False, "error": "not found"},
        ),
        extra={"__jiuwenswarm_cb_session_id__": "s1"},
    )
    await rail.after_tool_call(ctx)
    assert ctx.has_force_finish_request is True


def test_circuit_breaker_builder_stays_disabled_by_default(monkeypatch) -> None:
    from jiuwenswarm.server.runtime.agent_adapter import interface_deep as deep_mod

    monkeypatch.setattr(deep_mod, "get_config", lambda: {"execution_guard": {}})
    adapter = JiuWenSwarmDeepAdapter()
    assert adapter._build_circuit_breaker_rail() is None


def test_circuit_breaker_builder_rejects_invalid_thresholds(monkeypatch) -> None:
    from jiuwenswarm.server.runtime.agent_adapter import interface_deep as deep_mod

    monkeypatch.setattr(
        deep_mod,
        "get_config",
        lambda: {
            "execution_guard": {
                "circuit_breaker": {
                    "enabled": True,
                    "warning_threshold": "10",
                    "critical_threshold": 5,
                }
            }
        },
    )
    adapter = JiuWenSwarmDeepAdapter()
    assert adapter._build_circuit_breaker_rail() is None


def test_circuit_breaker_builder_coerces_string_thresholds(monkeypatch) -> None:
    from jiuwenswarm.server.runtime.agent_adapter import interface_deep as deep_mod

    monkeypatch.setattr(
        deep_mod,
        "get_config",
        lambda: {
            "execution_guard": {
                "circuit_breaker": {
                    "enabled": True,
                    "warning_threshold": "10",
                    "critical_threshold": "20",
                    "global_breaker_threshold": "30",
                    "unknown_tool_threshold": "10",
                }
            }
        },
    )
    adapter = JiuWenSwarmDeepAdapter()
    rail = adapter._build_circuit_breaker_rail()
    assert rail is not None
    assert rail._config.warning_threshold == 10
    assert rail._config.critical_threshold == 20


def test_dest_agent_ras_passthrough_is_not_disabled() -> None:
    """dest-stable RAS remains the default loop detector; do not overwrite it."""
    kwargs = _agent_ras_kwargs_from_config(
        {
            "agent_ras": {
                "enabled": True,
                "detectors": {"repeat_tool": {"enabled": True}},
            }
        }
    )
    assert kwargs["agent_ras"]["enabled"] is True
    assert kwargs["agent_ras"]["detectors"]["repeat_tool"]["enabled"] is True
