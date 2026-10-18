"""Compact wire/log serialization, including immutable mappings and UTF-16 text."""
import json
import math
from collections.abc import Mapping

from .observation.formatting import json_string, number_string


def loads(value):
    """Read standard JSON; Python's optional NaN/Infinity tokens are not wire JSON."""

    def invalid_constant(token):
        source = value if isinstance(value, str) else value.decode("utf-8", "replace")
        raise json.JSONDecodeError(f"Invalid JSON constant: {token}", source, 0)

    return json.loads(value, parse_constant=invalid_constant)


def _is_array_index(key):
    return (isinstance(key, str) and key.isascii() and key.isdecimal()
            and str(int(key)) == key and int(key) < 4294967295)


def dumps(value):
    active = set()

    def encode(value):
        if value is None:
            return "null"
        if value is True:
            return "true"
        if value is False:
            return "false"
        if isinstance(value, str):
            return json_string(value)
        if type(value) in (int, float):
            return "null" if isinstance(value, float) and not math.isfinite(value) else number_string(value)
        if isinstance(value, (Mapping, list, tuple)):
            identity = id(value)
            if identity in active:
                raise ValueError("circular value cannot be serialized")
            active.add(identity)
            try:
                if isinstance(value, Mapping):
                    # Numeric object property ordering is observable in budgets
                    # and original JSONL traces; preserve it during the port.
                    keys = list(value)
                    indexes = sorted((key for key in keys if _is_array_index(key)), key=int)
                    rest = [key for key in keys if key not in indexes]
                    return "{" + ",".join(json_string(str(key)) + ":" + encode(value[key])
                                          for key in indexes + rest) + "}"
                return "[" + ",".join(encode(child) for child in value) + "]"
            finally:
                active.remove(identity)
        if isinstance(value, BaseException):
            return encode({
                key: getattr(value, key)
                for key in ("name", "code", "effect", "target", "detail", "next") if hasattr(value, key)
            })
        raise TypeError(f"Cannot serialize {type(value).__name__}")

    return encode(value)


def utf8(value):
    # Buffer.byteLength replaces isolated surrogate code units with U+FFFD.
    return value.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace").encode("utf-8")
