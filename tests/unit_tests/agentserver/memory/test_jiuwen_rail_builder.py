# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for jiuwen_rail_builder (builtin jiuwen memory variant).

Covers both mount modes (http / sdk) by stubbing the site-packages rail /
provider classes at call time. The adapter is imported ONCE at module level
(skip the whole module when absent) and only its class attribute is swapped
per test — the builder's lazy dotted from-import is flaky when the module
isn't already cached in sys.modules (intermittent ModuleNotFoundError in the
pytest process after openjiuwen imports), so the tests pre-cache the real
module and never exercise the finder during tests.

sdk 模式的内核配置优先级（2026-10-17 起 builder 不再自行缝合）：
``memory.builtin.jiuwen.sdk`` 内联的两层命名空间 dict > provider 内置 agent 场景
默认装配（裸构造）；``sdk.config_path`` 为历史字段，已不再读取。
"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

# Import the adapter EARLY (clean import state, before jiuwenswarm/openjiuwen
# module-graph side effects) and skip this module when it is not installed.
adapter_mod = pytest.importorskip(
    "jiuwen_memory_adapter.jiuwenswarm.agent_memory_provider"
)

from jiuwenswarm.agents.harness.common.memory import jiuwen_rail_builder as jrb  # noqa: E402


class _FakeJiuwenRail:
    last_args = None

    def __init__(
        self,
        provider,
        *,
        user_id="__default__",
        scope_id="__default__",
        agent_id="jiuwenswarm",
        session_id="",
        session_end_extract=False,
    ):
        _FakeJiuwenRail.last_args = {
            "provider": provider,
            "user_id": user_id,
            "scope_id": scope_id,
            "agent_id": agent_id,
            "session_id": session_id,
            "session_end_extract": session_end_extract,
        }
        self.provider = provider


class _FakeAgentMemoryProvider:
    last_init_kwargs = None
    available = True

    def __init__(
        self,
        *,
        base_url=None,
        config_path=None,
        config_dict=None,
        security_provider=None,
        api_key="",
        user_id="",
        agent_id="",
        space="",
        identity_org="local",
        identity_user="developer",
        **_kwargs,
    ):
        _FakeAgentMemoryProvider.last_init_kwargs = {
            "base_url": base_url,
            "config_path": config_path,
            "config_dict": config_dict,
            "security_provider": security_provider,
            "api_key": api_key,
            "user_id": user_id,
            "agent_id": agent_id,
            "space": space,
            "identity_org": identity_org,
            "identity_user": identity_user,
        }

    @staticmethod
    def is_available() -> bool:
        return _FakeAgentMemoryProvider.available


@pytest.fixture(autouse=True)
def _stubs(monkeypatch):
    """Fake rail/provider + hermetic env."""
    _FakeJiuwenRail.last_args = None
    _FakeAgentMemoryProvider.last_init_kwargs = None
    _FakeAgentMemoryProvider.available = True

    # The pinned openjiuwen may not export JiuwenMemoryRail at all (the builder
    # then disables the variant). Inject the stub unconditionally so the
    # builder's lazy from-import resolves it in either dependency state.
    monkeypatch.setattr(
        "openjiuwen.harness.rails.JiuwenMemoryRail", _FakeJiuwenRail, raising=False
    )

    # Patch the provider class on the (module-level imported, hence cached)
    # real adapter module — the builder's lazy from-import then resolves the
    # fake without touching the import machinery.
    monkeypatch.setattr(adapter_mod, "AgentMemoryMemoryProvider", _FakeAgentMemoryProvider)

    for env in (
        "JIUWEN_MEMORY_MODE",
        "JIUWEN_MEMORY_BASE_URL",
        "JIUWEN_MEMORY_API_KEY",
        "JIUWEN_MEMORY_VIDEO_ROOTS",
        "VIDEO_ENABLED",
        "AUDIO_ENABLED",
    ):
        monkeypatch.delenv(env, raising=False)
    yield


