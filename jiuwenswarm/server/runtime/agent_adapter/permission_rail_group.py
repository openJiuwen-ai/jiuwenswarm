# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""One permission rail recipe for cold installation and session replacement."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from openjiuwen.harness.rails.security.tool_security_rail import PermissionInterruptRail

from jiuwenswarm.agents.harness.common.rails import JiuSwarmStreamEventRail, StructuredAskUserRail
from jiuwenswarm.agents.harness.common.rails.permissions.auto_permission_rail import AutoPermissionInterruptRail
from jiuwenswarm.agents.harness.common.rails.permissions.root_context_rail import RootContextRail
from jiuwenswarm.agents.harness.common.rails.permissions.root_permission_queue import RootPermissionQueue
from jiuwenswarm.agents.harness.common.rails.permissions.root_permission_queue_rail import (
    RootPermissionCompletionRail, RootPermissionQueueRail,
)
from jiuwenswarm.agents.harness.common.rails.permissions.tool_invocation_key import ToolInvocationKeyV1


PERMISSION_RAIL_TYPES = (PermissionInterruptRail, AutoPermissionInterruptRail)
PERMISSION_GROUP_TYPES = PERMISSION_RAIL_TYPES + (
    RootPermissionQueueRail, RootContextRail, RootPermissionCompletionRail,
    JiuSwarmStreamEventRail, StructuredAskUserRail,
)


@dataclass(frozen=True)
class PermissionRailGroup:
    permission_rail: PermissionInterruptRail | AutoPermissionInterruptRail | None
    root_permission_queue_rail: RootPermissionQueueRail | None
    root_context_rail: RootContextRail | None
    root_permission_completion_rail: RootPermissionCompletionRail | None
    stream_event_rail: JiuSwarmStreamEventRail | None
    ask_user_rail: StructuredAskUserRail | None

    def rails(self) -> list[Any]:
        return [rail for rail in (
            self.permission_rail, self.root_permission_queue_rail, self.root_context_rail,
            self.root_permission_completion_rail, self.stream_event_rail, self.ask_user_rail,
        ) if rail is not None]

    def verify(self, instance: Any, *, smart: bool, queue: RootPermissionQueue, sys_operation: Any) -> None:
        """Check the SDK graph, not just adapter fields or a configure list."""
        actual = instance.find_rails_by_type(PERMISSION_GROUP_TYPES)
        wanted = self.rails()
        if len(actual) != len(wanted) or any(
            sum(rail is item for rail in actual) != 1
            or not instance.is_registered_rail(item)
            for item in wanted
        ):
            raise RuntimeError("permission_registered_graph_mismatch")
        permission = self.permission_rail
        if permission is not None and (
            isinstance(permission, AutoPermissionInterruptRail) is not smart
        ):
            raise RuntimeError("permission_registered_type_mismatch")
        if smart and (permission is None or permission.sys_operation is not sys_operation):
            raise RuntimeError("permission_registered_owner_mismatch")
        stream = self.stream_event_rail
        # Assembly must verify the exact installed queue; no public getter exists.
        if (
            stream is None
            or stream._root_permission_queue is not (  # pylint: disable=protected-access
                queue if smart else None
            )
        ):
            raise RuntimeError("permission_stream_rail_unavailable")
        ask = self.ask_user_rail
        # The rail exposes a setter only; verify its installed value without mutation.
        if ask is None or ask._strict_continuation_contract is not smart:  # pylint: disable=protected-access
            raise RuntimeError("permission_ask_rail_unavailable")


def build_permission_group(
    config: dict[str, Any], *, permission_builder: Callable[..., Any],
    permission_inputs: dict[str, Any], queue: RootPermissionQueue,
    answer_claimed: Callable[[ToolInvocationKeyV1], None], sandboxed: bool, language: str,
) -> PermissionRailGroup:
    """Return candidates only; the caller owns registration and publication."""
    smart = permission_inputs["enable_auto_permission"]
    permission = permission_builder(config=config, **permission_inputs)
    return PermissionRailGroup(
        permission_rail=permission,
        root_permission_queue_rail=RootPermissionQueueRail(
            queue, answer_claimed=answer_claimed,
        ) if smart else None,
        root_context_rail=RootContextRail(
            root_permission_queue=queue, sys_operation=permission_inputs["sys_operation"],
            sandboxed=sandboxed,
        ) if smart else None,
        root_permission_completion_rail=RootPermissionCompletionRail(queue) if smart else None,
        stream_event_rail=JiuSwarmStreamEventRail(root_permission_queue=queue if smart else None),
        ask_user_rail=StructuredAskUserRail(language=language, strict_continuation_contract=smart),
    )
