# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Configuration-independent protocol discovery for local SDK hosts."""

from __future__ import annotations

from typing import Any

from jiuwenswarm.channels.process_cli.protocol.contracts import (
    PROTOCOL_REVISION,
    STABLE_EVENT_TYPES,
)
from jiuwenswarm.channels.process_cli.protocol.schema import protocol_schema
from jiuwenswarm.channels.process_cli.protocol.version import (
    CURRENT_SCHEMA_VERSION,
    SUPPORTED_SCHEMA_VERSIONS,
)


def protocol_capabilities() -> dict[str, Any]:
    """Advertise implemented features; model/tool availability is queried separately."""
    schema = protocol_schema()
    return {
        "schema_version": CURRENT_SCHEMA_VERSION,
        "supported_schema_versions": list(SUPPORTED_SCHEMA_VERSIONS),
        "protocol_revision": PROTOCOL_REVISION,
        "features": {
            name: True
            for name in (
                "custom_agent",
                "run_model",
                "run_skills",
                "run_mcp",
                "run_permissions",
                "structured_output",
                "max_turns",
                "max_budget_usd",
                "host_tools",
                "interaction_callback",
                "unattended_permission_rejection",
                "cancel",
                "session_resume",
                "stable_event_fields",
                "field_errors",
                "empty_tools",
                "protocol_schema",
            )
        },
        "run_fields": list(schema["$defs"]["run"]["properties"]),
        "query_operations": schema["$defs"]["query"]["properties"]["operation"]["enum"],
        "control_types": ["answer", "tool_result", "cancel"],
        "stable_event_types": list(STABLE_EVENT_TYPES),
        "input_encoding": "utf-8",
        "output_encoding": "utf-8",
        "output_format": "jsonl",
        "max_input_bytes": 1024 * 1024,
        "compatibility": {
            "unknown_input_fields": "reject",
            "unknown_output_fields": "ignore",
            "unknown_event_types": "ignore",
            "feature_detection": "query protocol.capabilities before using optional features",
            "breaking_changes": "require a new schema_version",
        },
    }
