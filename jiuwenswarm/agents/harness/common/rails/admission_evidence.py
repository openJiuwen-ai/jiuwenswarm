# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""When-gate evidence trail for SkillEvolutionRail (opt-in).

Native evolution writes on execution signals. ``ExecutionGroundedGate`` already
decides *when* to allow a write (headroom control). This module records that
decision as a reconstructible JSONL row so a third party can audit promotion
attempts without re-running the agent.

Enable by setting ``JIUWEN_EVIDENCE_LOG`` to a file path. Unset means no I/O,
default assembly unchanged.

This is the trigger-time half of ASG-SI-style evidence. The candidate-rule
three-critic bundle (schema / semantic / replay) lives in the Scholar harness
and is recorded at write time, because the native trigger does not yet have
the distilled skill text.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

VERIFIER_VERSION = "skillforge-vag-1.0"
SCHEMA_VERSION = "1.0"


def emit_when_gate(
    path: str | Path,
    *,
    allowed: bool,
    reason: str,
    recent_success_rate: float,
) -> None:
    """Append one when-gate decision. Never raises (audit must not break evolution)."""
    record = {
        "schema_version": SCHEMA_VERSION,
        "verifier_version": VERIFIER_VERSION,
        "kind": "when_gate",
        "verified": bool(allowed),
        "decision": "allow_evolve" if allowed else "suppress",
        "reason": str(reason),
        "recent_success_rate": float(recent_success_rate),
        "checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ts": time.time(),
    }
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        return


def evidence_log_path_from_env() -> str:
    """Return ``JIUWEN_EVIDENCE_LOG`` or empty (disabled)."""
    return str(os.environ.get("JIUWEN_EVIDENCE_LOG") or "").strip()


def maybe_log_when_gate(
    *,
    allowed: bool,
    reason: str,
    recent_success_rate: float,
    extra: dict[str, Any] | None = None,
) -> None:
    path = evidence_log_path_from_env()
    if not path:
        return
    if extra:
        # extra is folded into the reason trail only; keep the on-disk schema stable
        reason = f"{reason} {json.dumps(extra, ensure_ascii=False)}"
    emit_when_gate(
        path,
        allowed=allowed,
        reason=reason,
        recent_success_rate=recent_success_rate,
    )
