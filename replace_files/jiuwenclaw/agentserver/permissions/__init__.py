# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""
JiuwenClaw 细粒度权限管控系统（Phase-1）。

提供三级权限动作：

- ``allow``：直接执行
- ``ask``：弹出审批确认（支持本次允许 / 总是允许 / 拒绝）
- ``deny``：拒绝执行

工具档位另含 ``guard``（无 baseline，进入 Guard 管线评估命令规则 + 文件三轴）。

判定优先级：

- ``deny`` 绝对否决：任何匹配到的 ``deny`` 规则都不会被覆盖。
- 子线 A 命令 / 参数规则按 ``approval_overrides → rules → builtin`` 顺序查找 ``allow``。
- 子线 B 文件路径走 ``file_guard``（``workspace`` / ``global`` / ``trusted_exec_directory``）。
- 二者结果按 ``strictest`` 合并到 ``PermissionResult``。

使用示例::

    from jiuwenclaw.agentserver.permissions import (
        get_permission_engine,
        PermissionLevel,
    )

    engine = get_permission_engine()
    result = await engine.check_permission(
        tool_name="mcp_exec_command",
        tool_args={"command": "ls -la"},
    )

    if result.is_allowed:
        ...
    elif result.needs_approval:
        # 审批流程由 PermissionInterruptRail 处理
        for op in result.file_operations or []:
            print(op.action, op.path, op.source)
"""

from jiuwenclaw.agentserver.permissions.core import (
    PermissionEngine,
    get_permission_engine,
    init_permission_engine,
    set_permission_engine,
)
from jiuwenclaw.agentserver.permissions.checker import (
    assess_command_risk_static,
    assess_command_risk_with_llm,
    check_tool_permissions,
)
from jiuwenclaw.agentserver.permissions.command_intent import (
    CommandIntent,
    collect_command_intents,
    extract_l1_intents,
    is_command_intent_enabled,
    resolve_l3_cmd_extra_body,
    resolve_l3_cmd_model_name,
    resolve_l3_cmd_timeout,
    run_l3_cmd_intents,
)
from jiuwenclaw.agentserver.permissions.file_guard import (
    FileGuardChecker,
    apply_cli_trusted_to_permissions_dict,
    list_pending_file_operations_for_tool,
    merged_file_guard_config,
    persist_file_operations_allow,
    persist_legacy_external_allow_paths,
    report_legacy_path_rules_at_load,
)
from jiuwenclaw.agentserver.permissions.patterns import (
    build_command_allow_pattern,
    persist_cli_trusted_directory,
    persist_external_directory_allow,
    persist_permission_allow_rule,
)
from jiuwenclaw.agentserver.permissions.models import (
    FileOperation,
    PermissionLevel,
    PermissionResult,
    SubcommandPermissionResult,
)
from jiuwenclaw.agentserver.permissions.owner_scopes import (
    TOOL_PERMISSION_CONTEXT,
    check_tool_permissions_with_context,
)

__all__ = [
    # Models
    "FileOperation",
    "PermissionLevel",
    "PermissionResult",
    "SubcommandPermissionResult",
    # Core
    "PermissionEngine",
    "init_permission_engine",
    "get_permission_engine",
    "set_permission_engine",
    # Guard
    "check_tool_permissions",
    # File guard (Phase-1)
    "FileGuardChecker",
    "merged_file_guard_config",
    "apply_cli_trusted_to_permissions_dict",
    "list_pending_file_operations_for_tool",
    "persist_file_operations_allow",
    "persist_legacy_external_allow_paths",
    "report_legacy_path_rules_at_load",
    # Command intent (Phase-1)
    "CommandIntent",
    "collect_command_intents",
    "extract_l1_intents",
    "is_command_intent_enabled",
    "resolve_l3_cmd_extra_body",
    "resolve_l3_cmd_model_name",
    "resolve_l3_cmd_timeout",
    "run_l3_cmd_intents",
    # Persist
    "build_command_allow_pattern",
    "persist_permission_allow_rule",
    "persist_external_directory_allow",
    "persist_cli_trusted_directory",
    # Risk
    "assess_command_risk_static",
    "assess_command_risk_with_llm",
    # Owner Scopes (数字分身)
    "TOOL_PERMISSION_CONTEXT",
    "check_tool_permissions_with_context",
]
