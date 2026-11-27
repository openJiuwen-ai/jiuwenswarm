# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Session Toolkit
生命周期：Agent创建新session开始，到所有session协程结束

在结束后，MultiSessionToolkit所有内容

Agent 可以通过以下工具操控协程
1. create_new_sessions
接收一个任务描述的列表，对列表里每一个任务，创建一个agent实例，并通过Runner运行该agent，同时把session信息记录在self.sessions中
2. cancel_session
根据session_id取消对应协程
3. list_all_sessions
查看所有协程信息

协程管理原则：
1. 协程创建后，任务信息保存在self.sessions中
2. 协程取消后，对应信息需要同步在self.sessions中
3. 某一协程结束后，会调用notify方法，通过MessageHandler将消息发送出去
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from enum import Enum
from typing import Dict, List

from openjiuwen.core.runner import Runner
from openjiuwen.core.single_agent import ReActAgent, ReActAgentConfig, AgentCard
from pydantic import BaseModel

from openjiuwen.core.foundation.tool import LocalFunction, Tool, ToolCard

from jiuwenclaw.agentserver.tools.mcp_toolkits import get_mcp_tools
from jiuwenclaw.gateway.message_handler import MessageHandler
from jiuwenclaw.schema import AgentResponseChunk
from jiuwenclaw.schema.message import EventType, Message
from jiuwenclaw.utils import format_session_log

logger = logging.getLogger(__name__)


