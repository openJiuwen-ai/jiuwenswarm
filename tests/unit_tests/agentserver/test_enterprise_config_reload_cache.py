# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""reload 刷新企业配置失败时不得清空缓存（否则 MCP 会被整表清掉）。"""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

# interface_deep 顶部会拉可选依赖；精简环境先占位，且不要把 google 打成非包模块。
def _ensure_pkg(name: str) -> types.ModuleType:
    existing = sys.modules.get(name)
    if existing is not None:
        return existing
    mod = types.ModuleType(name)
    mod.__path__ = []
    sys.modules[name] = mod
    return mod


def _ensure_attr(mod: types.ModuleType, name: str, value: object) -> None:
    if not hasattr(mod, name):
        setattr(mod, name, value)


gc = _ensure_pkg("gitcode_api")
_ensure_attr(gc, "AsyncGitCode", type("AsyncGitCode", (), {}))
gc_models = _ensure_pkg("gitcode_api._models")
_ensure_attr(
    gc_models, "RepositoryGitCodeTemplate", type("RepositoryGitCodeTemplate", (), {})
)

# 仅 stub image_tools，避免 google.genai 链路
if "jiuwenswarm.agents.harness.common.tools.image_tools" not in sys.modules:
    image_tools = types.ModuleType(
        "jiuwenswarm.agents.harness.common.tools.image_tools"
    )
    image_tools.generate_image = MagicMock()
    sys.modules["jiuwenswarm.agents.harness.common.tools.image_tools"] = image_tools

from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (  # noqa: E402
    JiuWenSwarmDeepAdapter,
)
from jiuwenswarm.server.runtime.enterprise_config.schemas import (  # noqa: E402
    EffectiveEnterpriseConfig,
    RoutingContext,
)


def _adapter_with_cached_enterprise() -> JiuWenSwarmDeepAdapter:
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._enterprise_config = EffectiveEnterpriseConfig(
        routing=RoutingContext(group_id="g1", bot_id="b1", user_id="u1"),
        mcp=[
            {
                "template_id": "t1",
                "enabled": True,
                "mcp_entry": {
                    "name": "keep-me",
                    "transport": "sse",
                    "url": "http://127.0.0.1:9000/sse",
                    "enabled": True,
                },
            }
        ],
    )
    adapter._agent_permissions_body = None
    adapter._skill_manager = None
    return adapter


@pytest.mark.asyncio
async def test_refresh_enterprise_config_restores_cache_when_load_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_adapter.interface_deep.is_enterprise",
        lambda: True,
    )
    adapter = _adapter_with_cached_enterprise()
    cached = adapter._enterprise_config

    async def _boom(_request):
        # 模拟旧实现「先清后载」：加载失败前已把缓存冲掉。
        adapter._enterprise_config = None
        raise TimeoutError("gateway db unavailable")

    monkeypatch.setattr(adapter, "_load_enterprise_config", _boom)

    await adapter._refresh_enterprise_config_for_reload()

    assert adapter._enterprise_config is cached
    assert adapter._enterprise_config is not None
    assert len(adapter._enterprise_config.mcp) == 1


def test_skill_authorization_rail_uses_agent_template_without_request_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.server.runtime.agent_adapter import interface_deep

    monkeypatch.setattr(
        interface_deep,
        "get_effective_permissions_config",
        lambda **_kwargs: {
            "enabled": True,
            "skill_authorization": {"enabled": False},
            "defaults": {"*": "allow"},
        },
    )
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._agent_permissions_body = {
        "enabled": True,
        "skill_authorization": {"enabled": True},
        "defaults": {"*": "ask"},
    }
    adapter._enterprise_config = None
    adapter._permission_rail = None
    adapter._skill_rail = None

    rail = adapter._build_skill_authorization_rail()

    assert rail is not None
    assert rail._feature_enabled() is True
    assert rail._base_effective_config("session-1")["defaults"] == {"*": "ask"}


@pytest.mark.asyncio
async def test_load_enterprise_config_keeps_cache_when_gateway_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_adapter.interface_deep.is_enterprise",
        lambda: True,
    )
    adapter = _adapter_with_cached_enterprise()
    cached = adapter._enterprise_config

    async def _raise_load(*_args, **_kwargs):
        raise TimeoutError("gateway db unavailable")

    import jiuwenswarm.server.runtime.enterprise_config as enterprise_config_mod

    monkeypatch.setattr(
        enterprise_config_mod,
        "load_effective_enterprise_config",
        _raise_load,
    )

    request = SimpleNamespace(
        request_id="r1",
        channel_id="web",
        metadata={"routing": {"group_id": "g1", "bot_id": "b1", "user_id": "u1"}},
        params={},
        user_id="u1",
    )

    with pytest.raises(TimeoutError):
        await adapter._load_enterprise_config(request)

    assert adapter._enterprise_config is cached


@pytest.mark.asyncio
async def test_merge_clears_mcp_only_when_enterprise_config_is_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_adapter.interface_deep.is_enterprise",
        lambda: True,
    )
    adapter = _adapter_with_cached_enterprise()
    config_base = {
        "mcp": {
            "servers": [
                {
                    "name": "local-demo",
                    "transport": "stdio",
                    "command": "echo",
                    "enabled": True,
                }
            ]
        }
    }

    kept = adapter._merge_enterprise_mcp_into_config(config_base)
    assert {item["name"] for item in kept["mcp"]["servers"]} == {"keep-me"}

    adapter._enterprise_config = None
    cleared = adapter._merge_enterprise_mcp_into_config(config_base)
    assert cleared["mcp"]["servers"] == []
