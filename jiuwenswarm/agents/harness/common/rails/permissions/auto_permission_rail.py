"""Auto-permission wrapper around the OpenJiuwen permission rail."""

from __future__ import annotations

from openjiuwen.core.single_agent.rail.base import AgentRail

from jiuwenswarm.agents.harness.common.rails.permissions._auto_permission.artifact_authorization import (
    AutoPermissionArtifactAuthorizationMixin,
)
from jiuwenswarm.agents.harness.common.rails.permissions._auto_permission.artifact_tracking_capture import (
    AutoPermissionArtifactCaptureMixin,
)
from jiuwenswarm.agents.harness.common.rails.permissions._auto_permission.before_tool import (
    AutoPermissionBeforeToolMixin,
)
from jiuwenswarm.agents.harness.common.rails.permissions._auto_permission.manual_authorization import (
    AutoPermissionManualAuthorizationMixin,
)
from jiuwenswarm.agents.harness.common.rails.permissions._auto_permission.deny_response import (
    AutoPermissionDenyResponseMixin,
)
from jiuwenswarm.agents.harness.common.rails.permissions._auto_permission.deterministic import (
    AutoPermissionDeterministicMixin,
)
from jiuwenswarm.agents.harness.common.rails.permissions._auto_permission.domain_grant_audit import (
    AutoPermissionDomainGrantAuditMixin,
)
from jiuwenswarm.agents.harness.common.rails.permissions._auto_permission.lifecycle import (
    AutoPermissionLifecycleMixin,
)
from jiuwenswarm.agents.harness.common.rails.permissions._auto_permission.models import (
    ToolInvocation,
)
from jiuwenswarm.agents.harness.common.rails.permissions._auto_permission.reviewer_execution import (
    AutoPermissionReviewerExecutionMixin,
)
from jiuwenswarm.agents.harness.common.rails.permissions._auto_permission.reviewer_metadata import (
    _reviewer_action_summary,
    _reviewer_ui_metadata,
)
from jiuwenswarm.agents.harness.common.rails.permissions._auto_permission.reviewer_override_consume import (
    AutoPermissionReviewerOverrideConsumeMixin,
)
from jiuwenswarm.agents.harness.common.rails.permissions._auto_permission.reviewer_override_gate import (
    AutoPermissionReviewerOverrideGateMixin,
)
from jiuwenswarm.agents.harness.common.rails.permissions._auto_permission.runtime_bridge import (
    AutoPermissionRuntimeBridgeMixin,
)


class AutoPermissionInterruptRail(
    AutoPermissionLifecycleMixin,
    AutoPermissionBeforeToolMixin,
    AutoPermissionArtifactCaptureMixin,
    AutoPermissionReviewerOverrideConsumeMixin,
    AutoPermissionReviewerOverrideGateMixin,
    AutoPermissionReviewerExecutionMixin,
    AutoPermissionDeterministicMixin,
    AutoPermissionArtifactAuthorizationMixin,
    AutoPermissionDenyResponseMixin,
    AutoPermissionManualAuthorizationMixin,
    AutoPermissionDomainGrantAuditMixin,
    AutoPermissionRuntimeBridgeMixin,
    AgentRail,
):
    """JiuwenSwarm-owned auto-permission guard around a base permission rail."""


__all__ = [
    "AutoPermissionInterruptRail",
    "ToolInvocation",
    "_reviewer_action_summary",
    "_reviewer_ui_metadata",
]
