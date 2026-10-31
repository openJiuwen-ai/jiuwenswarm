# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""AgentSSAS 存储模块。

持久化 raw_event、统一事件、告警记录、建模数据、分析结果、呈现输出。
存储后端支持 SQLite(0.1 版本)和文本形式(呈现输出)。
遵循 jiuwenswarm 的 JIUWENSWARM_HOME 环境变量约定。
"""

from agent_ssas.core.framework.storage.interfaces import StoragePlugin
from agent_ssas.core.framework.storage.memory_store import MemoryStore
from agent_ssas.core.framework.storage.module_store import ModuleStorageManager
from agent_ssas.core.framework.storage.sqlite_store import SQLiteStore

__all__ = [
    "MemoryStore",
    "ModuleStorageManager",
    "SQLiteStore",
    "StoragePlugin",
]
