# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Prompt rail：把已安装三方 Agent 状态注入系统提示。

让模型在新会话中无需先调 list_third_agents 即可感知哪些三方 Agent
已安装、运行状态与访问地址，从而能直接调用管理工具或向用户陈述状态。
状态来源与聊天工具/web RPC 一致（third_agent_toolkits 读同一状态文件）。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.prompts import PromptSection
from openjiuwen.harness.rails.base import DeepAgentRail

from jiuwenswarm.agents.harness.common.tools.third_agent_toolkits import (
    list_installed_third_agents,
)

logger = logging.getLogger(__name__)


def render_third_agent_prompt(language: str = "cn") -> str:
    """渲染已安装三方 Agent 段落；无已安装 Agent 时返回空串（不注入）。"""
    items = list_installed_third_agents()
    if not items:
        return ""
    zh = language != "en"
    lines = ["已安装的三方 Agent（本地进程）：" if zh else
             "Installed third-party agents (local processes):"]
    for item in items:
        if zh:
            status_text = "运行中" if item["status"] == "running" else "已停止"
        else:
            status_text = item["status"]
        lines.append(f"- {item['display_name']}（{status_text}）：{item['url']}")
    lines.append(
        "可用 install_third_agent / list_third_agents / uninstall_third_agent 管理它们；"
        "已停止的 Agent 可通过 install_third_agent 重新启动。"
        "安装时可通过 user 参数指定用户管理中的用户，用其模型凭据替换 "
        "start_cmd 中的 {api_key}/{api_base}/{model} 占位符。"
        if zh else
        "Manage them via install_third_agent / list_third_agents / uninstall_third_agent; "
        "a stopped agent can be restarted with install_third_agent. "
        "Pass `user` (a user from the user management UI) to install_third_agent to "
        "replace the {api_key}/{api_base}/{model} placeholders in start_cmd with "
        "that user's model credentials."
    )
    return "\n".join(lines)


class ThirdAgentPromptRail(DeepAgentRail):
    """Inject installed third-party agent status into the system prompt."""

    # 不隐藏/改写其他 section，add_section 同名幂等覆盖，执行顺序无关，取中档值。
    priority = 50
    SECTION_NAME = "third_agents"
    SECTION_PRIORITY = 42

    def __init__(self) -> None:
        super().__init__()
        self.system_prompt_builder = None

    def init(self, agent: Any) -> None:
        self.system_prompt_builder = getattr(agent, "system_prompt_builder", None)

    def uninit(self, agent: Any) -> None:
        if self.system_prompt_builder is not None:
            self.system_prompt_builder.remove_section(self.SECTION_NAME)
        self.system_prompt_builder = None

    async def before_model_call(self, ctx: AgentCallbackContext) -> None:
        agent = getattr(ctx, "agent", None)
        if agent is not None and self.system_prompt_builder is None:
            self.system_prompt_builder = getattr(agent, "system_prompt_builder", None)
        if self.system_prompt_builder is None:
            return

        language = getattr(self.system_prompt_builder, "language", "cn") or "cn"
        try:
            # 状态探测含端口连接，放到线程里避免阻塞事件循环。
            content = await asyncio.to_thread(render_third_agent_prompt, language)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[ThirdAgentPromptRail] render failed: %s", exc)
            content = ""

        if not content.strip():
            self.system_prompt_builder.remove_section(self.SECTION_NAME)
            return

        self.system_prompt_builder.add_section(
            PromptSection(
                name=self.SECTION_NAME,
                content={language: content},
                priority=self.SECTION_PRIORITY,
            )
        )


__all__ = ["ThirdAgentPromptRail", "render_third_agent_prompt"]
