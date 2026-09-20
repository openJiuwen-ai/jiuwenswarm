# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""DesignRail package — SDD (Spec-Driven Development) state machine for Code mode.

Conditionally mounted as a fixed Rail by
``JiuwenSwarmCodeAdapter._build_design_rail`` when
``modes.code.sdd.enabled`` is ``true``. Inherits ``RailStateMachineBase``
(shared state-machine + advance-tool + skill-injection pattern).

The ``sdd_advance`` tool is registered when the rail is mounted; it lives
only while DesignRail is mounted (code mode + sdd=true), so team/deep
agents never see it.

Public surface:
    DesignRail       — RailStateMachineBase subclass; SDD state machine.
"""
from __future__ import annotations

from jiuwenswarm.agents.harness.code.rails.sdd.design_rail.rail import DesignRail

__all__ = ["DesignRail"]
