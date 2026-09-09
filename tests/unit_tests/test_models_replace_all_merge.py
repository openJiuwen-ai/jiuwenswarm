# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for the replace_all merge helper preserving YAML env-var placeholders."""

from jiuwenswarm.gateway.channel_manager.web.app_web_handlers import (
    _is_env_var_placeholder,
    _merge_models_for_replace_all,
    _reasoning_level_display,
    _values_match,
)


class _StubCrypto:
    """Predictable crypto stub: encrypt(s) -> 'enc:' + s, used to make assertions
    on the ciphertext path without depending on the real crypto provider."""

    @staticmethod
    def encrypt(value: str) -> str:
        return f"enc:{value}"

    @staticmethod
    def decrypt(value: str) -> str:
        return value.removeprefix("enc:")


def _raw_entry_with_placeholder() -> dict:
    """A persisted YAML entry that uses env-var placeholders and ciphertext."""
    return {
        "model_client_config": {
            "api_base": "${API_BASE:-https://api.example.com}",
            "api_key": "${API_KEY}",
            "model_name": "gpt-4o",
            "client_provider": "OpenAI",
            "timeout": 1800,
            "verify_ssl": False,
            "custom_headers": {"X-Trace-Id": "abc"},
        },
        "model_config_obj": {
            "temperature": 0.95,
        },
        "is_default": True,
        "alias": "gpt",
    }


def _resolved_entry_for(raw: dict, *, api_key_plain: str, api_base: str) -> dict:
    """Mimic what get_default_models() produces: env-vars expanded, api_key decrypted."""
    raw_mcc = raw["model_client_config"]
    resolved_mcc = {
        "api_base": api_base,
        "api_key": api_key_plain,
        "model_name": raw_mcc["model_name"],
        "client_provider": raw_mcc["client_provider"],
        "timeout": raw_mcc["timeout"],
        "verify_ssl": raw_mcc["verify_ssl"],
    }
    if "custom_headers" in raw_mcc:
        resolved_mcc["custom_headers"] = raw_mcc["custom_headers"]
    return {
        "model_client_config": resolved_mcc,
        "model_config_obj": dict(raw["model_config_obj"]),
        "is_default": raw["is_default"],
        "alias": raw["alias"],
    }


def test_values_match_normalizes_numeric_string_from_env_resolution():
    # ${TEMP:-0.95} resolves to the string "0.95"; frontend sends 0.95 as float.
    assert _values_match(0.95, "0.95")
    assert _values_match("1800", 1800)
    assert not _values_match(0.5, 0.95)


def test_values_match_treats_none_and_empty_as_equal():
    assert _values_match("", None)
    assert _values_match(None, None)


def test_reasoning_level_display_maps_legacy_yaml_booleans():
    # Bare on/off YAML scalars load as booleans under YAML 1.1 loaders.
    assert _reasoning_level_display(True) == "on"
    assert _reasoning_level_display(False) == "off"
    assert _reasoning_level_display(None) == ""
    assert _reasoning_level_display("") == ""
    assert _reasoning_level_display("high") == "high"


def test_clearing_reasoning_level_over_legacy_boolean_off_removes_it():
    """Regression: stored bare `off` loads as False; switching the UI back to
    "default" (empty level) must delete the stored value instead of being
    swallowed by _values_match's boolean branch (bool("")==bool(False))."""
    raw = _raw_entry_with_placeholder()
    raw["model_config_obj"]["reasoning_level"] = False  # legacy bare `off`
    resolved = _resolved_entry_for(raw, api_key_plain="sk-real-secret", api_base="https://api.example.com")

    parsed = [{
        "model_name": "gpt-4o",
        "api_base": "https://api.example.com",
        "api_key": "sk-real-secret",
        "model_provider": "OpenAI",
        "temperature": 0.95,
        "timeout": 1800,
        "verify_ssl": False,
        "is_default": True,
        "alias": "gpt",
        "origin_index": 0,
        "reasoning_level": "",
    }]

    out = _merge_models_for_replace_all(parsed, [raw], [resolved], crypto=_StubCrypto())

    assert "reasoning_level" not in out[0]["model_config_obj"]


