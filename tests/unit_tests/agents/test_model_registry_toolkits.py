"""ModelRegistry 与模型注册工具的最小单元测试。

覆盖:校验(缺 name / 非法 api_base / 非法 model_type)、注册-upsert-注销幂等、
持久化往返、损坏文件降级、api_key 脱敏、健康检查三态(mock 网络)。
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from jiuwenswarm.server.runtime.model_registry.model_registry import (
    ModelRegistry,
    mask_api_key,
)
from jiuwenswarm.agents.harness.common.tools.model_registry_toolkits import (
    ModelRegistryToolkit,
)

_ENTRY = {
    "name": "Qwen2.5-7B-Instruct",
    "api_base": "http://127.0.0.1:8000/v1",
    "provider": "vllm",
    "param_size": "7B",
    "quantization": "BF16",
    "context_length": 32768,
    "capabilities": ["tool_calling"],
    "api_key": "sk-abcdef1234563f2a",
}


@pytest.fixture()
def registry(tmp_path: Path) -> ModelRegistry:
    return ModelRegistry(path=tmp_path / "model_registry.json")


async def test_register_and_mask(registry: ModelRegistry) -> None:
    result = await registry.register(dict(_ENTRY))
    assert result["success"] is True
    model = result["model"]
    assert model["api_key_masked"] == "sk-****3f2a"
    assert "api_key" not in model  # 原文不离开注册表对外视图


async def test_register_validation(registry: ModelRegistry) -> None:
    assert (await registry.register({"api_base": "http://x"}))["success"] is False
    assert (await registry.register({"name": "x", "api_base": "ftp://y"}))["success"] is False
    assert (await registry.register({"name": "x", "api_base": "http://h:1", "model_type": "bad"}))["success"] is False


async def test_upsert_replaces_but_keeps_registered_at(registry: ModelRegistry) -> None:
    first = await registry.register(dict(_ENTRY))
    second = await registry.register({"name": _ENTRY["name"], "api_base": _ENTRY["api_base"], "quantization": "W8A8"})
    assert second["model"]["registered_at"] == first["model"]["registered_at"]
    assert second["model"]["quantization"] == "W8A8"
    assert "api_key_masked" not in second["model"]  # upsert 为整体替换
    assert len(registry._read_entries()) == 1


async def test_unregister_idempotent(registry: ModelRegistry) -> None:
    await registry.register(dict(_ENTRY))
    assert (await registry.unregister(_ENTRY["name"]))["success"] is True
    assert (await registry.unregister(_ENTRY["name"]))["success"] is True
    assert (await registry.handle_models_list({}))["models"] == []


async def test_persistence_roundtrip(registry: ModelRegistry) -> None:
    await registry.register(dict(_ENTRY))
    reloaded = ModelRegistry(path=registry._path)
    models = (await reloaded.handle_models_list({}))["models"]
    assert [m["name"] for m in models] == [_ENTRY["name"]]


async def test_corrupt_file_degrades_to_empty(registry: ModelRegistry) -> None:
    registry._path.write_text("{not json", encoding="utf-8")
    assert (await registry.handle_models_list({}))["models"] == []


def test_mask_api_key() -> None:
    assert mask_api_key("abc") == "****"
    assert mask_api_key("") == "****"


async def test_health_check_branches(registry: ModelRegistry, monkeypatch: pytest.MonkeyPatch) -> None:
    """mock _check_health 的三种结果:running(有响应)/ stopped(连接拒绝)/ unknown(超时)。"""
    await registry.register(dict(_ENTRY))

    ok_client = MagicMock()
    ok_client.get = AsyncMock(return_value=MagicMock(status_code=200))
    check = await registry._check_health(ok_client, dict(_ENTRY))
    assert check["status"] == "running"

    refused_client = MagicMock()
    refused_client.get = AsyncMock(side_effect=httpx.ConnectError("refused"))
    check = await registry._check_health(refused_client, dict(_ENTRY))
    assert check["status"] == "stopped"

    timeout_client = MagicMock()
    timeout_client.get = AsyncMock(side_effect=httpx.TimeoutException("timeout"))
    check = await registry._check_health(timeout_client, dict(_ENTRY))
    assert check["status"] == "unknown"


def test_toolkit_builds_tools(registry: ModelRegistry) -> None:
    toolkit = ModelRegistryToolkit(registry=registry)
    names = [t.card.name for t in toolkit.get_tools()]
    assert names == ["register_model", "unregister_model"]
