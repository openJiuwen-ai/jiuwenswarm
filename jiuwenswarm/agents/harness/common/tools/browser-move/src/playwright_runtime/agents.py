#!/usr/bin/env python
# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Agent builders for runtime and browser worker."""

from __future__ import annotations

import asyncio
import inspect
import os
from typing import Any

import anyio

from openjiuwen.core.common.logging import logger
from openjiuwen.core.foundation.tool import McpServerConfig
from openjiuwen.core.single_agent.agents.react_agent import ReActAgent, ReActAgentConfig
from openjiuwen.core.single_agent.schema.agent_card import AgentCard


def _resolve_tool_timeout_s(default_s: float = 180.0) -> float:
    raw = (
        os.getenv("PLAYWRIGHT_TOOL_TIMEOUT_S")
        or os.getenv("PLAYWRIGHT_MCP_TIMEOUT_S")
        or os.getenv("BROWSER_TIMEOUT_S")
        or str(default_s)
    )
    try:
        parsed = float(raw)
        if parsed > 0:
            return parsed
    except (TypeError, ValueError):
        pass
    return default_s


def _format_tool_names(tool_call: Any) -> str:
    if isinstance(tool_call, list):
        names = [getattr(item, "name", "") for item in tool_call]
        names = [name for name in names if name]
        return ", ".join(names) if names else "<unknown>"
    name = getattr(tool_call, "name", "")
    return name or "<unknown>"


_XIAOHONGSHU_PUBLISH_GUIDANCE = """平台特定行为示例：小红书网页版纯文字帖子发布。
仅当任务明确是在小红书发布帖子时才应用本示例。

【全局原则】
- 只在小红书创作或发布页面内操作，不进入首页推荐流、搜索、消息、个人主页或其他无关入口。
- 每一步先确认当前页面状态，再执行动作。
- 同一个动作最多重试 1 次；仍失败则立即停止，并汇报当前页面与失败原因。
- 不要连续点击同一个按钮。
- 只能选择一条路径执行；除非当前路径入口明确不存在，否则不要中途切换路径。
- 若出现登录、扫码、验证码、风控、人机验证，立即停止并请求人工接管。
- 若页面文案与预期明显不符，停止而不是猜测。

【内容一致性要求】
- 必须发布用户任务中已经提供、或上游流程已经明确生成好的最终文本内容。
- 严禁擅自改写为“测试文本”“示例文本”“占位文本”“体验文案”“默认文案”或任何临时内容。
- 严禁为了省事只输入部分内容、摘要内容、前几句内容，除非任务明确要求缩写或摘要。
- 如果任务里同时给出了标题和正文，必须分别写入对应字段，不要混淆。
- 如果任务里没有提供可发布的最终文本内容，就停止并明确说明“缺少可发布正文”，不要自行编造内容。
- 在输入完成后，应读取或检查输入框中的内容，确认其与目标文本核心内容一致，而不是测试文案或占位文案。

【文本格式要求】
- 输入正文时尽量保留原始段落结构、换行、列表、分段和语气，不要压成一整段。
- 若原文包含空行、段落分隔、项目符号或编号，输入时应尽可能保留，以保证发布后的可读性和美观度。
- 不要在输入过程中频繁删改、反复覆盖或多次重写同一段内容。
- 若页面输入框会吞掉换行，应在输入后检查实际显示结果；如换行丢失，只允许进行一次针对性的格式修正。
- 标题应保持简洁，不要把整段正文误填到标题里。
- 正文应完整，不要把标题重复粘贴多次，也不要在正文里加入无关说明，如“以下是正文”“测试发布”“帮你生成如下内容”等。

【路径选择规则】
- 如果能看到“上传图文”或“文字配图”，优先走路径 A。
- 如果路径 A 不可见，但能看到“写长文”或“新的创作”，走路径 B。
- 如果两条路径入口都看不到，立即停止并汇报“未发现可用发布入口”。

【路径 A：上传图文 -> 文字配图】
- 只点击“上传图文”一次，并等待页面切换。
- 然后只选择“文字配图”，不要误点普通图片上传、视频上传、模板或灵感内容。
- 在目标输入区一次性输入用于生成图片的完整文本，并确认文本确实已写入。
- 只点击与“生成图片”语义一致的主按钮一次，等待生成结果；若生成失败，最多重试 1 次。
- 一次性填写标题，并确认标题非空。
- 一次性填写正文或内容，并确认正文非空。
- 发布前必须确认：至少保留 1 张生成图片、标题非空、正文非空、当前仍在发布编辑页。
- 只点击一次“发布”，然后等待页面变化，不要再次点击。
- 若页面离开当前编辑页、进入作品页/内容管理页/列表页，或原编辑态消失，即判定任务完成。
- 若出现明确报错、审核提示、网络异常、风控，提取报错并停止。


【路径 B：写长文 -> 新的创作 -> 一键排版】
- 只点击“写长文”一次并等待变化；如无变化，最多重试 1 次。
- 然后只点击“新的创作”，不要进入历史草稿、模板或示例文章。
- 在主编辑区一次性粘贴完整正文，并确认正文确实已写入，再进行后续操作。
- 仅在正文存在时点击“一键排版”。
- 只选择默认风格或任务指定风格，不来回尝试多个风格。
- 只点击一次“下一步”，如未跳转最多重试 1 次，并等待进入发布信息页。
- 填写标题并确认非空；只有当最终页明确存在且为空的内容或摘要字段时，才填写该字段。
- 发布前必须确认：长文正文存在、标题非空、排版已完成、当前页面存在“发布”按钮。
- 只点击一次“发布”，然后等待页面变化，不要再次点击。
- 若页面离开当前编辑页、进入作品页/内容管理页/列表页，或原编辑态消失，即判定任务完成。
- 若出现明确报错、审核提示、网络异常、风控，提取报错并停止。

【高价值关键词】
上传图文、文字配图、输入文字生成图片、写长文、新的创作、一键排版、下一步、标题、内容、正文、发布。
若这些关键词不存在，不要猜测相似按钮。

【发布结果判定规则】
- 小红书网页版在点击“发布”后，可能不会出现明显的“发布成功”弹窗或强提示。
- 因此，不能把“没有成功弹窗”直接判断为发布失败。
- 点击“发布”后，只要出现以下任一信号，即可判定为发布成功：
  1. 页面跳转离开当前编辑页
  2. 当前编辑态消失，无法继续看到原来的标题/正文编辑框
  3. “发布”按钮消失、变灰后不再恢复，且页面进入新的内容页、作品页、管理页或列表页
  4. 页面出现与“作品”“笔记”“内容管理”“创作中心”“发布管理”“我的内容”相关的结果页
  5. 新发布内容出现在作品列表、内容列表或笔记列表中
- 如果点击“发布”后页面进入加载、跳转、刷新或编辑页退出，也应优先视为成功后的正常流转，而不是立即判定失败。
- 只有在页面明确出现报错、网络异常、审核拦截、风控拦截、权限不足，或仍然停留在原编辑页且“发布”按钮恢复可点击时，才判定为未成功。
- 若点击“发布”后未看到明显成功弹窗，不要重复点击“发布”；应先观察页面是否已离开编辑态或进入作品/管理相关页面。

【失败时的固定输出格式】
- 当前路径：
- 当前步骤：
- 当前页面可见关键词：
- 已执行动作：
- 失败原因判断：
- 需要人工处理事项："""



