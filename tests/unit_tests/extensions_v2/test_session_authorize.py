# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Real Registry/Builder authorization calls with non-production credentials."""

import asyncio
import json
from textwrap import indent
from types import SimpleNamespace

import pytest

from openjiuwen.core.common.exception.errors import ValidationError
from openjiuwen.core.events import Event, EventHandlerOpsBuilder, HandlerLevel, SessionContext

from jiuwenswarm.extensions_v2 import assembly
from jiuwenswarm.extensions_v2.event_types.session_authorize import SessionAuthorizationError
from jiuwenswarm.extensions_v2.session_authorization.service import AuthorizationCaller
from .management.support import write_package

TEST_CREDENTIAL = "TEST_ONLY_DO_NOT_USE_IN_REAL_REQUESTS"

CALLER = '''
from jiuwenswarm.extensions_v2.event_types.session_authorize import SessionAuthorizationError

class Caller:
    def register(self, registrar):
        registrar.register_event_handler("test.authorize", self.call, {})

    async def call(self, event, ops, next_handler):
        try:
            value = await ops.session.authorize()
        except SessionAuthorizationError as error:
            await ops.probe.record({"phase": "caller", "error": error.code,
                                    "reason": getattr(error, "reason", None)})
        else:
            await ops.probe.record({"phase": "caller", "value": value})
        return await next_handler(event)

def create_extension():
    return Caller()
'''


@pytest.fixture
async def host(tmp_path, monkeypatch):
    monkeypatch.setattr(assembly, "_ASSEMBLY", None)
    monkeypatch.setattr(assembly, "resolve_install_dir", lambda: tmp_path / "installed")
    monkeypatch.setattr(assembly, "resolve_store_dir", lambda: tmp_path / "store")
    original = assembly.bootstrap.register_handlers
    records, captured_ops = [], []
    control = {"record": None}

    async def record(ops, value):
        records.append({"package": ops.package_metadata.package_id, **value})
        captured_ops.append(ops)
        if control["record"] is not None:
            await control["record"](value)

    async def build(event, ops, next_handler):
        builder = await next_handler(event)
        builder.add_namespace("probe")
        builder.add_method("probe", "record", record)
        return builder

    def register(bus, webui, *, file_service=None, authorization_service=None, compact_service=None):
        original(bus, webui, file_service=file_service,
                 authorization_service=authorization_service, compact_service=compact_service)
        bus.register_event_handler("ops.build", build, {}, HandlerLevel.BUILTIN)

    monkeypatch.setattr(assembly.bootstrap, "register_handlers", register)
    instance = await assembly.install_extensions()
    assert instance is not None
    async def original_core(event, ops, next_handler):
        return "original-complete"
    instance.bus.register_event_type("test.authorize", lambda value, pattern: True, original_core)
    now = [1000.0]
    instance.authorization._clock = lambda: now[0]
    value = SimpleNamespace(instance=instance, service=instance.authorization, manager=instance.manager,
                            bus=instance.bus, records=records, ops=captured_ops, control=control,
                            now=now, tmp=tmp_path)
    try:
        yield value
    finally:
        control["record"] = None
        await assembly.shutdown_extensions()


async def _install(host, name="auth.caller", source=CALLER):
    root = write_package(host.tmp / "source", package_id=name, module_source=source)
    report = (await host.manager.install([root]))[0]
    assert report.status == "SUCCESS", report
    return root


async def _policy(host, body):
    source = '''
from jiuwenswarm.extensions_v2.event_types.session_authorize import SessionAuthorizationError

class Policy:
    def register(self, registrar):
        registrar.register_event_handler("session.authorize", self.handle, {})

    async def handle(self, event, ops, next_handler):
''' + indent(body, '        ') + '''

def create_extension():
    return Policy()
'''
    return await _install(host, "auth.policy", source)


def _bind(host, *, sid="s1", provider="anthropic", kind="api-key", expires_at=None):
    return host.service.bind(SessionContext(sid), provider=provider, credential=TEST_CREDENTIAL,
                             kind=kind, expires_at=expires_at)


async def _call(host, sid="s1"):
    assert await host.bus.trigger(Event("test.authorize", {}), SessionContext(sid)) == "original-complete"
    return [record for record in host.records if record.get("phase") == "caller"][-1]


