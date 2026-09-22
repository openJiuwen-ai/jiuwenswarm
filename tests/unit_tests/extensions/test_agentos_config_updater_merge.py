# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Unit tests for AgentOS config updater merge helpers; pure functions."""

from __future__ import annotations

import pytest

from jiuwenswarm.extensions.agentos.config_updater.merge import (
    extract_section,
    filter_managed_section,
    merge_section,
    normalize_section,
    validate_section,
)


# --------------------------------------------------------------------------
# extract_section
# --------------------------------------------------------------------------

def test_extract_section_extracts_component_and_metadata():
    doc = {
        "_version": 1,
        "_revision": 7,
        "_updated_at": "2026-09-04T10:00:00+08:00",
        "gateway": {"sandbox": {"cpu": 2000}},
        "jiuwenbox": {"network": {}},
    }
    section, metadata = extract_section(doc, "gateway")

    assert section == {"sandbox": {"cpu": 2000}}
    assert metadata == {
        "_version": 1,
        "_revision": 7,
        "_updated_at": "2026-09-04T10:00:00+08:00",
    }


def test_extract_section_metadata_not_in_section():
    section, _ = extract_section({"_version": 1, "gateway": {}}, "gateway")
    assert "_version" not in section


def test_extract_section_missing_component_returns_empty():
    section, metadata = extract_section({"_version": 1}, "gateway")
    assert section == {}
    assert metadata == {"_version": 1}


def test_extract_section_non_mapping_section_returns_empty():
    section, _ = extract_section({"gateway": ["not", "a", "dict"]}, "gateway")
    assert section == {}


def test_extract_section_non_mapping_document_returns_empty():
    assert extract_section(None, "gateway") == ({}, {})
    assert extract_section("string", "gateway") == ({}, {})


# --------------------------------------------------------------------------
# filter_managed_section
# --------------------------------------------------------------------------

def test_filter_managed_section_keeps_only_allowlisted_fields():
    section = {
        "gateway": {
            "agentos": {
                "sandbox_idle_timeout_seconds": 120,
                "workspace_root": "/tmp/override",
            },
            "cron": {"store_backend": "file"},
        },
        "sandbox": {
            "cpu": 2000,
            "memory": 4096,
            "type": "other",
            "image": "other:latest",
        },
        "channels": {"web": {"enabled": False}},
    }

    managed, ignored = filter_managed_section(section)

    assert managed == {
        "gateway": {"agentos": {"sandbox_idle_timeout_seconds": 120}},
        "sandbox": {"cpu": 2000, "memory": 4096},
    }
    assert ignored == [
        "gateway.agentos.workspace_root",
        "gateway.cron",
        "sandbox.type",
        "sandbox.image",
        "channels",
    ]


def test_filter_managed_section_ignores_malformed_parent_nodes():
    managed, ignored = filter_managed_section(
        {"gateway": "invalid", "sandbox": None}
    )

    assert managed == {}
    assert ignored == ["gateway", "sandbox"]


def test_filter_managed_section_preserves_explicit_null_for_validation():
    managed, ignored = filter_managed_section({"sandbox": {"cpu": None}})

    assert managed == {"sandbox": {"cpu": None}}
    assert ignored == []


# --------------------------------------------------------------------------
# merge_section
# --------------------------------------------------------------------------

def test_merge_remote_overrides_same_scalar():
    assert merge_section({"cpu": 1000}, {"cpu": 2000}) == {"cpu": 2000}


def test_merge_local_only_keys_preserved():
    local = {"type": "yuanrong", "image": "img:1", "cpu": 1000}
    remote = {"cpu": 2000}
    merged = merge_section(local, remote)

    assert merged == {"type": "yuanrong", "image": "img:1", "cpu": 2000}


def test_merge_recurses_into_nested_dicts():
    local = {"gateway": {"agentos": {"a": 1, "b": 2}}}
    remote = {"gateway": {"agentos": {"b": 20, "c": 30}}}
    merged = merge_section(local, remote)

    assert merged == {"gateway": {"agentos": {"a": 1, "b": 20, "c": 30}}}


def test_merge_list_replaced_wholesale_not_appended():
    local = {"allowed_ips": ["a", "b"]}
    remote = {"allowed_ips": ["a"]}
    merged = merge_section(local, remote)

    assert merged == {"allowed_ips": ["a"]}


def test_merge_new_key_added():
    assert merge_section({}, {"cpu": 2000}) == {"cpu": 2000}


def test_merge_mutates_local_in_place():
    # Mutating in place is required to preserve ruamel's comment/format
    # metadata on the document we write back.
    local = {"a": {"b": 1}}
    remote = {"a": {"c": 2}}
    result = merge_section(local, remote)

    assert result is local
    assert local == {"a": {"b": 1, "c": 2}}


