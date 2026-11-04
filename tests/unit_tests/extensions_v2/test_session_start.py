# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Activation-scoped initialization through production assembly and Registry."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from openjiuwen.core.common.exception.errors import ValidationError
from openjiuwen.core.events import Event, HandlerLevel, SessionContext

from jiuwenswarm.extensions_v2 import assembly
from jiuwenswarm.extensions_v2.event_types.session_start import SessionStartOutput
from jiuwenswarm.extensions_v2.handler_ops import session_ops
from jiuwenswarm.server.runtime.session.model_selection_store import SESSION_MODEL_INFO_KEY
from .management.support import EMPTY_EXTENSION, write_package


USER_CODE = '''
from dataclasses import asdict, replace
from openjiuwen.core.events import Event
from jiuwenswarm.extensions_v2.event_types.session_start import SessionStartOutput

class Starter:
    def register(self, registrar):
        registrar.register_event_handler("session.start", self.start, {})

    async def start(self, event, ops, next_handler):
        await ops.probe.record({
            "phase": "enter", "input": asdict(event.event_input),
            "package": ops.package_metadata.package_id,
            "session": ops.session_context.session_id,
            "cwd": await ops.session.cwd(), "root": await ops.session.root(),
            "model": await ops.session.model(),
            "history_ops": callable(ops.session.append) and callable(ops.session.messages),
            "statistics_ops": callable(ops.session.turns) and callable(ops.session.usage),
        })
        output = await next_handler(event)
        await ops.probe.record({"phase": "exit", "receipt": asdict(output)})
        return output

def create_extension():
    return Starter()
'''


@pytest.fixture
async def host(tmp_path, monkeypatch):
    monkeypatch.setattr(assembly, "_ASSEMBLY", None)
    monkeypatch.setattr(assembly, "resolve_install_dir", lambda: tmp_path / "installed")
    original_build = assembly.bootstrap.register_handlers
    records = []
    controls = {"on_record": None}

    async def record(ops, value):
        record = instance.manager.registry.runtime.get_active(ops.package_metadata.package_id)
        records.append({"caller": ops.package_metadata.package_id, "activation_generation": record.generation, **value})
        if controls["on_record"] is not None:
            await controls["on_record"](value)

    async def build(event, ops, next_handler):
        builder = await next_handler(event)
        builder.add_namespace("probe")
        builder.add_method("probe", "record", record)
        return builder

    def register(bus, webui, *, file_service=None, compact_service=None, authorization_service=None):
        original_build(bus, webui, file_service=file_service, compact_service=compact_service,
                       authorization_service=authorization_service)
        bus.register_event_handler("ops.build", build, {}, HandlerLevel.BUILTIN)

    monkeypatch.setattr(assembly.bootstrap, "register_handlers", register)
    instance = await assembly.install_extensions()
    assert instance is not None
    value = SimpleNamespace(assembly=instance, manager=instance.manager, bus=instance.bus,
                            records=records, controls=controls, tmp=tmp_path)
    try:
        yield value
    finally:
        controls["on_record"] = None
        await assembly.shutdown_extensions()


def _context(sid="s1"):
    return SessionContext(sid, cwd="D:/project/src", metadata={
        "project_root": "D:/project",
        SESSION_MODEL_INFO_KEY: {"selection": None, "model_name": "active-model", "provider": "OpenAI",
                                 "source": "runtime", "activated": True},
    })


async def _install(host, name="start.a", source=USER_CODE):
    package = write_package(host.tmp / "source", package_id=name, module_source=source)
    reports = await host.manager.install([package])
    assert len(reports) == 1 and reports[0].status == "SUCCESS"
    return package


async def _start(host, sid="s1", request="r1"):
    return await host.manager.ensure_session_started(_context(sid), request_id=request,
                                                      channel_id="web", is_interactive=True, surface="desktop")


def _entered(host):
    return [row for row in host.records if row.get("phase") == "enter"]


@pytest.mark.asyncio
async def test_real_packages_start_once_and_read_bound_ops_with_history_and_usage(host):
    await _install(host, "start.a")
    await _install(host, "start.b")
    receipts = await _start(host)
    assert len(receipts) == 2 and all(value.cwd == "D:/project/src" for value in receipts)
    assert await _start(host, "s2", "reconnect") == ()
    assert [row["caller"] for row in _entered(host)] == ["start.a", "start.b"]
    for row in _entered(host):
        assert row["package"] == row["caller"]
        assert row["session"] == "s1"
        assert row["input"] == {"cwd": "D:/project/src", "surface": "desktop", "isInteractive": True}
        assert row["cwd"] == "D:/project/src" and row["root"] == "D:/project"
        assert row["model"]["model_name"] == "active-model" and row["model"]["source"] == "runtime"
        assert row["history_ops"] and row["statistics_ops"]


