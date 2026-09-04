#!/usr/bin/env python
"""自定义 Tool 示例：通过环境变量非侵入式注册（AGENT_EXTRA_TOOLS，仅企业版）。

使用方式：
    export AGENT_EXTRA_TOOLS=examples.custom_extension_tool
    # 多模块分号分隔：examples.custom_extension_tool;my.pkg.other_tools
    # 然后启动 AgentServer；env 变更需重启进程生效

部署契约（docs/zh/自定义Tool扩展设计.md §5.3）：
    1. 本模块及其第三方依赖须预装在 AgentServer 运行时 Python 环境
       （与 AGENT_EXTRA_RAILS 扩展同一部署链路）；
    2. register_tools() 返回模块级 @tool 单例——工具实例进程级共享，
       不要在工厂里新建实例；
    3. per-session 数据只经 contextvars 或工具参数传入，禁止模块级
       可变状态缓存会话数据（并发会话下会串数据）。

本模块演示两个工具：
    - query_system_api：渠道系统调用示例，演示安全契约——模型只能给
      system_code/api_code，下游地址由模块内白名单解析；禁止把裸 url
      暴露为模型可填参数（提示词注入可驱动服务端任意内网访问）。
    - get_current_user：用户信息示例，演示契约 3——当前用户经
      contextvars 注入读取（通常由客户自己的 AGENT_EXTRA_RAILS rail 在
      before_tool_call 时 set），工具侧只读，不落任何全局可变状态。
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
from typing import Any

from openjiuwen.core.foundation.tool import Tool, tool

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 契约 3：per-session 上下文只走 contextvar。注入点在调用链路（示例：客户
# 自定义 rail 的 before_tool_call 中 ``CURRENT_USER.set(run_context...)``），
# 工具侧只读。这里用 default="" 兜底，未注入时显式报错而不是串到别的会话。
# ---------------------------------------------------------------------------
CURRENT_USER: contextvars.ContextVar[str] = contextvars.ContextVar(
    "current_user", default=""
)

# 模块级【不可变】白名单：允许的 (system_code, api_code) -> 下游地址。
# 这是常量注册表，不是会话状态；真实集成里换成签名客户端配置。
SYSTEM_API_REGISTRY: dict[tuple[str, str], str] = {
    ("core", "query_account_balance"): "https://core.example.psbc/balance",
    ("core", "query_account_detail"): "https://core.example.psbc/detail",
    ("channel", "submit_transaction"): "https://channel.example.psbc/submit",
}

# 超时钳制：模型可填 timeout 参数，但不能给离谱值拖垮会话
_TIMEOUT_CLAMP_S = (1, 60)


@tool(
    name="query_system_api",
    description=(
        "通过渠道系统调用第三方系统 API。传入 system_code 与 api_code，"
        "返回下游系统响应 JSON 文本。仅支持白名单内的系统与接口。"
    ),
)
async def query_system_api(
    system_code: str,
    api_code: str,
    payload: str = "{}",
    timeout: int = 10,
) -> str:
    """调用白名单内的第三方系统接口。

    Args:
        system_code: 目标系统编码，如 core / channel。
        api_code: 接口编码，如 query_account_balance。
        payload: 业务请求报文（JSON 字符串）。
        timeout: 超时秒数，范围 1-60，超范围自动钳制。
    """
    url = SYSTEM_API_REGISTRY.get((system_code, api_code))
    if url is None:
        # 白名单外直接拒绝，不回显任何路由能力
        return json.dumps(
            {"error": f"unregistered system/api: {system_code}/{api_code}"},
            ensure_ascii=False,
        )

    clamped = max(_TIMEOUT_CLAMP_S[0], min(_TIMEOUT_CLAMP_S[1], timeout))
    # 真实集成：在此用 httpx/aiohttp + 签名客户端请求 url；
    # 样例用 sleep 模拟网络往返，保持零第三方依赖、可直接挂载。
    await asyncio.sleep(0.05)
    logger.info(
        "[custom_extension_tool] query_system_api user=%s system=%s api=%s timeout=%ss",
        CURRENT_USER.get() or "anonymous",
        system_code,
        api_code,
        clamped,
    )
    return json.dumps(
        {
            "system": system_code,
            "api": api_code,
            "endpoint": url,
            "echo": payload,
        },
        ensure_ascii=False,
    )


@tool(
    name="get_current_user",
    description="查询当前会话的用户信息。无需参数。",
)
async def get_current_user() -> str:
    """返回当前会话上下文中的用户信息（contextvars 注入，工具侧只读）。"""
    user = CURRENT_USER.get()
    if not user:
        return json.dumps(
            {"error": "当前调用链路未注入用户上下文"}, ensure_ascii=False
        )
    return json.dumps({"user_id": user}, ensure_ascii=False)


def register_tools() -> list[Tool]:
    """AGENT_EXTRA_TOOLS 注册入口：返回要挂载的工具实例列表。

    必须返回模块级 @tool 单例（上面的函数对象本身），不要在这里
    构造新实例——实例被所有并发会话共享。
    """
    return [query_system_api, get_current_user]
