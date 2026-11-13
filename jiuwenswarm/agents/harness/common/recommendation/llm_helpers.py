# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Small LLM response helpers shared by recommendation background jobs."""

from __future__ import annotations

import re
from typing import Any


def extract_output_text(invoke_result: Any) -> str:
    """Extract the model's text output from a DeepAgent.invoke result."""

    if invoke_result is None:
        return ""
    if isinstance(invoke_result, str):
        return invoke_result
    if isinstance(invoke_result, dict):
        out = invoke_result.get("output")
        if isinstance(out, str):
            return out
        if isinstance(out, list):
            return "\n".join(s for s in out if isinstance(s, str))
        return ""
    return ""


def extract_json_from_response(text: str) -> str:
    fence_match = re.search(r"```(?:json)?\s*\n(.*?)\n```", text, re.DOTALL)
    if fence_match:
        return fence_match.group(1).strip()
    brace_match = re.search(r"\{.*\}", text, re.DOTALL)
    if brace_match:
        return brace_match.group(0).strip()
    return ""


def get_recommendation_model(temperature: float = 0.0) -> Any:
    """Build the default chat model for recommendation background DeepAgents."""

    try:
        from jiuwenswarm.common.config import get_default_models
        from openjiuwen.core.foundation.llm import (
            Model,
            ModelClientConfig,
            ModelRequestConfig,
        )
    except ImportError:
        return None
    entries = get_default_models()
    if not entries:
        return None
    entry = entries[0]
    mcc = entry.get("model_client_config", {})
    model_name = mcc.get("model_name", "")
    mcc_fields = {k: v for k, v in mcc.items() if k != "model_name"}
    try:
        return Model(
            model_client_config=ModelClientConfig(**mcc_fields),
            model_config=ModelRequestConfig(
                model=model_name,
                temperature=temperature,
                extra_body={
                    "thinking": {"type": "disabled"},
                    "chat_template_kwargs": {"enable_thinking": False},
                },
            ),
        )
    except Exception:
        return None