@pytest.mark.asyncio
async def test_new_package_does_not_repeat_old_empty_matchers_and_reinstall_is_new(host):
    first = await _install(host)
    await _start(host)
    await _install(host, "start.b")
    assert len(await _start(host, "s2")) == 1
    assert _entered(host)[-1]["caller"] == "start.b"
    assert (await host.manager.uninstall("start.a")).status == "SUCCESS"
    assert (await host.manager.install([first]))[0].status == "SUCCESS"
    receipts = await _start(host)
    assert len(receipts) == 1 and _entered(host)[-1]["activation_generation"] == 1
    assert [row["caller"] for row in _entered(host)] == ["start.a", "start.b", "start.a"]


@pytest.mark.asyncio
async def test_package_scope_keeps_user_match_pattern_and_does_not_replay_a_miss(host):
    terminal_only = USER_CODE.replace('self.start, {})', 'self.start, {"surface": "terminal"})')
    await _install(host, "start.a", terminal_only)
    await _install(host, "start.b")
    assert len(await _start(host)) == 2
    assert [row["caller"] for row in _entered(host)] == ["start.b"]
    assert await host.manager.ensure_session_started(
        _context("s2"), request_id="r2", channel_id="cli", is_interactive=True, surface="terminal",
    ) == ()


@pytest.mark.asyncio
async def test_extension_cannot_supply_host_package_match_key(host):
    source = USER_CODE.replace(
        'self.start, {})', 'self.start, {"__session_start_package_id__": "start.b"})',
    )
    package = write_package(host.tmp / "source", package_id="start.a", module_source=source)
    report = (await host.manager.install([package]))[0]
    assert report.status == "FAILED"
    assert host.manager.active_packages() == ()


@pytest.mark.asyncio
async def test_successful_reload_starts_new_generation_and_failed_reload_keeps_old(host):
    await _install(host)
    await _start(host)
    assert (await host.manager.reload("start.a")).status == "SUCCESS"
    assert len(await _start(host)) == 1
    assert _entered(host)[-1]["activation_generation"] == 2
    root = host.manager.active_packages()[0].root
    (root / "extension.py").write_text("def invalid(:", encoding="utf-8")
    assert (await host.manager.reload("start.a")).status == "FAILED"
    assert await _start(host) == ()
    assert [row["activation_generation"] for row in _entered(host)] == [1, 2]


@pytest.mark.asyncio
async def test_concurrent_first_inputs_wait_for_one_initialization(host):
    await _install(host)
    entered, release = asyncio.Event(), asyncio.Event()

    async def pause(value):
        if value["phase"] == "enter":
            entered.set()
            await release.wait()

    host.controls["on_record"] = pause
    tasks = [asyncio.create_task(_start(host, f"s{i}", f"r{i}")) for i in range(8)]
    await entered.wait()
    await asyncio.sleep(0)
    assert not any(task.done() for task in tasks)
    release.set()
    results = await asyncio.gather(*tasks)
    assert sorted(map(len, results)) == [0] * 7 + [1]
    assert len(_entered(host)) == 1


@pytest.mark.asyncio
async def test_start_holds_inflight_but_not_management_lock(host):
    await _install(host)
    reports = []

    async def query_and_try_uninstall(value):
        if value["phase"] != "enter":
            return
        query = await host.manager.query("start.a")
        # The host gate and SDK handler can each reserve an in-flight use.
        assert query.inflight["start.a"] >= 1
        reports.append(await host.manager.uninstall("start.a"))

    host.controls["on_record"] = query_and_try_uninstall
    await asyncio.wait_for(_start(host), timeout=5)
    assert reports[0].status == "FAILED" and reports[0].error.code == "EXTENSION_PACKAGE_BUSY"
    assert host.manager.registry.runtime.inflight_count("start.a") == 0
    assert (await host.manager.uninstall("start.a")).status == "SUCCESS"


@pytest.mark.asyncio
async def test_reload_during_start_initializes_new_instance_before_gate_returns(host):
    await _install(host)
    changed = False

    async def reload_once(value):
        nonlocal changed
        if value["phase"] == "enter" and not changed:
            changed = True
            assert (await host.manager.reload("start.a")).status == "SUCCESS"

    host.controls["on_record"] = reload_once
    receipts = await asyncio.wait_for(_start(host), timeout=5)
    assert len(receipts) == 2 and [r["activation_generation"] for r in _entered(host)] == [1, 2]
    assert [row["activation_generation"] for row in _entered(host)] == [1, 2]
    assert await _start(host) == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement", [
    'raise RuntimeError("user init failed")',
    'return SessionStartOutput("forged")',
    'return await next_handler(Event(event.event_type, replace(event.event_input, cwd="forged")))',
])
async def test_user_failures_and_forgery_are_isolated_without_changing_host_facts(host, replacement):
    source = USER_CODE.replace('output = await next_handler(event)', replacement)
    await _install(host, source=source)
    result = await _start(host)
    assert result == (SessionStartOutput("D:/project/src"),)
    assert await _start(host) == () and len(_entered(host)) == 1