@pytest.mark.asyncio
@pytest.mark.parametrize(("provider", "kind"), [
    ("anthropic", "bearer"), ("anthropic", "api-key"),
    ("openai", "bearer"), (" OpenAI ", "api-key"),
])
async def test_zero_argument_ops_returns_only_handle_kind_and_keeps_secret_in_host(host, provider, kind, caplog):
    await _install(host)
    assert _bind(host, provider=provider, kind=kind)
    result = (await _call(host))["value"]
    assert set(result) == {"handle", "kind"} and result["kind"] == kind
    assert isinstance(result["handle"], str) and result["handle"].startswith("sa_")
    assert result["handle"] != TEST_CREDENTIAL
    entry = host.service._handles[result["handle"]]
    assert entry.session_id == "s1" and entry.caller.metadata.package_id == "auth.caller"
    assert entry.binding.provider == provider.strip().casefold()
    assert entry.binding.secret == TEST_CREDENTIAL
    assert TEST_CREDENTIAL not in json.dumps(host.records) + caplog.text + repr(host.service._credentials)


@pytest.mark.asyncio
async def test_no_binding_and_unsupported_provider_return_none(host):
    await _install(host)
    assert (await _call(host))["value"] is None
    assert not _bind(host, provider="Qwen")
    assert (await _call(host))["value"] is None
    assert not host.service._credentials and not host.service._handles


@pytest.mark.asyncio
async def test_build_without_invocation_guard_never_issues_handle(host):
    await _install(host)
    _bind(host)
    host.service._supported = lambda: False
    assert (await _call(host))["value"] is None and not host.service._handles


@pytest.mark.asyncio
async def test_empty_input_and_user_builtin_core_order_preserve_original_caller(host, monkeypatch):
    from jiuwenswarm.extensions_v2.event_handlers import session_authorize as handlers
    await _install(host)
    await _policy(host, '''assert not event.event_input
await ops.probe.record({"phase": "user-before"})
result = await next_handler(event)
await ops.probe.record({"phase": "user-after"})
return result
''')
    trace = []
    original = handlers.authorize_core
    def core(event, ops):
        trace.append("core")
        return original(event, ops)
    monkeypatch.setattr(handlers, "authorize_core", core)
    async def builtin(event, ops, next_handler):
        trace.append("builtin-before")
        result = await next_handler(event)
        trace.append("builtin-after")
        return result
    host.bus.register_event_handler("session.authorize", builtin, {}, HandlerLevel.BUILTIN)
    _bind(host)
    result = (await _call(host))["value"]
    assert trace == ["builtin-before", "core", "builtin-after"]
    assert [record["phase"] for record in host.records] == ["user-before", "user-after", "caller"]
    assert host.service._handles[result["handle"]].caller.metadata.package_id == "auth.caller"


@pytest.mark.asyncio
@pytest.mark.parametrize("after_next", [False, True])
@pytest.mark.parametrize("answer", [{"value": None}, {"deny": "private operation"}])
async def test_withhold_and_deny_do_not_leave_issued_handles(host, after_next, answer):
    await _install(host)
    body = 'result = await next_handler(event)\n' if after_next else ''
    await _policy(host, body + 'return ' + repr(answer) + '\n')
    _bind(host)
    result = await _call(host)
    if "deny" in answer:
        assert result["error"] == "DENIED" and result["reason"] == "private operation"
    else:
        assert result["value"] is None
    assert not host.service._handles


@pytest.mark.asyncio
@pytest.mark.parametrize("forged", [
    {"value": {"handle": "fake", "kind": "bearer"}},
    {"value": {"handle": [], "kind": "api-key"}},
    {"value": {"handle": "fake", "kind": "api_key"}},
    {"value": {"handle": "fake", "kind": "api-key", "token": "fake"}},
])
async def test_forged_results_cannot_replace_host_issued_authorization(host, forged):
    await _install(host)
    await _policy(host, 'return ' + repr(forged) + '\n')
    _bind(host)
    result = (await _call(host))["value"]
    assert result["handle"] != "fake" and result["kind"] == "api-key"
    assert result["handle"] in host.service._handles


@pytest.mark.asyncio
async def test_repeated_next_issues_once_and_same_event_reentry_is_rejected(host):
    await _install(host)
    await _policy(host, '''try:
    await ops.session.authorize()
except SessionAuthorizationError as error:
    await ops.probe.record({"phase": "reentry", "code": error.code})
first = await next_handler(event)
second = await next_handler(event)
assert first == second
return second
''')
    _bind(host)
    assert (await _call(host))["value"] is not None
    assert len(host.service._handles) == 1
    assert host.records[0]["code"] == "AUTHORIZATION_REENTRANT"


