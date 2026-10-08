"""Explicit restart waits for application and never hides backend failures."""

from types import SimpleNamespace
from types import MethodType
from unittest.mock import AsyncMock, Mock

import pytest

from jiuwenswarm.server import agent_ws_server as server_module
from jiuwenswarm.server.sandbox_config_rpc import apply_sandbox_policy_for_restart as apply_saved_policy


@pytest.fixture(autouse=True)
def saved_service_policy(monkeypatch):
    from jiuwenswarm.server import sandbox_config_rpc

    apply = AsyncMock(return_value=False)
    monkeypatch.setattr(sandbox_config_rpc, "apply_sandbox_policy_for_restart", apply)
    return apply


@pytest.mark.asyncio
async def test_network_application_failure_prevents_instance_recreation(monkeypatch, saved_service_policy):
    adapter = SimpleNamespace(apply_sandbox_runtime_patch=AsyncMock(return_value=True))
    server = SimpleNamespace(
        _agent_manager=SimpleNamespace(agents={"one": {"a": adapter}}),
        _resolve_adapter=lambda agent: agent,
    )
    monkeypatch.setattr(server_module, "get_sandbox_runtime", lambda: {"enabled": True})
    monkeypatch.setattr(server_module, "get_sandbox_endpoint", lambda: {"type": "jiuwenbox"})
    saved_service_policy.side_effect = RuntimeError("Network policy reload failed")
    with pytest.raises(RuntimeError, match="Network policy reload failed"):
        await server_module.AgentWebSocketServer._restart_configured_sandboxes(server, {})
    adapter.apply_sandbox_runtime_patch.assert_not_awaited()


@pytest.mark.asyncio
async def test_external_service_allows_file_rebuild_and_reports_network_ownership(monkeypatch):
    from jiuwenswarm.common import config
    from jiuwenswarm.server import sandbox_config_rpc as rpc
    from jiuwenswarm.common import net_guard_config

    monkeypatch.setattr(config, "get_sandbox_startup_mode", lambda: "external")
    monkeypatch.setattr(rpc, "apply_sandbox_policy_for_restart", apply_saved_policy)
    render = Mock(side_effect=AssertionError("must not mutate external service policy"))
    monkeypatch.setattr(net_guard_config, "render_saved_sandbox_urls", render)
    adapter = SimpleNamespace(apply_sandbox_runtime_patch=AsyncMock(return_value=True))
    server = SimpleNamespace(
        _agent_manager=SimpleNamespace(agents={"web": {"one": adapter}}),
        _resolve_adapter=lambda agent: agent,
    )
    monkeypatch.setattr(server_module, "get_sandbox_endpoint", lambda: {"type": "jiuwenbox"})
    monkeypatch.setattr(server_module, "get_sandbox_runtime", lambda: {"enabled": True, "files": []})
    result = await server_module.AgentWebSocketServer._restart_configured_sandboxes(server, {})
    assert result["restarted"] == 1
    assert result["network_status"] == "externally_managed"
    assert adapter.apply_sandbox_runtime_patch.await_count == 2
    render.assert_not_called()


@pytest.mark.asyncio
async def test_restart_applies_all_adapters_and_reports_failure(monkeypatch):
    first = SimpleNamespace(apply_sandbox_runtime_patch=AsyncMock(return_value=True))
    second = SimpleNamespace(apply_sandbox_runtime_patch=AsyncMock(return_value=True))
    server = SimpleNamespace(
        _agent_manager=SimpleNamespace(agents={"one": {"a": first}, "two": {"b": second}}),
        _resolve_adapter=lambda agent: agent,
    )
    runtime = {"enabled": True, "files": []}
    monkeypatch.setattr(server_module, "get_sandbox_runtime", lambda: runtime)
    monkeypatch.setattr(server_module, "get_sandbox_endpoint", lambda: {"type": "jiuwenbox"})
    restart = server_module.AgentWebSocketServer._restart_configured_sandboxes
    assert (await restart(server, {}))["restarted"] == 2
    assert first.apply_sandbox_runtime_patch.await_count == 2
    first.apply_sandbox_runtime_patch.assert_awaited_with(runtime, files_changed=True, strict=True)
    second.apply_sandbox_runtime_patch.side_effect = RuntimeError("backend unavailable")
    with pytest.raises(RuntimeError, match="backend unavailable"):
        await restart(server, {})
    # Preparation failure must not start any replacement ACL installation.
    assert first.apply_sandbox_runtime_patch.await_count == 3
    assert first.apply_sandbox_runtime_patch.await_args.kwargs["prepare_only"] is True


