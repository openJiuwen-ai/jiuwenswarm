# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for opencode_zen default free-model fallback helpers."""

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