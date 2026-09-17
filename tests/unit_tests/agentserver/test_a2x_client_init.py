from __future__ import annotations

import importlib
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter
from jiuwenswarm.server.runtime.agent_adapter import interface_deep as interface_module
from jiuwenswarm.agents.harness.team.a2x.a2x_registry_runtime import (
    clear_blank_registration_cache_for_tests,
    reserve_blank_teammate_agent,
    resolve_a2x_config,
    restore_teammate_blank_agent_on_destroy,
    register_teammate_blank_agent_at_startup,
)
from jiuwenswarm.agents.harness.team.a2x.client.errors import NotOwnedError


class _FakeAsyncA2XRegistryClient:
    instances: list["_FakeAsyncA2XRegistryClient"] = []

    def __init__(
        self,
        *,
        base_url: str,
        timeout: float,
        api_key: str | None,
        ownership_file,
    ) -> None:
        self.base_url = base_url
        self.timeout = timeout
        self.api_key = api_key
        self.ownership_file = ownership_file
        self.blank_registrations: list[dict[str, object]] = []
        self.card_replacements: list[dict[str, object]] = []
        self.reservations: list[dict[str, object]] = []
        self.released_reservations: list[str] = []
        self.closed = False
        self.__class__.instances.append(self)

    async def register_blank_agent(
        self,
        dataset: str,
        endpoint: str,
        service_id: str | None = None,
        persistent: bool = True,
    ):
        self.blank_registrations.append(
            {
                "dataset": dataset,
                "endpoint": endpoint,
                "service_id": service_id,
                "persistent": persistent,
            }
        )
        return SimpleNamespace(service_id="blank-service-id")

    async def replace_agent_card(
        self,
        dataset: str,
        service_id: str,
        agent_card: dict[str, object],
        release_lease: bool = True,
    ):
        self.card_replacements.append(
            {
                "dataset": dataset,
                "service_id": service_id,
                "agent_card": agent_card,
                "release_lease": release_lease,
            }
        )
        return SimpleNamespace(service_id=service_id)

    async def reserve_blank_agents(
        self,
        dataset: str,
        n: int = 1,
        ttl_seconds: int = 30,
        holder_id: str | None = None,
        extra_filters: dict[str, object] | None = None,
    ):
        self.reservations.append(
            {
                "dataset": dataset,
                "n": n,
                "ttl_seconds": ttl_seconds,
                "holder_id": holder_id,
                "extra_filters": extra_filters,
            }
        )
        return SimpleNamespace(
            holder_id="holder-1",
            agents=[
                {
                    "id": "blank-service-id",
                    "endpoint": "tcp://127.0.0.1:28610",
                    "status": "online",
                }
            ],
        )

    async def release_reservation(self, reservation) -> list[str]:
        self.released_reservations.append(reservation.holder_id)
        return ["blank-service-id"]

    async def aclose(self) -> None:
        self.closed = True


class _FailingAsyncA2XRegistryClient:
    def __init__(self, **_: object) -> None:
        raise RuntimeError("boom")


class _NotOwnedOnceAsyncA2XRegistryClient(_FakeAsyncA2XRegistryClient):
    async def replace_agent_card(
        self,
        dataset: str,
        service_id: str,
        agent_card: dict[str, object],
        release_lease: bool = True,
    ):
        if not self.blank_registrations and not self.card_replacements:
            raise NotOwnedError(dataset, service_id)
        return await super().replace_agent_card(dataset, service_id, agent_card, release_lease)


class _RegisterFailingAsyncA2XRegistryClient(_FakeAsyncA2XRegistryClient):
    async def register_blank_agent(
        self,
        dataset: str,
        endpoint: str,
        service_id: str | None = None,
        persistent: bool = True,
    ):
        raise RuntimeError("register failed")


def _make_config(role: str, *, dataset: str = "", endpoint: str = "") -> dict:
    return {
        "react": {
            "agent_name": "main_agent",
            "workspace_dir": "/tmp/test-workspace",
            "enable_task_loop": True,
            "max_iterations": 3,
            "a2x_registry": {
                "base_url": "http://127.0.0.1:8000",
                "timeout": 30.0,
                "api_key": "",
                "ownership_file": False,
                "role": role,
                "dataset": dataset,
                "endpoint": endpoint,
            },
        },
        "team": {
            "runtime": {
                "mode": "distributed",
                "role": "leader" if role == "teamleader" else role,
            },
        },
        "permissions": {"enabled": True},
    }


