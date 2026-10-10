# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""IM 渠道（xiaoyi 等）按消息携带的权限档位/工作空间应用到 JiuwenSwarm 的工具集。

对齐桌面客户端 src/core/framework/jiuwenswarm/permission-profile.ts 的档位映射：
  - 默认权限(default)：permissions.enabled=true + permission_mode=strict
    + bash/网络工具 ask + file_guard 工作区外读写 ask；chat.send 带 trusted_dirs
    + 注入工作空间约束指令
  - 替我审批(auto_approve)：enabled=true + normal + bash/网络/文件 allow
  - 完全访问权限(full_access)：permissions.enabled=false（与 Jiuwen Web full_access 相同）

手机端经 A2A data part 的 variables.clientVariables 携带：
  - workspace：工作空间路径（WorkspaceQuery 应答里的 path；兼容 {"name","path"} 对象）
  - permission：档位 id（default / full_access，兼容中文名与替我审批）
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any, Optional

PERMISSION_PROFILE_DEFAULT = "default"
PERMISSION_PROFILE_AUTO_APPROVE = "auto_approve"
PERMISSION_PROFILE_FULL_ACCESS = "full_access"

# clientVariables.permission 取值（id 或中文显示名）→ 档位 id
_PROFILE_ALIASES: dict[str, str] = {
    PERMISSION_PROFILE_DEFAULT: PERMISSION_PROFILE_DEFAULT,
    "默认权限": PERMISSION_PROFILE_DEFAULT,
    "默认": PERMISSION_PROFILE_DEFAULT,
    PERMISSION_PROFILE_AUTO_APPROVE: PERMISSION_PROFILE_AUTO_APPROVE,
    "替我审批": PERMISSION_PROFILE_AUTO_APPROVE,
    "auto": PERMISSION_PROFILE_AUTO_APPROVE,
    PERMISSION_PROFILE_FULL_ACCESS: PERMISSION_PROFILE_FULL_ACCESS,
    "fullaccess": PERMISSION_PROFILE_FULL_ACCESS,
    "full": PERMISSION_PROFILE_FULL_ACCESS,
    "完全访问权限": PERMISSION_PROFILE_FULL_ACCESS,
    "完全访问": PERMISSION_PROFILE_FULL_ACCESS,
    "yolo": PERMISSION_PROFILE_FULL_ACCESS,
}

# 网络类工具（与桌面端 patchPermissionsForProfile 对齐）
_NETWORK_TOOLS = ("mcp_free_search", "mcp_paid_search", "mcp_fetch_webpage")


def normalize_permission_profile(raw: Any) -> Optional[str]:
    """clientVariables.permission → 档位 id；缺省/未识别返回 None（调用方不动全局配置）。"""
    if not isinstance(raw, str):
        return None
    key = raw.strip()
    if not key:
        return None
    return _PROFILE_ALIASES.get(key) or _PROFILE_ALIASES.get(key.lower())


def permission_profile_config_patch(profile: str) -> Optional[dict[str, Any]]:
    """档位 → config.yaml permissions 段补丁（键与桌面端 patchPermissionsForProfile 对齐）。

    返回 None 表示档位未识别（调用方不应写配置）。
    """
    pid = normalize_permission_profile(profile)
    if pid is None:
        return None
    if pid == PERMISSION_PROFILE_FULL_ACCESS:
        return {
            "enabled": False,
            "permission_mode": "normal",
            "tools": {"bash": "allow", **{t: "allow" for t in _NETWORK_TOOLS}},
            "file_guard_rw": "allow",
        }
    if pid == PERMISSION_PROFILE_AUTO_APPROVE:
        return {
            "enabled": True,
            "permission_mode": "normal",
            "tools": {"bash": "allow", **{t: "allow" for t in _NETWORK_TOOLS}},
            "file_guard_rw": "allow",
        }
    return {
        "enabled": True,
        "permission_mode": "strict",
        "tools": {"bash": "ask", **{t: "ask" for t in _NETWORK_TOOLS}},
        "file_guard_rw": "ask",
    }


def resolve_client_workspace(raw: Any) -> str:
    """clientVariables.workspace → 本机工作空间绝对路径；无效/不存在返回空串。

    兼容两种形态：字符串路径，或 {"name": ..., "path": ...} 对象（WorkspaceQuery
    应答条目的回传）。目录不存在时不强行创建——手机端只能从 WorkspaceQuery 返回的
    既有空间里选择， stale 路径按未携带处理（回退框架默认工作目录），避免误建目录。
    """
    value = raw
    if isinstance(value, dict):
        value = value.get("path") or value.get("name")
    if not isinstance(value, str):
        return ""
    path = os.path.expandvars(os.path.expanduser(value.strip()))
    if not path:
        return ""
    if not os.path.isdir(path):
        return ""
    return os.path.abspath(path)


def resolve_trusted_dirs(profile: Optional[str], workspace: str) -> Optional[list[str]]:
    """chat.send 的 trusted_dirs：受限档只放行当前工作空间；完全访问不传白名单。"""
    if not workspace:
        return None
    if profile == PERMISSION_PROFILE_FULL_ACCESS:
        return None
    return [workspace]


def with_workspace_directive(text: str, workspace: str, profile: Optional[str]) -> str:
    """注入工作空间提示（受限档为约束指令；完全访问档为位置提示）。

    手机端经 clientVariables.workspace 显式选择了工作空间——即便完全访问档，
    模型也必须知道落盘位置：xiaoyi/desktop 渠道没有 RuntimePromptRail 的目录
    上下文段（仅 tui/web 注入），此前 full_access 下零提示，任务全落默认工作目录。
    """
    path = (workspace or "").strip()
    if not path:
        return text
    # timestamp 为本条消息的处理时间（每轮重新生成），供模型感知当前时间
    payload = json.dumps(
        {
            "path": path,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        },
        ensure_ascii=False,
    )
    if profile == PERMISSION_PROFILE_FULL_ACCESS:
        # 位置提示（非权限约束）：完全访问档不设写入边界，但落盘默认位置要告知
        return (
            f"{text}\n\n"
            f"<claw_workspace>{payload}</claw_workspace>\n"
            f"【工作空间】当前项目目录是 `{path}`。除非用户明确指定其他位置，"
            f"新建与写入文件请落在该目录下（可用相对路径）。"
        )
    return (
        f"{text}\n\n"
        f"<claw_workspace>{payload}</claw_workspace>\n"
        f"【工作空间】当前项目目录是 `{path}`。除非用户明确指定其他位置，否则所有新建与写入文件必须落在该目录下（可用相对路径）。"
    )