def _cfg(jiuwen: dict) -> dict:
    """A full-shaped config with the jiuwen builtin section."""
    return {"memory": {"builtin": {"provider": "jiuwen", "jiuwen": jiuwen}}}


# ---------------------------------------------------------------------------
# mode resolution
# ---------------------------------------------------------------------------

def test_default_mode_is_http():
    assert jrb.get_jiuwen_builtin_mode(_cfg({})) == "http"


def test_sdk_mode_selected_from_config():
    assert jrb.get_jiuwen_builtin_mode(_cfg({"mode": "sdk"})) == "sdk"


def test_sdk_mode_env_override(monkeypatch):
    monkeypatch.setenv("JIUWEN_MEMORY_MODE", "sdk")
    assert jrb.get_jiuwen_builtin_mode(_cfg({})) == "sdk"


def test_unknown_mode_falls_back_to_http():
    assert jrb.get_jiuwen_builtin_mode(_cfg({"mode": "socks"})) == "http"


# ---------------------------------------------------------------------------
# build: http mode (unchanged behavior)
# ---------------------------------------------------------------------------

def test_http_mode_builds_network_provider():
    rail = jrb.build_jiuwen_memory_rail(_cfg({"base_url": "http://127.0.0.1:8137"}))
    assert isinstance(rail, _FakeJiuwenRail)
    init = _FakeAgentMemoryProvider.last_init_kwargs
    assert init["base_url"] == "http://127.0.0.1:8137"
    assert init["config_path"] is None
    assert init["identity_org"] == "local"
    # rail wiring: cross-session sharing default
    assert _FakeJiuwenRail.last_args["session_id"] == ""
    assert _FakeJiuwenRail.last_args["session_end_extract"] is False


# ---------------------------------------------------------------------------
# build: sdk mode
# ---------------------------------------------------------------------------

def test_sdk_bare_uses_provider_builtin_default():
    """sdk + 无内联 sdk dict：provider 自填内置 agent 默认装配。"""
    rail = jrb.build_jiuwen_memory_rail(_cfg({"mode": "sdk"}))
    assert isinstance(rail, _FakeJiuwenRail)
    init = _FakeAgentMemoryProvider.last_init_kwargs
    assert init["config_dict"] is None  # provider 端走内置默认
    assert init["config_path"] is None
    assert not init["base_url"]
    assert init["security_provider"] is not None  # mem2.0 进程内身份 fail-closed
    assert _FakeJiuwenRail.last_args["user_id"] == "__default__"
    assert _FakeJiuwenRail.last_args["session_id"] == ""


def test_sdk_inline_config_dict_passthrough():
    """sdk + 内联 sdk dict：整份 dict 直传 provider（config_path 被忽略）。"""
    rail = jrb.build_jiuwen_memory_rail(
        _cfg({"mode": "sdk", "sdk": {"globals": {"llm_model": "inline-model"}}})
    )
    assert isinstance(rail, _FakeJiuwenRail)
    init = _FakeAgentMemoryProvider.last_init_kwargs
    assert init["config_path"] is None
    assert not init["base_url"]
    passed = init["config_dict"]
    assert passed["globals"]["llm_model"] == "inline-model"
    # 空凭证字段就地填入 jiuwenswarm 真源（可能为空串），不破坏内联值
    assert "llm_api_key" in passed["globals"]
    assert "config_path" not in passed


def test_sdk_agent_name_overrides_agent_id():
    """运行 agent name 优先作为 scope.agent 轴。"""
    rail = jrb.build_jiuwen_memory_rail(_cfg({"mode": "sdk", "agent_id": "cfg-agent"}), agent_name="run-agent")
    assert isinstance(rail, _FakeJiuwenRail)
    assert _FakeJiuwenRail.last_args["agent_id"] == "run-agent"