def test_teammate_a2x_endpoint_defaults_to_bootstrap_addr() -> None:
    config = _make_config("teammate", dataset="team_pool")
    config["team"] = {
        "transport": {
            "params": {
                "bootstrap_direct_addr": "tcp://0.0.0.0:28610",
            }
        }
    }

    resolved = resolve_a2x_config(config)

    assert resolved["endpoint"] == "tcp://127.0.0.1:28610"


@pytest.mark.asyncio
async def test_create_instance_registers_blank_agent_for_teammate(monkeypatch: pytest.MonkeyPatch) -> None:
    clear_blank_registration_cache_for_tests()
    _FakeAsyncA2XRegistryClient.instances.clear()
    fake_module = ModuleType("jiuwenswarm.agents.harness.team.a2x.client")
    fake_module.AsyncA2XRegistryClient = _FakeAsyncA2XRegistryClient

    adapter = JiuWenSwarmDeepAdapter()
    adapter.mark_as_session_scoped("test_session")
    config_base = _make_config(
        "teammate",
        dataset="team_dataset",
        endpoint="http://agent.example/ws",
    )

    monkeypatch.setitem(sys.modules, "jiuwenswarm.agents.harness.team.a2x.client", fake_module)
    monkeypatch.setattr(interface_module, "get_config", lambda: config_base)

    with (
        patch.object(interface_module.JiuWenSwarmDeepAdapter, "set_checkpoint", AsyncMock()),
        patch.object(interface_module.JiuWenSwarmDeepAdapter, "_refresh_multimodal_configs", return_value=None),
        patch.object(interface_module.JiuWenSwarmDeepAdapter, "_create_model", return_value=object()),
        patch.object(interface_module.JiuWenSwarmDeepAdapter, "_get_tool_cards", AsyncMock(return_value=[])),
        patch.object(interface_module.JiuWenSwarmDeepAdapter, "_build_agent_rails", return_value=[]),
        patch.object(interface_module.JiuWenSwarmDeepAdapter, "_create_sys_operation", return_value=MagicMock()),
        patch.object(interface_module.JiuWenSwarmDeepAdapter, "_build_configured_subagents",
                     return_value=(None, False)),
        patch.object(interface_module.JiuWenSwarmDeepAdapter, "load_user_rails", AsyncMock()),
        patch.object(interface_module, "init_permission_engine", return_value=None),
        patch.object(
            interface_module,
            "create_deep_agent",
            return_value=MagicMock(
                name="deep_agent",
                ensure_initialized=AsyncMock(),
            ),
        ),
    ):
        await adapter.create_instance()

    assert len(_FakeAsyncA2XRegistryClient.instances) == 1
    assert _FakeAsyncA2XRegistryClient.instances[0].blank_registrations == [
        {
            "dataset": "team_dataset",
            "endpoint": "http://agent.example/ws",
            "service_id": None,
            "persistent": True,
        }
    ]


@pytest.mark.asyncio
async def test_startup_registers_blank_agent_without_deepagent(monkeypatch: pytest.MonkeyPatch) -> None:
    clear_blank_registration_cache_for_tests()
    _FakeAsyncA2XRegistryClient.instances.clear()
    fake_module = ModuleType("jiuwenswarm.agents.harness.team.a2x.client")
    fake_module.AsyncA2XRegistryClient = _FakeAsyncA2XRegistryClient
    monkeypatch.setitem(sys.modules, "jiuwenswarm.agents.harness.team.a2x.client", fake_module)

    registered = await register_teammate_blank_agent_at_startup(
        _make_config(
            "teammate",
            dataset="team_pool",
            endpoint="tcp://127.0.0.1:28610",
        ),
        source="test-startup",
    )

    assert registered is True
    assert len(_FakeAsyncA2XRegistryClient.instances) == 1
    assert _FakeAsyncA2XRegistryClient.instances[0].closed is True
    assert _FakeAsyncA2XRegistryClient.instances[0].blank_registrations == [
        {
            "dataset": "team_pool",
            "endpoint": "tcp://127.0.0.1:28610",
            "service_id": None,
            "persistent": True,
        }
    ]


