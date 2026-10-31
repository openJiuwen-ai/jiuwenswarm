# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""AgentSSAS 模块存储管理器。

为每个检测模块提供独立的存储管理。每个模块获得自己的子目录,
包含 process.db、result.db 和 config/ 目录。遵循 jiuwenswarm 的
JIUWENSWARM_HOME 环境变量约定。
"""

from __future__ import annotations

import os
from pathlib import Path

from agent_ssas.core.framework.storage.sqlite_store import SQLiteStore


class ModuleStorageManager:
    """为每个检测模块提供独立的存储管理。

    每个模块获得自己的子目录,包含 process.db、result.db 和 config/ 目录。
    遵循 jiuwenswarm 的 JIUWENSWARM_HOME 环境变量约定。

    存储目录结构:
        ${ssas_home}/ssas/modules/<module_name>/
        ├── process.db    # 过程数据(建模数据、中间结果)
        ├── result.db     # 结果数据(告警、审计)
        └── config/       # 配置数据(规则、基线)

    过程与结果分库,便于不同 TTL 和读写特征独立清理互不影响。
    """

    def __init__(self, module_name: str, ssas_home: Path | None = None):
        """初始化模块存储管理器。

        创建模块子目录(若不存在),初始化 process_store 和 result_store,
        并创建 config 目录。

        Args:
            module_name: 检测模块名称,用作子目录名。不能为空字符串。
            ssas_home: 存储根目录。为 None 时按
                SSAS_HOME -> JIUWENSWARM_DATA_DIR -> JIUWENSWARM_HOME -> ~/.jiuwenswarm
                顺序解析。

        Raises:
            ValueError: module_name 为空字符串时。
        """
        if not module_name:
            raise ValueError("module_name 不能为空字符串")

        self._module_name = module_name
        if ssas_home is None:
            ssas_home = self._resolve_ssas_home()
        # 模块目录结构:ssas_home / "ssas" / "modules" / module_name
        self._module_dir = ssas_home / "ssas" / "modules" / module_name
        self._module_dir.mkdir(parents=True, exist_ok=True)

        # 过程库:建模数据、中间结果(TTL 30 天)
        self._process_store = SQLiteStore(self._module_dir / "process.db")
        # 结果库:告警、审计、威胁分析报告(TTL 90 天)
        self._result_store = SQLiteStore(self._module_dir / "result.db")
        # 配置目录:检测规则、基线配置、阈值参数(手动更新)
        self._config_dir = self._module_dir / "config"
        self._config_dir.mkdir(exist_ok=True)

    @staticmethod
    def _resolve_ssas_home() -> Path:
        """解析存储根目录。

        按 SSAS_HOME -> JIUWENSWARM_DATA_DIR -> JIUWENSWARM_HOME -> ~/.jiuwenswarm
        顺序解析,返回第一个非空环境变量对应的路径,否则回退到用户主目录。

        Returns:
            存储根目录 Path 对象。
        """
        home = os.environ.get("SSAS_HOME")
        if home:
            return Path(home)
        home = os.environ.get("JIUWENSWARM_DATA_DIR")
        if home:
            return Path(home)
        home = os.environ.get("JIUWENSWARM_HOME")
        if home:
            return Path(home)
        return Path.home() / ".jiuwenswarm"

    @property
    def process_store(self) -> SQLiteStore:
        """过程数据存储(建模数据、中间结果)。"""
        return self._process_store

    @property
    def result_store(self) -> SQLiteStore:
        """结果数据存储(告警、审计、威胁分析报告)。"""
        return self._result_store

    @property
    def config_dir(self) -> Path:
        """配置数据目录(规则、基线、阈值参数)。"""
        return self._config_dir

    async def cleanup(self, event_ttl_days: int, alert_ttl_days: int) -> None:
        """按 TTL 清理过程库与结果库的过期数据。

        process.db 三表按 event_ttl_days(过程数据 TTL,默认 30 天),
        result.db 三表按 alert_ttl_days(结果数据 TTL,默认 90 天)清理。
        TTL 天数 <= 0 时跳过对应库的清理(禁用语义)。

        Args:
            event_ttl_days: 过程数据保留天数。
            alert_ttl_days: 结果数据保留天数。
        """
        if event_ttl_days > 0:
            for table in ("events", "alerts", "raw_events"):
                await self._process_store.cleanup_expired(table, event_ttl_days)
        if alert_ttl_days > 0:
            for table in ("events", "alerts", "raw_events"):
                await self._result_store.cleanup_expired(table, alert_ttl_days)

    async def close(self) -> None:
        """关闭所有存储连接。

        依次关闭 process_store 和 result_store。供模块卸载或系统关闭时调用。
        """
        await self._process_store.close()
        await self._result_store.close()
