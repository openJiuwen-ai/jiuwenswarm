# -*- coding: utf-8 -*-
"""
待办事项采集器

功能：
- 读取 todo.md 文件
- 解析任务状态
- 统计完成情况
"""

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional


@dataclass
class TodoTask:
    """待办任务"""

    id: str
    content: str
    status: str  # completed, running, waiting, cancelled
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "content": self.content,
            "status": self.status,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


@dataclass
class TodoStats:
    """待办统计"""

    total: int = 0
    completed: int = 0
    running: int = 0
    waiting: int = 0
    cancelled: int = 0
    tasks: list[TodoTask] = field(default_factory=list)

    @property
    def completion_rate(self) -> float:
        """完成率"""
        if self.total == 0:
            return 0.0
        return self.completed / self.total

    def to_dict(self) -> dict:
        return {
            "total": self.total,
            "completed": self.completed,
            "running": self.running,
            "waiting": self.waiting,
            "cancelled": self.cancelled,
            "completion_rate": round(self.completion_rate, 2),
            "tasks": [t.to_dict() for t in self.tasks],
        }


class TodoCollector:
    """待办事项采集器"""

    def __init__(self, workspace_dir: str | Path):
        """
        初始化待办采集器

        Args:
            workspace_dir: Agent 根目录（如 ~/.jiuwenclaw/agent）
        """
        self.workspace_dir = Path(workspace_dir)
        self.session_dir = self.workspace_dir / "sessions"

    @staticmethod
    def _read_file_safe(file_path: Path) -> str:
        """安全读取文件"""
        if not file_path.exists():
            return ""
        try:
            return file_path.read_text(encoding="utf-8")
        except Exception:
            return ""

    def _find_latest_todo_file(self) -> Optional[Path]:
        """查找最新的 todo.md 文件"""
        if not self.session_dir.exists():
            return None

        todo_files = list(self.session_dir.rglob("todo.md"))

        if not todo_files:
            return None

        # 按修改时间排序，返回最新的
        todo_files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
        return todo_files[0]

    @staticmethod
    def _parse_status(line: str) -> tuple[str, str]:
        """
        解析任务行，提取 ID 和状态

        支持格式：
        - [x] 1. 任务内容
        - [ ] 1. 任务内容
        - 1. [status:completed] 任务内容
        - 1. ✅ 任务内容
        """
        # Markdown checkbox 格式
        checkbox_match = re.match(r"\s*-\s*\[([xX ])\]\s*(.+)", line)
        if checkbox_match:
            checked = checkbox_match.group(1).lower() == "x"
            content = checkbox_match.group(2).strip()
            status = "completed" if checked else "waiting"
            return "", status

        # 带状态标记格式
        status_match = re.match(r"\s*(\d+)\.\s*\[status:(\w+)\]\s*(.+)", line, re.IGNORECASE)
        if status_match:
            task_id = status_match.group(1)
            status = status_match.group(2).lower()
            content = status_match.group(3).strip()
            return task_id, status

        # 带状态标记格式（中括号前）
        bracket_match = re.match(r"\s*(\d+)\.\s*\[([xX✅🔄⏳❌])\]\s*(.+)", line)
        if bracket_match:
            task_id = bracket_match.group(1)
            status_char = bracket_match.group(2)
            content = bracket_match.group(3).strip()

            status_map = {
                "x": "completed",
                "X": "completed",
                "✅": "completed",
                "🔄": "running",
                "⏳": "waiting",
                "❌": "cancelled",
            }
            status = status_map.get(status_char, "waiting")
            return task_id, status

        # 普通编号格式
        number_match = re.match(r"\s*(\d+)\.\s+(.+)", line)
        if number_match:
            task_id = number_match.group(1)
            content = number_match.group(2).strip()

            # 从内容中检测状态
            if "✅" in content or "[完成]" in content:
                status = "completed"
            elif "🔄" in content or "[进行中]" in content:
                status = "running"
            elif "❌" in content or "[取消]" in content:
                status = "cancelled"
            else:
                status = "waiting"

            return task_id, status

        return "", ""

    def collect(self) -> TodoStats:
        """
        采集待办数据

        Returns:
            TodoStats: 待办统计数据
        """
        stats = TodoStats()

        todo_file = self._find_latest_todo_file()
        if not todo_file:
            return stats

        content = self._read_file_safe(todo_file)
        if not content:
            return stats

        task_counter = 0

        for line in content.split("\n"):
            if not line.strip():
                continue

            task_id, status = self._parse_status(line)

            if status:
                task_counter += 1

                # 提取任务内容（去除状态标记）
                content_clean = re.sub(r"\[status:\w+\]\s*", "", line)
                content_clean = re.sub(r"\[[xX✅🔄⏳❌]\]\s*", "", content_clean)
                content_clean = re.sub(r"^\s*-\s*\[[xX ]\]\s*", "", content_clean)
                content_clean = re.sub(r"^\s*\d+\.\s*", "", content_clean)
                content_clean = content_clean.strip()

                if not task_id:
                    task_id = str(task_counter)

                task = TodoTask(
                    id=task_id,
                    content=content_clean,
                    status=status,
                )

                stats.tasks.append(task)
                stats.total += 1

                if status == "completed":
                    stats.completed += 1
                elif status == "running":
                    stats.running += 1
                elif status == "waiting":
                    stats.waiting += 1
                elif status == "cancelled":
                    stats.cancelled += 1

        return stats


def main():
    """测试入口"""
    import sys

    if len(sys.argv) < 2:
        print("Usage: python todo_collector.py <workspace_dir>")
        sys.exit(1)

    workspace_dir = sys.argv[1]
    collector = TodoCollector(workspace_dir)
    stats = collector.collect()

    print("待办统计:")
    print(f"  总数: {stats.total}")
    print(f"  已完成: {stats.completed}")
    print(f"  进行中: {stats.running}")
    print(f"  待处理: {stats.waiting}")
    print(f"  完成率: {stats.completion_rate:.1%}")

    if stats.tasks:
        print("\n任务列表:")
        for task in stats.tasks:
            status_icon = {
                "completed": "✅",
                "running": "🔄",
                "waiting": "⏳",
                "cancelled": "❌",
            }.get(task.status, "❓")
            print(f"  {status_icon} [{task.id}] {task.content}")


if __name__ == "__main__":
    main()
