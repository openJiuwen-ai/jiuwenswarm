"""Build invocation-local Smart Approval rails from a published employee policy."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from openjiuwen.harness.security import ToolPermissionHost


class EmployeePermissionRailFactory:
    """Create a fresh, non-persistent permission rail for one employee execution."""

    def build(
        self,
        *,
        execution,
        permissions: dict[str, Any],
        model: Any,
        model_name: str | None,
        workspace_root: str | None,
        sys_operation: Any,
        tool_names: tuple[str, ...],
    ):
        from jiuwenswarm.agents.harness.common.rails.permissions.auto_config import (
            normalize_auto_permission_options,
            normalize_permissions_for_runtime,
        )
        from jiuwenswarm.agents.harness.common.rails.permissions.auto_permission_rail import (
            AutoPermissionInterruptRail,
        )
        from jiuwenswarm.agents.harness.common.rails.permissions.auto_reviewer import (
            AutoReviewer,
            IsolatedModelReviewerClient,
            build_isolated_reviewer_model,
        )
        from jiuwenswarm.agents.harness.common.rails.permissions.permission_interrupt_rail import (
            JiuwenSwarmPermissionInterruptRail,
        )

        policy = deepcopy(permissions)
        policy["enabled"] = True
        policy["mode"] = "auto"
        policy = normalize_permissions_for_runtime(policy)
        workspace = Path(workspace_root).resolve(strict=False) if workspace_root else None
        host = ToolPermissionHost(
            get_permissions_snapshot=lambda session_id=None: deepcopy(policy),
            persist_allow_rule=lambda _: False,
            persist_session_allow_rule=lambda *_args, **_kwargs: False,
            resolve_workspace_dir=lambda: workspace,
        )
        base_rail = JiuwenSwarmPermissionInterruptRail(
            config=policy,
            tool_names=tool_names,
            llm=model,
            model_name=model_name,
            host=host,
            exact_persist_callback=None,
        )
        options = normalize_auto_permission_options(policy.get("auto"))
        reviewer_model = build_isolated_reviewer_model(model)
        reviewer = (
            AutoReviewer(
                client=IsolatedModelReviewerClient(model=reviewer_model, display_language_getter=lambda: "zh"),
                timeout_ms=options["reviewer_timeout_ms"],
                min_confidence=options["reviewer_min_confidence"],
            )
            if reviewer_model is not None
            else None
        )
        return AutoPermissionInterruptRail(
            base_rail=base_rail,
            permission_config=policy,
            workspace_root=workspace,
            sys_operation=sys_operation,
            auto_reviewer=reviewer,
        )


EMPLOYEE_PERMISSION_RAIL_FACTORY = EmployeePermissionRailFactory()
