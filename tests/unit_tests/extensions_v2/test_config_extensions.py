# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Installed USER packages, real host reads and native configuration persistence."""

import asyncio
import json
from pathlib import Path
from textwrap import indent
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zipfile import ZipFile

import pytest
import yaml

from openjiuwen.core.events import Event, HandlerLevel, SessionContext

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.extensions_v2 import assembly
from jiuwenswarm.server.runtime.gateway_adapter.config_adapter import ConfigAdapter
from .management.support import write_package


@pytest.fixture
async def host(tmp_path, monkeypatch):
    from jiuwenswarm.common import config
    from jiuwenswarm.extensions_v2.management import local_cli

    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({
        "preferred_language": "zh", "react": {"context_engine_config": {"enabled": False}},
        "rsi": {"enabled": False}, "rea": {"enabled": False},
        "models": {"api_key": "SECRET_FIXTURE_ONLY"}, "unpublished": {"value": "private"},
    }), encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_YAML_PATH", path)
    monkeypatch.setattr(config, "_CONFIG_YAML_PATH", path)
    monkeypatch.setattr(config, "get_config_file", lambda: path)
    monkeypatch.setattr(assembly, "_ASSEMBLY", None)
    monkeypatch.setattr(assembly, "resolve_install_dir", lambda: tmp_path / "installed")
    monkeypatch.setattr(assembly, "resolve_store_dir", lambda: tmp_path / "store")
    monkeypatch.setattr(assembly, "resolve_state_dir", lambda: tmp_path / "state")
    monkeypatch.setattr(assembly, "resolve_install_report_path", lambda: tmp_path / "reports" / "install.json")
    monkeypatch.setattr(local_cli, "start_local_cli", AsyncMock(return_value=None))
    monkeypatch.setattr("jiuwenswarm.enterprise.edition.is_enterprise", lambda: False)
    records = []
    original = assembly.bootstrap.register_handlers

    async def record(ops, value):
        records.append(value)

    async def build(event, ops, next_handler):
        builder = await next_handler(event)
        builder.add_namespace("probe")
        builder.add_method("probe", "record", record)
        return builder

    def register(bus, webui, *, file_service=None, compact_service=None, authorization_service=None):
        original(bus, webui, file_service=file_service, compact_service=compact_service,
                 authorization_service=authorization_service)
        bus.register_event_handler("ops.build", build, {}, HandlerLevel.BUILTIN)

    monkeypatch.setattr(assembly.bootstrap, "register_handlers", register)
    instance = await assembly.install_extensions()
    assert instance is not None

    async def core(event, ops, next_handler):
        return "original-complete"

    instance.bus.register_event_type("test.config", lambda value, pattern: True, core)
    callback = AsyncMock(return_value=True)
    value = SimpleNamespace(instance=instance, records=records, path=path, tmp=tmp_path,
                            callback=callback, adapter=ConfigAdapter(on_config_saved=callback))
    try:
        yield value
    finally:
        await assembly.shutdown_extensions()


async def install(host, body, *, event="test.config", name="config.probe", pattern=None):
    source = '''
import asyncio
from openjiuwen.core.events import Event
from jiuwenswarm.extensions_v2.event_types.config import ConfigExtensionError

class Probe:
    def register(self, registrar):
        registrar.register_event_handler(EVENT, self.handle, PATTERN)

    async def handle(self, event, ops, next_handler):
BODY

def create_extension():
    return Probe()
'''.replace("EVENT", repr(event)).replace("PATTERN", repr(pattern or {})).replace("BODY", indent(body, "        "))
    root = write_package(host.tmp / name, package_id=name, module_source=source)
    result = (await host.instance.manager.install([root]))[0]
    assert result.status == "SUCCESS", result


async def call(host, *, context=True):
    value = await host.instance.bus.trigger(Event("test.config", {}), SessionContext("s1") if context else None)
    assert value == "original-complete"
    return host.records[-1]


