# -*- coding: utf-8 -*-
"""
数据聚合器

功能：
- 整合所有采集器的数据
- 统一时间窗口过滤
- 提供统一的数据访问接口
"""

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

# 与 run_report / work_analyzer 一致：日历日与采集时间戳使用东八区
_REPORT_TZ = ZoneInfo("Asia/Shanghai")

from .email_collector import EmailCollector, EmailStats
from .git_collector import GitCollector, GitStats
from .memory_collector import MemoryCollector, MemoryData
from .todo_collector import TodoCollector, TodoStats


@dataclass
class CollectedData:
    """聚合后的数据"""

    date: str  # 日期
    collected_at: datetime  # 采集时间

    # Git 数据
    git: GitStats = field(default_factory=GitStats)

    # 邮件数据
    email: EmailStats = field(default_factory=EmailStats)

    # 记忆数据
    memory: MemoryData = field(default_factory=MemoryData)

    # 待办数据
    todo: TodoStats = field(default_factory=TodoStats)

    # 历史对比数据
    comparison: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "date": self.date,
            "collected_at": self.collected_at.isoformat(),
            "git": self.git.to_dict(),
            "email": self.email.to_dict(),
            "memory": self.memory.to_dict(),
            "todo": self.todo.to_dict(),
            "comparison": self.comparison,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)


class DataAggregator:
    """数据聚合器"""

    def __init__(
        self,
        workspace_dir: str | Path,
        git_repo: Optional[str | Path] = None,
        email_config: Optional[dict] = None,
    ):
        """
        初始化数据聚合器

        Args:
            workspace_dir: workspace 目录
            git_repo: Git 仓库路径
            email_config: 邮箱配置 {"address": str, "auth_code": str, "provider": str}
        """
        self.workspace_dir = Path(workspace_dir)

        # 初始化各采集器
        self.memory_collector = MemoryCollector(self.workspace_dir)
        self.todo_collector = TodoCollector(self.workspace_dir)

        # Git 采集器（可选）
        self.git_collector = None
        if git_repo:
            self.git_collector = GitCollector(git_repo)

        # 邮件采集器（可选）
        self.email_collector = None
        self.email_config = email_config

    def collect(self, date: Optional[str] = None, include_comparison: bool = True) -> CollectedData:
        """
        聚合采集数据

        Args:
            date: 日期字符串，默认今天
            include_comparison: 是否包含历史对比

        Returns:
            CollectedData: 聚合后的数据
        """
        if date is None:
            date = datetime.now(_REPORT_TZ).strftime("%Y-%m-%d")

        data = CollectedData(
            date=date,
            collected_at=datetime.now(_REPORT_TZ),
        )

        # 采集记忆数据
        data.memory = self.memory_collector.collect(date)

        # 采集待办数据
        data.todo = self.todo_collector.collect()

        # 采集 Git 数据
        if self.git_collector:
            data.git = self.git_collector.get_commits(date)

        # 采集邮件数据
        if self.email_config and self.email_collector is None:
            try:
                self.email_collector = EmailCollector(
                    email_address=self.email_config["address"],
                    auth_code=self.email_config["auth_code"],
                    provider=self.email_config.get("provider", "163"),
                )
            except Exception as e:
                print(f"邮件采集器初始化失败: {e}")

        if self.email_collector:
            try:
                with self.email_collector:
                    data.email = self.email_collector.get_stats(date)
            except Exception as e:
                print(f"邮件数据采集失败: {e}")

        # 历史对比
        if include_comparison:
            data.comparison = self._generate_comparison(data, date)

        return data

    def _generate_comparison(self, current_data: CollectedData, date: str) -> dict:
        """生成历史对比数据"""
        comparison = {}

        try:
            current_date = datetime.strptime(date, "%Y-%m-%d")

            # 与昨日对比
            yesterday = (current_date - timedelta(days=1)).strftime("%Y-%m-%d")
            yesterday_data = self._collect_light(yesterday)

            comparison["yesterday"] = {
                "git_commits": {
                    "current": current_data.git.total_commits,
                    "previous": yesterday_data.git.total_commits,
                    "change": current_data.git.total_commits - yesterday_data.git.total_commits,
                },
                "todo_completed": {
                    "current": current_data.todo.completed,
                    "previous": yesterday_data.todo.completed,
                    "change": current_data.todo.completed - yesterday_data.todo.completed,
                },
            }

            # 与上周同期对比
            last_week = (current_date - timedelta(days=7)).strftime("%Y-%m-%d")
            last_week_data = self._collect_light(last_week)

            comparison["last_week"] = {
                "git_commits": {
                    "current": current_data.git.total_commits,
                    "previous": last_week_data.git.total_commits,
                    "change": current_data.git.total_commits - last_week_data.git.total_commits,
                },
            }

        except Exception:
            pass

        return comparison

    def _collect_light(self, date: str) -> CollectedData:
        """轻量采集（仅 Git 和记忆）"""
        data = CollectedData(
            date=date,
            collected_at=datetime.now(_REPORT_TZ),
        )

        if self.git_collector:
            data.git = self.git_collector.get_commits(date)

        data.memory = self.memory_collector.collect(date)

        return data

    def collect_week(self, end_date: Optional[str] = None) -> dict[str, CollectedData]:
        """采集一周的数据"""
        if end_date is None:
            end_date = datetime.now(_REPORT_TZ)
        else:
            end_date = datetime.strptime(end_date, "%Y-%m-%d").replace(tzinfo=_REPORT_TZ)

        result = {}
        for i in range(7):
            date = (end_date - timedelta(days=i)).strftime("%Y-%m-%d")
            result[date] = self.collect(date, include_comparison=False)

        return result

    def collect_month(self, year: int, month: int) -> dict[str, CollectedData]:
        """采集一月的数据"""
        import calendar

        _, days_in_month = calendar.monthrange(year, month)
        result = {}

        for day in range(1, days_in_month + 1):
            date = f"{year:04d}-{month:02d}-{day:02d}"
            result[date] = self.collect(date, include_comparison=False)

        return result

    def collect_for_pattern_analysis(self, days: int = 7) -> list[dict]:
        """
        采集用于工作模式分析的数据

        Args:
            days: 天数，默认 7 天

        Returns:
            包含日期、时间、提交信息的字典列表
        """
        if self.git_collector:
            return self.git_collector.get_commits_for_pattern_analysis(days)
        return []


