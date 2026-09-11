# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""AgentSSAS 聚合事件栈管理模块。

维护聚合栈,基础事件按到达顺序入栈;
当遇到结束事件(tool_output / llm_output / invoke_end)时,
从栈顶弹出相关节点,合并为对应的聚合事件(one_toolcall_event /
one_llmcall_event / one_interaction_event)。
"""

from __future__ import annotations

from agent_ssas.core.framework.core_types.event import (
    CURRENT_EVENT_VERSION,
    UnifiedEvent,
)


class EventAggregator:
    """聚合事件栈管理器。

    维护一个聚合栈,基础事件按到达顺序入栈;
    遇到结束事件时触发聚合,生成对应的聚合事件。
    聚合事件通过 parse 方法返回(当聚合发生时,parse 返回聚合事件而非基础事件)。

    聚合事件类型与触发条件(见文档 5.8.3 节):
    - one_toolcall_event: 收到 tool_output 时触发(合并 tool_input + tool_output)
    - one_llmcall_event: 收到 llm_output 时触发(合并 llm_input + llm_output + 其间的工具调用)
    - one_interaction_event: 收到 invoke_end 时触发(合并整个 interaction 的所有事件)
    """

    def __init__(self) -> None:
        # 聚合栈,存储基础事件的 UnifiedEvent(按到达顺序)
        self._stack: list[UnifiedEvent] = []

    def push(self, unified: UnifiedEvent) -> None:
        """基础事件入栈。

        将解析后的基础事件 UnifiedEvent 压入聚合栈,
        等待对应的结束事件到达时触发聚合。

        参数 unified: 解析后的基础事件。
        """
        self._stack.append(unified)

    def aggregate(self, unified: UnifiedEvent) -> list[dict]:
        """根据结束事件触发聚合,返回生成的聚合事件列表。

        当传入的是结束事件(tool_output / llm_output / invoke_end)时,
        从栈中弹出相关节点,合并为聚合事件。
        非结束事件不触发聚合,返回空列表。

        参数 unified: 当前到达的基础事件。

        返回: 生成的聚合事件 dict 列表(可能为空)。
        """
        event_type = unified.event_node.event_type

        if event_type == "tool_output":
            return self._aggregate_toolcall(unified)
        if event_type == "llm_output":
            return self._aggregate_llmcall(unified)
        if event_type == "invoke_end":
            return self._aggregate_interaction(unified)
        return []

    def _aggregate_toolcall(self, end_event: UnifiedEvent) -> list[dict]:
        """聚合一次工具调用,生成 one_toolcall_event。

        从栈中查找同一 tool_call_seq 的 tool_input 事件,
        合并输入和输出内容,生成 one_toolcall_event。

        参数 end_event: tool_output 结束事件。

        返回: 包含一个 one_toolcall_event dict 的列表。
        """
        tool_call_seq = end_event.event_node.tool_call_seq
        node_id = end_event.event_node.node_id

        # 从栈中查找匹配的 tool_input 事件
        start_event = None
        start_index = -1
        for i in range(len(self._stack) - 1, -1, -1):
            node = self._stack[i].event_node
            if (
                node.event_type == "tool_input"
                and node.tool_call_seq == tool_call_seq
                and node.session_id == end_event.event_node.session_id
            ):
                start_event = self._stack[i]
                start_index = i
                break

        # 构建 one_toolcall_event
        if start_event is not None:
            input_content = start_event.event_node.input_content
            tool_name = start_event.event_node.action_name
            start_time = start_event.event_node.timestamp
        else:
            input_content = ""
            tool_name = end_event.event_node.action_name
            start_time = end_event.event_node.timestamp

        end_time = end_event.event_node.timestamp
        aggregated = self._build_toolcall_event(
            node_id=node_id,
            tool_name=tool_name,
            input_content=input_content,
            output_content=end_event.event_node.output_content,
            start_time=start_time,
            end_time=end_time,
            aux_ids=self._build_aux_ids(end_event),
        )

        # 从栈中移除已聚合的 tool_input
        if start_index >= 0:
            self._stack.pop(start_index)

        return [aggregated]

    def _aggregate_llmcall(self, end_event: UnifiedEvent) -> list[dict]:
        """聚合一次 LLM 调用,生成 one_llmcall_event。

        从栈中查找同一 llm_call_seq 的 llm_input 事件和其间的工具调用,
        合并为 one_llmcall_event。

        参数 end_event: llm_output 结束事件。

        返回: 包含一个 one_llmcall_event dict 的列表。
        """
        llm_call_seq = end_event.event_node.llm_call_seq
        node_id = end_event.event_node.node_id

        # 从栈中查找匹配的 llm_input 事件和其间的工具调用
        start_index = -1
        tool_calls: list[dict] = []
        for i in range(len(self._stack) - 1, -1, -1):
            node = self._stack[i].event_node
            if node.event_type == "llm_input" and node.llm_call_seq == llm_call_seq:
                start_index = i
                break

        if start_index >= 0:
            start_event = self._stack[start_index]
            input_content = start_event.event_node.input_content
            start_time = start_event.event_node.timestamp
            # 收集其间的 tool_input/tool_output 事件(已在栈中的聚合或基础事件)
            for j in range(start_index + 1, len(self._stack)):
                stack_node = self._stack[j].event_node
                if stack_node.event_type in ("tool_input", "tool_output"):
                    if stack_node.tool_call_seq >= 0:
                        tool_calls.append(self._build_toolcall_event(
                            node_id=stack_node.node_id,
                            tool_name=stack_node.action_name,
                            input_content=stack_node.input_content,
                            output_content=stack_node.output_content,
                            start_time=stack_node.timestamp,
                            end_time=stack_node.timestamp,
                            aux_ids=self._build_aux_ids(self._stack[j]),
                        ))
        else:
            input_content = ""
            start_time = end_event.event_node.timestamp

        aggregated = self._build_llmcall_event(
            node_id=node_id,
            input_content=input_content,
            output_content=end_event.event_node.output_content,
            start_time=start_time,
            end_time=end_event.event_node.timestamp,
            tool_calls=tool_calls,
            aux_ids=self._build_aux_ids(end_event),
        )

        # 从栈中移除已聚合的 llm_input 和其间的工具调用
        if start_index >= 0:
            del self._stack[start_index:]

        return [aggregated]

    def _aggregate_interaction(self, end_event: UnifiedEvent) -> list[dict]:
        """聚合一个 interaction,生成 one_interaction_event。

        从栈中查找同一 interaction_seq 的所有事件,
        合并为 one_interaction_event。

        参数 end_event: invoke_end 结束事件。

        返回: 包含一个 one_interaction_event dict 的列表。
        """
        interaction_seq = end_event.event_node.interaction_seq
        session_id = end_event.event_node.session_id

        # 从栈中查找同一 interaction 的 invoke_start 和其间的事件
        start_index = -1
        llm_calls: list[dict] = []
        input_content = ""
        output_content = end_event.event_node.output_content
        start_time = end_event.event_node.timestamp

        for i in range(len(self._stack) - 1, -1, -1):
            node = self._stack[i].event_node
            if (
                node.interaction_seq == interaction_seq
                and node.session_id == session_id
                and node.event_type == "invoke_start"
            ):
                start_index = i
                input_content = node.input_content
                start_time = node.timestamp
                break

        # 收集其间的 LLM 调用和工具调用
        if start_index >= 0:
            for j in range(start_index + 1, len(self._stack)):
                stack_node = self._stack[j].event_node
                if stack_node.event_type == "llm_output":
                    llm_calls.append(self._build_llmcall_event(
                        node_id=stack_node.node_id,
                        input_content="",
                        output_content=stack_node.output_content,
                        start_time=stack_node.timestamp,
                        end_time=stack_node.timestamp,
                        tool_calls=[],
                        aux_ids=self._build_aux_ids(self._stack[j]),
                    ))

        aggregated = self._build_interaction_event(
            input_content=input_content,
            output_content=output_content,
            start_time=start_time,
            end_time=end_event.event_node.timestamp,
            llm_calls=llm_calls,
            aux_ids=self._build_aux_ids(end_event),
        )

        # 从栈中移除已聚合的整个 interaction 的事件
        if start_index >= 0:
            del self._stack[start_index:]

        return [aggregated]

    @staticmethod
    def _build_aux_ids(event: UnifiedEvent) -> dict:
        """构建聚合事件的 aux_ids 子结构。

        参数 event: 触发聚合的结束事件。

        返回: 包含 interaction_seq、session_id、agent_id、trace_id 等的 dict。
        """
        return {
            "interaction_seq": event.event_node.interaction_seq,
            "session_id": event.event_node.session_id,
            "agent_id": event.event_node.agent_id,
            "trace_id": event.trace.trace_id,
            "llm_call_seq": event.event_node.llm_call_seq,
            "tool_call_seq": event.event_node.tool_call_seq,
            "tool_call_id": event.event_node.tool_call_id,
        }

    @staticmethod
    def _build_toolcall_event(
        node_id: str,
        tool_name: str,
        input_content: str,
        output_content: str,
        start_time: float,
        end_time: float,
        aux_ids: dict,
    ) -> dict:
        """构建 one_toolcall_event 聚合事件 dict。

        参数:
            node_id: 节点唯一标识。
            tool_name: 工具名称。
            input_content: 工具输入参数。
            output_content: 工具执行结果。
            start_time: 开始时间戳。
            end_time: 结束时间戳。
            aux_ids: 辅助 ID 子结构。

        返回: one_toolcall_event dict。
        """
        return {
            "event_type": "one_toolcall_event",
            "event_version": CURRENT_EVENT_VERSION,
            "aux_ids": aux_ids,
            "node_id": node_id,
            "tool_name": tool_name,
            "input_content": input_content,
            "output_content": output_content,
            "start_time": start_time,
            "end_time": end_time,
            "duration": end_time - start_time if end_time >= start_time else 0.0,
        }

    @staticmethod
    def _build_llmcall_event(
        node_id: str,
        input_content: str,
        output_content: str,
        start_time: float,
        end_time: float,
        tool_calls: list[dict],
        aux_ids: dict,
    ) -> dict:
        """构建 one_llmcall_event 聚合事件 dict。

        参数:
            node_id: 节点唯一标识。
            input_content: 模型输入内容(messages)。
            output_content: 模型输出内容(response)。
            start_time: 开始时间戳。
            end_time: 结束时间戳。
            tool_calls: 工具调用序列。
            aux_ids: 辅助 ID 子结构。

        返回: one_llmcall_event dict。
        """
        return {
            "event_type": "one_llmcall_event",
            "event_version": CURRENT_EVENT_VERSION,
            "aux_ids": aux_ids,
            "node_id": node_id,
            "input_content": input_content,
            "output_content": output_content,
            "start_time": start_time,
            "end_time": end_time,
            "duration": end_time - start_time if end_time >= start_time else 0.0,
            "tool_calls": tool_calls,
        }

    @staticmethod
    def _build_interaction_event(
        input_content: str,
        output_content: str,
        start_time: float,
        end_time: float,
        llm_calls: list[dict],
        aux_ids: dict,
    ) -> dict:
        """构建 one_interaction_event 聚合事件 dict。

        参数:
            input_content: 用户输入内容。
            output_content: Agent 输出内容。
            start_time: 开始时间戳。
            end_time: 结束时间戳。
            llm_calls: LLM 调用序列。
            aux_ids: 辅助 ID 子结构。

        返回: one_interaction_event dict。
        """
        return {
            "event_type": "one_interaction_event",
            "event_version": CURRENT_EVENT_VERSION,
            "aux_ids": aux_ids,
            "input_content": input_content,
            "output_content": output_content,
            "start_time": start_time,
            "end_time": end_time,
            "duration": end_time - start_time if end_time >= start_time else 0.0,
            "llm_calls": llm_calls,
        }

    def clear(self) -> None:
        """清空聚合栈。"""
        self._stack.clear()
