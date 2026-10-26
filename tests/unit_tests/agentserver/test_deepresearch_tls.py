# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for pure child-runtime DeepResearch TLS configuration."""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor

import pytest

from jiuwenswarm.agents.harness.common.tools.deepresearch.tls import (
    build_child_tls_config_frame,
    normalize_child_tls_config,
)


def test_normalize_child_tls_config_returns_boolean_values():
    assert normalize_child_tls_config({
        "LLM_SSL_VERIFY": " TrUe ",
        "TOOL_SSL_VERIFY": False,
    }) == {
        "LLM_SSL_VERIFY": True,
        "TOOL_SSL_VERIFY": False,
    }


def test_normalize_child_tls_config_defaults_both_values_to_false():
    assert normalize_child_tls_config({}) == {
        "LLM_SSL_VERIFY": False,
        "TOOL_SSL_VERIFY": False,
    }


@pytest.mark.parametrize("value", ["yes", "0", 0, 1, None, object()])
def test_normalize_child_tls_config_rejects_ambiguous_values(value):
    with pytest.raises(ValueError, match="LLM_SSL_VERIFY"):
        normalize_child_tls_config({"LLM_SSL_VERIFY": value})


def test_normalize_child_tls_config_rejects_unknown_keys():
    with pytest.raises(ValueError, match="^deepresearch_tls_invalid$"):
        normalize_child_tls_config({
            "LLM_SSL_VERIFY": True,
            "LLM_SSL_CERT": "/secret/client.pem",
        })


def test_build_child_tls_config_frame_is_json_serializable():
    frame = build_child_tls_config_frame({
        "LLM_SSL_VERIFY": "false",
        "TOOL_SSL_VERIFY": "TRUE",
    })

    assert frame == {
        "tls": {
            "LLM_SSL_VERIFY": False,
            "TOOL_SSL_VERIFY": True,
        }
    }
    assert json.loads(json.dumps(frame)) == frame


def test_concurrent_child_tls_config_calls_are_pure(monkeypatch):
    monkeypatch.setenv("LLM_SSL_VERIFY", "ambient-llm")
    monkeypatch.setenv("TOOL_SSL_VERIFY", "ambient-tool")
    before = dict(os.environ)

    configs = [
        {
            "LLM_SSL_VERIFY": bool(index % 2),
            "TOOL_SSL_VERIFY": bool((index + 1) % 2),
        }
        for index in range(64)
    ]
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(build_child_tls_config_frame, configs))

    assert len(results) == len(configs)
    assert dict(os.environ) == before


@pytest.mark.parametrize("config", [[], (), ["LLM_SSL_VERIFY"]])
def test_tls_config_rejects_non_mappings_with_fixed_error(config):
    with pytest.raises(ValueError, match="^deepresearch_tls_invalid$"):
        normalize_child_tls_config(config)


@pytest.mark.parametrize(
    "config",
    [
        {1: True},
        {"LLM_SSL_VERIFY": True, 1: False},
        {"SUPER_SECRET_TOKEN_DO_NOT_ECHO": True},
    ],
)
def test_tls_config_rejects_bad_keys_without_echoing_them(config):
    before = dict(config)

    with pytest.raises(ValueError, match="^deepresearch_tls_invalid$") as exc:
        normalize_child_tls_config(config)

    assert "SECRET" not in str(exc.value)
    assert config == before


def test_tls_config_does_not_mutate_input_mapping():
    config = {
        "LLM_SSL_VERIFY": " TRUE ",
        "TOOL_SSL_VERIFY": False,
    }
    before = dict(config)

    normalize_child_tls_config(config)

    assert config == before