def test_sdk_portal_video_globals_passthrough(tmp_path):
    video_settings = {
        "local_media_roots": [{"scope": {"org": "local"}, "root": str(tmp_path)}],
        "asr_base_url": "https://asr.example.invalid/v1",
        "asr_model": "asr-model",
        "asr_api_key": None,
        "video_base_url": "https://video.example.invalid/v1",
        "video_model": "video-model",
        "video_api_key": "",
    }
    config = _cfg({"mode": "sdk", "sdk": {"globals": dict(video_settings)}})
    rail = jrb.build_jiuwen_memory_rail(config)
    assert isinstance(rail, _FakeJiuwenRail)
    forwarded = _FakeAgentMemoryProvider.last_init_kwargs["config_dict"]["globals"]
    assert {key: forwarded[key] for key in video_settings} == video_settings


def _web_tool_config() -> dict:
    return {
        "permissions": {"enabled": False, "mode": "manual"},
        "models": {
            kind: {
                "enabled": True,
                "model_client_config": {
                    "client_provider": "OpenAI",
                    "api_base": f"https://{kind}.example.invalid/v1",
                    "api_key": f"{kind}-test-key",
                    "model_name": f"{kind}-test-model",
                },
            }
            for kind in ("video", "audio")
        },
    }


def test_web_settings_assemble_sdk_video_with_exact_scope(tmp_path):
    local_media = pytest.importorskip("jiuwen_memory.common.security.protection.local_media")
    from jiuwen_memory.api import Scope
    from jiuwen_memory.common.errors import PermissionDeniedError

    config = _cfg({
        "mode": "sdk", "scope_id": "org", "space": "space", "user_id": "user", "session_id": "session",
    })
    project = tmp_path / "project"
    project.mkdir()
    media = jrb.get_jiuwen_sdk_media_globals(
        config, _web_tool_config(), workspace=str(project), agent_name="run-agent",
    )
    assert jrb.build_jiuwen_memory_rail(config, "run-agent", sdk_media_globals=media)
    forwarded = _FakeAgentMemoryProvider.last_init_kwargs["config_dict"]
    assembled = adapter_mod._merge_config_over_default(forwarded)
    assert assembled["asr"]["video"]["params"] == {
        "asr_base_url": "https://audio.example.invalid/v1",
        "asr_api_key": "audio-test-key",
        "asr_model": "audio-test-model",
    }
    video = assembled["llm"]["video_vision"]["params"]
    assert video["llm_base_url"] == "https://video.example.invalid/v1"
    assert video["llm_api_key"] == "video-test-key"
    assert video["llm_model"] == "video-test-model"
    normalizer = assembled["normalizer"]["default"]["params"]["routes"]["video"]["params"]
    imports = local_media.LocalMediaImports(normalizer["local_media_roots"])
    scope = Scope(org="org", space="space", user="user", agent="run-agent", session="session")
    inside = project / "inside.mp4"
    outside = tmp_path / "outside.mp4"
    inside.touch()
    outside.touch()
    assert imports.preflight_video(inside.as_uri(), scope) == inside.as_uri()
    with pytest.raises(PermissionDeniedError):
        imports.preflight_video(outside.as_uri(), scope)
    with pytest.raises(PermissionDeniedError):
        imports.preflight_video(
            inside.as_uri(), Scope(org="org", space="space", user="user", agent="other", session="session"),
        )


def test_web_model_settings_preserve_deployment_import_roots(tmp_path, monkeypatch):
    deployed_root = tmp_path / "deployed"
    deployed_root.mkdir()
    roots = [{"scope": {"org": "__default__"}, "root": str(deployed_root)}]
    config = _cfg({"mode": "sdk", "sdk": {"globals": {"local_media_roots": roots}}})
    media = jrb.get_jiuwen_sdk_media_globals(config, _web_tool_config(), workspace=str(tmp_path), agent_name=None)
    assert media["video_model"] == "video-test-model"
    assert "local_media_roots" not in media
    assert jrb.build_jiuwen_memory_rail(config, sdk_media_globals=media)
    assert _FakeAgentMemoryProvider.last_init_kwargs["config_dict"]["globals"]["local_media_roots"] == roots

    monkeypatch.setenv("JIUWEN_MEMORY_VIDEO_ROOTS", json.dumps(roots))
    media = jrb.get_jiuwen_sdk_media_globals(
        _cfg({"mode": "sdk"}), _web_tool_config(), workspace=str(tmp_path), agent_name=None,
    )
    assert media["asr_model"] == "audio-test-model"
    assert "local_media_roots" not in media