def _is_xiaohongshu_publish_task(task: str) -> bool:
    text = (task or "").strip().lower()
    if not text:
        return False
    platform_markers = ("小红书", "xiaohongshu", "xhs")
    publish_markers = ("发布", "发帖", "帖子", "笔记", "创作")

    return any(marker in text for marker in platform_markers) and any(
        marker in text for marker in publish_markers
    )


def augment_browser_task_prompt(task: str) -> str:
    base = (task or "").strip()
    if not _is_xiaohongshu_publish_task(base):
        return base
    return (
        f"{base}\n\n{_XIAOHONGSHU_PUBLISH_GUIDANCE}\n\n"
        "Execution requirement: if this Xiaohongshu publish task reaches a "
        "valid final 发布 button and there is no explicit blocking "
        "verification prompt, you must click 发布 directly instead of asking "
        "the user to do it manually.\n"
        "Content requirement: you must publish the exact user-provided or "
        "already-generated final text content, preserve intended paragraph "
        "breaks as much as the editor allows, and never replace it with test "
        "text, placeholder text, or abbreviated sample text."
    )


def _build_main_agent_system_prompt(default_timeout_s: float) -> str:
    timeout_text = f"{int(default_timeout_s)}" if default_timeout_s.is_integer() else f"{default_timeout_s:.1f}"
    return (
        "You are the main orchestration agent.\n"
        "For browser tasks, prefer browser_run_task.\n"
        "Default to one comprehensive browser_run_task call per user request.\n"
        "Do not split work into many small browser_run_task calls unless a prior browser result shows "
        "a concrete blocking error that requires a narrower retry.\n"
        "Reuse the same session_id across retries to preserve browser continuity.\n"
        f"Use a long browser timeout. Do not pass timeout_s below {timeout_text}s. "
        "Prefer omitting timeout_s so the default long timeout is used.\n"
        "When a request is not straightforward and needs custom logic, call browser_custom_action first.\n"
        "If action names or params are unclear, call browser_list_custom_actions first and "
        "then call browser_custom_action with the matching action and params.\n"
        "Do not simulate browser actions yourself.\n"
        "For explicit user-authorized publishing, posting, sending, or "
        "submitting tasks, you are expected to complete the final "
        "Publish/Post/Send/Submit click yourself once the required fields are "
        "valid.\n"
        "Do not stop for generic account-safety concerns. Only stop when the "
        "website explicitly shows a blocking login, captcha, risk-control, "
        "security verification, permission gate, or other manual-review "
        "requirement.\n"
        "If the final publish or submit button is visible, enabled, and all "
        "required prechecks pass, click it exactly once and wait for the "
        "result.\n"
        "Pass through the full user goal clearly as browser task text.\n"
        "Keep user-facing answer concise and factual.\n"
        "If a browser tool returns an error, report it explicitly."
    )


