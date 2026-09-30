# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Image-modality probe cache warm-up.

The cache is what keeps agents (and every sub-agent, which always starts on
auto) from firing their own probe request at start-up, so the warm-up must
cover each configured model exactly once and must never propagate a failure to
the caller.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Iterator
from unittest.mock import patch

import pytest

from jiuwenswarm.common.config import resolve_env_vars
from jiuwenswarm.server.runtime import image_modality_warmup
from jiuwenswarm.server.runtime.image_modality_warmup import (
    refresh_image_modality_cache,
    warm_image_modality_cache,
)


def _model_entry(model_name: str, api_base: str = "https://api.example.invalid") -> dict:
    """Build one ``models.defaults`` entry."""
    return {
        "model_client_config": {
            "model_name": model_name,
            "api_base": api_base,
            "api_key": "test-key",
            "client_provider": "OpenAI",
            "verify_ssl": False,
        },
        "model_config_obj": {"temperature": 0.5},
    }


@pytest.mark.asyncio
async def test_warmup_probes_each_configured_model_once():
    config = {"models": {"defaults": [_model_entry("model-a"), _model_entry("model-b")]}}
    probed: list[tuple[str, str]] = []

    async def _fake_probe(model):
        probed.append(image_modality_warmup.probe_cache_key(model))
        return True

    with patch.object(image_modality_warmup, "probe_image_support", _fake_probe):
        await warm_image_modality_cache(config, reason="test")

    assert probed == [
        ("https://api.example.invalid", "model-a"),
        ("https://api.example.invalid", "model-b"),
    ]


@pytest.mark.asyncio
async def test_warmup_dedupes_entries_sharing_a_probe_key():
    """Two entries on the same endpoint+model share one cache key, so probe once."""
    config = {"models": {"defaults": [_model_entry("model-a"), _model_entry("model-a")]}}
    call_count = 0

    async def _fake_probe(model):
        nonlocal call_count
        call_count += 1
        return True

    with patch.object(image_modality_warmup, "probe_image_support", _fake_probe):
        await warm_image_modality_cache(config, reason="test")

    assert call_count == 1


@pytest.mark.asyncio
async def test_warmup_skipped_when_switch_is_explicit():
    """A pinned enable_read_image_multimodal means no agent ever reads a verdict."""
    config = {
        "models": {"defaults": [_model_entry("model-a")]},
        "react": {"enable_read_image_multimodal": False},
    }
    call_count = 0

    async def _fake_probe(model):
        nonlocal call_count
        call_count += 1
        return True

    with patch.object(image_modality_warmup, "probe_image_support", _fake_probe):
        await warm_image_modality_cache(config, reason="test")

    assert call_count == 0


@pytest.mark.asyncio
async def test_warmup_swallows_probe_failures():
    config = {"models": {"defaults": [_model_entry("model-a"), _model_entry("model-b")]}}

    async def _failing_probe(model):
        raise RuntimeError("probe exploded")

    with patch.object(image_modality_warmup, "probe_image_support", _failing_probe):
        await warm_image_modality_cache(config, reason="test")


@pytest.mark.asyncio
async def test_warmup_without_configured_models_is_a_noop():
    call_count = 0

    async def _fake_probe(model):
        nonlocal call_count
        call_count += 1
        return True

    with (
        patch.object(image_modality_warmup, "probe_image_support", _fake_probe),
        patch.object(image_modality_warmup, "get_default_models", lambda config: []),
    ):
        await warm_image_modality_cache({}, reason="test")

    assert call_count == 0


@pytest.mark.asyncio
async def test_refresh_drops_stale_verdicts_before_probing():
    """A model entry may now point at a different backend behind the same key."""
    config = {"models": {"defaults": [_model_entry("model-a")]}}
    call_order: list[str] = []

    def _fake_reset():
        call_order.append("reset")

    async def _fake_probe(model):
        call_order.append("probe")
        return True

    with (
        patch.object(image_modality_warmup, "reset_image_support_cache", _fake_reset),
        patch.object(image_modality_warmup, "probe_image_support", _fake_probe),
    ):
        await refresh_image_modality_cache(config, reason="test")

    assert call_order == ["reset", "probe"]


