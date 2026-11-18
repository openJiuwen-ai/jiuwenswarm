# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Design generation uses the multimodal catalogue, not the agent tool binding."""

import json
from uuid import uuid4

import pytest

from jiuwenswarm.common.config import dump_yaml_round_trip, load_yaml_round_trip
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
    catalogue_generation_model,
    generation_selection_view,
    reuse_catalogue_snapshot,
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
    tools = store.read()["tools"]
    activation_tools = [tool for tool in model["capabilities"] if not tools[tool]["enabled"]]
    store.mutate("models.upsert", {
        "mode": "create",
        "model": model,
        "tool_activation": {"tools": activation_tools, "enabled": True},
    })
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


def test_unsupported_catalogue_protocol_is_not_a_choice(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _prepare(monkeypatch)
    monkeypatch.setenv("VIDEO_GEN_ENABLED", "false")
    blocked = _create(store, _model(
        "video_gen",
        "open-video",
        provider="OpenAI",
        protocol="openai",
        api_base="https://example.invalid/v1",
        api_key="sk-open",
        model_name="doubao-seedream-test",
    ))
    allowed = _create(store, _model("video_gen", "wan-a"))
    _create(store, _model("visual_gen", "qwen-image"))
    store.mutate("tools.update", {"tool_id": "video_gen", "enabled": False})
    raw = load_yaml_round_trip(get_config_file())
    raw["design_generation"] = {"video_gen": blocked["model_id"]}
    dump_yaml_round_trip(get_config_file(), raw)

    assert generation_selection_view()["video_gen"] == allowed["model_id"]
    assert mg.slot_settings("video").model == "wan-a"
    assert mg.generation_problem("video") is None
    assert load_yaml_round_trip(get_config_file())["design_generation"]["video_gen"] == blocked["model_id"]
    with pytest.raises(DesignSelectionError, match="protocol is not supported"):
        set_generation_selection("video_gen", blocked["model_id"])
    assert load_yaml_round_trip(get_config_file())["design_generation"]["video_gen"] == blocked["model_id"]

    store.mutate("models.delete", {"model_id": allowed["model_id"]})
    problem = mg.generation_problem("video") or ""
    assert "add a model" in problem
    assert "not one of them" not in problem
    assert "switched off" not in problem
    assert generation_selection_view()["video_gen"] is None


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


def _count_catalogue_yaml_reads(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Count YAML reads performed by MultimodalStore catalogue loads."""
    import jiuwenswarm.common.multimodal_store as multimodal_store

    counts = {"n": 0}
    original = multimodal_store.load_yaml_round_trip

    def _counted(*args: object, **kwargs: object):
        counts["n"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(multimodal_store, "load_yaml_round_trip", _counted)
    return counts


def test_catalogue_resolution_parses_the_file_once(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _prepare(monkeypatch)
    _create(store, _model("video_gen", "wan-once"))
    counts = _count_catalogue_yaml_reads(monkeypatch)
    found = catalogue_generation_model("video_gen")
    assert found.present
    assert found.model is not None
    assert counts["n"] == 1


def test_prompt_budget_wave_reuses_one_catalogue_read(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _prepare(monkeypatch)
    _create(store, _model("video_gen", "wan-wave"))
    _create(store, _model("visual_gen", "qwen-wave"))
    counts = _count_catalogue_yaml_reads(monkeypatch)
    from jiuwenswarm.server.runtime.designer.pipeline.media_prompt_limits import (
        media_prompt_limit_packet,
        resolve_prompt_limit,
    )

    with reuse_catalogue_snapshot():
        for _ in range(120):
            resolve_prompt_limit("image")
            resolve_prompt_limit("video")
            media_prompt_limit_packet()
    assert counts["n"] == 1

    with reuse_catalogue_snapshot():
        resolve_prompt_limit("video")
    assert counts["n"] == 2


def test_enforce_leaf_agents_loads_the_catalogue_once(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _prepare(monkeypatch)
    _create(store, _model("video_gen", "wan-leaf"))
    _create(store, _model("visual_gen", "qwen-leaf"))
    counts = _count_catalogue_yaml_reads(monkeypatch)
    from jiuwenswarm.server.runtime.designer.orchestration import Director

    graph = {
        "nodes": [
            {"id": f"clip_{index}", "config": {"role": "clip"}}
            for index in range(120)
        ]
    }
    Director._enforce_leaf_agents(graph)
    assert counts["n"] == 1
    assert "media_prompt_limits" in graph["nodes"][0]["config"]
    assert graph["nodes"][0]["config"]["prompt_char_limit"]


def test_stale_multimodal_journal_resolves_as_catalogue_error(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _prepare(monkeypatch)
    _create(store, _model("video_gen", "wan-journal"))
    get_config_file().with_name(".multimodal-transaction.json").write_text(
        json.dumps({
            "transaction_id": "deadbeef",
            "old_yaml_hash": "0" * 64,
            "new_yaml_hash": "1" * 64,
            "old_env": "",
            "new_env": "",
        }),
        encoding="utf-8",
    )
    found = catalogue_generation_model("video_gen")
    assert found.present is True
    assert found.model is None
    assert generation_selection_view() == {"video_gen": None, "visual_gen": None}
    with reuse_catalogue_snapshot():
        nested = catalogue_generation_model("video_gen")
        assert nested.present is True
        assert nested.model is None


def test_config_lock_timeout_resolves_as_catalogue_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import jiuwenswarm.server.runtime.designer.generation_selection as selection

    store = _prepare(monkeypatch)
    _create(store, _model("video_gen", "wan-lock"))

    class _TimeoutLock:
        def __call__(self, **_kwargs: object):
            return self

        def __enter__(self):
            raise TimeoutError("config write lock timed out")

        def __exit__(self, *_args: object) -> bool:
            return False

    monkeypatch.setattr(selection, "config_write_lock", _TimeoutLock())
    found = catalogue_generation_model("video_gen")
    assert found.present is True
    assert found.model is None
