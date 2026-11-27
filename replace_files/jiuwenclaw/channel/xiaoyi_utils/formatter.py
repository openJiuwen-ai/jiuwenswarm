# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""XiaoYi Formatter - 消息格式化和发送模块。
基于 TypeScript formatter.ts 实现。
"""

import json
import time
import uuid
from dataclasses import dataclass
from typing import Any

from jiuwenclaw.schema.message import EventType, Message


# ==================== Data Classes ====================

@dataclass
class FileInfo:
    """文件信息."""
    file_name: str
    file_type: str
    file_id: str


# ==================== A2A Protocol Builders ====================

def _build_agent_response_wrapper(
    agent_id: str,
    session_id: str,
    task_id: str,
    response_body: dict[str, Any],
) -> dict[str, Any]:
    """
    构建 agent_response 包装消息（A2A 格式）。

    Args:
        agent_id: Agent ID
        session_id: Session ID
        task_id: Task ID
        response_body: JSON-RPC 响应体

    Returns:
        dict: agent_response 包装的消息
    """
    return {
        "msgType": "agent_response",
        "agentId": agent_id,
        "sessionId": session_id,
        "taskId": task_id,
        "msgDetail": json.dumps(response_body, ensure_ascii=False),
    }


def _build_json_rpc_response(
    message_id: str,
    result: dict[str, Any],
) -> dict[str, Any]:
    """
    构建 JSON-RPC 2.0 响应。

    Args:
        message_id: 消息 ID
        result: 结果对象

    Returns:
        dict: JSON-RPC 2.0 响应
    """
    return {
        "jsonrpc": "2.0",
        "id": message_id,
        "result": result,
    }


def build_status_update_response(
    task_id: str,
    text: str,
    state: str,
) -> dict[str, Any]:
    """
    构建 A2A status-update 事件。

    Args:
        task_id: Task ID
        text: 状态文本
        state: 状态值

    Returns:
        dict: status-update 事件
    """
    return {
        "taskId": task_id,
        "kind": "status-update",
        "final": False,
        "status": {
            "message": {
                "role": "agent",
                "parts": [
                    {
                        "kind": "text",
                        "text": text,
                    },
                ],
            },
            "state": state,
        },
    }


def build_clear_context_response() -> dict[str, Any]:
    """
    构建 clearContext 响应。

    Returns:
        dict: clearContext 响应
    """
    return {
        "status": {
            "state": "cleared",
        },
        "error": {
            "code": 0,
            "message": "",
        },
    }


def build_tasks_cancel_response(task_id: str) -> dict[str, Any]:
    """
    构建 tasks/cancel 响应。

    Args:
        task_id: Task ID

    Returns:
        dict: tasks/cancel 响应
    """
    return {
        "id": task_id,
        "status": {
            "state": "canceled",
        },
        "error": {
            "code": 0,
            "message": "",
        },
    }


# ==================== Message Part Builders ====================

def build_text_part(text: str) -> dict[str, Any]:
    """
    构建文本消息部分。

    Args:
        text: 文本内容

    Returns:
        dict: 文本消息部分
    """
    return {
        "kind": "text",
        "text": text,
    }


def build_reasoning_text_part(text: str) -> dict[str, Any]:
    """
    构建推理文本消息部分（reasoningText）。

    Args:
        text: 推理文本内容

    Returns:
        dict: 推理文本消息部分
    """
    return {
        "kind": "reasoningText",
        "reasoningText": text,
    }


def build_file_part(files: list[FileInfo]) -> dict[str, Any]:
    """
    构建文件消息部分。

    Args:
        files: 文件信息列表

    Returns:
        dict: 文件消息部分
    """
    return {
        "kind": "data",
        "data": {
            "fileInfo": [
                {
                    "fileName": f.file_name,
                    "fileType": f.file_type,
                    "fileId": f.file_id,
                }
                for f in files
            ],
        },
    }


def build_command_part(command: dict[str, Any]) -> dict[str, Any]:
    """
    构建命令消息部分。

    Args:
        command: 命令对象

    Returns:
        dict: 命令消息部分
    """
    return {
        "kind": "data",
        "data": {
            "commands": [command],
        },
    }


# ==================== Main Formatter Functions ====================

class MessageFormatter:
    """消息格式化器，用于将 JiuwenClaw 消息转换为 A2A 格式。"""

    def __init__(self, agent_id: str):
        self.agent_id = agent_id
        self._accumulated_texts: dict[str, str] = {}
        self._last_text_lengths: dict[str, int] = {}

    @staticmethod
    def get_message_id(self) -> str:
        """生成消息 ID。"""
        return f"msg_{int(time.time() * 1000)}"

    @staticmethod
    def get_artifact_id(self) -> str:
        """生成 artifact ID。"""
        return str(uuid.uuid4())

    def get_accumulated_text(self, session_id: str) -> str:
        """获取累积的文本。"""
        return self._accumulated_texts.get(session_id, "")

    def update_accumulated_text(self, session_id: str, text: str) -> None:
        """更新累积的文本。"""
        self._accumulated_texts[session_id] = text

    def clear_accumulated_text(self, session_id: str) -> None:
        """清除累积的文本。"""
        self._accumulated_texts.pop(session_id, None)
        self._last_text_lengths.pop(session_id, None)

    def calculate_delta_text(self, session_id: str, current_text: str) -> str:
        """
        计算增量文本。

        Args:
            session_id: Session ID
            current_text: 当前完整文本

        Returns:
            str: 增量文本
        """
        previous_text = self._accumulated_texts.get(session_id, "")
        self._accumulated_texts[session_id] = current_text

        if current_text.startswith(previous_text):
            return current_text[len(previous_text):]
        else:
            # 如果不是追加模式，返回完整文本
            return current_text

    def format_text_response(
        self,
        session_id: str,
        task_id: str,
        text: str,
        *,
        append: bool = False,
        last_chunk: bool = True,
        is_final: bool = True,
        message_id: str | None = None,
        artifact_id: str | None = None,
    ) -> dict[str, Any]:
        """
        格式化文本响应（A2A artifact-update）。

        Args:
            session_id: Session ID
            task_id: Task ID
            text: 文本内容
            append: 是否追加
            last_chunk: 是否为最后一块
            is_final: 是否为最终消息
            message_id: 消息 ID（可选，默认自动生成）
            artifact_id: Artifact ID（可选，默认自动生成）

        Returns:
            dict: agent_response 包装的消息
        """
        if message_id is None:
            message_id = self.get_message_id()
        if artifact_id is None:
            artifact_id = self.get_artifact_id()

        # 根据 last_chunk 选择使用 text 或 reasoningText
        if last_chunk:
            data_part = build_text_part(text)
        else:
            data_part = build_reasoning_text_part(text)

        artifact_update = {
            "taskId": task_id,
            "kind": "artifact-update",
            "append": append,
            "lastChunk": last_chunk,
            "final": is_final,
            "artifact": {
                "artifactId": artifact_id,
                "parts": [data_part],
            },
        }

        json_rpc_response = _build_json_rpc_response(message_id, artifact_update)

        return _build_agent_response_wrapper(
            agent_id=self.agent_id,
            session_id=session_id,
            task_id=task_id,
            response_body=json_rpc_response,
        )

    def format_status_update(
        self,
        session_id: str,
        task_id: str,
        text: str,
        state: str,
        *,
        message_id: str | None = None,
    ) -> dict[str, Any]:
        """
        格式化状态更新（A2A status-update）。

        Args:
            session_id: Session ID
            task_id: Task ID
            text: 状态文本
            state: 状态值
            message_id: 消息 ID（可选，默认自动生成）

        Returns:
            dict: agent_response 包装的消息
        """
        if message_id is None:
            message_id = self.get_message_id()

        status_update = build_status_update_response(task_id, text, state)
        json_rpc_response = _build_json_rpc_response(message_id, status_update)

        return _build_agent_response_wrapper(
            agent_id=self.agent_id,
            session_id=session_id,
            task_id=task_id,
            response_body=json_rpc_response,
        )

    def format_command(
        self,
        session_id: str,
        task_id: str,
        command: dict[str, Any],
        *,
        message_id: str | None = None,
        artifact_id: str | None = None,
    ) -> dict[str, Any]:
        """
        格式化命令（A2A artifact-update with command）。

        Args:
            session_id: Session ID
            task_id: Task ID
            command: 命令对象
            message_id: 消息 ID（可选，默认自动生成）
            artifact_id: Artifact ID（可选，默认自动生成）

        Returns:
            dict: agent_response 包装的消息
        """
        if message_id is None:
            message_id = self.get_message_id()
        if artifact_id is None:
            artifact_id = self.get_artifact_id()

        artifact_update = {
            "taskId": task_id,
            "kind": "artifact-update",
            "append": False,
            "lastChunk": True,
            "final": False,
            "artifact": {
                "artifactId": artifact_id,
                "parts": [build_command_part(command)],
            },
        }

        json_rpc_response = _build_json_rpc_response(message_id, artifact_update)

        return _build_agent_response_wrapper(
            agent_id=self.agent_id,
            session_id=session_id,
            task_id=task_id,
            response_body=json_rpc_response,
        )

    def format_clear_context(
        self,
        session_id: str,
        message_id: str | None = None,
    ) -> dict[str, Any]:
        """
        格式化 clearContext 响应。

        Args:
            session_id: Session ID
            message_id: 消息 ID（可选，默认自动生成）

        Returns:
            dict: agent_response 包装的消息
        """
        if message_id is None:
            message_id = self.get_message_id()

        clear_context_response = build_clear_context_response()
        json_rpc_response = _build_json_rpc_response(message_id, clear_context_response)

        return _build_agent_response_wrapper(
            agent_id=self.agent_id,
            session_id=session_id,
            task_id=session_id,  # Use sessionId as taskId for clearContext
            response_body=json_rpc_response,
        )

    def format_tasks_cancel(
        self,
        session_id: str,
        task_id: str,
        message_id: str | None = None,
    ) -> dict[str, Any]:
        """
        格式化 tasks/cancel 响应。

        Args:
            session_id: Session ID
            task_id: Task ID
            message_id: 消息 ID（可选，默认自动生成）

        Returns:
            dict: agent_response 包装的消息
        """
        if message_id is None:
            message_id = self.get_message_id()

        cancel_response = build_tasks_cancel_response(task_id)
        json_rpc_response = _build_json_rpc_response(message_id, cancel_response)

        return _build_agent_response_wrapper(
            agent_id=self.agent_id,
            session_id=session_id,
            task_id=task_id,
            response_body=json_rpc_response,
        )


# ==================== Event Type Utilities ====================

def should_send_as_reasoning_text(event_type: EventType | None) -> bool:
    """
    判断是否应该将消息作为 reasoningText 发送。

    Args:
        event_type: 事件类型

    Returns:
        bool: 是否应该作为 reasoningText 发送
    """
    if event_type is None:
        return False

    # Reasoning text 用于以下事件：
    # - CHAT_DELTA: 流式输出中的增量文本
    # - CHAT_TOOL_RESULT: 工具结果
    # - CHAT_SUBTASK_UPDATE: 子任务更新
    # - CHAT_PROCESSING_STATUS: 处理状态
    reasoning_text_events = {
        EventType.CHAT_DELTA,
        EventType.CHAT_SUBTASK_UPDATE,
        EventType.CHAT_PROCESSING_STATUS,
    }

    return event_type in reasoning_text_events


def should_send_as_text(event_type: EventType | None) -> bool:
    """
    判断是否应该将消息作为 text 发送。

    Args:
        event_type: 事件类型

    Returns:
        bool: 是否应该作为 text 发送
    """
    if event_type is None:
        return True  # Default to text

    # Text 用于以下事件：
    # - CHAT_FINAL: 最终完整回复
    # - CHAT_MEDIA: 媒体消息
    # - CHAT_ERROR: 错误消息
    # - CHAT_INTERRUPT_RESULT: 中断结果
    text_events = {
        EventType.CHAT_FINAL,
        EventType.CHAT_MEDIA,
        EventType.CHAT_ERROR,
        EventType.CHAT_INTERRUPT_RESULT,
    }

    return event_type in text_events


def should_send_as_status_update(event_type: EventType | None) -> bool:
    """
    判断是否应该作为 status update 发送。

    Args:
        event_type: 事件类型

    Returns:
        bool: 是否应该作为 status update 发送
    """
    if event_type is None:
        return False

    # Status update 用于以下事件：
    # - CHAT_TOOL_CALL: 工具调用
    # - CHAT_TOOL_RESULT: 工具结果
    # - CHAT_PROCESSING_STATUS: 处理状态
    status_events = {
        EventType.CHAT_TOOL_CALL,
        EventType.CHAT_TOOL_RESULT
    }

    return event_type in status_events


def get_status_state_for_event(event_type: EventType | None, payload: dict | None = None) -> str:
    """
    根据事件类型获取状态值。

    Args:
        event_type: 事件类型
        payload: 消息载荷

    Returns:
        str: 状态值
    """
    if event_type is None:
        return "unknown"

    if event_type == EventType.CHAT_PROCESSING_STATUS:
        if payload and isinstance(payload, dict):
            is_processing = payload.get("is_processing", True)
            return "working" if is_processing else "completed"
        return "working"

    status_map = {
        EventType.CHAT_TOOL_CALL: "working",
        EventType.CHAT_TOOL_RESULT: "working",
        EventType.CHAT_FINAL: "completed",
        EventType.CHAT_ERROR: "failed",
    }

    return status_map.get(event_type, "unknown")


def get_status_text_for_event(event_type: EventType | None, payload: dict | None = None) -> str:
    """
    根据事件类型获取状态文本。

    Args:
        event_type: 事件类型
        payload: 消息载荷

    Returns:
        str: 状态文本
    """
    if event_type is None:
        return "处理中"

    if payload and isinstance(payload, dict):
        if event_type == EventType.CHAT_TOOL_CALL:
            tool_call = payload.get("tool_call", {})
            if isinstance(tool_call, dict):
                tool_name = tool_call.get("name", "")
                if tool_name:
                    return f"正在使用工具：{tool_name}"
            return "正在使用工具..."
        if event_type == EventType.CHAT_TOOL_RESULT:
            tool_name = payload.get("tool_name", "")
            if tool_name:
                return f"工具 {tool_name} 执行完成"
            return "工具执行完成"
        if event_type == EventType.CHAT_PROCESSING_STATUS:
            is_processing = payload.get("is_processing", True)
            if is_processing:
                return "任务正在处理中，请稍后~"
            return "任务处理已完成~"
        content = payload.get("content", "")
        if isinstance(content, str) and content:
            return content
        if isinstance(content, dict):
            return content.get("output", "处理中")

    status_text_map = {
        EventType.CHAT_TOOL_CALL: "正在使用工具...",
        EventType.CHAT_TOOL_RESULT: "工具执行完成",
        EventType.CHAT_PROCESSING_STATUS: "任务正在处理中",
        EventType.CHAT_FINAL: "任务已完成",
        EventType.CHAT_ERROR: "处理失败，请稍后重试",
        EventType.CHAT_INTERRUPT_RESULT: "任务已中断",
    }

    return status_text_map.get(event_type, "处理中")


def extract_msg_content(msg: Message) -> str:
    """提取msg 信息"""
    content = ""
    payload = msg.payload if msg.payload else {}
    if not isinstance(payload, dict):
        return str(msg.payload)
    if msg.event_type == EventType.CHAT_TOOL_CALL:
        content = payload.get("tool_call", {}).get("name", "")
    elif msg.event_type == EventType.CHAT_TOOL_RESULT:
        content = payload.get("result", "")
    else:
        content = msg.payload.get("content", "")
        if isinstance(content, dict):
            content = content.get("output", str(content))
        content = str(content)
    return content