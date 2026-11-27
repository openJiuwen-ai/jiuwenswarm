# -*- coding: utf-8 -*-
"""
工作分析引擎

功能：
- 效率指标计算
- 趋势对比分析
- 关键词提取
- 智能摘要生成
"""

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

# 与 run_report 等日报技能一致：默认日历日/分析时间使用东八区
_REPORT_TZ = ZoneInfo("Asia/Shanghai")

# 尝试导入分词库
try:
    import jieba
    import jieba.analyse

    JIEBA_AVAILABLE = True
except ImportError:
    JIEBA_AVAILABLE = False
    jieba = None


@dataclass
class EfficiencyMetrics:
    """效率指标"""

    # 任务指标
    task_completion_rate: float = 0.0  # 任务完成率
    tasks_completed: int = 0  # 已完成任务数
    tasks_total: int = 0  # 总任务数

    # Git 指标
    commit_count: int = 0  # 提交次数
    files_changed: int = 0  # 修改文件数
    lines_added: int = 0  # 新增行数
    lines_deleted: int = 0  # 删除行数
    net_lines: int = 0  # 净增行数

    # 沟通指标
    emails_received: int = 0  # 收到邮件
    emails_sent: int = 0  # 发送邮件

    # 综合指标
    productivity_score: float = 0.0  # 生产力得分 (0-100)
    focus_score: float = 0.0  # 专注度得分 (0-100)

    def to_dict(self) -> dict:
        return {
            "task_completion_rate": round(self.task_completion_rate, 2),
            "tasks_completed": self.tasks_completed,
            "tasks_total": self.tasks_total,
            "commit_count": self.commit_count,
            "files_changed": self.files_changed,
            "lines_added": self.lines_added,
            "lines_deleted": self.lines_deleted,
            "net_lines": self.net_lines,
            "emails_received": self.emails_received,
            "emails_sent": self.emails_sent,
            "productivity_score": round(self.productivity_score, 2),
            "focus_score": round(self.focus_score, 2),
        }


@dataclass
class TrendComparison:
    """趋势对比"""

    # 与昨日对比
    vs_yesterday: dict[str, Any] = field(default_factory=dict)

    # 与上周同期对比
    vs_last_week: dict[str, Any] = field(default_factory=dict)

    # 与上月同期对比
    vs_last_month: dict[str, Any] = field(default_factory=dict)

    # 周趋势
    weekly_trend: list[dict] = field(default_factory=list)

    # 月趋势
    monthly_trend: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "vs_yesterday": self.vs_yesterday,
            "vs_last_week": self.vs_last_week,
            "vs_last_month": self.vs_last_month,
            "weekly_trend": self.weekly_trend,
            "monthly_trend": self.monthly_trend,
        }


@dataclass
class AnalysisResult:
    """分析结果"""

    date: str
    analyzed_at: datetime

    # 效率指标
    metrics: EfficiencyMetrics = field(default_factory=EfficiencyMetrics)

    # 趋势对比
    trends: TrendComparison = field(default_factory=TrendComparison)

    # 关键词
    keywords: list[str] = field(default_factory=list)

    # 智能摘要
    summary: str = ""

    # 工作建议
    suggestions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "date": self.date,
            "analyzed_at": self.analyzed_at.isoformat(),
            "metrics": self.metrics.to_dict(),
            "trends": self.trends.to_dict(),
            "keywords": self.keywords,
            "summary": self.summary,
            "suggestions": self.suggestions,
        }


