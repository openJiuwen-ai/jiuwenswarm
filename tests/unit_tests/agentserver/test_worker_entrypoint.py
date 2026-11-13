<<<<<<< HEAD
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Unit tests for the Runtime Worker entrypoint startup wiring."""

from __future__ import annotations

import asyncio
import signal
import sys
from pathlib import Path

import pytest

import jiuwenswarm.common.external_cli_runtime as external_cli_runtime
from jiuwenswarm.server.worker import main as worker_main


@pytest.fixture
def restore_process_state():
    """Snapshot and restore what in-process ``main()`` runs mutate globally.

    ``main()`` installs SIGINT/SIGTERM handlers on its own event loop and
    leaves that loop as the thread's current (closed) loop after returning;
    without restoration, later tests in the same process would inherit them.
    """
    policy = asyncio.get_event_loop_policy()
    try:
        previous_loop = policy.get_event_loop()
    except RuntimeError:
        previous_loop = None
    previous_handlers = {
        sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)
    }
    yield
    for sig, handler in previous_handlers.items():
        signal.signal(sig, handler)
    asyncio.set_event_loop(previous_loop)


def _patch_scaffolding(monkeypatch: pytest.MonkeyPatch) -> None:
    """Silence the entrypoint's logging and diagnostics side effects."""
    from jiuwenswarm.server.worker import diagnostics

    monkeypatch.setattr(
        sys,
        "argv",
        ["jiuwenswarm-runtime-worker", "--socket", "unused-ipc.sock"],
    )
    monkeypatch.setattr(diagnostics, "enable_worker_diagnostics", lambda: None)


def test_worker_main_activates_external_cli_paths_before_runtime_load(
    monkeypatch: pytest.MonkeyPatch,
    restore_process_state: None,
) -> None:
    """Team members import their CLI SDK in the worker; paths must be live first."""
    import jiuwenswarm.server.worker.service as worker_service

    order: list[str] = []

    async def _record_serve(*args: object, **kwargs: object) -> None:
        order.append("serve")

    _patch_scaffolding(monkeypatch)
    monkeypatch.setattr(
        external_cli_runtime,
        "activate_external_cli_runtime_paths",
        lambda: order.append("activate"),
    )
    monkeypatch.setattr(worker_service, "serve_worker", _record_serve)

    worker_main.main()

    assert order == ["activate", "serve"]


def test_worker_main_survives_activation_failure(
    monkeypatch: pytest.MonkeyPatch,
    restore_process_state: None,
) -> None:
    """An optional SDK problem must not take the whole worker down."""
    import jiuwenswarm.server.worker.service as worker_service

    order: list[str] = []

    async def _record_serve(*args: object, **kwargs: object) -> None:
        order.append("serve")

    def _raise_activation() -> None:
        order.append("activate")
        raise RuntimeError("broken managed runtime layout")

    _patch_scaffolding(monkeypatch)
    monkeypatch.setattr(external_cli_runtime, "activate_external_cli_runtime_paths", _raise_activation)
    monkeypatch.setattr(worker_service, "serve_worker", _record_serve)

    worker_main.main()

    assert order == ["activate", "serve"]


def test_worker_stub_mode_skips_external_cli_activation(
    monkeypatch: pytest.MonkeyPatch,
    restore_process_state: None,
) -> None:
    """The stub speaks IPC without loading the Runtime, so it skips activation."""
    from jiuwenswarm.server.worker import stub as worker_stub

    order: list[str] = []

    async def _record_stub(*args: object, **kwargs: object) -> None:
        order.append("stub")

    _patch_scaffolding(monkeypatch)
    monkeypatch.setattr(
        sys,
        "argv",
        ["jiuwenswarm-runtime-worker", "--socket", "unused-ipc.sock", "--stub"],
    )
    monkeypatch.setattr(
        external_cli_runtime,
        "activate_external_cli_runtime_paths",
        lambda: order.append("activate"),
    )
    monkeypatch.setattr(worker_stub, "serve_stub", _record_stub)

    worker_main.main()

    assert order == ["stub"]


