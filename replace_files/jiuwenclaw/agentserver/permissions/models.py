# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""权限系统数据模型."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Literal


class PermissionLevel(str, Enum):
    """权限级别.

    - ALLOW: 直接执行，无需确认
    - ASK:   弹出确认框，用户决定
    - DENY:  拒绝执行，返回错误
    """
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


FileOperationAction = Literal["read", "write", "exec"]
FileOperationSource = Literal["tool_arg", "shlex", "script_scan", "llm"]


@dataclass(frozen=True)
class FileOperation:
    """文件操作明细（审批卡渲染 + 持久化分发）。

    - ``action``: 单值 ``read`` / ``write`` / ``exec``
    - ``path``:   规范化绝对路径（POSIX）
    - ``source``: 抽取来源（注册表 / shlex / 脚本扫描 / LLM）
    - ``prompt``: 给用户的中文文案（如 "是否允许读取 xxx"）
    """
    action: FileOperationAction
    path: str
    source: FileOperationSource = "tool_arg"
    prompt: str = ""


@dataclass
class SubcommandPermissionResult:
    """单个 shell 子命令的权限评估结果。

    用于 simple 多子命令 shell 调用，把第一次评估每个子命令的结果传递给后续
    的持久化逻辑（避免在 rail 中重复评估同一个子命令）。
    """
    text: str
    permission: PermissionLevel
    matched_rule: str | None = None


@dataclass
class PermissionResult:
    permission: PermissionLevel
    matched_rule: str | None = None
    reason: str | None = None
    external_paths: list[str] | None = None
    risk: dict | None = None
    subcommand_results: list[SubcommandPermissionResult] | None = None
    file_operations: list[FileOperation] | None = None

    @property
    def is_allowed(self) -> bool:
        return self.permission == PermissionLevel.ALLOW

    @property
    def is_denied(self) -> bool:
        return self.permission == PermissionLevel.DENY

    @property
    def needs_approval(self) -> bool:
        return self.permission == PermissionLevel.ASK


@dataclass
class PatternRule:
    """模式匹配规则."""
    pattern: str
    permission: PermissionLevel
    description: str = ""
    rule_id: str = ""
