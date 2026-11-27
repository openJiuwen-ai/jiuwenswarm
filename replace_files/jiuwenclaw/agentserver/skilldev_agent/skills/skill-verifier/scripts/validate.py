#!/usr/bin/env python3
"""Unified skill validator — single source of truth for spec + security checks.

Replaces skill-creator/quick_validate.py, skill-standardizer/validate.py,
and the inline validation in direct_import.py.

Enforces both character limits and token limits (dual-limit policy).
"""

from __future__ import annotations

import logging
import math
import re
import sys
from pathlib import Path
from typing import TypeAlias

import yaml

logger = logging.getLogger(__name__)

CredentialPattern: TypeAlias = tuple[re.Pattern[str], str, str | int | None]

DANGEROUS_PATTERNS = [
    # destructive deletion
    (re.compile(r"(?i)\brm\b[^\n\r]*\s-[a-z]*r[a-z]*f[a-z]*\b"), "forced recursive deletion"),
    # risky permissions
    (re.compile(r"(?i)\bchmod\b[^\n\r]*\b777\b"), "world-writable permissions"),
    (re.compile(r"(?i)\bchmod\b[^\n\r]*\bu\+s\b"), "setuid bit modification"),
    # download & execute (bash/sh/iex/etc)
    (
        re.compile(r"(?i)\b(curl|wget|fetch)\b[^\n\r|]*\|\s*\b(bash|sh|zsh|dash|ash|source)\b"),
        "piped remote shell execution",
    ),
    (
        re.compile(
            r"(?i)\b(iwr|irm|Invoke-WebRequest|Invoke-RestMethod)\b[^\n\r|]*\|\s*\b(iex|Invoke-Expression)\b"
        ),
        "piped remote powershell execution",
    ),
    # obfuscated/dynamic execution
    (re.compile(r"(?i)\bbase64\s+(-d|--decode)\b[^\n\r|]*\|\s*\b(bash|sh|zsh|dash|ash)\b"), "base64 decode then execute"),
    (re.compile(r"(?i)\bcertutil\s+-decode\b"), "certutil decode (potentially obfuscated payload)"),
    (re.compile(r"(?i)\b-EncodedCommand\b|\b-[Ee]nc\b"), "powershell encoded command"),
    (re.compile(r"\[Convert\]::FromBase64String\("), "powershell base64 decode"),
    # eval/exec-like
    (re.compile(r"(?i)\beval\s*\("), "dynamic eval execution"),
    (re.compile(r"(?i)\bexec\s*\("), "dynamic exec execution"),
    (re.compile(r"(?i)\bos\.system\s*\("), "os.system execution"),
    (re.compile(r"(?i)\bsubprocess\.(?:call|run|Popen)\b[^\n\r]*\bshell\s*=\s*True\b"), "subprocess shell=True execution"),
]
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
        re.compile(
            r"(?i)\bpassword\b\s*[:=：]\s*(?P<val>(?:['\"][^'\"\n]{6,}['\"]|[^\s#]{6,}))"
        ),
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

ALLOWED_FRONTMATTER_KEYS = {
    "name",
    "description",
    "license",
    "allowed-tools",
    "metadata",
    "compatibility",
}

DESCRIPTION_MAX_CHARS_CJK = 512
DESCRIPTION_MAX_CHARS_EN = 1024
DESCRIPTION_MAX_WEIGHTED = 1024
DESCRIPTION_MAX_TOKENS = 300
BODY_MAX_LINES = 500
BODY_MAX_TOKENS = 5000
FRONTMATTER_RE = re.compile(r"^---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|$)", re.DOTALL)


def _estimate_tokens(text: str) -> int:
    """按字符估算 token：CJK 0.6 / 其余 0.3，结果向上取整。"""
    if not text:
        return 0
    total = sum(
        0.6 if _is_cjk_char(ch) else 0.3
        for ch in text
    )
    return int(math.ceil(total))


def _is_cjk_char(char: str) -> bool:
    return "\u4e00" <= char <= "\u9fff"


def _description_weighted_len(text: str) -> int:
    """description 加权长度：CJK 计 2，其余计 1。

    等价于：纯中文 ≤512、纯英文 ≤1024、中英混杂加权总和 ≤1024。
    """
    return sum(2 if _is_cjk_char(ch) else 1 for ch in text)


