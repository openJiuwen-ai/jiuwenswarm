# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""AgentSSAS 数据预处理模块。

按 common.source 选择格式解析器,将 raw_event(原始事件 dict)
转换为 UnifiedEvent(引擎内部统一事件),并管理聚合事件栈。
订阅列表管理由 DetectionModuleManager 负责,本模块不参与订阅查询和分发。

聚合事件分发:
当结束事件到达时(tool_output / llm_output / invoke_end),触发聚合,
生成对应的聚合事件(one_toolcall_event / one_llmcall_event / one_interaction_event)。
parse 方法返回事件列表:[基础事件] + [聚合事件(如有)],
供接入适配模块逐个送入流水线执行检测。
"""

from __future__ import annotations

import uuid
from typing import Any, ClassVar

from agent_ssas.core.framework.config.settings import AgentSSASConfig
from agent_ssas.core.framework.core_types.event import (
    CURRENT_EVENT_VERSION,
    EventNode,
    Trace,
    UnifiedEvent,
)
from agent_ssas.core.framework.data_preprocessor.event_aggregator import EventAggregator
from agent_ssas.core.framework.data_preprocessor.agent_preprocessor import (
    AgentSSASPreprocessor,
)

# 聚合事件 event_type → node_type 的映射表
_AGG_EVENT_TYPE_TO_NODE_TYPE: dict[str, str] = {
    "one_toolcall_event": "tool_call",
    "one_llmcall_event": "llm_call",
    "one_interaction_event": "interaction",
}


class DataPreprocessor:
    """数据预处理模块。

    按 common.source 选择格式解析器,将 raw_event(原始事件 dict)
    转换为 UnifiedEvent(event)。管理聚合事件栈,当结束事件到达时
    触发聚合,返回事件列表(基础事件 + 聚合事件)。

    使用方式:
    1. 通过 register_parser 注册各 source 对应的格式解析器;
    2. 调用 parse 方法解析 raw_event,返回 UnifiedEvent 列表;
    3. 解析过程中自动管理聚合事件栈。
    """

    # source → parser 的注册表(类级别共享,便于全局注册)
    # ClassVar 标注:该 dict 是类级共享注册表,有意设计为可变默认值
    _parsers: ClassVar[dict[str, Any]] = {}

    @classmethod
    def register_parser(cls, source: str, parser: Any) -> None:
        """注册格式解析器。

        将指定 source 与对应的解析器绑定,后续解析 raw_event 时
        根据 common.source 字段选择对应的解析器。

        参数:
            source: 上报源标识,如 "AgentSSASSecurityRail"。
            parser: 解析器实例,需实现 EventParserProtocol(async parse 方法)。
        """
        cls._parsers[source] = parser

    def __init__(self, config: AgentSSASConfig) -> None:
        """初始化数据预处理模块。

        参数 config: SSAS 配置实例。
        """
        self._config = config
        # 默认解析器:AgentSSASPreprocessor(处理 AgentSSASSecurityRail 上报的事件)
        self._default_parser = AgentSSASPreprocessor(config)
        # 聚合事件栈管理器
        self._aggregator = EventAggregator()

    async def parse(self, raw_event: dict) -> list[UnifiedEvent]:
        """解析 raw_event → UnifiedEvent 列表。

        读取 raw_event 的 common.source 字段,选择对应的格式解析器进行解析。
        解析完成后,将基础事件入聚合栈;若是结束事件,触发聚合。
        返回事件列表:[派生事件(如有)] + [基础事件] + [聚合事件(如有)]。

        参数 raw_event: 原始事件 dict(包含 common / payload / metadata 三层)。

        返回: 转换后的 UnifiedEvent 列表。首部可能是派生事件(session_start 等),
            然后是基础事件,末尾是聚合事件(如有)。

        异常:
            ValueError: raw_event 不是 dict 或缺少 common 层时。
        """
        # 选择格式解析器
        common = raw_event.get("common", {}) if isinstance(raw_event, dict) else {}
        source = common.get("source", "") if isinstance(common, dict) else ""
        parser = self._parsers.get(source, self._default_parser)

        # 解析 raw_event → UnifiedEvent 列表(含派生事件)
        parsed_events = await parser.parse(raw_event)

        # 管理聚合事件栈,返回事件列表
        # parsed_events 可能包含 [派生事件...] + [基础事件]
        # 对基础事件(列表中最后一个,即原始事件对应的 UnifiedEvent)做聚合处理
        result: list[UnifiedEvent] = []
        for i, unified in enumerate(parsed_events):
            if i < len(parsed_events) - 1:
                # 派生事件直接加入结果,不入聚合栈
                result.append(unified)
            else:
                # 基础事件:入聚合栈并处理聚合
                result.extend(self._handle_aggregation(unified))

        return result

    def _handle_aggregation(self, unified: UnifiedEvent) -> list[UnifiedEvent]:
        """管理聚合事件栈,返回事件列表。

        将基础事件入栈;若是结束事件(tool_output / llm_output / invoke_end),
        触发聚合,生成对应的聚合事件。

        参数 unified: 当前解析后的基础事件。

        返回: 事件列表,首个元素是基础事件,后续是聚合事件(如有)。
        """
        # 基础事件入栈
        self._aggregator.push(unified)

        # 触发聚合(结束事件到达时)
        aggregated_dicts = self._aggregator.aggregate(unified)

        # 聚合事件转为 UnifiedEvent
        events = [unified]
        for agg_dict in aggregated_dicts:
            agg_unified = self._aggregated_to_unified(agg_dict, unified)
            if agg_unified is not None:
                events.append(agg_unified)

        return events

    @staticmethod
    def _aggregated_to_unified(
        agg_dict: dict[str, Any],
        trigger_event: UnifiedEvent,
    ) -> UnifiedEvent | None:
        """将聚合事件 dict 转为 UnifiedEvent。

        聚合事件 dict 包含 event_type、aux_ids、内容字段等,
        转为流水线可消费的 UnifiedEvent 结构。

        参数:
            agg_dict: 聚合事件 dict(来自 EventAggregator.aggregate)。
            trigger_event: 触发聚合的结束事件,用于关联 trace 等。

        返回: UnifiedEvent 实例;转换失败时返回 None。
        """
        event_type = agg_dict.get("event_type", "")
        if not event_type:
            return None

        aux_ids = agg_dict.get("aux_ids", {})

        # 构建 EventNode
        node = EventNode(
            node_id=agg_dict.get("node_id", ""),
            node_type=_AGG_EVENT_TYPE_TO_NODE_TYPE.get(event_type, event_type),
            parent_node_id="",
            next_node_id="",
            session_id=aux_ids.get("session_id", ""),
            interaction_seq=aux_ids.get("interaction_seq", -1),
            agent_id=aux_ids.get("agent_id", ""),
            input_content=agg_dict.get("input_content", ""),
            output_content=agg_dict.get("output_content", ""),
            action_name=agg_dict.get("tool_name", event_type),
            event_type=event_type,
            event_class="lifecycle",
            source="AgentSSASSecurityRail",
            timestamp=agg_dict.get("end_time", 0.0),
            llm_call_seq=aux_ids.get("llm_call_seq", -1),
            tool_call_seq=aux_ids.get("tool_call_seq", -1),
            tool_call_id=aux_ids.get("tool_call_id", ""),
        )

        # 构建 Trace(复用触发事件的 trace_id)
        trace = Trace(
            trace_id=aux_ids.get("trace_id", trigger_event.trace.trace_id),
        )

        return UnifiedEvent(
            event_node=node,
            trace=trace,
            event_id=str(uuid.uuid4()),
            event_version=CURRENT_EVENT_VERSION,
        )

    @property
    def aggregator(self) -> EventAggregator:
        """聚合事件栈管理器(供外部查询聚合状态)。"""
        return self._aggregator
