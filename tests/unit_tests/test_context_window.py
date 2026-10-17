# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from jiuwenswarm.common.context_window import (
    DEFAULT_CONTEXT_WINDOW_TOKENS,
    MAX_CONTEXT_WINDOW_TOKENS,
    parse_positive_int,
    resolve_context_window_tokens,
)


def test_model_name_does_not_trigger_provider_metadata_lookup():
    """Every unconfigured model uses the same local default."""
    assert DEFAULT_CONTEXT_WINDOW_TOKENS == 262144
    assert resolve_context_window_tokens("DeepSeek-V4-Flash-0731") == 262144
    assert resolve_context_window_tokens("GLM5.3") == 262144
    assert resolve_context_window_tokens("qwen3.8-max-2026-06-08") == 262144


def test_context_window_parser_accepts_case_insensitive_units_and_plain_counts():
    assert parse_positive_int("256k") == 262144
    assert parse_positive_int("256 K") == 262144
    assert parse_positive_int("1m") == 1048576
    assert parse_positive_int("1,310,720") == 1310720
    assert parse_positive_int("1310K") == 1310 * 1024
    assert parse_positive_int("256 tokens") == 256


def test_context_window_parser_rejects_invalid_or_unsafe_values():
    for value in (
        "",
        "0",
        "-1",
        "1.5",
        "1e6",
        "abc",
        True,
        MAX_CONTEXT_WINDOW_TOKENS + 1,
    ):
        assert parse_positive_int(value) is None


def test_explicit_global_window_has_highest_priority():
    assert (
        resolve_context_window_tokens(
            "some-model",
            context_engine_config={"context_window_tokens": 1048576},
            model_config_obj={"context_window": 262144},
            model_context_window_override=262144,
        )
        == 1048576
    )


def test_explicit_selected_model_window_beats_manual_mapping():
    assert (
        resolve_context_window_tokens(
            "some-model",
            context_engine_config={
                "model_context_window_tokens": {"some-model": 1048576},
            },
            model_config_obj={"context_window": 262144},
        )
        == 262144
    )


def test_manual_mapping_is_preserved_for_legacy_explicit_configuration():
    assert (
        resolve_context_window_tokens(
            "some-model",
            context_engine_config={
                "model_context_window_tokens": {"some-model": 1048576},
            },
        )
        == 1048576
    )


def test_runtime_override_is_used_when_model_config_is_not_available():
    assert (
        resolve_context_window_tokens(
            "some-model",
            model_context_window_override=1048576,
        )
        == 1048576
    )


def test_invalid_values_fall_back_to_fixed_default():
    assert (
        resolve_context_window_tokens(
            "some-model",
            context_engine_config={
                "context_window_tokens": "not-a-number",
                "model_context_window_tokens": {"some-model": 0},
            },
            model_config_obj={"context_window": None},
        )
        == DEFAULT_CONTEXT_WINDOW_TOKENS
    )
