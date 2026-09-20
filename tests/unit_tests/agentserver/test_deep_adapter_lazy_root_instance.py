# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for the root adapter's on-demand DeepAgent construction."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jiuwenswarm.server.runtime.agent_adapter import interface_deep as interface_deep_module
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter


def _make_adapter(session_id: str | None) -> JiuWenSwarmDeepAdapter:
    """Create a bare adapter carrying only the lazy-build bookkeeping state."""
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._instance = None
    adapter._is_session_scoped_adapter = session_id is not None
    adapter._parent_session_id = session_id
    adapter._root_instance_requested = False
    adapter._root_instance_lock = None
    adapter._config_base_cache = None
    adapter._session_instance_config = {"agent_name": "main_agent"}
    adapter._session_instance_mode = "agent"
    adapter._session_instance_sub_mode = None
    return adapter


def _stub_create_instance(adapter: JiuWenSwarmDeepAdapter, calls: list[tuple[str, object]]) -> None:
    """Replace ``create_instance`` with a slow builder that records its calls."""

    async def _create_instance(config=None, *, mode="agent", sub_mode=None, config_base=None):
        calls.append((mode, config_base))
        # Yield control so a concurrent waiter can interleave if the lock is
        # missing; without an await the race would be untestable.
        await asyncio.sleep(0)
        adapter._instance = object()

    adapter.create_instance = _create_instance


def test_root_adapter_skips_its_own_instance_build() -> None:
    """A root adapter defers building until someone actually asks for a handle."""
    adapter = _make_adapter(None)

    assert adapter._skip_own_instance_build() is True


def test_session_adapter_always_builds_its_instance() -> None:
    """Session adapters own the live DeepAgent, so they never defer."""
    adapter = _make_adapter("sess_a")

    assert adapter._skip_own_instance_build() is False


def test_root_adapter_stops_skipping_once_requested() -> None:
    """After a build is requested the root adapter builds eagerly on rebuilds."""
    adapter = _make_adapter(None)
    adapter._root_instance_requested = True

    assert adapter._skip_own_instance_build() is False


@pytest.mark.asyncio
async def test_ensure_instance_builds_the_root_agent_once() -> None:
    """The first call builds; later calls hand back the same instance."""
    adapter = _make_adapter(None)
    calls: list[tuple[str, object]] = []
    _stub_create_instance(adapter, calls)

    first = await adapter.ensure_instance()
    second = await adapter.ensure_instance()

    assert first is second
    assert calls == [("agent", None)]


@pytest.mark.asyncio
async def test_concurrent_ensure_instance_builds_only_once() -> None:
    """Parallel callers must not each build a DeepAgent.

    Two builds would register two sets of tools under the same owner id, which
    is exactly the duplicate-registration state the owner scoping prevents.
    """
    adapter = _make_adapter(None)
    calls: list[tuple[str, object]] = []
    _stub_create_instance(adapter, calls)

    results = await asyncio.gather(*(adapter.ensure_instance() for _ in range(5)))

    assert len(calls) == 1
    assert len({id(item) for item in results}) == 1


@pytest.mark.asyncio
async def test_ensure_instance_returns_existing_instance_without_building() -> None:
    """A session adapter already holds an instance, so nothing is rebuilt."""
    adapter = _make_adapter("sess_a")
    existing = object()
    adapter._instance = existing
    calls: list[tuple[str, object]] = []
    _stub_create_instance(adapter, calls)

    assert await adapter.ensure_instance() is existing
    assert calls == []


@pytest.mark.asyncio
async def test_ensure_instance_preserves_the_configured_mode() -> None:
    """The deferred build must reuse the mode create_instance was called with."""
    adapter = _make_adapter(None)
    adapter._session_instance_mode = "code"
    adapter._session_instance_sub_mode = "plan"
    calls: list[tuple[str, object]] = []
    _stub_create_instance(adapter, calls)

    await adapter.ensure_instance()

    assert calls == [("code", None)]


@pytest.mark.asyncio
async def test_ensure_instance_reuses_authoritative_config_snapshot() -> None:
    """A deferred root build must not fall back to config.yaml."""
    adapter = _make_adapter(None)
    tenant_config = {"models": {"defaults": [{"model": "tenant-model"}]}}
    adapter._config_base_cache = tenant_config
    calls: list[tuple[str, object]] = []
    _stub_create_instance(adapter, calls)

    await adapter.ensure_instance()

    assert calls == [("agent", tenant_config)]


