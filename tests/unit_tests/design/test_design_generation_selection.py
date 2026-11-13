# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Design generation uses the multimodal catalogue, not the agent tool binding."""

from uuid import uuid4

import pytest

from jiuwenswarm.common.config import load_yaml_round_trip
from jiuwenswarm.common.config_panel.config_set_handlers import (
    ConfigPanelBadRequest,
    apply_config_payload,
)
from jiuwenswarm.common.multimodal_models import CAPABILITIES
from jiuwenswarm.common.multimodal_projection import ALIASES, FIELDS
from jiuwenswarm.common.multimodal_store import MultimodalStore
from jiuwenswarm.common.utils import get_config_file
from jiuwenswarm.server.runtime.designer import media_generation as mg
from jiuwenswarm.server.runtime.designer.generation_selection import (
    DesignSelectionError,
    generation_selection_view,
    set_generation_selection,
)


def _model(capability: str, name: str, **connection: str) -> dict:
    return {
        "model_id": f"mm_{uuid4()}",
        "display_name": name,
        "purpose": "generation",
        "capabilities": [capability],
        "connection": {
            "provider": "DashScope",
            "protocol": "dashscope",
            "api_base": "https://dashscope.aliyuncs.com/api/v1",
            "api_key": f"sk-{name}",
            "model_name": name,
            **connection,
        },
    }


def _prepare(monkeypatch: pytest.MonkeyPatch) -> MultimodalStore:
    for tool in CAPABILITIES:
        for suffix in (*FIELDS.values(), "ENABLED", *ALIASES.get(tool, ())):
            monkeypatch.setenv(f"{tool.upper()}_{suffix}", "")
    monkeypatch.setattr(
        "jiuwenswarm.common.multimodal_store.get_crypto_provider",
        lambda: None,
    )
    store = MultimodalStore()
    store.initialize()
    return store


def _create(store: MultimodalStore, model: dict) -> dict:
    store.mutate("models.upsert", {"mode": "create", "model": model})
    return model


def test_without_a_catalogue_design_still_uses_the_environment_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VIDEO_GEN_ENABLED", "false")
    assert "switched off" in (mg.generation_problem("video") or "")
    monkeypatch.setenv("VIDEO_GEN_ENABLED", "true")
    monkeypatch.setenv("VIDEO_GEN_API_KEY", "sk-env")
    monkeypatch.setenv("VIDEO_GEN_API_BASE", "https://dashscope.aliyuncs.com/api/v1")
    monkeypatch.setenv("VIDEO_GEN_MODEL_NAME", "wan-env")
    monkeypatch.setenv("VIDEO_GEN_PROTOCOL", "dashscope")
    assert mg.generation_problem("video") is None
    assert mg.slot_settings("video").model == "wan-env"


def test_stored_catalogue_ignores_the_agent_switch_and_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _prepare(monkeypatch)
    monkeypatch.setenv("VIDEO_GEN_ENABLED", "false")
    monkeypatch.setenv("VIDEO_GEN_API_KEY", "sk-env")
    monkeypatch.setenv("VIDEO_GEN_API_BASE", "https://example.invalid/v1")
    monkeypatch.setenv("VIDEO_GEN_MODEL_NAME", "env-model")
    monkeypatch.setenv("VIDEO_GEN_PROTOCOL", "openai")
    assert mg.generation_enabled("video") is False
    assert mg.slot_settings("video").model == ""
    problem = mg.generation_problem("video") or ""
    assert "add a model" in problem
    assert "switched off" not in problem

    first = _create(store, _model("video_gen", "wan-a"))
    second = _create(store, _model("video_gen", "wan-b"))
    image = _create(store, _model("visual_gen", "qwen-image"))
    store.mutate("tools.update", {"tool_id": "video_gen", "enabled": False})
    store.mutate("tools.update", {"tool_id": "visual_gen", "enabled": False})

    slot = mg.slot_settings("video")
    assert slot.model == "wan-a"
    assert slot.api_key == "sk-wan-a"
    assert slot.api_base == "https://dashscope.aliyuncs.com/api/v1"
    assert slot.backend == "dashscope"
    assert mg.generation_enabled("video") is True
    assert mg.generation_problem("video") is None
    assert mg.generation_enabled("image") is True
    assert mg.generation_problem("image") is None
    assert generation_selection_view() == {"video_gen": first["model_id"], "visual_gen": image["model_id"]}

    assert set_generation_selection("video_gen", second["model_id"])["video_gen"] == second["model_id"]
    assert mg.slot_settings("video").model == "wan-b"
    assert mg.slot_settings("image").model == "qwen-image"

    store.mutate("models.delete", {"model_id": second["model_id"]})
    assert generation_selection_view()["video_gen"] == first["model_id"]
    assert mg.slot_settings("video").model == "wan-a"
    saved = load_yaml_round_trip(get_config_file())["design_generation"]["video_gen"]
    assert saved == second["model_id"]

    with pytest.raises(DesignSelectionError):
        set_generation_selection("vision", first["model_id"])
    with pytest.raises(DesignSelectionError):
        set_generation_selection("video_gen", "not-a-model-id")


def test_config_set_records_the_design_choice(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _prepare(monkeypatch)
    video = _create(store, _model("video_gen", "wan-a"))
    other = _create(store, _model("video_gen", "wan-b"))
    _create(store, _model("visual_gen", "qwen-image"))

    result = apply_config_payload({"design_generation_video_gen": other["model_id"]})
    assert "design_generation_video_gen" in result.yaml_updated
    assert mg.slot_settings("video").model == "wan-b"
    assert load_yaml_round_trip(get_config_file())["design_generation"]["video_gen"] == other["model_id"]
    assert mg.slot_settings("image").model == "qwen-image"

    blank = apply_config_payload({"design_generation_video_gen": ""})
    assert "design_generation_video_gen" not in blank.yaml_updated
    assert load_yaml_round_trip(get_config_file())["design_generation"]["video_gen"] == other["model_id"]

    with pytest.raises(ConfigPanelBadRequest):
        apply_config_payload({"design_generation_video_gen": "not-a-model-id"})
    assert mg.slot_settings("video").model == "wan-b"
    assert video["model_id"] != other["model_id"]


def test_unsupported_catalogue_backend_is_reported_even_when_the_tool_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _prepare(monkeypatch)
    monkeypatch.setenv("VIDEO_GEN_ENABLED", "false")
    video = _model(
        "video_gen",
        "open-video",
        provider="OpenAI",
        protocol="openai",
        api_base="https://example.invalid/v1",
        api_key="sk-open",
        model_name="open-video",
    )
    _create(store, video)
    _create(store, _model("visual_gen", "qwen-image"))
    store.mutate("tools.update", {"tool_id": "video_gen", "enabled": False})
    problem = mg.generation_problem("video") or ""
    assert "not one of them" in problem
    assert "Models > Multimodal" in problem
    assert "switched off" not in problem


def test_vllm_omni_catalogue_model_needs_only_the_url(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _prepare(monkeypatch)
    video = _model(
        "video_gen",
        "local-video",
        protocol="vllm-omni",
        api_base="http://127.0.0.1:8091/v1",
        api_key="",
        model_name="",
    )
    _create(store, video)
    store.mutate("tools.update", {"tool_id": "video_gen", "enabled": False})
    assert mg.generation_problem("video") is None
    assert mg.slot_settings("video").backend == "vllm-omni"
    assert mg.slot_settings("video").api_key == ""