class WorkAnalyzer:
    """工作分析引擎"""

    # 停用词列表
    STOPWORDS = {
        "的", "了", "是", "在", "我", "有", "和", "就", "不", "人", "都", "一", "一个",
        "上", "也", "很", "到", "说", "要", "去", "你", "会", "着", "没有", "看", "好",
        "自己", "这", "那", "什么", "这个", "那个", "可以", "然后", "还是", "但是",
        "如果", "因为", "所以", "或者", "而且", "已经", "可能", "应该", "需要", "今天",
        "昨天", "明天", "进行", "完成", "工作", "任务", "今天", "功能", "代码", "文件",
    }

    def __init__(self):
        """初始化分析引擎"""
        # 初始化 jieba（如果可用）
        if JIEBA_AVAILABLE:
            jieba.initialize()

    def _extract_keywords_from_text(self, text: str, top_k: int = 10) -> list[str]:
        """从文本中提取关键词"""
        if not text:
            return []

        if JIEBA_AVAILABLE:
            # 使用 jieba 提取关键词
            keywords = jieba.analyse.extract_tags(text, topK=top_k * 2)
            # 过滤停用词
            keywords = [k for k in keywords if k not in self.STOPWORDS and len(k) > 1]
            return keywords[:top_k]
        else:
            # 简单的关键词提取（基于词频）
            words = re.findall(r"[\u4e00-\u9fa5]{2,}", text)
            word_freq = Counter(words)
            keywords = [
                w for w, _ in word_freq.most_common(top_k * 2)
                if w not in self.STOPWORDS
            ]
            return keywords[:top_k]

    def calculate_metrics(self, data: dict[str, Any]) -> EfficiencyMetrics:
        """
        计算效率指标

        Args:
            data: 采集的数据（来自 DataAggregator）

        Returns:
            EfficiencyMetrics: 效率指标
        """
        metrics = EfficiencyMetrics()

        # 任务指标
        todo_data = data.get("todo", {})
        metrics.tasks_total = todo_data.get("total", 0)
        metrics.tasks_completed = todo_data.get("completed", 0)
        if metrics.tasks_total > 0:
            metrics.task_completion_rate = metrics.tasks_completed / metrics.tasks_total

        # Git 指标
        git_data = data.get("git", {})
        metrics.commit_count = git_data.get("total_commits", 0)
        metrics.files_changed = git_data.get("total_files_changed", 0)
        metrics.lines_added = git_data.get("total_insertions", 0)
        metrics.lines_deleted = git_data.get("total_deletions", 0)
        metrics.net_lines = git_data.get("net_lines", 0)

        # 邮件指标
        email_data = data.get("email", {})
        metrics.emails_received = email_data.get("received_today", 0)
        metrics.emails_sent = email_data.get("sent_today", 0)

        # 计算综合指标
        metrics.productivity_score = self._calculate_productivity_score(metrics)
        metrics.focus_score = self._calculate_focus_score(metrics)

        return metrics

    @staticmethod
    def _calculate_productivity_score(metrics: EfficiencyMetrics) -> float:
        """计算生产力得分"""
        score = 0.0

        # 任务完成贡献（最高 40 分）
        score += metrics.task_completion_rate * 40

        # 代码贡献（最高 30 分）
        code_score = min(metrics.commit_count * 5, 15)  # 提交次数
        code_score += min(metrics.net_lines / 50, 15)  # 代码行数
        score += code_score

        # 沟通贡献（最高 20 分）
        communication_score = min(metrics.emails_sent * 2, 10)
        communication_score += min(metrics.emails_received / 5, 10)
        score += communication_score

        # 活跃度贡献（最高 10 分）
        if metrics.commit_count > 0 or metrics.tasks_completed > 0:
            score += 10

        return min(score, 100.0)
        
    @staticmethod
    def _calculate_focus_score(metrics: EfficiencyMetrics) -> float:
        """计算专注度得分"""
        score = 100.0

        # 任务未完成扣分
        pending_tasks = metrics.tasks_total - metrics.tasks_completed
        score -= min(pending_tasks * 5, 30)

        # 提交频繁度（适中最优）
        if metrics.commit_count > 0:
            if metrics.commit_count <= 5:
                score += 10  # 合理的提交频率
            elif metrics.commit_count > 10:
                score -= 5  # 过于碎片化

        # 邮件干扰扣分
        if metrics.emails_received > 20:
            score -= min((metrics.emails_received - 20) * 0.5, 20)

        # 指标约定为 0–100；起点 100 且 commit 合理时可 +10，需封顶
        return min(max(score, 0.0), 100.0)

    def generate_comparison(
        self,
        current_data: dict,
        historical_data: dict[str, dict],
    ) -> TrendComparison:
        """
        生成趋势对比

        Args:
            current_data: 当前数据
            historical_data: 历史数据 {date: data}

        Returns:
            TrendComparison: 趋势对比
        """
        trends = TrendComparison()
        current_date = datetime.strptime(current_data.get("date", ""), "%Y-%m-%d")

        # 与昨日对比
        yesterday = (current_date - timedelta(days=1)).strftime("%Y-%m-%d")
        if yesterday in historical_data:
            trends.vs_yesterday = self._compare_data(
                current_data,
                historical_data[yesterday],
            )

        # 与上周同期对比
        last_week = (current_date - timedelta(days=7)).strftime("%Y-%m-%d")
        if last_week in historical_data:
            trends.vs_last_week = self._compare_data(
                current_data,
                historical_data[last_week],
            )

        # 生成周趋势
        trends.weekly_trend = self._generate_weekly_trend(current_date, historical_data)

        return trends

    def _compare_data(self, current: dict, previous: dict) -> dict:
        """对比两天的数据"""
        current_metrics = self.calculate_metrics(current)
        previous_metrics = self.calculate_metrics(previous)

        return {
            "commits": {
                "current": current_metrics.commit_count,
                "previous": previous_metrics.commit_count,
                "change": current_metrics.commit_count - previous_metrics.commit_count,
            },
            "tasks_completed": {
                "current": current_metrics.tasks_completed,
                "previous": previous_metrics.tasks_completed,
                "change": current_metrics.tasks_completed - previous_metrics.tasks_completed,
            },
            "productivity_score": {
                "current": current_metrics.productivity_score,
                "previous": previous_metrics.productivity_score,
                "change": round(
                    current_metrics.productivity_score - previous_metrics.productivity_score, 2
                ),
            },
            "net_lines": {
                "current": current_metrics.net_lines,
                "previous": previous_metrics.net_lines,
                "change": current_metrics.net_lines - previous_metrics.net_lines,
            },
        }

    def _generate_weekly_trend(self, end_date: datetime, historical_data: dict) -> list[dict]:
        """生成周趋势数据"""
        trend = []
        for i in range(6, -1, -1):
            date = (end_date - timedelta(days=i)).strftime("%Y-%m-%d")
            if date in historical_data:
                metrics = self.calculate_metrics(historical_data[date])
                trend.append({
                    "date": date,
                    "commits": metrics.commit_count,
                    "tasks_completed": metrics.tasks_completed,
                    "productivity_score": metrics.productivity_score,
                })
        return trend

    def analyze(
        self,
        data: dict[str, Any],
        historical_data: Optional[dict[str, dict]] = None,
    ) -> AnalysisResult:
        """
        执行完整分析

        Args:
            data: 当前采集的数据
            historical_data: 历史数据（用于趋势对比）

        Returns:
            AnalysisResult: 分析结果
        """
        result = AnalysisResult(
            date=data.get("date", datetime.now(_REPORT_TZ).strftime("%Y-%m-%d")),
            analyzed_at=datetime.now(_REPORT_TZ),
        )

        # 计算效率指标
        result.metrics = self.calculate_metrics(data)

        # 生成趋势对比
        if historical_data:
            result.trends = self.generate_comparison(data, historical_data)

        # 提取关键词
        all_text = ""
        memory_data = data.get("memory", {})
        all_text += memory_data.get("today_content", "") + " "
        all_text += " ".join(memory_data.get("work_summaries", []))

        # 添加 Git 提交信息
        git_data = data.get("git", {})
        for commit in git_data.get("commits", []):
            all_text += commit.get("message", "") + " "

        result.keywords = self._extract_keywords_from_text(all_text, 8)

        # 生成智能摘要
        result.summary = self._generate_summary(data, result.metrics)

        # 生成工作建议
        result.suggestions = self._generate_suggestions(result.metrics, result.trends)

        return result

    @staticmethod
    def _generate_summary(data: dict, metrics: EfficiencyMetrics) -> str:
        """生成工作摘要"""
        parts = []

        # Git 工作
        if metrics.commit_count > 0:
            parts.append(f"完成 {metrics.commit_count} 次代码提交")
            if metrics.net_lines > 0:
                parts.append(f"新增 {metrics.net_lines} 行代码")
            elif metrics.net_lines < 0:
                parts.append(f"优化了 {abs(metrics.net_lines)} 行代码")

        # 任务完成
        if metrics.tasks_completed > 0:
            rate = metrics.task_completion_rate * 100
            parts.append(f"完成 {metrics.tasks_completed}/{metrics.tasks_total} 个任务（{rate:.0f}%）")

        # 邮件沟通
        if metrics.emails_sent > 0 or metrics.emails_received > 0:
            parts.append(f"处理邮件 {metrics.emails_received} 封，发送 {metrics.emails_sent} 封")

        if not parts:
            return "今日暂无工作记录"

        return "，".join(parts) + "。"

    @staticmethod
    def _generate_suggestions(
        metrics: EfficiencyMetrics, trends: TrendComparison
    ) -> list[str]:
        """生成工作建议"""
        suggestions = []

        # 基于任务完成率
        if metrics.task_completion_rate < 0.5:
            suggestions.append("建议集中精力完成待办任务，提高任务完成率")
        elif metrics.task_completion_rate > 0.9:
            suggestions.append("任务完成率很高，可以考虑挑战更有难度的任务")

        # 基于代码提交
        if metrics.commit_count == 0:
            suggestions.append("今日暂无代码提交，建议及时提交工作成果")
        elif metrics.commit_count > 10:
            suggestions.append("提交较为频繁，建议合理规划提交粒度")

        # 基于趋势对比
        if trends.vs_yesterday:
            productivity_change = trends.vs_yesterday.get("productivity_score", {}).get("change", 0)
            if productivity_change < -10:
                suggestions.append("今日效率较昨日有所下降，注意调整状态")
            elif productivity_change > 10:
                suggestions.append("今日效率提升明显，保持良好状态")

        # 基于专注度
        if metrics.focus_score < 60:
            suggestions.append("专注度较低，建议减少干扰，集中处理重要任务")

        return suggestions[:3]  # 最多返回 3 条建议


