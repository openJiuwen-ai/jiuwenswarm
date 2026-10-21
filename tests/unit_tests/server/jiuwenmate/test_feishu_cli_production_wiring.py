"""Binding → Gateway E2A → server dispatch → optional CLI preparation."""

import asyncio
from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

from tests.unit_tests.server.jiuwenmate.test_resource_assembly import MapCatalog, service, settings, write_skill
from tests.unit_tests.server.jiuwenmate.test_feishu_group_production_wiring import _agent_websocket_server_type
from tests.unit_tests.gateway.test_app_web_handlers import FakeWebChannel, FakeChannelManager
from jiuwenswarm.common.e2a.agent_compat import e2a_to_agent_request
from jiuwenswarm.gateway.channel_manager.web.app_web_handlers import WebHandlersBindParams, _register_web_handlers
from jiuwenswarm.server.jiuwenmate.agent_manager import AgentManager
from jiuwenswarm.server.jiuwenmate.lark_cli_runtime import FeishuCliRuntime
from tests.unit_tests.server.jiuwenmate.test_lark_cli_runtime import config, setup_cli


def make_manager(tmp_path, config_loader, setup_cli):
    host = service(tmp_path, MapCatalog(), iter(["agent-a"]))
    host.create_colleague("local", settings())
    acquire, run, _ = setup_cli
    host.cli_runtime = FeishuCliRuntime(tmp_path, config_loader=config_loader, package_loader=acquire, runner=run)
    manager = AgentManager.create_default(host, tmp_path)
    return manager, host


async def ready(manager):
    for _ in range(200):
        if manager._cli_tasks:
            await asyncio.gather(*tuple(manager._cli_tasks.values()))
            return
        await asyncio.sleep(0.01)
    raise AssertionError("CLI preparation was not scheduled")


@pytest.mark.asyncio
async def test_saved_binding_automatically_prepares_agent_cli(tmp_path, monkeypatch, setup_cli):
    from jiuwenswarm.common import config as config_module

    channel, cm = FakeWebChannel(), FakeChannelManager()
    cm.configs["feishu"] = {"apps": []}
    path = tmp_path / "config.yaml"
    config_module.dump_yaml_round_trip(path, {"channels": {"feishu": {"apps": []}}})
    monkeypatch.setattr(config_module, "CONFIG_YAML_PATH", path)
    manager, host = make_manager(tmp_path, lambda: config_module.load_yaml_round_trip(path), setup_cli)
    initial = host.current_snapshot("local", "agent-a")
    server = object.__new__(_agent_websocket_server_type())
    server._feishu_agent_manager = manager
    responses = []

    async def send_wire(_ws, payload):
        responses.append(payload)

    monkeypatch.setattr("jiuwenswarm.server.agent_ws_server.send_wire_payload", send_wire)
    forwarded = []

    async def send_request(envelope):
        if envelope.method != "jiuwenmate.feishu.prepare":
            return SimpleNamespace(ok=True, payload={})
        forwarded.append(envelope)
        assert config_module.load_yaml_round_trip(path)["channels"]["feishu"]["apps"][0]["app_id"] == "app-a"
        await server.dispatch_parsed_request(object(), e2a_to_agent_request(envelope), asyncio.Lock())
        return SimpleNamespace(ok=True, payload={"status": "pending"})

    _register_web_handlers(WebHandlersBindParams(channel=channel, channel_manager=cm,
                                                 agent_client=SimpleNamespace(send_request=send_request)))
    try:
        await channel.methods["channel.feishu.bind_agent"](object(), "bind-1",
            {"app_id": "app-a", "app_secret": "test-secret", "persistent_agent_id": "agent-a"}, "session-a")
        assert forwarded, "Saved binding did not reach the AgentServer"
        assert forwarded[0].method == "jiuwenmate.feishu.prepare"
        assert forwarded[0].params == {"agent_id": "agent-a"}
        await ready(manager)
        assert manager.cli_runtime.status("local", "agent-a")["status"] == "ready"
        assert host.current_snapshot("local", "agent-a").config_version != initial.config_version
        assert responses and channel.responses[-1]["ok"]
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_restart_prepares_saved_bindings(tmp_path, setup_cli):
    manager, host = make_manager(tmp_path, lambda: config(), setup_cli)
    assert hasattr(manager, "prepare_feishu_cli"), "Manager does not prepare Feishu CLI"
    try:
        await manager.start()
        await ready(manager)
        assert manager.cli_runtime.status("local", "agent-a")["status"] == "ready"
        assert "lark-doc" in host.current_snapshot("local", "agent-a").load_spec().skills
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_unbound_channel_does_not_prepare_cli(tmp_path, setup_cli):
    manager, _ = make_manager(tmp_path, lambda: {"channels": {}}, setup_cli)
    assert hasattr(manager, "prepare_feishu_cli"), "Manager does not prepare Feishu CLI"
    try:
        await manager.start()
        await ready(manager)
        assert (await manager.prepare_feishu_cli("local", "agent-a"))["status"] == "unbound"
        assert setup_cli[2] == []
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_rebinding_during_preparation_schedules_the_new_identity(tmp_path, setup_cli):
    current = config()
    manager, host = make_manager(tmp_path, lambda: current, setup_cli)
    acquire = manager.cli_runtime._package
    entered, release = asyncio.Event(), asyncio.Event()
    first = True

    async def delayed(root):
        nonlocal first
        if first:
            first = False
            entered.set()
            await release.wait()
        return await acquire(root)

    manager.cli_runtime._package = delayed
    try:
        manager.schedule_feishu_cli("local", "agent-a")
        await asyncio.wait_for(entered.wait(), 5)
        current["channels"]["feishu"]["apps"][0]["app_id"] = "new-app"
        manager.schedule_feishu_cli("local", "agent-a")
        release.set()
        await ready(manager)
        assert manager.cli_runtime.status("local", "agent-a")["status"] == "ready"
        assert manager.cli_runtime.status("local", "agent-a")["app_id"] == "new-app"
        assert "lark-doc" in host.current_snapshot("local", "agent-a").load_spec().skills
    finally:
        release.set()
        await manager.aclose()


