# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shared redaction helpers for AutoReviewer evidence."""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Mapping, Sequence
from math import isfinite
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

DEFAULT_MAX_REDACTED_TEXT_LENGTH = 240
DEFAULT_MAX_REDACTED_ITEMS = 5
REVIEWABLE_PAYLOAD_TEXT_LIMIT = 1024
PERMISSION_UI_PAYLOAD_MAX_DEPTH = 6
PERMISSION_UI_PAYLOAD_MAX_ITEMS = 32
PERMISSION_UI_PAYLOAD_MAX_STRING_LENGTH = 1024
PERMISSION_UI_PAYLOAD_MAX_TOTAL_BYTES = 16 * 1024

PERMISSION_UI_REDACTED = "[REDACTED]"
PERMISSION_UI_TRUNCATED = "[TRUNCATED]"
PERMISSION_UI_UNAVAILABLE = "[UNAVAILABLE]"

_REVIEW_LOCATION_LIMIT = 240
_SENSITIVE_LOCATION_PARTS = frozenset({
    ".aws", ".gnupg", ".kube", ".ssh", "authorized_keys", "credentials",
    "id_dsa", "id_ed25519", "id_ecdsa", "id_rsa", "passwd", "shadow",
})
_SENSITIVE_LOCATION_TOKENS = (
    "credential", "password", "private_key", "secret", "token",
)

