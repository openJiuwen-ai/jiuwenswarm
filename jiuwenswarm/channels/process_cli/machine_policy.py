# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Process-local output and usage limits for a single machine invocation."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from jsonschema import Draft202012Validator, SchemaError, ValidationError

from jiuwenswarm.channels.process_cli.protocol.model import _thaw_json
from jiuwenswarm.runtime.events import RuntimeEvent


def validate_output_schema(schema: Mapping[str, Any]) -> None:
    """Reject malformed schemas before starting an Agent or charging a model."""
    try:
        Draft202012Validator.check_schema(_thaw_json(schema))
    except SchemaError as error:
        raise ValueError("output_schema is not a valid JSON Schema") from error


def structured_prompt(input_text: str, schema: Mapping[str, Any]) -> str:
    """Ask the model for a JSON object while retaining the caller's request."""
    contract = json.dumps(_thaw_json(schema), ensure_ascii=False, separators=(",", ":"))
    return (
        f"{input_text}\n\n"
        "Return exactly one JSON object conforming to this JSON Schema. "
        "Do not use Markdown fences or text outside the JSON object.\n"
        f"JSON Schema: {contract}"
    )


def parse_structured_output(
    output: str | None, schema: Mapping[str, Any]
) -> dict[str, Any]:
    """Only a fully valid JSON object can make a structured run succeed."""
    try:
        value = json.loads(output or "")
    except (TypeError, ValueError) as error:
        raise ValueError("Agent output is not a JSON object") from error
    if not isinstance(value, dict):
        raise ValueError("Agent output is not a JSON object")
    try:
        Draft202012Validator(_thaw_json(schema)).validate(value)
    except ValidationError as error:
        raise ValueError("Agent output does not match output_schema") from error
    return value


class RunLimits:
    """Meter one run from Runtime model usage events, without shared state."""

    def __init__(self, *, max_turns: int | None, max_budget_usd: float | None) -> None:
        self.max_turns = max_turns
        self.max_budget_usd = max_budget_usd
        self.model_calls = 0
        self.total_cost = 0.0

    def observe(self, event: RuntimeEvent) -> str | None:
        """Return a stable failure code when a configured limit is exceeded."""
        if event.event_type != "chat.usage_metadata":
            return None
        payload = event.payload if isinstance(event.payload, Mapping) else {}
        metadata = payload.get("metadata")
        usage = metadata.get("usage_metadata") if isinstance(metadata, Mapping) else None
        if not isinstance(usage, Mapping):
            return "BUDGET_METER_UNAVAILABLE" if self.max_budget_usd is not None else None
        self.model_calls += 1
        if self.max_budget_usd is not None:
            raw = usage.get("total_cost")
            if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                return "BUDGET_METER_UNAVAILABLE"
            if not math.isfinite(raw) or raw < 0:
                return "BUDGET_METER_UNAVAILABLE"
            self.total_cost += float(raw)
            if self.total_cost > self.max_budget_usd:
                return "BUDGET_EXCEEDED"
        if self.max_turns is not None and self.model_calls > self.max_turns:
            return "TURN_LIMIT_EXCEEDED"
        return None

    def add_usage(self, usage: Mapping[str, Any]) -> dict[str, Any]:
        result = deepcopy(dict(usage))
        if self.max_turns is not None or self.max_budget_usd is not None:
            result["model_calls"] = self.model_calls
        return result
