# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""AgentSSAS 统一事件数据模型。

定义引擎内部统一的事件数据结构 UnifiedEvent,
以及 Trace → Session → Interaction → EventNode → DataNode 的层次结构。

适配实时事件流场景,采用增量构建策略:每个事件到达时创建对应的 EventNode,
并通过 interaction_seq、session_id、trace_id 关联到已有的 Interaction、Session、Trace 结构。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# 当前 event 格式版本号(见文档 5.11 节 event 格式版本号管理)
CURRENT_EVENT_VERSION = "1.0"


@dataclass
class DataNode:
    """数据节点:记录事件中产生的数据。"""

    data_id: str  # 数据唯一标识
    content: str  # 数据内容
    source_node_id: str  # 产生此数据的节点 node_id
    tags: list[str] = field(default_factory=list)  # 数据标签列表


@dataclass
class EventNode:
    """事件节点:统一节点结构,对应一个基础事件(由单个 raw_event 转换而来)。

    所有 seq 字段(interaction_seq、llm_call_seq、tool_call_seq)缺省值为 -1,
    表示当前事件不具备此序号字段。首个有效值为 0(int 类型自增序号)。
    """

    node_id: str  # 节点唯一标识
    node_type: str  # 节点类型:session / interaction / llm_call / tool_call
    parent_node_id: str  # 父节点 node_id
    next_node_id: str  # 同级下一个节点 node_id(控制流顺序)
    session_id: str  # 所属会话 ID
    interaction_seq: int  # 所属交互 ID(int 类型自增序号;session 为 -1)
    agent_id: str = ""  # 智能体 ID(用于报告呈现时的资源关联)

    # 通用内容字段(所有类型节点共用)
    input_content: str = ""  # 节点的输入内容
    output_content: str = ""  # 节点的输出内容
    action_name: str = ""  # 节点执行的动作名称
    # - tool_call 类型:action_name = tool_name,input_content = args,output_content = tool_output
    # - interaction 类型:action_name = "invoke_start"(开始端)或 "invoke_end"(结束端),input_content = query,output_content = result
    # - session 类型:action_name = "session_start"(开始端)或 "session_end"(结束端),input_content = "",output_content = ""
    # - llm_call 类型:action_name = "llm_call",input_content = prompt,output_content = response

    # 数据关联字段
    input_data_ids: list[str] = field(default_factory=list)  # 输入数据 ID 列表
    output_data_ids: list[str] = field(default_factory=list)  # 输出数据 ID 列表

    # raw_event 溯源字段
    event_type: str = ""  # 对应 raw_event 的 common.event_type 字段
    event_class: str = ""  # 事件大类:lifecycle / security
    source: str = ""  # 对应 raw_event 的 common.source 字段
    timestamp: float = 0.0  # 对应 raw_event 的 common.timestamp 字段

    # LLM/工具调用序号字段(int 类型自增序号)
    # 缺省值为 -1,表示当前事件不具备此序号字段。首个有效值为 0
    llm_call_seq: int = -1  # llm_call 事件时为本这次大模型调用的序号;tool_call 事件时为所属大模型调用的序号
    tool_call_seq: int = -1  # 工具调用自增序号;非工具事件为 -1
    tool_call_id: str = ""  # 上报端工具调用 ID;用于跨输入/输出事件显式关联

    # 安全检测事件字段(仅 event_class="security" 的事件有值)
    is_risk_event: bool = False  # 是否为安全检测事件(等价于 event_class == "security")
    risk_source: str = ""  # 安全检测事件的风险来源 Rail 名
    risk_type: str = ""  # 风险类型
    risk_level: str = ""  # 风险等级
    risk_assessment: dict | None = None  # 风险事件携带的检测结果


@dataclass
class Interaction:
    """交互结构:对应一个 interaction 的完整生命周期。"""

    interaction_seq: int  # 交互唯一标识(int 类型组内自增序号)
    session_id: str  # 所属会话 ID
    interaction_node_id: str  # interaction 节点的 node_id
    tool_call_node_ids: list[str] = field(default_factory=list)  # tool_call 节点 node_id 列表(有序)
    llm_call_node_ids: list[str] = field(default_factory=list)  # llm_call 节点 node_id 列表(有序)