def test_merge_does_not_mutate_remote():
    remote = {"a": {"c": 2}}
    merge_section({"a": {"b": 1}}, remote)
    assert remote == {"a": {"c": 2}}


def test_merge_scalar_over_local_dict_replaces():
    # remote scalar where local holds a mapping -> remote wins wholesale
    merged = merge_section({"x": {"nested": 1}}, {"x": 5})
    assert merged == {"x": 5}


def test_merge_none_is_explicit_clear():
    assert merge_section({"x": 1}, {"x": None}) == {"x": None}


def test_merge_handles_non_dict_local():
    assert merge_section(None, {"cpu": 1}) == {"cpu": 1}
    assert merge_section([], {"cpu": 1}) == {"cpu": 1}


# --------------------------------------------------------------------------
# validate_section
# --------------------------------------------------------------------------

def test_validate_accepts_managed_fields():
    section = {
        "gateway": {"agentos": {"sandbox_idle_timeout_seconds": 600}},
        "sandbox": {"cpu": 2000, "memory": 4096},
    }
    assert validate_section(section) == []


def test_validate_rejects_non_positive_cpu():
    errors = validate_section({"sandbox": {"cpu": 0}})
    assert len(errors) == 1
    assert "sandbox.cpu" in errors[0]


def test_validate_rejects_negative_memory():
    errors = validate_section({"sandbox": {"memory": -1}})
    assert len(errors) == 1
    assert "sandbox.memory" in errors[0]


def test_validate_rejects_non_numeric_cpu():
    errors = validate_section({"sandbox": {"cpu": "big"}})
    assert len(errors) == 1


def test_validate_rejects_boolean_cpu():
    # bool is an int subclass -- must be caught explicitly.
    errors = validate_section({"sandbox": {"cpu": True}})
    assert len(errors) == 1


def test_validate_allows_idle_timeout_zero_and_negative():
    # <= 0 is a valid "disable reclamation" value, unlike cpu/memory.
    assert validate_section(
        {"gateway": {"agentos": {"sandbox_idle_timeout_seconds": 0}}}
    ) == []
    assert validate_section(
        {"gateway": {"agentos": {"sandbox_idle_timeout_seconds": -1}}}
    ) == []


def test_validate_ignores_unknown_fields_after_filtering_boundary():
    assert validate_section({"sandbox": {"unknown_field": "anything"}}) == []


@pytest.mark.parametrize(
    ("section", "field"),
    [
        ({"sandbox": {"cpu": None}}, "sandbox.cpu"),
        ({"sandbox": {"memory": None}}, "sandbox.memory"),
        (
            {"gateway": {"agentos": {"sandbox_idle_timeout_seconds": None}}},
            "gateway.agentos.sandbox_idle_timeout_seconds",
        ),
    ],
)
def test_validate_rejects_explicit_null_managed_field(section, field):
    errors = validate_section(section)

    assert len(errors) == 1
    assert field in errors[0]


@pytest.mark.parametrize("value", [0.5, "1.25"])
def test_validate_rejects_fractional_cpu_and_memory(value):
    assert validate_section({"sandbox": {"cpu": value}})
    assert validate_section({"sandbox": {"memory": value}})


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_validate_rejects_non_finite_managed_values(value):
    assert validate_section({"sandbox": {"cpu": value}})
    assert validate_section({"sandbox": {"memory": value}})
    assert validate_section(
        {"gateway": {"agentos": {"sandbox_idle_timeout_seconds": value}}}
    )


def test_validate_missing_fields_is_ok():
    assert validate_section({}) == []


def test_validate_accepts_numeric_strings():
    assert validate_section({"sandbox": {"cpu": "2000"}}) == []


# --------------------------------------------------------------------------
# normalize_section
# --------------------------------------------------------------------------

def test_normalize_coerces_numeric_strings():
    normalized = normalize_section({"sandbox": {"cpu": "2000", "memory": "4096"}})
    assert normalized == {"sandbox": {"cpu": 2000, "memory": 4096}}


def test_normalize_idle_timeout_whole_number_stays_int():
    # Whole numbers stay int so writing back does not turn a user's 600 into
    # 600.0; consumers cast to float as needed.
    normalized = normalize_section(
        {"gateway": {"agentos": {"sandbox_idle_timeout_seconds": "120"}}}
    )
    assert normalized["gateway"]["agentos"]["sandbox_idle_timeout_seconds"] == 120


def test_normalize_mutates_in_place():
    # In-place is required to preserve ruamel's nested CommentedMap types.
    section = {"sandbox": {"cpu": "2000"}}
    result = normalize_section(section)
    assert result is section
    assert section == {"sandbox": {"cpu": 2000}}


def test_normalize_leaves_unknown_fields_untouched():
    normalized = normalize_section({"sandbox": {"other": "x"}})
    assert normalized == {"sandbox": {"other": "x"}}
