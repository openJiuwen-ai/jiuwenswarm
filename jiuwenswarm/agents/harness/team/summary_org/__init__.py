# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Lazy exports for the organization Summary Team host adapter."""

from __future__ import annotations

__all__ = ["JiuwenSummaryTeamLauncher", "install_summary_org_adapter", "register_summary_adapter_installer"]


def __getattr__(name: str):
    """Import Summary Team adapter components only when the host needs them."""
    if name == "JiuwenSummaryTeamLauncher":
        from jiuwenswarm.agents.harness.team.summary_org.launcher import JiuwenSummaryTeamLauncher

        return JiuwenSummaryTeamLauncher
    if name in {"install_summary_org_adapter", "register_summary_adapter_installer"}:
        from jiuwenswarm.agents.harness.team.summary_org.wiring import (
            install_summary_org_adapter,
            register_summary_adapter_installer,
        )

        return {"install_summary_org_adapter": install_summary_org_adapter,
                "register_summary_adapter_installer": register_summary_adapter_installer}[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