@pytest.mark.asyncio
async def test_teammate_destroy_restore_replaces_agent_card(monkeypatch: pytest.MonkeyPatch) -> None:
    clear_blank_registration_cache_for_tests()
    _FakeAsyncA2XRegistryClient.instances.clear()
    fake_module = ModuleType("jiuwenswarm.agents.harness.team.a2x.client")
    fake_module.AsyncA2XRegistryClient = _FakeAsyncA2XRegistryClient
    a2x_internal = importlib.import_module("jiuwenswarm.agents.harness.team.a2x.client._internal")
    setattr(fake_module, "_internal", a2x_internal)
    monkeypatch.setitem(sys.modules, "jiuwenswarm.agents.harness.team.a2x.client", fake_module)

    restored = await restore_teammate_blank_agent_on_destroy(
        _make_config(
            "teammate",
            dataset="team_pool",
            endpoint="tcp://127.0.0.1:28610",
        ),
        service_id="blank-service-id",
        source="test-destroy",
    )

    assert restored is True
    assert len(_FakeAsyncA2XRegistryClient.instances) == 1
    assert _FakeAsyncA2XRegistryClient.instances[0].closed is True
    assert _FakeAsyncA2XRegistryClient.instances[0].blank_registrations == []
    assert _FakeAsyncA2XRegistryClient.instances[0].card_replacements == [
        {
            "dataset": "team_pool",
            "service_id": "blank-service-id",
            "agent_card": {
                "name": "_BlankAgent_tcp://127.0.0.1:28610",
                "description": "__BLANK__",
                "endpoint": "tcp://127.0.0.1:28610",
                "status": "online",
            },
            "release_lease": True,
        }
    ]


@pytest.mark.asyncio
async def test_teammate_destroy_restore_recovers_missing_ownership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clear_blank_registration_cache_for_tests()
    _NotOwnedOnceAsyncA2XRegistryClient.instances.clear()
    fake_module = ModuleType("jiuwenswarm.agents.harness.team.a2x.client")
    fake_module.AsyncA2XRegistryClient = _NotOwnedOnceAsyncA2XRegistryClient
    a2x_internal = importlib.import_module("jiuwenswarm.agents.harness.team.a2x.client._internal")
    setattr(fake_module, "_internal", a2x_internal)
    monkeypatch.setitem(sys.modules, "jiuwenswarm.agents.harness.team.a2x.client", fake_module)

    restored = await restore_teammate_blank_agent_on_destroy(
        _make_config(
            "teammate",
            dataset="team_pool",
            endpoint="tcp://127.0.0.1:28610",
        ),
        service_id="blank-service-id",
        source="test-destroy",
    )

    assert restored is True
    client = _NotOwnedOnceAsyncA2XRegistryClient.instances[0]
    assert client.blank_registrations == [
        {
            "dataset": "team_pool",
            "endpoint": "tcp://127.0.0.1:28610",
            "service_id": "blank-service-id",
            "persistent": True,
        }
    ]
    assert client.card_replacements[0]["service_id"] == "blank-service-id"
    assert client.card_replacements[0]["release_lease"] is True


@pytest.mark.asyncio
async def test_leader_reserves_blank_teammate_from_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    clear_blank_registration_cache_for_tests()
    _FakeAsyncA2XRegistryClient.instances.clear()
    fake_module = ModuleType("jiuwenswarm.agents.harness.team.a2x.client")
    fake_module.AsyncA2XRegistryClient = _FakeAsyncA2XRegistryClient
    monkeypatch.setitem(sys.modules, "jiuwenswarm.agents.harness.team.a2x.client", fake_module)

    reserved = await reserve_blank_teammate_agent(
        _make_config("teamleader", dataset="team_pool"),
        source="test-leader",
    )

    assert reserved is not None
    assert reserved.service_id == "blank-service-id"
    assert reserved.endpoint == "tcp://127.0.0.1:28610"
    assert _FakeAsyncA2XRegistryClient.instances[0].reservations == [
        {
            "dataset": "team_pool",
            "n": 1,
            "ttl_seconds": 30,
            "holder_id": None,
            "extra_filters": None,
        }
    ]
    await reserved.release()
    await reserved.close()
    assert _FakeAsyncA2XRegistryClient.instances[0].released_reservations == ["holder-1"]
    assert _FakeAsyncA2XRegistryClient.instances[0].closed is True


