"""Host model and request adapters; Core owns capability assembly and identity."""

from __future__ import annotations
from typing import Any
from jiuwenswarm.runtime.symphony.llm import LLMConfig, create_model_response_observer


def candidate_ids_from_skill_ids(value: Any) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, (list, tuple)):
        return []
    return list(dict.fromkeys(str(item).strip() for item in value if str(item or "").strip()))


def llm_config_signature(config: LLMConfig) -> str:
    return config.identity_digest()


def model_from_config(config: LLMConfig):
    model = config.create_model()
    model.symphony_response_observer = create_model_response_observer(config)
    return model