def test_worker_entry_activation_appends_managed_site_packages_when_frozen(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Simulate the frozen app: the managed SDK dirs join sys.path on startup."""
    managed_root = tmp_path / "external-cli"

    def _site_packages(cli_agent: str) -> Path:
        return managed_root / cli_agent / "site-packages"

    monkeypatch.setattr(external_cli_runtime, "_is_frozen_windows", lambda: True)
    monkeypatch.setattr(external_cli_runtime, "_is_frozen_macos", lambda: False)
    monkeypatch.setattr(external_cli_runtime, "_runtime_site_packages", _site_packages)
    # The upgrade-preservation check passes: the runtime is installed & current.
    monkeypatch.setattr(
        external_cli_runtime,
        "get_external_cli_runtime_status",
        lambda cli_agent: {"runtime_state": "current"},
    )
    monkeypatch.setattr(external_cli_runtime.sys, "path", list(sys.path))

    worker_main._activate_managed_external_cli_paths()

    cli_agents = external_cli_runtime.external_cli_runtime_names()
    assert external_cli_runtime.sys.path[-len(cli_agents) :] == [
        str(_site_packages(cli_agent)) for cli_agent in cli_agents
    ]
||||||| e487e9a30
=======
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Unit tests for the Runtime Worker entrypoint startup wiring."""

from __future__ import annotations

import asyncio
import signal
import sys
from pathlib import Path

import pytest

import jiuwenswarm.common.external_cli_runtime as external_cli_runtime
from jiuwenswarm.server.worker import main as worker_main


@pytest.fixture
def restore_process_state():
    """Snapshot and restore what in-process ``main()`` runs mutate globally.

    ``main()`` installs SIGINT/SIGTERM handlers and owns an event loop.
    Restore the caller's globals so later tests do not inherit these changes.
    """
    policy = asyncio.get_event_loop_policy()
    try:
        previous_loop = policy.get_event_loop()
    except RuntimeError:
        # A previous asyncio.run() (any earlier test in this xdist worker)
        # leaves the thread with no current loop; capturing None here keeps
        # teardown's set_event_loop(previous_loop) restoring that exact state
        # instead of blowing up the same way get_event_loop() just did.
        previous_loop = None
    previous_handlers = {
        sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)
    }
    yield
    for sig, handler in previous_handlers.items():
        signal.signal(sig, handler)
    asyncio.set_event_loop(previous_loop)


def _patch_scaffolding(monkeypatch: pytest.MonkeyPatch) -> None:
    """Silence the entrypoint's logging and diagnostics side effects."""
    from jiuwenswarm.common import background_logging
    from jiuwenswarm.server.worker import diagnostics

    monkeypatch.setattr(
        sys,
        "argv",
        ["jiuwenswarm-runtime-worker", "--socket", "unused-ipc.sock"],
    )
    monkeypatch.setattr(background_logging, "enable_background_logging", lambda: None)
    monkeypatch.setattr(background_logging, "stop_background_logging", lambda *args, **kwargs: True)
    monkeypatch.setattr(diagnostics, "enable_worker_diagnostics", lambda: None)


def test_worker_main_activates_external_cli_paths_before_runtime_load(
    monkeypatch: pytest.MonkeyPatch,
    restore_process_state: None,
) -> None:
    """Team members import their CLI SDK in the worker; paths must be live first."""
    import jiuwenswarm.server.worker.service as worker_service

    order: list[str] = []

    async def _record_serve(*args: object, **kwargs: object) -> None:
        order.append("serve")

    _patch_scaffolding(monkeypatch)
    monkeypatch.setattr(
        external_cli_runtime,
        "activate_external_cli_runtime_paths",
        lambda: order.append("activate"),
    )
    monkeypatch.setattr(worker_service, "serve_worker", _record_serve)
    previous_handler = signal.getsignal(signal.SIGINT)

    worker_main.main()

    assert order == ["activate", "serve"]
    assert signal.getsignal(signal.SIGINT) == previous_handler


def test_worker_main_survives_activation_failure(
    monkeypatch: pytest.MonkeyPatch,
    restore_process_state: None,
) -> None:
    """An optional SDK problem must not take the whole worker down."""
    import jiuwenswarm.server.worker.service as worker_service

    order: list[str] = []

    async def _record_serve(*args: object, **kwargs: object) -> None:
        order.append("serve")

    def _raise_activation() -> None:
        order.append("activate")
        raise RuntimeError("broken managed runtime layout")

    _patch_scaffolding(monkeypatch)
    monkeypatch.setattr(external_cli_runtime, "activate_external_cli_runtime_paths", _raise_activation)
    monkeypatch.setattr(worker_service, "serve_worker", _record_serve)

    worker_main.main()

    assert order == ["activate", "serve"]