@pytest.mark.asyncio
async def test_create_instance_continues_when_a2x_client_init_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_module = ModuleType("jiuwenswarm.agents.harness.team.a2x.client")
    fake_module.AsyncA2XRegistryClient = _FailingAsyncA2XRegistryClient

    adapter = JiuWenSwarmDeepAdapter()
    # Only a session-scoped adapter builds its own DeepAgent; the root adapter
    # defers that to ``ensure_instance`` so the chat path does not pay for it.
    adapter.mark_as_session_scoped("sess_a2x_test")
    config_base = _make_config("teamleader")

    monkeypatch.setitem(sys.modules, "jiuwenswarm.agents.harness.team.a2x.client", fake_module)
    monkeypatch.setattr(interface_module, "get_config", lambda: config_base)

    created_instance = MagicMock(name="deep_agent", ensure_initialized=AsyncMock())

    with (
        patch.object(interface_module.JiuWenSwarmDeepAdapter, "set_checkpoint", AsyncMock()),
        patch.object(interface_module.JiuWenSwarmDeepAdapter, "_refresh_multimodal_configs", return_value=None),
        patch.object(interface_module.JiuWenSwarmDeepAdapter, "_create_model", return_value=object()),
        patch.object(interface_module.JiuWenSwarmDeepAdapter, "_get_tool_cards", AsyncMock(return_value=[])),
        patch.object(interface_module.JiuWenSwarmDeepAdapter, "_build_agent_rails", return_value=[]),
        patch.object(interface_module.JiuWenSwarmDeepAdapter, "_create_sys_operation", return_value=MagicMock()),
        patch.object(interface_module.JiuWenSwarmDeepAdapter, "_build_configured_subagents",
                     return_value=(None, False)),
        patch.object(interface_module.JiuWenSwarmDeepAdapter, "load_user_rails", AsyncMock()),
        patch.object(interface_module, "init_permission_engine", return_value=None),
        patch.object(interface_module, "create_deep_agent", return_value=created_instance) as create_agent_mock,
    ):
        await adapter.create_instance()

    create_agent_mock.assert_called_once()
    kwargs = create_agent_mock.call_args.kwargs
    assert "runtime_cwd" not in kwargs
    assert "project_root" not in kwargs
    assert kwargs["enable_read_image_multimodal"] is None


@pytest.mark.asyncio
async def test_reload_reuses_existing_a2x_client_when_config_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeAsyncA2XRegistryClient.instances.clear()
    fake_module = ModuleType("jiuwenswarm.agents.harness.team.a2x.client")
    fake_module.AsyncA2XRegistryClient = _FakeAsyncA2XRegistryClient

    config_base = _make_config(
        "teammate",
        dataset="team_pool",
        endpoint="tcp://127.0.0.1:28610",
    )
    adapter = JiuWenSwarmDeepAdapter()
    existing = _FakeAsyncA2XRegistryClient(
        base_url="http://127.0.0.1:8000",
        timeout=30.0,
        api_key=None,
        ownership_file=False,
    )
    adapter._a2x_client = existing
    adapter._a2x_config = adapter._get_a2x_config(config_base)

    monkeypatch.setitem(sys.modules, "jiuwenswarm.agents.harness.team.a2x.client", fake_module)

    await adapter._try_init_a2x_client(config_base, reload=True)

    assert _FakeAsyncA2XRegistryClient.instances == [existing]
    assert existing.closed is False
    assert adapter._a2x_client is existing


@pytest.mark.asyncio
async def test_reload_keeps_existing_a2x_client_when_unchanged_registration_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _RegisterFailingAsyncA2XRegistryClient.instances.clear()
    fake_module = ModuleType("jiuwenswarm.agents.harness.team.a2x.client")
    fake_module.AsyncA2XRegistryClient = _RegisterFailingAsyncA2XRegistryClient

    config_base = _make_config(
        "teammate",
        dataset="team_pool",
        endpoint="tcp://127.0.0.1:28610",
    )
    adapter = JiuWenSwarmDeepAdapter()
    existing = _RegisterFailingAsyncA2XRegistryClient(
        base_url="http://127.0.0.1:8000",
        timeout=30.0,
        api_key=None,
        ownership_file=False,
    )
    adapter._a2x_client = existing
    adapter._a2x_config = adapter._get_a2x_config(config_base)

    monkeypatch.setitem(sys.modules, "jiuwenswarm.agents.harness.team.a2x.client", fake_module)

    await adapter._try_init_a2x_client(config_base, reload=True)

    assert _RegisterFailingAsyncA2XRegistryClient.instances == [existing]
    assert existing.closed is False
    assert adapter._a2x_client is existing


def test_make_deep_agent_config_keeps_read_image_multimodal_without_vision_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = JiuWenSwarmDeepAdapter()
    # Only a session-scoped adapter builds its own DeepAgent; the root adapter
    # defers that to ``ensure_instance`` so the chat path does not pay for it.
    adapter.mark_as_session_scoped("sess_a2x_test")
    config_base = _make_config("teamleader")
    monkeypatch.setattr(interface_module, "get_config", lambda: config_base)

    with patch.object(
        interface_module.JiuWenSwarmDeepAdapter,
        "_build_configured_subagents",
        return_value=(None, False),
    ):
        deep_cfg = adapter._make_deep_agent_config(
            model=object(),
            config=config_base["react"],
            agent_card=MagicMock(),
            tool_cards=[],
            rails=[],
        )

    assert deep_cfg.enable_read_image_multimodal is None


