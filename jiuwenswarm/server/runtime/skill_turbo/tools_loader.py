# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""SkillTurbo 工具装载器。

设计目标：
- 以最小成本让 SkillTurbo 复用 jiuwenclaw + openjiuwen 已有工具；
- 不抽公共层、不改 DeepAgent、不改 openjiuwen；
- 工具一律「逐个 import + 一个 loader 函数」；
- 可用性判断 100% 复用上游已有逻辑（``is_xxx_enabled`` / 工厂自身分支 /
  ``_get_tool_cards`` 同款 if 开关），本模块不引入新的可用性算法。

接入方式：::

    ctx = ToolLoaderContext(agent_id="skill_turbo", language="zh", sys_operation=op)
    for tool in await load_all(ctx):
        env.register_tool(tool.card)
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from openjiuwen.core.runner import Runner

logger = logging.getLogger(__name__)


# ────────────────────────── Context ──────────────────────────


@dataclass
class ToolLoaderContext:
    """工具装载上下文。

    字段命名与 ``JiuWenClawDeepAdapter._get_tool_cards`` 内部使用的开关保持对齐，
    便于 1:1 平移可用性条件。
    """

    agent_id: str = "skill_turbo"
    language: str = "zh"
    sys_operation: Any | None = None
    vision_model_config: Any | None = None
    audio_model_config: Any | None = None
    video_model_enabled: bool = False
    image_gen_enabled: bool = False
    skill_manager: Any | None = None
    request_id: str = ""
    session_id: str = ""
    channel_id: str = ""
    request_metadata: dict[str, Any] | None = None
    extra: dict[str, Any] = field(default_factory=dict)


# ────────────────────────── 统一注册流程 ──────────────────────────


LoaderFn = Callable[[ToolLoaderContext], Iterable[Any]]


def _register_group(
    out_tools: list[Any],
    ctx: ToolLoaderContext,
    loader: LoaderFn,
    group: str,
) -> None:
    """统一执行单个分组 loader：try/except + 去重 + 日志。

    异常隔离：单组失败不影响其他组（沿用 ``_get_tool_cards`` 的同款套路）。
    """
    try:
        tools = list(loader(ctx))
    except Exception as exc:
        logger.warning(
            "[ToolsLoader] group=%s build failed: %s", group, exc, exc_info=True
        )
        return

    registered = 0
    for tool in tools:
        try:
            if tool is None:
                continue
            card = getattr(tool, "card", None)
            tool_id = getattr(card, "id", None) if card is not None else None
            if not tool_id:
                logger.warning(
                    "[ToolsLoader] group=%s skip tool without card.id: %r",
                    group,
                    tool,
                )
                continue
            if Runner.resource_mgr.get_tool(tool_id) is None:
                Runner.resource_mgr.add_tool(tool)
            out_tools.append(tool)
            registered += 1
        except Exception as exc:
            logger.warning(
                "[ToolsLoader] group=%s tool register failed: %s",
                group,
                exc,
                exc_info=True,
            )
    if registered:
        logger.info(
            "[ToolsLoader] group=%s registered=%d total=%d",
            group,
            registered,
            len(out_tools),
        )


async def load_all(ctx: ToolLoaderContext) -> list[Any]:
    """加载所有可用工具。

    各分组独立装载，相互隔离；返回最终成功装载的工具实例列表。
    """
    tools: list[Any] = []

    # ─── jiuwenclaw 工具 ─────────────────────────────────────
    _register_group(tools, ctx, _load_jw_named_web, "named_web")
    _register_group(tools, ctx, _load_jw_vision, "vision")
    _register_group(tools, ctx, _load_jw_audio, "audio")
    _register_group(tools, ctx, _load_jw_video, "video")
    _register_group(tools, ctx, _load_jw_image_gen, "image_gen")
    _register_group(tools, ctx, _load_jw_skill_toolkit, "skill_toolkit")
    _register_group(tools, ctx, _load_jw_ask_user, "ask_user")
    _register_group(tools, ctx, _load_jw_deepresearch, "deepresearch")

    # ─── openjiuwen 工具 ─────────────────────────────────────
    _register_group(tools, ctx, _load_oj_filesystem, "filesystem")
    _register_group(tools, ctx, _load_oj_bash, "bash")

    logger.info("[ToolsLoader] load_all done total=%d", len(tools))
    return tools


def load_send_file_tools(ctx: ToolLoaderContext) -> list[Any]:
    """加载 send_file_to_user 工具（每次请求单独刷新，不走 load_all 一次性缓存）。"""
    try:
        return list(_load_jw_send_file(ctx))
    except Exception as exc:
        logger.warning(
            "[ToolsLoader] load_send_file_tools failed: %s", exc, exc_info=True
        )
        return []


# ────────────────────────── jiuwenclaw loaders ──────────────────────────


def _load_jw_named_web(ctx: ToolLoaderContext) -> list[Any]:
    """jiuwenclaw 命名 Web 工具：web_search / fetch_webpage。"""
    from jiuwenswarm.agents.harness.common.tools.harness_named_web_tools import (
        build_jiuwen_harness_named_web_tools,
    )

    return list(
        build_jiuwen_harness_named_web_tools(
            agent_id=ctx.agent_id,
            language=ctx.language,
        )
    )


