# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import base64

import pytest

from jiuwenswarm.agents.harness.common.tools.image_tools import (
    _invoke_model_image_generation,
    _normalize_image_size,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1024x1024", "1024*1024"),
        ("1792X1024", "1792*1024"),
        ("1664*928", "1664*928"),
        ("  1024x1024  ", "1024*1024"),
        ("", None),
        ("   ", None),
        (None, None),
    ],
)
def test_normalize_image_size(raw, expected):
    assert _normalize_image_size(raw) == expected


class _StubResponse:
    """Minimal stand-in for agent-core's ImageGenerationResponse."""

    def __init__(self, payload: bytes) -> None:
        self.images = []
        self.images_base64 = [base64.b64encode(payload).decode("ascii")]


class _StubModel:
    """Records the kwargs generate_image was called with."""

    calls: list[dict] = []

    def __init__(self, *args, **kwargs) -> None:
        pass

    async def generate_image(self, **kwargs):
        type(self).calls.append(kwargs)
        return _StubResponse(b"stub-image-bytes")


def _isolate_image_gen(monkeypatch, tmp_path) -> None:
    """Stub the Model class, workspace dir and yaml lookup so no real config/API is used."""
    import openjiuwen.core.foundation.llm as llm_mod
    from jiuwenswarm.agents.harness.common.tools import image_tools

    _StubModel.calls = []
    monkeypatch.setattr(llm_mod, "Model", _StubModel)
    monkeypatch.setattr(image_tools, "get_agent_workspace_dir", lambda: tmp_path)
    # config.yaml would otherwise supply credentials and mask the env fallback.
    monkeypatch.setattr(image_tools, "_get_model_config", lambda *a, **kw: {})


@pytest.fixture
def stub_model(monkeypatch, tmp_path):
    _isolate_image_gen(monkeypatch, tmp_path)
    monkeypatch.setenv("IMAGE_GEN_API_KEY", "test-key")
    monkeypatch.setenv("IMAGE_GEN_API_BASE", "https://example.invalid/api/v1")
    monkeypatch.setenv("IMAGE_GEN_MODEL_NAME", "wanx-v1")
    return _StubModel


@pytest.mark.asyncio
async def test_invoke_forwards_normalized_size(stub_model):
    result = await _invoke_model_image_generation("a cat", size="1024x1024")

    assert "error" not in result, result
    assert len(stub_model.calls) == 1
    assert stub_model.calls[0]["size"] == "1024*1024"


@pytest.mark.asyncio
async def test_invoke_omits_size_when_not_supplied(stub_model):
    result = await _invoke_model_image_generation("a cat")

    assert "error" not in result, result
    assert len(stub_model.calls) == 1
    # No size key at all, so the provider default applies.
    assert "size" not in stub_model.calls[0]


@pytest.mark.asyncio
async def test_invoke_omits_size_when_blank(stub_model):
    result = await _invoke_model_image_generation("a cat", size="   ")

    assert "error" not in result, result
    assert "size" not in stub_model.calls[0]


@pytest.mark.asyncio
async def test_invoke_writes_image_to_workspace(stub_model, tmp_path):
    result = await _invoke_model_image_generation("a cat", size="1664*928")

    image_path = result["image_path"]
    assert image_path.startswith(str(tmp_path))
    with open(image_path, "rb") as f:
        assert f.read() == b"stub-image-bytes"


@pytest.mark.asyncio
async def test_invoke_reports_missing_api_key(monkeypatch, tmp_path):
    _isolate_image_gen(monkeypatch, tmp_path)
    monkeypatch.delenv("IMAGE_GEN_API_KEY", raising=False)
    monkeypatch.delenv("API_KEY", raising=False)

    result = await _invoke_model_image_generation("a cat")

    assert "IMAGE_GEN_API_KEY" in result["error"]
    assert _StubModel.calls == []


def test_generate_image_tool_no_longer_advertises_quality():
    """quality has no destination in agent-core, so it must not reach the model card."""
    import inspect

    from jiuwenswarm.agents.harness.common.tools.image_tools import generate_image

    params = inspect.signature(generate_image._func).parameters
    assert "quality" not in params
    assert params["size"].default is None

    schema_props = generate_image.card.input_params["properties"]
    assert "quality" not in schema_props
    # The size hint must survive intact; the card keeps only the docstring's first line.
    assert "1664*928" in schema_props["size"]["description"]
