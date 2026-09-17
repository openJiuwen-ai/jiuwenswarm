# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""WorkspaceProvider — SkillDev 任务工作区管理.

职责：提供每个 task_id 的隔离工作区目录，并维护标准目录结构。

目录结构（单机本地模式）：
    ~/.jiuwenswarm/agent/workspace/skilldev/{task_id}/
    ├── state.json          ← StateStore checkpoint
    ├── resources/          ← 上传的资源文件（解压后）
    ├── skill/              ← 生成的 skill 目录
    │   ├── SKILL.md
    │   └── ...
    ├── evals/
    │   ├── evals.json      ← 测试用例定义
    │   └── iteration-{N}/  ← 每轮测试结果
    └── output/
        └── {skill_name}.skill  ← 最终打包产物

base_dir 由调用方传入，约定为 get_workspace_dir() / "skilldev"，
与整个 jiuwenswarm 的目录体系保持一致，不另起顶级目录。

扩展点：替换为支持远程对象存储的实现（接口不变），
        sync_to_remote 届时将文件同步到 S3/OBS。
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class WorkspaceProvider:
    """SkillDev 工作区管理（本地文件系统实现）."""

    def __init__(self, base_dir: Path) -> None:
        """
        Args:
            base_dir: SkillDev 工作区根目录，约定为 get_workspace_dir() / "skilldev"
                      即 ~/.jiuwenswarm/agent/workspace/skilldev/
        """
        self._base_dir = base_dir

    def get_local_path(self, task_id: str) -> Path:
        """返回指定任务的本地工作区路径（不保证已创建）."""
        return self._base_dir / task_id

    async def ensure_local(self, task_id: str) -> Path:
        """确保工作区目录及其标准子目录存在，返回工作区根路径."""
        workspace = self._base_dir / task_id
        for sub in ("resources", "skill", "evals", "output"):
            (workspace / sub).mkdir(parents=True, exist_ok=True)
        logger.debug("[WorkspaceProvider] workspace ready: %s", workspace)
        return workspace

    async def sync_to_remote(self, task_id: str) -> None:
        """将本地工作区同步到远程存储（本地实现为空操作）.

        扩展点：多实例部署时，此处将文件同步到共享存储（S3/OBS/NFS），
        以支持不同实例间的工作区共享。当前单机部署无需实现。
        """
        # 待实现: 生产环境实现远程同步（S3 / OBS / NFS）
        pass