def ensure_execute_signature_compat(agent: ReActAgent) -> None:
    """Adapt execute signature and add a timeout watchdog around tool execution."""
    execute_fn = getattr(agent.ability_manager, "execute", None)
    if execute_fn is None:
        return
    if getattr(execute_fn, "_playwright_timeout_wrapped", False):
        return

    try:
        params = inspect.signature(execute_fn).parameters
    except (TypeError, ValueError):
        return

    original_execute = execute_fn
    supports_tag = "tag" in params
    tool_timeout_s = _resolve_tool_timeout_s()

    async def execute_with_tag(tool_call, session, tag=None):
        tool_names = _format_tool_names(tool_call)
        try:
            with anyio.fail_after(tool_timeout_s):
                if supports_tag:
                    return await original_execute(tool_call, session, tag=tag)
                return await original_execute(tool_call, session)
        except TimeoutError as exc:
            logger.error(
                f"Tool execution timed out after {tool_timeout_s:.1f}s; tools={tool_names}"
            )
            raise RuntimeError(
                f"tool_execution_timeout: tools={tool_names}, timeout_s={tool_timeout_s:.1f}"
            ) from exc

    agent.ability_manager.execute = execute_with_tag
    setattr(agent.ability_manager.execute, "_playwright_timeout_wrapped", True)


def build_browser_worker_agent(
    provider: str,
    api_key: str,
    api_base: str,
    model_name: str,
    mcp_cfg: McpServerConfig,
    max_steps: int,
    screenshot_subdir: str = "screenshots",
) -> ReActAgent:
    screenshot_subdir = (
        (screenshot_subdir or "screenshots").strip().replace("\\", "/").strip("/") or "screenshots"
    )
    card = AgentCard(
        id="agent.playwright.browser_worker",
        name="playwright_browser_worker",
        description="Browser worker that executes web tasks using Playwright MCP tools.",
        input_params={},
    )
    agent = ReActAgent(card=card).configure(
        ReActAgentConfig()
        .configure_model_client(
            provider=provider,
            api_key=api_key,
            api_base=api_base,
            model_name=model_name,
        )
        .configure_max_iterations(max_steps)
        .configure_prompt_template(
            [
                {
                    "role": "system",
                    "content": (
                        "You are a browser worker agent.\n"
                        "Execute browser tasks step-by-step with Playwright MCP tools only.\n"
                        "Before interacting, ensure page or selector readiness.\n"
                        "Keep actions targeted and avoid unnecessary page snapshots.\n"
                        "If actions repeatedly fail, stop and report the exact failing action.\n"
                        "When the user has explicitly asked to publish, post, "
                        "send, or submit content, you should directly perform "
                        "the final click yourself once required fields are "
                        "valid.\n"
                        "Do not hand the last step back to the user because of "
                        "generic account-safety concerns. Stop only for explicit "
                        "blocking login, captcha, risk-control, security "
                        "verification, permission, or human-takeover "
                        "requirements shown by the site.\n"
                        "IMPORTANT: Do NOT use browser_take_screenshot unless strictly necessary. "
                        f"If a screenshot is needed, always save it under '{screenshot_subdir}/'. "
                        "Use browser_run_code with: "
                        f"async (page) => {{ await page.screenshot({{ path: '{screenshot_subdir}/screenshot.png' }}); "
                        f"return '{screenshot_subdir}/screenshot.png'; }}\n"
                        "Final output MUST be a single JSON object with keys:\n"
                        "ok (boolean), final (string), page (object with url and title), "
                        "screenshot (string|null), error (string|null).\n"
                        "Do not output markdown."
                    ),
                }
            ]
        )
    )
    agent.ability_manager.add(mcp_cfg)
    ensure_execute_signature_compat(agent)
    return agent


def build_main_agent(
    provider: str,
    api_key: str,
    api_base: str,
    model_name: str,
    browser_tool_card,
    custom_action_tool_card=None,
    list_actions_tool_card=None,
) -> ReActAgent:
    default_timeout_s = _resolve_tool_timeout_s()
    card = AgentCard(
        id="agent.playwright.main_runtime",
        name="playwright_main_runtime",
        description="Main runtime agent that delegates browser work to browser_run_task.",
        input_params={},
    )
    agent = ReActAgent(card=card).configure(
        ReActAgentConfig()
        .configure_model_client(
            provider=provider,
            api_key=api_key,
            api_base=api_base,
            model_name=model_name,
        )
        .configure_max_iterations(25)
        .configure_prompt_template(
            [
                {
                    "role": "system",
                    "content": _build_main_agent_system_prompt(default_timeout_s),
                }
            ]
        )
    )
    agent.ability_manager.add(browser_tool_card)
    if custom_action_tool_card is not None:
        agent.ability_manager.add(custom_action_tool_card)
    if list_actions_tool_card is not None:
        agent.ability_manager.add(list_actions_tool_card)
    ensure_execute_signature_compat(agent)
    return agent
