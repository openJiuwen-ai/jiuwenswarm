# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Provider declarations for the opt-in governance rails.

Declares swarm-owned governance rails as serializable harness elements so they
can be referenced from ``config_specs`` by name and built through the manifest
catalog, exactly like the other ``swarm.*`` rails. Declarations run at import
time; the registry module imports this so the descriptors are present before
``register_from_catalog`` runs.

The rails declared here are opt-in: ``config_specs`` only mounts one when the
team config enables it, so default team assembly is unchanged.
"""

from __future__ import annotations

from typing import Any

from openjiuwen.agent_teams.harness.manifest import (
    ConstructionInput,
    ElementKind,
    harness_element,
)
from openjiuwen.harness.manifest.inputs import param_field

from jiuwenswarm.agents.harness.common.rails.usage_report_rail import UsageReportRail

# swarm.* namespaced element names (parity with registry constants).
USAGE_REPORT = "swarm.usage_report"


class UsageReportInput(ConstructionInput):
    """Construction inputs for the usage-report rail (all from ``params``)."""

    report_path: str = param_field(default="usage_report.json", description="Destination JSON report path.")
    stage_label_key: str = param_field(default="stage_label", description="ctx.extra key holding the stage label.")
    default_label: str = param_field(default="unlabeled", description="Fallback stage label.")


@harness_element(
    kind=ElementKind.RAIL,
    name=USAGE_REPORT,
    description="Accumulates per-stage model-call token usage and persists a merged JSON report.",
    input_model=UsageReportInput,
)
def _build_usage_report_rail(params: dict[str, Any], context: Any) -> UsageReportRail:
    inp = UsageReportInput.resolve(params, context)
    return UsageReportRail(
        inp.report_path,
        stage_label_key=inp.stage_label_key,
        default_label=inp.default_label,
    )


__all__ = ["USAGE_REPORT", "UsageReportInput"]