async def save(host, params, *, method=ReqMethod.CONFIG_SET, session_id="s1"):
    return await host.adapter.handle(AgentRequest(
        request_id="config-test", req_method=method, params=params,
        session_id=session_id, user_id="test-user", channel_id="web",
    ))


def stored(host):
    return yaml.safe_load(host.path.read_text(encoding="utf-8"))


@pytest.mark.asyncio
async def test_three_ops_are_registered_and_project_real_host_config(host):
    await install(host, '''
value = await ops.config.value("preferred_language")
rows = await ops.config.items()
snapshot = await ops.config.snapshot()
missing = await ops.config.value("models.api_key")
await ops.probe.record({"value": value, "rows": rows, "snapshot": snapshot, "missing": missing})
return await next_handler(event)
''')
    result = await call(host)
    assert result["value"] == "zh" and result["missing"] is None
    assert {row["key"] for row in result["rows"]} == {
        "preferred_language", "rsi.enabled", "rea.enabled", "react.context_engine_config.enabled",
    }
    assert result["snapshot"] == {
        "preferred_language": "zh", "rsi": {"enabled": False}, "rea": {"enabled": False},
        "react": {"context_engine_config": {"enabled": False}},
    }
    assert "SECRET_FIXTURE_ONLY" not in json.dumps(result)
    assert "unpublished" not in json.dumps(result)


@pytest.mark.asyncio
async def test_global_config_ops_work_without_session_context(host):
    await install(host, 'await ops.probe.record(await ops.config.value("rsi.enabled"))\nreturn await next_handler(event)')
    assert await call(host, context=False) is False


@pytest.mark.asyncio
async def test_describe_changes_only_presentation_and_can_hide_rows(host):
    await install(host, '''
result = await next_handler(event)
return {**result, "label": "Custom label", "isHidden": event.event_input["key"] == "rsi.enabled"}
''', event="config.describe", name="config.presentation")
    await install(host, '''
await ops.probe.record({"rows": await ops.config.items(), "value": await ops.config.value("rsi.enabled")})
return await next_handler(event)
''')
    result = await call(host)
    assert len(result["rows"]) == 3 and result["value"] is False
    assert all(row["label"] == "Custom label" and row["provider"] == {"plugin": "engine", "tier": "core"}
               for row in result["rows"])
    assert stored(host)["rsi"]["enabled"] is False


@pytest.mark.asyncio
async def test_value_and_snapshot_do_not_trigger_describe(host):
    await install(host, 'await ops.probe.record("describe")\nreturn await next_handler(event)',
                  event="config.describe", name="config.presentation")
    await install(host, '''
await ops.config.value("preferred_language")
await ops.config.snapshot()
return await next_handler(event)
''')
    await host.instance.bus.trigger(Event("test.config", {}), SessionContext("s1"))
    assert host.records == []


@pytest.mark.asyncio
async def test_snapshot_is_detached_and_does_not_persist_mutation(host):
    await install(host, '''
snapshot = await ops.config.snapshot()
snapshot["rsi"]["enabled"] = True
await ops.probe.record(await ops.config.snapshot())
return await next_handler(event)
''')
    assert (await call(host))["rsi"]["enabled"] is False
    assert stored(host)["rsi"]["enabled"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["project", "session", "unknown", []])
async def test_unsupported_scopes_are_explicit_errors(host, scope):
    await install(host, f'''
try:
    await ops.config.items({scope!r})
except ConfigExtensionError as error:
    await ops.probe.record(error.code)
return await next_handler(event)
''')
    assert await call(host) == "UNSUPPORTED_SCOPE"