@pytest.mark.asyncio
async def test_restart_finds_session_owned_sandboxes_and_deduplicates(monkeypatch):
    launcher = SimpleNamespace()
    card = SimpleNamespace(gateway_config=SimpleNamespace(launcher_config=launcher))
    first = SimpleNamespace(_sys_operation_card=card, apply_sandbox_runtime_patch=AsyncMock(return_value=True))
    shared = SimpleNamespace(_sys_operation_card=card, apply_sandbox_runtime_patch=AsyncMock(return_value=True))
    root = SimpleNamespace(
        _sys_operation_card=None,
        _session_adapters={"one": first, "two": shared},
        apply_sandbox_runtime_patch=AsyncMock(return_value=False),
    )
    server = SimpleNamespace(
        _agent_manager=SimpleNamespace(agents={"web": {"agent": SimpleNamespace(_adapter=root)}}),
        _resolve_adapter=server_module.AgentWebSocketServer._resolve_adapter,
    )
    monkeypatch.setattr(server_module, "get_sandbox_runtime", lambda: {"enabled": True, "files": []})
    monkeypatch.setattr(server_module, "get_sandbox_endpoint", lambda: {"type": "jiuwenbox"})
    result = await server_module.AgentWebSocketServer._restart_configured_sandboxes(server, {})
    assert result["status"] == "applied"
    assert result["restarted"] == 1
    assert first.apply_sandbox_runtime_patch.await_count + shared.apply_sandbox_runtime_patch.await_count == 2


@pytest.mark.asyncio
async def test_restart_no_active_sandbox_is_not_reported_applied(monkeypatch):
    server = SimpleNamespace(_agent_manager=SimpleNamespace(agents={}))
    monkeypatch.setattr(server_module, "get_sandbox_runtime", lambda: {"enabled": True})
    monkeypatch.setattr(server_module, "get_sandbox_endpoint", lambda: {"type": "jiuwenbox"})
    result = await server_module.AgentWebSocketServer._restart_configured_sandboxes(server, {})
    assert result["status"] == "no_active_sandboxes"


@pytest.mark.asyncio
async def test_restart_rebinds_old_session_before_deduplicating(monkeypatch):
    from jiuwenswarm.common import config
    from jiuwenswarm.server.runtime.agent_adapter import interface_deep as mod
    from openjiuwen.extensions.sys_operation.sandbox.providers import jiuwenbox as box

    monkeypatch.setattr(config, "get_config", lambda: {})
    monkeypatch.setattr(mod, "build_filesystem_policy", lambda *a, **kw: ({}, []))
    cls = mod.JiuWenSwarmDeepAdapter
    old_launcher = SimpleNamespace(
        sandbox_type="jiuwenbox", base_url="http://127.0.0.1:8321", extra_params={},
    )
    current_launcher = SimpleNamespace(
        sandbox_type="jiuwenbox", base_url="http://127.0.0.1:58543", extra_params={},
    )
    gateway = SimpleNamespace(launcher_config=current_launcher)
    registered = SimpleNamespace(id="shared-op", _run_config=SimpleNamespace(config=gateway))
    monkeypatch.setattr(cls, "_get_registered_sys_operation_by_isolation_key",
                        staticmethod(lambda key: registered if key == "same-project" else None))
    adapters = []
    for launcher in (old_launcher, current_launcher):
        adapter = SimpleNamespace(
            _sys_operation_card=SimpleNamespace(
                id="shared-op", mode=mod.OperationMode.SANDBOX,
                gateway_config=SimpleNamespace(launcher_config=launcher),
            ),
            _sys_operation_isolation_key=lambda card: "same-project",
            _is_code_agent=False, _resolve_project_dir_for_sandbox=lambda: None,
        )
        adapter.refresh_sandbox_runtime_binding = MethodType(cls.refresh_sandbox_runtime_binding, adapter)
        adapter.apply_sandbox_runtime_patch = MethodType(cls.apply_sandbox_runtime_patch, adapter)
        adapters.append(adapter)
    create = AsyncMock(return_value="new-sandbox")
    monkeypatch.setattr(box, "force_recreate_jiuwenbox_sandbox", create)
    monkeypatch.setattr(box, "delete_jiuwenbox_sandbox", AsyncMock(return_value=[]))
    server = SimpleNamespace(
        _agent_manager=SimpleNamespace(agents={"web": dict(enumerate(adapters))}),
        _resolve_adapter=lambda agent: agent,
    )
    monkeypatch.setattr(server_module, "get_sandbox_runtime", lambda: {"enabled": True, "files": []})
    monkeypatch.setattr(server_module, "get_sandbox_endpoint", lambda: {"type": "jiuwenbox"})

    result = await server_module.AgentWebSocketServer._restart_configured_sandboxes(server, {})

    assert result["restarted"] == 1
    create.assert_awaited_once()
    assert create.await_args.args[0] == "http://127.0.0.1:58543"
    assert create.await_args.kwargs["shared_key"] == "http://127.0.0.1:58543|same-project"
    assert all(adapter._sys_operation_card.gateway_config is gateway for adapter in adapters)
    assert current_launcher.extra_params["sandbox_id"] == "new-sandbox"
    assert "sandbox_id" not in old_launcher.extra_params