@pytest.mark.asyncio
async def test_task_expiry_and_manually_constructed_ops_cannot_authorize(host):
    source = 'import asyncio\n' + CALLER.replace('await ops.session.authorize()', 'await asyncio.create_task(ops.session.authorize())')
    await _install(host, source=source)
    _bind(host)
    assert (await _call(host))["error"] == "UNBOUND_INVOCATION"
    expired = host.ops[-1]
    with pytest.raises(SessionAuthorizationError, match="UNBOUND_INVOCATION"):
        await expired.session.authorize()
    fake = EventHandlerOpsBuilder().build(package_metadata=expired.package_metadata, session_context=SessionContext("s1"))
    with pytest.raises(SessionAuthorizationError, match="UNBOUND_INVOCATION"):
        await host.service.authorize(fake)
    assert not host.service._handles


@pytest.mark.asyncio
async def test_direct_event_and_direct_issue_require_host_frame(host):
    await _install(host)
    _bind(host)
    with pytest.raises(ValidationError):
        await host.bus.trigger(Event("session.authorize", {}), SessionContext("s1"))
    record = host.manager.active_packages()[0]
    from openjiuwen.core.events import PackageMetadata
    caller = AuthorizationCaller(PackageMetadata(record.package_id, record.version, record.root), record.generation)
    with pytest.raises(SessionAuthorizationError, match="UNBOUND_INVOCATION"):
        host.service.issue(caller, "s1")
    assert not host.service._handles


@pytest.mark.asyncio
async def test_bind_rotation_provider_change_and_unbind_revoke_existing_handles(host):
    await _install(host)
    _bind(host)
    first = (await _call(host))["value"]["handle"]
    _bind(host, provider="OpenAI", kind="bearer")
    assert first not in host.service._handles
    second = (await _call(host))["value"]
    assert second["kind"] == "bearer"
    assert host.service._handles[second["handle"]].binding.provider == "openai"
    assert not _bind(host, provider="Qwen")
    assert second["handle"] not in host.service._handles
    assert (await _call(host))["value"] is None
    _bind(host)
    third = (await _call(host))["value"]["handle"]
    host.service.unbind("s1")
    assert third not in host.service._handles and (await _call(host))["value"] is None


@pytest.mark.asyncio
async def test_binding_expiry_and_handle_capacity_are_bounded(host):
    await _install(host)
    host.service._max_handles = 1
    _bind(host, expires_at=1010)
    first = (await _call(host))["value"]
    assert host.service._handles[first["handle"]].expires_at == 1010
    assert (await _call(host))["error"] == "HANDLE_CAPACITY_EXCEEDED"
    host.now[0] = 1011
    assert (await _call(host))["value"] is None
    assert not host.service._handles and not host.service._credentials
    _bind(host)
    second = (await _call(host))["value"]
    host.now[0] += 301
    third = (await _call(host))["value"]
    assert second["handle"] not in host.service._handles
    assert third["handle"] in host.service._handles


@pytest.mark.asyncio
async def test_session_and_package_activation_ownership_and_cleanup(host):
    await _install(host)
    await _install(host, "auth.second")
    _bind(host)
    await _call(host)
    values = {record["package"]: record["value"] for record in host.records if record.get("phase") == "caller"}
    assert len(values) == 2 and values["auth.caller"]["handle"] != values["auth.second"]["handle"]
    assert {entry.caller.metadata.package_id for entry in host.service._handles.values()} == {"auth.caller", "auth.second"}
    assert (await host.manager.reload("auth.caller")).status == "SUCCESS"
    assert values["auth.caller"]["handle"] not in host.service._handles
    assert values["auth.second"]["handle"] in host.service._handles
    assert (await host.manager.uninstall("auth.second")).status == "SUCCESS"
    assert not host.service._handles
    assert (await _call(host, "other-session"))["value"] is None
    host.service.close()
    assert not host.service._credentials
    assert (await _call(host))["error"] == "SERVICE_CLOSED"


