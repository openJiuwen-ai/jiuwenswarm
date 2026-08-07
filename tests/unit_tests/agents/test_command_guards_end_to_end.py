# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The 2026-08-02 Git OOM incident, driven end to end through the real path.

The unit tests next door pin each guard in isolation. These drive the whole
chain the incident actually took -- shipped config, adapter wiring, real
subprocess, real failure payload, real detector, real rail -- because every
defect this incident exposed lived in a seam between two working parts:

  - the tool reported failures in a shape the detector could not read
  - the detector had no rule matching "identical call, identical failure"
  - the rule that was added was never read out of the config
  - and the whole breaker was disabled, so none of it ran

Each of those passes any test that stops at the component boundary.
"""

from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from typing import Any

import pytest
import yaml

from jiuwenswarm.agents.harness.common.rails.execution_guard.circuit_breaker_rail import (
    CircuitBreakerConfig,
    CircuitBreakerRail,
    ToolResultErrorDetector,
)
from jiuwenswarm.agents.harness.common.tools import command_tools

CONFIG_PATH = (
    Path(__file__).resolve().parents[3] / "jiuwenswarm" / "resources" / "config.yaml"
)

# Deterministic and byte-identical every run, like the incident's git call.
FAILING_COMMAND = (
    "python3 -c \"import sys; "
    "sys.stderr.write('fatal: Out of memory, realloc failed\\n'); sys.exit(128)\""
)


class _Ctx:
    """The slice of the rail context the circuit breaker touches."""

    def __init__(self, command: str, result: str) -> None:
        args = {"command": command}
        self.session = SimpleNamespace(session_id="sess-e2e")
        self.inputs = SimpleNamespace(
            tool_call=SimpleNamespace(name="bash", arguments=args),
            tool_name="bash",
            tool_args=args,
            tool_result=result,
            tool_msg=None,
        )
        self.exception = None
        self.extra: dict[str, Any] = {}
        self.steering: list[str] = []
        self.force_finish: dict | None = None

    def push_steering(self, msg: str) -> None:
        self.steering.append(msg)

    def request_force_finish(self, result: dict) -> None:
        self.force_finish = result


# ---------------------------------------------------------------------------
# The shipped configuration
# ---------------------------------------------------------------------------


def _shipped_circuit_breaker() -> dict[str, Any]:
    raw = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    return (raw.get("execution_guard") or {}).get("circuit_breaker") or {}


def test_the_shipped_config_enables_the_breaker() -> None:
    """It shipped disabled, which is why a tuned ladder of detectors sat out.

    Every threshold in this incident was already calibrated. None of them ran.
    """
    assert _shipped_circuit_breaker().get("enabled") is True


def test_the_shipped_config_cuts_the_incident_before_a_human_had_to() -> None:
    """3 and 5, against the 9 calls the incident reached."""
    cb = _shipped_circuit_breaker()
    warn = cb["identical_repeat_threshold"]
    abort = cb["identical_repeat_abort_threshold"]

    assert 2 <= warn < abort, (warn, abort)
    assert abort < 9, "the incident reached 9; the guard must land well before"


# ---------------------------------------------------------------------------
# Config -> adapter -> rail
# ---------------------------------------------------------------------------


ADAPTER_SOURCE = (
    Path(__file__).resolve().parents[3]
    / "jiuwenswarm" / "server" / "runtime" / "agent_adapter" / "interface_deep.py"
)


def _build_rail(cb_cfg: dict[str, Any]) -> CircuitBreakerRail | None:
    """Drive the adapter's real builder against a given config.

    Importing the adapter pulls in the whole server runtime, so this skips
    rather than fails where agent-core is older than the tree (it is pinned to
    a branch, not a version). `test_no_config_field_is_left_unwired` reads the
    source from disk instead and covers the same seam without an import.
    """
    interface_deep = pytest.importorskip(
        "jiuwenswarm.server.runtime.agent_adapter.interface_deep",
        reason="server runtime unavailable in this environment",
    )

    adapter = SimpleNamespace(_resolve_runtime_language=lambda: "en")
    original = interface_deep.get_config
    interface_deep.get_config = lambda: {"execution_guard": {"circuit_breaker": cb_cfg}}
    try:
        return interface_deep.JiuWenSwarmDeepAdapter._build_circuit_breaker_rail(adapter)
    finally:
        interface_deep.get_config = original


def test_every_threshold_survives_the_trip_from_config_to_rail() -> None:
    """A knob the adapter forgets to read is a knob that silently does nothing.

    `identical_repeat_abort_threshold` shipped unread once already: the config
    key existed, the dataclass field existed, and the value in between was the
    default by coincidence rather than by wiring.
    """
    rail = _build_rail({
        "enabled": True,
        "warning_threshold": 11,
        "critical_threshold": 21,
        "global_breaker_threshold": 31,
        "unknown_tool_threshold": 12,
        "identical_repeat_threshold": 4,
        "identical_repeat_abort_threshold": 6,
    })

    assert rail is not None
    config = rail._config
    assert config.warning_threshold == 11
    assert config.critical_threshold == 21
    assert config.global_breaker_threshold == 31
    assert config.unknown_tool_threshold == 12
    assert config.identical_repeat_threshold == 4
    assert config.identical_repeat_abort_threshold == 6


def test_no_config_field_is_left_unwired() -> None:
    """Guards the seam generically, so the next field added cannot be forgotten.

    Reads the source from disk rather than importing it, so this runs even where
    the server runtime cannot be imported -- it is the check that would have
    caught `identical_repeat_abort_threshold` shipping unread.
    """
    source = ADAPTER_SOURCE.read_text(encoding="utf-8")
    start = source.index("def _build_circuit_breaker_rail")
    end = source.index("\n    def ", start + 1)
    builder = source[start:end]

    missing = [
        field.name
        for field in dataclasses.fields(CircuitBreakerConfig)
        if f'"{field.name}"' not in builder
    ]
    assert not missing, f"never read from config: {missing}"


def test_the_breaker_stays_off_when_the_config_says_so() -> None:
    assert _build_rail({"enabled": False, "identical_repeat_threshold": 2}) is None


# ---------------------------------------------------------------------------
# Reaching installations that already exist
# ---------------------------------------------------------------------------


def test_the_enabled_flip_survives_the_config_merge() -> None:
    """Changing a template default does not reach anyone on its own.

    `migrate_config_from_template` deep-merges the template into the user's
    config and *keeps the user's value* for any key the template already had.
    `circuit_breaker.enabled` has been in the template since 2026-06, so every
    existing config carries an explicit `false` and would keep it -- while the
    new thresholds, being new keys, are added. The result is a config that looks
    configured with the guard switched off, which is worse than either state, and
    it would have left the incident's own host unprotected by this change.

    `test_the_shipped_config_enables_the_breaker` cannot catch this: it asserts
    on the template, which is the half that was never in doubt.
    """
    from jiuwenswarm.common.config import (
        _deep_merge,
        _migrate_circuit_breaker_default_enabled,
    )

    template = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    existing_install = {"execution_guard": {"circuit_breaker": {"enabled": False}}}

    without_migration = _deep_merge(template, dict(existing_install))
    assert (
        without_migration["execution_guard"]["circuit_breaker"]["enabled"] is False
    ), "the merge alone must be shown to preserve the old value, or this proves nothing"

    _migrate_circuit_breaker_default_enabled(existing_install)
    merged = _deep_merge(template, existing_install)
    breaker = merged["execution_guard"]["circuit_breaker"]

    assert breaker["enabled"] is True
    assert breaker["identical_repeat_threshold"] == 3
    assert breaker["identical_repeat_abort_threshold"] == 5


def test_the_migration_leaves_a_deliberate_setting_alone() -> None:
    """A `false` alongside the new thresholds is a decision, not the old default.

    The thresholds ship in the same template change that flipped the default,
    so a config carrying them has already been merged against it: whatever
    `enabled` says survived that merge and was chosen. Without this the
    documented opt-out is undone on the next init, which is worse than never
    having offered one.
    """
    from jiuwenswarm.common.config import _migrate_circuit_breaker_default_enabled

    config = {"execution_guard": {"circuit_breaker": {
        "enabled": False, "identical_repeat_threshold": 7}}}
    _migrate_circuit_breaker_default_enabled(config)

    breaker = config["execution_guard"]["circuit_breaker"]
    assert breaker["enabled"] is False, "a deliberate opt-out must survive an upgrade"
    assert breaker["identical_repeat_threshold"] == 7

    for shape in ({}, {"execution_guard": {}}, {"execution_guard": {"circuit_breaker": {}}}):
        _migrate_circuit_breaker_default_enabled(shape)  # must not raise


def test_a_reaped_pid_is_never_walked_for_descendants() -> None:
    """The one way this teardown can do damage is walking a recycled pid.

    Once the child has been waited on, the kernel may hand its pid to an
    unrelated process, whose children are not ours to kill. The pre-kill
    snapshot is what covers orphaned background jobs; walking afterwards adds
    nothing and must not happen.
    """
    from jiuwenswarm.agents.harness.common.tools import command_tools

    walked: list[int | None] = []

    def _record(pid):
        walked.append(pid)
        return [999999]

    killed: list[int] = []

    with mock.patch.object(command_tools, "collect_process_descendant_pids", _record), \
            mock.patch.object(command_tools, "kill_process_pids",
                              lambda pids: killed.extend(pids)), \
            mock.patch.object(command_tools, "terminate_shell_process", lambda proc: True):
        reaped = SimpleNamespace(pid=4321, returncode=0)
        command_tools._terminate_command_tree(reaped)

    assert walked == [], "a reaped pid must never be enumerated"
    assert killed == []


# ---------------------------------------------------------------------------
# The incident, through the real tool
# ---------------------------------------------------------------------------


async def _run_and_feed(rail: CircuitBreakerRail, command: str, times: int) -> list[_Ctx]:
    """Actually run the command, and give the real result to the real rail."""
    seen: list[_Ctx] = []
    for _ in range(times):
        raw = await command_tools.mcp_exec_command.invoke(
            {"command": command, "timeout_seconds": 30}
        )
        ctx = _Ctx(command, raw)
        await rail.after_tool_call(ctx)
        seen.append(ctx)
    return seen


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="uses a POSIX shell")
async def test_a_real_repeating_failure_is_counted_and_then_cut() -> None:
    """The incident's shape, from subprocess to abort, with nothing stubbed."""
    rail = CircuitBreakerRail(
        CircuitBreakerConfig(
            identical_repeat_threshold=3, identical_repeat_abort_threshold=5
        ),
        language="en",
    )
    contexts = await _run_and_feed(rail, FAILING_COMMAND, times=5)

    # The tool's own report must be legible to the guards, or none of the rest
    # can happen -- this is the seam the incident fell through.
    payload = json.loads(contexts[0].inputs.tool_result)
    assert payload["exit_code"] == 128
    assert "Out of memory" in payload["stderr"]
    assert ToolResultErrorDetector.has_error(contexts[0].inputs.tool_result) is True

    # Nothing is written back to the model at any point -- see
    # test_the_warning_stage_appends_nothing_to_the_model_context for why that is
    # the intended behaviour rather than an omission.
    assert all(ctx.steering == [] for ctx in contexts)

    assert all(ctx.force_finish is None for ctx in contexts[:4])
    assert contexts[4].force_finish is not None, "the fifth must end it"
    assert contexts[4].force_finish["result_type"] == "error"


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="uses a POSIX shell")
async def test_a_real_command_that_makes_progress_is_never_cut() -> None:
    """Identical arguments returning new output each time is still work.

    Without this, the guard that fixes the incident becomes a new way to kill
    healthy runs -- polling, tailing, watching a counter.
    """
    rail = CircuitBreakerRail(
        CircuitBreakerConfig(
            identical_repeat_threshold=3, identical_repeat_abort_threshold=5
        ),
        language="en",
    )
    changing = "python3 -c \"import time; print(time.time_ns())\""
    contexts = await _run_and_feed(rail, changing, times=6)

    assert all(ctx.force_finish is None for ctx in contexts)
    assert all(not ctx.steering for ctx in contexts)