@pytest.mark.asyncio
async def test_adapter_restart_uses_scoped_key_and_propagates_error(monkeypatch):
    from jiuwenswarm.common import config
    monkeypatch.setattr(config, "get_config", lambda: {})
    from jiuwenswarm.server.runtime.agent_adapter import interface_deep as adapter_module
    from openjiuwen.extensions.sys_operation.sandbox.providers import jiuwenbox

    launcher = SimpleNamespace(sandbox_type="jiuwenbox", base_url="http://localhost:8321", extra_params={})
    card = SimpleNamespace(mode=adapter_module.OperationMode.SANDBOX,
                           gateway_config=SimpleNamespace(launcher_config=launcher))
    adapter = SimpleNamespace(_sys_operation_card=card, _is_code_agent=False,
                              _resolve_project_dir_for_sandbox=lambda: None,
                              _sys_operation_isolation_key=lambda _: "scope-1")
    monkeypatch.setattr(adapter_module, "build_filesystem_policy", lambda *a, **k: ({"filesystem_policy": {}}, []))
    create = AsyncMock(return_value="new-id")
    monkeypatch.setattr(jiuwenbox, "force_recreate_jiuwenbox_sandbox", create)
    delete = AsyncMock(return_value=["new-id"])
    monkeypatch.setattr(jiuwenbox, "delete_jiuwenbox_sandbox", delete)
    calls = Mock()
    calls.attach_mock(delete, "delete")
    calls.attach_mock(create, "create")
    apply = adapter_module.JiuWenSwarmDeepAdapter.apply_sandbox_runtime_patch
    assert await apply(adapter, {"files": []}, files_changed=True, strict=True)
    assert create.await_args.kwargs["shared_key"] == "http://localhost:8321|scope-1"
    assert launcher.extra_params["sandbox_id"] == "new-id"
    import sys
    if sys.platform == "win32":
        assert [call[0] for call in calls.mock_calls] == ["delete", "create"]
    create.side_effect = RuntimeError("create failed")
    with pytest.raises(RuntimeError, match="create failed"):
        await apply(adapter, {"files": []}, files_changed=True, strict=True)


@pytest.mark.asyncio
async def test_all_old_acl_cleanup_precedes_any_replacement(monkeypatch):
    events = []

    def adapter(name):
        async def apply(runtime, *, files_changed, strict, prepare_only=False):
            events.append((name, "prepare" if prepare_only else "create"))
            return not prepare_only
        return SimpleNamespace(apply_sandbox_runtime_patch=apply)

    server = SimpleNamespace(
        _agent_manager=SimpleNamespace(agents={"test": {"a": adapter("a"), "b": adapter("b")}}),
        _resolve_adapter=lambda agent: agent,
    )
    monkeypatch.setattr(server_module, "get_sandbox_runtime", lambda: {"enabled": True, "files": []})
    monkeypatch.setattr(server_module, "get_sandbox_endpoint", lambda: {"type": "jiuwenbox"})
    await server_module.AgentWebSocketServer._restart_configured_sandboxes(server, {})
    assert events == [("a", "prepare"), ("b", "prepare"), ("a", "create"), ("b", "create")]


