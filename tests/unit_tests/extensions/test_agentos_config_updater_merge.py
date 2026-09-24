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
            "sandbox": {
                "sandbox_idle_timeout_seconds": 120,
                "jiuwen_sandbox": {"cpu": 2000, "memory": 4096},
            }
        },
        "jiuwenbox": {"network": {"egress": {"allowed_ips": ["127.0.0.1/32"]}}},
    }
    section, metadata = extract_section(doc, "gateway")

    assert section == {
        "sandbox": {
            "sandbox_idle_timeout_seconds": 120,
            "jiuwen_sandbox": {"cpu": 2000, "memory": 4096},
        }
    }
    assert metadata == {"_version": 1, "_revision": 7}


def test_extract_section_handles_missing_or_invalid_documents():
    assert extract_section({"_version": 1}, "gateway") == ({}, {"_version": 1})
    assert extract_section({"gateway": []}, "gateway") == ({}, {})
    assert extract_section(None, "gateway") == ({}, {})


def test_filter_managed_section_keeps_agent_sandbox_fields():
    managed, ignored = filter_managed_section(
        {
            "sandbox": {
                "sandbox_idle_timeout_seconds": 120,
                "jiuwen_sandbox": {"cpu": 2000, "memory": 4096},
                "type": "yuanrong",
                "cpu": 9999,
            },
            "channels": {"web": {"enabled": False}},
        }
    )

    assert managed == {
        "sandbox": {
            "sandbox_idle_timeout_seconds": 120,
            "jiuwen_sandbox": {"cpu": 2000, "memory": 4096},
        }
    }
    assert ignored == ["sandbox.type", "sandbox.cpu", "channels"]


def test_filter_managed_section_preserves_explicit_null_timeout():
    managed, ignored = filter_managed_section(
        {"sandbox": {"sandbox_idle_timeout_seconds": None}}
    )

    assert managed == {"sandbox": {"sandbox_idle_timeout_seconds": None}}
    assert ignored == []


def test_merge_section_preserves_unmanaged_local_config():
    local = {
        "gateway": {"cron": {"store_backend": "etcd"}},
        "sandbox": {"type": "yuanrong", "cpu": 1000},
    }
    merged = merge_section(
        local,
        {"sandbox": {"sandbox_idle_timeout_seconds": 120}},
    )

    assert merged == {
        "gateway": {"cron": {"store_backend": "etcd"}},
        "sandbox": {
            "type": "yuanrong",
            "cpu": 1000,
            "sandbox_idle_timeout_seconds": 120,
        },
    }


def test_validate_accepts_agent_sandbox_fields():
    assert validate_section(
        {
            "sandbox": {
                "sandbox_idle_timeout_seconds": 600,
                "jiuwen_sandbox": {"cpu": 2000, "memory": 4096},
            }
        }
    ) == []


def test_validate_timeout_values():
    assert validate_section(
        {"sandbox": {"sandbox_idle_timeout_seconds": 0}}
    ) == []
    assert validate_section(
        {"sandbox": {"sandbox_idle_timeout_seconds": -1}}
    ) == []


@pytest.mark.parametrize("value", [None, True, "invalid", float("nan"), float("inf")])
def test_validate_rejects_invalid_timeout(value):
    errors = validate_section({"sandbox": {"sandbox_idle_timeout_seconds": value}})
    assert len(errors) == 1
    assert "sandbox_idle_timeout_seconds" in errors[0]


@pytest.mark.parametrize("value", [0, -1, True, "big", 1.5, float("inf")])
def test_validate_rejects_invalid_resources(value):
    assert validate_section({"sandbox": {"jiuwen_sandbox": {"cpu": value}}})
    assert validate_section({"sandbox": {"jiuwen_sandbox": {"memory": value}}})


def test_validate_ignores_unmanaged_fields():
    assert validate_section({"sandbox": {"cpu": 2000}}) == []


def test_normalize_agent_sandbox_fields():
    section = {
        "sandbox": {
            "sandbox_idle_timeout_seconds": "120",
            "jiuwen_sandbox": {"cpu": "2000", "memory": "4096"},
        }
    }
    result = normalize_section(section)

    assert result is section
    assert section == {
        "sandbox": {
            "sandbox_idle_timeout_seconds": 120,
            "jiuwen_sandbox": {"cpu": 2000, "memory": 4096},
        }
    }
