"""Tests for the AgentServer/Gateway supervisor used by ``jiuwenswarm.app``."""

from __future__ import annotations

import json
import importlib
import signal
import time
from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest

from jiuwenswarm.common import process_supervision
from jiuwenswarm.common.process_supervision import (
    CRASH_EXIT_CODE,
    GATEWAY_RESTART_EXIT_CODE,
    is_crash_exit,
    supervise,
)

_COMMANDS = {"agentserver": ["agentserver"], "gateway": ["gateway"]}


@pytest.mark.parametrize(
    "keep_worker,worker_ok,expected,gateway_config",
    [
        (False, True, 0, "managed"),
        (False, False, 1, "fallback"),
        (True, False, 0, "explicit"),
    ],
)
def test_application_final_exit_owns_worker_unless_launcher_delegates(
    tmp_path,
    monkeypatch,
    keep_worker,
    worker_ok,
    expected,
    gateway_config,
):
    import sys
    from jiuwenswarm.common import paths, utils
    from jiuwenswarm.server.supervisor import shutdown

    monkeypatch.setattr(paths, "get_user_workspace_dir", lambda: tmp_path)
    monkeypatch.setattr(paths, "get_config_dir", lambda: tmp_path / "config")
    monkeypatch.setattr(utils, "get_config_dir", lambda: tmp_path / "config")
    monkeypatch.setattr(utils, "is_agentos_runtime", lambda: False)
    monkeypatch.delenv("JIUWENSWARM_GATEWAY_CONFIG_DIR", raising=False)
    monkeypatch.setattr(utils, "cleanup_stale_openjiuwen_descs", lambda: None)
    monkeypatch.setattr(utils, "prepare_runtime_workspace", lambda **kwargs: None)
    monkeypatch.setattr(utils, "ensure_gateway_config", lambda **kwargs: None)
    monkeypatch.setattr(sys, "argv", ["jiuwenswarm-app"] + (["--keep-worker"] if keep_worker else []))
    app = importlib.import_module("jiuwenswarm.app")
    order = []
    supervised = []

    def supervise_application(_commands, popen_kwargs, **kwargs):
        supervised.append((popen_kwargs, kwargs))
        order.append("fronts-stopped")
        return 0

    monkeypatch.setattr(app, "supervise", supervise_application)
    expected_dir = tmp_path / "config"
    if gateway_config == "managed":
        expected_dir.mkdir(parents=True, exist_ok=True)
        (expected_dir / "config-gateway.yaml").write_text("extensions: {}\n", encoding="utf-8")
    if gateway_config == "explicit":
        expected_dir = tmp_path / "explicit-gateway"
        monkeypatch.setenv("JIUWENSWARM_GATEWAY_CONFIG_DIR", str(expected_dir))
    monkeypatch.setattr(app, "begin_system_plugin_recovery_session", lambda: None)
    monkeypatch.setattr(app.subprocess, "Popen", lambda *args, **kwargs: SimpleNamespace(poll=lambda: 0))
    monkeypatch.setattr(app.signal, "signal", lambda *args: None)
    monkeypatch.setattr(app, "install_async_dump_handler", lambda *args: None)
    monkeypatch.setattr(shutdown, "shutdown_application_worker",
                        lambda **kwargs: order.append("worker-stopped") or worker_ok, raising=False)
    with pytest.raises(SystemExit) as result:
        app.main()
    assert result.value.code == expected
    assert order == (["fronts-stopped"] if keep_worker else ["fronts-stopped", "worker-stopped"])
    _popen_kwargs, kwargs = supervised[0]
    agent_env = kwargs["command_popen_kwargs"]["agentserver"]["env"]
    assert "JIUWENSWARM_GATEWAY_CONFIG_DIR" not in agent_env
    gateway_env = kwargs["command_popen_kwargs"]["gateway"]["env"]
    assert gateway_env["JIUWENSWARM_GATEWAY_CONFIG_DIR"] == str(expected_dir)


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.interrupt_at: float | None = None

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        if self.interrupt_at is not None and self.now >= self.interrupt_at:
            raise KeyboardInterrupt


