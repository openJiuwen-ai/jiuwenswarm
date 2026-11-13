# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Catalogue transitions and migration preserve independently specified intent."""

from copy import deepcopy
from uuid import uuid4

import pytest

from jiuwenswarm.common.multimodal_models import MultimodalError, transition, validate_catalogue, validate_model
from jiuwenswarm.common.multimodal_projection import migrate_catalogue, project_catalogue


def empty_state():
    return migrate_catalogue({}, {})


def model_for(capabilities, name="model-a"):
    generation = capabilities[0] in ("visual_gen", "video_gen")
    return {"model_id": f"mm_{uuid4()}", "display_name": name,
            "purpose": "generation" if generation else "understanding", "capabilities": capabilities,
            "connection": {"provider": "OpenAI", "protocol": "openai", "api_base": "https://example.invalid/v1",
                           "api_key": "test-key", "model_name": name,
                           **({} if generation else {"context_window_tokens": 262144})}}


def create(state, model):
    return transition(state, "models.upsert", {"mode": "create", "model": model})[0]


def test_new_model_binds_all_unbound_capabilities_without_overwriting_choice_or_switch():
    first = model_for(["vision"])
    state = create(empty_state(), first)
    state, _ = transition(state, "tools.update", {"tool_id": "vision", "enabled": False})
    second = model_for(["vision", "audio"])
    state = create(state, second)
    assert state["tools"]["vision"] == {"model_id": first["model_id"], "enabled": False}
    assert state["tools"]["audio"] == {"model_id": second["model_id"], "enabled": True}


def test_edit_adds_capability_and_updates_connection_for_enabled_and_disabled_references():
    model = model_for(["vision", "audio"])
    state = create(empty_state(), model)
    state["tools"]["audio"]["enabled"] = False
    model["capabilities"].append("video")
    model["connection"]["api_base"] = "https://updated.invalid/v1"
    candidate, result = transition(state, "models.upsert", {"mode": "update", "model": model})
    assert candidate["tools"]["audio"]["enabled"] is False
    assert candidate["tools"]["video"]["enabled"] is True
    assert result["changed_tools"] == ["vision", "audio", "video"]
    _, env = project_catalogue({}, candidate)
    assert {env[f"{tool}_API_BASE"] for tool in ("VISION", "AUDIO", "VIDEO")} == {"https://updated.invalid/v1"}


@pytest.mark.parametrize("operation", ["models.delete", "models.upsert"])
def test_reference_confirmation_includes_off_tools_and_fallback_preserves_each_switch(operation):
    a = model_for(["vision", "audio"])
    b = model_for(["vision"])
    state = create(create(empty_state(), a), b)
    state["tools"]["vision"]["enabled"] = False
    params = {"model_id": a["model_id"]}
    if operation == "models.upsert":
        edited = deepcopy(a)
        edited["purpose"] = "generation"
        edited["capabilities"] = ["video_gen"]
        edited["connection"].pop("context_window_tokens")
        params.update(mode="update", model=edited)
    original = deepcopy(state)
    with pytest.raises(MultimodalError) as error:
        transition(state, operation, params)
    assert error.value.code == "MM_CONFIRM_REQUIRED"
    assert error.value.payload["affected_tools"] == ["vision", "audio"]
    assert state == original
    candidate, _ = transition(state, operation, {**params, "confirmed_tools": ["vision", "audio"]})
    assert candidate["tools"]["vision"] == {"model_id": b["model_id"], "enabled": False}
    assert candidate["tools"]["audio"] == {"model_id": None, "enabled": False}


@pytest.mark.parametrize("operation", ["models.upsert", "models.delete"])
def test_unreferenced_capability_edit_or_model_delete_needs_no_reference_confirmation(operation):
    original = model_for(["vision", "audio"])
    candidate = model_for(["vision", "audio"])
    state = create(create(empty_state(), original), candidate)
    bindings = deepcopy(state["tools"])
    params = {}
    if operation == "models.upsert":
        edited = deepcopy(candidate)
        edited["capabilities"] = ["vision"]
        params.update(mode="update", model=edited)
    else:
        params["model_id"] = candidate["model_id"]
    updated, result = transition(state, operation, params)
    assert updated["tools"] == bindings
    assert result["changed"] is True


