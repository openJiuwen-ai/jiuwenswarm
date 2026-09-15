# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""One-shot, single-Agent machine contracts for the Process CLI."""

from jiuwenswarm.channels.process_cli.protocol.jsonl import (
    OneShotRecord,
    decode_jsonl,
    encode_jsonl,
    encode_jsonl_record,
    validate_one_shot_records,
)
from jiuwenswarm.channels.process_cli.protocol.model import (
    AgentSpec,
    JsonObject,
    JsonScalar,
    JsonValue,
    OneShotEvent,
    OneShotRunInput,
    OneShotRunResult,
    RunStatus,
    RuntimeErrorInfo,
    SingleAgentMode,
    WorkspaceSpec,
)
from jiuwenswarm.channels.process_cli.protocol.version import (
    CURRENT_SCHEMA_VERSION,
    SUPPORTED_SCHEMA_VERSIONS,
    is_schema_version_supported,
    require_supported_schema_version,
)

__all__ = [
    "AgentSpec",
    "CURRENT_SCHEMA_VERSION",
    "JsonObject",
    "JsonScalar",
    "JsonValue",
    "OneShotEvent",
    "OneShotRecord",
    "OneShotRunInput",
    "OneShotRunResult",
    "RunStatus",
    "RuntimeErrorInfo",
    "SUPPORTED_SCHEMA_VERSIONS",
    "SingleAgentMode",
    "WorkspaceSpec",
    "decode_jsonl",
    "encode_jsonl",
    "encode_jsonl_record",
    "is_schema_version_supported",
    "require_supported_schema_version",
    "validate_one_shot_records",
]