@pytest.mark.asyncio
async def test_restart_removes_unbound_capability_without_publishing_a_draft(tmp_path, setup_cli):
    current = config()
    manager, host = make_manager(tmp_path, lambda: current, setup_cli)
    try:
        await manager.prepare_feishu_cli("local", "agent-a")
        assert "lark-doc" in host.current_snapshot("local", "agent-a").load_spec().skills
        host.update_settings("local", "agent-a", replace(host.read_settings("local", "agent-a"), name="草稿"))
        current["channels"]["feishu"]["apps"].clear()
        await manager.start()
        await ready(manager)
        spec = host.current_snapshot("local", "agent-a").load_spec()
        assert "lark-doc" not in spec.skills
        assert spec.card.name != "草稿"
        assert host.get_colleague("local", "agent-a").has_unpublished_changes
        assert not (tmp_path / "runtime/local/agent-a/lark-cli/config").exists()
    finally:
        await manager.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["channel.feishu.bind_agent", "channel.feishu.set_conf"])
async def test_binding_changes_notify_both_previous_and_new_owner(tmp_path, monkeypatch, method):
    from jiuwenswarm.common import config as config_module

    channel, cm = FakeWebChannel(), FakeChannelManager()
    cm.configs["feishu"] = config()["channels"]["feishu"]
    path = tmp_path / "config.yaml"
    config_module.dump_yaml_round_trip(path, config())
    monkeypatch.setattr(config_module, "CONFIG_YAML_PATH", path)
    forwarded = []

    async def send_request(envelope):
        if envelope.method == "jiuwenmate.feishu.prepare":
            forwarded.append(envelope.params["agent_id"])
        return SimpleNamespace(ok=True, payload={})

    _register_web_handlers(WebHandlersBindParams(channel=channel, channel_manager=cm,
                                                 agent_client=SimpleNamespace(send_request=send_request)))
    app = config("agent-b")["channels"]["feishu"]["apps"][0]
    params = app if method.endswith("bind_agent") else {"apps": [app]}
    await channel.methods[method](object(), "bind-2", params, "session-a")
    assert channel.responses[-1]["ok"]
    assert set(forwarded) == {"agent-a", "agent-b"}