@pytest.mark.parametrize(
    ("configured_timeout", "expected_timeout"),
    [(None, None), (7200, 7200.0)],
)
def test_make_deep_agent_config_resolves_completion_timeout(
    monkeypatch: pytest.MonkeyPatch,
    configured_timeout: float | None,
    expected_timeout: float | None,
) -> None:
    adapter = JiuWenSwarmDeepAdapter()
    config_base = _make_config("teamleader")
    config_base["react"].pop("completion_timeout", None)
    if configured_timeout is not None:
        config_base["react"]["completion_timeout"] = configured_timeout
    monkeypatch.setattr(interface_module, "get_config", lambda: config_base)

    with patch.object(
        interface_module.JiuWenSwarmDeepAdapter,
        "_build_configured_subagents",
        return_value=(None, False),
    ):
        deep_cfg = adapter._make_deep_agent_config(
            model=object(),
            config=config_base["react"],
            agent_card=MagicMock(),
            tool_cards=[],
            rails=[],
        )

    assert deep_cfg.completion_timeout == expected_timeout


def test_make_deep_agent_config_disables_read_image_multimodal_with_vision_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = JiuWenSwarmDeepAdapter()
    adapter._vision_model_config = interface_module.VisionModelConfig(
        api_key="vision-key",
        base_url="https://vision.example/v1",
        model="vision-model",
    )
    config_base = _make_config("teamleader")
    monkeypatch.setattr(interface_module, "get_config", lambda: config_base)

    with patch.object(
        interface_module.JiuWenSwarmDeepAdapter,
        "_build_configured_subagents",
        return_value=(None, False),
    ):
        deep_cfg = adapter._make_deep_agent_config(
            model=object(),
            config=config_base["react"],
            agent_card=MagicMock(),
            tool_cards=[],
            rails=[],
        )

    assert deep_cfg.enable_read_image_multimodal is False


@pytest.mark.asyncio
async def test_create_instance_keeps_workspace_root_separate_from_project_dir(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    adapter = JiuWenSwarmDeepAdapter()
    # Only a session-scoped adapter builds its own DeepAgent; the root adapter
    # defers that to ``ensure_instance`` so the chat path does not pay for it.
    adapter.mark_as_session_scoped("sess_a2x_workspace_test")
    workspace_dir = tmp_path / "workspace"
    project_dir = tmp_path / "project"
    workspace_dir.mkdir()
    project_dir.mkdir()
    config_base = _make_config("teamleader")
    config_base["react"]["workspace_dir"] = str(workspace_dir)

    monkeypatch.setattr(interface_module, "get_config", lambda: config_base)
    created_instance = MagicMock(name="deep_agent", ensure_initialized=AsyncMock())

    with (
        patch.object(interface_module.JiuWenSwarmDeepAdapter, "set_checkpoint", AsyncMock()),
        patch.object(interface_module.JiuWenSwarmDeepAdapter, "_refresh_multimodal_configs", return_value=None),
        patch.object(interface_module.JiuWenSwarmDeepAdapter, "_create_model", return_value=object()),
        patch.object(interface_module.JiuWenSwarmDeepAdapter, "_try_init_a2x_client", AsyncMock()),
        patch.object(interface_module.JiuWenSwarmDeepAdapter, "_get_tool_cards", AsyncMock(return_value=[])),
        patch.object(interface_module.JiuWenSwarmDeepAdapter, "_build_agent_rails", return_value=[]),
        patch.object(interface_module.JiuWenSwarmDeepAdapter, "_create_sys_operation", return_value=MagicMock()),
        patch.object(interface_module.JiuWenSwarmDeepAdapter, "_build_configured_subagents",
                     return_value=(None, False)),
        patch.object(interface_module.JiuWenSwarmDeepAdapter, "load_user_rails", AsyncMock()),
        patch.object(interface_module, "create_deep_agent", return_value=created_instance) as create_agent_mock,
    ):
        await adapter.create_instance({"project_dir": str(project_dir)})

    workspace = create_agent_mock.call_args.kwargs["workspace"]
    assert workspace.root_path == str(workspace_dir)
    assert adapter._project_dir == str(project_dir)
