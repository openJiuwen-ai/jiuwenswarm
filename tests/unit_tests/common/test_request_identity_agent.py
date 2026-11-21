# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Agent display identity metadata contract tests."""

from __future__ import annotations

from jiuwenswarm.common.request_identity import (
    AGENT_IDENTITY_METADATA_KEY,
    apply_agent_identity_metadata,
    normalize_agent_identity,
    normalize_agent_identity_query,
    read_agent_identity,
)


def test_normalize_agent_identity_query_maps_display_id_alias() -> None:
    query = {
        "agent_name": ["agent_svc_agent"],
        "agent_identity_id": ["svc_agent"],
        "runtime_template_id": ["svc"],
        "agent_template_id": ["agent"],
    }

    assert normalize_agent_identity_query(query) == {
        "agent_name": "agent_svc_agent",
        "agent_id": "svc_agent",
        "runtime_template_id": "svc",
        "agent_template_id": "agent",
    }
    assert normalize_agent_identity_query(None) == {}


def test_normalize_agent_identity_accepts_query_lists_and_first_wins() -> None:
    identity = normalize_agent_identity(
        {
            "agent_name": ["agent_svc_agent"],
            "agent_identity_id": ["svc_agent"],
            "runtime_template_id": "svc",
            "agent_template_id": "",
        },
        {"agent_template_id": "agent", "agent_name": "ignored"},
    )

    assert identity == {
        "agent_name": "agent_svc_agent",
        "runtime_template_id": "svc",
        "agent_template_id": "agent",
    }


def test_apply_agent_identity_metadata_writes_grouped_identity() -> None:
    meta = apply_agent_identity_metadata(
        {"method": "chat.send"},
        {
            "agent_name": "agent_svc_agent",
            "agent_id": "svc_agent",
            "runtime_template_id": "svc",
            "agent_template_id": "agent",
            "unrelated": "drop",
        },
    )

    assert meta["method"] == "chat.send"
    assert meta[AGENT_IDENTITY_METADATA_KEY] == {
        "agent_name": "agent_svc_agent",
        "agent_id": "svc_agent",
        "runtime_template_id": "svc",
        "agent_template_id": "agent",
    }


def test_apply_agent_identity_metadata_removes_empty_identity() -> None:
    meta = apply_agent_identity_metadata(
        {AGENT_IDENTITY_METADATA_KEY: {"agent_name": "old"}},
        {},
    )

    assert AGENT_IDENTITY_METADATA_KEY not in meta


def test_read_agent_identity_ignores_malformed_metadata() -> None:
    assert read_agent_identity(None) == {}
    assert read_agent_identity({"agent_identity": "oops"}) == {}
    assert read_agent_identity(
        {
            "agent_identity": {
                "agent_name": " agent_svc_agent ",
                "agent_id": "svc_agent",
            }
        }
    ) == {"agent_name": "agent_svc_agent", "agent_id": "svc_agent"}
