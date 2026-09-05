# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tiny LLM scheduler for uncertain MACRO gate decisions."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from jiuwenswarm.agents.harness.macro_routing.schemas import (
    MacroRoutingDecision,
    normalize_macro_mode,
)

logger = logging.getLogger(__name__)


def _extract_json(text: str) -> dict[str, Any]:
    if not text:
        return {}
    text = text.strip()
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        pass
    match = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if match:
        try:
            parsed = json.loads(match.group(1).strip())
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            pass
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            parsed = json.loads(text[start:end + 1])
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            pass
    return {}


def _build_prompt(query: str, gate: MacroRoutingDecision) -> str:
    return f"""You are a lightweight execution-mode scheduler.
Pick ONE top-level mode for this user task.

Modes:
- agent: Agent Mode — single-agent tasks (Q&A, coding, design, execution)
- team: Cluster Mode — multi-role or multi-area work needing a team

Return ONLY JSON:
{{"mode":"agent|team","confidence":0.0,"rationale":"one sentence"}}

Gate suggestion (may be uncertain):
mode={gate.mode}, confidence={gate.confidence:.2f}, rationale={gate.rationale}

User task:
{query}
"""


def _pick_system_model_entry(*, model_name: str = "") -> dict[str, Any]:
    """Use the same chat model catalog as the rest of the system (models.defaults)."""
    from jiuwenswarm.common.config import get_default_models

    entries = [e for e in get_default_models() if isinstance(e, dict)]
    want = str(model_name or "").strip()
    if want:
        for entry in entries:
            client = entry.get("model_client_config")
            if isinstance(client, dict) and str(client.get("model_name") or "").strip() == want:
                return entry
            if str(entry.get("alias") or "").strip() == want:
                return entry
    for entry in entries:
        if entry.get("is_default"):
            return entry
    return entries[0] if entries else {}


async def route_with_llm_scheduler(
    query: str,
    *,
    gate: MacroRoutingDecision,
    model_name: str = "",
) -> MacroRoutingDecision:
    """Ask the configured system chat model for MACRO mode when the gate is uncertain."""
    from openjiuwen.core.foundation.llm import Model, ModelClientConfig, ModelRequestConfig

    try:
        entry = _pick_system_model_entry(model_name=model_name)
        client_cfg = entry.get("model_client_config") if isinstance(entry, dict) else {}
        if not isinstance(client_cfg, dict):
            client_cfg = {}
        model_cfg_obj = entry.get("model_config_obj") if isinstance(entry, dict) else {}
        if not isinstance(model_cfg_obj, dict):
            model_cfg_obj = {}

        provider = str(client_cfg.get("client_provider") or "").strip()
        api_key = str(client_cfg.get("api_key") or "").strip()
        api_base = str(client_cfg.get("api_base") or "").strip()
        resolved_model = str(
            model_name or client_cfg.get("model_name") or ""
        ).strip()
        if not provider or not resolved_model:
            raise RuntimeError(
                "No valid system chat model configured for MACRO scheduler "
                f"(provider={provider!r}, model={resolved_model!r})"
            )

        mcc_fields = {k: v for k, v in client_cfg.items() if k != "model_name"}
        mcc_fields.setdefault("api_key", api_key)
        mcc_fields.setdefault("api_base", api_base)
        mcc_fields["client_provider"] = provider

        # openjiuwen Model.invoke requires a non-None model_config (reads top_p etc.).
        request_cfg = ModelRequestConfig(
            model_name=resolved_model,
            temperature=0.0,
            top_p=float(model_cfg_obj.get("top_p", 1.0) or 1.0),
            max_tokens=256,
        )
        model = Model(
            model_client_config=ModelClientConfig(**mcc_fields),
            model_config=request_cfg,
        )
        response = await model.invoke(
            messages=[{"role": "user", "content": _build_prompt(query, gate)}],
            temperature=0.0,
            max_tokens=256,
            model=resolved_model,
        )
        content = response.content
        if isinstance(content, list):
            parts: list[str] = []
            for block in content:
                if isinstance(block, dict) and "text" in block:
                    parts.append(str(block["text"]))
                elif isinstance(block, str):
                    parts.append(block)
            content = "\n".join(parts)
        payload = _extract_json(str(content or ""))
        mode = normalize_macro_mode(payload.get("mode"), default=gate.mode)
        try:
            confidence = float(payload.get("confidence", 0.7))
        except (TypeError, ValueError):
            confidence = 0.7
        confidence = max(0.0, min(1.0, confidence))
        rationale = str(payload.get("rationale") or "LLM MACRO scheduler decision.").strip()
        features = dict(gate.features)
        features["scheduler_model"] = resolved_model
        features["scheduler_provider"] = provider
        # openjiuwen AssistantMessage exposes usage_metadata (not .usage).
        usage_obj = getattr(response, "usage_metadata", None) or getattr(response, "usage", None)
        if usage_obj is not None:
            def _usage_get(obj: Any, *names: str) -> int | None:
                for name in names:
                    if isinstance(obj, dict) and name in obj:
                        try:
                            return int(obj[name] or 0)
                        except (TypeError, ValueError):
                            return None
                    if hasattr(obj, name):
                        try:
                            return int(getattr(obj, name) or 0)
                        except (TypeError, ValueError):
                            return None
                return None

            in_tok = _usage_get(usage_obj, "input_tokens", "prompt_tokens")
            out_tok = _usage_get(usage_obj, "output_tokens", "completion_tokens")
            tot_tok = _usage_get(usage_obj, "total_tokens")
            if tot_tok is None and in_tok is not None and out_tok is not None:
                tot_tok = in_tok + out_tok
            features["scheduler_tokens"] = {
                "input_tokens": in_tok,
                "output_tokens": out_tok,
                "total_tokens": tot_tok,
            }
        logger.info(
            "[MacroRouter] LLM scheduler used system model=%s provider=%s -> %s conf=%.2f",
            resolved_model,
            provider,
            mode,
            confidence,
        )
        return MacroRoutingDecision(
            mode=mode,
            confidence=confidence,
            rationale=rationale,
            source="llm",
            features=features,
            gate_confident=True,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[MacroRouter] LLM scheduler failed: %s", exc)
        # Prefer Agent Mode on scheduler failure unless the gate was already confident.
        fallback_mode = gate.mode if gate.gate_confident else "agent"
        return MacroRoutingDecision(
            mode=fallback_mode,
            confidence=max(0.5, float(gate.confidence)),
            rationale=(
                f"LLM scheduler failed; using "
                f"{'gate choice' if gate.gate_confident else 'Agent Mode'} ({fallback_mode})."
            ),
            source="fallback",
            features=dict(gate.features),
            gate_confident=True,
        )


__all__ = ["route_with_llm_scheduler"]
