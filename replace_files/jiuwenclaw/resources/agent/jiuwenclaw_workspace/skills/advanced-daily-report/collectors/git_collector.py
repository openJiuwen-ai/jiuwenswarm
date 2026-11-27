# -*- coding: utf-8 -*-
"""
Git 提交记录采集器

功能：
- 获取指定日期的 Git 提交记录
- 统计提交次数、修改文件数、代码行数变化
- 支持多个仓库
"""

import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

_REPORT_TZ = ZoneInfo("Asia/Shanghai")


@dataclass
class GitCommit:
    """Git 提交记录"""

    hash: str  # 提交哈希
    message: str  # 提交信息
    author: str  # 作者
    date: datetime  # 提交时间
    files_changed: int = 0  # 修改文件数
    insertions: int = 0  # 新增行数
    deletions: int = 0  # 删除行数

    def to_dict(self) -> dict:
        return {
            "hash": self.hash,
            "message": self.message,
            "author": self.author,
            "date": self.date.isoformat(),
            "files_changed": self.files_changed,
            "insertions": self.insertions,
            "deletions": self.deletions,
        }


@dataclass
class GitStats:
    """Git 统计数据"""

    commits: list[GitCommit] = field(default_factory=list)
    total_commits: int = 0
    total_files_changed: int = 0
    total_insertions: int = 0
    total_deletions: int = 0

    @property
    def net_lines(self) -> int:
        """净增行数"""
        return self.total_insertions - self.total_deletions

    def to_dict(self) -> dict:
        return {
            "total_commits": self.total_commits,
            "total_files_changed": self.total_files_changed,
            "total_insertions": self.total_insertions,
            "total_deletions": self.total_deletions,
            "net_lines": self.net_lines,
            "commits": [c.to_dict() for c in self.commits],
        }


