from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from jiuwenswarm.server.runtime.agent_adapter import interface_deep
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
)


class _AbilityManager:
    def __init__(self) -> None:
        self.added: list[str] = []
        self.removed: list[str] = []

    def add(self, card) -> None:
        self.added.append(card.name)

    def remove(self, name: str) -> None:
        self.removed.append(name)


def _vision_config() -> dict:
    return {
        "models": {
            "vision": {
                "model_client_config": {
                    "api_base": "https://vision.example/v1",
                    "api_key": "secret",
                    "model_name": "vision-model",
                    "client_provider": "OpenAI",
                }
            }
        }
    }


def test_multimodal_switch_hot_reload_registers_and_removes_vision_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    card = SimpleNamespace(name="vision_test", stateless=False)
    tool = SimpleNamespace(card=card, vision_model_config=None)
    registered: list[tuple[object, str | None]] = []
    unregistered: list[object] = []

    monkeypatch.setattr(interface_deep, "create_vision_tools", lambda **_kwargs: [tool])
    monkeypatch.setattr(
        interface_deep,
        "register_tool",
        lambda value, owner_id=None: registered.append((value, owner_id)),
    )
    monkeypatch.setattr(
        interface_deep, "unregister_tool", lambda value: unregistered.append(value)
    )
    monkeypatch.setenv("AUDIO_ENABLED", "false")
    monkeypatch.setenv("VIDEO_ENABLED", "false")
    monkeypatch.delenv("IMAGE_GEN_API_KEY", raising=False)

    adapter = JiuWenSwarmDeepAdapter()
    ability_manager = _AbilityManager()
    native_config = SimpleNamespace(enable_read_image_multimodal=None)
    adapter._instance = SimpleNamespace(
        ability_manager=ability_manager,
        deep_config=native_config,
    )
    adapter._tool_cards = []
    config = _vision_config()

    monkeypatch.setenv("VISION_ENABLED", "false")
    adapter._refresh_multimodal_configs(config)
    adapter._sync_multimodal_tools_for_runtime()
    assert adapter._vision_tools_registered is False
    assert registered == []

    monkeypatch.setenv("VISION_ENABLED", "true")
    adapter._refresh_multimodal_configs(config)
    adapter._sync_multimodal_tools_for_runtime()
    assert adapter._vision_tools_registered is True
    assert registered == [(tool, adapter._tool_owner_id())]
    assert ability_manager.added == ["vision_test"]
    assert [item.name for item in adapter._tool_cards] == ["vision_test"]

    monkeypatch.setenv("VISION_ENABLED", "false")
    adapter._refresh_multimodal_configs(config)
    adapter._sync_multimodal_tools_for_runtime()
    assert adapter._vision_tools_registered is False
    assert unregistered == [tool]
    assert ability_manager.removed == ["vision_test"]
    assert adapter._tool_cards == []
    assert adapter._resolve_enable_read_image_multimodal({}) is None
    assert adapter._resolve_enable_read_image_multimodal(
        {"enable_read_image_multimodal": True}
    ) is True
    assert native_config.enable_read_image_multimodal is None


def _image_gen_config() -> dict:
    return {
        "models": {
            "image_gen": {
                "model_client_config": {
                    "api_base": "https://dashscope.aliyuncs.com/api/v1",
                    "api_key": "secret",
                    "model_name": "wanx-v1",
                    "client_provider": "OpenAI",
                }
            }
        }
    }


