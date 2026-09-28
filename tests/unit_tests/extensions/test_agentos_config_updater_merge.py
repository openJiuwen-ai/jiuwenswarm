# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Unit tests for AgentOS config updater merge helpers."""

from __future__ import annotations

import pytest

from jiuwenswarm.extensions.agentos.config_updater.merge import (
    extract_section,
    filter_managed_section,
    merge_section,
    normalize_section,
    validate_section,
)


def test_extract_section_extracts_component_and_metadata():
    doc = {
        "_version": 1,
        "_revision": 7,
        "gateway": {
            "agent_sandbox": {"idle_timeout": 120, "cpu": 2000, "memory": 4096},
            "tool_sandbox": {"idle_timeout": 300, "cpu": 1000, "memory": 2048},
        },
        "jiuwenbox": {"network": {"egress": {"allowed_ips": []}}},
    }
    section, metadata = extract_section(doc, "gateway")
    assert section == doc["gateway"]
    assert metadata == {"_version": 1, "_revision": 7}


def test_extract_section_handles_missing_or_invalid_documents():
    assert extract_section({"_version": 1}, "gateway") == ({}, {"_version": 1})
    assert extract_section({"gateway": []}, "gateway") == ({}, {})
    assert extract_section(None, "gateway") == ({}, {})


def test_filter_managed_section_keeps_managed_sandbox_fields():
    managed, ignored = filter_managed_section(
        {
            "agent_sandbox": {"idle_timeout": 120, "cpu": 2000, "memory": 4096},
            "tool_sandbox": {"idle_timeout": 300, "cpu": 1000, "memory": 2048},
            "sandbox": {"type": "yuanrong", "cpu": 9999},
            "channels": {"web": {"enabled": False}},
        }
    )
    assert managed == {
        "agent_sandbox": {"idle_timeout": 120, "cpu": 2000, "memory": 4096},
        "tool_sandbox": {"idle_timeout": 300, "cpu": 1000, "memory": 2048},
    }
    assert ignored == ["sandbox", "channels"]


def test_filter_managed_section_preserves_explicit_null_timeout():
    managed, ignored = filter_managed_section({"agent_sandbox": {"idle_timeout": None}})
    assert managed == {"agent_sandbox": {"idle_timeout": None}}
    assert ignored == []


def test_merge_section_preserves_unmanaged_local_config():
    local = {"sandbox": {"type": "yuanrong", "cpu": 1000}}
    merged = merge_section(local, {"agent_sandbox": {"idle_timeout": 120}})
    assert merged == {
        "sandbox": {"type": "yuanrong", "cpu": 1000},
        "agent_sandbox": {"idle_timeout": 120},
    }


def test_validate_accepts_agent_and_tool_sandbox_fields():
    assert validate_section(
        {
            "agent_sandbox": {"idle_timeout": 600, "cpu": 2000, "memory": 4096},
            "tool_sandbox": {"idle_timeout": 600, "cpu": 2000, "memory": 4096},
        }
    ) == []


def test_validate_accepts_nonpositive_idle_timeout():
    assert validate_section({"agent_sandbox": {"idle_timeout": 0}}) == []
    assert validate_section({"tool_sandbox": {"idle_timeout": -1}}) == []


@pytest.mark.parametrize("value", [None, True, "invalid", float("nan"), float("inf")])
def test_validate_rejects_invalid_idle_timeout(value):
    errors = validate_section({"agent_sandbox": {"idle_timeout": value}})
    assert len(errors) == 1
    assert "agent_sandbox.idle_timeout" in errors[0]


@pytest.mark.parametrize("value", [0, -1, True, "big", 1.5, float("inf")])
def test_validate_rejects_invalid_resources(value):
    assert validate_section({"agent_sandbox": {"cpu": value}})
    assert validate_section({"agent_sandbox": {"memory": value}})
    assert validate_section({"tool_sandbox": {"cpu": value}})
    assert validate_section({"tool_sandbox": {"memory": value}})


def test_validate_ignores_old_flat_sandbox_fields():
    assert validate_section({"sandbox": {"cpu": 2000}}) == []


def test_normalize_managed_fields():
    section = {
        "agent_sandbox": {"idle_timeout": "120", "cpu": "2000", "memory": "4096"},
        "tool_sandbox": {"idle_timeout": "60", "cpu": "1000", "memory": "2048"},
    }
    result = normalize_section(section)
    assert result is section
    assert section == {
        "agent_sandbox": {"idle_timeout": 120, "cpu": 2000, "memory": 4096},
        "tool_sandbox": {"idle_timeout": 60, "cpu": 1000, "memory": 2048},
    }