def _read_utf8_text(path: Path) -> tuple[str | None, bool]:
    """Read UTF-8/UTF-8-BOM text, returning (text, is_binary)."""
    data = path.read_bytes()
    if b"\0" in data:
        return None, True
    try:
        return data.decode("utf-8-sig"), False
    except UnicodeDecodeError:
        return None, False


def _find_duplicate_frontmatter_key(frontmatter_text: str) -> str | None:
    seen: set[str] = set()
    for line in frontmatter_text.splitlines():
        m = re.match(r"^([A-Za-z0-9_-]+)\s*:", line)
        if not m:
            continue
        key = m.group(1)
        if key in seen:
            return key
        seen.add(key)
    return None


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


def _iter_scannable_files(skill_path: Path, skill_content: str) -> tuple[list[tuple[Path, str]], list[str]]:
    """Return UTF-8/UTF-8-BOM text files to scan plus decode errors."""
    files: list[tuple[Path, str]] = [(skill_path / "SKILL.md", skill_content)]
    errors: list[str] = []
    scripts_dir = skill_path / "scripts"
    if not scripts_dir.exists():
        return files, errors

    for sp in scripts_dir.rglob("*"):
        if not sp.is_file():
            continue
        text, is_binary = _read_utf8_text(sp)
        if is_binary:
            continue
        if text is None:
            errors.append(
                f"Non-UTF-8 text file detected: {sp.relative_to(skill_path)}. "
                "Use UTF-8 or UTF-8 with BOM."
            )
            continue
        files.append((sp, text))
    return files, errors


def validate_skill(skill_path: str | Path) -> tuple[bool, str]:
    """Validate a skill directory against all spec + security rules.

    Collects ALL errors before returning so the caller can fix them in one pass.
    Returns (True, "Skill is valid!") or (False, "<bullet list of all errors>").
    """
    skill_path = Path(skill_path).resolve()
    errors: list[str] = []

    skill_md = skill_path / "SKILL.md"
    if not skill_md.exists():
        return False, "SKILL.md not found"

    content, is_binary = _read_utf8_text(skill_md)
    if is_binary or content is None:
        return False, "SKILL.md must be UTF-8 encoded"
    if not content.startswith("---"):
        return False, "No YAML frontmatter found"

    match = FRONTMATTER_RE.match(content)
    if not match:
        return False, "Invalid frontmatter format"

    frontmatter_text = match.group(1)
    body = content[match.end():].lstrip("\r\n")

    dup = _find_duplicate_frontmatter_key(frontmatter_text)
    if dup:
        errors.append(f"Duplicate key in SKILL.md frontmatter: {dup}")

    try:
        frontmatter = yaml.safe_load(frontmatter_text)
        if not isinstance(frontmatter, dict):
            return False, "Frontmatter must be a YAML dictionary"
    except yaml.YAMLError as e:
        return False, f"Invalid YAML in frontmatter: {e}"

    # unexpected = set(frontmatter.keys()) - ALLOWED_FRONTMATTER_KEYS
    # if unexpected:
    #     errors.append(
    #         f"Unexpected key(s) in SKILL.md frontmatter: {', '.join(sorted(unexpected))}. "
    #         f"Allowed properties are: {', '.join(sorted(ALLOWED_FRONTMATTER_KEYS))}"
    #     )

    if "name" not in frontmatter:
        errors.append("Missing 'name' in frontmatter")
    if "description" not in frontmatter:
        errors.append("Missing 'description' in frontmatter")

    # --- name ---
    name = frontmatter.get("name", "")
    if isinstance(name, str):
        name = name.strip()
        if not name:
            errors.append("Name cannot be empty")
        else:
            if not re.match(r"^[a-z0-9-]+$", name):
                errors.append(
                    f"Name '{name}' should be kebab-case (lowercase letters, digits, and hyphens only)"
                )
            if name.startswith("-") or name.endswith("-") or "--" in name:
                errors.append(
                    f"Name '{name}' cannot start/end with hyphen or contain consecutive hyphens"
                )
            if len(name) > 64:
                errors.append(f"Name is too long ({len(name)} characters). Maximum is 64 characters.")
            if name != skill_path.name:
                errors.append(f"Name '{name}' must match directory name '{skill_path.name}'")
    elif name is not None and "name" in frontmatter:
        errors.append(f"Name must be a string, got {type(name).__name__}")

    # --- description (char + token dual limit) ---
    description = frontmatter.get("description", "")
    if isinstance(description, str):
        description = description.strip()
        if not description:
            errors.append("Description cannot be empty")
        else:
            weighted_len = _description_weighted_len(description)
            if weighted_len > DESCRIPTION_MAX_WEIGHTED:
                errors.append(
                    f"Description is too long (weighted length {weighted_len}). "
                    f"Maximum weighted length is {DESCRIPTION_MAX_WEIGHTED} "
                    f"(CJK counts as 2, others as 1; "
                    f"pure Chinese ≤{DESCRIPTION_MAX_CHARS_CJK}, "
                    f"pure English ≤{DESCRIPTION_MAX_CHARS_EN})."
                )
            desc_tokens = _estimate_tokens(description)
            if desc_tokens > DESCRIPTION_MAX_TOKENS:
                errors.append(
                    f"Description token count too high (~{desc_tokens} tokens). "
                    f"Maximum is {DESCRIPTION_MAX_TOKENS} tokens "
                    f"(CJK 0.6 / others 0.3 token per char)."
                )
    elif description is not None and "description" in frontmatter:
        errors.append(f"Description must be a string, got {type(description).__name__}")

    # --- body (line + token dual limit) ---
    if not body.strip():
        errors.append("SKILL.md body cannot be empty")
    else:
        body_lines = body.splitlines()
        if len(body_lines) > BODY_MAX_LINES:
            errors.append(
                f"SKILL.md body is too long ({len(body_lines)} lines). "
                f"Maximum is {BODY_MAX_LINES} lines."
            )
        body_tokens = _estimate_tokens(body)
        if body_tokens > BODY_MAX_TOKENS:
            errors.append(
                f"SKILL.md body token count too high (~{body_tokens} tokens). "
                f"Maximum is {BODY_MAX_TOKENS} tokens "
                f"(CJK 0.6 / others 0.3 token per char)."
            )

    # --- static security ---
    sec_errors = _validate_static_security_all(skill_path, content)
    errors.extend(sec_errors)

    if errors:
        return False, "\n".join(f"- {e}" for e in errors)
    return True, "Skill is valid!"