def test_reasoning_level_over_legacy_boolean_on_is_rewritten_as_string():
    raw = _raw_entry_with_placeholder()
    raw["model_config_obj"]["reasoning_level"] = True  # legacy bare `on`
    resolved = _resolved_entry_for(raw, api_key_plain="sk-real-secret", api_base="https://api.example.com")

    parsed = [{
        "model_name": "gpt-4o",
        "api_base": "https://api.example.com",
        "api_key": "sk-real-secret",
        "model_provider": "OpenAI",
        "temperature": 0.95,
        "timeout": 1800,
        "verify_ssl": False,
        "is_default": True,
        "alias": "gpt",
        "origin_index": 0,
        "reasoning_level": "off",
    }]

    out = _merge_models_for_replace_all(parsed, [raw], [resolved], crypto=_StubCrypto())

    level = out[0]["model_config_obj"]["reasoning_level"]
    assert level == "off"
    assert isinstance(level, str)


def test_unchanged_reasoning_level_string_keeps_raw_value():
    raw = _raw_entry_with_placeholder()
    raw["model_config_obj"]["reasoning_level"] = "high"
    resolved = _resolved_entry_for(raw, api_key_plain="sk-real-secret", api_base="https://api.example.com")

    parsed = [{
        "model_name": "gpt-4o",
        "api_base": "https://api.example.com",
        "api_key": "sk-real-secret",
        "model_provider": "OpenAI",
        "temperature": 0.95,
        "timeout": 1800,
        "verify_ssl": False,
        "is_default": True,
        "alias": "gpt",
        "origin_index": 0,
        "reasoning_level": "high",
    }]

    out = _merge_models_for_replace_all(parsed, [raw], [resolved], crypto=_StubCrypto())

    assert out[0]["model_config_obj"]["reasoning_level"] == "high"


def test_is_env_var_placeholder():
    assert _is_env_var_placeholder("${MODEL_PROVIDER}")
    assert _is_env_var_placeholder("${API_BASE:-https://api.example.com}")
    assert not _is_env_var_placeholder("OpenAI")
    assert not _is_env_var_placeholder("")


def test_first_time_setup_materializes_client_provider_placeholder():
    """Persist client_provider literally when YAML still uses ${MODEL_PROVIDER}.

    Mirrors the shipped template: model_name is a bare ${MODEL_NAME} (no inline
    default), so on a first-time setup with MODEL_NAME unset it resolves to "".
    """
    raw = {
        "model_client_config": {
            "api_base": "${API_BASE}",
            "api_key": "${API_KEY}",
            "model_name": "${MODEL_NAME}",
            "client_provider": "${MODEL_PROVIDER}",
            "timeout": 1800,
            "verify_ssl": False,
        },
        "model_config_obj": {"temperature": 0.95},
        "is_default": True,
    }
    resolved = {
        "model_client_config": {
            "api_base": "https://api.example.com",
            "api_key": "sk-template",
            "model_name": "",
            "client_provider": "OpenAI",
            "timeout": 1800,
            "verify_ssl": False,
        },
        "model_config_obj": {"temperature": 0.95},
        "is_default": True,
    }
    parsed = [{
        "model_name": "gpt-4o",
        "api_base": "https://api.openai.com/v1",
        "api_key": "sk-user-secret",
        "model_provider": "OpenAI",
        "temperature": 0.95,
        "timeout": 1800,
        "verify_ssl": False,
        "is_default": True,
        "alias": "",
        "origin_index": 0,
    }]

    out = _merge_models_for_replace_all(parsed, [raw], [resolved], crypto=_StubCrypto())

    mcc = out[0]["model_client_config"]
    assert mcc["client_provider"] == "OpenAI"
    assert mcc["api_key"] == "enc:sk-user-secret"
    assert mcc["api_base"] == "https://api.openai.com/v1"
    assert mcc["model_name"] == "gpt-4o"


def test_unchanged_entry_keeps_raw_placeholders_and_custom_headers():
    raw = _raw_entry_with_placeholder()
    resolved = _resolved_entry_for(raw, api_key_plain="sk-real-secret", api_base="https://api.example.com")

    parsed = [{
        # Frontend echoes back exactly what list returned.
        "model_name": "gpt-4o",
        "api_base": "https://api.example.com",
        "api_key": "sk-real-secret",
        "model_provider": "OpenAI",
        "temperature": 0.95,
        "timeout": 1800,
        "verify_ssl": False,
        "is_default": True,
        "alias": "gpt",
        "origin_index": 0,
    }]

    out = _merge_models_for_replace_all(parsed, [raw], [resolved], crypto=_StubCrypto())

    assert len(out) == 1
    mcc = out[0]["model_client_config"]
    # Placeholders preserved verbatim.
    assert mcc["api_base"] == "${API_BASE:-https://api.example.com}"
    assert mcc["api_key"] == "${API_KEY}"
    # custom_headers (not exposed on the frontend) survives the round-trip.
    assert mcc["custom_headers"] == {"X-Trace-Id": "abc"}
    assert out[0]["alias"] == "gpt"


