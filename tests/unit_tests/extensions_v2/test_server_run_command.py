# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Exercise command policies through real installed USER packages and CORE."""

import asyncio
from types import SimpleNamespace

import pytest

from openjiuwen.core.common.exception.errors import ValidationError
from openjiuwen.core.events import Event, HandlerLevel, SessionContext

from jiuwenswarm.extensions_v2 import assembly
from jiuwenswarm.extensions_v2.event_types.server_run_command import (
    SERVER_RUN_COMMAND_EVENT, CommandOrigin, CommandPresentation, ServerCommandError,
    ServerRunCommandInput, ServerRunCommandResult,
)
from jiuwenswarm.server.runtime.commands.run_command_events import (
    NativeCommandResult, current_command, dispatch_server_run_command,
)
from .management.support import write_package


@pytest.fixture
async def host(tmp_path, monkeypatch):
    monkeypatch.setattr(assembly, "_ASSEMBLY", None)
    monkeypatch.setattr(assembly, "resolve_install_dir", lambda: tmp_path / "installed")
    monkeypatch.setattr(assembly, "resolve_store_dir", lambda: tmp_path / "store")
    instance = await assembly.install_extensions()
    assert instance is not None
    value = SimpleNamespace(instance=instance, bus=instance.bus, tmp=tmp_path,
                            calls=[], notes=[], trace=[], native_context=())
    try:
        yield value
    finally:
        await assembly.shutdown_extensions()


async def _policy(host, body, *, pattern=None, package_id="command.policy"):
    source = '''from dataclasses import replace
from jiuwenswarm.extensions_v2.event_types.server_run_command import ServerRunCommandResult
from openjiuwen.core.events import Event

class Policy:
    def register(self, registrar):
        registrar.register_event_handler("server.run_command", self.handle, PATTERN)

    async def handle(self, event, ops, next_handler):
BODY

def create_extension():
    return Policy()
'''.replace("PATTERN", repr(pattern or {})).replace("BODY", "\n".join("        " + line for line in body.splitlines()))
    root = write_package(host.tmp / "source", package_id=package_id, module_source=source)
    report = (await host.instance.manager.install([root]))[0]
    assert report.status == "SUCCESS", report


async def _run(host, *, command="status", args="", origin=None, presentation=None, execute=None):
    async def native(value):
        host.calls.append(value)
        host.trace.append("core")
        return NativeCommandResult({"output": "native:" + value, "panel": "still-open"},
                                   "native:" + value, host.native_context)

    async def record(notes):
        host.notes.append(notes)

    return await dispatch_server_run_command(
        host.bus, SessionContext("session-a", cwd=str(host.tmp)), command=command, args=args,
        origin=origin or CommandOrigin("bridge"), presentation=presentation or CommandPresentation(),
        execute=execute or native, record_context=record,
    )


@pytest.mark.asyncio
async def test_default_core_executes_once_and_keeps_native_payload_private(host):
    result = await _run(host, args="one  two ")
    assert host.calls == ["one  two "]
    assert result.use_native and result.output.text == "native:one  two "
    assert result.output.ref > 0 and result.native["panel"] == "still-open"
    assert not hasattr(result.output, "panel")
    assert host.notes == [] and current_command() is None


@pytest.mark.asyncio
async def test_user_next_builtin_core_uses_bound_context_and_changed_args(host):
    await _policy(host, '''assert ops.session_context.session_id == "session-a"
assert event.event_input.origin.kind == "bridge"
value = replace(event.event_input, args="rewritten", presentation=None)
return await next_handler(Event(event.event_type, value))''', pattern={"command": "status", "origin.kind": "bridge"})

    async def builtin(event, ops, next_handler):
        host.trace.append("builtin-before")
        value = await next_handler(event)
        host.trace.append("builtin-after")
        return value

    host.bus.register_event_handler(SERVER_RUN_COMMAND_EVENT, builtin, {}, HandlerLevel.BUILTIN)
    result = await _run(host, args="before")
    assert host.calls == ["rewritten"]
    assert host.trace == ["builtin-before", "core", "builtin-after"]
    assert result.output.text == "native:rewritten"


@pytest.mark.asyncio
async def test_nonmatching_policy_keeps_native_args(host):
    await _policy(host, 'return ServerRunCommandResult(text="handled")', pattern={"command": "hello"})
    assert (await _run(host)).use_native
    assert host.calls == [""]


@pytest.mark.asyncio
async def test_user_can_short_circuit_with_text_and_hidden_messages(host):
    await _policy(host, 'return ServerRunCommandResult(text="project ready", context=("Use project rules",))')
    result = await _run(host, command="project_status")
    assert not result.use_native and result.native is None and result.output.ref is None
    assert result.output.text == "project ready"
    assert host.calls == [] and host.notes == [("Use project rules",)]


@pytest.mark.asyncio
async def test_post_next_replaces_text_and_extends_native_notes_without_reexecution(host):
    host.native_context = ("native note",)
    await _policy(host, '''result = await next_handler(event)
return ServerRunCommandResult(text=result.text + " / checked", context=result.context + ("plugin note",))''')
    result = await _run(host, args="x")
    assert result.output.text == "native:x / checked" and result.output.ref is None
    assert result.native["panel"] == "still-open"
    assert host.calls == ["x"] and host.notes == [("native note", "plugin note")]