@pytest.mark.parametrize("deployment_source", ["inline", "env", "workspace"])
def test_video_upload_root_preserves_effective_import_roots(tmp_path, monkeypatch, deployment_source):
    local_media = pytest.importorskip("jiuwen_memory.common.security.protection.local_media")
    from jiuwen_memory.api import Scope
    from jiuwen_memory.common.errors import PermissionDeniedError

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    uploads = tmp_path / "web_session" / "uploads"
    uploads.mkdir(parents=True)
    scope = {"org": "org", "space": "space", "user": "user", "agent": "run-agent", "session": "memory-session"}
    roots = [{"scope": scope, "root": str(workspace)}]
    jiuwen = {"mode": "sdk", "scope_id": "org", "space": "space", "user_id": "user", "session_id": "memory-session"}
    if deployment_source == "inline":
        jiuwen["sdk"] = {"globals": {"local_media_roots": roots}}
    elif deployment_source == "env":
        monkeypatch.setenv("JIUWEN_MEMORY_VIDEO_ROOTS", json.dumps(roots))
    media = jrb.get_jiuwen_sdk_media_globals(
        _cfg(jiuwen), _web_tool_config(), workspace=str(workspace), agent_name="run-agent",
        session_upload_root=uploads,
    )
    assert media["local_media_roots"] == [*roots, {"scope": scope, "root": str(uploads)}]
    imports = local_media.LocalMediaImports(media["local_media_roots"])
    uploaded = uploads / "demo.mp4"
    uploaded.touch()
    unrelated = tmp_path / "unrelated.mp4"
    unrelated.touch()
    assert imports.preflight_video(uploaded.as_uri(), Scope(**scope)) == uploaded.as_uri()
    with pytest.raises(PermissionDeniedError):
        imports.preflight_video(unrelated.as_uri(), Scope(**scope))


@pytest.mark.parametrize(("disabled_kind", "prefix"), [("audio", "asr"), ("video", "video")])
def test_web_model_settings_can_use_deployment_fallback(tmp_path, disabled_kind, prefix):
    pytest.importorskip("jiuwen_memory.common.security.protection.local_media")
    fallback = {
        f"{prefix}_base_url": "https://deployment.example.invalid/v1",
        f"{prefix}_model": "deployment-model",
        f"{prefix}_api_key": "deployment-test-key",
    }
    config = _cfg({"mode": "sdk", "sdk": {"globals": fallback}})
    web_config = _web_tool_config()
    web_config["models"][disabled_kind]["enabled"] = False

    media = jrb.get_jiuwen_sdk_media_globals(config, web_config, workspace=str(tmp_path), agent_name=None)
    assert media["local_media_roots"][0]["root"] == str(tmp_path.resolve())
    assert not fallback.keys() & media.keys()
    assert jrb.build_jiuwen_memory_rail(config, sdk_media_globals=media)
    assembled = adapter_mod._merge_config_over_default(_FakeAgentMemoryProvider.last_init_kwargs["config_dict"])
    audio_model = assembled["asr"]["video"]["params"]["asr_model"]
    video_model = assembled["llm"]["video_vision"]["params"]["llm_model"]
    if disabled_kind == "audio":
        assert (audio_model, video_model) == ("deployment-model", "video-test-model")
    else:
        assert (audio_model, video_model) == ("audio-test-model", "deployment-model")