class GitCollector:
    """Git 提交记录采集器"""

    def __init__(self, repo_path: str | Path):
        """
        初始化 Git 采集器

        Args:
            repo_path: Git 仓库路径
        """
        self.repo_path = Path(repo_path).resolve()

    def _run_git_command(self, args: list[str], timeout: int = 30) -> str:
        """
        执行 Git 命令

        Args:
            args: Git 命令参数
            timeout: 超时时间（秒）

        Returns:
            命令输出
        """
        try:
            result = subprocess.run(
                ["git", "-C", str(self.repo_path)] + args,
                capture_output=True,
                text=True,
                timeout=timeout,
                encoding="utf-8",
                errors="replace",
            )
            return result.stdout.strip()
        except subprocess.TimeoutExpired:
            return ""
        except Exception as e:
            return ""

    def get_commits(self, date: Optional[str] = None, author: Optional[str] = None) -> GitStats:
        """
        获取指定日期的提交记录

        Args:
            date: 日期字符串 (YYYY-MM-DD)，默认今天
            author: 作者名称过滤，默认不过滤

        Returns:
            GitStats: Git 统计数据
        """
        if date is None:
            date = datetime.now(_REPORT_TZ).strftime("%Y-%m-%d")

        stats = GitStats()

        # 获取提交列表
        log_format = "%H|%s|%an|%ai"
        since = f"{date} 00:00:00"
        until = f"{date} 23:59:59"

        cmd_args = [
            "log",
            f"--since={since}",
            f"--until={until}",
            f"--format={log_format}",
        ]

        if author:
            cmd_args.append(f"--author={author}")

        log_output = self._run_git_command(cmd_args)

        if not log_output:
            return stats

        # 解析提交记录
        for line in log_output.split("\n"):
            if not line.strip():
                continue

            parts = line.split("|", 3)
            if len(parts) < 4:
                continue

            commit_hash, message, author_name, date_str = parts

            try:
                commit_date = datetime.fromisoformat(date_str.replace(" ", "T").split("+")[0])
            except ValueError:
                commit_date = datetime.now(_REPORT_TZ)

            # 获取每个提交的文件变更统计
            numstat = self._run_git_command(
                ["show", "--numstat", "--format=", commit_hash]
            )

            files_changed = 0
            insertions = 0
            deletions = 0

            for stat_line in numstat.split("\n"):
                if not stat_line.strip():
                    continue
                stat_parts = stat_line.split("\t")
                if len(stat_parts) >= 2:
                    try:
                        ins = int(stat_parts[0]) if stat_parts[0] != "-" else 0
                        dels = int(stat_parts[1]) if stat_parts[1] != "-" else 0
                        insertions += ins
                        deletions += dels
                        files_changed += 1
                    except ValueError:
                        continue

            commit = GitCommit(
                hash=commit_hash[:8],
                message=message.strip(),
                author=author_name,
                date=commit_date,
                files_changed=files_changed,
                insertions=insertions,
                deletions=deletions,
            )

            stats.commits.append(commit)
            stats.total_commits += 1
            stats.total_files_changed += files_changed
            stats.total_insertions += insertions
            stats.total_deletions += deletions

        return stats

    def get_week_commits(self, end_date: Optional[str] = None) -> dict[str, GitStats]:
        """
        获取一周内每天的提交统计

        Args:
            end_date: 结束日期，默认今天

        Returns:
            按日期分组的 GitStats 字典
        """
        if end_date is None:
            end_date = datetime.now(_REPORT_TZ)
        else:
            end_date = datetime.strptime(end_date, "%Y-%m-%d").replace(tzinfo=_REPORT_TZ)

        result = {}
        for i in range(7):
            date = (end_date - timedelta(days=i)).strftime("%Y-%m-%d")
            result[date] = self.get_commits(date)

        return result

    def get_month_commits(self, year: int, month: int) -> dict[str, GitStats]:
        """
        获取一个月内每天的提交统计

        Args:
            year: 年份
            month: 月份

        Returns:
            按日期分组的 GitStats 字典
        """
        import calendar

        _, days_in_month = calendar.monthrange(year, month)
        result = {}

        for day in range(1, days_in_month + 1):
            date = f"{year:04d}-{month:02d}-{day:02d}"
            result[date] = self.get_commits(date)

        return result

    def get_commits_for_pattern_analysis(self, days: int = 7) -> list[dict]:
        """
        获取近期提交数据，用于工作模式分析

        Args:
            days: 天数，默认 7 天

        Returns:
            包含日期、时间、提交信息的字典列表
        """
        end_date = datetime.now(_REPORT_TZ)
        result = []

        for i in range(days):
            check_date = (end_date - timedelta(days=i)).strftime("%Y-%m-%d")
            stats = self.get_commits(check_date)

            for commit in stats.commits:
                result.append({
                    "date": commit.date.strftime("%Y-%m-%d"),
                    "time": commit.date.strftime("%H:%M"),
                    "hour": commit.date.hour,
                    "message": commit.message,
                    "hash": commit.hash,
                })

        return result


def main():
    """测试入口"""
    import sys

    if len(sys.argv) < 2:
        print("Usage: python git_collector.py <repo_path> [date]")
        sys.exit(1)

    repo_path = sys.argv[1]
    date = sys.argv[2] if len(sys.argv) > 2 else None

    collector = GitCollector(repo_path)
    stats = collector.get_commits(date)

    print(f"Git 统计 ({date or '今天'}):")
    print(f"  提交次数: {stats.total_commits}")
    print(f"  修改文件: {stats.total_files_changed}")
    print(f"  新增行数: {stats.total_insertions}")
    print(f"  删除行数: {stats.total_deletions}")
    print(f"  净增行数: {stats.net_lines}")

    if stats.commits:
        print("\n提交记录:")
        for commit in stats.commits:
            print(f"  [{commit.hash}] {commit.message} ({commit.author})")


if __name__ == "__main__":
    main()