@pytest.mark.asyncio
async def test_effective_scope_uses_native_environment_resolution(host, monkeypatch):
    document = stored(host)
    document["rsi"]["enabled"] = "${CONFIG_EXT_TEST_FLAG}"
    host.path.write_text(yaml.safe_dump(document), encoding="utf-8")
    monkeypatch.setenv("CONFIG_EXT_TEST_FLAG", "true")
    await install(host, '''
await ops.probe.record([await ops.config.value("rsi.enabled", "effective"),
                        await ops.config.value("rsi.enabled", "user")])
return await next_handler(event)
''')
    assert list(await call(host)) == [True, False]


@pytest.mark.asyncio
async def test_native_save_without_user_hooks_persists_and_reloads_once(host):
    result = await save(host, {"rsi_enabled": "true"})
    assert result.ok and result.metadata["config_changed"] is True
    assert stored(host)["rsi"]["enabled"] is True
    host.callback.assert_awaited_once()


@pytest.mark.asyncio
async def test_set_user_can_rewrite_value_before_original_writer(host):
    await install(host, '''
await ops.probe.record(dict(event.event_input))
return await next_handler(Event(event.event_type, {**event.event_input, "value": False}))
''', event="config.set", pattern={"key": "rsi.enabled"})
    result = await save(host, {"rsi_enabled": True})
    assert result.ok and stored(host)["rsi"]["enabled"] is False
    assert host.records[0]["previous"] is False and host.records[0]["value"] is True
    assert host.records[0]["origin"] == {"kind": "bridge"}
    assert host.records[0]["scope"] == "user"
    host.callback.assert_awaited_once()


@pytest.mark.asyncio
async def test_pre_save_denial_prevents_yaml_and_reload(host):
    await install(host, 'return {"deny": "blocked by policy"}', event="config.set")
    before = host.path.read_bytes()
    result = await save(host, {"rsi_enabled": True})
    assert not result.ok and result.payload["code"] == "CONFIG_SET_DENIED"
    assert host.path.read_bytes() == before
    host.callback.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("method", [ReqMethod.CONFIG_SET, ReqMethod.CONFIG_SAVE_ALL])
async def test_second_field_denial_prevents_the_entire_batch(host, method):
    await install(host, 'return {"deny": "second field blocked"}', event="config.set", pattern={"key": "rea.enabled"})
    before = host.path.read_bytes()
    updates = {"rsi_enabled": True, "rea_enabled": True}
    result = await save(host, {"config": updates} if method == ReqMethod.CONFIG_SAVE_ALL else updates, method=method)
    assert not result.ok and result.payload["code"] == "CONFIG_SET_DENIED"
    assert host.path.read_bytes() == before
    host.callback.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("method", [ReqMethod.CONFIG_SET, ReqMethod.CONFIG_SAVE_ALL])
async def test_batch_uses_one_native_save_and_retains_response_contract(host, method):
    await install(host, 'await ops.probe.record(event.event_input["key"])\nreturn await next_handler(event)', event="config.set")
    updates = {"rsi_enabled": True, "rea_enabled": True}
    result = await save(host, {"config": updates} if method == ReqMethod.CONFIG_SAVE_ALL else updates, method=method)
    assert result.ok and {"rsi_enabled", "rea_enabled"} <= set(result.payload["updated"])
    assert stored(host)["rsi"]["enabled"] is True and stored(host)["rea"]["enabled"] is True
    assert list(host.records) == ["rsi.enabled", "rea.enabled"]
    host.callback.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("output", ['{"deny": "too late"}', '{"value": False}', 'None'])
async def test_post_next_cannot_falsify_a_committed_native_receipt(host, output):
    await install(host, f'await next_handler(event)\nreturn {output}', event="config.set")
    result = await save(host, {"rsi_enabled": True})
    assert result.ok and stored(host)["rsi"]["enabled"] is True
    host.callback.assert_awaited_once()


@pytest.mark.asyncio
async def test_fake_success_without_core_never_persists(host):
    await install(host, 'return {"value": True}', event="config.set")
    before = host.path.read_bytes()
    result = await save(host, {"rsi_enabled": True})
    assert not result.ok and host.path.read_bytes() == before
    host.callback.assert_not_called()


