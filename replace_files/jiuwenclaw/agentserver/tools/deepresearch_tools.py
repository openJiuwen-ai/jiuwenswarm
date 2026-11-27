# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
from __future__ import annotations

import contextvars
import json

from openjiuwen.core.foundation.tool import tool

from jiuwenclaw.agentserver.tools.deepresearch_task_manager import DeepResearchTaskManager
from jiuwenclaw.config import get_config

# 使用 contextvars
_deepresearch_route_ctx: contextvars.ContextVar[dict[str, str] | None] = contextvars.ContextVar(
    "jiuwenclaw_deepresearch_route", default=None
)



def push_deepresearch_route(request_id: str, channel_id: str, session_id: str) -> contextvars.Token:
    """设置 DeepResearch 路由上下文。
    
    Args:
        request_id: 请求 ID
        channel_id: 渠道 ID
        session_id: 会话 ID
        
    Returns:
        contextvars.Token，用于恢复上下文
    """
    return _deepresearch_route_ctx.set({
        "request_id": request_id,
        "channel_id": channel_id,
        "session_id": session_id,
    })


def reset_deepresearch_route(token: contextvars.Token) -> None:
    """恢复 DeepResearch 路由上下文。
    
    Args:
        token: contextvars.Token
    """
    _deepresearch_route_ctx.reset(token)


def _get_route() -> dict[str, str]:
    """获取当前路由上下文。
    
    Returns:
        包含 request_id、channel_id、session_id 的字典
    """
    route = _deepresearch_route_ctx.get()
    return route if route is not None else {"request_id": "", "channel_id": "", "session_id": ""}


@tool(
    name="deepresearch_create_task",
    description=(
        "创建深度研究任务，生成独立的长文研究报告。"
        "适用场景：独立的深度研究报告生成、全面市场调研、行业分析报告、政策解读报告。"
        "任务将在后台异步运行且耗时长，完成后通过 WebSocket 推送结果。"
        "返回任务 ID，可用于后续查询状态、取消任务或获取结果，但由于任务执行时间较长，创建后无需立刻查询。"
        "⚠不适用场景：PPT制作辅助研究或准备内容素材、单点数据查询、快速搜索"
    ),
)
async def deepresearch_create_task(
    query: str,
    file_name: str,
) -> str:
    """创建 DeepResearch 任务.

    Args:
        query: 研究查询
        file_name: 报告文件名，不带后缀

    Returns:
        任务 ID
    """
    manager = await DeepResearchTaskManager.get_instance()
    route = _get_route()
    task_id = await manager.create_task(
        query=query,
        file_name=file_name,
        session_id=route.get("session_id", ""),
        channel_id=route.get("channel_id", ""),
        request_id=route.get("request_id", ""),
    )

    # 任务已在 create_task() 返回前写入 _tasks，无需等待
    return f"已创建 DeepResearch 任务，任务 ID: {task_id}"


@tool(
    name="deepresearch_get_status",
    description=(
        "查询 DeepResearch 任务的状态。"
        "DeepResearch任务是一个长时间的任务，建议不要频繁查询，以避免对系统造成过大压力。"
        "返回任务的详细信息，包括状态、创建时间、开始时间、完成时间等。"
    ),
)
async def deepresearch_get_status(task_id: str) -> str:
    """获取任务状态.

    Args:
        task_id: 任务 ID

    Returns:
        任务状态信息（JSON 格式字符串）
    """
    manager = await DeepResearchTaskManager.get_instance()
    route = _get_route()
    task_info = await manager.get_task_status(
        task_id,
        caller_session_id=route.get("session_id", ""),
        caller_channel_id=route.get("channel_id", ""),
    )

    if task_info is None:
        return f"未找到任务 ID: {task_id} 或无权访问"

    return json.dumps(task_info, ensure_ascii=False, indent=2)


@tool(
    name="deepresearch_list_tasks",
    description=(
        "列出所有 DeepResearch 任务。"
        "支持按状态过滤（running/completed/cancelled/error）。"
        "返回任务列表，按创建时间倒序排列。"
    ),
)
async def deepresearch_list_tasks(status: str = "") -> str:
    """列出当前会话的任务.

    Args:
        status: 可选的状态过滤器

    Returns:
        任务列表（JSON 格式字符串）
    """
    manager = await DeepResearchTaskManager.get_instance()
    route = _get_route()
    status_filter = status if status else None
    tasks = await manager.list_tasks(
        status_filter=status_filter,
        caller_session_id=route.get("session_id", ""),
        caller_channel_id=route.get("channel_id", ""),
    )

    if not tasks:
        return "暂无 DeepResearch 任务"

    return json.dumps(tasks, ensure_ascii=False, indent=2)