def main():
    """测试入口"""
    import sys

    # 创建测试数据
    test_data = {
        "date": datetime.now(_REPORT_TZ).strftime("%Y-%m-%d"),
        "git": {
            "total_commits": 5,
            "total_files_changed": 12,
            "total_insertions": 350,
            "total_deletions": 80,
            "net_lines": 270,
            "commits": [
                {"message": "feat: 添加日报生成功能"},
                {"message": "fix: 修复邮件采集bug"},
            ],
        },
        "todo": {
            "total": 8,
            "completed": 5,
            "running": 2,
            "waiting": 1,
        },
        "email": {
            "received_today": 15,
            "sent_today": 3,
        },
        "memory": {
            "today_content": "今天完成了日报生成器的开发，包括数据采集、工作分析等模块",
            "work_summaries": ["完成技能文件编写", "创建辅助脚本"],
        },
    }

    analyzer = WorkAnalyzer()
    result = analyzer.analyze(test_data)

    print(f"=== 工作分析报告 ({result.date}) ===\n")

    print("效率指标:")
    print(f"  任务完成率: {result.metrics.task_completion_rate:.1%}")
    print(f"  提交次数: {result.metrics.commit_count}")
    print(f"  生产力得分: {result.metrics.productivity_score:.1f}")
    print(f"  专注度得分: {result.metrics.focus_score:.1f}")

    print(f"\n关键词: {', '.join(result.keywords)}")

    print(f"\n工作摘要: {result.summary}")

    if result.suggestions:
        print("\n工作建议:")
        for i, s in enumerate(result.suggestions, 1):
            print(f"  {i}. {s}")


if __name__ == "__main__":
    main()
