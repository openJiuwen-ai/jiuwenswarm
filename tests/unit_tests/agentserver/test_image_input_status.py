# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Image routing stays conservative without treating unknown as unsupported."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from openjiuwen.harness import image_modality_probe

from jiuwenswarm.server.runtime.agent_adapter import interface_deep
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter as Adapter


def _model(supports_vision=None):
    return SimpleNamespace(
        model_config=SimpleNamespace(model_name="kimi-k2.6"),
        model_client_config=SimpleNamespace(api_base="https://example.invalid/v1", supports_vision=supports_vision),
    )


def _request():
    return SimpleNamespace(
        request_id="image-status-test", session_id="test-session",
        params={"query": "解释图片", "media_items": [
            {"type": "image", "path": "C:/tmp/sample.png", "mime_type": "image/png"},
        ]},
    )


@pytest.mark.parametrize("verdict, expected", [(True, "supported"), (False, "unsupported"), (None, "unknown")])
def test_cached_verdict_preserves_unknown_and_routing(monkeypatch, verdict, expected):
    monkeypatch.setattr(interface_deep, "get_cached_image_support", lambda _model: verdict)
    assert Adapter._native_image_input_status({}, _model()) == expected
    assert Adapter._native_image_input_enabled({}, _model()) is (verdict is True)


@pytest.mark.parametrize("declared, global_flag, expected", [
    (True, False, "supported"), (False, True, "disabled"),
    (None, True, "supported"), (None, False, "disabled"),
])
def test_explicit_settings_keep_precedence_without_probing(monkeypatch, declared, global_flag, expected):
    read_cache = Mock(side_effect=AssertionError("explicit settings must not inspect probes"))
    monkeypatch.setattr(interface_deep, "get_cached_image_support", read_cache)
    assert Adapter._native_image_input_status(
        {"enable_read_image_multimodal": global_flag}, _model(declared),
    ) == expected
    read_cache.assert_not_called()


@pytest.mark.parametrize("vision_tool_available", [False, True])
@pytest.mark.parametrize("status, wording", [
    ("unknown", "尚未确认"), ("disabled", "配置中关闭"),
    ("unsupported", "经检测不支持"),
])
def test_notice_and_prompt_agree_without_changing_attachment_routing(
    monkeypatch, status, wording, vision_tool_available,
):
    monkeypatch.setattr(
        interface_deep, "get_cached_image_support",
        lambda _model: False if status == "unsupported" else None,
    )
    model = _model(False) if status == "disabled" else _model()
    snapshot = Adapter._native_image_input_status({}, model)
    assert snapshot == status
    request = _request()
    inputs = Adapter._prepare_multimodal_image_inputs(request, {"query": request.params["query"]})
    notice = Adapter._build_image_tool_fallback_notice(
        request, enable_read_image_multimodal=False, model=model,
        vision_tool_available=vision_tool_available, image_input_status=snapshot,
    )
    updated = Adapter._prepare_react_image_tool_prompt(
        request, inputs, enable_read_image_multimodal=False,
        vision_tool_available=vision_tool_available, image_input_status=snapshot,
    )
    context = json.loads(updated["query"].split("图片附件上下文（供 ReAct 选择图片理解工具使用）：\n", 1)[1])
    assert notice["image_input_status"] == context["imageInputStatus"] == status
    assert wording in notice["content"]
    assert wording in context["toolHint"]
    if status != "unsupported":
        assert "经检测不支持" not in notice["content"]
        assert "当前主模型不支持" not in context["toolHint"]
        assert "当前没有图片理解能力" not in context["toolHint"]
    assert "_multimodal_image_files" not in updated
    assert context["mediaPath"] == "C:/tmp/sample.png"
    assert "_multimodal_image_files" in inputs  # original input is not mutated
    if vision_tool_available:
        assert "image_reading(" in context["toolHint"]
        assert "已切换" in notice["content"]
    else:
        assert "不要猜测图片内容" in context["toolHint"]
        assert "未配置可用的视觉模型工具" in notice["content"]


def test_supported_images_and_text_only_requests_have_no_fallback():
    request = _request()
    inputs = Adapter._prepare_multimodal_image_inputs(request, {"query": "解释图片"})
    assert Adapter._prepare_react_image_tool_prompt(
        request, inputs, enable_read_image_multimodal=True,
        vision_tool_available=False, image_input_status="supported",
    ) is inputs
    assert Adapter._build_image_tool_fallback_notice(
        request, enable_read_image_multimodal=True, model=_model(), vision_tool_available=False,
        image_input_status="supported",
    ) is None
    request.params.pop("media_items")
    assert Adapter._build_image_tool_fallback_notice(
        request, enable_read_image_multimodal=False, model=_model(), vision_tool_available=False,
    ) is None
    assert Adapter._prepare_react_image_tool_prompt(
        request, {"query": "hello"}, enable_read_image_multimodal=False, vision_tool_available=False,
    ) == {"query": "hello"}


@pytest.mark.asyncio
async def test_existing_core_timeout_stays_unknown_and_recovery_enables_images():
    probe = image_modality_probe
    probe.reset_image_support_cache()
    model = _model()
    model.invoke = AsyncMock(side_effect=asyncio.TimeoutError())
    try:
        assert await probe.probe_image_support(model) is None
        status = Adapter._native_image_input_status({}, model)
        assert status == "unknown"
        assert not Adapter._native_image_input_enabled({}, model)
        notice = Adapter._build_image_tool_fallback_notice(
            _request(), enable_read_image_multimodal=False, model=model,
            vision_tool_available=False, image_input_status=status,
        )
        assert "尚未确认" in notice["content"]
        assert "超时" not in notice["content"]
        assert "不支持" not in notice["content"]
        model.invoke = AsyncMock(return_value=SimpleNamespace(content="red", finish_reason="stop"))
        assert await probe.probe_image_support(model) is True
        assert Adapter._native_image_input_enabled({}, model)
    finally:
        probe.reset_image_support_cache()
