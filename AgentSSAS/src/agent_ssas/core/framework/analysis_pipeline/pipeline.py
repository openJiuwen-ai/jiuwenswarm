# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""AgentSSAS 威胁分析流水线模块。

ThreatAnalysisPipeline 从检测模块管理器查询订阅列表,
遍历执行每条流水线(建模→分析→存储→呈现),聚合报告为 RiskAssessment。
流水线同时负责将有风险的报告统一写入主库 alerts 表
(notify 与 auth 模式均覆盖),供告警查询与态势呈现使用。

订阅模式(notify / auth):
- notify 模式:异步检测,report_event 不等待结果,直接返回无风险 RiskAssessment。
  有风险的报告由后台任务写入主库 alerts 表(告警落库不受同步返回影响)。
- auth 模式:同步检测,report_event 必须等待该模块的检测结果完成后才能返回。
  如果 auth 订阅者超过 AgentSSASConfig.auth_timeout(默认 2 秒)未返回,
  则按该模块的 auth_timeout_policy(默认 allow)生成默认报告,不阻断业务。
- 如果有任一订阅者是 auth 模式,report_event 同步等待所有 auth 模式检测完成
  (或超时后按默认策略返回)。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from agent_ssas.core.framework.config.settings import AgentSSASConfig
from agent_ssas.core.framework.core_types.assessment import RiskAssessment, RiskLevel
from agent_ssas.core.framework.core_types.event import UnifiedEvent
from agent_ssas.core.framework.presentation.threat_log import AgentSSASThreatLog

logger = logging.getLogger(__name__)

# 订阅模式常量(与 module_manager 保持一致)
MODE_NOTIFY = "notify"
MODE_AUTH = "auth"


