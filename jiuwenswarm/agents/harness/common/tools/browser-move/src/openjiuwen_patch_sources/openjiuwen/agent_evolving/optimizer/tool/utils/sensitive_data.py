# coding: utf-8
"""Keep authentication material out of optimizer prompts, logs and examples."""

import json
import re
from collections.abc import Mapping


REDACTED = "[REDACTED]"
_CREDENTIAL_NAMES = {
    "api_key", "access_key", "access_token", "refresh_token", "auth_token",
    "bearer_token", "id_token", "client_secret", "private_key", "session_id",
    "password", "passwd", "secret", "token", "credential", "authorization",
    "cookie", "auth", "key",
}
_CREDENTIAL_SUFFIXES = (
    "_api_key", "_access_key", "_access_token", "_refresh_token",
    "_auth_token", "_client_secret", "_private_key", "_password",
    "_passwd", "_credential", "_secret", "_token", "_auth",
    "_cookie", "_session_id", "_authorization",
)
_AUTH_TEXT = re.compile(
    r"(?i)(\b(?:authorization|api[_-]?key|access[_-]?token|password|"
    r"client[_-]?secret|private[_-]?key|session[_-]?id|"
    r"cookie|passwd|secret|token|auth|key)\b[\"']?\s*[:=]\s*"
    r"[\"']?(?:(?:bearer|basic)\s+)?[\"']?)([^\s,;\"'}\]]+)"
)


def is_credential_name(name):
    normalized = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(name))
    normalized = re.sub(r"[-.]", "_", normalized).lower()
    return normalized in _CREDENTIAL_NAMES or normalized.endswith(_CREDENTIAL_SUFFIXES)


def argument_dict(call):
    """Read tool-call arguments whether the model returned a dict or JSON text."""
    if not isinstance(call, Mapping):
        return None
    arguments = call.get("arguments")
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except (TypeError, ValueError):
            return None
    return arguments if isinstance(arguments, dict) else None


def credential_values(*objects):
    """Collect known secrets so copies in free text can also be removed."""
    values = set()

    def collect_literal(value):
        if isinstance(value, str) and len(value) >= 4 and value != REDACTED:
            values.add(value)
        elif isinstance(value, Mapping):
            for item in value.values():
                collect_literal(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                collect_literal(item)

    def visit(obj):
        if isinstance(obj, Mapping):
            for key, value in obj.items():
                if is_credential_name(key) and isinstance(value, str):
                    collect_literal(value)
                elif is_credential_name(key) and isinstance(value, Mapping):
                    if "type" in value or "$ref" in value or "properties" in value:
                        for field in ("default", "example", "examples", "const", "enum"):
                            if field in value:
                                collect_literal(value[field])
                        visit(value)
                    else:
                        collect_literal(value)
                else:
                    visit(value)
        elif isinstance(obj, (list, tuple)):
            for value in obj:
                visit(value)
        elif isinstance(obj, str):
            for match in _AUTH_TEXT.finditer(obj):
                if len(match.group(2)) >= 4 and match.group(2) != REDACTED:
                    values.add(match.group(2))

    for obj in objects:
        visit(obj)
    return values


def redact(value, secrets=()):
    """Return a new value; never mutate arguments needed for execution."""
    if isinstance(value, Mapping):
        return {
            key: _redact_credential_value(item, secrets)
            if is_credential_name(key) else redact(item, secrets)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item, secrets) for item in value]
    if isinstance(value, tuple):
        return tuple(redact(item, secrets) for item in value)
    if isinstance(value, str):
        if value.lstrip().startswith(("{", "[")):
            try:
                parsed = json.loads(value)
            except (TypeError, ValueError):
                pass
            else:
                return json.dumps(redact(parsed, secrets), ensure_ascii=False)
        for secret in sorted((str(s) for s in secrets if s), key=len, reverse=True):
            value = value.replace(secret, REDACTED)
        return _AUTH_TEXT.sub(lambda match: match.group(1) + REDACTED, value)
    return value


def _redact_credential_value(value, secrets):
    """Mask values while preserving the shape of credential parameter schemas."""
    if isinstance(value, Mapping):
        if "type" in value or "$ref" in value or "properties" in value:
            return {
                key: _mask_leaves(item) if key in {"default", "example", "examples", "const", "enum"}
                else redact(item, secrets)
                for key, item in value.items()
            }
        return _mask_leaves(value)
    if isinstance(value, (list, tuple)):
        return _mask_leaves(value)
    return REDACTED


def _mask_leaves(value):
    if isinstance(value, Mapping):
        return {key: _mask_leaves(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_mask_leaves(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_mask_leaves(item) for item in value)
    return REDACTED
