# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""用户管理存储 — 状态唯一收口于 ``~/.jiuwenswarm/users.json``。

web 通道 ``users.list`` / ``users.create`` / ``users.delete`` 与三方 Agent 安装
（``install_third_agent`` 的 ``user`` 参数）共用本模块，保证逻辑唯一来源。

密码与 API Key 仅在本地状态文件明文落盘（与 third_agents.json 同级同权），
对外（users.list RPC）一律脱敏返回，明文不出后端。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jiuwenswarm.server.runtime.model_registry.model_registry import mask_api_key

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 状态文件（唯一状态收口，原子写）
# ---------------------------------------------------------------------------

def _state_file() -> Path:
    return Path.home() / ".jiuwenswarm" / "users.json"


def _read_state() -> list[dict[str, Any]]:
    """读取用户列表；文件缺失/损坏时视为空列表。"""
    try:
        data = json.loads(_state_file().read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def _write_state(users: list[dict[str, Any]]) -> None:
    path = _state_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(users, f, ensure_ascii=False, indent=2)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# 公共查询（web RPC 与三方 Agent 安装复用）
# ---------------------------------------------------------------------------

def _public_user(record: dict[str, Any]) -> dict[str, Any]:
    """对外展示用用户卡片：密码/API Key 只返回脱敏结果。"""
    api_key = str(record.get("api_key") or "")
    return {
        "username": record.get("username"),
        "password_masked": mask_api_key(str(record.get("password") or "")),
        "api_key_masked": mask_api_key(api_key) if api_key else "",
        "api_base": record.get("api_base") or "",
        "model": record.get("model") or "",
        "created_at": record.get("created_at"),
    }


def list_users() -> list[dict[str, Any]]:
    """返回全部用户的展示卡片（敏感字段已脱敏）。"""
    return [_public_user(record) for record in _read_state()]


def get_user(username: Any) -> dict[str, Any] | None:
    """按用户名精确查找，返回含明文凭据的完整记录（仅限后端内部使用）。"""
    target = str(username or "").strip()
    if not target:
        return None
    for record in _read_state():
        if record.get("username") == target:
            return record
    return None


def create_user(
    username: Any,
    password: Any,
    api_key: Any = "",
    api_base: Any = "",
    model: Any = "",
) -> dict[str, Any]:
    """创建用户；用户名重复或必填字段缺失时返回 success=False。"""
    username = str(username or "").strip()
    password = str(password or "")
    if not username:
        return {"success": False, "detail": "用户名不能为空。"}
    if not password:
        return {"success": False, "detail": "密码不能为空。"}
    if get_user(username) is not None:
        return {"success": False, "detail": f"用户 {username} 已存在。"}

    record = {
        "username": username,
        "password": password,
        "api_key": str(api_key or ""),
        "api_base": str(api_base or "").strip(),
        "model": str(model or "").strip(),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    users = _read_state()
    users.append(record)
    _write_state(users)
    return {
        "success": True,
        "user": _public_user(record),
        "detail": f"用户 {username} 创建成功。",
    }


def delete_user(username: Any) -> dict[str, Any]:
    """删除用户；用户不存在时返回 success=False。"""
    target = str(username or "").strip()
    if not target:
        return {"success": False, "detail": "用户名不能为空。"}
    users = _read_state()
    remaining = [r for r in users if r.get("username") != target]
    if len(remaining) == len(users):
        return {"success": False, "detail": f"用户 {target} 不存在。"}
    _write_state(remaining)
    return {"success": True, "detail": f"用户 {target} 已删除。"}


# ---------------------------------------------------------------------------
# web RPC handlers
# ---------------------------------------------------------------------------

async def handle_users_list(params: dict) -> dict:
    """web 通道 ``users.list`` 的 handler（只读，敏感字段脱敏）。"""
    return {"users": await asyncio.to_thread(list_users)}


async def handle_users_create(params: dict) -> dict:
    """web 通道 ``users.create`` 的 handler。"""
    params = params if isinstance(params, dict) else {}
    return await asyncio.to_thread(
        create_user,
        params.get("username"),
        params.get("password"),
        params.get("api_key"),
        params.get("api_base"),
        params.get("model"),
    )


async def handle_users_delete(params: dict) -> dict:
    """web 通道 ``users.delete`` 的 handler。"""
    params = params if isinstance(params, dict) else {}
    return await asyncio.to_thread(delete_user, params.get("username"))


__all__ = [
    "create_user",
    "delete_user",
    "get_user",
    "handle_users_create",
    "handle_users_delete",
    "handle_users_list",
    "list_users",
]