class Status(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    ERROR = "error"


class SessionTask(BaseModel):
    session_id: str
    description: str
    status: Status
    result: str = ""


class MultiSessionToolkit:
    """Toolkit for multi-session agent task tracking. Supports parallel sub-agent execution."""

    def __init__(self, session_id: str, channel_id: str, request_id: str, sub_agent_config: ReActAgentConfig) -> None:
        """Initialize MultiSessionToolkit for a session.

        Args:
            session_id: Parent session/conversation identifier.
            channel_id: Channel ID for routing notify messages back to parent.
        """
        self.session_id = session_id
        self.channel_id = channel_id
        self.request_id = request_id
        self.sessions: List[SessionTask] = []
        self._tasks: Dict[str, asyncio.Task] = {}
        self._sub_agent_config: ReActAgentConfig = sub_agent_config
        logger.info(
            format_session_log(
                session_id,
                f"[MultiSessionToolkit] 初始化 channel_id={channel_id} request_id={request_id}",
            )
        )

    async def get_sub_agent(self) -> ReActAgent:
        """Create and return a sub-agent instance. Override in subclass."""
        logger.debug("[MultiSessionToolkit] get_sub_agent 创建子 agent")
        agent_card = AgentCard(
            name="spawn_sub_agent"
        )
        agent = ReActAgent(agent_card)
        agent.configure(self._sub_agent_config)
        mcp_tools = get_mcp_tools()
        for mcp_tool in mcp_tools:
            Runner.resource_mgr.add_tool(mcp_tool)
            agent.ability_manager.add(mcp_tool.card)
        logger.debug("[MultiSessionToolkit] get_sub_agent 完成 mcp_tools_count=%d", len(mcp_tools))
        return agent

    async def _run_and_notify(
        self,
        session_id: str,
        description: str,
        agent: ReActAgent,
        inputs: dict,
    ) -> None:
        """Run agent and call notify on completion (success/cancel/error)."""
        logger.debug(
            format_session_log(
                session_id,
                f"[MultiSessionToolkit] _run_and_notify 开始 "
                f"description={description[:80] + '...' if len(description) > 80 else description}",
            )
        )
        task = SessionTask(
            session_id=session_id,
            description=description,
            status=Status.RUNNING,
            result="",
        )
        self.sessions.append(task)

        try:
            result = await Runner.run_agent(agent, inputs)
            result_str = result.get("output", "") if isinstance(result, dict) else str(result)
            logger.info(
                format_session_log(
                    session_id,
                    f"[MultiSessionToolkit] 协程完成 status=completed result_len={len(result_str)}",
                )
            )
            self._update_session(session_id, Status.COMPLETED, result_str)
            await self.notify(session_id, Status.COMPLETED, result=result_str)
        except asyncio.CancelledError:
            logger.info(format_session_log(session_id, "[MultiSessionToolkit] 协程已取消"))
            self._update_session(session_id, Status.CANCELLED, "任务已取消")
            await self.notify(session_id, Status.CANCELLED)
            raise
        except Exception as e:
            err_str = str(e)
            logger.exception(
                format_session_log(session_id, f"[MultiSessionToolkit] 协程异常 error={err_str}")
            )
            self._update_session(session_id, Status.ERROR, err_str)
            await self.notify(session_id, Status.ERROR, error=err_str)
            raise
        finally:
            self._tasks.pop(session_id, None)
            logger.debug(
                format_session_log(
                    session_id,
                    f"[MultiSessionToolkit] _run_and_notify 结束 剩余协程数={len(self._tasks)}",
                )
            )

    def _update_session(self, session_id: str, status: Status, result: str = "") -> None:
        """Update session task status in self.sessions."""
        for st in self.sessions:
            if st.session_id == session_id:
                st.status = status
                st.result = result
                logger.debug(
                    format_session_log(
                        session_id,
                        f"[MultiSessionToolkit] _update_session status={status.value}",
                    )
                )
                break

    async def notify(
        self,
        session_id: str,
        status: Status,
        result: str = "",
        error: str = "",
    ) -> None:
        """Send subtask update to MessageHandler. Called on completion (success/cancel/error)."""
        try:
            mh = MessageHandler.get_instance()
        except RuntimeError as e:
            logger.warning(
                format_session_log(session_id, f"[MultiSessionToolkit] MessageHandler 未初始化，跳过 notify: {e}")
            )
            return

        st = next((s for s in self.sessions if s.session_id == session_id), None)
        description = st.description if st else ""
        index = next((i for i, s in enumerate(self.sessions) if s.session_id == session_id), 0)
        total = len(self.sessions)

        # 前端 SubtaskStatus: 'completed' | 'error'，cancelled 映射为 error
        if status == Status.COMPLETED:
            payload_status = "completed"
            message = result or ""
        else:
            payload_status = "error"
            message = error or "任务已取消" if status == Status.CANCELLED else error
        payload = {
            "event_type": "chat.session_result",
            "session_id": session_id,
            "description": description,
            "status": payload_status,
            "index": index + 1,
            "total": total,
            "result": message,
            "is_parallel": True,
        }
        msg = {
            "request_id": self.request_id,
            "channel_id": self.channel_id,
            "session_id": self.session_id,
            "payload": payload,
            "is_complete": False,
        }
        logger.debug(
            format_session_log(
                session_id,
                f"[MultiSessionToolkit] notify 发送 subtask_update status={payload_status} index={index + 1}/{total}",
            )
        )
        from jiuwenclaw.agentserver.agent_ws_server import AgentWebSocketServer
        server = AgentWebSocketServer.get_instance()
        await server.send_push(msg)

        if self.all_tasks_done():
            session_result_summary = "后台会话任务均已完成：\n"
            for st in self.sessions:
                session_result_summary += (f"\nsession_id: {st.session_id}\n"
                                           f"description: {st.description}\nresult: {st.result}\n")
            inputs = {
                "conversation_id": self.session_id,
                "query": json.dumps({
                    "source": "system",
                    "content": session_result_summary,
                    "type": "notify"
                }),
            }
            # 使用 run_agent_streaming 而非 run_agent，以确保 session.post_run() 被调用，
            # 从而将对话历史持久化到 checkpoint。run_agent 不会创建 Session 或调用 post_run，
            # 导致 notify 中的 agent 对话未保存。
            accumulated: list[str] = []
            final_output: str | None = None
            async for chunk in Runner.run_agent_streaming(
                server.get_agent(),
                inputs=inputs,
            ):
                if not hasattr(chunk, "type") or not hasattr(chunk, "payload"):
                    continue
                payload = chunk.payload if isinstance(chunk.payload, dict) else {}
                if chunk.type == "content_chunk":
                    c = payload.get("content", "")
                    if c:
                        accumulated.append(str(c))
                elif chunk.type == "answer":
                    out = payload.get("output")
                    if isinstance(out, dict):
                        temp = out.get("output", str(out)) or "".join(accumulated)
                        if temp != "":
                            final_output = temp
                    elif out is not None:
                        final_output = str(out)
                    else:
                        final_output = "".join(accumulated) if accumulated else ""
            result = {
                "output": final_output if final_output is not None else "".join(accumulated),
                "result_type": "answer",
            }
            payload = {
                "event_type": "chat.final",
                "task_id": self.session_id,
                "content": result,
            }
            msg = {
                "request_id": self.request_id,
                "channel_id": self.channel_id,
                "session_id": self.session_id,
                "payload": payload,
                "is_complete": True,
            }
            await server.send_push(msg)

    async def create_new_sessions(self, task_descriptions: List[str]) -> str:
        """Create sub-agent sessions for each task description."""
        logger.info(
            format_session_log(
                self.session_id,
                f"[MultiSessionToolkit] create_new_sessions 开始 任务数={len(task_descriptions)}",
            )
        )
        created = []
        for i, task_description in enumerate(task_descriptions):
            session_id = f"spawn_{time.monotonic_ns()}_{secrets.token_hex(4)}"
            logger.debug(
                format_session_log(
                    session_id,
                    f"[MultiSessionToolkit] 创建协程 [{i + 1}/{len(task_descriptions)}] "
                    f"description={task_description[:60] + '...' if len(task_description) > 60 else task_description}",
                )
            )
            agent = await self.get_sub_agent()
            inputs = {
                "conversation_id": session_id,
                "query": task_description,
            }
            coro = self._run_and_notify(session_id, task_description, agent, inputs)
            task = asyncio.create_task(coro)
            self._tasks[session_id] = task
            created.append(session_id)
        logger.info(
            "[MultiSessionToolkit] create_new_sessions 完成 已创建 %d 个协程: %s",
            len(created),
            ", ".join(created),
        )
        return f"已创建 {len(created)} 个协程: {', '.join(created)}"

    async def cancel_session(self, session_id: str) -> str:
        """Cancel a running session by session_id."""
        logger.info(
            format_session_log(
                session_id,
                f"[MultiSessionToolkit] cancel_session 请求 parent_session_id={self.session_id}",
            )
        )
        task = self._tasks.get(session_id)
        if task is None:
            logger.warning(
                format_session_log(
                    session_id,
                    f"[MultiSessionToolkit] cancel_session 未找到 当前协程: {list(self._tasks.keys())}",
                )
            )
            return f"未找到 session_id={session_id}"
        if task.done():
            logger.info(format_session_log(session_id, "[MultiSessionToolkit] cancel_session 已结束，无需取消"))
            return f"session_id={session_id} 已结束"
        task.cancel()
        try:
            await asyncio.gather(task, return_exceptions=True)
        except asyncio.CancelledError:
            pass
        logger.info(format_session_log(session_id, "[MultiSessionToolkit] cancel_session 已取消"))
        return f"已取消 session_id={session_id}"

    async def cancel_all_sessions(self) -> str:
        """取消本工具包下所有仍在运行的子 agent 会话。"""
        active_session_ids = [
            session_id
            for session_id, task in list(self._tasks.items())
            if task is not None and not task.done()
        ]
        logger.info(
            format_session_log(
                self.session_id,
                f"[MultiSessionToolkit] cancel_all_sessions 请求 active_count={len(active_session_ids)}",
            )
        )
        if not active_session_ids:
            return "无运行中的协程"

        tasks = [self._tasks[session_id] for session_id in active_session_ids if session_id in self._tasks]
        for task in tasks:
            task.cancel()
        try:
            await asyncio.gather(*tasks, return_exceptions=True)
        except asyncio.CancelledError:
            pass
        logger.info(
            format_session_log(
                self.session_id,
                f"[MultiSessionToolkit] cancel_all_sessions 已取消 {len(active_session_ids)} 个协程",
            )
        )
        return f"已取消 {len(active_session_ids)} 个协程"

    async def list_all_sessions(self) -> str:
        """List all session tasks with status."""
        logger.debug(
            format_session_log(
                self.session_id,
                f"[MultiSessionToolkit] list_all_sessions 协程数={len(self.sessions)}",
            )
        )
        if not self.sessions:
            return "暂无协程"
        lines = []
        for st in self.sessions:
            lines.append(f"{st.session_id} | {st.description} | {st.status.value} | {st.result}")
        return "\n".join(lines)

    def get_tools(self) -> List[Tool]:
        """Return tools for registration in Runner."""
        session_id = self.session_id

        def make_tool(
            name: str,
            description: str,
            input_params: dict,
            func,
        ) -> Tool:
            card = ToolCard(
                name=name,
                description=description,
                input_params=input_params,
            )
            return LocalFunction(card=card, func=func)

        return [
            make_tool(
                name="session_new",
                description=(
                    "创建多个协程任务。接收任务描述列表，每个任务创建一个子 agent 并异步运行。"
                    "协程完成后会通过 notify 发送结果。"
                ),
                input_params={
                    "type": "object",
                    "properties": {
                        "task_descriptions": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "任务描述列表",
                        }
                    },
                    "required": ["task_descriptions"],
                },
                func=self.create_new_sessions,
            ),
            make_tool(
                name="session_cancel",
                description="根据 session_id 取消正在运行的协程。",
                input_params={
                    "type": "object",
                    "properties": {
                        "session_id": {
                            "type": "string",
                            "description": "要取消的协程 session_id",
                        }
                    },
                    "required": ["session_id"],
                },
                func=self.cancel_session,
            ),
            make_tool(
                name="session_list",
                description="查看所有协程列表及其状态（session_id | description | status | result）。",
                input_params={"type": "object", "properties": {}},
                func=self.list_all_sessions,
            ),
        ]

    def all_tasks_done(self) -> bool:
        """判断是否所有任务都已结束。"""
        return all([s.status in [Status.COMPLETED, Status.ERROR] for s in self.sessions])