@dataclass
class Session:
    """会话结构:对应一个 session 的完整生命周期。"""

    session_id: str  # 会话唯一标识
    session_node_id: str  # session 节点的 node_id
    interactions: list[Interaction] = field(default_factory=list)  # 交互列表


@dataclass
class Trace:
    """追踪结构:顶层结构,包含全局索引。"""

    trace_id: str  # 追踪唯一标识
    source_info: dict[str, str] = field(default_factory=dict)  # 来源信息
    sessions: list[Session] = field(default_factory=list)  # 会话列表
    all_nodes: dict[str, EventNode] = field(default_factory=dict)  # node_id → EventNode 全局索引
    all_data: dict[str, DataNode] = field(default_factory=dict)  # data_id → DataNode 全局索引


@dataclass
class UnifiedEvent:
    """引擎统一事件格式,所有检测模块的共同输入。

    适配实时事件流场景:每个事件到达时创建对应的 EventNode,
    并通过 interaction_seq、session_id、trace_id 关联到已有结构。
    event_version 字段标识当前 event 格式版本号,
    用于版本兼容管理(见 5.11 节)。
    """

    event_node: EventNode  # 当前事件对应的 EventNode
    trace: Trace  # 当前事件所属的 Trace(增量构建中)
    event_id: str  # 事件唯一标识(UUID)
    event_version: str = CURRENT_EVENT_VERSION  # event 格式版本号,如 "1.0"

    def to_event_desc(self) -> dict:
        """转换为事件描述 json,供数据建模插件消费。

        输出格式与文档 5.8.3 节聚合事件格式对齐:都包含 aux_ids 子结构、
        action_name/input_content/output_content 内容字段,
        便于检测模块以统一视角处理基础事件和聚合事件。
        """
        return {
            "event_version": self.event_version,
            "event_node": self._node_to_dict(self.event_node),
            "aux_ids": self._build_aux_ids(self.event_node, self.trace),
            "trace": self._trace_to_dict(self.trace),
            "event_id": self.event_id,
        }

    @staticmethod
    def _build_aux_ids(node: EventNode, trace: Trace) -> dict:
        """构建辅助 ID 子结构,集中存放用于呈现与溯源的关联 ID。

        这些 ID 字段(interaction_seq、session_id、agent_id、trace_id 等)
        代表了事件之间的关联关系,在分析过程中也有参考价值;但在聚合事件中,
        关联关系已通过聚合结构体现,因此这些 ID 字段在聚合场景下主要用于呈现和溯源。
        """
        return {
            "interaction_seq": node.interaction_seq,
            "session_id": node.session_id,
            "agent_id": node.agent_id,
            "trace_id": trace.trace_id,
            "llm_call_seq": node.llm_call_seq,
            "tool_call_seq": node.tool_call_seq,
            "tool_call_id": node.tool_call_id,
        }

    @staticmethod
    def _node_to_dict(node: EventNode) -> dict:
        """将 EventNode 序列化为 dict。"""
        return {
            "node_id": node.node_id,
            "node_type": node.node_type,
            "parent_node_id": node.parent_node_id,
            "next_node_id": node.next_node_id,
            "session_id": node.session_id,
            "interaction_seq": node.interaction_seq,
            "agent_id": node.agent_id,
            "input_content": node.input_content,
            "output_content": node.output_content,
            "action_name": node.action_name,
            "input_data_ids": node.input_data_ids,
            "output_data_ids": node.output_data_ids,
            "event_type": node.event_type,
            "event_class": node.event_class,
            "source": node.source,
            "timestamp": node.timestamp,
            "llm_call_seq": node.llm_call_seq,
            "tool_call_seq": node.tool_call_seq,
            "tool_call_id": node.tool_call_id,
            "is_risk_event": node.is_risk_event,
            "risk_source": node.risk_source,
            "risk_type": node.risk_type,
            "risk_level": node.risk_level,
        }

    @staticmethod
    def _trace_to_dict(trace: Trace) -> dict:
        """将 Trace 序列化为 dict(仅包含来源信息)。"""
        return {
            "trace_id": trace.trace_id,
            "source_info": trace.source_info,
        }