@pytest.mark.asyncio
async def test_cancellation_after_core_revokes_handle_and_finishes_frame(host):
    await _install(host)
    await _policy(host, '''result = await next_handler(event)
await ops.probe.record({"phase": "issued"})
return result
''')
    entered = asyncio.Event()
    async def pause(value):
        if value.get("phase") == "issued":
            entered.set()
            await asyncio.Event().wait()
    host.control["record"] = pause
    _bind(host)
    task = asyncio.create_task(_call(host))
    await asyncio.wait_for(entered.wait(), 5)
    assert len(host.service._handles) == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not host.service._handles
    assert host.manager.registry.runtime.inflight_count("auth.caller") == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("call", ['ops.session.authorize("service", ["read"])', 'ops.session.authorize(service="x")', 'ops.session.authorize(scopes=["read"])'])
async def test_service_and_scopes_are_not_accepted(host, call):
    source = CALLER.replace('value = await ops.session.authorize()', 'value = await ' + call)
    source = source.replace('except SessionAuthorizationError as error:', 'except TypeError as error:')
    source = source.replace('"error": error.code', '"error": type(error).__name__')
    await _install(host, source=source)
    _bind(host)
    assert (await _call(host))["error"] == "TypeError"
    assert not host.service._handles


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [
    {"credential": ""}, {"credential": "unsafe\r\nheader"}, {"credential": None},
    {"kind": "api_key"}, {"expires_at": True}, {"expires_at": float("inf")},
])
async def test_invalid_host_binding_is_rejected_without_secret_diagnostics(host, invalid):
    await _install(host)
    _bind(host)
    parameters = {"provider": "anthropic", "credential": TEST_CREDENTIAL, "kind": "api-key", **invalid}
    with pytest.raises((ValueError, TypeError)) as error:
        host.service.bind(SessionContext("s1"), **parameters)
    assert TEST_CREDENTIAL not in str(error.value)
    assert (await _call(host))["value"]["kind"] == "api-key"


@pytest.mark.asyncio
async def test_binding_revoked_during_post_next_cannot_return_stale_handle(host):
    await _install(host)
    await _policy(host, '''result = await next_handler(event)
await ops.probe.record({"phase": "issued"})
return result
''')
    async def revoke(value):
        if value.get("phase") == "issued":
            host.service.unbind("s1")
    host.control["record"] = revoke
    _bind(host)
    result = await _call(host)
    assert result["error"] == "INVALID_RESULT"
    assert not host.service._handles and not host.service._credentials


@pytest.mark.asyncio
async def test_another_session_handle_cannot_be_returned_for_current_request(host):
    await _install(host)
    _bind(host, sid="s1")
    _bind(host, sid="s2")
    other = (await _call(host, "s2"))["value"]
    await _policy(host, 'return {"value": ' + repr(other) + '}\n')
    result = (await _call(host, "s1"))["value"]
    assert result["handle"] != other["handle"]
    assert host.service._handles[result["handle"]].session_id == "s1"


@pytest.mark.asyncio
async def test_failed_reload_preserves_old_generation_handles(host):
    await _install(host)
    _bind(host)
    result = (await _call(host))["value"]
    record = host.manager.active_packages()[0]
    (record.root / "extension.py").write_text("def broken(:", encoding="utf-8")
    assert (await host.manager.reload("auth.caller")).status == "FAILED"
    assert host.manager.active_packages()[0].generation == record.generation
    assert result["handle"] in host.service._handles


@pytest.mark.asyncio
async def test_old_inflight_handler_cannot_authorize_as_reloaded_generation(host):
    source = CALLER.replace(
        "        try:\n",
        '        await ops.probe.record({"phase": "before_authorize"})\n        try:\n',
        1,
    )
    await _install(host, source=source)
    _bind(host)
    entered, release = asyncio.Event(), asyncio.Event()

    async def pause(value):
        if value.get("phase") == "before_authorize":
            entered.set()
            await release.wait()

    host.control["record"] = pause
    task = asyncio.create_task(_call(host))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        runtime = host.manager.registry.runtime
        old_generation = runtime.invocation_generation(host.ops[-1])
        report = await host.manager.reload("auth.caller")
        assert report.status == "SUCCESS"
        assert runtime.get_active("auth.caller").generation > old_generation
    finally:
        release.set()
    result = await asyncio.wait_for(task, 5)
    assert result.get("error") == "UNBOUND_INVOCATION", result
    assert not host.service._handles
    host.control["record"] = None
    assert (await _call(host))["value"]["kind"] == "api-key"
