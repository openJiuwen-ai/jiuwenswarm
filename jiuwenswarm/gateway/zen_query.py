# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Ask AgentServer for Opencode Zen rows. Gateway does not import that cache."""

from __future__ import annotations

import logging
from typing import Any

from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.gateway.routing.e2a_proxy import fetch_agent_unary

logger = logging.getLogger(__name__)


async def fetch_zen_free_model_rows(agent_client: Any) -> list[dict[str, Any]]:
    """Return public Zen rows from AgentServer, or ``[]`` when it is unreachable."""
    ok, payload = await fetch_agent_unary(
        agent_client=agent_client,
        req_method=ReqMethod.MODELS_ZEN_ENTRIES,
        params={},
        session_id=None,
        user_id=None,
        channel_id="web",
        label="models.zen_entries",
    )
    if not ok:
        return []
    models = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(models, list):
        return []
    return [row for row in models if isinstance(row, dict)]


async def request_zen_warmup(agent_client: Any) -> None:
    """Ask AgentServer to refresh its Zen cache after a free-model toggle."""
    ok, payload = await fetch_agent_unary(
        agent_client=agent_client,
        req_method=ReqMethod.MODELS_ZEN_WARM,
        params={},
        session_id=None,
        user_id=None,
        channel_id="web",
        label="models.zen_warm",
    )
    if not ok:
        logger.warning(
            "[zen] AgentServer warmup failed: %s",
            payload.get("error") if isinstance(payload, dict) else payload,
        )