@pytest.mark.asyncio
async def test_personal_channel_changes_never_prepare_persistent_cli(tmp_path, monkeypatch):
    from jiuwenswarm.common import config as config_module

    channel, cm = FakeWebChannel(), FakeChannelManager()
    path = tmp_path / "config.yaml"
    config_module.dump_yaml_round_trip(path, {})
    monkeypatch.setattr(config_module, "CONFIG_YAML_PATH", path)
    forwarded = []

    async def send_request(envelope):
        forwarded.append(envelope.method)
        return SimpleNamespace(ok=True, payload={})

    _register_web_handlers(WebHandlersBindParams(channel=channel, channel_manager=cm,
                                                 agent_client=SimpleNamespace(send_request=send_request)))
    await channel.methods["channel.feishu.set_conf"](object(), "personal",
        {"apps": [{"app_id": "personal", "app_secret": "secret"}]}, "session-a")
    assert channel.responses[-1]["ok"]
    assert "jiuwenmate.feishu.prepare" not in forwarded


@pytest.mark.asyncio
async def test_saved_secret_rotation_notifies_the_bound_agent(tmp_path, monkeypatch):
    from jiuwenswarm.common import config as config_module

    channel, cm = FakeWebChannel(), FakeChannelManager()
    cm.configs["feishu"] = config()["channels"]["feishu"]
    path = tmp_path / "config.yaml"
    config_module.dump_yaml_round_trip(path, config())
    monkeypatch.setattr(config_module, "CONFIG_YAML_PATH", path)
    forwarded = []

    async def send_request(envelope):
        if envelope.method == "jiuwenmate.feishu.prepare":
            forwarded.append(envelope.params["agent_id"])
            saved = config_module.load_yaml_round_trip(path)["channels"]["feishu"]["apps"][0]
            assert saved["app_secret"] == "new-secret"
        return SimpleNamespace(ok=True, payload={})

    _register_web_handlers(WebHandlersBindParams(channel=channel, channel_manager=cm,
                                                 agent_client=SimpleNamespace(send_request=send_request)))
    await channel.methods["channel.feishu.set_conf"](object(), "rotation",
        config(secret="new-secret")["channels"]["feishu"], "session-a")
    assert channel.responses[-1]["ok"]
    assert forwarded == ["agent-a"]


@pytest.mark.asyncio
async def test_cli_failure_does_not_undo_saved_binding(tmp_path, monkeypatch):
    from jiuwenswarm.common import config as config_module

    channel, cm = FakeWebChannel(), FakeChannelManager()
    cm.configs["feishu"] = {"apps": []}
    path = tmp_path / "config.yaml"
    config_module.dump_yaml_round_trip(path, {})
    monkeypatch.setattr(config_module, "CONFIG_YAML_PATH", path)

    async def unavailable(_envelope):
        raise ConnectionError("AgentServer unavailable")

    _register_web_handlers(WebHandlersBindParams(channel=channel, channel_manager=cm,
                                                 agent_client=SimpleNamespace(send_request=unavailable)))
    await channel.methods["channel.feishu.bind_agent"](object(), "bind-1",
        {"app_id": "app-a", "app_secret": "test-secret", "persistent_agent_id": "agent-a"}, "session-a")
    assert channel.responses[-1]["ok"]
    assert config_module.load_yaml_round_trip(path)["channels"]["feishu"]["apps"][0]["persistent_agent_id"] == "agent-a"


@pytest.mark.asyncio
async def test_deleting_agent_removes_only_its_private_profile(tmp_path, setup_cli):
    current = config()
    current["channels"]["feishu"]["apps"] += config("agent-b", "app-b")["channels"]["feishu"]["apps"]
    manager, _ = make_manager(tmp_path, lambda: current, setup_cli)
    try:
        await manager.prepare_feishu_cli("local", "agent-a")
        await manager.cli_runtime.prepare("local", "agent-b")
        await manager.delete_colleague("local", "agent-a")
        assert not (tmp_path / "runtime/local/agent-a/lark-cli/config").exists()
        assert (tmp_path / "runtime/local/agent-b/lark-cli/config").is_dir()
        assert (tmp_path / "cli").is_dir()
    finally:
        await manager.aclose()