class _FakeProc:
    _next_pid = 1000
    _instances = {}

    def __init__(self, clock: _Clock, exit_at: float | None, code: int) -> None:
        _FakeProc._next_pid += 1
        self.pid = _FakeProc._next_pid
        _FakeProc._instances[self.pid] = self
        self._clock = clock
        self._exit_at = exit_at
        self._code = code
        self.returncode: int | None = None
        self.terminated = False

    def poll(self) -> int | None:
        if self.returncode is None and self._exit_at is not None and self._clock.now >= self._exit_at:
            self.returncode = self._code
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        if self.returncode is None:
            self.returncode = -15

    def kill(self) -> None:
        if self.returncode is None:
            self.returncode = -9


@pytest.fixture
def clock(monkeypatch) -> _Clock:
    clock = _Clock()
    monkeypatch.setattr(
        process_supervision,
        "time",
        SimpleNamespace(monotonic=clock.monotonic, sleep=clock.sleep, time=time.time),
    )
    monkeypatch.setattr(process_supervision, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(process_supervision, "_upgrade_pending_for", lambda pid: False)
    return clock


@pytest.fixture(autouse=True)
def breadcrumb_file(monkeypatch, tmp_path):
    """Keep exit breadcrumbs out of the real logs dir."""
    path = tmp_path / "service_exits.jsonl"
    monkeypatch.setattr(process_supervision, "_breadcrumb_path", lambda: path)
    _FakeProc._instances.clear()
    # Platform branches emulate Linux even when pytest itself runs on Windows.
    monkeypatch.setattr(process_supervision, "_POSIX_CRASH_SIGNALS", frozenset((4, 6, 7, 8, 11)))
    monkeypatch.setattr(process_supervision.signal, "SIGKILL", process_supervision._SIGKILL, raising=False)

    def stop_fake_tree(pid, *, timeout):
        assert pid in _FakeProc._instances, "Unit tests must never signal a real PID"
        _FakeProc._instances[pid].terminate()

    monkeypatch.setattr(process_supervision, "terminate_service_tree", stop_fake_tree)
    return path


def _script_children(monkeypatch, clock: _Clock, plans: dict[str, list[tuple[float | None, int]]]):
    """Each spawn of a child takes its next ``(seconds until exit, exit code)``."""
    spawned: list[tuple[str, _FakeProc]] = []

    def fake_popen(command, **_kwargs):
        name = command[0]
        exit_after, code = plans[name].pop(0)
        proc = _FakeProc(clock, None if exit_after is None else clock.now + exit_after, code)
        spawned.append((name, proc))
        return proc

    monkeypatch.setattr(process_supervision, "subprocess", SimpleNamespace(Popen=fake_popen))
    return spawned


def test_command_specific_popen_kwargs_override_only_selected_child(monkeypatch, clock):
    seen = {}

    def fake_popen(command, **kwargs):
        name = command[0]
        seen[name] = kwargs
        return _FakeProc(clock, None if name == "gateway" else clock.now + 100, 0)

    monkeypatch.setattr(process_supervision, "subprocess", SimpleNamespace(Popen=fake_popen))

    assert supervise(
        _COMMANDS,
        {"env": {"CHILD_ENV": "base"}},
        command_popen_kwargs={"gateway": {"env": {"CHILD_ENV": "gateway"}}},
    ) == 0
    assert seen["agentserver"]["env"] == {"CHILD_ENV": "base"}
    assert seen["gateway"]["env"] == {"CHILD_ENV": "gateway"}


def test_gateway_restart_request_respawns_only_the_gateway(monkeypatch, clock):
    spawned = _script_children(
        monkeypatch,
        clock,
        {"agentserver": [(100, 0)], "gateway": [(10, GATEWAY_RESTART_EXIT_CODE), (None, 0)]},
    )

    assert supervise(_COMMANDS, {}) == 0

    assert [name for name, _ in spawned] == ["agentserver", "gateway", "gateway"]
    assert spawned[2][1].terminated


def test_crashed_child_is_respawned_until_the_budget_runs_out(monkeypatch, clock):
    spawned = _script_children(
        monkeypatch,
        clock,
        {"agentserver": [(60, 1)] * 4, "gateway": [(None, 0)]},
    )

    assert supervise(_COMMANDS, {}) == 1

    assert [name for name, _ in spawned].count("agentserver") == 4
    gateway = next(proc for name, proc in spawned if name == "gateway")
    assert gateway.terminated


@pytest.mark.parametrize("stop_code", [0, -15, -2])
def test_stop_path_exits_tear_the_service_down(monkeypatch, clock, stop_code):
    spawned = _script_children(
        monkeypatch,
        clock,
        {"agentserver": [(None, 0)], "gateway": [(40, stop_code)]},
    )

    assert supervise(_COMMANDS, {}) == stop_code

    assert [name for name, _ in spawned] == ["agentserver", "gateway"]
    assert spawned[0][1].terminated


def test_sigkilled_child_is_respawned(monkeypatch, clock):
    # A SIGKILL is ambiguous (stop paths escalate to it after SIGTERM), but the
    # OOM killer / infra hard kills read the same way; treating it as a stop
    # path tore the whole service down (issue #4946). It must respawn like a
    # crash, under the same uptime/budget gates.
    spawned = _script_children(
        monkeypatch,
        clock,
        {"agentserver": [(100, 0)], "gateway": [(60, -9), (None, 0)]},
    )

    assert supervise(_COMMANDS, {}) == 0

    assert [name for name, _ in spawned] == ["agentserver", "gateway", "gateway"]
    assert spawned[2][1].terminated  # the respawned gateway, on final teardown


def test_sigkilled_child_below_min_uptime_is_not_respawned(monkeypatch, clock):
    # A hard kill during startup (port taken, lock held) would only repeat; the
    # uptime gate keeps it a one-shot teardown.
    spawned = _script_children(
        monkeypatch,
        clock,
        {"agentserver": [(None, 0)], "gateway": [(5, -9)]},
    )

    assert supervise(_COMMANDS, {}) == -9

    assert len(spawned) == 2


def test_startup_failures_are_not_respawned(monkeypatch, clock):
    spawned = _script_children(
        monkeypatch,
        clock,
        {"agentserver": [(5, 1)], "gateway": [(None, 0)]},
    )

    assert supervise(_COMMANDS, {}) == 1
    assert len(spawned) == 2


def test_upgrade_exit_is_not_respawned(monkeypatch, clock):
    spawned = _script_children(
        monkeypatch,
        clock,
        {"agentserver": [(None, 0)], "gateway": [(60, 1)]},
    )
    gateway_pids: list[int] = []
    monkeypatch.setattr(
        process_supervision,
        "_upgrade_pending_for",
        lambda pid: pid in gateway_pids,
    )
    original_popen = process_supervision.subprocess.Popen

    def recording_popen(command, **kwargs):
        proc = original_popen(command, **kwargs)
        if command[0] == "gateway":
            gateway_pids.append(proc.pid)
        return proc

    monkeypatch.setattr(process_supervision, "subprocess", SimpleNamespace(Popen=recording_popen))

    assert supervise(_COMMANDS, {}) == 1
    assert len(spawned) == 2


def test_keyboard_interrupt_returns_130_and_terminates_children(monkeypatch, clock):
    spawned = _script_children(
        monkeypatch,
        clock,
        {"agentserver": [(None, 0)], "gateway": [(None, 0)]},
    )
    clock.interrupt_at = 5.0

    assert supervise(_COMMANDS, {}) == 130
    assert all(proc.terminated for _, proc in spawned)


def test_requested_restart_restarts_only_selected_children(monkeypatch, clock):
    spawned = _script_children(
        monkeypatch, clock,
        {"recovery": [(None, 0)], "agentserver": [(None, 0), (None, 0)], "gateway": [(None, 0), (None, 0)]},
    )
    requested = True
    order = []

    def stop(pid, *, timeout):
        del timeout
        name, proc = next(item for item in spawned if item[1].pid == pid)
        order.append("stop-" + name)
        proc.terminate()

    monkeypatch.setattr(process_supervision, "terminate_service_tree", stop)

    def before_restart():
        assert len(spawned) == 3
        assert spawned[0][1].poll() is None
        assert spawned[1][1].poll() is not None and spawned[2][1].poll() is not None
        order.append("worker-retired")

    def restart_requested():
        nonlocal requested
        result, requested = requested, False
        return result

    clock.interrupt_at = 1.0
    assert supervise(
        {"recovery": ["recovery"], **_COMMANDS}, {},
        restart_requested=restart_requested, restart_names=("gateway", "agentserver"), before_restart=before_restart,
    ) == 130
    assert [name for name, _ in spawned] == ["recovery", "agentserver", "gateway", "agentserver", "gateway"]
    assert order[:3] == ["stop-gateway", "stop-agentserver", "worker-retired"]


def test_recovery_worker_failure_prevents_replacement_start(monkeypatch, clock):
    spawned = _script_children(monkeypatch, clock, {"agentserver": [(None, 0)], "gateway": [(None, 0)]})

    def refuse_worker():
        assert all(proc.poll() is not None for _, proc in spawned)
        raise RuntimeError("Worker retirement incomplete")

    with pytest.raises(RuntimeError, match="Worker retirement incomplete"):
        supervise(_COMMANDS, {}, restart_requested=lambda: True,
                  restart_names=("gateway", "agentserver"), before_restart=refuse_worker)
    assert [name for name, _ in spawned] == ["agentserver", "gateway"]


def test_recovery_service_failure_never_retires_worker_or_starts_replacements(monkeypatch, clock):
    spawned = _script_children(monkeypatch, clock, {"agentserver": [(None, 0)], "gateway": [(None, 0)]})
    failures = [True]
    workers = []

    def stop(pid, *, timeout):
        del timeout
        if failures:
            failures.pop()
            raise RuntimeError("Service exit unconfirmed")
        _FakeProc._instances[pid].terminate()

    monkeypatch.setattr(process_supervision, "terminate_service_tree", stop)
    with pytest.raises(RuntimeError, match="Application services did not completely stop"):
        supervise(_COMMANDS, {}, restart_requested=lambda: True,
                  restart_names=("gateway", "agentserver"), before_restart=lambda: workers.append(True))
    assert len(spawned) == 2 and workers == []


def test_partial_recovery_start_retires_the_replacement_worker_after_front_cleanup(monkeypatch, clock):
    spawned = _script_children(monkeypatch, clock,
                               {"agentserver": [(None, 0), (None, 0)], "gateway": [(None, 0)]})
    original = process_supervision.subprocess.Popen
    gateway_starts = []
    retirements = []

    def spawn(command, **kwargs):
        if command[0] == "gateway":
            gateway_starts.append(True)
            if len(gateway_starts) > 1:
                raise OSError("replacement Gateway failed")
        return original(command, **kwargs)

    def retire():
        assert all(proc.poll() is not None for _, proc in spawned)
        retirements.append(True)

    monkeypatch.setattr(process_supervision, "subprocess", SimpleNamespace(Popen=spawn))
    with pytest.raises(OSError, match="replacement Gateway failed"):
        supervise(_COMMANDS, {}, restart_requested=lambda: True,
                  restart_names=("gateway", "agentserver"), before_restart=retire)
    assert [name for name, _ in spawned] == ["agentserver", "gateway", "agentserver"]
    assert len(retirements) == 2


def test_unmatched_restart_request_keeps_the_supervisor_polling(monkeypatch, clock):
    spawned = _script_children(
        monkeypatch,
        clock,
        {"agentserver": [(None, 0)], "gateway": [(None, 0)]},
    )
    requested = True

    def restart_requested() -> bool:
        nonlocal requested
        if not requested:
            return False
        requested = False
        return True

    clock.interrupt_at = 0.5
    assert supervise(
        _COMMANDS,
        {},
        restart_requested=restart_requested,
        restart_names=frozenset({"missing"}),
    ) == 130

    assert [name for name, _ in spawned] == ["agentserver", "gateway"]
    assert clock.now >= process_supervision._POLL_INTERVAL


@pytest.mark.parametrize(
    ("platform", "returncode", "expected"),
    [
        ("linux", 1, True),
        ("linux", -11, True),
        ("linux", -6, True),
        ("linux", 0, False),
        ("linux", 2, False),
        ("linux", -15, False),
        ("linux", -9, False),
        ("linux", 130, False),
        ("linux", GATEWAY_RESTART_EXIT_CODE, False),
        ("win32", 0xC0000005, True),
        ("win32", 0xC0000409, True),
        ("win32", 1, False),
        ("win32", 15, False),
        ("win32", 0xC000013A, False),
        # The crash handler's dedicated code must read as a crash on Windows,
        # where a plain unhandled-exception exit (1) is indistinguishable
        # from taskkill /F.
        ("win32", CRASH_EXIT_CODE, True),
        ("linux", CRASH_EXIT_CODE, True),
    ],
)
def test_only_crash_exits_count_as_crashes(monkeypatch, platform, returncode, expected):
    monkeypatch.setattr(process_supervision, "sys", SimpleNamespace(platform=platform))

    assert is_crash_exit(returncode) is expected


def test_upgrade_marker_matches_the_exiting_pid(monkeypatch, tmp_path):
    from jiuwenswarm.common import upgrade_executor

    marker = tmp_path / ".restart_pending.json"
    monkeypatch.setattr(upgrade_executor, "restart_pending_path", lambda: marker)

    assert process_supervision._upgrade_pending_for(4321) is False
    marker.write_text(json.dumps({"pid": 4321, "timestamp": time.time()}), encoding="utf-8")
    assert process_supervision._upgrade_pending_for(4321) is True
    assert process_supervision._upgrade_pending_for(1234) is False


def test_upgrade_marker_expires_and_requires_a_timestamp(monkeypatch, tmp_path):
    # A marker left behind by a failed upgrade is never cleaned up, and Windows
    # recycles PIDs fast: a stale match must not suppress respawns forever.
    from jiuwenswarm.common import upgrade_executor

    marker = tmp_path / ".restart_pending.json"
    monkeypatch.setattr(upgrade_executor, "restart_pending_path", lambda: marker)

    stale = time.time() - process_supervision._UPGRADE_MARKER_TTL - 1
    marker.write_text(json.dumps({"pid": 4321, "timestamp": stale}), encoding="utf-8")
    assert process_supervision._upgrade_pending_for(4321) is False
    marker.write_text(json.dumps({"pid": 4321}), encoding="utf-8")
    assert process_supervision._upgrade_pending_for(4321) is False


def test_restart_request_yields_to_a_pending_upgrade(monkeypatch, clock):
    # Exit 75 during the upgrade window: the helper is about to relaunch the
    # full stack, so respawning here would race it into a duplicate instance.
    spawned = _script_children(
        monkeypatch,
        clock,
        {"agentserver": [(None, 0)], "gateway": [(60, GATEWAY_RESTART_EXIT_CODE)]},
    )
    gateway_pids: list[int] = []
    original_popen = process_supervision.subprocess.Popen

    def recording_popen(command, **kwargs):
        proc = original_popen(command, **kwargs)
        if command[0] == "gateway":
            gateway_pids.append(proc.pid)
        return proc

    monkeypatch.setattr(process_supervision, "subprocess", SimpleNamespace(Popen=recording_popen))
    monkeypatch.setattr(process_supervision, "_upgrade_pending_for", lambda pid: pid in gateway_pids)

    assert supervise(_COMMANDS, {}) == GATEWAY_RESTART_EXIT_CODE
    assert sum(1 for name, _ in spawned if name == "gateway") == 1
    assert spawned[0][1].terminated


def test_posix_respawn_kills_the_dead_childs_process_group(monkeypatch, clock):
    # A signal-killed child leaves its own subprocesses behind; the replacement
    # would fail on the ports/locks they still hold.
    spawned = _script_children(
        monkeypatch,
        clock,
        {"agentserver": [(40, -signal.SIGSEGV), (60, 0)], "gateway": [(None, 0)]},
    )
    killpg_calls: list[tuple[int, int]] = []
    monkeypatch.setattr(
        process_supervision,
        "os",
        SimpleNamespace(killpg=lambda pgid, sig: killpg_calls.append((pgid, sig))),
    )

    assert supervise(_COMMANDS, {"start_new_session": True}) == 0

    dead_pid = spawned[0][1].pid
    assert killpg_calls == [(dead_pid, process_supervision._SIGKILL)]


def test_posix_respawn_without_own_session_skips_killpg(monkeypatch, clock):
    # Without start_new_session the child shares OUR process group; killpg on
    # its pid would be meaningless at best and must not be attempted.
    _script_children(
        monkeypatch,
        clock,
        {"agentserver": [(40, -signal.SIGSEGV), (60, 0)], "gateway": [(None, 0)]},
    )
    killpg_calls: list[tuple[int, int]] = []
    monkeypatch.setattr(
        process_supervision,
        "os",
        SimpleNamespace(killpg=lambda pgid, sig: killpg_calls.append((pgid, sig))),
    )

    assert supervise(_COMMANDS, {}) == 0
    assert killpg_calls == []


def test_win32_respawn_kills_verified_descendants_only(monkeypatch, clock):
    monkeypatch.setattr(process_supervision, "sys", SimpleNamespace(platform="win32"))
    _script_children(
        monkeypatch,
        clock,
        {"agentserver": [(40, 0xC0000005), (50, 0)], "gateway": [(None, 0)]},
    )

    killed: list[int] = []

    class _Victim:
        def __init__(self, pid: int) -> None:
            self.pid = pid

        def create_time(self) -> float:
            return _Dead.create_time_value

        def kill(self) -> None:
            killed.append(self.pid)

    class _Dead:
        create_time_value = 111.0

        def __init__(self, pid: int) -> None:
            self.pid = pid

        def create_time(self) -> float:
            return _Dead.create_time_value

        def children(self, recursive: bool = False) -> list[_Victim]:
            assert recursive
            return [_Victim(201), _Victim(202)]

    monkeypatch.setattr(psutil, "Process", _Dead)

    assert supervise(_COMMANDS, {}) == 0
    # Deepest first; only for the crash respawn, not for the clean exit-0 stop.
    assert killed == [202, 201]


def test_win32_respawn_skips_a_recycled_pid(monkeypatch, clock):
    monkeypatch.setattr(process_supervision, "sys", SimpleNamespace(platform="win32"))
    _script_children(
        monkeypatch,
        clock,
        {"agentserver": [(40, 0xC0000005), (50, 0)], "gateway": [(None, 0)]},
    )

    killed: list[int] = []
    real_create_time = psutil.Process().create_time()

    class _Recycled:
        def __init__(self, pid: int) -> None:
            self.pid = pid

        def create_time(self) -> float:
            # Both the spawn-time record and the reap-time lookup see the same
            # stranger, but the supervisor recorded a DIFFERENT create_time at
            # spawn; simulate that by shifting the recorded value.
            return real_create_time

        def children(self, recursive: bool = False) -> list:
            raise AssertionError("must not enumerate a recycled PID's children")

    monkeypatch.setattr(psutil, "Process", _Recycled)
    monkeypatch.setattr(process_supervision, "_process_create_time", lambda pid: real_create_time - 100.0)

    assert supervise(_COMMANDS, {}) == 0
    assert killed == []


def test_crash_exit_code_respawns_on_windows(monkeypatch, clock):
    # A silent exit-1 death on Windows is indistinguishable from taskkill /F;
    # the crash handler's dedicated exit code must trigger a respawn instead.
    monkeypatch.setattr(process_supervision, "sys", SimpleNamespace(platform="win32"))
    # Keep the win32 reap branch away from the real psutil: fake PIDs could
    # collide with live processes on the test machine.
    monkeypatch.setattr(process_supervision, "_process_create_time", lambda pid: None)
    spawned = _script_children(
        monkeypatch,
        clock,
        {"agentserver": [(40, CRASH_EXIT_CODE), (50, 0)], "gateway": [(None, 0)]},
    )

    assert supervise(_COMMANDS, {}) == 0
    assert [name for name, _ in spawned].count("agentserver") == 2


def test_exit_breadcrumbs_record_the_supervisor_decisions(monkeypatch, clock, breadcrumb_file):
    _script_children(
        monkeypatch,
        clock,
        {"agentserver": [(40, -signal.SIGSEGV), (60, 0)], "gateway": [(None, 0)]},
    )

    assert supervise(_COMMANDS, {}) == 0

    events = [
        json.loads(line) for line in breadcrumb_file.read_text(encoding="utf-8").splitlines()
    ]
    assert [(event["service"], event["action"]) for event in events] == [
        ("agentserver", "respawn"),
        ("agentserver", "teardown"),
    ]
    assert events[0]["returncode"] == -signal.SIGSEGV
    assert events[0]["uptime_seconds"] == 40.0
    assert events[0]["pid"]
    assert events[0]["time"]
