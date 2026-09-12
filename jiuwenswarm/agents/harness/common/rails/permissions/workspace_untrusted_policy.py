# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""关闭「信任工作空间」后，审批结果跟安全策略表走。

RelayClaw 关闭信任空间时会把 ``file_guard.workspace`` 写成 ask，且不再注入
``trusted_dirs``。openjiuwen 引擎再对路径做 ``strictest(tool_allow, file_guard_ask)``，
策略表全关仍会弹窗。

产品预期（信任空间关闭时）：
- 安全策略开启 → ask
- 安全策略关闭 → allow

信任空间开启时不改引擎结果（工作区白名单仍由 ``trusted_dirs`` 处理）。
"""

from __future__ import annotations

import logging
from typing import Any

from openjiuwen.harness.security.core import PermissionEngine
from openjiuwen.harness.security.models import PermissionLevel, PermissionResult

logger = logging.getLogger(__name__)


def workspace_rw_trusted(access: dict[str, str] | None = None) -> bool:
    """信任工作空间是否开启：与 ``_build_inputs`` 一致，看 ``workspace.read == allow``。"""
    if access is None:
        from jiuwenswarm.common.config import get_permissions_file_guard_workspace_access

        access = get_permissions_file_guard_workspace_access()
    return access.get("read") == "allow"


def reconcile_tool_policy_when_workspace_untrusted(
    engine: PermissionEngine,
    tool_name: str,
    tool_args: dict[str, Any],
    result: PermissionResult,
    *,
    workspace_trusted: bool | None = None,
) -> PermissionResult:
    """信任空间关闭时，用 Pipeline A（策略表）覆盖 file_guard 的 ask 抬升。

    ``deny`` 仍保留（路径层否决权）。信任空间开启时原样返回 ``result``。
    """
    if workspace_trusted is None:
        try:
            workspace_trusted = workspace_rw_trusted()
        except Exception:
            logger.warning(
                "[PermissionEngine] workspace trust lookup failed; keep engine result",
                exc_info=True,
            )
            return result

    if workspace_trusted:
        return result
    if result.permission == PermissionLevel.DENY:
        return result

    if not isinstance(tool_args, dict):
        tool_args = {}
    policy, policy_rule = engine.evaluate_global_policy_directly(
        tool_name,
        tool_args,
        include_external_directory=False,
    )
    if policy is None:
        policy = PermissionLevel.ASK
        policy_rule = policy_rule or "default"
    if policy == result.permission:
        return result

    logger.info(
        "[PermissionEngine] permission.workspace_untrusted.policy_wins "
        "tool=%s policy=%s merged=%s matched_rule=%s",
        tool_name,
        policy.value,
        result.permission.value,
        policy_rule,
    )
    return PermissionResult(
        permission=policy,
        matched_rule=f"{policy_rule}|workspace_untrusted:policy",
        reason=result.reason,
        external_paths=result.external_paths,
    )


class WorkspaceUntrustedPolicyEngine(PermissionEngine):
    """主/子 Agent 权限引擎：关闭信任空间后按策略表 ask/allow 裁决。"""

    async def check_permission(
        self,
        tool_name: str,
        tool_args: dict[str, Any],
    ) -> PermissionResult:
        result = await super().check_permission(tool_name, tool_args)
        return reconcile_tool_policy_when_workspace_untrusted(
            self,
            tool_name,
            tool_args,
            result,
        )
