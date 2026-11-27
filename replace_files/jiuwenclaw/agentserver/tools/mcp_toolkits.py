# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""MCP toolkit aggregator for openjiuwen tools."""

from __future__ import annotations
import json
from pathlib import Path
import os

from openjiuwen.core.foundation.tool import Tool, McpServerConfig

from jiuwenclaw.agentserver.tools.command_tools import mcp_exec_command
from jiuwenclaw.agentserver.tools.petal_search_tools import enable_petal_search, mcp_petal_search
from jiuwenclaw.agentserver.tools.search_tools import mcp_free_search, mcp_paid_search
from jiuwenclaw.agentserver.tools.web_fetch_tools import mcp_fetch_webpage


def _normalize_stdio_command_kind(command: str) -> str:
    """将 command 归一化为 'node' 或 'python'。

    支持绝对路径如 /usr/local/bin/node、C:\\Program Files\\node.exe 等。
    """
    raw = str(command or "").strip()
    if not raw:
        raise ValueError("工具配置缺少 'command' 字段")

    normalized = Path(raw).name.lower()
    if normalized in ("node", "node.exe"):
        return "node"
    if normalized.startswith("python"):
        return "python"
    raise ValueError(f"不支持的 command 类型: '{command}'，目前仅支持 node/python 及其绝对路径")


def _normalize_mcp_client_type(raw_type: object) -> str:
    """与 ``ToolMgr._create_client`` 接受的类型对齐（小写、下划线归一）。"""
    if raw_type is None:
        return "stdio"
    s = str(raw_type).strip().lower().replace("_", "-")
    if "streamable" in s:
        return "streamable-http"
    if s == "sse":
        return "sse"
    if s == "stdio":
        return "stdio"
    return s if s else "stdio"


def _pick_mcp_url(tool_config: dict) -> str:
    v = tool_config.get("url")
    if isinstance(v, str) and v.strip():
        return v.strip()
    return ""


def _optional_auth_dict(tool_config: dict, key: str) -> dict | None:
    raw = tool_config.get(key)
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(f"字段 {key!r} 必须是 JSON 对象")
    return dict(raw)


