# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""AgentSSAS 进程内模式接入适配插件。

AgentSSASBackend 直接实现 AgentSSASBackendProtocol,内部持有数据预处理模块、
流水线模块、存储模块、检测模块管理器,零网络延迟。数据建模、威胁分析、
呈现等模块由 ThreatAnalysisPipeline 统一调度;有风险的报告由流水线
统一写入主库 alerts 表(notify/auth 模式均覆盖)。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from agent_ssas.core.framework.analysis_pipeline.pipeline import ThreatAnalysisPipeline
from agent_ssas.core.framework.config.settings import AgentSSASConfig
from agent_ssas.core.framework.core_types.assessment import RiskAssessment, RiskLevel
from agent_ssas.core.framework.data_preprocessor.preprocessor import DataPreprocessor
from agent_ssas.core.framework.module_manager.manager import DetectionModuleManager
from agent_ssas.core.framework.storage.sqlite_store import SQLiteStore

logger = logging.getLogger(__name__)

# 每累计该次数的事件上报后触发一次后台 TTL 清理
_TTL_CLEANUP_INTERVAL = 100


class AgentSSASBackend:
    """进程内模式接入适配插件。

    直接持有引擎各模块实例,零网络延迟。report_event() 将事件写入存储、
    送入预处理解析,再由流水线模块协调建模+分析+聚合,返回 RiskAssessment。
    有风险的报告由流水线统一写入主库 alerts 表(notify/auth 均覆盖)。

    注意:agent-ssas 包不依赖 openjiuwen(agent-core),不导入
    SecurityAllow/SecurityReject/SecurityAlert 等类型。RiskAssessment ->
    SecurityDecision 的映射逻辑在 AgentSSASSecurityRail 侧实现(见文档 02)。
    """

    def __init__(self, config: AgentSSASConfig) -> None:
        """初始化进程内模式接入适配插件。

        构造引擎各模块实例(存储、预处理、检测模块管理器、流水线),
        但不触发检测模块加载。实际加载由 initialize() 触发。

        Args:
            config: AgentSSAS 子系统配置,注入到各内部模块。
        """
        self._config = config
        # 核心库文件路径:<storage_path>/ssas_core.db(见文档 12.2 节存储布局)
        from pathlib import Path

        storage_dir = Path(config.storage_path)
        storage_dir.mkdir(parents=True, exist_ok=True)
        self._storage = SQLiteStore(storage_dir / "ssas_core.db")
        self._preprocessor = DataPreprocessor(config)
        self._module_manager = DetectionModuleManager(config)
        # 流水线持有主库引用,notify/auth 模式的有风险报告统一写入 alerts 表
        self._pipeline = ThreatAnalysisPipeline(
            config, self._module_manager, storage=self._storage
        )
        # 初始化状态(幂等):initialize() 完成前 report_event 自动等待
        self._initialized = False
        self._init_lock = asyncio.Lock()
        self._init_task: asyncio.Task[None] | None = None
        # 事件上报计数,达到阈值触发后台 TTL 清理
        self._report_count = 0

    async def close(self) -> None:
        """关闭数据库连接,释放资源。

        在 Windows 上,SQLite WAL 模式会锁定 -wal/-shm 文件句柄,
        若不显式关闭连接,后续清理目录会因文件锁定失败。
        调用方应在不再使用 backend 时调用此方法(如测试结束、进程退出前)。
        """
        try:
            await self._storage.close()
        except Exception:
            logger.exception("关闭 SQLite 连接异常,忽略")
        try:
            await self._module_manager.close()
        except Exception:
            logger.exception("关闭检测模块存储异常,忽略")
        self._initialized = False
        self._init_task = None

    async def initialize(self) -> None:
        """初始化,加载所有检测模块(幂等)。

        触发 DetectionModuleManager.initialize() 扫描、加载、注册所有
        检测模块(见文档 4.3 节实现要点 4)。幂等设计:并发或重复调用
        只执行一次实际加载,其余调用等待同一初始化完成。
        随后执行一次启动 TTL 清理(失败不影响初始化)。
        """
        if self._initialized:
            return
        async with self._init_lock:
            if self._initialized:
                return
            await self._module_manager.initialize()
            self._initialized = True
            logger.info("AgentSSASBackend 初始化完成")
            try:
                await self._cleanup_storages()
            except Exception:
                logger.exception("启动 TTL 清理失败,忽略")

    async def _ensure_initialized(self) -> None:
        """确保初始化已完成,未完成时等待或触发。

        jiuwenswarm 集成层(patch)在同步上下文中以 fire-and-forget 方式
        调用 initialize(),初始化未完成前到达的事件可能因订阅表为空而
        漏检测。本方法在 report_event 入口调用,保证首个事件处理前
        模块加载必然完成(fire-and-forget 的初始化任务因此无害)。
        """
        if self._initialized:
            return
        if self._init_task is None or self._init_task.done():
            # 无在途初始化任务(或已完成但未成功),自行触发并等待
            await self.initialize()
            return
        # 等待在途的 fire-and-forget 初始化任务完成
        await self._init_task

    async def report_event(self, raw_event: dict) -> RiskAssessment:
        """上报事件,存储 + 预处理解析 + 流水线协调聚合 + 返回 RiskAssessment。

        单一接口处理所有事件类型。安全检测事件(如
        event_type="permission_interrupt_tool"、event_class="security")
        在分析后会推送实时告警。

        处理流程(见文档 3.2 节):
        1. 确保初始化已完成(未完成时自动等待,防 fire-and-forget 竞态)
        2. 持久化 raw_event 到存储模块(record_raw_event)
        3. 解析 raw_event -> UnifiedEvent 列表(DataPreprocessor.parse)
           (含基础事件 + 聚合事件,如 tool_output 触发 one_toolcall_event)
        4. 每个事件送入流水线执行 -> RiskAssessment(ThreatAnalysisPipeline.run)
           (有风险的报告由流水线统一写入主库 alerts 表)
        5. 聚合所有事件的 RiskAssessment(取最高风险等级)

        异常时 fail-open:返回无风险 RiskAssessment(risk_level=Safe),
        不阻断业务。

        Args:
            raw_event: 原始事件 dict(三层结构:common / payload / metadata)。

        Returns:
            聚合后的 RiskAssessment。自身运行故障时返回无风险结果。
        """
        # 防竞态:初始化未完成时等待,避免订阅表为空导致漏检测
        try:
            await self._ensure_initialized()
        except Exception:
            logger.exception("等待初始化异常,fail-open 继续处理")
            if not self._initialized:
                # 初始化彻底失败时仍 fail-open 放行(不阻断业务)
                return RiskAssessment()

        # 持久化 raw_event 到存储(失败不阻断后续流程,记录异常后继续)
        try:
            await self._storage.record_raw_event(raw_event)
        except Exception:
            logger.exception("持久化 raw_event 失败,继续后续流程")

        # DataPreprocessor 负责解析 raw_event -> UnifiedEvent 列表
        # (含基础事件 + 聚合事件)
        # AnalysisPipeline 负责协调检测模块、收集报告、聚合为 RiskAssessment
        try:
            events = await self._preprocessor.parse(raw_event)
            assessments: list[RiskAssessment] = []
            for unified in events:
                # 持久化 UnifiedEvent 到主库 events 表
                try:
                    event_dict = unified.to_event_desc()
                    event_dict["event_id"] = unified.event_id
                    event_dict["event_type"] = unified.event_node.event_type
                    event_dict["event_class"] = unified.event_node.event_class
                    event_dict["interaction_seq"] = unified.event_node.interaction_seq
                    event_dict["session_id"] = unified.event_node.session_id
                    event_dict["agent_id"] = unified.event_node.agent_id
                    event_dict["trace_id"] = unified.trace.trace_id
                    event_dict["timestamp"] = unified.event_node.timestamp
                    await self._storage.record_event(event_dict)
                except Exception:
                    logger.exception("持久化 UnifiedEvent 失败,继续后续流程")

                # 告警写入已移至 ThreatAnalysisPipeline(统一覆盖 notify/auth 模式)
                assessment = await self._pipeline.run(unified)
                assessments.append(assessment)

            # 聚合所有事件的 RiskAssessment(取最高风险等级)
            return self._merge_assessments(assessments)
        except Exception:
            # fail-open:AgentSSAS 自身运行故障时返回无风险 RiskAssessment,不阻断业务
            logger.exception(
                "report_event 执行异常,fail-open 返回无风险结果"
            )
            return RiskAssessment()
        finally:
            # 累计上报达到阈值时,后台触发一次 TTL 清理(不阻塞上报路径)
            self._report_count += 1
            if self._report_count >= _TTL_CLEANUP_INTERVAL:
                self._report_count = 0
                task = asyncio.create_task(self._cleanup_storages_safe())
                self._pipeline._background_tasks.add(task)
                task.add_done_callback(self._pipeline._background_tasks.discard)

    async def _cleanup_storages_safe(self) -> None:
        """后台 TTL 清理入口,异常仅记日志(后台任务无人 await)。"""
        try:
            await self._cleanup_storages()
        except Exception:
            logger.exception("后台 TTL 清理失败,忽略")

    async def _cleanup_storages(self) -> None:
        """按 TTL 配置清理主库与模块库的过期数据。

        清理映射(按配置项语义):
        - 主库 ssas_core.db:events/raw_events 表按 event_ttl_days,
          alerts 表按 alert_ttl_days。
        - 模块库:process.db 按 event_ttl_days,result.db 按 alert_ttl_days。
        TTL 天数 <= 0 时视为禁用对应表的清理。
        """
        event_ttl = self._config.event_ttl_days
        alert_ttl = self._config.alert_ttl_days

        if event_ttl > 0:
            await self._storage.cleanup_expired("events", event_ttl)
            await self._storage.cleanup_expired("raw_events", event_ttl)
        if alert_ttl > 0:
            await self._storage.cleanup_expired("alerts", alert_ttl)

        await self._module_manager.cleanup_all(event_ttl, alert_ttl)

    @staticmethod
    def _merge_assessments(
        assessments: list[RiskAssessment],
    ) -> RiskAssessment:
        """聚合多个事件的 RiskAssessment 为单个 RiskAssessment。

        取最高风险等级作为基础(risk_level、risk_type、risk_score、confidence
        取最高风险等级对应值),合并所有 assessment 的 detected_threats 和
        recommended_actions(去重保序),evidence 合并。

        Args:
            assessments: 各事件的 RiskAssessment 列表。

        Returns:
            聚合后的 RiskAssessment。空列表返回无风险结果。
        """
        if not assessments:
            return RiskAssessment(risk_level=RiskLevel.SAFE)

        if len(assessments) == 1:
            return assessments[0]

        # 找最高风险等级的 assessment 作为基础
        max_assessment = max(assessments, key=lambda a: a.risk_level)

        # 合并 detected_threats 和 recommended_actions(去重保序)
        all_threats: list[str] = []
        all_actions: list[str] = []
        all_evidence: dict[str, Any] = {}
        for a in assessments:
            for t in a.detected_threats:
                if t not in all_threats:
                    all_threats.append(t)
            for act in a.recommended_actions:
                if act not in all_actions:
                    all_actions.append(act)
            all_evidence.update(a.evidence)

        return RiskAssessment(
            has_risk=max_assessment.has_risk,
            risk_level=max_assessment.risk_level,
            risk_type=max_assessment.risk_type,
            risk_score=max_assessment.risk_score,
            confidence=max_assessment.confidence,
            detected_threats=all_threats,
            recommended_actions=all_actions or ["log"],
            evidence=all_evidence,
        )
