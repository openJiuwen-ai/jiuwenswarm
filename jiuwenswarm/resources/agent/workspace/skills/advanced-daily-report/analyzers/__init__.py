# -*- coding: utf-8 -*-
"""
进阶版日报生成器 - 分析模块

包含：
- WorkAnalyzer: 工作分析引擎
- EfficiencyMetrics: 效率指标
- TrendComparison: 趋势对比
- AnalysisResult: 分析结果
- AIAnalyzer: AI 智能分析器
- AIAnalysisResult: AI 分析结果
- WorkPatternResult: 工作模式分析结果
"""

from .work_analyzer import (
    WorkAnalyzer,
    EfficiencyMetrics,
    TrendComparison,
    AnalysisResult,
)
from .ai_analyzer import (
    AIAnalyzer,
    AIAnalysisResult,
    WorkPatternResult,
)

__all__ = [
    "WorkAnalyzer",
    "EfficiencyMetrics",
    "TrendComparison",
    "AnalysisResult",
    "AIAnalyzer",
    "AIAnalysisResult",
    "WorkPatternResult",
]
