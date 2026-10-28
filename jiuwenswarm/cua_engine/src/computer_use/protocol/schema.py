"""The small strict-schema subset used by native protocol v3.

Preserves field order, omitted-vs-null, and the original validation diagnostics.
No coercion, unknown-field stripping, or platform access is performed here.
"""
from __future__ import annotations

import json
import re
from typing import Any

from .primitives import MAX_SAFE_INTEGER, finite, trim, utf16_length

MISSING = object()


class SchemaError(ValueError):

    def __init__(self, issues: list[dict]):
        self.issues = issues
        super().__init__(json.dumps(issues, ensure_ascii=False, indent=2))


def _type(value: Any) -> str:
    if value is MISSING:
        return "undefined"
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        if not finite(value):
            if value != value:
                return "NaN"
            return "Infinity"
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"


def _invalid(expected, value, path, *, formatter=None):
    result = {"expected": expected}
    if formatter:
        result["format"] = formatter
    result["code"] = "invalid_type"
    received = _type(value)
    if received in ("NaN", "Infinity"):
        result["received"] = received
    result.update(path=path, message=f"Invalid input: expected {expected}, received {received}")
    return result


class Schema:

    def __init__(self, kind, **options):
        self.kind, self.options = kind, options

    def optional(self):
        return Schema("optional", inner=self)

    def nullable(self):
        return Schema("nullable", inner=self)

    def parse(self, value):
        issues = []
        result = self.parse_into(value, [], issues)
        if issues:
            raise SchemaError(issues)
        return result

    def parse_into(self, value, path, issues):
        kind, options = self.kind, self.options
        if kind == "optional" and value is MISSING:
            return MISSING
        if kind == "nullable" and value is None:
            return None
        if kind in ("optional", "nullable"):
            return options["inner"].parse_into(value, path, issues)
        if kind == "unknown":
            return value
        if kind == "union":
            errors = []
            for schema in options["schemas"]:
                errors_for_branch = []
                result = schema.parse_into(value, [], errors_for_branch)
                if not errors_for_branch:
                    return result
                errors.append(errors_for_branch)
            issues.append(dict(code="invalid_union", errors=errors, path=path, message="Invalid input"))
            return value
        if kind == "enum":
            if not any(type(value) is type(v) and value == v for v in options["values"]):
                values = options["values"]
                encoded = "|".join(json.dumps(v, ensure_ascii=False) for v in values)
                message = ("Invalid input: expected "
                           if len(values) == 1 else "Invalid option: expected one of ") + encoded
                issues.append(dict(code="invalid_value", values=values, path=path, message=message))
            return value
        expected = {"strictObject": "object"}.get(kind, kind)
        matches = {
            "string": isinstance(value, str), "boolean": isinstance(value, bool), "number": finite(value), "array":
            isinstance(value, list), "strictObject": isinstance(value, dict)
        }
        if not matches.get(kind):
            issues.append(_invalid(expected, value, path))
            if kind == "array" and isinstance(value, str):
                self._bounds(utf16_length(value), "string", path, issues)
            if kind == "string" and isinstance(value, list):
                self._bounds(len(value), "array", path, issues)
            return value
        if kind == "strictObject":
            result = {}
            for key, schema in options["fields"].items():
                if key not in value and schema.kind == "unknown":
                    issues.append(
                        dict(code="invalid_type", expected="nonoptional", path=path + [key],
                             message="Invalid input: expected nonoptional, received undefined"))
                    continue
                parsed = schema.parse_into(value.get(key, MISSING), path + [key], issues)
                if parsed is not MISSING:
                    result[key] = parsed
            unknown_keys = [k for k in value if k not in options["fields"]]
            if unknown_keys:
                message = "Unrecognized " + ("key: " if len(unknown_keys) == 1 else "keys: ") + \
                    ", ".join(json.dumps(k, ensure_ascii=False) for k in unknown_keys)
                issues.append(dict(code="unrecognized_keys", keys=unknown_keys, path=path, message=message))
            return result
        if kind == "array":
            result = [options["item"].parse_into(v, path + [i], issues) for i, v in enumerate(value)]
            self._bounds(len(value), "array", path, issues)
            return result
        if kind == "string":
            self._bounds(utf16_length(value), "string", path, issues)
            if "pattern" in options:
                if re.search(options["pattern"], value, re.ASCII) is None:
                    pattern = options["patternLabel"]
                    issues.append(
                        dict(origin="string", code="invalid_format", format="regex", pattern=pattern, path=path,
                             message=f"Invalid string: must match pattern {pattern}"))
            if options.get("nonblank") and not trim(value):
                issues.append(dict(code="custom", path=path, message="Invalid input"))
        if kind == "number":
            if options.get("integer"):
                if int(value) != value:
                    issues.append(_invalid("int", value, path, formatter="safeint"))
                    return value
                if abs(value) > MAX_SAFE_INTEGER:
                    big = value > 0
                    bound = MAX_SAFE_INTEGER if big else -MAX_SAFE_INTEGER
                    issue = {
                        "code": "too_big" if big else "too_small", "maximum" if big else "minimum": bound, "note":
                        "Integers must be within the safe integer range.", "origin": "int", "inclusive": True, "path":
                        path, "message":
                        f"{'Too big' if big else 'Too small'}: expected int to be {'<=' if big else '>='}{bound}"
                    }
                    issues.append(issue)
            self._bounds(value, "number", path, issues)
        return value

    def _bounds(self, value, origin, path, issues):
        for key in ("min", "max"):
            if key not in self.options:
                continue
            bound = self.options[key]
            inclusive = not (key == "min" and self.options.get("exclusiveMin"))
            invalid = (value < bound if inclusive else value <= bound) if key == "min" else value > bound
            if not invalid:
                continue
            small = key == "min"
            symbol = (">=" if inclusive else ">") if small else "<="
            suffix = " characters" if origin == "string" else " items" if origin == "array" else ""
            message = (f"{'Too small' if small else 'Too big'}: expected {origin} to "
                       f"{'have' if suffix else 'be'} {symbol}{bound}{suffix}")
            issues.append({
                "origin": origin, "code": "too_small" if small else "too_big", "minimum" if small else "maximum": bound,
                "inclusive": inclusive, "path": path, "message": message
            })


def string(**options):
    return Schema("string", **options)


def number(**options):
    return Schema("number", **options)


def boolean():
    return Schema("boolean")


def array(item, **options):
    return Schema("array", item=item, **options)


def obj(fields):
    return Schema("strictObject", fields=fields)


def enum(*values):
    return Schema("enum", values=list(values))


def union(*schemas):
    return Schema("union", schemas=schemas)


def unknown():
    return Schema("unknown")


ref = string(pattern=r"^(?:0|[1-9][0-9]*)\Z", patternLabel=r"/^(?:0|[1-9][0-9]*)(?![\s\S])/")
role = string(pattern=r"^[a-z][a-zA-Z0-9]*\Z", patternLabel=r"/^[a-z][a-zA-Z0-9]*$/")
count = number(integer=True, min=0, max=MAX_SAFE_INTEGER)
identity = string(nonblank=True)
