# -*- coding: utf-8 -*-
"""
进阶版日报生成器 - 数据采集模块

包含：
- GitCollector: Git 提交记录采集
- EmailCollector: 邮件统计采集
- MemoryCollector: 记忆数据采集
- TodoCollector: 待办事项采集
- DataAggregator: 数据聚合器
"""

from .git_collector import GitCollector, GitCommit
from .email_collector import EmailCollector, EmailStats
from .memory_collector import MemoryCollector
from .todo_collector import TodoCollector
from .aggregator import DataAggregator, CollectedData

__all__ = [
    "GitCollector",
    "GitCommit",
    "EmailCollector",
    "EmailStats",
    "MemoryCollector",
    "TodoCollector",
    "DataAggregator",
    "CollectedData",
]
