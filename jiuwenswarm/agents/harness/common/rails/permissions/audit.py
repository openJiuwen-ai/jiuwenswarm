# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Observe-only audit logging for auto permissions."""

from __future__ import annotations

import json
import logging
from typing import Any

from jiuwenswarm.agents.harness.common.rails.permissions.tool_decision_facts import (
    ToolDecisionFacts,
)
from jiuwenswarm.agents.harness.common.rails.permissions.persistent_audit import (
    build_sanitized_audit_fields,
)

logger = logging.getLogger(__name__)


def emit_permission_audit(
    facts: ToolDecisionFacts,
    *,
    decision: str,
    reason: str,
    degraded: bool,
    grant_id: str = "",
    grant_reason: str = "",
    extra: dict[str, object] | None = None,
    persistent_writer: Any | None = None,
) -> Any | None:
    """Emit an auto-permission audit event without raw tool arguments."""
    try:
        event = build_sanitized_audit_fields(
            facts,
            decision=decision,
            reason=reason,
            degraded=degraded,
            grant_id=grant_id,
            grant_reason=grant_reason,
            extra=extra,
        )
        logger.info(
            "permission_audit %s",
            json.dumps(event, sort_keys=True, separators=(",", ":")),
        )
    except Exception:
        # Logging is observe-only. Sanitization, serialization, and handlers are
        # isolated from the permission result and from the persistent sink.
        pass
    if persistent_writer is None:
        return None
    try:
        return persistent_writer.write(
            facts,
            decision=decision,
            reason=reason,
            degraded=degraded,
            grant_id=grant_id,
            grant_reason=grant_reason,
            extra=extra,
        )
    except Exception:
        # Third-party audit writers share the same observe-only boundary.
        return None