def _validate_static_security_all(skill_path: Path, skill_content: str) -> list[str]:
    """Collect all static security errors instead of stopping at the first."""
    errors: list[str] = []
    files, scan_errors = _iter_scannable_files(skill_path, skill_content)
    errors.extend(scan_errors)

    for file_path, _ in files:
        rel = file_path.relative_to(skill_path)
        if ".." in rel.parts:
            errors.append(f"Path traversal detected: {rel}")

    # dangerous commands: scripts/** only (SKILL.md excluded)
    for file_path, text in files:
        rel = file_path.relative_to(skill_path)
        if not rel.parts or rel.parts[0] != "scripts":
            continue
        for line_no, line in enumerate(text.splitlines(), start=1):
            for pattern, label in DANGEROUS_PATTERNS:
                if pattern.search(line):
                    errors.append(
                        f"Security check failed in {rel}:{line_no}: "
                        f"prohibited command pattern `{label}`"
                    )

    # hardcoded credentials: SKILL.md + scripts/**
    for file_path, text in files:
        rel = file_path.relative_to(skill_path)
        for line_no, line in enumerate(text.splitlines(), start=1):
            for pattern, label, value_group in CREDENTIAL_PATTERNS:
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

                errors.append(
                    f"Security check failed in {rel}:{line_no}: "
                    f"possible hardcoded credential (`{label}`)"
                )

    return errors


def find_skill_root(skill_dir: Path) -> Path | None:
    """Locate the skill root directory containing SKILL.md under skill/."""
    if not skill_dir.is_dir():
        return None
    if (skill_dir / "SKILL.md").is_file():
        return skill_dir

    subdirs = [
        child
        for child in skill_dir.iterdir()
        if child.is_dir() and (child / "SKILL.md").is_file()
    ]
    if len(subdirs) == 1:
        return subdirs[0]
    if len(subdirs) > 1:
        names = ", ".join(sorted(d.name for d in subdirs))
        logger.warning("multiple skill roots under %s: %s", skill_dir, names)
        return None

    for skill_md in skill_dir.rglob("SKILL.md"):
        return skill_md.parent
    return None


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("Usage: python3 -m scripts.validate <workspace>")
        return 2

    workspace = Path(argv[1]).resolve()
    skill_root = find_skill_root(workspace / "skill")
    if skill_root is None:
        print("Validation failed: cannot find skill root under <workspace>/skill/")
        return 1

    valid, message = validate_skill(skill_root)
    if not valid:
        print("Validation failed:")
        print(message)
        return 1

    print("Validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