@pytest.mark.asyncio
async def test_memory_rail_rebuilds_for_web_media_settings_and_permission_changes(tmp_path, monkeypatch):
    from jiuwenswarm.server.runtime.agent_adapter import interface_deep as deep

    config = _cfg({"mode": "sdk", "sdk": {"globals": {"llm_model": "text-model"}}})
    monkeypatch.setattr(deep, "get_config", lambda: config)
    monkeypatch.setattr(deep, "get_memory_mode", lambda _: "local")
    monkeypatch.setattr(deep, "is_builtin_memory_allowed", lambda _: True)
    monkeypatch.setattr(deep, "is_memory_enabled", lambda _mode, _config: True)

    adapter = object.__new__(deep.JiuWenSwarmDeepAdapter)
    adapter._instance = SimpleNamespace(register_rail=AsyncMock(), unregister_rail=AsyncMock())
    adapter._instance_overrides = {}
    adapter._enable_auto_permission = False
    adapter._sys_operation_card = SimpleNamespace(mode=deep.OperationMode.LOCAL)
    adapter._config_base_cache = _web_tool_config()
    adapter._agent_name = "run-agent"
    adapter._memory_media_workspace = str(tmp_path)
    adapter._memory_rail = None
    adapter._memory_embedding_fingerprint = ""
    adapter._drain_jiuwen_memory_rail = AsyncMock()

    await adapter._handle_memory_rail_by_config("agent")
    first = adapter._memory_rail
    globals_config = _FakeAgentMemoryProvider.last_init_kwargs["config_dict"]["globals"]
    assert globals_config["llm_model"] == "text-model"
    assert globals_config["local_media_roots"][0]["root"] == str(tmp_path.resolve())
    assert "video-test-key" not in adapter._memory_embedding_fingerprint
    assert config["memory"]["builtin"]["jiuwen"]["sdk"] == {"globals": {"llm_model": "text-model"}}

    await adapter._handle_memory_rail_by_config("agent")
    assert adapter._memory_rail is first
    adapter._config_base_cache["models"]["audio"]["model_client_config"]["api_key"] = "rotated"
    await adapter._handle_memory_rail_by_config("agent")
    adapter._instance.unregister_rail.assert_awaited_once_with(first)
    assert _FakeAgentMemoryProvider.last_init_kwargs["config_dict"]["globals"]["asr_api_key"] == "rotated"

    other_project = tmp_path / "other-project"
    other_project.mkdir()
    adapter._memory_media_workspace = str(other_project)
    await adapter._handle_memory_rail_by_config("agent")
    bindings = _FakeAgentMemoryProvider.last_init_kwargs["config_dict"]["globals"]["local_media_roots"]
    assert [binding["root"] for binding in bindings] == [str(other_project.resolve())]

    adapter._config_base_cache["permissions"]["enabled"] = True
    await adapter._handle_memory_rail_by_config("agent")
    globals_config = _FakeAgentMemoryProvider.last_init_kwargs["config_dict"]["globals"]
    assert "local_media_roots" not in globals_config
    assert globals_config["video_api_key"] == "video-test-key"
    assert globals_config["asr_api_key"] == "rotated"

    roots = [{"scope": {"org": "__default__"}, "root": str(tmp_path)}]
    config["memory"]["builtin"]["jiuwen"]["sdk"]["globals"]["local_media_roots"] = roots
    await adapter._handle_memory_rail_by_config("agent")
    adapter._config_base_cache["models"]["video"]["model_client_config"]["api_key"] = "new-video-key"
    previous = adapter._memory_rail
    await adapter._handle_memory_rail_by_config("agent")
    assert adapter._memory_rail is not previous
    globals_config = _FakeAgentMemoryProvider.last_init_kwargs["config_dict"]["globals"]
    assert globals_config["local_media_roots"] == roots
    assert globals_config["video_api_key"] == "new-video-key"
    assert globals_config["asr_api_key"] == "rotated"

    adapter._config_base_cache["permissions"]["enabled"] = False
    for setting in ("auto_permission", "run_permissions_enabled"):
        adapter._enable_auto_permission = setting == "auto_permission"
        adapter._instance_overrides["run_permissions_enabled"] = setting == "run_permissions_enabled"
        media = adapter._jiuwen_memory_media_globals(_cfg({"mode": "sdk"}))
        assert "local_media_roots" not in media
        assert media["video_api_key"] == "new-video-key"
        assert media["asr_api_key"] == "rotated"
    adapter._instance_overrides.clear()

    uploads = tmp_path / "uploads"
    uploads.mkdir()
    adapter._memory_media_upload_root = uploads
    adapter._enable_auto_permission = False
    adapter._config_base_cache["permissions"]["enabled"] = True
    assert "local_media_roots" not in adapter._jiuwen_memory_media_globals(config)
    adapter._config_base_cache["permissions"]["enabled"] = False
    media = adapter._jiuwen_memory_media_globals(config)
    assert media["local_media_roots"][-1]["root"] == str(uploads)
    adapter._memory_media_upload_root = None

    adapter._config_base_cache["models"]["video"]["enabled"] = False
    adapter._config_base_cache["models"]["audio"]["enabled"] = False
    assert adapter._jiuwen_memory_media_globals(config) == {}
    adapter._config_base_cache["models"]["video"]["enabled"] = True
    adapter._sys_operation_card.mode = deep.OperationMode.SANDBOX
    assert adapter._jiuwen_memory_media_globals(config) == {}


