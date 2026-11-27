from __future__ import annotations

import re
from typing import TypeAlias

from jiuwenclaw.agentserver.skilldev.error_codes import (
    ERR_FW_SCRIPT_CREDENTIAL,
    ERR_FW_SCRIPT_DESTRUCTIVE,
    ERR_FW_SCRIPT_DYNAMIC_EXEC,
    ERR_FW_SCRIPT_REMOTE_EXEC,
    ERR_FW_SCRIPT_RISKY_PERMISSION,
)

# NOTE:
# This module intentionally duplicates the static security rules from:
# jiuwenclaw/agentserver/skilldev_agent/skills/skill-verifier/scripts/validate.py
# so that SkillDevService can validate scripts writes without importing verifier code.

CredentialPattern: TypeAlias = tuple[re.Pattern[str], str, str | int | None]

# Each entry: (pattern, label, category)
# category is one of: "destructive" | "risky_permission" | "remote_exec" | "dynamic_exec"
DANGEROUS_PATTERNS: list[tuple[re.Pattern[str], str, str]] = [
    # destructive deletion
    (re.compile(r"(?i)\brm\b[^\n\r]*\s-[a-z]*r[a-z]*f[a-z]*\b"), "forced recursive deletion", "destructive"),
    # risky permissions
    (re.compile(r"(?i)\bchmod\b[^\n\r]*\b777\b"), "world-writable permissions", "risky_permission"),
    (re.compile(r"(?i)\bchmod\b[^\n\r]*\bu\+s\b"), "setuid bit modification", "risky_permission"),
    # download & execute (bash/sh/iex/etc)
    (
        re.compile(r"(?i)\b(curl|wget|fetch)\b[^\n\r|]*\|\s*\b(bash|sh|zsh|dash|ash|source)\b"),
        "piped remote shell execution",
        "remote_exec",
    ),
    (
        re.compile(
            r"(?i)\b(iwr|irm|Invoke-WebRequest|Invoke-RestMethod)\b[^\n\r|]*\|\s*\b(iex|Invoke-Expression)\b"
        ),
        "piped remote powershell execution",
        "remote_exec",
    ),
    # obfuscated/dynamic execution
    (re.compile(r"(?i)\bbase64\s+(-d|--decode)\b[^\n\r|]*\|\s*\b(bash|sh|zsh|dash|ash)\b"), "base64 decode then execute", "remote_exec"),
    (re.compile(r"(?i)\bcertutil\s+-decode\b"), "certutil decode (potentially obfuscated payload)", "remote_exec"),
    (re.compile(r"(?i)\b-EncodedCommand\b|\b-[Ee]nc\b"), "powershell encoded command", "remote_exec"),
    (re.compile(r"\[Convert\]::FromBase64String\("), "powershell base64 decode", "remote_exec"),
    # eval/exec-like
    (re.compile(r"(?i)\beval\s*\("), "dynamic eval execution", "dynamic_exec"),
    (re.compile(r"(?i)\bexec\s*\("), "dynamic exec execution", "dynamic_exec"),
    (re.compile(r"(?i)\bos\.system\s*\("), "os.system execution", "dynamic_exec"),
    (re.compile(r"(?i)\bsubprocess\.(?:call|run|Popen)\b[^\n\r]*\bshell\s*=\s*True\b"), "subprocess shell=True execution", "dynamic_exec"),
]

_CATEGORY_TO_ERROR_CODE: dict[str, str] = {
    "destructive": ERR_FW_SCRIPT_DESTRUCTIVE,
    "risky_permission": ERR_FW_SCRIPT_RISKY_PERMISSION,
    "remote_exec": ERR_FW_SCRIPT_REMOTE_EXEC,
    "dynamic_exec": ERR_FW_SCRIPT_DYNAMIC_EXEC,
}

CREDENTIAL_PATTERNS: list[CredentialPattern] = [
    # (pattern, label, capture-group or named-group for placeholder filtering)
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "aws_access_key_id", None),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"), "openai_api_key", None),
    (re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b"), "anthropic_api_key", None),
    (re.compile(r"\bgh[pousu]_[A-Za-z0-9]{36}\b"), "github_token", None),
    (re.compile(r"-----BEGIN (?:RSA |EC |DSA )?PRIVATE KEY-----"), "private_key", None),
    (re.compile(r"(?i)\b(postgresql|mongodb|mysql|redis)://[^:\s]+:[^@\s]+@"), "db_url_with_credentials", None),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"), "jwt_token", None),
    # password is often short and may contain special characters, so check it explicitly
    (
        re.compile(r"(?i)\bpassword\b\s*[:=]\s*(?P<val>(?:['\"][^'\"\n]{6,}['\"]|[^\s#]{6,}))"),
        "password_assignment",
        "val",
    ),
    (
        re.compile(
            r"(?i)\b(api[_-]?key|apikey|api_secret|secret|token|password|credential)\b\s*[:=]\s*(?P<val>(?:['\"][^'\"\n]{12,}['\"]|[A-Za-z0-9_\-\.]{12,}))"
        ),
        "generic_secret_assignment",
        "val",
    ),
]

_PLACEHOLDER_SUBSTRINGS = (
    "your_",
    "example",
    "sample",
    "placeholder",
    "enter_",
    "insert_",
    "replace_",
    "env_",
)


def _is_placeholder(value: str | None) -> bool:
    """Heuristic allowlist to reduce false positives for template values."""
    if value is None:
        return False
    v = value.strip()
    if not v:
        return True

    # Common template syntaxes
    if v.startswith("${") and v.endswith("}"):
        return True
    if v.startswith("$") and re.match(r"^\$[A-Za-z_][A-Za-z0-9_]*$", v):
        return True
    if v.startswith("<") and v.endswith(">"):
        return True

    # Strip quotes for checks
    if (v.startswith("'") and v.endswith("'")) or (v.startswith('"') and v.endswith('"')):
        v = v[1:-1].strip()
        if not v:
            return True

    lower = v.lower()
    if any(s in lower for s in _PLACEHOLDER_SUBSTRINGS):
        return True
    if lower.startswith("your"):
        return True
    if set(lower) <= {"x"} and len(lower) >= 6:
        return True
    if set(lower) <= {"*"} and len(lower) >= 6:
        return True
    if re.fullmatch(r"sk-[xX]{6,}", v):
        return True
    return False


def validate_scripts_file_content(text: str, *, rel_path: str) -> str | None:
    """Validate a scripts/** text file content. Return error code or None."""

    for _line_no, line in enumerate(text.splitlines(), start=1):
        for pattern, _label, category in DANGEROUS_PATTERNS:
            if pattern.search(line):
                return _CATEGORY_TO_ERROR_CODE[category]

    for _line_no, line in enumerate(text.splitlines(), start=1):
        for pattern, _label, value_group in CREDENTIAL_PATTERNS:
            m = pattern.search(line)
            if not m:
                continue

            if isinstance(value_group, str):
                raw_val = m.groupdict().get(value_group)
            elif isinstance(value_group, int):
                raw_val = m.group(value_group)
            else:
                raw_val = m.group(0)

            if raw_val is not None and _is_placeholder(str(raw_val)):
                continue

            return ERR_FW_SCRIPT_CREDENTIAL

    return None
