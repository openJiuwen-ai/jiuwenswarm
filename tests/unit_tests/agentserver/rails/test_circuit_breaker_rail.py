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
            non_retryable_threshold=overrides.get("non_retryable_threshold", 3),
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


def test_no_progress_streak_stops_at_a_different_call() -> None:
    """Intervening tools must not be skipped when counting a no-progress streak."""
    rail = _rail(global_breaker_threshold=3, warning_threshold=10)
    history = [
        _record("read_file", "a", "same"),
        _record("bash", "b", "other"),
        _record("read_file", "a", "same"),
        _record("read_file", "a", "same"),
    ]
    assert rail._get_no_progress_streak(history, "read_file", "a") == 2
    result = rail._detect(history, "read_file", "a")
    assert result.stuck is False


def test_no_progress_streak_stops_when_result_hash_is_missing() -> None:
    """A missing result hash cannot continue a no-progress streak."""
    rail = _rail(global_breaker_threshold=2, warning_threshold=10)
    history = [
        _record("bash", "cmd", "same"),
        _record("bash", "cmd", None),
        _record("bash", "cmd", "same"),
    ]
    assert rail._get_no_progress_streak(history, "bash", "cmd") == 1
    result = rail._detect(history, "bash", "cmd")
    assert result.stuck is False


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


def test_non_retryable_tool_streak_stops_on_threshold_failure() -> None:
    rail = _rail(
        warning_threshold=10,
        critical_threshold=10,
        global_breaker_threshold=10,
        unknown_tool_threshold=10,
    )
    history = [
        _record("invoke_pptx_translation", "a", "err", has_error=True),
        _record("invoke_pptx_translation", "a", "err", has_error=True),
        _record("invoke_pptx_translation", "a", "err", has_error=True),
    ]
    for record in history:
        record.non_retryable = True
    result = rail._detect(history, "invoke_pptx_translation", "a")
    assert result.stuck is True
    assert result.level == "critical"
    assert result.detector == "non_retryable_tool"


def test_non_retryable_streak_requires_same_args() -> None:
    rail = _rail(
        warning_threshold=10,
        critical_threshold=10,
        global_breaker_threshold=10,
        unknown_tool_threshold=10,
    )
    history = [
        _record("send_file_to_user", "missing-a", "err", has_error=True),
        _record("send_file_to_user", "missing-b", "err", has_error=True),
        _record("send_file_to_user", "missing-c", "err", has_error=True),
    ]
    for record in history:
        record.non_retryable = True
    result = rail._detect(history, "send_file_to_user", "missing-c")
    assert result.detector != "non_retryable_tool"
    assert result.stuck is False


def test_non_retryable_result_marker_is_detected() -> None:
    rail = _rail()
    contract = '"contract": "jiuwenswarm.non_retryable.v1"'
    assert rail._is_non_retryable_result(
        f'{{{contract}, "success": false, "retryable": false, '
        '"suggested_tool": "send_file_to_user"}'
    )
    assert rail._is_non_retryable_result(
        {"contract": "jiuwenswarm.non_retryable.v1", "retryable": False}
    )
    assert rail._is_non_retryable_result(
        {"content": f'{{{contract}, "retryable": false}}'}
    )
    assert not rail._is_non_retryable_result('{"success": true}')


def test_contract_shaped_json_file_is_not_a_tool_failure() -> None:
    rail = _rail()
    assert not rail._is_non_retryable_result(
        '{"success": false, "retryable": false, "suggested_tool": "read_file"}'
    )


def test_free_text_retryable_field_does_not_trigger() -> None:
    rail = _rail(
        warning_threshold=10,
        critical_threshold=10,
        global_breaker_threshold=10,
        unknown_tool_threshold=10,
    )
    history = [
        _record(
            "read_file",
            "a",
            'config example: {"retryable": false, "note": "not a tool contract"}',
        ),
        _record(
            "read_file",
            "a",
            'doc example: {"retryable": false, "note": "not a tool contract"}',
        ),
    ]
    for record in history:
        record.non_retryable = rail._is_non_retryable_result(record.result_hash)
    result = rail._detect(history, "read_file", "a")
    assert result.detector != "non_retryable_tool"
    assert result.stuck is False


def test_bare_retryable_false_without_contract_marker_is_ignored() -> None:
    rail = _rail()
    assert not rail._is_non_retryable_result({"retryable": False})
    assert not rail._is_non_retryable_result('{"retryable": false}')


def test_build_non_retryable_tool_result_roundtrips() -> None:
    from jiuwenswarm.agents.harness.common.tools.tool_result_contract import (
        build_non_retryable_tool_result,
        is_non_retryable_tool_result,
    )

    payload = build_non_retryable_tool_result(
        "PPT_TOOLS_NOT_ACTIVE", "send_file_to_user"
    )
    assert is_non_retryable_tool_result(payload)


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


async def test_real_non_retryable_tool_result_stops_on_threshold_call(tmp_path) -> None:
    from jiuwenswarm.agents.harness.common.tools.send_file_to_user import (
        SendFileToolkit,
    )

    rail = _rail(
        warning_threshold=10,
        critical_threshold=10,
        global_breaker_threshold=10,
        unknown_tool_threshold=10,
    )
    missing = tmp_path / "ai_chip_report.md"
    result = await SendFileToolkit(
        request_id="r-missing",
        session_id="s1",
        channel_id="web",
    ).send_file(str(missing))

    contexts = []
    for _ in range(3):
        ctx = AgentCallbackContext(
            agent=object(),
            inputs=ToolCallInputs(
                tool_call=SimpleNamespace(
                    name="send_file_to_user",
                    arguments={"abs_file_path_list": [str(missing)]},
                ),
                tool_result=result,
            ),
            extra={"__jiuwenswarm_cb_session_id__": "s1"},
        )
        contexts.append(ctx)
        await rail.after_tool_call(ctx)

    assert contexts[0].has_force_finish_request is False
    assert contexts[1].has_force_finish_request is False
    assert contexts[2].has_force_finish_request is True


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
                    "non_retryable_threshold": "3",
                }
            }
        },
    )
    adapter = JiuWenSwarmDeepAdapter()
    rail = adapter._build_circuit_breaker_rail()
    assert rail is not None
    assert rail._config.warning_threshold == 10
    assert rail._config.critical_threshold == 20
    assert rail._config.non_retryable_threshold == 3


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
