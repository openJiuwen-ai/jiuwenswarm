# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

from dataclasses import FrozenInstanceError
from inspect import Parameter, signature

import pytest

from jiuwenswarm.runtime.mode_catalog import (
    ModeCatalogError,
    RuntimeModeDescriptor,
    list_mode_capabilities,
    resolve_mode_capability,
)


def test_mode_catalog_is_single_agent_only_and_stably_ordered() -> None:
    catalog = list_mode_capabilities()

    assert [item.mode for item in catalog.modes] == [
        "agent.work.normal",
        "agent.work.plan",
        "agent.code.normal",
        "agent.code.plan",
    ]
    assert [item.work_mode for item in catalog.modes] == [
        "work",
        "work",
        "code",
        "code",
    ]
    assert [item.is_plan for item in catalog.modes] == [False, True, False, True]


def test_custom_agent_definitions_are_available_only_in_code_modes() -> None:
    catalog = list_mode_capabilities()

    assert all(
        item.supports_custom_agent_definitions == (item.work_mode == "code")
        for item in catalog.modes
    )


def test_mode_resolution_accepts_legacy_single_agent_values_only() -> None:
    assert resolve_mode_capability("agent").mode == "agent.work.normal"
    assert resolve_mode_capability("code.normal").mode == "agent.code.normal"

    for unsupported in ("team", "team.code.normal", "workflow", "auto_harness"):
        with pytest.raises(ModeCatalogError, match="single-Agent mode not found"):
            resolve_mode_capability(unsupported)


def test_mode_dto_is_frozen_slotted_keyword_only_and_returns_fresh_data() -> None:
    descriptor = RuntimeModeDescriptor(
        mode="agent.code.normal",
        work_mode="code",
        is_plan=False,
        supports_custom_agent_definitions=True,
    )

    assert not hasattr(descriptor, "__dict__")
    with pytest.raises(FrozenInstanceError):
        descriptor.mode = "changed"  # type: ignore[misc]
    assert all(
        parameter.kind is Parameter.KEYWORD_ONLY
        for parameter in signature(RuntimeModeDescriptor).parameters.values()
    )

    first = list_mode_capabilities().to_dict()
    first["modes"].clear()
    assert len(list_mode_capabilities().to_dict()["modes"]) == 4