_PATH_LIKE_PATTERN = re.compile(
    r"(?:"
    r"(?:/Users/|/private/|/tmp/)[^\s\"']+"
    r"|/home/[^/\s\"']+/[^\s\"']+"
    r"|~(?:[/\\][^\s\"']+)+"
    r"|[A-Za-z]:[\\/](?:Users|Documents and Settings)[\\/]"
    r"(?:(?!\s+[A-Za-z]:[\\/])[^'\";|&<>`,，。；、\r\n])+"
    r")"
)
_FILE_URI_PATTERN = re.compile(r"file://[^\s\"'<>]*", re.IGNORECASE)
_ABSOLUTE_URI_PREFIX_PATTERN = re.compile(
    r"^[A-Za-z][A-Za-z0-9+.-]*://",
)
_REVIEWABLE_POSIX_ABSOLUTE_PATH_PATTERN = re.compile(
    r"(?<![\w:/])/(?!/)(?:"
    r"(?:Applications|Library|System|Users|Volumes|bin|dev|etc|home|media|mnt|opt|"
    r"private|root|run|sbin|srv|tmp|usr|var|workspace)"
    r"(?:/[^'\";|&<>`,，。；、\r\n\s)\]}]+)+"
    r"|[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)+\.[A-Za-z0-9]{1,16}"
    r")"
)
_REVIEWABLE_WINDOWS_ABSOLUTE_PATH_PATTERN = re.compile(
    r"\b[A-Za-z]:[\\/][^'\";|&<>`,，。；、\r\n\s]+"
)
_REVIEWABLE_WINDOWS_UNC_PATH_PATTERN = re.compile(
    r"\\\\[^\\/\s'\";|&<>`,，。；、\r\n]+[\\/][^'\";|&<>`,，。；、\r\n]+"
)
_REVIEWABLE_QUOTED_LOCAL_PATH_PATTERN = re.compile(
    r"(?P<quote>[\"'])(?:"
    r"/Users/|/home/|/private/|/tmp/|/workspace/|~[/\\]|"
    r"[A-Za-z]:[\\/](?:Users|Documents and Settings)[\\/]"
    r")[^\"']+(?P=quote)"
)
_REVIEWABLE_ESCAPED_LOCAL_PATH_PATTERN = re.compile(
    r"(?:/Users/|/home/|/private/|/tmp/|/workspace/|~[/\\])"
    r"(?:\\\s|[^\s\"';|&<>`,，。；、\r\n])+"
)
_REVIEWABLE_RELATIVE_FILE_PATH_PATTERN = re.compile(
    r"(?<![\w:/])(?:\.{1,2}/)?[A-Za-z0-9._-]+"
    r"(?:/[A-Za-z0-9._-]+)+\.[A-Za-z0-9]{1,16}(?![\w./-])"
)
_SECRET_KEY_PATTERN = re.compile(
    r"(?i)^(api[_-]?key|authorization|credential|credentials|password|"
    r"private[_-]?key|secret|token|access[_-]?token|refresh[_-]?token|"
    r"session[_-]?token|client[_-]?secret|aws[_-]?access[_-]?key[_-]?id|"
    r"aws[_-]?secret[_-]?access[_-]?key)$"
)
_SECRET_URL_QUERY_KEY_PATTERN = re.compile(
    r"(?i)^(api[_-]?key|apikey|auth|authorization|code|credential|credentials|"
    r"key|oauth[_-]?token|password|private[_-]?key|refresh[_-]?token|secret|"
    r"session|session[_-]?id|session[_-]?token|sig|signature|token|"
    r"access[_-]?token|client[_-]?secret|aws[_-]?access[_-]?key[_-]?id|"
    r"aws[_-]?secret[_-]?access[_-]?key)$"
)
_SECRET_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)([\"']?(?:api[_-]?key|authorization|credential|credentials|password|"
    r"private[_-]?key|secret|token|access[_-]?token|refresh[_-]?token|"
    r"session[_-]?token|client[_-]?secret|aws[_-]?access[_-]?key[_-]?id|"
    r"aws[_-]?secret[_-]?access[_-]?key)[\"']?\s*[:=]\s*)"
    r"(?:\"[^\"]*\"|'[^']*'|[^\"'\s,;}]+)"
)
_AUTH_VALUE_PATTERN = re.compile(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{8,}")
_COMMON_SECRET_VALUE_PATTERNS = (
    re.compile(r"(?<![A-Za-z0-9_])sk-[A-Za-z0-9_-]{8,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9_]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"(?:AKIA|ASIA)[0-9A-Z]{16}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
        re.DOTALL,
    ),
)
_WHITESPACE_PATTERN = re.compile(r"\s+")
_CAMEL_CASE_BOUNDARY_PATTERN = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_NON_ALNUM_KEY_PATTERN = re.compile(r"[^a-z0-9]+")
_PERMISSION_UI_SECRET_KEY_TOKENS = frozenset(
    {
        "auth",
        "authorization",
        "cookie",
        "credential",
        "credentials",
        "password",
        "passwd",
        "pwd",
        "secret",
        "token",
    }
)
_PERMISSION_UI_SECRET_KEY_COMPACT = frozenset(
    {
        "accesskey",
        "accesskeyid",
        "apikey",
        "authheader",
        "authorization",
        "authorizationheader",
        "awsaccesskeyid",
        "awssecretaccesskey",
        "clientsecret",
        "cookie",
        "privatekey",
        "proxyauth",
        "proxyauthorization",
        "setcookie",
        "signingkey",
    }
)
_PERMISSION_UI_SECRET_KEY_SUFFIXES = (
    "accesskey",
    "accesskeyid",
    "apikey",
    "authheader",
    "authorization",
    "authorizationheader",
    "clientsecret",
    "credential",
    "credentials",
    "privatekey",
    "proxyauth",
    "proxyauthorization",
    "password",
    "secret",
    "secretkey",
    "signingkey",
    "token",
)


def redact_text(
    value: Any,
    *,
    max_length: int = DEFAULT_MAX_REDACTED_TEXT_LENGTH,
) -> str:
    """Return a bounded text representation with common secrets redacted."""
    raw_text = _stringify(value)
    text = raw_text.replace("\r", " ").replace("\n", " ")
    text = _FILE_URI_PATTERN.sub("file://[redacted-path]", text)
    text = _PATH_LIKE_PATTERN.sub("[path]", text)
    return redact_secret_values(text, max_length=max_length)


def reviewer_path_location(
    raw_path: str, *, workspace_root: str, platform_trusted_root: str,
) -> tuple[dict[str, str] | None, str]:
    """Project Smart evidence only; never change the underlying access identity."""
    try:
        if _sensitive_location(raw_path):
            return None, "redacted"
        path = Path(raw_path).expanduser()
        if not path.is_absolute() or raw_path.startswith(("//", "\\\\")):
            return None, "unavailable"
        target = path.resolve(strict=False)
        for base, raw_root in (
            ("workspace", workspace_root),
            ("platform_trusted_root", platform_trusted_root),
        ):
            if not raw_root:
                continue
            root = Path(raw_root).expanduser()
            if not root.is_absolute():
                continue
            try:
                relative = target.relative_to(root.resolve(strict=False)).as_posix()
            except ValueError:
                continue
            # Check the full input too: resolving '..' must not reveal a secret
            # that was carried in a discarded component of the evidence.
            if _sensitive_location(relative):
                return None, "redacted"
            if len(relative) > _REVIEW_LOCATION_LIMIT:
                return None, "omitted"
            return {"base": base, "relative_path": relative}, "complete"
    except (OSError, RuntimeError, TypeError, ValueError):
        pass
    return None, "unavailable"


def _sensitive_location(value: str) -> bool:
    if any(unicodedata.category(char).startswith("C") for char in value):
        return True
    for part in value.replace("\\", "/").split("/"):
        name = part.casefold()
        sensitive_name = name in _SENSITIVE_LOCATION_PARTS or name == ".env" or name.startswith(".env.")
        if (
            sensitive_name
            or name.endswith((".key", ".pem", ".p12", ".pfx"))
            or any(token in name for token in _SENSITIVE_LOCATION_TOKENS)
        ):
            return True
    return redact_secret_values(value, max_length=len(value) + 1) != value


def redact_reviewer_intent(
    value: Any, *, workspace_root: str, platform_trusted_root: str,
    max_length: int = 1024,
) -> str:
    """Retain literal in-root locations in Smart intent, not path authority."""
    text = _stringify(value)
    # Match only known absolute roots, including quoted paths containing spaces.
    # The existing broad redactor still owns all unknown/URI/private paths.
    roots: list[str] = []
    for raw_root in (workspace_root, platform_trusted_root):
        try:
            root = Path(raw_root).expanduser()
            if raw_root and root.is_absolute():
                roots.append(str(root.resolve(strict=False)).rstrip("/"))
        except (OSError, RuntimeError, TypeError, ValueError):
            continue
    placeholders: dict[str, str] = {}
    for root in sorted(set(roots), key=len, reverse=True):
        if not root:
            continue
        escaped = re.escape(root)
        pattern = re.compile(
            r"(?<![\w:/\\])(?:"
            r"(?P<quote>[\"'])" + escaped + r"(?=/|(?P=quote))"
            r"[^\"'\r\n]*(?P=quote)|"
            + escaped + r"(?=/|$|[\s\"'`,，。；、)\]}])"
            r"(?:/[^\s\"'`;|&<>`,，。；、)\]}]*)?)"
        )

        def replace_path(match: re.Match[str]) -> str:
            literal = match.group(0)
            if match.group("quote"):
                literal = literal[1:-1]
            location, _status = reviewer_path_location(
                literal, workspace_root=workspace_root,
                platform_trusted_root=platform_trusted_root,
            )
            if location is None:
                return "[path]"
            # Keep the broad path redactor from erasing this already vetted view.
            placeholder = f"\x00{len(placeholders)}\x00"
            placeholders[placeholder] = (
                f"[{location['base']}]/{location['relative_path']}"
            )
            return placeholder

        text = pattern.sub(replace_path, text)
    # Literal controls cannot spoof placeholders created inside this function.
    # Rejecting them is preferable to guessing which path a malformed turn meant.
    if "\x00" in _stringify(value):
        return redact_reviewable_payload_text(value, max_length=max_length).replace("\x00", "")
    text = redact_reviewable_payload_text(text, max_length=len(text) + 1)
    # Unknown absolute literals remain opaque even outside the shared redactor's
    # list of common home/system directories. Vetted locations are placeholders.
    text = re.sub(
        r"(?<![\w:/\\])(?:[\"']/[^\"'\r\n]*[\"']|"
        r"/(?!/)[^\s\"'`;|&<>`,，。；、)\]}]+)",
        "[path]", text,
    )
    for placeholder, location in placeholders.items():
        text = text.replace(placeholder, location)
    return redact_secret_values(text, max_length=max_length)


def redact_reviewable_payload_text(
    value: Any,
    *,
    max_length: int = REVIEWABLE_PAYLOAD_TEXT_LIMIT,
    redact_relative_paths: bool = False,
) -> str:
    """Return reviewer-visible payload text with broad path and secret redaction."""
    raw_text = _stringify(value)
    text = raw_text.replace("\r", " ").replace("\n", " ")
    text = _FILE_URI_PATTERN.sub("file://[redacted-path]", text)
    text = _REVIEWABLE_QUOTED_LOCAL_PATH_PATTERN.sub("[path]", text)
    text = _REVIEWABLE_ESCAPED_LOCAL_PATH_PATTERN.sub("[path]", text)
    text = _REVIEWABLE_POSIX_ABSOLUTE_PATH_PATTERN.sub("[path]", text)
    text = _REVIEWABLE_WINDOWS_ABSOLUTE_PATH_PATTERN.sub("[path]", text)
    text = _REVIEWABLE_WINDOWS_UNC_PATH_PATTERN.sub("[path]", text)
    if redact_relative_paths:
        text = _REVIEWABLE_RELATIVE_FILE_PATH_PATTERN.sub("[path]", text)
    return redact_text(text, max_length=max_length)


def redact_secret_values(
    value: Any,
    *,
    max_length: int = DEFAULT_MAX_REDACTED_TEXT_LENGTH,
) -> str:
    """Return bounded text with secret values redacted while preserving paths."""
    text = _stringify(value).replace("\r", " ").replace("\n", " ")
    text = _AUTH_VALUE_PATTERN.sub(lambda match: f"{match.group(1)} [redacted]", text)
    for pattern in _COMMON_SECRET_VALUE_PATTERNS:
        text = pattern.sub("[redacted]", text)
    text = _SECRET_ASSIGNMENT_PATTERN.sub(r"\1[redacted]", text)
    normalized = _WHITESPACE_PATTERN.sub(" ", text).strip()
    if len(normalized) <= max_length:
        return normalized
    if max_length <= 3:
        return normalized[:max_length]
    return f"{normalized[: max_length - 3]}..."


def redact_secret_values_preserve_format(
    value: Any,
    *,
    max_length: int = DEFAULT_MAX_REDACTED_TEXT_LENGTH,
) -> str:
    """Return bounded text with secret values redacted while preserving whitespace."""
    text = _stringify(value).replace("\r\n", "\n").replace("\r", "\n")
    text = _AUTH_VALUE_PATTERN.sub(lambda match: f"{match.group(1)} [redacted]", text)
    for pattern in _COMMON_SECRET_VALUE_PATTERNS:
        text = pattern.sub("[redacted]", text)
    text = _SECRET_ASSIGNMENT_PATTERN.sub(r"\1[redacted]", text)
    if len(text) <= max_length:
        return text
    if max_length <= 3:
        return text[:max_length]
    return f"{text[: max_length - 3]}..."


def redact_json_value(
    value: Any,
    *,
    max_length: int = DEFAULT_MAX_REDACTED_TEXT_LENGTH,
    max_items: int = DEFAULT_MAX_REDACTED_ITEMS,
) -> Any:
    """Return a JSON-safe redacted value while preserving simple structure."""
    if isinstance(value, Mapping):
        return {
            str(key): _redacted_mapping_value(
                key,
                nested_value,
                max_length=max_length,
                max_items=max_items,
            )
            for key, nested_value in value.items()
        }
    if isinstance(value, str):
        if _is_absolute_url(value):
            return redact_url(value, max_length=max_length)
        return redact_text(value, max_length=max_length)
    if isinstance(value, bool | int | float) or value is None:
        return value
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [
            redact_json_value(
                item,
                max_length=max_length,
                max_items=max_items,
            )
            for item in value[:max_items]
        ]
    return redact_text(value, max_length=max_length)


def redact_url(
    value: Any,
    *,
    max_length: int = DEFAULT_MAX_REDACTED_TEXT_LENGTH,
) -> str:
    """Return a bounded URL with credentials and sensitive query values redacted."""
    raw_url = _stringify(value).strip()
    try:
        parsed = urlparse(raw_url)
    except ValueError:
        return redact_text(raw_url, max_length=max_length)
    if parsed.scheme.lower() == "file" and _ABSOLUTE_URI_PREFIX_PATTERN.match(raw_url):
        return "file://[redacted-path]"
    if not parsed.scheme or not parsed.netloc:
        return redact_text(raw_url, max_length=max_length)
    safe_query = []
    for key, nested_value in parse_qsl(parsed.query, keep_blank_values=True):
        if _SECRET_URL_QUERY_KEY_PATTERN.match(key.strip()):
            safe_query.append((key, "[redacted]"))
        else:
            safe_query.append((key, redact_text(nested_value, max_length=max_length)))
    safe_query_text = urlencode(safe_query, doseq=True).replace(
        "%5Bredacted%5D",
        "[redacted]",
    )
    redacted = urlunparse(
        (
            parsed.scheme,
            _safe_url_netloc(parsed),
            redact_text(parsed.path, max_length=max_length),
            "",
            safe_query_text,
            _redact_url_fragment(parsed.fragment, max_length=max_length),
        )
    )
    return _truncate_text(redacted, max_length=max_length)


def sanitize_permission_ui_payload(
    value: Any,
    *,
    max_depth: int = PERMISSION_UI_PAYLOAD_MAX_DEPTH,
    max_items: int = PERMISSION_UI_PAYLOAD_MAX_ITEMS,
    max_string_length: int = PERMISSION_UI_PAYLOAD_MAX_STRING_LENGTH,
    max_total_bytes: int = PERMISSION_UI_PAYLOAD_MAX_TOTAL_BYTES,
) -> Any:
    """Return a bounded, redacted copy suitable only for permission UI display."""
    if min(max_depth, max_items, max_string_length) < 1 or max_total_bytes < 64:
        raise ValueError("permission UI payload limits are too small")
    try:
        sanitized = _sanitize_permission_ui_value(
            value,
            depth=0,
            max_depth=max_depth,
            max_items=max_items,
            max_string_length=max_string_length,
            active_ids=set(),
        )
        return _bound_permission_ui_payload(
            sanitized,
            max_total_bytes=max_total_bytes,
        )
    except Exception:  # noqa: BLE001 - display boundary must fail closed
        # This is a display-only safety boundary. Never fall back to raw values.
        return PERMISSION_UI_UNAVAILABLE


def _sanitize_permission_ui_value(
    value: Any,
    *,
    depth: int,
    max_depth: int,
    max_items: int,
    max_string_length: int,
    active_ids: set[int],
) -> Any:
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, float):
        if isfinite(value):
            return value
        return PERMISSION_UI_UNAVAILABLE
    if isinstance(value, str):
        return _permission_ui_text(value, max_length=max_string_length)
    if isinstance(value, Mapping):
        if depth >= max_depth:
            return PERMISSION_UI_TRUNCATED
        value_id = id(value)
        if value_id in active_ids:
            return PERMISSION_UI_UNAVAILABLE
        active_ids.add(value_id)
        try:
            result: dict[str, Any] = {}
            for index, (raw_key, nested_value) in enumerate(value.items()):
                if index >= max_items:
                    result[_unique_payload_key(result, "[TRUNCATED]")] = (
                        PERMISSION_UI_TRUNCATED
                    )
                    break
                safe_key = _safe_permission_ui_key(raw_key)
                if safe_key is None:
                    result[_unique_payload_key(result, "[UNAVAILABLE]")] = (
                        PERMISSION_UI_UNAVAILABLE
                    )
                    continue
                normalized_key, compact_key = _normalized_permission_ui_key(safe_key)
                if normalized_key == "secret_context" or compact_key == "secretcontext":
                    continue
                if _is_permission_ui_secret_key(normalized_key, compact_key):
                    result[_unique_payload_key(result, safe_key)] = (
                        PERMISSION_UI_REDACTED
                    )
                    continue
                result[_unique_payload_key(result, safe_key)] = (
                    _sanitize_permission_ui_value(
                        nested_value,
                        depth=depth + 1,
                        max_depth=max_depth,
                        max_items=max_items,
                        max_string_length=max_string_length,
                        active_ids=active_ids,
                    )
                )
            return result
        finally:
            active_ids.remove(value_id)
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        if depth >= max_depth:
            return PERMISSION_UI_TRUNCATED
        value_id = id(value)
        if value_id in active_ids:
            return PERMISSION_UI_UNAVAILABLE
        active_ids.add(value_id)
        try:
            sequence_result = []
            for index, item in enumerate(value):
                if index >= max_items:
                    sequence_result.append(PERMISSION_UI_TRUNCATED)
                    break
                sequence_result.append(
                    _sanitize_permission_ui_value(
                        item,
                        depth=depth + 1,
                        max_depth=max_depth,
                        max_items=max_items,
                        max_string_length=max_string_length,
                        active_ids=active_ids,
                    )
                )
            return sequence_result
        finally:
            active_ids.remove(value_id)
    return PERMISSION_UI_UNAVAILABLE


def _normalized_permission_ui_key(value: str) -> tuple[str, str]:
    separated = _CAMEL_CASE_BOUNDARY_PATTERN.sub("_", value)
    normalized = _NON_ALNUM_KEY_PATTERN.sub("_", separated.lower()).strip("_")
    return normalized, normalized.replace("_", "")


def _is_permission_ui_secret_key(normalized: str, compact: str) -> bool:
    tokens = frozenset(part for part in normalized.split("_") if part)
    return bool(
        tokens & _PERMISSION_UI_SECRET_KEY_TOKENS
        or compact in _PERMISSION_UI_SECRET_KEY_COMPACT
        or compact.endswith(_PERMISSION_UI_SECRET_KEY_SUFFIXES)
    )


def _safe_permission_ui_key(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    return _permission_ui_text(value, max_length=128)


def _unique_payload_key(payload: Mapping[str, Any], preferred: str) -> str:
    candidate = preferred
    suffix = 2
    while candidate in payload:
        candidate = f"{preferred} ({suffix})"
        suffix += 1
    return candidate


def _permission_ui_text(value: str, *, max_length: int) -> str:
    if _is_absolute_url(value):
        redacted = redact_url(value, max_length=max(len(value) + 256, max_length))
    else:
        redacted = redact_secret_values_preserve_format(
            value,
            max_length=max(len(value) + 1, max_length),
        )
    redacted = redacted.replace("[redacted]", PERMISSION_UI_REDACTED)
    marker = f" {PERMISSION_UI_TRUNCATED}"
    if len(redacted) <= max_length:
        return redacted
    return f"{redacted[: max_length - len(marker)]}{marker}"


def _bound_permission_ui_payload(value: Any, *, max_total_bytes: int) -> Any:
    serialized = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )
    if len(serialized.encode("utf-8")) <= max_total_bytes:
        return value
    low = 0
    high = len(serialized)
    best: dict[str, str] = {"preview": PERMISSION_UI_TRUNCATED}
    while low <= high:
        midpoint = (low + high) // 2
        candidate = {"preview": f"{serialized[:midpoint]} {PERMISSION_UI_TRUNCATED}"}
        size = len(
            json.dumps(candidate, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
        )
        if size <= max_total_bytes:
            best = candidate
            low = midpoint + 1
        else:
            high = midpoint - 1
    return best


def _redacted_mapping_value(
    key: object,
    value: Any,
    *,
    max_length: int,
    max_items: int,
) -> Any:
    if _SECRET_KEY_PATTERN.match(str(key or "").strip()):
        return "[redacted]"
    return redact_json_value(value, max_length=max_length, max_items=max_items)


def _is_absolute_url(value: str) -> bool:
    return bool(_ABSOLUTE_URI_PREFIX_PATTERN.match(value.strip()))


def _safe_url_netloc(parsed: Any) -> str:
    try:
        hostname = str(parsed.hostname or "")
    except ValueError:
        hostname = ""
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    try:
        port = parsed.port
    except ValueError:
        port = None
    if port is None:
        return hostname
    return f"{hostname}:{port}"


def _redact_url_fragment(fragment: str, *, max_length: int) -> str:
    if not fragment:
        return ""
    if "=" not in fragment:
        return redact_text(fragment, max_length=max_length)
    safe_fragment = []
    for key, nested_value in parse_qsl(fragment, keep_blank_values=True):
        if _SECRET_URL_QUERY_KEY_PATTERN.match(key.strip()):
            safe_fragment.append((key, "[redacted]"))
        else:
            safe_fragment.append(
                (key, redact_text(nested_value, max_length=max_length))
            )
    if not safe_fragment:
        return redact_text(fragment, max_length=max_length)
    return urlencode(safe_fragment, doseq=True).replace(
        "%5Bredacted%5D",
        "[redacted]",
    )


def _truncate_text(value: str, *, max_length: int) -> str:
    if len(value) <= max_length:
        return value
    if max_length <= 3:
        return value[:max_length]
    return f"{value[: max_length - 3]}..."


def _stringify(value: Any) -> str:
    if isinstance(value, Mapping):
        try:
            return json.dumps(value, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            return str(value)
    return str(value or "")


__all__ = [
    "redact_json_value",
    "redact_reviewable_payload_text",
    "redact_secret_values",
    "redact_text",
    "redact_url",
    "sanitize_permission_ui_payload",
]
