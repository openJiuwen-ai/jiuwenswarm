"""Bounded structured events for tuning; never log skill bodies or model options."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
from uuid import uuid4


def configuration_metadata(settings):
    return {
        "config_id": hashlib.sha256(settings.identity().encode()).hexdigest()[:16],
        "scheme": "bm25-main-llm-v6", "flow": "two_round", "candidate_k": settings.candidate_k,
        "k1": settings.k1, "b": settings.b, "name_weight": settings.name_weight,
    }


def selection_trace(query, ctx, agent, settings):
    session = getattr(ctx, "session", None)
    get_session_id = getattr(session, "get_session_id", None)
    result = {
        "selection_id": uuid4().hex,
        "session_id": get_session_id() if callable(get_session_id) else None,
        "agent_id": getattr(agent, "_runtime_id", None),
        "query_sha256": hashlib.sha256(query.encode()).hexdigest(),
        "query_chars": len(query),
        **configuration_metadata(settings),
    }
    # Operator opt-in: normal diagnostics can be joined to the request log by
    # session/time without copying the user's potentially sensitive text again.
    if os.environ.get("FLASH_SKILL_LOG_QUERY", "").lower() in {"1", "true", "yes"}:
        result["query_preview"] = query[:200]
        result["query_truncated"] = len(query) > 200
    return result


def emit(logger: logging.Logger, event: str, trace: dict, **fields):
    def numeric_precision(value):
        # Stable, readable metrics, without long binary-float decimal tails.
        # Only the log representation changes; decisions use original precision.
        if isinstance(value, float):
            return float(f"{value:.8g}") if math.isfinite(value) else None
        if isinstance(value, dict):
            return {key: numeric_precision(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [numeric_precision(item) for item in value]
        return value

    logger.info(
        "[SkillSelection] diagnostic=%s",
        json.dumps(
            numeric_precision({"event": event, **trace, **fields}),
            ensure_ascii=False,
            allow_nan=False,
            default=str,
        ),
    )