def test_fallback_uses_persisted_order_and_not_name():
    models = [model_for(["vision"], name) for name in ("z", "y", "a")]
    state = empty_state()
    for model in models:
        state = create(state, model)
    candidate, _ = transition(state, "models.delete", {"model_id": models[0]["model_id"],
                                                       "confirmed_tools": ["vision"]})
    assert candidate["tools"]["vision"]["model_id"] == models[1]["model_id"]


def test_later_save_updates_target_model_without_overwriting_other_models_or_tool_choices():
    first = model_for(["vision"])
    second = model_for(["audio"])
    state = create(create(empty_state(), first), second)
    earlier_draft = deepcopy(first)
    first["display_name"] = "saved first"
    state, _ = transition(state, "models.upsert", {"mode": "update", "model": first})
    state, _ = transition(state, "tools.update", {"tool_id": "audio", "enabled": False})
    earlier_draft["display_name"] = "saved later"
    updated, _ = transition(state, "models.upsert", {"mode": "update", "model": earlier_draft})
    assert updated["models"][0]["display_name"] == "saved later"
    assert updated["models"][1] == state["models"][1]
    assert updated["tools"] == state["tools"]


def test_edit_deleted_model_cannot_resurrect_it():
    model = model_for(["vision"])
    state = create(empty_state(), model)
    state, _ = transition(state, "models.delete", {"model_id": model["model_id"], "confirmed_tools": ["vision"]})
    with pytest.raises(MultimodalError, match="MM_MODEL_NOT_FOUND"):
        transition(state, "models.upsert", {"mode": "update", "model": model})
    assert state["models"] == []


def test_changed_references_require_current_confirmation_before_deletion():
    first = model_for(["vision", "audio"])
    second = model_for(["audio"])
    state = create(create(empty_state(), first), second)
    state, _ = transition(state, "tools.update", {"tool_id": "audio", "model_id": second["model_id"]})
    with pytest.raises(MultimodalError, match="MM_CONFIRM_REQUIRED") as error:
        transition(state, "models.delete", {"model_id": first["model_id"], "confirmed_tools": ["vision", "audio"]})
    assert error.value.payload["affected_tools"] == ["vision"]
    assert "revision" not in error.value.payload
    assert state["models"][0]["model_id"] == first["model_id"]


def test_noop_preserves_catalogue_and_enable_without_model_is_rejected():
    state = empty_state()
    with pytest.raises(MultimodalError, match="MM_MODEL_REQUIRED"):
        transition(state, "tools.update", {"tool_id": "vision", "enabled": True})
    candidate, result = transition(state, "tools.update", {"tool_id": "vision", "enabled": False})
    assert candidate == state and result["changed"] is False


def test_migration_merges_same_purpose_and_complete_config_but_keeps_switches():
    env = {}
    for prefix in ("VISION", "AUDIO", "VIDEO", "VISUAL_GEN", "VIDEO_GEN"):
        env.update({f"{prefix}_API_BASE": "https://example.invalid", f"{prefix}_API_KEY": "key",
                    f"{prefix}_MODEL_NAME": "same", f"{prefix}_PROVIDER": "OpenAI"})
    env["AUDIO_ENABLED"] = "false"
    state = migrate_catalogue({}, env)
    assert len(state["models"]) == 2
    assert state["models"][0]["capabilities"] == ["vision", "audio", "video"]
    assert state["models"][1]["capabilities"] == ["visual_gen", "video_gen"]
    assert state["tools"]["vision"]["enabled"] is True
    assert state["tools"]["audio"]["enabled"] is False
    assert state["tools"]["video_gen"]["enabled"] is False


@pytest.mark.parametrize("difference", ["context", "client_options", "alias"])
def test_migration_does_not_merge_advanced_configuration_differences(difference):
    client = {"api_base": "https://example.invalid", "api_key": "key", "model_name": "same", "client_provider": "OpenAI"}
    raw = {"models": {"vision": {"model_client_config": deepcopy(client)}, "audio": {"model_config": deepcopy(client)}}}
    env = {}
    if difference == "context":
        raw["models"]["vision"]["model_client_config"]["context_window_tokens"] = 100
    elif difference == "client_options":
        raw["models"]["vision"]["model_client_config"]["timeout"] = 9
    else:
        env["VISION_MODEL"] = "different"
    state = migrate_catalogue(raw, env)
    assert len(state["models"]) == 2