def test_multimodal_switch_hot_reload_registers_and_removes_image_gen_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registered: list[str] = []
    unregistered: list[str] = []
    monkeypatch.setattr(
        interface_deep,
        "register_tool",
        lambda value, owner_id=None: registered.append(value.card.name),
    )
    monkeypatch.setattr(
        interface_deep, "unregister_tool", lambda value: unregistered.append(value.card.name)
    )
    monkeypatch.setenv("VISION_ENABLED", "false")
    monkeypatch.setenv("AUDIO_ENABLED", "false")
    monkeypatch.setenv("VIDEO_ENABLED", "false")
    # apply_image_gen_model_config_from_yaml writes these; pre-set them so monkeypatch restores them.
    monkeypatch.setenv("IMAGE_GEN_API_KEY", "secret")
    monkeypatch.setenv("IMAGE_GEN_API_BASE", "https://dashscope.aliyuncs.com/api/v1")
    monkeypatch.setenv("IMAGE_GEN_MODEL_NAME", "wanx-v1")
    monkeypatch.setenv("IMAGE_GEN_PROVIDER", "OpenAI")

    adapter = JiuWenSwarmDeepAdapter()
    ability_manager = _AbilityManager()
    adapter._instance = SimpleNamespace(
        ability_manager=ability_manager,
        deep_config=SimpleNamespace(enable_read_image_multimodal=None),
    )
    adapter._tool_cards = []
    config = _image_gen_config()

    monkeypatch.setenv("IMAGE_GEN_ENABLED", "false")
    adapter._refresh_multimodal_configs(config)
    adapter._sync_multimodal_tools_for_runtime()
    assert adapter._image_gen_tool_registered is False
    assert registered == []

    monkeypatch.setenv("IMAGE_GEN_ENABLED", "true")
    adapter._refresh_multimodal_configs(config)
    adapter._sync_multimodal_tools_for_runtime()
    assert adapter._image_gen_tool_registered is True
    assert registered == ["generate_image"]
    assert ability_manager.added == ["generate_image"]
    assert [item.name for item in adapter._tool_cards] == ["generate_image"]

    monkeypatch.setenv("IMAGE_GEN_ENABLED", "false")
    adapter._refresh_multimodal_configs(config)
    adapter._sync_multimodal_tools_for_runtime()
    assert adapter._image_gen_tool_registered is False
    assert unregistered == ["generate_image"]
    assert ability_manager.removed == ["generate_image"]
    assert adapter._tool_cards == []


def test_native_image_auto_uses_probe_cache_independently_of_vision_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = JiuWenSwarmDeepAdapter()
    adapter._vision_model_config = SimpleNamespace(model="vision-model")
    model = object()

    monkeypatch.setattr(
        interface_deep,
        "get_cached_image_support",
        lambda candidate: candidate is model,
    )

    assert adapter._native_image_input_enabled({}, model) is True
    assert adapter._native_image_input_enabled({}, object()) is False
    assert adapter._native_image_input_enabled(
        {"enable_read_image_multimodal": False},
        model,
    ) is False


def test_image_fallback_prompt_reports_missing_vision_capability() -> None:
    request = SimpleNamespace(
        params={
            "query": "这张图片是什么？",
            "media_items": [
                {
                    "type": "image",
                    "filename": "sample.png",
                    "path": "C:/tmp/sample.png",
                    "mime_type": "image/png",
                }
            ],
        }
    )
    inputs = {
        "query": "这张图片是什么？",
        "_multimodal_image_files": request.params["media_items"],
    }

    updated = JiuWenSwarmDeepAdapter._prepare_react_image_tool_prompt(
        request,
        inputs,
        enable_read_image_multimodal=False,
        vision_tool_available=False,
    )

    assert "没有配置可用的视觉模型工具" in updated["query"]
    assert "不要猜测图片内容" in updated["query"]


@pytest.mark.asyncio
async def test_multimodal_scope_uses_targeted_reload_without_resetting_other_runtimes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = JiuWenSwarmDeepAdapter()
    adapter._instance = SimpleNamespace()
    config = _vision_config()
    targeted_snapshot = AsyncMock(return_value=config)
    full_snapshot = AsyncMock()
    sync_tools = MagicMock()
    fan_out = AsyncMock()

    monkeypatch.setattr(
        adapter,
        "_apply_multimodal_reload_snapshot",
        targeted_snapshot,
    )
    monkeypatch.setattr(adapter, "_apply_reload_config_snapshot", full_snapshot)
    monkeypatch.setattr(adapter, "_sync_multimodal_tools_for_runtime", sync_tools)
    monkeypatch.setattr(adapter, "_fan_out_reload_to_session_adapters", fan_out)

    await adapter.reload_agent_config(
        config,
        {"VISION_ENABLED": "true"},
        reload_scopes={"multimodal"},
    )

    targeted_snapshot.assert_awaited_once_with(
        config,
        {"VISION_ENABLED": "true"},
    )
    full_snapshot.assert_not_awaited()
    sync_tools.assert_called_once_with()
    fan_out.assert_awaited_once_with(
        config,
        {"VISION_ENABLED": "true"},
        None,
        {"multimodal"},
    )
