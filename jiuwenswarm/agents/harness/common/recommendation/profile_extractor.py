# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Recommendation state persistence for the proactive recommendation engine.

只存引擎运行态——冷却记录 + 推荐历史。用户画像已废弃（所有推荐基于当前对话）。

Storage: ``~/.jiuwenswarm/agent/workspace/recommendation.json``
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ── RecommendationState ──────────────────────────────────────────


@dataclass
class RecommendationState:
    """Persistent state for the proactive recommendation engine.

    Only engine-managed runtime state — no user profile fields.
    User profile (preferences/goals/interests/commitments) is deprecated;
    all recommendations are now based on the current conversation.
    """

    recommendation_history: list[dict[str, Any]] = field(default_factory=list)
    """Past recommendations with type, target, reason, timestamp (max 20)."""

    cooldown_records: dict[str, float] = field(default_factory=dict)
    """Cooldown records: target -> last recommended timestamp."""

    last_updated: str = ""

    def add_recommendation(self, rec: dict[str, Any]) -> None:
        """Append a recommendation record and cap at 20 entries."""
        self.recommendation_history.append(rec)
        if len(self.recommendation_history) > 20:
            self.recommendation_history = self.recommendation_history[-20:]

    def touch(self) -> None:
        """Update last_updated timestamp."""
        self.last_updated = datetime.now(timezone.utc).isoformat()


# ── File helpers ──────────────────────────────────────────────────


def _default_state_path() -> Path:
    from jiuwenswarm.common.utils import get_agent_workspace_dir
    return get_agent_workspace_dir() / "recommendation.json"


def load_recommendation_state(path: Path | None = None) -> RecommendationState:
    """Load state from disk, returning empty state on missing/corrupt file."""
    state_path = path or _default_state_path()
    if not state_path.exists() or state_path.stat().st_size == 0:
        return RecommendationState()
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return RecommendationState()

        return RecommendationState(
            recommendation_history=(
                data.get("recommendation_history", [])
                if isinstance(data.get("recommendation_history"), list)
                else []
            ),
            cooldown_records=data.get("cooldown_records", {}) if isinstance(data.get("cooldown_records"), dict) else {},
            last_updated=data.get("last_updated", ""),
        )
    except Exception as exc:
        logger.warning("[RecommendationState] load failed: %s", exc)
        return RecommendationState()


def save_recommendation_state(state: RecommendationState, path: Path | None = None) -> None:
    """Persist state to disk."""
    state_path = path or _default_state_path()
    state_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        state_path.write_text(
            json.dumps(asdict(state), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:
        logger.warning("[RecommendationState] save failed: %s", exc)
