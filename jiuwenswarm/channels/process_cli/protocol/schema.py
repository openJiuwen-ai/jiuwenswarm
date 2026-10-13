# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Published JSON Schema and safe field diagnostics for the machine protocol."""

from __future__ import annotations

import json
from functools import lru_cache
from importlib.resources import files
from typing import Any

from jsonschema import Draft202012Validator


def protocol_schema() -> dict[str, Any]:
    """Return a fresh copy of the packaged complete protocol schema."""
    document = files(__package__).joinpath("schema.json").read_text(encoding="utf-8")
    return json.loads(document)


@lru_cache(maxsize=3)
def _validator(kind: str) -> Draft202012Validator:
    schema = protocol_schema()
    return Draft202012Validator({"$ref": f"#/$defs/{kind}", "$defs": schema["$defs"]})


def input_error_details(value: object, kind: str) -> dict[str, Any]:
    """Locate a rejected input without reflecting its values or secret keys.

    DTO validation remains authoritative. Schema diagnostics are evaluated only
    on its failure, retaining existing successful input and error behavior.
    """
    try:
        error = next(_validator(kind).iter_errors(value), None)
    except (RecursionError, TypeError, ValueError, OverflowError):
        return {"field": "/", "reason": "invalid_value"}
    if error is None:
        return {"field": "/", "reason": "invalid_value"}
    while error.context:
        error = max(
            error.context,
            key=lambda item: (
                len(item.absolute_path),
                item.schema != {"type": "null"},
            ),
        )
    path = list(error.absolute_path)
    if error.validator == "required" and isinstance(error.instance, dict):
        missing = [key for key in error.validator_value if key not in error.instance]
        if missing:
            path.append(missing[0])
    field = "/" + "/".join(
        str(key).replace("~", "~0").replace("/", "~1") for key in path
    )
    reasons = {
        "type": "invalid_type",
        "required": "required",
        "additionalProperties": "unknown_field",
    }
    result = {"field": field, "reason": reasons.get(error.validator, "invalid_value")}
    if error.validator == "type":
        result["expected"] = error.validator_value
    return result