def main():
    """测试入口"""
    import sys

    if len(sys.argv) < 2:
        print("Usage: python aggregator.py <workspace_dir> [git_repo] [email] [auth_code]")
        sys.exit(1)

    workspace_dir = sys.argv[1]
    git_repo = sys.argv[2] if len(sys.argv) > 2 else None
    email_config = None

    if len(sys.argv) > 4:
        email_config = {
            "address": sys.argv[3],
            "auth_code": sys.argv[4],
            "provider": "163",
        }

    aggregator = DataAggregator(
        workspace_dir=workspace_dir,
        git_repo=git_repo,
        email_config=email_config,
    )

    print("采集数据中...")
    data = aggregator.collect()

    print(f"\n=== 数据采集结果 ({data.date}) ===\n")

    print("Git 统计:")
    print(f"  提交次数: {data.git.total_commits}")
    print(f"  修改文件: {data.git.total_files_changed}")
    print(f"  代码变更: +{data.git.total_insertions}/-{data.git.total_deletions}")

    print("\n邮件统计:")
    print(f"  今日收件: {data.email.received_today}")
    print(f"  今日发件: {data.email.sent_today}")
    print(f"  未读邮件: {data.email.unread}")

    print("\n待办统计:")
    print(f"  总数: {data.todo.total}")
    print(f"  已完成: {data.todo.completed}")
    print(f"  完成率: {data.todo.completion_rate:.1%}")

    print("\n记忆数据:")
    print(f"  今日记录: {len(data.memory.work_summaries)} 条")

    if data.comparison:
        print("\n历史对比:")
        if "yesterday" in data.comparison:
            y = data.comparison["yesterday"]
            print(f"  vs 昨日: 提交 {y['git_commits']['change']:+d}, 任务 {y['todo_completed']['change']:+d}")


if __name__ == "__main__":
    main()
