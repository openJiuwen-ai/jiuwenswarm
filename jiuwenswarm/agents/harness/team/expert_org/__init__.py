# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Host adapters that wire AgentGroup packages into OrganizationRuntime."""

from __future__ import annotations

__all__ = [
    "JiuwenExpertGroupCatalog",
    "JiuwenExpertTeamLauncher",
    "install_expert_org_adapters",
    "register_expert_adapter_installer",
]


def __getattr__(name: str):
    if name == "JiuwenExpertGroupCatalog":
        from jiuwenswarm.agents.harness.team.expert_org.catalog import JiuwenExpertGroupCatalog

        return JiuwenExpertGroupCatalog
    if name == "JiuwenExpertTeamLauncher":
        from jiuwenswarm.agents.harness.team.expert_org.launcher import JiuwenExpertTeamLauncher

        return JiuwenExpertTeamLauncher
    if name == "install_expert_org_adapters":
        from jiuwenswarm.agents.harness.team.expert_org.wiring import install_expert_org_adapters

        return install_expert_org_adapters
    if name == "register_expert_adapter_installer":
        from jiuwenswarm.agents.harness.team.expert_org.wiring import (
            register_expert_adapter_installer,
        )

        return register_expert_adapter_installer
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