@pytest.mark.asyncio
async def test_repeated_next_does_not_repeat_the_native_write(host):
    await install(host, 'await next_handler(event)\nreturn await next_handler(event)', event="config.set")
    result = await save(host, {"rsi_enabled": True})
    assert result.ok and stored(host)["rsi"]["enabled"] is True
    host.callback.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [0, None, {}, "not-a-boolean"])
async def test_invalid_public_value_cannot_mutate_native_storage(host, value):
    before = host.path.read_bytes()
    result = await save(host, {"rsi_enabled": value})
    assert not result.ok and result.payload["code"] == "INVALID_VALUE"
    assert host.path.read_bytes() == before


@pytest.mark.asyncio
async def test_direct_bus_set_without_platform_frame_is_rejected(host):
    before = host.path.read_bytes()
    with pytest.raises(Exception):
        await host.instance.bus.trigger(Event("config.set", {
            "key": "rsi.enabled", "value": True, "previous": False, "scope": "user",
            "provider": {"plugin": "engine", "tier": "core"}, "origin": {"kind": "bridge"},
        }), SessionContext("s1"))
    assert host.path.read_bytes() == before


@pytest.mark.asyncio
async def test_locale_uses_existing_language_writer_and_accepts_user_normalization(host):
    await install(host, '''
return await next_handler(Event(event.event_type, {
    **event.event_input, "value": event.event_input["value"].strip().lower(),
}))
''', event="config.set", pattern={"key": "preferred_language"})
    result = await save(host, {"preferred_language": " EN "}, method=ReqMethod.LOCALE_SET_CONF)
    assert result.ok and result.payload["preferred_language"] == "en"
    assert stored(host)["preferred_language"] == "en"


@pytest.mark.asyncio
async def test_enterprise_locale_is_not_published_as_host_yaml(host, monkeypatch):
    monkeypatch.setattr("jiuwenswarm.enterprise.edition.is_enterprise", lambda: True)
    await install(host, '''
await ops.probe.record({"value": await ops.config.value("preferred_language"),
                        "snapshot": await ops.config.snapshot()})
return await next_handler(event)
''')
    result = await call(host)
    assert result["value"] is None and "preferred_language" not in result["snapshot"]


@pytest.mark.asyncio
async def test_uninstall_removes_policy_but_keeps_native_and_read_capabilities(host):
    await install(host, 'return {"deny": "blocked"}', event="config.set", name="config.policy")
    assert not (await save(host, {"rsi_enabled": True})).ok
    assert (await host.instance.manager.uninstall("config.policy")).status == "SUCCESS"
    assert (await save(host, {"rsi_enabled": True})).ok
    assert stored(host)["rsi"]["enabled"] is True


@pytest.mark.asyncio
async def test_example_zip_runs_the_real_command_and_blocks_native_save(host):
    from jiuwenswarm.server.runtime.commands.adapter_commands import run_adapter_slash_command

    example = Path(__file__).resolve().parents[3] / "jiuwenswarm/extensions_v2/examples/config_probe"
    archive = host.tmp / "config-probe.zip"
    with ZipFile(archive, "w") as package:
        for name in ("manifest.json", "extension.py", "README.md", "README_EN.md"):
            package.write(example / name, name)
    assert (await host.instance.manager.install([archive]))[0].status == "SUCCESS"

    async def native(*args, **kwargs):
        return None

    result = await run_adapter_slash_command(SimpleNamespace(_project_dir=str(host.tmp)),
                                            "/config_probe", "s1", "agent.code.normal", "web", native)
    parsed = json.loads(result["output"])
    assert parsed["value"] is False and parsed["snapshot"]["rsi"]["enabled"] is False
    assert next(row for row in parsed["items"] if row["key"] == "rsi.enabled")["label"] == "RSI (config probe)"
    before = host.path.read_bytes()
    assert not (await save(host, {"rsi_enabled": True})).ok
    assert host.path.read_bytes() == before