class ThreatAnalysisPipeline:
    """威胁分析流水线模块。

    从检测模块管理器(DetectionModuleManager)查询订阅列表,
    遍历执行每个订阅模块的流水线(建模→分析→存储→呈现),
    聚合所有报告为单个 RiskAssessment 返回给接入适配模块。

    订阅模式区分:
    - notify 模式的订阅者:后台异步执行(不等待结果),不影响 report_event 返回。
      后台任务完成后,有风险的报告统一写入主库 alerts 表。
    - auth 模式的订阅者:同步等待结果,report_event 必须等待其完成后才返回。
      超过 AgentSSASConfig.auth_timeout 未返回时,按模块的 auth_timeout_policy 生成默认报告。
    """

    def __init__(
        self,
        config: AgentSSASConfig,
        module_manager: Any,
        storage: Any | None = None,
    ) -> None:
        """初始化威胁分析流水线。

        Args:
            config: AgentSSAS 子系统配置,注入到呈现插件等内部组件。
            module_manager: 检测模块管理器实例,需提供以下接口:
                get_subscribers_with_mode(event_type) -> list[(module_name, mode)]、
                get_module(module_name) -> DetectionModule、
                get_storage(module_name) -> DetectionModule 的存储管理器。
                DetectionModule 需暴露 .modeler 和 .analyzer 插件属性。
            storage: 主库存储实例(SQLiteStore),用于将有风险的报告
                写入 alerts 表。为 None 时不写告警(兼容独立测试场景)。
        """
        self._config = config
        self._module_manager = module_manager
        # 主库存储,用于告警落库(notify/auth 模式统一覆盖)
        self._storage = storage
        # 呈现插件实例,用于流水线末端的呈现输出
        self._presentation = AgentSSASThreatLog(config)
        # 后台任务引用集合, 防止 task 被 GC 回收
        self._background_tasks: set[asyncio.Task[Any]] = set()

    async def run(self, unified: UnifiedEvent) -> RiskAssessment:
        """执行威胁分析流水线,返回聚合的 RiskAssessment。

        从 unified 获取事件类型,查询订阅该事件的检测模块列表(含模式),
        分离 auth 和 notify 订阅者:
        - notify 订阅者:后台异步执行(不等待结果),后台任务完成后
          有风险的报告统一写入主库 alerts 表。
        - auth 订阅者:同步等待结果,聚合为 RiskAssessment 返回,
          有风险的报告同样写入主库 alerts 表。
        - 全 notify 时返回无风险 RiskAssessment(检测在后台异步进行)。

        Args:
            unified: 统一事件,包含 event_node、trace 等结构。

        Returns:
            聚合后的 RiskAssessment。若无订阅模块则返回无风险结果。
            若全为 notify 模式订阅者,返回无风险结果(检测在后台异步进行)。
            若有 auth 模式订阅者,同步等待其结果后聚合返回。
        """
        event_type = unified.event_node.event_type
        subs = self._module_manager.get_subscribers_with_mode(event_type)

        # 未订阅直接返回无风险结果,避免无效遍历开销
        if not subs:
            logger.debug("事件类型 %s 无订阅模块,直接返回无风险结果", event_type)
            return RiskAssessment()

        # 分离 auth 和 notify 订阅者
        auth_modules = [name for name, mode in subs if mode == MODE_AUTH]
        notify_modules = [name for name, mode in subs if mode == MODE_NOTIFY]

        event_desc = unified.to_event_desc()

        # notify 模式:后台异步执行(不等待结果),完成后统一告警落库
        if notify_modules:
            task = asyncio.create_task(
                self._run_notify_pipeline(notify_modules, event_desc, unified)
            )
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)

        # auth 模式:同步等待结果(含超时控制)
        if auth_modules:
            reports = await self._run_auth_subs_pipeline(
                auth_modules, event_desc
            )
            # 有风险的报告统一写入主库 alerts 表
            await self._write_alerts(reports, unified)
            return self._aggregate_reports(reports)

        # 全 notify:返回无风险结果(检测在后台异步进行)
        return RiskAssessment()

    async def _run_notify_pipeline(
        self,
        module_names: list[str],
        event_desc: dict[str, Any],
        unified: UnifiedEvent,
    ) -> None:
        """执行一组 notify 订阅模块的流水线,完成后统一告警落库。

        Args:
            module_names: notify 模式检测模块名列表。
            event_desc: 事件描述 json。
            unified: 统一事件,用于构建告警记录的关联字段。
        """
        reports = await self._run_subs_pipeline(module_names, event_desc)
        # 有风险的报告统一写入主库 alerts 表
        # (后台任务无人 await,告警落库失败仅记日志,不影响其他流程)
        await self._write_alerts(reports, unified)

    async def _write_alerts(
        self,
        reports: list[dict[str, Any]],
        unified: UnifiedEvent,
    ) -> None:
        """将有风险的报告写入主库 alerts 表。

        遍历报告列表,筛选有风险(has_risk 为 True 且 risk_level 高于 safe)
        的报告,逐条写入主库存储的 alerts 表。告警落库失败仅记录日志,
        不影响检测与聚合流程(与 fail-open 策略一致)。

        alert_id 采用 "{event_id}_{module_name}" 格式,保证同一事件的
        多个模块告警互相独立且可追溯。

        Args:
            reports: 威胁分析报告列表(简化格式)。
            unified: 统一事件,用于构建告警记录的关联字段。
        """
        if self._storage is None:
            return

        for report in reports:
            if not isinstance(report, dict):
                continue
            if not report.get("has_risk", False):
                continue
            risk_level_str = report.get("risk_level", "safe")
            if self._parse_risk_level(risk_level_str) <= RiskLevel.SAFE:
                continue

            module_name = report.get("module_name", "unknown")
            try:
                alert = {
                    # event_id + module_name 保证多模块告警互不覆盖
                    "alert_id": f"{unified.event_id}_{module_name}",
                    "module_name": module_name,
                    "interaction_seq": unified.event_node.interaction_seq,
                    "session_id": unified.event_node.session_id,
                    "trace_id": unified.trace.trace_id,
                    "risk_level": risk_level_str,
                    "risk_type": report.get("risk_type", ""),
                    "timestamp": unified.event_node.timestamp,
                    "acknowledged": False,
                    "risk_score": report.get("risk_score", 0.0),
                    "confidence": report.get("confidence", 0.0),
                    "detected_threats": report.get("detected_threats", []),
                    "recommended_actions": report.get(
                        "recommended_actions", ["log"]
                    ),
                    "evidence": report.get("evidence", {}),
                }
                await self._storage.create_alert(alert)
            except Exception:
                logger.exception(
                    "告警落库失败: module_name=%s, event_id=%s",
                    module_name,
                    unified.event_id,
                )

    async def _run_subs_pipeline(
        self,
        module_names: list[str],
        event_desc: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """执行一组订阅模块的流水线,返回报告列表。

        遍历每个模块执行建模→分析→存储→呈现,收集非 None 的报告。

        Args:
            module_names: 检测模块名列表。
            event_desc: 事件描述 json。

        Returns:
            威胁分析报告列表(跳过失败模块)。
        """
        reports: list[dict[str, Any]] = []
        for module_name in module_names:
            report = await self._run_module_pipeline(module_name, event_desc)
            if report is not None:
                reports.append(report)
        return reports

    async def _run_auth_subs_pipeline(
        self,
        module_names: list[str],
        event_desc: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """执行 auth 模式订阅模块的流水线(含超时控制),返回报告列表。

        对每个 auth 模块使用 asyncio.wait_for 控制超时。
        超时后按模块的 auth_timeout_policy(默认 allow)生成默认报告。

        Args:
            module_names: auth 模式检测模块名列表。
            event_desc: 事件描述 json。

        Returns:
            威胁分析报告列表(含超时后生成的默认报告)。
        """
        reports: list[dict[str, Any]] = []
        timeout = self._config.auth_timeout
        for module_name in module_names:
            try:
                report = await asyncio.wait_for(
                    self._run_module_pipeline(module_name, event_desc),
                    timeout=timeout,
                )
                if report is not None:
                    reports.append(report)
            except TimeoutError:
                logger.warning(
                    "auth 模式检测超时,按默认策略返回: "
                    "module_name=%s, timeout=%ss",
                    module_name,
                    timeout,
                )
                report = self._make_timeout_report(
                    module_name, event_desc
                )
                if report is not None:
                    reports.append(report)
        return reports

    def _make_timeout_report(
        self,
        module_name: str,
        event_desc: dict[str, Any],
    ) -> dict[str, Any]:
        """根据检测模块的 auth_timeout_policy 生成超时默认报告。

        从 module.yaml 的 auth_timeout_policy 字段读取策略(默认 allow),
        生成对应的威胁分析报告。

        Args:
            module_name: 检测模块名。
            event_desc: 事件描述 json。

        Returns:
            超时默认报告 dict。
        """
        module = self._module_manager.get_module(module_name)
        module_config = getattr(module, "config", {}) or {}
        policy = module_config.get("auth_timeout_policy", "allow")
        aux_ids = event_desc.get("aux_ids", {})
        event_node = event_desc.get("event_node", {})
        analytic_type_id = module_config.get("analytic_type_id", 0)

        if policy == "reject":
            return {
                "has_risk": True,
                "risk_level": "high",
                "risk_type": "auth_timeout_reject",
                "risk_score": 75.0,
                "confidence": 1.0,
                "detected_threats": ["auth_timeout"],
                "recommended_actions": ["block"],
                "evidence": {
                    "reason": f"auth timeout, policy=reject, module={module_name}",
                },
                "module_name": module_name,
                "aux_ids": aux_ids,
                "event_node": event_node,
                "analytic_type_id": analytic_type_id,
            }
        # 默认 allow 策略
        return {
            "has_risk": False,
            "risk_level": "safe",
            "risk_type": "auth_timeout_allow",
            "risk_score": 0.0,
            "confidence": 0.0,
            "detected_threats": [],
            "recommended_actions": ["log"],
            "evidence": {
                "reason": f"auth timeout, policy=allow, module={module_name}",
            },
            "module_name": module_name,
            "aux_ids": aux_ids,
            "event_node": event_node,
            "analytic_type_id": analytic_type_id,
        }

    async def _run_module_pipeline(
        self,
        module_name: str,
        event_desc: dict[str, Any],
    ) -> dict[str, Any] | None:
        """执行单个检测模块的流水线:建模→分析→存储→呈现。

        任一步骤异常时记录日志并跳过该模块,不影响其他模块的执行。
        建模或分析失败时返回 None;存储或呈现失败时仍返回已产出的报告。

        Args:
            module_name: 检测模块名。
            event_desc: 事件描述 json,包含 event_node、aux_ids、trace 等字段。

        Returns:
            威胁分析报告(简化格式);建模或分析失败时返回 None。
        """
        module = self._module_manager.get_module(module_name)
        if module is None:
            logger.warning("未找到检测模块: module_name=%s", module_name)
            return None

        # 数据建模:直接调用检测模块的建模插件
        try:
            model_data = await module.modeler.build_model(event_desc)
        except Exception:
            logger.exception(
                "数据建模失败,跳过该模块: module_name=%s", module_name
            )
            return None

        # 威胁分析:直接调用检测模块的分析插件
        try:
            report = await module.analyzer.analyze(model_data)
        except Exception:
            logger.exception(
                "威胁分析失败,跳过该模块: module_name=%s", module_name
            )
            return None

        # 校验报告类型
        if not isinstance(report, dict):
            logger.warning(
                "威胁分析报告非 dict 类型,跳过该模块: module_name=%s, type=%s",
                module_name,
                type(report).__name__,
            )
            return None

        # 丰富报告:注入 aux_ids 和 module_name(若插件未提供)
        # aux_ids 来自事件描述,用于呈现模块关联构建 OCSF 报告
        aux_ids = event_desc.get("aux_ids", {})
        report.setdefault("aux_ids", aux_ids)
        # 注入 event_node 供呈现模块构建 OCSF 报告
        event_node = event_desc.get("event_node", {})
        report.setdefault("event_node", event_node)
        # module_name 设为检测模块名(若插件未提供或为空字符串)
        if not report.get("module_name"):
            report["module_name"] = module_name
        report.setdefault("recommended_actions", [])
        # 注入 analytic_type_id(从 module.config 读取,供 OCSF 构建 analytic 对象)
        # analytic_type_id 在 module.yaml 顶层声明,表示检测模块的分析类型
        module_config = getattr(module, "config", {}) or {}
        if isinstance(module_config, dict):
            analytic_type_id = module_config.get("analytic_type_id", 0)
            if isinstance(analytic_type_id, int):
                report.setdefault("analytic_type_id", analytic_type_id)

        # 存储持久化(失败不影响后续呈现和聚合)
        try:
            storage = self._module_manager.get_storage(module_name)
            if storage is not None:
                await storage.result_store.record_event(report)
        except Exception:
            logger.exception(
                "存储持久化失败: module_name=%s", module_name
            )

        # 呈现输出(流水线最后一步,失败不影响聚合)
        try:
            await self._presentation.render(report)
        except Exception:
            logger.exception(
                "呈现输出失败: module_name=%s", module_name
            )

        return report

    def _aggregate_reports(self, reports: list[dict]) -> RiskAssessment:
        """聚合多检测模块报告为 RiskAssessment。

        按文档 7.10 节聚合策略:
        1. 取最高风险等级(safe < low < medium < high < critical)
        2. 合并 detected_threats(去重保序)
        3. 合并 recommended_actions(去重保序)
        4. 合并 evidence(以模块名为 key)
        5. risk_score 取最高
        6. confidence 取最高风险等级报告的 confidence

        Args:
            reports: 威胁分析报告列表(简化格式)。

        Returns:
            聚合后的 RiskAssessment。空列表返回无风险结果。
        """
        if not reports:
            return RiskAssessment()

        max_risk_level = RiskLevel.SAFE
        # 最高风险等级的报告,用于取 risk_type 和 confidence
        max_level_report: dict | None = None
        max_risk_score = 0.0
        has_risk = False
        detected_threats: list[str] = []
        recommended_actions: list[str] = []
        evidence: dict[str, Any] = {}

        for report in reports:
            if not isinstance(report, dict):
                continue

            # 解析风险等级
            risk_level_str = report.get("risk_level", "safe")
            risk_level = self._parse_risk_level(risk_level_str)

            # 取最高风险等级,记录对应报告(首个最高等级报告优先)
            if max_level_report is None or risk_level > max_risk_level:
                max_risk_level = risk_level
                max_level_report = report

            # has_risk:任一报告有风险即为有风险
            if report.get("has_risk", False):
                has_risk = True

            # risk_score 取最高
            score = report.get("risk_score", 0.0)
            if isinstance(score, (int, float)) and score > max_risk_score:
                max_risk_score = float(score)

            # 合并 detected_threats(去重保序)
            for threat in report.get("detected_threats", []):
                if isinstance(threat, str) and threat not in detected_threats:
                    detected_threats.append(threat)

            # 合并 recommended_actions(去重保序)
            for action in report.get("recommended_actions", []):
                if isinstance(action, str) and action not in recommended_actions:
                    recommended_actions.append(action)

            # 合并 evidence(以模块名为 key,仅收录非空证据)
            module_name = report.get("module_name", "")
            ev = report.get("evidence", {})
            if isinstance(ev, dict) and ev:
                evidence[module_name] = ev

        # 若任一报告的风险等级高于 SAFE,则 has_risk 为 True
        if max_risk_level > RiskLevel.SAFE:
            has_risk = True

        # confidence 和 risk_type 取最高风险等级报告的值
        confidence = 0.0
        risk_type = ""
        if max_level_report is not None:
            conf = max_level_report.get("confidence", 0.0)
            if isinstance(conf, (int, float)):
                confidence = float(conf)
            risk_type = max_level_report.get("risk_type", "")

        # recommended_actions 为空时默认 ["log"]
        if not recommended_actions:
            recommended_actions = ["log"]

        return RiskAssessment(
            has_risk=has_risk,
            risk_level=max_risk_level,
            risk_type=risk_type,
            risk_score=max_risk_score,
            confidence=confidence,
            detected_threats=detected_threats,
            recommended_actions=recommended_actions,
            evidence=evidence,
        )

    @staticmethod
    def _parse_risk_level(risk_level_str: str) -> RiskLevel:
        """将风险等级字符串解析为 RiskLevel 枚举。

        未知值回退为 SAFE,保证聚合流程对异常输入的健壮性。

        Args:
            risk_level_str: 风险等级字符串,如 "high"、"safe"。

        Returns:
            对应的 RiskLevel 枚举值。
        """
        try:
            return RiskLevel(risk_level_str)
        except ValueError:
            logger.warning(
                "未知风险等级字符串,回退为 SAFE: risk_level=%s",
                risk_level_str,
            )
            return RiskLevel.SAFE