def test_editing_alias_only_keeps_other_placeholders_intact():
    raw = _raw_entry_with_placeholder()
    resolved = _resolved_entry_for(raw, api_key_plain="sk-real-secret", api_base="https://api.example.com")

    parsed = [{
        "model_name": "gpt-4o",
        "api_base": "https://api.example.com",
        "api_key": "sk-real-secret",
        "model_provider": "OpenAI",
        "temperature": 0.95,
        "timeout": 1800,
        "verify_ssl": False,
        "is_default": True,
        "alias": "openai-flagship",  # ← only this field changed
        "origin_index": 0,
    }]

    out = _merge_models_for_replace_all(parsed, [raw], [resolved], crypto=_StubCrypto())

    mcc = out[0]["model_client_config"]
    assert mcc["api_base"] == "${API_BASE:-https://api.example.com}"
    assert mcc["api_key"] == "${API_KEY}"
    assert out[0]["alias"] == "openai-flagship"


def test_vendor_key_round_trips_without_disturbing_placeholders():
    raw = _raw_entry_with_placeholder()
    resolved = _resolved_entry_for(raw, api_key_plain="sk-real-secret", api_base="https://api.example.com")
    parsed = [{
        "model_name": "openai/gpt-5.6-terra",
        "api_base": "https://openrouter.ai/api/v1",
        "api_key": "sk-real-secret",
        "model_provider": "OpenAI",
        "temperature": 0.95,
        "timeout": 1800,
        "verify_ssl": False,
        "is_default": True,
        "alias": "router",
        "origin_index": 0,
        "vendor_key": "openrouter",
    }]

    out = _merge_models_for_replace_all(parsed, [raw], [resolved], crypto=_StubCrypto())

    assert out[0]["model_client_config"]["vendor_key"] == "openrouter"
    assert out[0]["model_client_config"]["api_key"] == "${API_KEY}"


def test_new_vendor_model_persists_vendor_key():
    parsed = [{
        "model_name": "openai/gpt-5.6-terra",
        "api_base": "https://openrouter.ai/api/v1",
        "api_key": "sk-new",
        "model_provider": "OpenAI",
        "temperature": 0.95,
        "timeout": 1800,
        "verify_ssl": False,
        "is_default": True,
        "alias": "router",
        "origin_index": None,
        "vendor_key": "openrouter",
    }]

    out = _merge_models_for_replace_all(parsed, [], [], crypto=_StubCrypto())

    assert out[0]["model_client_config"]["vendor_key"] == "openrouter"


def test_changing_api_key_encrypts_new_value_and_drops_placeholder():
    raw = _raw_entry_with_placeholder()
    resolved = _resolved_entry_for(raw, api_key_plain="sk-real-secret", api_base="https://api.example.com")

    parsed = [{
        "model_name": "gpt-4o",
        "api_base": "https://api.example.com",
        "api_key": "sk-rotated",  # ← user typed a new key
        "model_provider": "OpenAI",
        "temperature": 0.95,
        "timeout": 1800,
        "verify_ssl": False,
        "is_default": True,
        "alias": "gpt",
        "origin_index": 0,
    }]

    out = _merge_models_for_replace_all(parsed, [raw], [resolved], crypto=_StubCrypto())

    mcc = out[0]["model_client_config"]
    assert mcc["api_key"] == "enc:sk-rotated"
    # api_base still untouched.
    assert mcc["api_base"] == "${API_BASE:-https://api.example.com}"


def test_new_entry_without_origin_index_uses_payload_verbatim():
    parsed = [{
        "model_name": "claude-opus-4-7",
        "api_base": "https://api.anthropic.com",
        "api_key": "sk-new",
        "model_provider": "Anthropic",
        "temperature": 0.5,
        "timeout": 600,
        "verify_ssl": True,
        "is_default": True,
        "alias": "claude",
        "origin_index": None,
        "vendor_key": "anthropic",
        "plan": "custom_api",
    }]

    out = _merge_models_for_replace_all(parsed, [], [], crypto=_StubCrypto())

    mcc = out[0]["model_client_config"]
    assert mcc["model_name"] == "claude-opus-4-7"
    assert mcc["api_key"] == "enc:sk-new"
    assert mcc["verify_ssl"] is True
    assert mcc["vendor_key"] == "anthropic"
    assert mcc["plan"] == "custom_api"
    assert out[0]["alias"] == "claude"


