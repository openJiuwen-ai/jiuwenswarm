# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""StateStore — SkillDev 任务状态的持久化层.

职责：在 Pipeline 的阶段边界 checkpoint 状态，支持断线/重启后从上次进度恢复。

当前实现：本地文件（state.json），适合单机部署。
扩展点：替换为 Redis 实现以支持多实例水平扩展（接口不变）。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from jiuwenclaw.agentserver.skilldev.schema import SkillDevState

logger = logging.getLogger(__name__)


class StateStore:
    """SkillDev 任务状态存储（本地文件实现）.

    线程/协程安全注意：当前本地文件实现不加锁，
    因为路由层保证同一 task_id 的请求始终路由到同一实例，不存在并发写入。
    """

    def __init__(self, base_dir: Path) -> None:
        """
        Args:
            base_dir: SkillDev 工作区根目录，约定为 get_workspace_dir() / "skilldev"
                      即 ~/.jiuwenclaw/agent/workspace/skilldev/
        """
        self._base_dir = base_dir

    def _state_file(self, task_id: str) -> Path:
        return self._base_dir / task_id / "state.json"

    async def save_state(self, task_id: str, state: SkillDevState) -> None:
        """将状态序列化并写入 state.json（checkpoint）."""
        state.touch()
        state_file = self._state_file(task_id)
        state_file.parent.mkdir(parents=True, exist_ok=True)
        data = state.to_checkpoint_dict()
        state_file.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        logger.debug(
            "[session=%s] [StateStore] checkpoint saved: stage=%s",
            task_id,
            state.stage.value,
        )

    async def load_state(self, task_id: str) -> SkillDevState | None:
        """从 state.json 恢复状态，不存在则返回 None."""
        state_file = self._state_file(task_id)
        if not state_file.exists():
            logger.warning("[session=%s] [StateStore] state not found", task_id)
            return None
        data = json.loads(state_file.read_text(encoding="utf-8"))
        state = SkillDevState.from_checkpoint_dict(data)
        logger.debug(
            "[session=%s] [StateStore] state loaded: stage=%s",
            task_id,
            state.stage.value,
        )
        return state

    def load_state_sync(self, task_id: str) -> SkillDevState | None:
        """同步版 load_state，供非 async 上下文使用（如 status 查询）."""
        data = self.load_checkpoint_dict(task_id)
        if data is None:
            return None
        if str(data.get("runner") or "") == "agent":
            return None
        return SkillDevState.from_checkpoint_dict(data)

    def load_checkpoint_dict(self, task_id: str) -> dict[str, Any] | None:
        """读取 state.json 原始字典（Pipeline / Agent 通用）."""
        state_file = self._state_file(task_id)
        if not state_file.exists():
            return None
        data = json.loads(state_file.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        return data

    def save_checkpoint_dict(self, task_id: str, data: dict[str, Any]) -> None:
        """写入 state.json（Agent checkpoint 或 Pipeline 字典）."""
        state_file = self._state_file(task_id)
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.debug(
            "[session=%s] [StateStore] checkpoint dict saved: runner=%s status=%s stage=%s",
            task_id,
            data.get("runner", "pipeline"),
            data.get("status"),
            data.get("stage"),
        )

    def list_tasks(self) -> list[str]:
        """列出所有存在 checkpoint 的 task_id."""
        if not self._base_dir.exists():
            return []
        return [
            d.name
            for d in self._base_dir.iterdir()
            if d.is_dir() and (d / "state.json").exists()
        ]