def _load_jw_vision(ctx: ToolLoaderContext) -> list[Any]:
    """视觉工具：vision_model_config 为空时跳过（沿用 _get_tool_cards 同款写法）。"""
    if ctx.vision_model_config is None:
        return []
    from openjiuwen.harness.tools import create_vision_tools

    return list(
        create_vision_tools(
            language=ctx.language,
            vision_model_config=ctx.vision_model_config,
            agent_id=ctx.agent_id,
        )
    )


def _load_jw_audio(ctx: ToolLoaderContext) -> list[Any]:
    """音频工具：create_audio_tools 内部已按 audio_model_config 过滤，本处不再判断。"""
    from openjiuwen.harness.tools import create_audio_tools

    return list(
        create_audio_tools(
            language=ctx.language,
            audio_model_config=ctx.audio_model_config,
            agent_id=ctx.agent_id,
        )
    )


def _load_jw_video(ctx: ToolLoaderContext) -> list[Any]:
    """视频理解：video_model_enabled 开关（沿用 _get_tool_cards 同款写法）。"""
    if not ctx.video_model_enabled:
        return []
    from jiuwenswarm.agents.harness.common.tools.video_tools import video_understanding

    return [video_understanding]


def _load_jw_image_gen(ctx: ToolLoaderContext) -> list[Any]:
    """图像生成：image_gen_enabled 开关（沿用 _get_tool_cards 同款写法）。"""
    if not ctx.image_gen_enabled:
        return []
    from jiuwenswarm.agents.harness.common.tools.image_tools import generate_image

    return [generate_image]


def _load_jw_skill_toolkit(ctx: ToolLoaderContext) -> list[Any]:
    """SkillToolkit：需要 skill_manager（沿用 _get_tool_cards 同款写法）。"""
    if ctx.skill_manager is None:
        return []
    from jiuwenswarm.agents.harness.common.tools import SkillToolkit

    return list(SkillToolkit(manager=ctx.skill_manager).get_tools())


def _load_jw_ask_user(ctx: ToolLoaderContext) -> list[Any]:
    """AskUserQuestion：始终注册（沿用 _get_tool_cards 同款写法）。"""
    try:
        from jiuwenswarm.agents.harness.common.tools.ask_user_question_tool import (
            get_ask_user_question_tool,
        )
    except ImportError:
        get_ask_user_question_tool = None
    if get_ask_user_question_tool is None:
        return []
    return [get_ask_user_question_tool()]


def _load_jw_deepresearch(ctx: ToolLoaderContext) -> list[Any]:
    """DeepResearch 工具集：始终注册（沿用 _get_tool_cards 同款写法）。"""
    try:
        from jiuwenswarm.agents.harness.common.tools.deepresearch_tools import (
            get_deepresearch_tools,
        )
    except ImportError:
        get_deepresearch_tools = None
    if get_deepresearch_tools is None:
        return []
    return list(get_deepresearch_tools())


def _load_jw_send_file(ctx: ToolLoaderContext) -> list[Any]:
    """send_file_to_user：沿用 interface_deep 同款可用性判断。"""
    if not (ctx.request_id and ctx.session_id):
        return []

    channel = str(ctx.channel_id or "web").strip() or "web"
    try:
        from jiuwenswarm.common.config import get_config

        config_base = get_config()
    except Exception as exc:
        logger.warning("[ToolsLoader] send_file config load failed: %s", exc)
        return []

    send_file_enabled = (
        config_base.get("channels", {}).get(channel, {}).get("send_file_allowed", False)
    )
    send_file_channel_allowed = send_file_enabled or channel == "officeclaw"
    if not send_file_channel_allowed:
        return []

    from jiuwenswarm.agents.harness.common.tools import SendFileToolkit

    toolkit = SendFileToolkit(
        request_id=ctx.request_id,
        session_id=ctx.session_id,
        channel_id=channel,
        metadata=ctx.request_metadata,
    )
    return list(toolkit.get_tools())


# ────────────────────────── openjiuwen loaders ──────────────────────────


def _load_oj_filesystem(ctx: ToolLoaderContext) -> list[Any]:
    """文件系统工具：构造前提是 sys_operation 存在（jiuwenclaw 既有惯例）。"""
    if ctx.sys_operation is None:
        return []
    from openjiuwen.harness.tools.filesystem import (
        EditFileTool,
        GlobTool,
        GrepTool,
        ListDirTool,
        ReadFileTool,
        WriteFileTool,
    )

    common = dict(
        operation=ctx.sys_operation,
        language=ctx.language,
        agent_id=ctx.agent_id,
    )
    return [
        ReadFileTool(**common),
        WriteFileTool(**common),
        EditFileTool(**common),
        ListDirTool(**common),
        GlobTool(**common),
        GrepTool(**common),
    ]


def _load_oj_bash(ctx: ToolLoaderContext) -> list[Any]:
    """Bash 工具：构造前提是 sys_operation 存在。"""
    if ctx.sys_operation is None:
        return []
    from openjiuwen.harness.tools.shell.bash import BashTool

    return [
        BashTool(
            operation=ctx.sys_operation,
            language=ctx.language,
            agent_id=ctx.agent_id,
        )
    ]


__all__ = [
    "ToolLoaderContext",
    "load_all",
    "load_send_file_tools",
]