def test_existing_entry_updates_and_clears_exact_vendor_identity():
    raw = _raw_entry_with_placeholder()
    raw["model_client_config"].update({"vendor_key": "alibaba", "plan": "token_plan"})
    resolved = _resolved_entry_for(raw, api_key_plain="sk-real-secret", api_base="https://api.example.com")
    parsed = [{
        "model_name": "gpt-4o",
        "api_base": "https://api.example.com",
        "api_key": "sk-real-secret",
        "model_provider": "OpenAI",
        "temperature": 0.95,
        "timeout": 1800,
        "verify_ssl": False,
        "is_default": True,
        "alias": "gpt",
        "origin_index": 0,
        "vendor_key": "alibaba",
        "plan": "coding_plan",
    }]

    updated = _merge_models_for_replace_all(parsed, [raw], [resolved], crypto=_StubCrypto())
    assert updated[0]["model_client_config"]["vendor_key"] == "alibaba"
    assert updated[0]["model_client_config"]["plan"] == "coding_plan"

    cleared = _merge_models_for_replace_all(
        [{**parsed[0], "vendor_key": None, "plan": None}],
        [raw],
        [resolved],
        crypto=_StubCrypto(),
    )
    assert "vendor_key" not in cleared[0]["model_client_config"]
    assert "plan" not in cleared[0]["model_client_config"]


def test_origin_index_out_of_range_falls_back_to_new_entry():
    raw = _raw_entry_with_placeholder()
    resolved = _resolved_entry_for(raw, api_key_plain="sk-real-secret", api_base="https://api.example.com")

    parsed = [{
        "model_name": "stale",
        "api_base": "https://api.example.com",
        "api_key": "sk-fresh",
        "model_provider": "OpenAI",
        "temperature": 0.95,
        "timeout": 1800,
        "verify_ssl": False,
        "is_default": True,
        "alias": "",
        "origin_index": 99,  # raw_defaults only has index 0
    }]

    out = _merge_models_for_replace_all(parsed, [raw], [resolved], crypto=_StubCrypto())

    # Treated as a brand-new entry: api_key encrypted from plaintext, no placeholders.
    assert out[0]["model_client_config"]["api_key"] == "enc:sk-fresh"
    assert out[0]["model_client_config"]["api_base"] == "https://api.example.com"


def test_reordering_two_entries_keeps_each_entries_placeholders():
    """User drags entry B above A; no field values change. Both placeholders survive."""
    raw_a = {
        "model_client_config": {
            "api_base": "${BASE_A}",
            "api_key": "${KEY_A}",
            "model_name": "model-a",
            "client_provider": "OpenAI",
            "timeout": 1800,
            "verify_ssl": False,
        },
        "model_config_obj": {"temperature": 0.7},
        "is_default": True,
        "alias": "a",
    }
    raw_b = {
        "model_client_config": {
            "api_base": "${BASE_B}",
            "api_key": "${KEY_B}",
            "model_name": "model-b",
            "client_provider": "OpenAI",
            "timeout": 1800,
            "verify_ssl": False,
        },
        "model_config_obj": {"temperature": 0.7},
        "is_default": True,
        "alias": "b",
    }
    resolved_a = _resolved_entry_for(raw_a, api_key_plain="sk-a", api_base="https://api-a")
    resolved_b = _resolved_entry_for(raw_b, api_key_plain="sk-b", api_base="https://api-b")

    parsed = [
        {  # was index 1 originally
            "model_name": "model-b",
            "api_base": "https://api-b",
            "api_key": "sk-b",
            "model_provider": "OpenAI",
            "temperature": 0.7,
            "timeout": 1800,
            "verify_ssl": False,
            "is_default": True,
            "alias": "b",
            "origin_index": 1,
        },
        {  # was index 0 originally
            "model_name": "model-a",
            "api_base": "https://api-a",
            "api_key": "sk-a",
            "model_provider": "OpenAI",
            "temperature": 0.7,
            "timeout": 1800,
            "verify_ssl": False,
            "is_default": False,
            "alias": "a",
            "origin_index": 0,
        },
    ]

    out = _merge_models_for_replace_all(parsed, [raw_a, raw_b], [resolved_a, resolved_b], crypto=_StubCrypto())

    assert out[0]["model_client_config"]["api_key"] == "${KEY_B}"
    assert out[0]["model_client_config"]["api_base"] == "${BASE_B}"
    assert out[1]["model_client_config"]["api_key"] == "${KEY_A}"
    assert out[1]["model_client_config"]["api_base"] == "${BASE_A}"
    assert out[1]["is_default"] is False