@pytest.mark.asyncio
async def test_post_core_receipt_change_and_multiple_next_keep_actual_receipt(host):
    source = USER_CODE.replace('output = await next_handler(event)',
        'output = await next_handler(event)\n        assert await next_handler(event) == output')
    source = source.replace('return output', 'return replace(output, cwd="forged")')
    await _install(host, source=source)
    result = await _start(host)
    assert result[0].cwd == "D:/project/src" and len(_entered(host)) == 1


@pytest.mark.asyncio
async def test_direct_event_cannot_forge_a_start_invocation(host):
    await _install(host)
    await _start(host)
    value = _entered(host)[0]["input"]
    from jiuwenswarm.extensions_v2.event_types.session_start import SessionStartInput
    with pytest.raises(ValidationError):
        await host.bus.trigger(Event("session.start", SessionStartInput(**value)), _context())
    assert len(_entered(host)) == 1


@pytest.mark.asyncio
async def test_cancelled_attempt_releases_counts_and_is_not_replayed(host):
    await _install(host)
    entered = asyncio.Event()

    async def pause(value):
        if value["phase"] == "enter":
            entered.set()
            await asyncio.Event().wait()

    host.controls["on_record"] = pause
    task = asyncio.create_task(_start(host))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    host.controls["on_record"] = None
    assert host.manager.registry.runtime.inflight_count("start.a") == 0
    assert await _start(host, request="retry") == ()
    assert len(_entered(host)) == 1


@pytest.mark.asyncio
async def test_framework_failure_is_not_a_success_or_automatic_replay(host, monkeypatch):
    await _install(host)
    original = host.bus.trigger

    async def broken(event, session=None, **kwargs):
        if event.event_type == "session.start":
            raise RuntimeError("framework failure")
        return await original(event, session, **kwargs)

    monkeypatch.setattr(host.bus, "trigger", broken)
    with pytest.raises(RuntimeError, match="framework failure"):
        await _start(host)
    assert await _start(host, request="retry") == ()
    assert host.manager.registry.runtime.inflight_count("start.a") == 0


@pytest.mark.asyncio
async def test_reentry_is_refused_and_escaped_child_loses_start_scope(host):
    await _install(host)
    release = asyncio.Event()
    children = []

    async def later():
        await release.wait()
        return await _start(host, request="later")

    async def reenter(value):
        if value["phase"] == "enter":
            with pytest.raises(RuntimeError, match="re-enter"):
                await _start(host)
            children.append(asyncio.create_task(later()))

    host.controls["on_record"] = reenter
    await _start(host)
    release.set()
    assert await children[0] == ()


@pytest.mark.asyncio
async def test_empty_package_and_invalid_host_facts_have_explicit_results(host):
    package = await _install(host, source=EMPTY_EXTENSION)
    with pytest.raises(ValueError):
        await host.manager.ensure_session_started(SessionContext("../other"), request_id="r",
                                                   channel_id="web", is_interactive=True)
    result = await _start(host)
    assert len(result) == 1 and result[0].cwd == "D:/project/src" and not host.records
    assert (await host.manager.uninstall("start.a")).status == "SUCCESS"
    assert (await host.manager.install([package]))[0].status == "SUCCESS"
    result = await _start(host)
    assert len(result) == 1 and host.manager.active_packages()[0].generation == 1


@pytest.mark.asyncio
async def test_installed_package_can_see_compact_ops_but_native_backend_is_not_integrated(host):
    source = '''
from openjiuwen.core.context_engine import CompactionError

class InterfaceOnly:
    def register(self, registrar):
        registrar.register_event_handler("session.start", self.start, {})
        registrar.register_event_handler("session.compact", self.compact, {})

    async def start(self, event, ops, next_handler):
        try:
            await ops.session.compact(instructions="Keep pending work")
        except CompactionError as error:
            await ops.probe.record({"code": error.code, "session": ops.session_context.session_id})
        else:
            raise AssertionError("the default host must not run real compaction")
        return await next_handler(event)

    async def compact(self, event, ops, next_handler):
        raise AssertionError("no native compact request should be dispatched")

def create_extension():
    return InterfaceOnly()
'''
    await _install(host, "compact.interface", source)
    assert len(await _start(host)) == 1
    assert len(host.records) == 1
    assert host.records[0]["code"] == "COMPACT_NOT_INTEGRATED"
    assert host.records[0]["session"] == "s1"
    assert not host.assembly.compact.bindings and not host.assembly.compact.tasks
