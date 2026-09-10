"""RsiArtifactService：采纳节点快照 + 下载定位（内部 v3 §4.5 / web §8.3）。

- ``make_snapshot``：``node.created.artifacts[]``（引擎本地产物路径）→ 快照 zip。
- ``locate``：``artifact_id`` 空 → 任务最终产物（best_artifact = 最新 adopted 节点快照）；指定 → 中间产物（全保留）。
- 下载通道：Gateway HTTP Range bridge（``app_web.py`` 既有 `_serve_verified_local_download`）；
  本层只负责定位出可下载的 zip 文件路径，不实现 HTTP 服务。
- zip 打包契约（format TODO）：本期仅按 artifacts 相对路径打包，保留目录结构。
"""

from __future__ import annotations

import logging
import zipfile
from pathlib import Path
from typing import Any

from jiuwenswarm.agents.harness.common.rsi.errors import RsiArtifactNotFound, RsiTaskNotFound
from jiuwenswarm.agents.harness.common.rsi.models import (
    RsiArtifactFile as ArtifactFile,
    ArtifactKind,
    RsiArtifactPath,
)

logger = logging.getLogger(__name__)

_SNAPSHOT_SUBDIR = "snapshots"


class RsiArtifactService:
    """产物/快照服务。

    Args:
        tasks_root: ``.jiuwenswarm/rsi/tasks``（与 TaskStore 同根）；快照落在
            ``<task_dir>/snapshots/A<node_id>.zip``。
    """

    def __init__(self, tasks_root: Path) -> None:
        self.tasks_root = Path(tasks_root)

    # -- 快照 --

    def make_snapshot(self, task_id: str, node_ref: str, node_id: str, artifacts: list[RsiArtifactPath]) -> str | None:
        """把引擎本地产物路径打包为 zip 快照。

        - 仅 ADOPTED 节点触发（由调用方保证；内部 v3 §4.5）。
        - 入参 ``node_ref`` 为引擎侧引用（调试/日志）；``node_id`` 为服务侧稳定 ID（``N<序号>``）。
        - 返回 ``artifact_id``（``A<node_id>``）；无产物文件时返回 None。
        """
        usable = self._resolve_artifact_paths(task_id, artifacts)
        if not usable:
            logger.warning("[RSI] make_snapshot: %s 无可用产物文件, 跳过快照", node_id)
            return None
        task_dir = Path(self.tasks_root) / task_id
        snapshots_dir = task_dir / _SNAPSHOT_SUBDIR
        snapshots_dir.mkdir(parents=True, exist_ok=True)
        artifact_id = f"A{node_id}"
        zip_path = snapshots_dir / f"{artifact_id}.zip"
        if zip_path.is_file():
            return artifact_id
        written: list[Path] = []
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for role, src in usable:
                arcname = f"{role}_{src.name}" if len(usable) > 1 else src.name
                files = sorted(src.rglob("*")) if src.is_dir() else [src]
                for source in files:
                    if not source.is_file():
                        continue
                    # A plugin link must not pull unrelated host files into a download.
                    if src.is_dir() and not source.resolve().is_relative_to(src.resolve()):
                        continue
                    name = f"{arcname}/{source.relative_to(src).as_posix()}" if src.is_dir() else arcname
                    zf.write(source, arcname=name)
                    written.append(source)
        logger.info("[RSI] make_snapshot: task=%s node=%s artifact_id=%s files=%d", task_id, node_id, artifact_id, len(written))
        return artifact_id

    def locate(self, task_id: str, artifact_id: str | None = None) -> ArtifactFile:
        """``rsi.artifact.download`` 定位（内部 v3 §4.5 / web §8.3）。"""
        task_dir = Path(self.tasks_root) / task_id
        if not task_dir.is_dir():
            raise RsiTaskNotFound(task_id)
        snapshots_dir = task_dir / _SNAPSHOT_SUBDIR
        if artifact_id:
            zip_path = snapshots_dir / f"{artifact_id}.zip"
            if not zip_path.is_file():
                raise RsiArtifactNotFound(f"artifact 不存在: {artifact_id}")
            return ArtifactFile(path=str(zip_path), kind=ArtifactKind.HARNESS_PLUGIN, is_best=False)
        # 空 → 任务最终产物（best_artifact = 最新 adopted 快照）
        best = self._latest_snapshot(task_id)
        if best is None:
            raise RsiArtifactNotFound(f"任务 {task_id} 尚无产物快照")
        return ArtifactFile(path=str(best), kind=ArtifactKind.HARNESS_PLUGIN, is_best=True)

    def best_artifact(self, task_id: str) -> dict[str, Any] | None:
        """``rsi.task.get`` / ``rsi.report.get`` 的 best_artifact 对象（web §3.4 语义）。"""
        best = self._latest_snapshot(task_id, include_root=False)
        if best is None:
            return None
        return {
            "artifact_id": best.stem,
            "name": best.name,
            "adopted": True,
        }

    # -- 内部 --

    def _resolve_artifact_paths(self, task_id: str, artifacts: list[RsiArtifactPath]) -> list[tuple[str, Path]]:
        """解析引擎产物路径：绝对路径直接用；相对路径挂在 task run_dir 下。"""
        task_dir = Path(self.tasks_root) / task_id
        usable: list[tuple[str, Path]] = []
        for art in artifacts:
            raw = art.path
            if not raw:
                continue
            p = Path(raw).expanduser()
            if not p.is_absolute():
                p = task_dir / p
            if p.is_file() or (art.format == "dir" and p.is_dir()):
                usable.append((art.role, p))
            else:
                logger.warning("[RSI] artifact 路径无效跳过: role=%s path=%s", art.role, raw)
        return usable

    def _latest_snapshot(self, task_id: str, *, include_root: bool = True) -> Path | None:
        snapshots_dir = Path(self.tasks_root) / task_id / _SNAPSHOT_SUBDIR
        if not snapshots_dir.is_dir():
            return None
        candidates = [p for p in snapshots_dir.glob("A*.zip") if p.is_file()]
        if not include_root:
            candidates = [p for p in candidates if p.stem != "AROOT"]
        if not candidates:
            return None
        return max(candidates, key=lambda p: p.stat().st_mtime)