def test_instance_config_base_falls_back_to_native_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deep and Code adapters share the native config.yaml fallback."""
    disk_config = {"models": {"defaults": [{"model": "disk-model"}]}}
    monkeypatch.setattr(interface_deep_module, "get_config", lambda: disk_config)

    assert interface_deep_module._resolve_instance_config_base(None) is disk_config


@pytest.mark.asyncio
async def test_session_child_reuses_authoritative_config_snapshot() -> None:
    """The executing session child must receive the root tenant snapshot."""
    adapter = _make_adapter(None)
    tenant_config = {"models": {"defaults": [{"model": "tenant-model"}]}}
    adapter._config_base_cache = tenant_config
    adapter._session_adapters = {}
    adapter._session_adapter_locks = {}
    calls: list[object] = []

    class FakeChild:
        async def create_instance(self, _config=None, **kwargs):
            calls.append(kwargs.get("config_base"))

        async def start_interaction(self, session_id=None):
            return None

    child = FakeChild()
    adapter._new_session_scoped_adapter = lambda _sid: child

    async def _reload_noop(_sid, _child):
        return None

    adapter._reload_session_adapter_if_stale = _reload_noop
    adapter._touch_session_adapter = lambda _sid: None

    assert await adapter._get_or_create_session_adapter("sess_a") is child
    assert calls == [tenant_config]


@pytest.mark.asyncio
async def test_enterprise_request_rebuilds_session_child_created_without_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A requestless helper must not leave the live session on YAML permissions."""
    monkeypatch.setattr(interface_deep_module, "is_enterprise", lambda: True)
    parent = JiuWenSwarmDeepAdapter()

    class FakeChild:
        def __init__(self, resource_id: str | None = None) -> None:
            self._enterprise_config_resource_id = resource_id
            self.cleaned = False
            self.created_with = None

        async def cleanup(self) -> None:
            self.cleaned = True

        async def create_instance(self, config=None, **_kwargs) -> None:
            self.created_with = config

        async def start_interaction(self, session_id=None) -> None:
            del session_id

    stale = FakeChild()
    fresh = FakeChild()
    parent._session_adapters = {"sess_a": stale}
    parent._new_session_scoped_adapter = lambda _sid: fresh

    async def _reload_noop(_sid, _child) -> None:
        return None

    parent._reload_session_adapter_if_stale = _reload_noop
    parent._touch_session_adapter = lambda _sid: None
    request = SimpleNamespace(
        metadata={"routing": {"bot_id": "agent-resource-1"}},
    )

    result = await parent._get_or_create_session_adapter(
        "sess_a",
        request=request,
    )

    assert result is fresh
    assert stale.cleaned is True
    assert fresh.created_with["request"] is request


def _complete_video_config() -> dict:
    return {
        "models": {
            "video": {
                "model_config": {
                    "api_key": "k",
                    "api_base": "http://video.example",
                    "model_name": "video-model",
                }
            }
        }
    }


