# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""面向 agent 的用户管理工具封装。

无独立后端管理器：创建/查询直接读写 ``~/.jiuwenswarm/users.json``
（唯一状态收口于 ``user_store``），web 通道 ``users.list`` / ``users.create``
与本工具共用同一存储，保证逻辑唯一来源。

``create_user`` 的用户名/密码为必填：用户没给全时，模型应先用 ``ask_user``
逐题收集（密码、API Key 建议按自由输入题逐题询问），收齐后再调本工具，
不要自行编造字段值。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

from openjiuwen.core.foundation.tool import LocalFunction, Tool, ToolCard

from jiuwenswarm.server.runtime.user_registry import user_store

logger = logging.getLogger(__name__)


class UserToolkit:
    """把用户管理（创建/查询）暴露成模型工具。"""

    # ----- create -----

    async def create_user(
        self,
        username: str,
        password: str,
        api_key: str = "",
        api_base: str = "",
        model: str = "",
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            user_store.create_user, username, password, api_key, api_base, model
        )

    # ----- list -----

    async def list_users(self) -> dict[str, Any]:
        users = await asyncio.to_thread(user_store.list_users)
        detail = "当前没有任何用户。" if not users else ""
        return {"success": True, "items": users, "detail": detail}

    # ----- delete -----

    async def delete_user(self, username: str) -> dict[str, Any]:
        return await asyncio.to_thread(user_store.delete_user, username)

    # ----- 注册 -----

    def get_tools(self) -> list[Tool]:
        """Return user management tools for agent registration."""

        def make_tool(name: str, description: str, input_params: dict, func: Callable[..., Any]) -> Tool:
            # 统一用 LocalFunction 包装，保持与现有 toolkit 注册方式一致。
            card = ToolCard(
                id=name,
                name=name,
                description=description,
                input_params=input_params,
            )
            return LocalFunction(card=card, func=func)

        return [
            make_tool(
                name="create_user",
                description=(
                    "Create a managed user (shown in the user management UI) with "
                    "their model credentials. `username` and `password` are required; "
                    "when the user asks to create a user but has not provided all "
                    "required fields, first collect the missing ones with the "
                    "ask_user tool (ask one question per field, free-text input), "
                    "then call this tool. Never invent field values. `api_key`, "
                    "`api_base` and `model` are optional but recommended: they can "
                    "later be injected into a third-party agent via "
                    "install_third_agent's `user` parameter. The response contains "
                    "only masked secrets."
                ),
                input_params={
                    "type": "object",
                    "properties": {
                        "username": {
                            "type": "string",
                            "description": "Unique username, e.g. 'alice'.",
                        },
                        "password": {
                            "type": "string",
                            "description": (
                                "User password. Required; ask for it via ask_user "
                                "when the user has not provided one."
                            ),
                        },
                        "api_key": {
                            "type": "string",
                            "description": "Model API key of this user. Optional.",
                        },
                        "api_base": {
                            "type": "string",
                            "description": (
                                "Model API base URL of this user, e.g. "
                                "'https://api.example.com/v1'. Optional."
                            ),
                        },
                        "model": {
                            "type": "string",
                            "description": "Model name of this user. Optional.",
                        },
                    },
                    "required": ["username", "password"],
                },
                func=self.create_user,
            ),
            make_tool(
                name="list_users",
                description=(
                    "List managed users with masked secrets (passwords and API keys "
                    "are never returned in plaintext). Useful to check which users "
                    "exist, e.g. before passing `user` to install_third_agent."
                ),
                input_params={"type": "object", "properties": {}},
                func=self.list_users,
            ),
            make_tool(
                name="delete_user",
                description=(
                    "Delete a managed user by username. The user record (including "
                    "its model credentials) is removed from the user management "
                    "store; third-party agents already installed keep running with "
                    "the credentials captured at install time."
                ),
                input_params={
                    "type": "object",
                    "properties": {
                        "username": {
                            "type": "string",
                            "description": "Username of the user to delete, e.g. 'alice'.",
                        },
                    },
                    "required": ["username"],
                },
                func=self.delete_user,
            ),
        ]


def get_user_tools() -> list[Tool]:
    """构建用户管理工具集（无状态，可直接注册到任意 adapter）。"""
    return UserToolkit().get_tools()


__all__ = [
    "UserToolkit",
    "get_user_tools",
]