# ---------------------------------------------------------------------------
# failure contract: None = memory disabled, never an error
# ---------------------------------------------------------------------------

def test_missing_adapter_class_returns_none(monkeypatch):
    # `from <adapter> import AgentMemoryMemoryProvider` raises ImportError when
    # the attribute is gone — the same builder branch as a missing package.
    monkeypatch.delattr(adapter_mod, "AgentMemoryMemoryProvider")
    assert jrb.build_jiuwen_memory_rail(_cfg({"mode": "sdk"})) is None


def test_provider_unavailable_returns_none():
    _FakeAgentMemoryProvider.available = False
    assert jrb.build_jiuwen_memory_rail(_cfg({"mode": "sdk"})) is None


# ---------------------------------------------------------------------------
# fingerprint: stable, and sensitive to every semantic change
# ---------------------------------------------------------------------------

def test_fingerprint_stable_for_equal_config():
    cfg = _cfg({"mode": "sdk"})
    assert jrb.jiuwen_builtin_config_fingerprint(cfg) == jrb.jiuwen_builtin_config_fingerprint(cfg)


def test_fingerprint_sensitive_to_mode():
    sdk = jrb.jiuwen_builtin_config_fingerprint(_cfg({"mode": "sdk"}))
    http = jrb.jiuwen_builtin_config_fingerprint(_cfg({"mode": "http", "base_url": "http://x:1"}))
    assert sdk != http


def test_fingerprint_builtin_default_is_constant_marker():
    fp = jrb.jiuwen_builtin_config_fingerprint(_cfg({"mode": "sdk"}))
    assert "builtin-default" in fp


def test_fingerprint_distinguishes_builtin_and_inline_dict():
    bare = jrb.jiuwen_builtin_config_fingerprint(_cfg({"mode": "sdk"}))
    inline_fp = jrb.jiuwen_builtin_config_fingerprint(
        _cfg({"mode": "sdk", "sdk": {"globals": {"a": 1}}})
    )
    assert bare != inline_fp


def test_fingerprint_sensitive_to_inline_dict_content():
    cfg = _cfg({"mode": "sdk", "sdk": {"globals": {"a": 1}}})
    base = jrb.jiuwen_builtin_config_fingerprint(cfg)
    assert jrb.jiuwen_builtin_config_fingerprint(cfg) == base  # stable
    cfg["memory"]["builtin"]["jiuwen"]["sdk"]["globals"]["a"] = 2  # 内容变化
    assert jrb.jiuwen_builtin_config_fingerprint(cfg) != base


