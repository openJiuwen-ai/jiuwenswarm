# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Host Summary Team adapter: wired into OrganizationRuntime like the expert org."""

from __future__ import annotations

__all__ = [
    "JiuwenSummaryTeamFactory",
    "install_summary_factory",
    "register_summary_factory_installer",
]


def __getattr__(name: str):
    if name == "JiuwenSummaryTeamFactory":
        from jiuwenswarm.agents.harness.team.summary_team_org.factory import (
            JiuwenSummaryTeamFactory,
        )

        return JiuwenSummaryTeamFactory
    if name == "install_summary_factory":
        from jiuwenswarm.agents.harness.team.summary_team_org.wiring import (
            install_summary_factory,
        )

        return install_summary_factory
    if name == "register_summary_factory_installer":
        from jiuwenswarm.agents.harness.team.summary_team_org.wiring import (
            register_summary_factory_installer,
        )

        return register_summary_factory_installer
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