@pytest.mark.asyncio
async def test_omitted_context_inherits_native_notes(host):
    host.native_context = ("retained",)
    await _policy(host, '''await next_handler(event)
return ServerRunCommandResult(text="replacement")''')
    result = await _run(host)
    assert result.output.context == ("retained",) and host.notes == [("retained",)]


@pytest.mark.asyncio
async def test_multiple_user_layers_see_inherited_notes_from_their_next(host):
    host.native_context = ("native",)
    await _policy(host, '''result = await next_handler(event)
assert result.context == ("native", "inner")
return ServerRunCommandResult(text="outer")''', package_id="command.outer")
    await _policy(host, '''result = await next_handler(event)
return ServerRunCommandResult(text="inner", context=result.context + ("inner",))''', package_id="command.inner")
    result = await _run(host)
    assert result.output.text == "outer" and host.notes == [("native", "inner")]


@pytest.mark.asyncio
async def test_child_task_cannot_execute_the_parent_continuation(host):
    await _policy(host, '''import asyncio
try:
    await asyncio.create_task(next_handler(event))
except Exception:
    pass
else:
    raise AssertionError("child task was allowed to execute")
return await next_handler(event)''')
    assert (await _run(host)).use_native
    assert host.calls == [""]


@pytest.mark.asyncio
async def test_same_session_reentry_is_rejected_before_another_command(host):
    async def recursive(args):
        host.calls.append(args)
        await _run(host)
        return NativeCommandResult(None)

    with pytest.raises(ServerCommandError, match="COMMAND_REENTRANT"):
        await _run(host, execute=recursive)
    assert host.calls == [""] and current_command() is None


@pytest.mark.asyncio
async def test_native_callback_retrigger_cannot_deadlock_its_own_core_lock(host):
    async def recursive(args):
        host.calls.append(args)
        frame = current_command()
        await host.bus.trigger(Event(SERVER_RUN_COMMAND_EVENT, frame.original), frame.context)
        return NativeCommandResult(None)

    with pytest.raises(ServerCommandError, match="COMMAND_REENTRANT"):
        await asyncio.wait_for(_run(host, execute=recursive), 3)
    assert host.calls == [""] and current_command() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("expression", [
    'ServerRunCommandResult(text="forged", ref=999999999)',
    'ServerRunCommandResult(text="forged", ref=result.ref)',
    'ServerRunCommandResult(text="forged", context=("drop native notes",))',
    '{"text": "wrong Python contract"}',
])
async def test_invalid_outputs_are_isolated_and_cannot_fake_native_facts(host, expression):
    host.native_context = ("protected",)
    await _policy(host, 'result = await next_handler(event)\nreturn ' + expression)
    result = await _run(host, args="once")
    assert result.use_native and result.output.text == "native:once"
    assert host.calls == ["once"] and host.notes == [("protected",)]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [
    'command="other"', 'origin=replace(value.origin, kind="sdk")',
    'presentation=replace(value.presentation, columns=120)', 'args=1',
])
async def test_protected_input_rewrites_do_not_reach_native_implementation(host, change):
    await _policy(host, 'value = event.event_input\nreturn await next_handler(Event(event.event_type, replace(value, ' + change + ')))')
    assert (await _run(host, args="original")).use_native
    assert host.calls == ["original"]


@pytest.mark.asyncio
async def test_repeated_next_reuses_one_native_execution_and_reference(host):
    await _policy(host, '''first = await next_handler(event)
second = await next_handler(event)
assert first.ref == second.ref
return second''')
    assert (await _run(host)).use_native and host.calls == [""]


@pytest.mark.asyncio
async def test_native_failure_is_not_retried_after_user_fault_isolation(host):
    await _policy(host, 'return await next_handler(event)')

    async def broken(args):
        host.calls.append(args)
        raise RuntimeError("native failure")

    with pytest.raises(RuntimeError, match="native failure"):
        await _run(host, execute=broken)
    assert host.calls == [""] and current_command() is None


@pytest.mark.asyncio
async def test_cancelled_native_invocation_closes_frame_without_committing_notes(host):
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked(args):
        entered.set()
        await release.wait()
        return NativeCommandResult(None)

    task = asyncio.create_task(_run(host, execute=blocked))
    await asyncio.wait_for(entered.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert current_command() is None and host.notes == []


@pytest.mark.asyncio
async def test_raw_event_requires_host_frame(host):
    value = ServerRunCommandInput("status", "", CommandOrigin("bridge"), CommandPresentation())
    with pytest.raises(ValidationError):
        await host.bus.trigger(Event(SERVER_RUN_COMMAND_EVENT, value), SessionContext("session-a"))


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", [
    CommandOrigin("unknown"), CommandOrigin("plugin"), CommandOrigin("channel"),
    CommandOrigin("bridge", name="fake"), CommandOrigin("channel", name="x", server="slack"),
    CommandOrigin(["invalid-kind"]),
])
async def test_host_rejects_invalid_origin_variants(host, origin):
    with pytest.raises(ServerCommandError, match="COMMAND_INVALID_INPUT"):
        await _run(host, origin=origin)


@pytest.mark.asyncio
@pytest.mark.parametrize("presentation", [CommandPresentation(columns=0), CommandPresentation(columns=True), CommandPresentation(isFullscreen=1)])
async def test_host_rejects_invalid_presentation(host, presentation):
    with pytest.raises(ServerCommandError, match="COMMAND_INVALID_INPUT"):
        await _run(host, presentation=presentation)
