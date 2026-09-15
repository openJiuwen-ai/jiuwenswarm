"""RsiTaskStore：任务存储 + 状态机（内部 v3 §4.1 + 一致性规则 §8）。

- JSON 文件存储：``.jiuwenswarm/rsi/tasks/<task_id>/task.json``（内部 v3 §1 边界：不新增 DB）。
- 状态机唯一入口 ``update_status(from_states, to, cause)``；非法迁移抛 ``TASK_STATE_CONFLICT``。
- P1 钩子：成功迁移后触发 ``on_status_changed(task_id, old_status, new_status)``（服务侧权威，不依赖引擎事件）。
- ``delete`` 按 guard 校验运行中/排队/暂停/在用产物不可删。
"""

from __future__ import annotations

import json
import shutil
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from jiuwenswarm.agents.harness.common.rsi.errors import RsiTaskNotFound, RsiTaskStateConflict
from jiuwenswarm.agents.harness.common.rsi.models import (
    RsiTask,
    RsiTaskView,
    TaskStatus,
    utcnow_iso,
)

#: 合法状态迁移表（内部 v3 §6）。key=当前状态；value=允许迁移到的状态。
_STATUS_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.CREATED: frozenset({TaskStatus.QUEUED, TaskStatus.TERMINATED}),
    TaskStatus.QUEUED: frozenset({TaskStatus.RUNNING, TaskStatus.PAUSED, TaskStatus.TERMINATED}),
    TaskStatus.RUNNING: frozenset({TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.PAUSED, TaskStatus.TERMINATED}),
    TaskStatus.PAUSED: frozenset({TaskStatus.QUEUED, TaskStatus.TERMINATED}),
    TaskStatus.COMPLETED: frozenset(),
    TaskStatus.FAILED: frozenset(),
    TaskStatus.TERMINATED: frozenset(),
}

#: 不可删除的状态（一致性规则 §8.2）
_NON_DELETABLE_STATES = frozenset({TaskStatus.QUEUED, TaskStatus.RUNNING, TaskStatus.PAUSED})

_LOCK = threading.RLock()