def test_build_video_model_config_does_not_apply_when_yaml_incomplete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unconfigured video must not pay apply_video_from_yaml (embed/env fallback)."""
    applied: list[object] = []
    monkeypatch.setattr(
        interface_deep_module,
        "apply_video_model_config_from_yaml",
        lambda cfg: applied.append(cfg),
    )

    assert JiuWenSwarmDeepAdapter._build_video_model_config({}) is False
    assert applied == []


def test_build_video_model_config_applies_when_yaml_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    applied: list[object] = []
    monkeypatch.setattr(
        interface_deep_module,
        "apply_video_model_config_from_yaml",
        lambda cfg: applied.append(True),
    )
    monkeypatch.setattr(
        interface_deep_module,
        "read_env",
        lambda key, default="": {
            "VIDEO_API_KEY": "k",
            "VIDEO_API_BASE": "http://video.example",
            "VIDEO_MODEL_NAME": "video-model",
        }.get(key, default),
    )

    assert JiuWenSwarmDeepAdapter._build_video_model_config(_complete_video_config()) is True
    assert applied == [True]


def test_build_image_gen_model_config_still_applies_before_key_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """image_gen keeps embed/main-API fallback: apply still runs without dedicated yaml."""
    applied: list[object] = []
    monkeypatch.setattr(
        interface_deep_module,
        "apply_image_gen_model_config_from_yaml",
        lambda cfg: applied.append(True),
    )
    monkeypatch.setattr(interface_deep_module, "read_env", lambda key, default="": "")

    assert JiuWenSwarmDeepAdapter._build_image_gen_model_config({}) is False
    assert applied == [True]


@contextmanager
def _stub_create_instance_build():
    """Stub the DeepAgent half of create_instance (after preamble / skip)."""
    created = MagicMock(name="deep_agent", ensure_initialized=AsyncMock())
    adapter_cls = interface_deep_module.JiuWenSwarmDeepAdapter
    with (
        patch.object(adapter_cls, "set_checkpoint", AsyncMock()),
        patch.object(adapter_cls, "_create_model", return_value=object()),
        patch.object(adapter_cls, "_get_tool_cards", AsyncMock(return_value=[])),
        patch.object(adapter_cls, "_build_agent_rails", return_value=[]),
        patch.object(adapter_cls, "_create_sys_operation", return_value=MagicMock()),
        patch.object(adapter_cls, "_build_configured_subagents", return_value=(None, False)),
        patch.object(adapter_cls, "load_user_rails", AsyncMock()),
        patch.object(adapter_cls, "_try_init_a2x_client", AsyncMock()),
        patch.object(interface_deep_module, "create_deep_agent", return_value=created),
    ):
        yield created


@pytest.mark.asyncio
async def test_root_create_instance_skips_multimodal_skill_and_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Root chat-path create_instance must stop before multimodal/skill/prompt."""
    adapter = JiuWenSwarmDeepAdapter()
    config_base = {"react": {"agent_name": "main_agent"}}
    monkeypatch.setattr(interface_deep_module, "get_config", lambda: config_base)
    refresh = MagicMock()
    loader_cls = MagicMock()
    sync_cls = MagicMock()
    adapter._enterprise_config = SimpleNamespace(skill_prebuilt=[{"name": "x"}])
    monkeypatch.setattr(adapter, "_merge_enterprise_models_into_config", lambda cfg: cfg)

    with (
        patch.object(
            interface_deep_module.JiuWenSwarmDeepAdapter,
            "set_checkpoint",
            AsyncMock(),
        ),
        patch.object(
            interface_deep_module.JiuWenSwarmDeepAdapter,
            "_refresh_multimodal_configs",
            refresh,
        ),
        patch.object(interface_deep_module, "PromptAttachmentLoader", loader_cls),
        patch.object(interface_deep_module, "SkillPrebuiltSynchronizer", sync_cls),
        patch.object(interface_deep_module, "is_skill_prebuilt_tenant", return_value=True),
    ):
        await adapter.create_instance(config_base=config_base)

    refresh.assert_not_called()
    loader_cls.assert_not_called()
    sync_cls.assert_not_called()
    assert adapter._instance is None
    assert adapter._config_base_cache["react"]["agent_name"] == "main_agent"
    assert adapter._agent_name == "main_agent"