@tool(
    name="deepresearch_cancel_task",
    description=(
        "取消正在运行的 DeepResearch 任务。"
        "取消后任务状态将变为 cancelled，已生成的内容将被保留。"
        "用于取消不需要的独立研究报告任务。"
    ),
)
async def deepresearch_cancel_task(task_id: str) -> str:
    """取消任务.

    Args:
        task_id: 任务 ID

    Returns:
        操作结果
    """
    manager = await DeepResearchTaskManager.get_instance()
    route = _get_route()
    success = await manager.cancel_task(
        task_id,
        caller_session_id=route.get("session_id", ""),
        caller_channel_id=route.get("channel_id", ""),
    )

    if success:
        return f"已取消任务 ID: {task_id}"
    return f"取消任务失败，任务不存在、已完成或无权访问: {task_id}"


@tool(
    name="deepresearch_get_result",
    description=(
        "获取已完成任务的详细结果。"
        "如果任务未完成，返回提示信息。"
    ),
)
async def deepresearch_get_result(task_id: str) -> str:
    """获取任务结果.

    Args:
        task_id: 任务 ID

    Returns:
        任务结果
    """
    manager = await DeepResearchTaskManager.get_instance()
    route = _get_route()
    result = await manager.get_task_result(
        task_id,
        caller_session_id=route.get("session_id", ""),
        caller_channel_id=route.get("channel_id", ""),
    )

    if result is None:
        task_info = await manager.get_task_status(
            task_id,
            caller_session_id=route.get("session_id", ""),
            caller_channel_id=route.get("channel_id", ""),
        )
        if task_info:
            return f"任务 {task_id} 尚未完成，当前状态: {task_info['status']}"
        else:
            return f"未找到任务 ID: {task_id} 或无权访问"

    return result


@tool(
    name="deepresearch_run_task",
    description=(
        "执行深度研究任务，并阻塞等待生成独立的长文研究报告。"
        "适用场景：独立的深度研究报告生成、全面市场调研、行业分析报告、政策解读报告。"
        "与异步版本的区别：不提交到后台任务池，直接在当前协程执行，阻塞等待完成后返回结果。"
        "执行时间较长（通常数分钟），会阻塞当前 Agent 会话直至完成。"
        "⚠不适用场景：PPT制作辅助研究、PPT准备内容素材、单点数据查询、快速搜索"
    ),
)
async def deepresearch_run_task(
    query: str,
    file_name: str,
) -> str:
    """阻塞执行 DeepResearch 任务并返回结果.

    与 deepresearch_create_task 的区别：
    - 不创建后台任务，直接在当前协程执行
    - 不返回任务 ID，直接返回报告保存路径
    - 阻塞等待完成，适合需要即时获取结果的场景
    - 不受任务池资源限制

    Args:
        query: 研究查询
        file_name: 报告文件名，不带后缀

    Returns:
        报告保存路径信息字符串
    """
    manager = await DeepResearchTaskManager.get_instance()
    route = _get_route()
    result = await manager.run_task_direct(
        query=query,
        file_name=file_name,
        session_id=route.get("session_id", ""),
        channel_id=route.get("channel_id", ""),
        request_id=route.get("request_id", ""),
    )
    return result


def enable_deepresearch() -> bool:
    """检查 DeepResearch 工具是否启用.

    Returns:
        是否启用 DeepResearch 工具
    """

    try:
        cfg = get_config()
        if not bool(cfg.get("enable_deepresearch", True)):
            return False
        return True
    except Exception:
        return False


def get_deepresearch_tools() -> list:
    """获取 DeepResearch 工具列表.

    Returns:
        工具列表
    """
    if not enable_deepresearch():
        return []
    return [
        deepresearch_run_task,
    ]


__all__ = [
    "deepresearch_create_task",
    "deepresearch_get_status",
    "deepresearch_list_tasks",
    "deepresearch_cancel_task",
    "deepresearch_get_result",
    "deepresearch_run_task",
    "get_deepresearch_tools",
]