class RsiTaskStore:
    """任务存储 + 状态机（JSON 文件, 原文件内锁保护）。"""

    def __init__(self, tasks_root: Path, *, on_status_changed: Callable[[str, str, str], None] | None = None) -> None:
        self.tasks_root = Path(tasks_root)
        self.tasks_root.mkdir(parents=True, exist_ok=True)
        self._on_status_changed = on_status_changed

    # -- 路径 --

    @staticmethod
    def task_dir(tasks_root: Path, task_id: str) -> Path:
        return Path(tasks_root) / task_id

    # -- 增查删 --

    def create(self, task: RsiTask) -> RsiTask:
        """写盘新任务（状态恒 CREATED；task_id 由服务层生成后传入）。"""
        if not task.task_id:
            raise ValueError("task_id must be provided by service layer")
        task_dir = self.task_dir(self.tasks_root, task.task_id)
        task_dir.mkdir(parents=True, exist_ok=True)
        payload = task.to_dict()
        with _LOCK:
            self._write_task(task_dir, payload)
        return task

    def get(self, task_id: str) -> RsiTask:
        payload = self._read_task(task_id)
        if payload is None:
            raise RsiTaskNotFound(task_id)
        return RsiTask.from_dict(payload)

    def get_view(self, task_id: str) -> RsiTaskView:
        return self.get(task_id).to_taskview()

    def list(self, *, scenario: str | None = None, artifact_type: str | None = None) -> list[RsiTask]:
        """纯存储投影（web §6.2）。scenario/artifact_type 可选过滤。"""
        tasks: list[RsiTask] = []
        with _LOCK:
            for task_dir in sorted(self.tasks_root.iterdir()):
                if not task_dir.is_dir():
                    continue
                payload = self._read_task_dir(task_dir)
                if payload is None:
                    continue
                if scenario and payload.get("scenario") != scenario:
                    continue
                if artifact_type and payload.get("artifact_type") != artifact_type:
                    continue
                tasks.append(RsiTask.from_dict(payload))
        tasks.sort(key=lambda t: t.created_at, reverse=True)
        return tasks

    def delete(self, task_id: str, *, forbid_running: bool = True, forbid_active_artifact: bool = True) -> None:
        """删除任务（一致性规则 §8.2：运行中/排队/暂停/在用产物不可删）。"""
        task = self.get(task_id)
        state = TaskStatus(task.status)
        if forbid_running and state in _NON_DELETABLE_STATES:
            raise RsiTaskStateConflict(f"任务 {task_id} 状态 {state.value} 不可删除")
        if forbid_active_artifact:
            active = (task.config or {}).get("harness_refs_path")
            if active:
                # 快照已消费（发布 refs）时仍可删：仅当该 refs 仍被 RsiArtifactService 标记在用才拦截。
                # 当前无反向注册表，借助 config.active_ref_consumed 标志（由 download/装载路径置位）。
                if not bool(task.config.get("active_ref_released", False)):
                    raise RsiTaskStateConflict(f"任务 {task_id} 产物仍在生效，不可删除")
        task_dir = self.task_dir(self.tasks_root, task_id)
        with _LOCK:
            try:
                shutil.rmtree(task_dir)
            except FileNotFoundError:
                pass  # 目录已不存在视为删除成功（幂等）

    # -- 状态机 --

    def update_status(self, task_id: str, from_states: list[str], to_state: str, cause: str = "") -> RsiTask:
        """状态机唯一入口（内部 v3 §4.1）。

        - ``from_states`` 约束合法迁移；当前状态不在其中 → ``TASK_STATE_CONFLICT``。
        - 成功迁移后 P1 钩子 ``on_status_changed(task_id, old, new)``。
        """
        task_dir = self.task_dir(self.tasks_root, task_id)
        with _LOCK:
            current = self.get(task_id)  # re-read under lock
            old = current.status
            if old not in set(from_states):
                raise RsiTaskStateConflict(
                    f"任务 {task_id} 状态 {old} 不允许迁移到 {to_state}（允许来源: {sorted(from_states)}）"
                )
            allowed = _STATUS_TRANSITIONS.get(TaskStatus(old), frozenset())
            if to_state not in allowed:
                raise RsiTaskStateConflict(f"任务 {task_id} 状态 {old} 不允许迁移到 {to_state}")
            payload = current.to_dict()
            payload["status"] = to_state
            payload["updated_at"] = utcnow_iso()
            if cause:
                history = payload.setdefault("status_history", [])
                if not isinstance(history, list):
                    history = []
                history.append({"from": old, "to": to_state, "cause": cause, "ts": utcnow_iso()})
                payload["status_history"] = history
            self._write_task(task_dir, payload)
        # P1 钩子放在锁外执行（回调不应持有文件锁）
        if self._on_status_changed is not None:
            self._on_status_changed(task_id, old, to_state)
        return RsiTask.from_dict(payload)

    def merge_results(self, task_id: str, results: dict[str, Any]) -> None:
        """锁内合并引擎结果到 task.json.config.results（worker 落盘唯一入口）。"""
        task_dir = self.task_dir(self.tasks_root, task_id)
        with _LOCK:
            current = self.get(task_id)
            payload = current.to_dict()
            config = dict(payload.get("config") or {})
            merged = dict(config.get("results") or {})
            merged.update({k: v for k, v in results.items() if v is not None})
            config["results"] = merged
            payload["config"] = config
            payload["updated_at"] = utcnow_iso()
            self._write_task(task_dir, payload)

    def merge_config(self, task_id: str, values: dict[str, Any]) -> None:
        """Merge service-owned config metadata without replacing other fields."""

        if not isinstance(values, dict):
            raise ValueError("config values must be a mapping")
        task_dir = self.task_dir(self.tasks_root, task_id)
        with _LOCK:
            current = self.get(task_id)
            payload = current.to_dict()
            config = dict(payload.get("config") or {})
            config.update(values)
            payload["config"] = config
            payload["updated_at"] = utcnow_iso()
            self._write_task(task_dir, payload)

    def set_status_changed_callback(self, callback: Callable[[str, str, str], None] | None) -> None:
        """公开注入状态变更 P1 钩子（替代直接改私有属性，PR !5798 #12）。"""
        self._on_status_changed = callback

    def mark_active_ref_released(self, task_id: str) -> None:
        """下载/装载路径消费产物后置位：允许后续 delete（在用产物放行）。"""
        task = self.get(task_id)
        task_dir = self.task_dir(self.tasks_root, task_id)
        with _LOCK:
            payload = task.to_dict()
            config = dict(payload.get("config") or {})
            config["active_ref_released"] = True
            payload["config"] = config
            payload["updated_at"] = utcnow_iso()
            self._write_task(task_dir, payload)

    # -- 内部 --

    def _read_task(self, task_id: str) -> dict[str, Any] | None:
        task_dir = self.task_dir(self.tasks_root, task_id)
        return self._read_task_dir(task_dir)

    @staticmethod
    def _read_task_dir(task_dir: Path) -> dict[str, Any] | None:
        task_file = task_dir / "task.json"
        if not task_file.is_file():
            return None
        try:
            with task_file.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    @staticmethod
    def _write_task(task_dir: Path, payload: dict[str, Any]) -> None:
        task_dir.mkdir(parents=True, exist_ok=True)
        task_file = task_dir / "task.json"
        tmp = task_file.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        tmp.replace(task_file)