@pytest.mark.asyncio
async def test_reused_sysoperation_receives_restarted_policy(monkeypatch, tmp_path):
    from jiuwenswarm.common import config
    monkeypatch.setattr(config, "get_config", lambda: {})
    from jiuwenswarm.server.runtime.agent_adapter import interface_deep as mod
    from jiuwenswarm.server.runtime.agent_adapter import sysop_builder as builder
    from openjiuwen.extensions.sys_operation.sandbox.providers import jiuwenbox

    monkeypatch.setattr(builder, "_resolve_workspace_dir", lambda: None)
    monkeypatch.setattr(builder, "_resolve_project_dir", lambda _: None)
    monkeypatch.setattr(builder, "_resolve_config_ro_path", lambda: None)
    old_card = builder.create_sandbox_sysop_card("http://localhost:8321", "jiuwenbox", files_runtime=[])
    registered = mod.SysOperation(old_card)
    cls = mod.JiuWenSwarmDeepAdapter
    monkeypatch.setattr(cls, "_get_registered_sys_operation_by_isolation_key", staticmethod(lambda _: registered))
    monkeypatch.setattr(mod, "get_sandbox_endpoint", lambda: {"type": "jiuwenbox", "url": "http://localhost:8321"})
    monkeypatch.setattr(mod, "get_sandbox_runtime", lambda: {"enabled": True, "files": []})
    adapter = SimpleNamespace(
        _is_code_agent=False,
        _resolve_project_dir_for_sandbox=lambda: None,
        _create_sandbox_sys_operation=lambda url, kind, **kw: builder.create_sandbox_sysop_card(url, kind),
        _sys_operation_isolation_key=cls._sys_operation_isolation_key,
    )
    assert cls._resolve_sys_operation(adapter) is registered
    create = AsyncMock(return_value="restarted-id")
    monkeypatch.setattr(jiuwenbox, "force_recreate_jiuwenbox_sandbox", create)
    files = [{"path": str(tmp_path), "read": "allow", "write": "deny"}]
    assert await cls.apply_sandbox_runtime_patch(adapter, {"files": files}, files_changed=True, strict=True)
    actual = registered._run_config.config.launcher_config.extra_params
    assert actual["policy"]["filesystem_policy"]["read_only"] == [str(tmp_path)]
    assert actual["sandbox_id"] == "restarted-id"


@pytest.mark.asyncio
async def test_windows_second_create_failure_disables_host_fallback(monkeypatch):
    import sys
    from jiuwenswarm.common import config
    monkeypatch.setattr(config, "get_config", lambda: {})
    from jiuwenswarm.server.runtime.agent_adapter import interface_deep as mod
    from openjiuwen.extensions.sys_operation.sandbox.providers import jiuwenbox as box

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(mod, "build_filesystem_policy", lambda *a, **kw: ({"filesystem_policy": {}}, []))
    create = AsyncMock(side_effect=["new-one", RuntimeError("second create failed"), "new-three"])
    monkeypatch.setattr(box, "force_recreate_jiuwenbox_sandbox", create)
    async def delete(**kwargs):
        return [kwargs["sandbox_id"]] if kwargs["sandbox_id"] else []
    monkeypatch.setattr(box, "delete_jiuwenbox_sandbox", delete)
    children = []
    for name in ("one", "two", "three"):
        launcher = SimpleNamespace(sandbox_type="jiuwenbox", base_url="http://localhost:8321",
                                   extra_params={"sandbox_id": name, "fallback_on_failure": True})
        child = SimpleNamespace(
            _sys_operation_card=SimpleNamespace(mode=mod.OperationMode.SANDBOX,
                                                gateway_config=SimpleNamespace(launcher_config=launcher)),
            _is_code_agent=False, _resolve_project_dir_for_sandbox=lambda: None,
            _sys_operation_isolation_key=lambda card: str(id(card)),
        )
        child.apply_sandbox_runtime_patch = MethodType(mod.JiuWenSwarmDeepAdapter.apply_sandbox_runtime_patch, child)
        children.append(child)
    server = SimpleNamespace(_agent_manager=SimpleNamespace(agents={"web": dict(enumerate(children))}),
                             _resolve_adapter=lambda agent: agent)
    monkeypatch.setattr(server_module, "get_sandbox_runtime", lambda: {"enabled": True, "files": [], "fallback_on_failure": True})
    monkeypatch.setattr(server_module, "get_sandbox_endpoint", lambda: {"type": "jiuwenbox"})
    with pytest.raises(RuntimeError, match="restarted=2, failed=1.*second create failed"):
        await server_module.AgentWebSocketServer._restart_configured_sandboxes(server, {})
    extras = [child._sys_operation_card.gateway_config.launcher_config.extra_params for child in children]
    assert all(extra["fallback_on_failure"] is False for extra in extras)
    assert extras[0]["sandbox_id"] == "new-one"
    assert "sandbox_id" not in extras[1]
    assert extras[2]["sandbox_id"] == "new-three"
    assert create.await_count == 3
    create.side_effect = ["retry-one", "retry-two", "retry-three"]
    result = await server_module.AgentWebSocketServer._restart_configured_sandboxes(server, {})
    assert result["restarted"] == 3
    assert extras[1]["sandbox_id"] == "retry-two"


