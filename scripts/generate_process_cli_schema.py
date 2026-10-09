# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Generate the published Process CLI schema without importing Runtime."""

from __future__ import annotations

import json
from pathlib import Path


TEXT = {"type": "string", "pattern": r"\S"}
OPTIONAL_TEXT = {"type": ["string", "null"], "pattern": r"\S"}
STRINGS = {"type": "array", "items": TEXT, "uniqueItems": True}
NUMBER = {"type": ["number", "null"], "exclusiveMinimum": 0}
NULLABLE_OBJECT = {"type": ["object", "null"]}
VERSION = {"const": "0.1"}
MODES = [
    "agent.work.normal",
    "agent.work.plan",
    "agent.code.normal",
    "agent.code.plan",
]


def ref(name: str) -> dict:
    """Reference a reusable definition in the generated document."""
    return {"$ref": f"#/$defs/{name}"}


def obj(properties: dict, required: tuple = (), *, extra: bool = False) -> dict:
    """Create an object contract with explicit field and extension policy."""
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": extra,
    }


class _SchemaBuilder:
    """Assemble independent input, output, event and catalog definitions."""

    def __init__(self) -> None:
        self.defs = {}
        self.operations = {}
        self.correlation = {}
        self.envelope = {}
        self.terminal = {}

    def _inputs(self) -> None:
        self.defs["workspace"] = obj(
            {
                "cwd": OPTIONAL_TEXT,
                "project_dir": OPTIONAL_TEXT,
                "trusted_dirs": STRINGS,
            }
        )
        tools = {
            **STRINGS,
            "allOf": [{"if": {"contains": {"const": "*"}}, "then": {"maxItems": 1}}],
            "description": (
                "Omitted: configured tools. []: no tools. "
                "['*']: configured tools. null: invalid."
            ),
        }
        self.defs["agent"] = obj(
            {
                "name": {"type": "string", "pattern": r"^[A-Za-z0-9_-]{3,50}$"},
                "instructions": TEXT,
                "description": OPTIONAL_TEXT,
                "model": OPTIONAL_TEXT,
                "tools": tools,
                "skills": STRINGS,
                "max_iterations": {"type": ["integer", "null"], "minimum": 1},
            },
            ("name", "instructions"),
        )
        self.defs["host_tool"] = obj(
            {
                "name": {"type": "string", "pattern": r"^[A-Za-z][A-Za-z0-9_-]{0,63}$"},
                "description": TEXT,
                "input_schema": {
                    "type": "object",
                    "properties": {"type": {"const": "object"}},
                    "required": ["type"],
                },
            },
            ("name", "description", "input_schema"),
        )
        self.defs["permissions"] = obj(
            {
                "tools": {
                    "type": "object",
                    "propertyNames": TEXT,
                    "additionalProperties": {"enum": ["allow", "ask", "deny"]},
                }
            }
        )
        self.defs["run"] = obj(
            {
                "schema_version": VERSION,
                "type": {"const": "run"},
                "request_id": OPTIONAL_TEXT,
                "session_id": OPTIONAL_TEXT,
                "input": TEXT,
                "agent": {"anyOf": [ref("agent"), {"type": "null"}]},
                "mode": {"enum": MODES + [None]},
                "model": OPTIONAL_TEXT,
                "skills": {"anyOf": [STRINGS, {"type": "null"}]},
                "mcp": {"anyOf": [STRINGS, {"type": "null"}]},
                "permissions": {"anyOf": [ref("permissions"), {"type": "null"}]},
                "output_schema": {
                    "type": ["object", "null"],
                    "properties": {"type": {"const": "object"}},
                    "required": ["type"],
                },
                "max_turns": {
                    "type": ["integer", "null"],
                    "minimum": 1,
                    "maximum": 1000,
                },
                "max_budget_usd": NUMBER,
                "host_tools": {"type": "array", "items": ref("host_tool")},
                "workspace": {"anyOf": [ref("workspace"), {"type": "null"}]},
                "timeout_seconds": NUMBER,
            },
            ("schema_version", "type", "input"),
        )

    def _queries(self) -> None:
        self.operations = {
            "protocol.capabilities": ({}, ()),
            "protocol.schema": ({}, ()),
            "session.get": ({"session_id": TEXT}, ("session_id",)),
            "session.list": (
                {
                    "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                    "offset": {"type": "integer", "minimum": 0},
                    "search": {"type": "string", "maxLength": 200},
                },
                (),
            ),
            "model.list": ({}, ()),
            "model.resolve": ({"requested": TEXT}, ("requested",)),
            "mode.list": ({}, ()),
            "mode.resolve": ({"requested": TEXT}, ("requested",)),
            "permission.get": ({"session_id": TEXT}, ()),
            "mcp.validate": (
                {"references": {"type": "array", "items": TEXT}},
                ("references",),
            ),
        }
        self.defs["query"] = obj(
            {
                "schema_version": VERSION,
                "type": {"const": "query"},
                "request_id": OPTIONAL_TEXT,
                "operation": {"enum": list(self.operations)},
                "params": {"type": "object"},
                "workspace": {"anyOf": [ref("workspace"), {"type": "null"}]},
                "timeout_seconds": NUMBER,
            },
            ("schema_version", "type", "operation"),
        )
        self.defs["query"]["allOf"] = [
            {
                "if": {
                    "properties": {"operation": {"const": name}},
                    "required": ["operation"],
                },
                "then": {
                    "properties": {"params": obj(properties, required)},
                    "required": ["params"] if required else [],
                },
            }
            for name, (properties, required) in self.operations.items()
        ]

    def _controls(self) -> None:
        self.correlation = {
            "schema_version": VERSION,
            "request_id": TEXT,
            "session_id": OPTIONAL_TEXT,
        }
        self.defs["answer_item"] = obj(
            {
                "question": {"type": "string"},
                "selected_options": {"type": "array", "items": {"type": "string"}},
                "custom_input": {"type": "string"},
                "card_id": TEXT,
            },
            extra=True,
        )
        self.defs["answer"] = obj(
            {
                **self.correlation,
                "type": {"const": "answer"},
                "interaction_id": TEXT,
                "answers": {
                    "type": "array",
                    "minItems": 1,
                    "items": ref("answer_item"),
                },
            },
            (
                "schema_version",
                "type",
                "request_id",
                "session_id",
                "interaction_id",
                "answers",
            ),
        )
        self.defs["answer"]["properties"]["session_id"] = TEXT
        self.defs["cancel"] = obj(
            {**self.correlation, "type": {"const": "cancel"}},
            ("schema_version", "type", "request_id"),
        )
        self.defs["tool_result"] = obj(
            {
                **self.correlation,
                "type": {"const": "tool_result"},
                "call_id": TEXT,
                "result": {},
                "error": TEXT,
            },
            ("schema_version", "type", "request_id", "session_id", "call_id"),
        )
        self.defs["tool_result"]["properties"]["session_id"] = TEXT
        self.defs["tool_result"]["oneOf"] = [
            {"required": ["result"], "not": {"required": ["error"]}},
            {"required": ["error"], "not": {"required": ["result"]}},
        ]
        self.defs["control"] = {
            "oneOf": [ref(name) for name in ("answer", "cancel", "tool_result")]
        }
        self.defs["error"] = obj(
            {
                "code": TEXT,
                "message": TEXT,
                "retryable": {"type": "boolean"},
                "details": {"type": "object"},
            },
            ("code", "message"),
            extra=True,
        )

    def _results(self) -> None:
        self.envelope = {
            **self.correlation,
            "sequence": {"type": "integer", "minimum": 0},
        }
        self.terminal = {
            **self.envelope,
            "status": {"enum": ["completed", "failed", "cancelled", "timed_out"]},
            "exit_code": {"enum": [0, 1, 2, 124, 130]},
            "error": {"anyOf": [ref("error"), {"type": "null"}]},
        }
        self.defs["terminal_state"] = {
            "oneOf": [
                {
                    "properties": {
                        "status": {"const": "completed"},
                        "exit_code": {"const": 0},
                        "error": {"type": "null"},
                    }
                },
                {
                    "properties": {
                        "status": {"const": "failed"},
                        "exit_code": {"enum": [1, 2]},
                        "error": ref("error"),
                    },
                    "required": ["error"],
                },
                {
                    "properties": {
                        "status": {"const": "cancelled"},
                        "exit_code": {"const": 130},
                        "error": ref("error"),
                    },
                    "required": ["error"],
                },
                {
                    "properties": {
                        "status": {"const": "timed_out"},
                        "exit_code": {"const": 124},
                        "error": ref("error"),
                    },
                    "required": ["error"],
                },
            ]
        }
        self.defs["result"] = obj(
            {
                **self.terminal,
                "type": {"const": "result"},
                "output": {"type": ["string", "null"]},
                "output_json": NULLABLE_OBJECT,
                "usage": {"type": "object"},
            },
            (
                "schema_version",
                "type",
                "sequence",
                "request_id",
                "session_id",
                "status",
                "exit_code",
                "output",
                "output_json",
                "error",
                "usage",
            ),
            extra=True,
        )
        self.defs["result"]["allOf"] = [
            ref("terminal_state"),
            {
                "if": {"properties": {"status": {"const": "completed"}}},
                "then": {"properties": {"session_id": TEXT}},
            },
        ]
        self.defs["query_result"] = obj(
            {
                **self.terminal,
                "type": {"const": "query_result"},
                "session_id": {"type": "null"},
                "sequence": {"const": 0},
                "operation": {"enum": list(self.operations) + [None]},
                "data": NULLABLE_OBJECT,
            },
            (
                "schema_version",
                "type",
                "sequence",
                "request_id",
                "session_id",
                "operation",
                "status",
                "exit_code",
                "data",
                "error",
            ),
            extra=True,
        )
        self.defs["query_result"]["allOf"] = [
            ref("terminal_state"),
            {
                "if": {"properties": {"status": {"const": "completed"}}},
                "then": {
                    "properties": {
                        "data": {"type": "object"},
                        "operation": {"enum": list(self.operations)},
                    }
                },
            },
        ]

    def _events(self) -> None:
        self.defs["question"] = obj(
            {
                "question_id": TEXT,
                "question": {"type": "string"},
                "options": {
                    "type": "array",
                    "items": obj(
                        {"value": {"type": "string"}, "label": {"type": "string"}},
                        ("value", "label"),
                        extra=True,
                    ),
                },
                "card_id": OPTIONAL_TEXT,
                "allow_custom_input": {"type": "boolean"},
            },
            ("question_id", "question", "options", "card_id", "allow_custom_input"),
            extra=True,
        )
        self.defs["text_payload"] = obj(
            {"text": {"type": "string"}}, ("text",), extra=True
        )
        self.defs["interaction_payload"] = obj(
            {
                "interaction_id": TEXT,
                "kind": {"enum": ["permission", "question", "confirmation"]},
                "questions": {"type": "array", "items": ref("question")},
                "interaction": {"type": "object"},
            },
            ("interaction_id", "kind", "questions", "interaction"),
            extra=True,
        )
        self.defs["host_tool_payload"] = obj(
            {"call_id": TEXT, "name": TEXT, "arguments": {"type": "object"}},
            ("call_id", "name", "arguments"),
            extra=True,
        )
        self.defs["tool_observation_payload"] = obj(
            {
                "tool": obj(
                    {
                        "call_id": {"type": "string"},
                        "name": {"type": "string"},
                        "arguments": NULLABLE_OBJECT,
                        "status": {"enum": ["started", "completed", "failed"]},
                        "result": {},
                    },
                    ("call_id", "name", "arguments", "status", "result"),
                    extra=True,
                )
            },
            ("tool",),
            extra=True,
        )
        self.defs["event"] = obj(
            {
                **self.envelope,
                "type": {"const": "event"},
                "event_type": TEXT,
                "payload": NULLABLE_OBJECT,
            },
            (
                "schema_version",
                "type",
                "sequence",
                "request_id",
                "session_id",
                "event_type",
                "payload",
            ),
            extra=True,
        )
        self.defs["event"]["allOf"] = [
            {
                "if": {
                    "properties": {"event_type": {"enum": names}},
                    "required": ["event_type"],
                },
                "then": {"properties": {"payload": ref(payload)}},
            }
            for names, payload in (
                (["chat.delta", "chat.final"], "text_payload"),
                (["chat.tool_call", "chat.tool_result"], "tool_observation_payload"),
                (["interaction.requested"], "interaction_payload"),
                (["host_tool.requested"], "host_tool_payload"),
            )
        ]

    def _capabilities(self) -> None:
        self.defs["capabilities"] = obj(
            {
                "schema_version": VERSION,
                "supported_schema_versions": {"type": "array", "items": TEXT},
                "protocol_revision": {"type": "integer", "minimum": 1},
                "features": {
                    "type": "object",
                    "additionalProperties": {"type": "boolean"},
                },
                "run_fields": STRINGS,
                "query_operations": STRINGS,
                "control_types": STRINGS,
                "stable_event_types": STRINGS,
                "input_encoding": {"const": "utf-8"},
                "output_encoding": {"const": "utf-8"},
                "output_format": {"const": "jsonl"},
                "max_input_bytes": {"type": "integer", "minimum": 1},
                "compatibility": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                },
            },
            (
                "schema_version",
                "supported_schema_versions",
                "protocol_revision",
                "features",
                "run_fields",
                "query_operations",
                "control_types",
                "stable_event_types",
                "input_encoding",
                "output_encoding",
                "output_format",
                "max_input_bytes",
                "compatibility",
            ),
            extra=True,
        )

    def _catalogs(self) -> None:
        self.defs["mode"] = obj(
            {
                "mode": {"enum": MODES},
                "work_mode": {"enum": ["work", "code"]},
                "is_plan": {"type": "boolean"},
                "supports_custom_agent_definitions": {"type": "boolean"},
            },
            ("mode", "work_mode", "is_plan", "supports_custom_agent_definitions"),
            extra=True,
        )
        model_fields = {
            name: {"type": "string"}
            for name in (
                "selection_key",
                "display_name",
                "model_name",
                "provider",
                "reasoning_level",
            )
        }
        model_fields.update(
            {
                name: {"type": "boolean"}
                for name in ("is_default", "is_agentos", "is_current")
            }
        )
        self.defs["model"] = obj(model_fields, model_fields, extra=True)
        session_fields = {
            name: {"type": "string"}
            for name in (
                "session_id",
                "channel_id",
                "title",
                "mode",
                "work_mode",
                "project_id",
                "project_dir",
                "model",
            )
        }
        session_fields.update(
            {name: {"type": "number"} for name in ("created_at", "last_message_at")}
        )
        session_fields["message_count"] = {"type": "integer"}
        self.defs["session"] = obj(session_fields, session_fields, extra=True)
        permission_level = {"enum": ["allow", "ask", "deny"]}
        self.defs["permission_rule"] = obj(
            {
                "id": {"type": "string"},
                "tools": STRINGS,
                "pattern": {"type": "string"},
                "action": {"enum": ["allow", "ask", "deny", None]},
                "severity": {"type": "string"},
                "description": {"type": "string"},
                "match_type": {"type": "string"},
            },
            extra=True,
        )
        self.defs["permission_layer"] = obj(
            {
                "enabled": {"type": ["boolean", "null"]},
                "tools": {
                    "type": "array",
                    "items": obj(
                        {"name": TEXT, "level": permission_level},
                        ("name", "level"),
                        extra=True,
                    ),
                },
                "rules": {"type": "array", "items": ref("permission_rule")},
            },
            ("enabled", "tools", "rules"),
            extra=True,
        )
        self.defs["permission_snapshot"] = obj(
            {
                "scope": {"enum": ["host", "session"]},
                "session_id": {"type": "string"},
                **{
                    name: ref("permission_layer")
                    for name in ("global", "user", "session", "effective")
                },
            },
            ("scope", "session_id", "global", "user", "session", "effective"),
            extra=True,
        )

    def _query_results(self) -> None:
        query_data = {
            "protocol.capabilities": ref("capabilities"),
            "protocol.schema": {
                "type": "object",
                "required": ["$schema", "$id", "$defs", "oneOf", "x-protocol-revision"],
            },
            "mode.list": obj(
                {"modes": {"type": "array", "items": ref("mode")}},
                ("modes",),
                extra=True,
            ),
            "mode.resolve": ref("mode"),
            "model.list": obj(
                {
                    "models": {"type": "array", "items": ref("model")},
                    "current_selection": {"type": "string"},
                    "current_display_name": {"type": "string"},
                },
                ("models", "current_selection", "current_display_name"),
                extra=True,
            ),
            "model.resolve": ref("model"),
            "session.get": obj(
                {"session": {"anyOf": [ref("session"), {"type": "null"}]}},
                ("session",),
                extra=True,
            ),
            "session.list": obj(
                {
                    "sessions": {"type": "array", "items": ref("session")},
                    **{
                        name: {"type": "integer", "minimum": 0}
                        for name in ("total", "limit", "offset")
                    },
                },
                ("sessions", "total", "limit", "offset"),
                extra=True,
            ),
            "permission.get": ref("permission_snapshot"),
            "mcp.validate": obj(
                {
                    "valid": {"type": "boolean"},
                    "references": {
                        "type": "array",
                        "items": obj(
                            {
                                "name": TEXT,
                                "status": {"enum": ["ready", "not_ready", "missing"]},
                            },
                            ("name", "status"),
                            extra=True,
                        ),
                    },
                },
                ("valid", "references"),
                extra=True,
            ),
        }
        self.defs["query_result"]["allOf"].extend(
            {
                "if": {
                    "properties": {
                        "status": {"const": "completed"},
                        "operation": {"const": operation},
                    }
                },
                "then": {"properties": {"data": data_schema}},
            }
            for operation, data_schema in query_data.items()
        )

    def _usage(self) -> None:
        self.defs["usage"] = obj(
            {
                name: {"type": "number", "minimum": 0}
                for name in (
                    "input_tokens",
                    "output_tokens",
                    "total_tokens",
                    "cache_tokens",
                    "input_cost",
                    "output_cost",
                    "total_cost",
                    "model_calls",
                )
            },
            extra=True,
        )
        self.defs["result"]["properties"]["usage"] = ref("usage")

    def build(self) -> dict:
        """Return the complete public revision 1 contract."""
        self._inputs()
        self._queries()
        self._controls()
        self._results()
        self._events()
        self._capabilities()
        self._catalogs()
        self._query_results()
        self._usage()
        return {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": "urn:jiuwenswarm:process-cli:0.1:revision:1",
            "title": "JiuwenSwarm Process CLI protocol 0.1 revision 1",
            "x-protocol-revision": 1,
            "description": (
                "One UTF-8 JSON input/control/output record. "
                "Validate JSONL ordering and OS exit status separately. "
                "Unknown output fields and event types are compatible additions."
            ),
            "oneOf": [
                ref(name)
                for name in (
                    "run",
                    "query",
                    "answer",
                    "cancel",
                    "tool_result",
                    "event",
                    "result",
                    "query_result",
                )
            ],
            "$defs": self.defs,
        }

    def write_schema(self, destination: Path) -> None:
        """Publish the assembled schema as a UTF-8 JSON package resource."""
        destination.write_text(
            json.dumps(self.build(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )


def build_schema() -> dict:
    """Build the complete revision 1 contract without importing Runtime."""
    return _SchemaBuilder().build()


if __name__ == "__main__":
    schema_destination = (
        Path(__file__).resolve().parents[1]
        / "jiuwenswarm/channels/process_cli/protocol/schema.json"
    )
    _SchemaBuilder().write_schema(schema_destination)
