# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Steer image readers must belong to the active owner and reach host uploads."""

from types import SimpleNamespace

import pytest

from openjiuwen.core.sys_operation import OperationMode
from openjiuwen.harness.rails import _multimodal
from openjiuwen.harness.schema.config import VisionModelConfig

from jiuwenswarm.server.runtime.agent_adapter import interface_deep
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter as Adapter
from jiuwenswarm.server.runtime.agent_adapter.session_input import SessionInputImageUnavailableError


def _adapter(monkeypatch, *, native=None, declared=None, cached=None, read_enabled=True,
             operation_mode=OperationMode.LOCAL, read_present=True, vision_config=None):
    tools = {}
    if read_present:
        tools["read-id"] = SimpleNamespace(
            enable_image_multimodal=read_enabled, operation=SimpleNamespace(mode=operation_mode),
        )
    if vision_config is not None:
        tools["vision-id"] = SimpleNamespace(vision_model_config=vision_config)
    cards = []
    if read_present:
        cards.append(SimpleNamespace(name="read_file", id="read-id"))
    if vision_config is not None:
        cards.append(SimpleNamespace(name="visual_question_answering", id="vision-id"))
    adapter = Adapter()
    adapter._instance = SimpleNamespace(
        deep_config=SimpleNamespace(
            enable_read_image_multimodal=native,
            model=SimpleNamespace(model_client_config=SimpleNamespace(supports_vision=declared)),
        ),
        ability_manager=SimpleNamespace(list=lambda: cards),
    )
    # The inactive defaults deliberately claim the opposite capability.
    adapter._model = SimpleNamespace(model_client_config=SimpleNamespace(supports_vision=native is not True))
    adapter._config_cache = {"enable_read_image_multimodal": native is not True}
    monkeypatch.setattr(interface_deep.Runner.resource_mgr, "get_tool", tools.get)
    monkeypatch.setattr(_multimodal, "get_cached_image_support", lambda _model: cached)
    return adapter, tools


@pytest.mark.parametrize("native, declared, cached, enabled, expected", [
    (True, False, False, True, "read_file"),
    (False, True, True, True, None),
    (None, True, False, True, "read_file"),
    (None, False, True, True, None),
    (None, None, True, True, "read_file"),
    (None, None, False, True, None),
    (None, None, None, True, None),
    (True, True, True, False, None),
])
def test_reader_selection_uses_active_owner_policy_and_tool_flag(
    monkeypatch, native, declared, cached, enabled, expected,
):
    adapter, _ = _adapter(
        monkeypatch, native=native, declared=declared, cached=cached, read_enabled=enabled,
    )
    if expected is None:
        with pytest.raises(SessionInputImageUnavailableError, match="image-reading capability"):
            adapter._select_steer_image_tool()
    else:
        assert adapter._select_steer_image_tool() == expected


@pytest.mark.parametrize("operation_mode", [OperationMode.LOCAL, OperationMode.SANDBOX, None])
@pytest.mark.parametrize("configured_vision", [False, True])
def test_host_uploads_require_local_file_operation_or_configured_host_vision(
    monkeypatch, operation_mode, configured_vision,
):
    vision = VisionModelConfig(api_key="fixture", base_url="https://vision.invalid/v1", model="fixture")
    adapter, _ = _adapter(
        monkeypatch, native=True, operation_mode=operation_mode,
        vision_config=vision if configured_vision else None,
    )
    if operation_mode is OperationMode.LOCAL:
        assert adapter._select_steer_image_tool() == "read_file"
    elif configured_vision:
        assert adapter._select_steer_image_tool() == "visual_question_answering"
    else:
        with pytest.raises(SessionInputImageUnavailableError):
            adapter._select_steer_image_tool()


@pytest.mark.parametrize("missing_field", ["api_key", "base_url", "model"])
def test_incomplete_registered_vision_tool_cannot_accept_image_steer(monkeypatch, missing_field):
    config = VisionModelConfig(api_key="fixture", base_url="https://vision.invalid/v1", model="fixture")
    setattr(config, missing_field, "")
    adapter, _ = _adapter(monkeypatch, read_present=False, vision_config=config)
    with pytest.raises(SessionInputImageUnavailableError):
        adapter._select_steer_image_tool()


def test_registered_tool_card_without_resource_is_unavailable(monkeypatch):
    adapter, tools = _adapter(monkeypatch, native=True)
    tools.clear()
    with pytest.raises(SessionInputImageUnavailableError):
        adapter._select_steer_image_tool()


@pytest.mark.parametrize("read_present, read_enabled, native", [
    (False, True, True), (True, False, True), (True, True, False),
])
def test_configured_host_vision_falls_back_when_native_reader_is_unavailable(
    monkeypatch, read_present, read_enabled, native,
):
    vision = VisionModelConfig(api_key="fixture", base_url="https://vision.invalid/v1", model="fixture")
    adapter, _ = _adapter(
        monkeypatch, native=native, read_present=read_present, read_enabled=read_enabled, vision_config=vision,
    )
    assert adapter._select_steer_image_tool() == "visual_question_answering"