@pytest.mark.parametrize("effective_name,expected_count", [("real-working-model", 1), ("your-vision-model-name", 0)])
def test_placeholder_filter_uses_the_effective_execution_model(effective_name, expected_count):
    state = migrate_catalogue({}, {
        "VISION_MODEL_NAME": "your-vision-model-name" if expected_count else "unused-name",
        "VISION_MODEL": effective_name, "VISION_BASE_URL": "https://effective.invalid/v1",
        "VISION_API_KEY": "key", "VISION_PROVIDER": "OpenAI",
    })
    assert len(state["models"]) == expected_count
    assert state["tools"]["vision"]["enabled"] is bool(expected_count)
    if expected_count:
        assert state["models"][0]["connection"]["model_name"] == effective_name


def test_partial_history_migrates_without_validation_or_inventing_defaults():
    state = migrate_catalogue({}, {"VISION_API_BASE": "https://partial.invalid", "VISION_ENABLED": "true"})
    model = state["models"][0]
    assert model["connection"] == {"protocol": "openai", "api_base": "https://partial.invalid"}
    assert state["tools"]["vision"]["enabled"] is True
    with pytest.raises(MultimodalError, match="MM_VALIDATION_FAILED"):
        validate_model({key: value for key, value in model.items() if key != "legacy_options"})


@pytest.mark.parametrize("tool", ["vision", "audio", "video"])
@pytest.mark.parametrize("other_fields_filled", [False, True])
def test_template_model_name_alone_excludes_historical_slot(tool, other_fields_filled):
    prefix = tool.upper()
    env = {f"{prefix}_MODEL_NAME": f"your-{tool}-model-name", f"{prefix}_ENABLED": "true"}
    if other_fields_filled:
        env.update({f"{prefix}_API_BASE": "https://filled.invalid", f"{prefix}_API_KEY": "filled-key",
                    f"{prefix}_PROVIDER": "OpenAI"})
    state = migrate_catalogue({}, env)
    assert state["models"] == []
    assert state["tools"][tool] == {"model_id": None, "enabled": False}
    model = model_for([tool])
    state = create(state, model)
    assert state["tools"][tool] == {"model_id": model["model_id"], "enabled": True}


@pytest.mark.parametrize("name", ["your-vision-model-name-custom", "my-your-vision-model-name", "YOUR-VISION-MODEL-NAME"])
def test_placeholder_filter_uses_exact_name_and_preserves_partial_history(name):
    state = migrate_catalogue({}, {"VISION_MODEL_NAME": name})
    assert state["models"][0]["connection"]["model_name"] == name


def test_migration_canonicalizes_effective_vision_aliases_before_merging():
    env = {"VISION_API_BASE": "https://unused.invalid", "VISION_BASE_URL": "https://effective.invalid",
           "VISION_MODEL_NAME": "unused", "VISION_MODEL": "effective", "VISION_API_KEY": "key",
           "VISION_PROVIDER": "OpenAI", "AUDIO_API_BASE": "https://effective.invalid",
           "AUDIO_MODEL_NAME": "effective", "AUDIO_API_KEY": "key", "AUDIO_PROVIDER": "OpenAI"}
    state = migrate_catalogue({}, env)
    assert len(state["models"]) == 1
    model = state["models"][0]
    assert model["capabilities"] == ["vision", "audio"]
    assert model["display_name"] == "effective"
    assert model["connection"]["api_base"] == "https://effective.invalid"
    assert model["connection"]["model_name"] == "effective"
    assert "environment_overrides" not in model["legacy_options"]