@pytest.mark.parametrize("repeat_signal", [False, True])
def test_windows_console_interrupt_drains_runtime_and_sdk_tasks(
    monkeypatch: pytest.MonkeyPatch,
    restore_process_state: None,
    repeat_signal: bool,
) -> None:
    """A real SIGINT must finish cleanup even without loop signal support."""
    import jiuwenswarm.server.worker.service as worker_service

    _patch_scaffolding(monkeypatch)
    monkeypatch.setattr(worker_main, "_activate_managed_external_cli_paths", lambda: None)
    loop = asyncio.new_event_loop()
    runner_type = asyncio.Runner
    previous_handler = signal.getsignal(signal.SIGINT)
    order = []
    background_tasks = []

    def unsupported(*_args):
        raise NotImplementedError

    def create_runner():
        runner = runner_type(loop_factory=lambda: loop)
        close = runner.close

        def close_and_deliver_late_signal():
            close()
            signal.raise_signal(signal.SIGINT)

        runner.close = close_and_deliver_late_signal
        return runner

    monkeypatch.setattr(loop, "add_signal_handler", unsupported)
    monkeypatch.setattr(asyncio, "Runner", create_runner)

    async def sdk_background():
        try:
            await asyncio.Future()
        finally:
            await asyncio.sleep(0.01)
            order.append("sdk_stopped")

    async def serve(*_args, **_kwargs):
        # asyncio only keeps weak Task references. Own this SDK fixture until
        # Runner.close can cancel it, including when CI runs a GC cycle.
        background_tasks.append(asyncio.create_task(sdk_background()))
        loop.call_soon(signal.raise_signal, signal.SIGINT)
        try:
            await asyncio.Future()
        finally:
            order.append("runtime_stopping")
            if repeat_signal:
                loop.call_soon(signal.raise_signal, signal.SIGINT)
            await asyncio.sleep(0.02)
            order.append("runtime_stopped")
            import gc

            gc.collect()

    monkeypatch.setattr(worker_service, "serve_worker", serve)
    worker_main.main()

    assert order == ["runtime_stopping", "runtime_stopped", "sdk_stopped"]
    assert all(task.done() for task in background_tasks)
    assert loop.is_closed()
    if sys.platform == "win32":
        assert signal.getsignal(signal.SIGINT) == signal.SIG_IGN
        # The console can deliver another interrupt while interpreter-owned
        # resources finish after main() returns. It must stay harmless too.
        signal.raise_signal(signal.SIGINT)
    else:
        assert signal.getsignal(signal.SIGINT) == previous_handler


def test_worker_stub_mode_skips_external_cli_activation(
    monkeypatch: pytest.MonkeyPatch,
    restore_process_state: None,
) -> None:
    """The stub speaks IPC without loading the Runtime, so it skips activation."""
    from jiuwenswarm.server.worker import stub as worker_stub

    order: list[str] = []

    async def _record_stub(*args: object, **kwargs: object) -> None:
        order.append("stub")

    _patch_scaffolding(monkeypatch)
    monkeypatch.setattr(
        sys,
        "argv",
        ["jiuwenswarm-runtime-worker", "--socket", "unused-ipc.sock", "--stub"],
    )
    monkeypatch.setattr(
        external_cli_runtime,
        "activate_external_cli_runtime_paths",
        lambda: order.append("activate"),
    )
    monkeypatch.setattr(worker_stub, "serve_stub", _record_stub)

    worker_main.main()

    assert order == ["stub"]


def test_worker_entry_activation_appends_managed_site_packages_when_frozen(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Simulate the frozen app: the managed SDK dirs join sys.path on startup."""
    managed_root = tmp_path / "external-cli"

    def _site_packages(cli_agent: str) -> Path:
        return managed_root / cli_agent / "site-packages"

    monkeypatch.setattr(external_cli_runtime, "_is_frozen_windows", lambda: True)
    monkeypatch.setattr(external_cli_runtime, "_is_frozen_macos", lambda: False)
    monkeypatch.setattr(external_cli_runtime, "_runtime_site_packages", _site_packages)
    # The upgrade-preservation check passes: the runtime is installed & current.
    monkeypatch.setattr(
        external_cli_runtime,
        "get_external_cli_runtime_status",
        lambda cli_agent: {"runtime_state": "current"},
    )
    monkeypatch.setattr(external_cli_runtime.sys, "path", list(sys.path))

    worker_main._activate_managed_external_cli_paths()

    cli_agents = external_cli_runtime.external_cli_runtime_names()
    assert external_cli_runtime.sys.path[-len(cli_agents) :] == [
        str(_site_packages(cli_agent)) for cli_agent in cli_agents
    ]
>>>>>>> origin/develop