def _collect_warmup_logs() -> tuple[list[str], object]:
    """Capture what the warm-up logs, without relying on logger propagation."""
    messages: list[str] = []

    class _Sink(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            messages.append(record.getMessage())

    return messages, _Sink()


@contextmanager
def _warmup_log_capture() -> Iterator[list[str]]:
    messages, sink = _collect_warmup_logs()
    logger = image_modality_warmup.logger
    previous_level = logger.level
    logger.addHandler(sink)
    logger.setLevel(logging.INFO)
    try:
        yield messages
    finally:
        logger.setLevel(previous_level)
        logger.removeHandler(sink)


_VENDOR_ROUTING_EXTRA_BODY = {"provider": {"quantizations": ["fp8"]}}


def _templated_entry(api_base_ref: str, model_name_ref: str, model_config_obj: dict) -> dict:
    """One ``models.defaults`` entry still carrying its ``${VAR}`` references."""
    return {
        "model_client_config": {
            "model_name": model_name_ref,
            "api_base": api_base_ref,
            "api_key": "test-key",
            "client_provider": "OpenAI",
            "verify_ssl": False,
        },
        "model_config_obj": dict(model_config_obj),
    }


def _three_entries_colliding_after_substitution(monkeypatch) -> dict:
    """Config whose three entries differ in the file and coincide once resolved.

    The three entries name three different environment variables. All three
    variables resolve to one endpoint and one model name. Only the first entry
    asks for vendor routing options in ``extra_body``.
    """
    for var in ("AGGREGATOR_API_BASE", "DIRECT_API_BASE", "SPARE_API_BASE"):
        monkeypatch.setenv(var, "https://api.example.invalid/v1")
    for var in ("AGGREGATOR_MODEL_NAME", "DIRECT_MODEL_NAME", "SPARE_MODEL_NAME"):
        monkeypatch.setenv(var, "example-org/example-model")

    raw = {
        "models": {
            "defaults": [
                _templated_entry(
                    "${AGGREGATOR_API_BASE}",
                    "${AGGREGATOR_MODEL_NAME}",
                    {"temperature": 0.2, "extra_body": _VENDOR_ROUTING_EXTRA_BODY},
                ),
                _templated_entry(
                    "${DIRECT_API_BASE}",
                    "${DIRECT_MODEL_NAME}",
                    {"temperature": 0.6},
                ),
                _templated_entry(
                    "${SPARE_API_BASE}",
                    "${SPARE_MODEL_NAME}",
                    {"temperature": 1.0},
                ),
            ]
        }
    }
    written = {
        (
            entry["model_client_config"]["api_base"],
            entry["model_client_config"]["model_name"],
        )
        for entry in raw["models"]["defaults"]
    }
    assert len(written) == 3, "the three entries must be distinct before substitution"

    resolved = resolve_env_vars(raw)
    substituted = {
        (
            entry["model_client_config"]["api_base"],
            entry["model_client_config"]["model_name"],
        )
        for entry in resolved["models"]["defaults"]
    }
    assert len(substituted) == 1, "the three entries must coincide after substitution"
    return resolved


def test_collapsed_entries_do_not_inherit_a_siblings_request_config(monkeypatch):
    """A probe target built from several entries sends none of their bodies.

    The first entry used to win outright. Its ``extra_body`` then went to the
    endpoint on behalf of two entries that never asked for it.
    """
    config = _three_entries_colliding_after_substitution(monkeypatch)

    models = image_modality_warmup._build_probe_models(config)

    assert len(models) == 1
    probe_target = models[0]
    assert image_modality_warmup.probe_cache_key(probe_target) == (
        "https://api.example.invalid/v1",
        "example-org/example-model",
    )
    assert getattr(probe_target.model_config, "extra_body", None) is None
    assert getattr(probe_target.model_config, "temperature", None) is None


def test_collapse_is_readable_in_the_log(monkeypatch):
    """The log names every enumerated entry, its key, and what merged."""
    config = _three_entries_colliding_after_substitution(monkeypatch)

    with _warmup_log_capture() as messages:
        image_modality_warmup._build_probe_models(config)

    joined = "\n".join(messages)
    assert "enumerating 3 configured model entry(ies)" in joined
    for index in range(3):
        assert f"models.defaults[{index}]" in joined
    assert "already claimed by models.defaults[0]" in joined
    assert "3 entries resolve to one probe target" in joined
    assert "no model_config_obj" in joined


@pytest.mark.asyncio
async def test_declared_incapable_model_is_never_probed():
    """``supports_vision: false`` spends no probe call and caches the verdict."""
    entry = _model_entry("text-only-model")
    entry["model_client_config"]["supports_vision"] = False
    config = {"models": {"defaults": [entry, _model_entry("model-b")]}}
    probed: list[tuple[str, str]] = []
    cached: list[tuple[tuple[str, str], bool]] = []

    async def _fake_probe(model):
        probed.append(image_modality_warmup.probe_cache_key(model))
        return True

    with (
        patch.object(image_modality_warmup, "probe_image_support", _fake_probe),
        patch.object(
            image_modality_warmup,
            "set_cached_image_support",
            lambda key, supported: cached.append((key, supported)),
        ),
    ):
        await warm_image_modality_cache(config, reason="test")

    assert probed == [("https://api.example.invalid", "model-b")]
    assert cached == [(("https://api.example.invalid", "text-only-model"), False)]


def test_declaration_on_a_later_colliding_entry_still_counts():
    """A declaration must not be lost because another entry claimed the key."""
    undeclared = _model_entry("model-a")
    declared = _model_entry("model-a")
    declared["model_client_config"]["supports_vision"] = False
    config = {"models": {"defaults": [undeclared, declared]}}
    cached: list[tuple[tuple[str, str], bool]] = []

    with patch.object(
        image_modality_warmup,
        "set_cached_image_support",
        lambda key, supported: cached.append((key, supported)),
    ):
        models = image_modality_warmup._build_probe_models(config)

    assert models == []
    assert cached == [(("https://api.example.invalid", "model-a"), False)]


def test_contradictory_declarations_fall_back_to_probing():
    """Two entries on one endpoint+model cannot both be right; probe instead."""
    yes = _model_entry("model-a")
    yes["model_client_config"]["supports_vision"] = True
    no = _model_entry("model-a")
    no["model_client_config"]["supports_vision"] = False
    config = {"models": {"defaults": [yes, no]}}
    cached: list[tuple[tuple[str, str], bool]] = []

    with patch.object(
        image_modality_warmup,
        "set_cached_image_support",
        lambda key, supported: cached.append((key, supported)),
    ):
        models = image_modality_warmup._build_probe_models(config)

    assert cached == []
    assert len(models) == 1
