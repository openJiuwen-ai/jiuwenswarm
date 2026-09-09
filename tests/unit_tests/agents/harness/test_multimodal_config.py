from __future__ import annotations

import os

import pytest

from jiuwenswarm.agents.harness.common.tools.multimodal_config import (
    apply_video_gen_model_config_from_yaml,
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


def test_apply_video_gen_does_not_inherit_image_gen_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Video gen must not copy IMAGE_GEN_* into VIDEO_GEN_* when video key is empty."""
    for key in (
        "VIDEO_GEN_API_KEY",
        "VIDEO_GEN_API_BASE",
        "VIDEO_GEN_MODEL_NAME",
        "VIDEO_GEN_PROVIDER",
        "VIDEO_GEN_ENDPOINT_PROFILE",
        "IMAGE_GEN_API_KEY",
        "IMAGE_GEN_API_BASE",
        "IMAGE_GEN_MODEL_NAME",
        "IMAGE_GEN_PROVIDER",
        "IMAGE_GEN_ENDPOINT_PROFILE",
    ):
        monkeypatch.delenv(key, raising=False)

    monkeypatch.setenv("IMAGE_GEN_API_KEY", "sk-image-only")
    monkeypatch.setenv("IMAGE_GEN_API_BASE", "https://image.example/api/v1")
    monkeypatch.setenv("IMAGE_GEN_MODEL_NAME", "wanx-v1")
    monkeypatch.setenv("IMAGE_GEN_PROVIDER", "DashScope")
    monkeypatch.setenv("IMAGE_GEN_ENDPOINT_PROFILE", "dashscope")

    apply_video_gen_model_config_from_yaml(
        {
            "models": {
                "image_gen": {
                    "model_client_config": {
                        "api_base": "https://image.example/api/v1",
                        "api_key": "sk-image-only",
                        "model_name": "wanx-v1",
                        "client_provider": "DashScope",
                        "endpoint_profile": "dashscope",
                    }
                },
                "video_gen": {
                    "model_client_config": {
                        "api_base": "",
                        "api_key": "",
                        "model_name": "wan2.6-t2v",
                        "client_provider": "DashScope",
                    }
                },
            }
        }
    )

    assert os.getenv("VIDEO_GEN_API_KEY") in (None, "")
    assert os.getenv("VIDEO_GEN_API_BASE") == "https://dashscope.aliyuncs.com/api/v1"
    assert os.getenv("VIDEO_GEN_MODEL_NAME") == "wan2.6-t2v"
    assert os.getenv("IMAGE_GEN_API_KEY") == "sk-image-only"
