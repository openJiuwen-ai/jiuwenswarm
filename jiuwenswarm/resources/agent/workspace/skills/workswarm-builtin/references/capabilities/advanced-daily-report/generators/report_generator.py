# -*- coding: utf-8 -*-
"""
报告生成器

支持：
- 日报生成
- 周报生成（聚合一周数据）
- 月报生成（聚合一月数据）
- AI 智能分析（智能摘要、明日计划建议、工作模式分析）
"""

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

_REPORT_TZ = ZoneInfo("Asia/Shanghai")

from ..analyzers.work_analyzer import AnalysisResult, WorkAnalyzer
from ..analyzers.ai_analyzer import AIAnalyzer, AIAnalysisResult
from ..collectors.aggregator import CollectedData, DataAggregator


@dataclass
class ReportConfig:
    """报告配置"""

    report_type: str = "daily"  # daily, weekly, monthly
    date: str = ""  # 报告日期
    include_trends: bool = True  # 是否包含趋势
    include_suggestions: bool = True  # 是否包含建议
    output_format: str = "markdown"  # markdown, json
    # AI 分析配置
    enable_ai_analysis: bool = False  # 是否启用 AI 分析
    ai_auto_mode: bool = True  # AI 分析模式：True=自动，False=手动触发


class ReportGenerator:
    """报告生成器"""

    def __init__(
        self,
        data_aggregator: DataAggregator,
        work_analyzer: Optional[WorkAnalyzer] = None,
        ai_analyzer: Optional[AIAnalyzer] = None,
    ):
        """
        初始化报告生成器

        Args:
            data_aggregator: 数据聚合器
            work_analyzer: 工作分析器（可选）
            ai_analyzer: AI 分析器（可选）
        """
        self.data_aggregator = data_aggregator
        self.work_analyzer = work_analyzer or WorkAnalyzer()
        self.ai_analyzer = ai_analyzer

    def generate_daily(self, date: Optional[str] = None, config: Optional[ReportConfig] = None) -> str:
        """
        生成日报

        Args:
            date: 日期，默认今天
            config: 报告配置

        Returns:
            str: Markdown 格式的日报
        """
        if date is None:
            date = datetime.now(_REPORT_TZ).strftime("%Y-%m-%d")

        if config is None:
            config = ReportConfig(report_type="daily", date=date)

        # 采集数据
        data = self.data_aggregator.collect(date, include_comparison=config.include_trends)

        # 分析数据
        analysis = self.work_analyzer.analyze(data.to_dict())

        # AI 分析（如果启用）
        ai_result = None
        if config.enable_ai_analysis:
            ai_result = self._run_ai_analysis(data, config)

        # 生成报告
        return self._render_daily_report(data, analysis, config, ai_result)

    def _run_ai_analysis(self, data: CollectedData, config: ReportConfig) -> Optional[AIAnalysisResult]:
        """运行 AI 分析"""
        if self.ai_analyzer is None:
            try:
                self.ai_analyzer = AIAnalyzer()
            except Exception as e:
                logging.info(f"[ReportGenerator] AI 分析器初始化失败: {e}")
                return None

        try:
            # 采集工作模式分析数据
            pattern_data = self.data_aggregator.collect_for_pattern_analysis(days=7)

            # 运行完整分析
            return asyncio.run(self.ai_analyzer.analyze_full(data.to_dict(), pattern_data))
        except Exception as e:
            logging.info(f"[ReportGenerator] AI 分析失败: {e}")
            return None

    def generate_weekly(self, end_date: Optional[str] = None) -> str:
        """
        生成周报

        Args:
            end_date: 结束日期，默认今天

        Returns:
            str: Markdown 格式的周报
        """
        if end_date is None:
            end_date = datetime.now(_REPORT_TZ)
        else:
            end_date = datetime.strptime(end_date, "%Y-%m-%d").replace(tzinfo=_REPORT_TZ)

        # 计算本周日期范围
        start_date = end_date - timedelta(days=6)
        start_str = start_date.strftime("%Y-%m-%d")
        end_str = end_date.strftime("%Y-%m-%d")

        # 采集一周数据
        week_data = self.data_aggregator.collect_week(end_str)

        # 聚合周数据
        aggregated = self._aggregate_week_data(week_data)

        # 生成周报
        return self._render_weekly_report(aggregated, start_str, end_str)

    def generate_monthly(self, year: Optional[int] = None, month: Optional[int] = None) -> str:
        """
        生成月报

        Args:
            year: 年份，默认当前年
            month: 月份，默认当前月

        Returns:
            str: Markdown 格式的月报
        """
        now = datetime.now(_REPORT_TZ)
        if year is None:
            year = now.year
        if month is None:
            month = now.month

        # 采集一月数据
        month_data = self.data_aggregator.collect_month(year, month)

        # 聚合月数据
        aggregated = self._aggregate_month_data(month_data)

        # 生成月报
        return self._render_monthly_report(aggregated, year, month)

    @staticmethod
    def _aggregate_week_data(week_data: dict[str, CollectedData]) -> dict:
        """聚合一周数据"""
        aggregated = {
            "total_commits": 0,
            "total_files_changed": 0,
            "total_insertions": 0,
            "total_deletions": 0,
            "total_tasks_completed": 0,
            "total_tasks": 0,
            "total_emails_received": 0,
            "total_emails_sent": 0,
            "active_days": 0,
            "daily_data": [],
        }

        for date, data in week_data.items():
            day_summary = {
                "date": date,
                "commits": data.git.total_commits,
                "tasks_completed": data.todo.completed,
                "productivity": 0,
            }

            aggregated["total_commits"] += data.git.total_commits
            aggregated["total_files_changed"] += data.git.total_files_changed
            aggregated["total_insertions"] += data.git.total_insertions
            aggregated["total_deletions"] += data.git.total_deletions
            aggregated["total_tasks_completed"] += data.todo.completed
            aggregated["total_tasks"] += data.todo.total
            aggregated["total_emails_received"] += data.email.received_today
            aggregated["total_emails_sent"] += data.email.sent_today

            if data.git.total_commits > 0 or data.todo.completed > 0:
                aggregated["active_days"] += 1

            aggregated["daily_data"].append(day_summary)

        return aggregated

    def _aggregate_month_data(self, month_data: dict[str, CollectedData]) -> dict:
        """聚合一月数据"""
        aggregated = self._aggregate_week_data(month_data)
        aggregated["total_days"] = len(month_data)
        return aggregated

    @staticmethod
    def _render_daily_report(
         data: CollectedData, analysis: AnalysisResult, config: ReportConfig,
        ai_result: Optional[AIAnalysisResult] = None
    ) -> str:
        """渲染日报"""
        lines = [
            f"# 📋 工作日报 - {data.date}",
            "",
        ]

        # AI 智能摘要（放在开头）
        if ai_result and ai_result.summary:
            lines.extend([
                "## 🤖 AI 智能摘要",
                "",
                f"> {ai_result.summary}",
                "",
            ])

        # 效率概览
        lines.extend([
            "## 📊 今日概览",
            "",
            "| 指标 | 数值 |",
            "|------|------|",
            f"| 提交次数 | {analysis.metrics.commit_count} |",
            f"| 任务完成 | {analysis.metrics.tasks_completed}/{analysis.metrics.tasks_total} |",
            f"| 代码变更 | +{analysis.metrics.lines_added}/-{analysis.metrics.lines_deleted} |",
            f"| 邮件处理 | 收 {analysis.metrics.emails_received} / 发 {analysis.metrics.emails_sent} |",
            f"| 生产力得分 | {analysis.metrics.productivity_score:.1f} |",
            "",
        ])

        # 已完成任务
        completed_tasks = [t for t in data.todo.tasks if t.status == "completed"]
        if completed_tasks:
            lines.extend([
                "## ✅ 已完成任务",
                "",
            ])
            for task in completed_tasks[:10]:
                lines.append(f"- {task.content}")
            lines.append("")

        # 进行中任务
        running_tasks = [t for t in data.todo.tasks if t.status == "running"]
        if running_tasks:
            lines.extend([
                "## 🔄 进行中任务",
                "",
            ])
            for task in running_tasks[:5]:
                lines.append(f"- {task.content}")
            lines.append("")

        # Git 提交记录
        if data.git.commits:
            lines.extend([
                "## 💻 代码提交",
                "",
                "| 时间 | 提交信息 | 变更 |",
                "|------|----------|------|",
            ])
            for commit in data.git.commits[:10]:
                time_str = commit.date.strftime("%H:%M") if commit.date else "-"
                lines.append(
                    f"| {time_str} | {commit.message[:40]} | "
                    f"+{commit.insertions}/-{commit.deletions} |"
                )
            lines.append("")

        # 今日工作记录
        if data.memory.work_summaries:
            lines.extend([
                "## 📝 今日工作记录",
                "",
            ])
            for summary in data.memory.work_summaries[:10]:
                lines.append(f"- {summary}")
            lines.append("")

        # 邮件概况
        if data.email.received_today > 0 or data.email.sent_today > 0:
            lines.extend([
                "## 📧 邮件概况",
                "",
                f"- 今日收件: {data.email.received_today} 封",
                f"- 今日发件: {data.email.sent_today} 封",
                f"- 未读邮件: {data.email.unread} 封",
                "",
            ])

            # 未读邮件
            if data.email.important_emails:
                lines.append("### 未读邮件")
                lines.append("")
                for email in data.email.important_emails[:5]:
                    lines.append(f"- [{email.sender}] {email.subject}")
                lines.append("")

        # 趋势对比
        if config.include_trends and analysis.trends.vs_yesterday:
            lines.extend([
                "## 📈 趋势对比",
                "",
            ])
            vs_y = analysis.trends.vs_yesterday
            if "commits" in vs_y:
                change = vs_y["commits"]["change"]
                symbol = "↑" if change > 0 else "↓" if change < 0 else "→"
                lines.append(f"- 提交: {symbol} {abs(change)} 次")
            if "productivity_score" in vs_y:
                change = vs_y["productivity_score"]["change"]
                symbol = "↑" if change > 0 else "↓" if change < 0 else "→"
                lines.append(f"- 效率: {symbol} {abs(change):.1f} 分")
            lines.append("")

        # 工作建议与明日计划
        lines.extend([
            "## 💡 工作建议与明日计划",
            "",
        ])

        # 原有建议
        if config.include_suggestions and analysis.suggestions:
            lines.append("### 今日改进建议")
            lines.append("")
            for i, suggestion in enumerate(analysis.suggestions, 1):
                lines.append(f"{i}. {suggestion}")
            lines.append("")

        # AI 明日计划建议
        if ai_result and ai_result.tomorrow_suggestions:
            lines.append("### 🔜 AI 明日计划建议")
            lines.append("")
            for suggestion in ai_result.tomorrow_suggestions:
                lines.append(f"- {suggestion}")
            lines.append("")

        # 待办任务
        waiting_tasks = [t for t in data.todo.tasks if t.status == "waiting"]
        if waiting_tasks:
            lines.append("### 📋 待办任务")
            lines.append("")
            for task in waiting_tasks[:5]:
                lines.append(f"- {task.content}")
            lines.append("")

        # 工作模式分析
        if ai_result and ai_result.work_pattern and ai_result.work_pattern.get("description"):
            lines.extend([
                "## 📊 工作模式分析（近7天）",
                "",
                ai_result.work_pattern.get("description", ""),
                "",
            ])

            # 高峰时段
            peak_hours = ai_result.work_pattern.get("peak_hours", [])
            if peak_hours:
                lines.append(f"- **效率高峰时段**: {', '.join([f'{h}:00' for h in peak_hours])}")

            # 平均提交
            avg_commits = ai_result.work_pattern.get("avg_commits_per_day", 0)
            if avg_commits > 0:
                lines.append(f"- **平均每日提交**: {avg_commits:.1f} 次")

            lines.append("")

        return "\n".join(lines)

    @staticmethod
    def _render_weekly_report(data: dict, start_date: str, end_date: str) -> str:
        """渲染周报"""
        lines = [
            f"# 📋 工作周报 - {start_date} ~ {end_date}",
            "",
        ]

        # 本周概览
        lines.extend([
            "## 📊 本周概览",
            "",
            "| 指标 | 数值 |",
            "|------|------|",
            f"| 活跃天数 | {data['active_days']}/7 天 |",
            f"| 提交次数 | {data['total_commits']} 次 |",
            f"| 任务完成 | {data['total_tasks_completed']} 个 |",
            f"| 代码变更 | +{data['total_insertions']}/-{data['total_deletions']} |",
            f"| 邮件处理 | 收 {data['total_emails_received']} / 发 {data['total_emails_sent']} |",
            "",
        ])

        # 每日数据
        if data["daily_data"]:
            lines.extend([
                "## 📅 每日统计",
                "",
                "| 日期 | 提交 | 任务完成 |",
                "|------|------|----------|",
            ])
            for day in data["daily_data"]:
                lines.append(f"| {day['date']} | {day['commits']} | {day['tasks_completed']} |")
            lines.append("")

        # 本周亮点
        lines.extend([
            "## ⭐ 本周亮点",
            "",
            "- 本周完成多次代码提交",
            "- 保持了稳定的工作节奏",
            "",
        ])

        # 下周计划
        lines.extend([
            "## 🔜 下周计划",
            "",
            "- 继续完善当前功能",
            "- 处理待办事项",
            "",
        ])

        return "\n".join(lines)

    @staticmethod
    def _render_monthly_report(data: dict, year: int, month: int) -> str:
        """渲染月报"""
        lines = [
            f"# 📋 工作月报 - {year}年{month}月",
            "",
        ]

        # 本月概览
        lines.extend([
            "## 📊 本月概览",
            "",
            "| 指标 | 数值 |",
            "|------|------|",
            f"| 活跃天数 | {data['active_days']}/{data['total_days']} 天 |",
            f"| 提交次数 | {data['total_commits']} 次 |",
            f"| 任务完成 | {data['total_tasks_completed']} 个 |",
            f"| 代码变更 | +{data['total_insertions']}/-{data['total_deletions']} |",
            "",
        ])

        # 工作总结
        lines.extend([
            "## 📝 工作总结",
            "",
            "本月工作主要包括：",
            "- 日报生成器功能开发",
            "- 进阶版数据采集模块",
            "- 多报告类型支持",
            "",
        ])

        # 下月计划
        lines.extend([
            "## 🔜 下月计划",
            "",
            "- 继续优化报告生成功能",
            "- 添加更多数据源支持",
            "",
        ])

        return "\n".join(lines)


def main():
    """测试入口"""
    import sys

    if len(sys.argv) < 2:
        logging.info("Usage: python report_generator.py <workspace_dir> [git_repo]")
        sys.exit(1)

    workspace_dir = sys.argv[1]
    git_repo = sys.argv[2] if len(sys.argv) > 2 else None

    # 初始化
    aggregator = DataAggregator(workspace_dir=workspace_dir, git_repo=git_repo)
    generator = ReportGenerator(aggregator)

    # 生成日报
    logging.info("=== 日报 ===\n")
    daily_report = generator.generate_daily()
    logging.info(daily_report)


if __name__ == "__main__":
    main()
