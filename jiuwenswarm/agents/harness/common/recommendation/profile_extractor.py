# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Recommendation state persistence for the proactive recommendation engine.

只存引擎运行态——冷却记录 + 推荐历史。用户画像已废弃（所有推荐基于当前对话）。

Storage: ``~/.jiuwenswarm/agent/workspace/recommendation.json``
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
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
    """Past recommendations with type, target, reason, timestamp (max 20).
    Each record now includes 'id' (unique ID) and 'content' (LLM-generated text)."""

    feedback_buffer: list[dict[str, Any]] = field(default_factory=list)
    """Pending feedback buffer (FIFO, max 20).
    Feedback arrives here, consumed in next tick for batch gradient update."""

    strategy_gradients: list[dict[str, Any]] = field(default_factory=list)
    """Strategy gradients / Filter Memory (max 10).
    Text-based recommendation strategy rules."""

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
            feedback_buffer=(
                data.get("feedback_buffer", [])
                if isinstance(data.get("feedback_buffer"), list)
                else []
            ),
            strategy_gradients=(
                data.get("strategy_gradients", [])
                if isinstance(data.get("strategy_gradients"), list)
                else []
            ),
            last_updated=data.get("last_updated", ""),
        )
    except Exception as exc:
        logger.warning("[RecommendationState] load failed: %s", exc)
        return RecommendationState()


def save_recommendation_state(state: RecommendationState, path: Path | None = None) -> None:
    """Persist state to disk（原子写）.

    写入路径：同目录临时文件 → os.replace 原子替换。直接 write_text 是
    "先截断再写"——写一半崩溃或多写者并发会留下残缺 JSON，下次
    load 失败返回空态并被后续保存固化，反馈缓冲/策略梯度/推荐历史
    全部永久清零。os.replace 保证任意时刻磁盘上只有完整的旧版或
    完整的新版；保存失败（含替换中断）时此前的好状态完好无损。
    """
    state_path = path or _default_state_path()
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_name: str | None = None
    try:
        fd, tmp_name = tempfile.mkstemp(
            dir=state_path.parent, prefix=f".{state_path.name}.", suffix=".tmp"
        )
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(asdict(state), ensure_ascii=False, indent=2))
        os.replace(tmp_name, state_path)
    except Exception as exc:
        logger.warning("[RecommendationState] save failed: %s", exc)
        if tmp_name:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass  # 清理失败无需再报——临时文件不影响正式状态文件