def test_fingerprint_sensitive_to_http_endpoint():
    a = jrb.jiuwen_builtin_config_fingerprint(_cfg({"mode": "http", "base_url": "http://a:1"}))
    b = jrb.jiuwen_builtin_config_fingerprint(_cfg({"mode": "http", "base_url": "http://b:1"}))
    assert a != b


@pytest.mark.parametrize("mode", ["sdk", "http"])
@pytest.mark.asyncio
async def test_jiuwen_provider_uses_default_project_across_operations(monkeypatch, mode):
    prefetch = AsyncMock()
    sync_turn = AsyncMock()
    tool = AsyncMock()
    monkeypatch.setattr(_FakeAgentMemoryProvider, "prefetch", prefetch, raising=False)
    monkeypatch.setattr(_FakeAgentMemoryProvider, "sync_turn", sync_turn, raising=False)
    monkeypatch.setattr(_FakeAgentMemoryProvider, "handle_tool_call", tool, raising=False)
    provider = jrb.build_jiuwen_memory_rail(_cfg({"mode": mode})).provider

    await provider.prefetch("query", project=None)
    await provider.sync_turn("user", "assistant")
    assert prefetch.call_args.kwargs["project"] == sync_turn.call_args.kwargs["project"] == "default"
    await provider.prefetch("query", project="testMemory")
    await provider.sync_turn("user", "assistant", project="testMemory")
    assert prefetch.call_args.kwargs["project"] == sync_turn.call_args.kwargs["project"] == "testMemory"

    for tool_name in (
        "agent_memory_conclude", "agent_memory_search", "agent_memory_video_submit",
        "agent_memory_read_md_file", "agent_memory_edit_md_file",
    ):
        await provider.handle_tool_call(tool_name, {})
        tool.assert_awaited_with(tool_name, {"project": "default"})
    await provider.handle_tool_call("agent_memory_read_md_file", {"project": ""})
    tool.assert_awaited_with("agent_memory_read_md_file", {"project": ""})
    await provider.handle_tool_call("agent_memory_conclude", {"project": "testMemory"})
    tool.assert_awaited_with("agent_memory_conclude", {"project": "testMemory"})


@pytest.mark.asyncio
@pytest.mark.parametrize("template_source", ["swarm", "core", "edited", "custom-root"])
async def test_sdk_initialize_archives_only_stock_workspace_memory(monkeypatch, tmp_path, template_source):
    from jiuwenswarm.common import utils
    from openjiuwen.harness.workspace.workspace import Workspace

    workspace = tmp_path / "agent" / "workspace"
    root = tmp_path / "custom" if template_source == "custom-root" else workspace
    target = root / "memory" / "MEMORY.md"
    target.parent.mkdir(parents=True)
    content = (
        utils._find_package_root() / "resources" / "agent" / "workspace" / "memory" / "MEMORY_EN.md"
    ).read_text(encoding="utf-8")
    if template_source == "core":
        templates = next(node["children"] for node in Workspace.get_default_directory("cn") if node["name"] == "memory")
        content = next(node["default_content"] for node in templates if node["name"] == "MEMORY.md")
    elif template_source == "edited":
        content += "\nI prefer Python.\n"
    target.write_text(content, encoding="utf-8")
    monkeypatch.setattr(utils, "get_agent_workspace_dir", lambda: workspace)
    monkeypatch.setattr(utils, "get_user_workspace_dir", lambda: tmp_path)

    archive_stock = template_source in ("swarm", "core")

    async def initialize():
        assert target.exists() is not archive_stock

    base_initialize = AsyncMock(side_effect=initialize)
    monkeypatch.setattr(_FakeAgentMemoryProvider, "initialize", base_initialize, raising=False)
    rail = jrb.build_jiuwen_memory_rail(_cfg({
        "mode": "sdk", "sdk": {"markdown_store": {"default": {"params": {"root": str(root)}}}},
    }))
    await rail.provider.initialize()
    base_initialize.assert_awaited_once()

    backups = list(target.parent.glob(".MEMORY.md.bootstrap-*.bak"))
    if archive_stock:
        assert len(backups) == 1
        assert backups[0].read_text(encoding="utf-8") == content
    else:
        assert not backups
        assert target.read_text(encoding="utf-8") == content