@pytest.mark.asyncio
async def test_root_create_instance_skip_applies_project_and_workspace_overrides(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """Skip still runs the cheap override block moved ahead of skill sync."""
    constructor_ws = str(tmp_path / "constructor-workspace")
    request_ws = str(tmp_path / "request-workspace")
    project_dir = str(tmp_path / "project")
    adapter = JiuWenSwarmDeepAdapter()
    adapter._workspace_dir = constructor_ws
    config_base = {"react": {"agent_name": "main_agent"}}
    monkeypatch.setattr(interface_deep_module, "get_config", lambda: config_base)
    refresh = MagicMock()
    loader_cls = MagicMock()
    sync_cls = MagicMock()
    adapter._enterprise_config = SimpleNamespace(skill_prebuilt=[{"name": "x"}])
    monkeypatch.setattr(adapter, "_merge_enterprise_models_into_config", lambda cfg: cfg)

    with (
        patch.object(
            interface_deep_module.JiuWenSwarmDeepAdapter,
            "set_checkpoint",
            AsyncMock(),
        ),
        patch.object(
            interface_deep_module.JiuWenSwarmDeepAdapter,
            "_refresh_multimodal_configs",
            refresh,
        ),
        patch.object(interface_deep_module, "PromptAttachmentLoader", loader_cls),
        patch.object(interface_deep_module, "SkillPrebuiltSynchronizer", sync_cls),
        patch.object(interface_deep_module, "is_skill_prebuilt_tenant", return_value=True),
    ):
        await adapter.create_instance(
            {"project_dir": project_dir, "workspace_dir": request_ws},
            config_base=config_base,
        )

    refresh.assert_not_called()
    loader_cls.assert_not_called()
    sync_cls.assert_not_called()
    assert adapter._instance is None
    assert adapter._project_dir == project_dir
    assert adapter._workspace_dir == request_ws


@pytest.mark.asyncio
async def test_session_create_instance_still_refreshes_multimodal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Session adapters still run multimodal / prompt layout before DeepAgent."""
    adapter = JiuWenSwarmDeepAdapter()
    adapter.mark_as_session_scoped("sess_preamble")
    config_base = {"react": {"agent_name": "main_agent"}}
    monkeypatch.setattr(interface_deep_module, "get_config", lambda: config_base)
    refresh = MagicMock()
    loader = MagicMock()
    loader.ensure_layout = MagicMock()
    loader_cls = MagicMock(return_value=loader)

    with (
        _stub_create_instance_build(),
        patch.object(
            interface_deep_module.JiuWenSwarmDeepAdapter,
            "_refresh_multimodal_configs",
            refresh,
        ),
        patch.object(interface_deep_module, "PromptAttachmentLoader", loader_cls),
    ):
        await adapter.create_instance(config_base=config_base)

    refresh.assert_called_once()
    loader_cls.assert_called_once()
    loader.ensure_layout.assert_called_once()
    assert adapter._instance is not None


@pytest.mark.asyncio
async def test_ensure_instance_after_root_skip_runs_preamble(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Skip then ensure_instance must rebuild via the cached snapshot, not yaml."""
    adapter = JiuWenSwarmDeepAdapter()
    yaml_config = {"react": {"agent_name": "from-yaml"}}
    snapshot = {"react": {"agent_name": "from-snapshot"}}
    monkeypatch.setattr(interface_deep_module, "get_config", lambda: yaml_config)
    refresh = MagicMock()

    with (
        _stub_create_instance_build(),
        patch.object(
            interface_deep_module.JiuWenSwarmDeepAdapter,
            "_refresh_multimodal_configs",
            refresh,
        ),
        patch.object(
            interface_deep_module,
            "PromptAttachmentLoader",
            return_value=MagicMock(ensure_layout=MagicMock()),
        ),
    ):
        await adapter.create_instance(
            {"agent_name": "cached-agent"},
            config_base=snapshot,
        )
        assert adapter._instance is None
        refresh.assert_not_called()

        instance = await adapter.ensure_instance()

    refresh.assert_called_once()
    assert refresh.call_args.args[0]["react"]["agent_name"] == "from-snapshot"
    assert instance is adapter._instance
    assert adapter._instance is not None
    assert adapter._agent_name == "cached-agent"


def _stub_skill_prebuilt_synchronizer() -> tuple[MagicMock, MagicMock]:
    """Return (cls, instance) so tests can assert the workspace passed to sync."""
    sync_result = SimpleNamespace(
        errors=[],
        enabled_skill_dirs=None,
        prebuilt_skill_dirs=[],
    )
    sync_obj = MagicMock()
    sync_obj.sync = AsyncMock(return_value=sync_result)
    sync_cls = MagicMock(return_value=sync_obj)
    return sync_cls, sync_obj


@pytest.mark.asyncio
async def test_session_skill_prebuilt_sync_uses_create_instance_workspace_dir(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """create_instance(config.workspace_dir) is the SkillPrebuiltSynchronizer target.

    Constructor tenant workspace is kept only when that override is absent.
    Chat params.workspace_dir is a different path (mapped to project_dir) and
    is not covered here.
    """
    constructor_ws = str(tmp_path / "constructor-workspace")
    request_ws = str(tmp_path / "request-workspace")
    adapter = JiuWenSwarmDeepAdapter()
    adapter._workspace_dir = constructor_ws
    adapter.mark_as_session_scoped("sess_skill_sync_workspace")
    adapter._enterprise_config = SimpleNamespace(skill_prebuilt=[{"skill_id": "prebuilt-x"}])
    monkeypatch.setattr(adapter, "_merge_enterprise_models_into_config", lambda cfg: cfg)
    config_base = {"react": {"agent_name": "main_agent"}}
    monkeypatch.setattr(interface_deep_module, "get_config", lambda: config_base)
    sync_cls, _sync_obj = _stub_skill_prebuilt_synchronizer()

    with (
        _stub_create_instance_build(),
        patch.object(
            interface_deep_module.JiuWenSwarmDeepAdapter,
            "_refresh_multimodal_configs",
            MagicMock(),
        ),
        patch.object(
            interface_deep_module,
            "PromptAttachmentLoader",
            return_value=MagicMock(ensure_layout=MagicMock()),
        ),
        patch.object(interface_deep_module, "SkillPrebuiltSynchronizer", sync_cls),
        patch.object(interface_deep_module, "is_skill_prebuilt_tenant", return_value=True),
    ):
        await adapter.create_instance(
            {"workspace_dir": request_ws},
            config_base=config_base,
        )

    sync_cls.assert_called_once()
    assert sync_cls.call_args.args[0] == request_ws
    assert adapter._workspace_dir == request_ws