def create_mcp_tool(config_str: str) -> McpServerConfig:
    """从 JSON 字符串解析并构造 ``McpServerConfig``（stdio MCP）。

    Args:
        1.stdio类型:
        config_str: JSON 格式配置字符串，格式为：
            {
                "name": "tool_name",
                "command": "node" | "python",
                "args": ["xxx.js"] | ["xxx.py"]
            }

        2.streamable-http类型:
            {
                "type": "streamableHttp",
                "url": "http://127.0.0.1:3002/mcp",
                "env": {},
                "auth_headers": {
                    "Authorization": "Bearer xxx"
                },
                "auth_query_params": {
                    "token": "yyy"
                }
            }

        3.sse类型:
            {
                "name": "my-sse-mcp",
                "type": "sse",
                "url": "http://127.0.0.1:3001/sse",
                "env": {},
                "auth_headers": {
                    "Authorization": "Bearer xxx"
                },
                "auth_query_params": {
                    "token": "yyy"
                }
            }
        4.playwright类型:
            {
                "name": "my-playwright-mcp",
                "description": "可选说明",
                "type": "playwright",
                "url": "http://127.0.0.1:3003/sse",
                "env": {}
            }

    Returns:
        ``McpServerConfig``，由调用方通过 ``Runner.resource_mgr.add_mcp_server(..., tag=...)`` 注册。

    Raises:
        ValueError: JSON 解析失败或配置不合法时
    """
    try:
        config = json.loads(config_str)
    except json.JSONDecodeError as e:
        raise ValueError(f"无效的 JSON 配置") from e

    # 处理数组格式或单个字典格式
    if isinstance(config, list):
        if len(config) == 0:
            raise ValueError("工具配置数组不能为空")
        # 使用第一个工具配置
        tool_config = config[0]
    elif isinstance(config, dict):
        tool_config = config
    else:
        raise ValueError("配置必须是字典或数组类型")

    if not isinstance(tool_config, dict):
        raise ValueError("工具配置必须是字典类型")

    tool_name = tool_config.get("name")
    server_id = str(tool_config.get("server_id") or tool_name or "").strip()
    command = tool_config.get("command")
    args = tool_config.get("args", [])
    env = tool_config.get("env")
    cwd = tool_config.get("cwd")

    if not tool_name:
        raise ValueError("工具配置缺少 'name' 字段")

    url = _pick_mcp_url(tool_config)
    client_type = _normalize_mcp_client_type(tool_config.get("type"))
    params = {}
    if isinstance(env, dict) and env:
        params["env"] = {str(k): str(v) for k, v in env.items() if k is not None and v is not None}
    if client_type == "sse":
        if not url:
            raise ValueError(f"工具 '{tool_name}'（'{client_type}'）需要 url")
        headers = _optional_auth_dict(tool_config, "auth_headers")
        query = _optional_auth_dict(tool_config, "auth_query_params")
        return McpServerConfig(
            server_id=tool_name,
            server_name=tool_name,
            server_path=url,
            client_type="sse",
            auth_headers=headers,
            auth_query_params=query,
            params=params
        )

    if client_type == "streamable-http":
        if not url:
            raise ValueError(
                f"工具 '{tool_name}'（'{client_type}'）需要 url"
            )
        headers = _optional_auth_dict(tool_config, "auth_headers")
        query = _optional_auth_dict(tool_config, "auth_query_params")
        return McpServerConfig(
            server_id=tool_name,
            server_name=tool_name,
            server_path=url,
            client_type="streamable-http",
            auth_headers=headers,
            auth_query_params=query,
            params=params
        )

    if client_type == "playwright":
        if not url:
            raise ValueError(
                f"工具 '{tool_name}'（'{client_type}'）需要 url"
            )
        return McpServerConfig(
            server_id=tool_name,
            server_name=tool_name,
            server_path=url,
            client_type="playwright",
            params=params
        )

    if client_type == "openapi":
        if not url:
            raise ValueError(
                f"工具 '{tool_name}'（'{client_type}'）需要 url"
            )
        return McpServerConfig(
            server_id=tool_name,
            server_name=tool_name,
            server_path=url,
            client_type="openapi",
            params=params
        )

    if not isinstance(args, list):
        raise ValueError(f"工具 '{tool_name}' 的 args 必须是列表类型")

    normalized_command = str(command or "").strip()
    _normalize_stdio_command_kind(normalized_command)
    params["command"] = normalized_command
    params["args"] = args
    if isinstance(cwd, str) and cwd.strip():
        params["cwd"] = cwd.strip()
    return McpServerConfig(
        server_id=server_id or tool_name,
        server_name=tool_name,
        server_path=f"stdio://{tool_name}",
        client_type="stdio",
        params=params
    )


def _has_paid_search_api_key() -> bool:
    """Check if any paid search API key is configured."""
    return any([
        os.environ.get("PERPLEXITY_API_KEY"),
        os.environ.get("SERPER_API_KEY"),
        os.environ.get("JINA_API_KEY"),
    ])


def get_mcp_tools() -> list[Tool]:
    """Return all MCP toolkit tools for registration in Runner."""
    tools: list[Tool] = []
    if enable_petal_search():
        tools.append(mcp_petal_search)
    tools.append(mcp_free_search)
    if _has_paid_search_api_key():
        tools.append(mcp_paid_search)
    tools.extend([mcp_fetch_webpage, mcp_exec_command])
    return tools


__all__ = [
    "mcp_free_search",
    "mcp_paid_search",
    "mcp_petal_search",
    "mcp_fetch_webpage",
    "mcp_exec_command",
    "get_mcp_tools",
    "create_mcp_tool",
]
