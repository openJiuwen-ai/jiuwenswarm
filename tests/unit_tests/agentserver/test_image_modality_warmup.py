# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Image-modality probe cache warm-up.

The cache is what keeps agents (and every sub-agent, which always starts on
auto) from firing their own probe request at start-up, so the warm-up must
cover each configured model exactly once and must never propagate a failure to
the caller.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

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