@pytest.mark.asyncio
async def test_unfiltered_policy_can_deny_later_field_in_the_same_batch(host):
    await install(host, '''
if event.event_input["key"] == "rea.enabled":
    return {"deny": "second field is blocked"}
return await next_handler(event)
''', event="config.set")
    before = host.path.read_bytes()
    result = await save(host, {"rsi_enabled": True, "rea_enabled": True})
    assert not result.ok and result.payload["code"] == "CONFIG_SET_DENIED"
    assert host.path.read_bytes() == before
    host.callback.assert_not_called()


@pytest.mark.asyncio
async def test_cancellation_before_commit_leaves_no_write_or_owned_event_tasks(host):
    await install(host, '''
await ops.probe.record("entered")
await asyncio.Event().wait()
return await next_handler(event)
''', event="config.set", name="config.blocking")
    before = host.path.read_bytes()
    task = asyncio.create_task(save(host, {"rsi_enabled": True, "rea_enabled": True}))
    async with asyncio.timeout(5):
        while not host.records:
            await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert host.path.read_bytes() == before
    assert not any(item.get_name().startswith("config.set:") and not item.done() for item in asyncio.all_tasks())
    host.callback.assert_not_called()


@pytest.mark.asyncio
async def test_set_policy_cannot_redirect_the_trusted_key(host):
    await install(host, '''
return await next_handler(Event(event.event_type, {**event.event_input, "key": "rea.enabled"}))
''', event="config.set", pattern={"key": "rsi.enabled"})
    await save(host, {"rsi_enabled": True})
    assert stored(host)["rea"]["enabled"] is False


@pytest.mark.asyncio
async def test_describe_cannot_change_provider_or_key(host):
    await install(host, '''
return await next_handler(Event(event.event_type, {
    **event.event_input, "key": "models.api_key", "provider": {"plugin": "forged", "tier": "core"},
}))
''', event="config.describe", pattern={"key": "rsi.enabled"}, name="config.presentation")
    await install(host, 'await ops.probe.record(await ops.config.items())\nreturn await next_handler(event)')
    result = await call(host)
    assert {row["key"] for row in result} == {"preferred_language", "react.context_engine_config.enabled", "rsi.enabled", "rea.enabled"}
    assert all(row["provider"] == {"plugin": "engine", "tier": "core"} for row in result)


@pytest.mark.asyncio
async def test_items_reentry_is_rejected_without_breaking_outer_catalog(host):
    await install(host, '''
try:
    await ops.config.items()
except ConfigExtensionError as error:
    await ops.probe.record(error.code)
return await next_handler(event)
''', event="config.describe", pattern={"key": "rsi.enabled"}, name="config.presentation")
    await install(host, 'await ops.probe.record(await ops.config.items())\nreturn await next_handler(event)')
    assert len(await call(host)) == 4
    assert host.records[0] == "CONFIG_REENTRANT"


@pytest.mark.asyncio
async def test_previous_boolean_cannot_be_replaced_by_equal_integer(host):
    await install(host, '''
return await next_handler(Event(event.event_type, {
    **event.event_input, "previous": 0, "value": False,
}))
''', event="config.set", pattern={"key": "rsi.enabled"})
    result = await save(host, {"rsi_enabled": True})
    # Core isolates the invalid rewrite and continues the original candidate.
    assert result.ok and stored(host)["rsi"]["enabled"] is True


@pytest.mark.asyncio
async def test_global_native_save_does_not_invent_a_session_binding(host):
    await install(host, 'await ops.probe.record(ops.session_context is None)\nreturn await next_handler(event)',
                  event="config.set")
    result = await save(host, {"rsi_enabled": True}, session_id=None)
    assert result.ok and stored(host)["rsi"]["enabled"] is True
    assert host.records == [True]