@pytest.mark.asyncio
async def test_jiuwen_rail_routes_same_named_projects_by_absolute_path(monkeypatch, tmp_path):
    rail_module = pytest.importorskip("openjiuwen.harness.rails.memory.jiuwen_memory_rail")
    rail_class = getattr(rail_module, "JiuwenMemoryRail", None)
    if rail_class is None:
        pytest.skip("Installed Core does not provide JiuwenMemoryRail")
    monkeypatch.setattr("openjiuwen.harness.rails.JiuwenMemoryRail", rail_class, raising=False)
    schema = {"name": "agent_memory_conclude", "parameters": {"properties": {
        "project": {"type": "string"}, "conclusion": {"type": "string"},
    }}}
    monkeypatch.setattr(_FakeAgentMemoryProvider, "get_tool_schemas", lambda self: [schema], raising=False)
    tool_handler = AsyncMock(return_value='{"result": "stored"}')
    monkeypatch.setattr(_FakeAgentMemoryProvider, "handle_tool_call", tool_handler, raising=False)
    monkeypatch.setattr(_FakeAgentMemoryProvider, "initialize", AsyncMock(), raising=False)
    prefetch = AsyncMock(return_value="")
    sync_finished = asyncio.Event()

    async def mark_synced(*_args, **_kwargs):
        sync_finished.set()

    sync_turn = AsyncMock(side_effect=mark_synced)
    monkeypatch.setattr(_FakeAgentMemoryProvider, "prefetch", prefetch, raising=False)
    monkeypatch.setattr(_FakeAgentMemoryProvider, "sync_turn", sync_turn, raising=False)

    rail = jrb.build_jiuwen_memory_rail(_cfg({"mode": "sdk"}))
    registrations = {}

    def register(card, function):
        registrations[card.name] = function
        return SimpleNamespace(added=True)

    agent = SimpleNamespace(ability_manager=SimpleNamespace(add_ability=register))
    rail.init(agent)
    tool = registrations["agent_memory_conclude"]
    coordinates = set()

    for parent in ("work", "archive"):
        sync_finished.clear()
        agent._jiuwenswarm_memory_project_dir = str(tmp_path / parent / "testMemory")
        ctx = SimpleNamespace(
            agent=agent, session=None, extra={},
            inputs=SimpleNamespace(query="Uses Python", messages=[], result={"output": "Saved"}),
        )
        await rail.before_invoke(ctx)
        await rail.before_model_call(ctx)
        await rail.after_invoke(ctx)
        await asyncio.wait_for(sync_finished.wait(), timeout=2)
        project = prefetch.call_args.kwargs["project"]
        assert project.split("-", 1)[0].casefold() == "testmemory"
        assert len(project.rsplit("-", 1)[1]) == 12
        assert sync_turn.call_args.kwargs["project"] == project
        await tool.invoke({"conclusion": "Uses Python"})
        assert tool_handler.call_args.args[1]["project"] == project
        coordinates.add(project)
    assert len(coordinates) == 2
    assert "description" not in schema["parameters"]["properties"]["project"]

    agent._jiuwenswarm_memory_project_dir = None
    agent._jiuwenswarm_project_dir = str(tmp_path / "chat-1")
    await tool.invoke({"conclusion": "General memory"})
    assert tool_handler.call_args.args[1]["project"] == "default"

    await tool._func(conclusion="Explicit group", project="testMemory")
    assert tool_handler.call_args.args[1]["project"] == "testMemory"
