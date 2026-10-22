# coding: utf-8
"""Keep authentication material out of optimizer prompts, logs and examples."""

import json
import re
from collections.abc import Mapping


REDACTED = "[REDACTED]"
_CREDENTIAL_NAME = re.compile(
    r"(^|[_\-.])(api[_\-.]?key|access[_\-.]?key|secret|password|passwd|"
    r"token|credential|authorization|private[_\-.]?key|client[_\-.]?secret|"
    r"session[_\-.]?id|cookie|auth|key)($|[_\-.])",
    re.IGNORECASE,
)
_AUTH_TEXT = re.compile(
    r"(?i)(\b(?:authorization|api[_-]?key|access[_-]?token|password|"
    r"client[_-]?secret|private[_-]?key|session[_-]?id|"
    r"cookie|passwd|secret|token|auth|key)\b\s*[:=]\s*"
    r"(?:(?:bearer|basic)\s+)?)([^\s,;]+)"
)


def is_credential_name(name):
    normalized = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(name))
    return bool(_CREDENTIAL_NAME.search(normalized))


def credential_values(*objects):
    """Collect known secrets so copies in free text can also be removed."""
    values = set()

    def visit(obj):
        if isinstance(obj, Mapping):
            for key, value in obj.items():
                if (is_credential_name(key) and isinstance(value, (str, int, float))
                        and not isinstance(value, bool)):
                    if str(value):
                        values.add(str(value))
                else:
                    visit(value)
        elif isinstance(obj, (list, tuple)):
            for value in obj:
                visit(value)

    for obj in objects:
        visit(obj)
    return values


def redact(value, secrets=()):
    """Return a new value; never mutate arguments needed for execution."""
    if isinstance(value, Mapping):
        return {
            key: REDACTED if is_credential_name(key) else redact(item, secrets)
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
