# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for opencode_zen default free-model fallback helpers."""

import asyncio
from unittest.mock import Mock

import pytest

from jiuwenswarm.server.runtime import opencode_zen
from jiuwenswarm.server.runtime.opencode_zen import get_zen_default_free_model_entry


def _entry(model_name: str) -> dict:
    return {"model_client_config": {"model_name": model_name}}


def test_zen_default_prefers_deepseek_v4_flash(monkeypatch):
    monkeypatch.setattr(
        opencode_zen,
        "get_zen_free_model_entries",
        lambda: [_entry("laguna-s-2.1-free"), _entry(opencode_zen.DEFAULT_FREE_MODEL_ID)],
    )
    entry = get_zen_default_free_model_entry()
    assert entry["model_client_config"]["model_name"] == opencode_zen.DEFAULT_FREE_MODEL_ID


def test_zen_default_falls_back_to_first_entry(monkeypatch):
    monkeypatch.setattr(
        opencode_zen,
        "get_zen_free_model_entries",
        lambda: [_entry("big-pickle"), _entry("grok-code")],
    )
    entry = get_zen_default_free_model_entry()
    assert entry["model_client_config"]["model_name"] == "big-pickle"


def test_zen_default_none_when_empty_cache(monkeypatch):
    monkeypatch.setattr(opencode_zen, "get_zen_free_model_entries", lambda: [])
    assert get_zen_default_free_model_entry() is None


def test_match_zen_free_model_name_accepts_id_and_alias(monkeypatch):
    monkeypatch.setattr(
        opencode_zen,
        "get_zen_free_model_entries",
        lambda: [{
            "model_client_config": {"model_name": "deepseek-v4-flash-free"},
            "alias": "DeepSeek V4 Flash",
        }],
    )
    assert opencode_zen.match_zen_free_model_name("deepseek-v4-flash-free") == (
        "deepseek-v4-flash-free"
    )
    assert opencode_zen.match_zen_free_model_name("DeepSeek V4 Flash") == (
        "deepseek-v4-flash-free"
    )
    assert opencode_zen.match_zen_free_model_name("missing") is None


def test_append_zen_free_models_adds_selectable_entry(monkeypatch):
    monkeypatch.setattr(
        opencode_zen,
        "get_zen_free_model_entries",
        lambda: [{
            "model_client_config": {
                "model_name": "deepseek-v4-flash-free",
                "api_base": "https://opencode.ai/zen/v1",
                "api_key": "public",
                "client_provider": "OpenAI",
            },
            "model_config_obj": {"temperature": 0.95},
            "alias": "DeepSeek V4 Flash",
            "context_window_tokens": 200000,
            "is_free": True,
        }],
    )
    models: list[dict] = []
    opencode_zen.append_zen_free_models(models)
    assert models == [{
        "model_name": "deepseek-v4-flash-free",
        "api_base": "https://opencode.ai/zen/v1",
        "api_key": "public",
        "model_provider": "OpenAI",
        "temperature": 0.95,
        "reasoning_level": "",
        "is_default": None,
        "is_agentos": False,
        "is_free": True,
        "alias": "DeepSeek V4 Flash",
        "context_window_tokens": 200000,
    }]


@pytest.mark.asyncio
@pytest.mark.parametrize("report_status", [False, True])
async def test_disabled_zen_warm_preserves_default_and_skips_fetch_and_retry(monkeypatch, report_status):
    fetch = Mock(side_effect=AssertionError("disabled capability must not fetch"))
    retry = Mock()
    monkeypatch.setattr(opencode_zen, "_zen_free_models_enabled", lambda: False)
    monkeypatch.setattr(opencode_zen, "_populate_zen_free_entries", fetch)
    monkeypatch.setattr(opencode_zen, "_start_background_retry", retry)
    result = await opencode_zen.warm_zen_free_models(reason="unit-disabled", report_status=report_status)
    assert result is (True if report_status else None)
    fetch.assert_not_called()
    retry.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("report_status", [False, True])
@pytest.mark.parametrize("outcome", ["cached", "empty", "timeout", "error"])
async def test_zen_warm_reports_actual_cache_availability_and_preserves_retry(monkeypatch, report_status, outcome):
    entries = [_entry("available-free-model")] if outcome == "cached" else []
    error = {"timeout": asyncio.TimeoutError("provider timed out"),
             "error": RuntimeError("provider unavailable")}.get(outcome)
    fetch = Mock(side_effect=error, return_value=len(entries))
    retry = Mock()
    monkeypatch.setattr(opencode_zen, "_zen_free_models_enabled", lambda: True)
    monkeypatch.setattr(opencode_zen, "_populate_zen_free_entries", fetch)
    monkeypatch.setattr(opencode_zen, "get_zen_free_model_entries", lambda: entries)
    monkeypatch.setattr(opencode_zen, "_start_background_retry", retry)
    result = await opencode_zen.warm_zen_free_models(reason=f"unit-{outcome}", report_status=report_status)
    assert result is (bool(entries) if report_status else None)
    fetch.assert_called_once_with()
    if entries:
        retry.assert_not_called()
    else:
        retry.assert_called_once_with()