@pytest.mark.asyncio
async def test_session_restart_updates_policy_and_existing_provider_cache(monkeypatch, tmp_path):
    """Use real restart/adapter/provider logic; replace only the remote client."""
    from jiuwenswarm.common import config
    monkeypatch.setattr(config, "get_config", lambda: {})
    from jiuwenswarm.server.runtime.agent_adapter import interface_deep as mod
    from jiuwenswarm.server.runtime.agent_adapter import sysop_builder as builder
    from openjiuwen.extensions.sys_operation.sandbox.providers import jiuwenbox as box

    monkeypatch.setattr(builder, "_resolve_workspace_dir", lambda: None)
    monkeypatch.setattr(builder, "_resolve_project_dir", lambda _: None)
    monkeypatch.setattr(builder, "_resolve_config_ro_path", lambda: None)
    monkeypatch.delenv("JIUWENBOX_SANDBOX_ID", raising=False)
    mixin = box._JiuwenBoxProviderMixin
    monkeypatch.setattr(mixin, "_shared_sandbox_ids", {})
    monkeypatch.setattr(mixin, "_lifecycle_hooks", {})
    events = []
    policies = []

    class Client:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def create_sandbox(self, *, policy, policy_mode):
            policies.append(policy)
            sandbox_id = f"new-{len(policies)}"
            events.append(("create", sandbox_id))
            return sandbox_id

        def delete_sandbox(self, sandbox_id):
            events.append(("delete", sandbox_id))

    monkeypatch.setattr(box, "_JiuwenBoxClient", Client)
    card = builder.create_sandbox_sysop_card("http://localhost:8321", "jiuwenbox", files_runtime=[])
    child = SimpleNamespace(
        _sys_operation_card=card, _is_code_agent=False,
        _resolve_project_dir_for_sandbox=lambda: None,
        _sys_operation_isolation_key=lambda _: "test-session",
    )
    child.apply_sandbox_runtime_patch = MethodType(mod.JiuWenSwarmDeepAdapter.apply_sandbox_runtime_patch, child)
    root = SimpleNamespace(
        _sys_operation_card=None, _session_adapters={"session": child},
        apply_sandbox_runtime_patch=AsyncMock(return_value=False),
    )
    server = SimpleNamespace(
        _agent_manager=SimpleNamespace(agents={"web": {"agent": SimpleNamespace(_adapter=root)}}),
        _resolve_adapter=server_module.AgentWebSocketServer._resolve_adapter,
    )
    launcher = card.gateway_config.launcher_config
    launcher.extra_params["sandbox_id"] = "old"
    key = box.build_jiuwenbox_shared_scope_key(launcher.base_url, "test-session")
    mixin.register_shared_sandbox_id(key, "old")
    provider = mixin()
    provider.config = card.gateway_config
    provider.endpoint = SimpleNamespace(base_url=launcher.base_url, isolation_key="test-session")
    provider._sandbox_id = "old"
    runtime = {"enabled": True, "files": [{"path": str(tmp_path), "read": "allow", "write": "deny"}]}
    monkeypatch.setattr(server_module, "get_sandbox_runtime", lambda: runtime)
    monkeypatch.setattr(server_module, "get_sandbox_endpoint", lambda: {"type": "jiuwenbox"})
    restart = server_module.AgentWebSocketServer._restart_configured_sandboxes
    assert (await restart(server, {}))["restarted"] == 1
    assert provider._get_sandbox_id() == "new-1"
    assert mixin._shared_sandbox_ids[key] == "new-1"
    assert policies[0]["filesystem_policy"]["read_only"] == [str(tmp_path)]
    runtime["files"][0]["write"] = "allow"
    assert (await restart(server, {}))["restarted"] == 1
    assert provider._get_sandbox_id() == "new-2"
    assert policies[1]["filesystem_policy"]["read_write"] == [str(tmp_path)]
    assert ("delete", "old") in events
    assert ("delete", "new-1") in events
