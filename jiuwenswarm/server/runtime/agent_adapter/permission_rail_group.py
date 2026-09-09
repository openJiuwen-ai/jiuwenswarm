# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""One permission rail recipe for cold installation and session replacement."""

from collections.abc import Callable
from typing import Any

from jiuwenswarm.agents.harness.common.rails import JiuSwarmStreamEventRail, StructuredAskUserRail
from jiuwenswarm.agents.harness.common.rails.permissions.root_context_rail import RootContextRail
from jiuwenswarm.agents.harness.common.rails.permissions.root_permission_queue import RootPermissionQueue
from jiuwenswarm.agents.harness.common.rails.permissions.root_permission_queue_rail import (
    RootPermissionCompletionRail, RootPermissionQueueRail,
)
from jiuwenswarm.agents.harness.common.rails.permissions.tool_invocation_key import ToolInvocationKeyV1


def build_permission_group(
    config: dict[str, Any], *, permission_builder: Callable[..., Any],
    permission_inputs: dict[str, Any], queue: RootPermissionQueue,
    answer_claimed: Callable[[ToolInvocationKeyV1], None], sandboxed: bool, language: str,
) -> dict[str, Any]:
    """Return candidates only; the caller owns registration and publication."""
    smart = permission_inputs["enable_auto_permission"]
    permission = permission_builder(config=config, **permission_inputs)
    return {
        "_permission_rail": permission,
        "_root_permission_queue_rail": RootPermissionQueueRail(
            queue, answer_claimed=answer_claimed,
        ) if smart else None,
        "_root_context_rail": RootContextRail(
            root_permission_queue=queue, sys_operation=permission_inputs["sys_operation"],
            sandboxed=sandboxed,
        ) if smart else None,
        "_root_permission_completion_rail": RootPermissionCompletionRail(queue) if smart else None,
        "_stream_event_rail": JiuSwarmStreamEventRail(root_permission_queue=queue if smart else None),
        "_ask_user_rail": StructuredAskUserRail(language=language, strict_continuation_contract=smart),
    }
