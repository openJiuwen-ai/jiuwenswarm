"""Celia's nine-tool MCP contract and its model-facing projection.

Identity, authorization scope and tracing come from the host request. Automatic
conversation ingestion owns memory_add; models only invoke explicit operations.
"""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from jsonschema import Draft202012Validator

CORE_TOOLS = {
    "memory_store",
    "memory_record_search",
    "memory_global_load",
    "memory_scene_load",
    "memory_scene_search",
}
ADVANCED_TOOLS = {"memory_backup", "memory_restore", "memory_update_config"}
INTERNAL_TOOLS = {"memory_add"}
HOST_FIELDS = {"userId", "sessionId", "traceId", "requestScope", "scope", "scopeFilter"}
MAX_CONTENT_BYTES = 81920


def _schema(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
    }


def _business_schema(
    name: str, description: str, properties: dict[str, Any], required: list[str]
) -> dict[str, Any]:
    return _schema(
        name,
        description,
        {
            "userId": {"type": "string", "description": "Memory owner identifier"},
            **properties,
            "sessionId": {"type": "string", "description": "Optional session ID"},
            "traceId": {"type": "string", "description": "Optional trace ID for observability"},
            "requestScope": {
                "type": "object",
                "description": "Dynamic isolation key-value pairs",
                "additionalProperties": {"type": "string"},
            },
        },
        ["userId", *required],
    )


def mcp_tool_schemas() -> list[dict[str, Any]]:
    """Return all nine wire schemas, including internal and optional tools."""
    content = {"type": "string", "description": "Content (max 81920 bytes)"}
    scope = {"type": "integer", "enum": [0, 1, 3], "description": "0=global, 1=user (default), 3=session"}
    return [
        _business_schema(
            "memory_add",
            "Record raw conversation and enqueue memory processing.",
            {
                "content": content,
                "seq": {
                    "description": "Optional conversation sequence identifier",
                    "oneOf": [{"type": "string"}, {"type": "number"}],
                },
                "role": {"type": "integer", "enum": [0, 1], "description": "0=user, 1=assistant; default 0"},
                "skipExtraction": {
                    "type": "integer",
                    "description": "1=skip extraction pipeline, 0=normal (default 0)",
                },
            },
            ["content"],
        ),
        _business_schema(
            "memory_store",
            "Extract and directly persist explicit long-term memories.",
            {
                "content": content,
                "scope": scope,
            },
            ["content"],
        ),
        _business_schema(
            "memory_record_search",
            "Search atomic facts or raw conversation chunks.",
            {
                "searchType": {
                    "type": "string",
                    "enum": ["atomic_fact", "raw_conv"],
                    "description": "Search target type",
                },
                "query": {"type": "string", "description": "Semantic search query"},
                "topK": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                    "description": "Maximum result count (default 5)",
                },
                "confidenceMin": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 1,
                    "description": "Minimum confidence score (default 0.0)",
                },
                "scopeFilter": scope,
                "dedupPolicy": {
                    "type": "integer",
                    "enum": [0, 1],
                    "description": "Raw chunk dedup policy, raw_conv only (default 0=off)",
                },
                "recallMode": {
                    "type": "integer",
                    "enum": [0, 1, 2],
                    "description": "Raw recall mode, raw_conv only (default 0=hybrid)",
                },
            },
            ["searchType", "query"],
        ),
        _business_schema(
            "memory_global_load", "Load the materialized global memory summary for one user.", {}, []
        ),
        _business_schema(
            "memory_scene_load",
            "Load materialized scene summaries by scene IDs.",
            {
                "sceneIds": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": 5,
                    "description": "Scene ID list to load",
                },
            },
            ["sceneIds"],
        ),
        _business_schema(
            "memory_scene_search",
            "Search scenes by subSceneTag semantic similarity.",
            {
                "subSceneTag": {"type": "string", "description": "Sub-scene tag to search"},
                "topK": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 10,
                    "description": "Maximum scene hit count (default 1)",
                },
            },
            ["subSceneTag"],
        ),
        _business_schema(
            "memory_backup",
            "Backup complete memory and state data for a single UID into a portable bundle JSON.",
            {},
            [],
        ),
        _business_schema(
            "memory_restore",
            "Restore a backup bundle into the current memory instance. Set dryRun=1 to preview without writing.",
            {
                "bundleJson": {"type": "string", "description": "Complete backup bundle JSON to restore"},
                "dryRun": {
                    "type": "integer",
                    "enum": [0, 1],
                    "description": "1=preview only (no write), 0=actual restore (default 0)",
                },
            },
            ["bundleJson"],
        ),
        _schema(
            "memory_update_config",
            "Hot-update pipeline switch configuration at runtime.",
            {
                "updates": {
                    "type": "object",
                    "description": 'Pipeline and global config updates, e.g. {"migration": {"inProgress": true}}',
                    "additionalProperties": True,
                },
            },
            ["updates"],
        ),
    ]


def tool_schemas(advanced: set[str] | None = None) -> list[dict[str, Any]]:
    """Project supported business arguments into schemas visible to the model."""
    names = CORE_TOOLS | (ADVANCED_TOOLS & (advanced or set()))
    result = []
    for schema in mcp_tool_schemas():
        if schema["name"] not in names:
            continue
        current = deepcopy(schema)
        parameters = current["parameters"]
        parameters["properties"] = {
            key: value for key, value in parameters["properties"].items() if key not in HOST_FIELDS
        }
        parameters["required"] = [key for key in parameters["required"] if key not in HOST_FIELDS]
        result.append(current)
    return result


_WIRE_VALIDATORS = {item["name"]: Draft202012Validator(item["parameters"]) for item in mcp_tool_schemas()}
_MODEL_VALIDATORS = {
    item["name"]: Draft202012Validator(item["parameters"]) for item in tool_schemas(ADVANCED_TOOLS)
}


def validate_arguments(name: str, arguments: dict[str, Any], *, model_facing: bool = False) -> None:
    """Reject stale fields and invalid bounds before making any MCP call."""
    validators = _MODEL_VALIDATORS if model_facing else _WIRE_VALIDATORS
    validator = validators.get(name)
    if validator is None:
        raise ValueError(f"Unsupported Celia tool: {name}")
    # Do not include the submitted content or credentials in validation errors.
    error = next(validator.iter_errors(arguments), None)
    if error is not None:
        field = ".".join(str(part) for part in error.path) or "arguments"
        raise ValueError(f"Invalid {name} {field}: {error.validator}")
    content = arguments.get("content")
    if isinstance(content, str) and len(content.encode("utf-8")) > MAX_CONTENT_BYTES:
        raise ValueError(f"{name} content exceeds {MAX_CONTENT_BYTES} bytes")


def disabled_payload(tool_name: str) -> str:
    return json.dumps(
        {
            "ok": False,
            "results": [],
            "message": "Memory extraction is disabled; use memory_record_search with searchType='raw_conv'.",
            "_memory_state": {"memoryState": 0, "skipped": True, "reason": "memory_disabled"},
            "reason": "memory_disabled",
            "tool": tool_name,
            "alternative_tool": "memory_record_search",
            "alternative_arguments": {"searchType": "raw_conv"},
        },
        ensure_ascii=False,
    )
