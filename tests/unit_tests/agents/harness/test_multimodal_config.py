from __future__ import annotations

import pytest

from jiuwenswarm.agents.harness.common.tools.multimodal_config import (
    complete_multimodal_model_configured,
    multimodal_model_enabled,
)


def _vision_config(*, provider: str = "OpenAI") -> dict:
    return {
        "models": {
            "vision": {
                "model_client_config": {
                    "api_base": "https://vision.example/v1",
                    "api_key": "secret",
                    "model_name": "vision-model",
                    "client_provider": provider,
                }
            }
        }
    }


def test_complete_multimodal_config_requires_all_four_fields() -> None:
    assert complete_multimodal_model_configured(_vision_config(), "vision") is True
    assert (
        complete_multimodal_model_configured(_vision_config(provider=""), "vision")
        is False
    )


def test_multimodal_enabled_prefers_explicit_switch_and_preserves_legacy_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _vision_config()
    monkeypatch.delenv("VISION_ENABLED", raising=False)
    assert multimodal_model_enabled(config, "vision") is True

    monkeypatch.setenv("VISION_ENABLED", "false")
    assert multimodal_model_enabled(config, "vision") is False

    monkeypatch.setenv("VISION_ENABLED", "")
    assert multimodal_model_enabled(config, "vision") is False

    monkeypatch.setenv("VISION_ENABLED", "true")
    assert multimodal_model_enabled(config, "vision") is True