@pytest.mark.parametrize("change_name", [False, True])
def test_audio_specialized_names_survive_until_unified_model_name_changes(change_name):
    state = migrate_catalogue({}, {"AUDIO_API_BASE": "https://audio.invalid", "AUDIO_API_KEY": "key",
                                  "AUDIO_MODEL_NAME": "old", "AUDIO_PROVIDER": "OpenAI",
                                  "AUDIO_TRANSCRIPTION_MODEL": "transcription", "AUDIO_QUESTION_ANSWERING_MODEL": "qa"})
    edited = {key: deepcopy(value) for key, value in state["models"][0].items() if key != "legacy_options"}
    edited["display_name"] = "renamed"
    if change_name:
        edited["connection"]["model_name"] = "new"
    state, _ = transition(state, "models.upsert", {"mode": "update", "model": edited})
    _, env = project_catalogue({}, state)
    assert (env["AUDIO_TRANSCRIPTION_MODEL"] or env["AUDIO_MODEL_NAME"]) == ("new" if change_name else "transcription")
    assert (env["AUDIO_QUESTION_ANSWERING_MODEL"] or env["AUDIO_MODEL_NAME"]) == ("new" if change_name else "qa")


@pytest.mark.parametrize("list_models", [False, True])
def test_projection_preserves_ordinary_models_and_clears_old_aliases(list_models):
    a = model_for(["vision", "audio"])
    state = create(empty_state(), a)
    state["models"][0]["legacy_options"] = {"client_options": {"timeout": 9}, "model_options": {"temperature": 0.5}}
    ordinary = {"defaults": [{"model_name": "chat"}], "image_gen": {"keep": "unchanged"}}
    raw = {"models": [{key: value} for key, value in ordinary.items()] if list_models else ordinary}
    projected, env = project_catalogue(raw, state)
    assert env["VISION_MODEL"] is None
    assert env["AUDIO_TRANSCRIPTION_MODEL"] is None
    assert env["VISION_ENABLED"] == "true"
    if list_models:
        assert projected["models"][:2] == raw["models"]
    else:
        assert projected["models"]["defaults"] == ordinary["defaults"]
        assert projected["models"]["image_gen"] == ordinary["image_gen"]


@pytest.mark.parametrize("capabilities", [["vision", "video_gen"], [], ["vision", "vision"]])
def test_invalid_purpose_capabilities_rejected(capabilities):
    model = model_for(["vision"])
    model["capabilities"] = capabilities
    with pytest.raises(MultimodalError, match="MM_VALIDATION_FAILED"):
        validate_model(model)


def test_generation_optional_credentials_and_utf16_limits_match_existing_form():
    model = model_for(["video_gen"])
    model["connection"].update(protocol="vllm-omni", api_key="", model_name="")
    assert validate_model(model)["connection"]["model_name"] == ""
    model["display_name"] = "😀" * 51
    with pytest.raises(MultimodalError, match="MM_VALIDATION_FAILED"):
        validate_model(model)


def test_generation_trims_protocol_before_applying_optional_credentials():
    model = model_for(["video_gen"])
    model["connection"].update(protocol=" vllm-omni ", api_key="", model_name="")
    assert validate_model(model)["connection"]["protocol"] == "vllm-omni"


@pytest.mark.parametrize("api_base", [None, [], 123])
def test_malformed_ark_address_returns_field_validation_error(api_base):
    model = model_for(["visual_gen"])
    model["connection"].update(protocol="modelark", api_base=api_base)
    with pytest.raises(MultimodalError, match="MM_VALIDATION_FAILED") as error:
        validate_model(model)
    assert {"field": "connection.api_base", "key": "invalid"} in error.value.payload["field_errors"]


def test_malformed_confirmation_cannot_delete_referenced_model():
    model = model_for(["vision"])
    state = create(empty_state(), model)
    with pytest.raises(MultimodalError, match="MM_CONFIRM_REQUIRED"):
        transition(state, "models.delete", {"model_id": model["model_id"],
                                            "confirmed_tools": ["vision", None]})
    assert state["tools"]["vision"]["model_id"] == model["model_id"]


def test_boolean_is_not_a_catalogue_schema_version():
    state = empty_state()
    state["schema_version"] = True
    with pytest.raises(MultimodalError, match="MM_UNSUPPORTED_SCHEMA"):
        validate_catalogue(state)
