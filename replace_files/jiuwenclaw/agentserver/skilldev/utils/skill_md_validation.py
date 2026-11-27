from __future__ import annotations

import re
import math

import yaml

# Keep the SKILL.md validation rules consistent with:
# jiuwenclaw/agentserver/skilldev_agent/skills/skill-verifier/scripts/validate.py

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

CredentialPattern = tuple[re.Pattern[str], str, str | int | None]

CREDENTIAL_PATTERNS: list[CredentialPattern] = [
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "aws_access_key_id", None),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"), "openai_api_key", None),
    (re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b"), "anthropic_api_key", None),
    (re.compile(r"\bgh[pousu]_[A-Za-z0-9]{36}\b"), "github_token", None),
    (re.compile(r"-----BEGIN (?:RSA |EC |DSA )?PRIVATE KEY-----"), "private_key", None),
    (re.compile(r"(?i)\b(postgresql|mongodb|mysql|redis)://[^:\s]+:[^@\s]+@"), "db_url_with_credentials", None),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"), "jwt_token", None),
    (
        re.compile(r"(?i)\bpassword\b\s*[:=：]\s*(?P<val>(?:['\"][^'\"\n]{6,}['\"]|[^\s#]{6,}))"),
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


TOKEN_PER_CJK_CHAR = 0.6
TOKEN_PER_OTHER_CHAR = 0.3


def estimate_skill_tokens(text: str) -> int:
    """按字符估算 token：CJK 0.6 / 其余 0.3，结果向上取整。"""
    if not text:
        return 0
    total = sum(
        TOKEN_PER_CJK_CHAR if _is_cjk_char(ch) else TOKEN_PER_OTHER_CHAR
        for ch in text
    )
    return int(math.ceil(total))


def _is_cjk_char(char: str) -> bool:
    return "\u4e00" <= char <= "\u9fff"


def description_weighted_len(text: str) -> int:
    """description 加权长度：CJK 计 2，其余计 1。

    等价于：纯中文 ≤512、纯英文 ≤1024、中英混杂加权总和 ≤1024。
    """
    return sum(2 if _is_cjk_char(ch) else 1 for ch in text)


def truncate_description_to_weighted_limit(
    text: str,
    limit: int = DESCRIPTION_MAX_WEIGHTED,
    token_limit: int = DESCRIPTION_MAX_TOKENS,
) -> str:
    """按加权字符数与 token 双限截断 description。"""
    weighted = 0
    tokens = 0.0
    chars: list[str] = []
    for ch in text:
        is_cjk = _is_cjk_char(ch)
        w = 2 if is_cjk else 1
        t = TOKEN_PER_CJK_CHAR if is_cjk else TOKEN_PER_OTHER_CHAR
        if weighted + w > limit:
            break
        if math.ceil(tokens + t) > token_limit:
            break
        chars.append(ch)
        weighted += w
        tokens += t
    return "".join(chars).rstrip()


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


def _is_placeholder(value: str | None) -> bool:
    if value is None:
        return False
    v = value.strip()
    if not v:
        return True
    if v.startswith("${") and v.endswith("}"):
        return True
    if v.startswith("$") and re.match(r"^\$[A-Za-z_][A-Za-z0-9_]*$", v):
        return True
    if v.startswith("<") and v.endswith(">"):
        return True
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


def validate_skill_md_content(content: str) -> str | None:
    """Validate SKILL.md content. Return error code or None if valid.

    Returns the first matching error code (from ``error_codes``), ordered by
    check priority.  Detailed diagnostic text is **not** returned — callers
    should rely on server-side logs for troubleshooting.
    """
    from jiuwenclaw.agentserver.skilldev.error_codes import (
        ERR_FW_SKILLMD_NO_FRONTMATTER_START,
        ERR_FW_SKILLMD_NO_FRONTMATTER_END,
        ERR_FW_SKILLMD_YAML_PARSE_ERROR,
        ERR_FW_SKILLMD_YAML_NOT_DICT,
        ERR_FW_SKILLMD_DUPLICATE_KEY,
        ERR_FW_SKILLMD_UNKNOWN_KEY,
        ERR_FW_SKILLMD_NAME_MISSING,
        ERR_FW_SKILLMD_NAME_EMPTY,
        ERR_FW_SKILLMD_NAME_FORMAT,
        ERR_FW_SKILLMD_NAME_TOO_LONG,
        ERR_FW_SKILLMD_DESC_MISSING,
        ERR_FW_SKILLMD_DESC_EMPTY,
        ERR_FW_SKILLMD_DESC_TOO_LONG,
        ERR_FW_SKILLMD_DESC_INVALID_TYPE,
        ERR_FW_SKILLMD_BODY_EMPTY,
        ERR_FW_SKILLMD_BODY_TOO_LONG,
        ERR_FW_SKILLMD_CREDENTIAL,
    )

    content = content.removeprefix("\ufeff")
    if not content.startswith("---"):
        return ERR_FW_SKILLMD_NO_FRONTMATTER_START

    match = FRONTMATTER_RE.match(content)
    if not match:
        return ERR_FW_SKILLMD_NO_FRONTMATTER_END

    frontmatter_text = match.group(1)
    body = content[match.end():].lstrip("\r\n")

    hit_codes: set[str] = set()

    dup = _find_duplicate_frontmatter_key(frontmatter_text)
    if dup:
        hit_codes.add(ERR_FW_SKILLMD_DUPLICATE_KEY)

    try:
        frontmatter = yaml.safe_load(frontmatter_text)
        if not isinstance(frontmatter, dict):
            return ERR_FW_SKILLMD_YAML_NOT_DICT
    except yaml.YAMLError:
        return ERR_FW_SKILLMD_YAML_PARSE_ERROR

    # unexpected = set(frontmatter.keys()) - ALLOWED_FRONTMATTER_KEYS
    # if unexpected:
    #     hit_codes.add(ERR_FW_SKILLMD_UNKNOWN_KEY)

    # --- name ---
    if "name" not in frontmatter:
        hit_codes.add(ERR_FW_SKILLMD_NAME_MISSING)
    else:
        name = frontmatter.get("name", "")
        if not isinstance(name, str):
            hit_codes.add(ERR_FW_SKILLMD_NAME_FORMAT)
        else:
            name = name.strip()
            if not name:
                hit_codes.add(ERR_FW_SKILLMD_NAME_EMPTY)
            else:
                if not re.match(r"^[a-z0-9-]+$", name) or name.startswith("-") or name.endswith("-") or "--" in name:
                    hit_codes.add(ERR_FW_SKILLMD_NAME_FORMAT)
                if len(name) > 64:
                    hit_codes.add(ERR_FW_SKILLMD_NAME_TOO_LONG)

    # --- description ---
    if "description" not in frontmatter:
        hit_codes.add(ERR_FW_SKILLMD_DESC_MISSING)
    else:
        description = frontmatter.get("description", "")
        if not isinstance(description, str):
            hit_codes.add(ERR_FW_SKILLMD_DESC_INVALID_TYPE)
        else:
            description = description.strip()
            if not description:
                hit_codes.add(ERR_FW_SKILLMD_DESC_EMPTY)
            else:
                if description_weighted_len(description) > DESCRIPTION_MAX_WEIGHTED:
                    hit_codes.add(ERR_FW_SKILLMD_DESC_TOO_LONG)
                elif estimate_skill_tokens(description) > DESCRIPTION_MAX_TOKENS:
                    hit_codes.add(ERR_FW_SKILLMD_DESC_TOO_LONG)

    # --- body ---
    if not body.strip():
        hit_codes.add(ERR_FW_SKILLMD_BODY_EMPTY)
    else:
        body_lines = body.splitlines()
        if len(body_lines) > BODY_MAX_LINES:
            hit_codes.add(ERR_FW_SKILLMD_BODY_TOO_LONG)
        elif estimate_skill_tokens(body) > BODY_MAX_TOKENS:
            hit_codes.add(ERR_FW_SKILLMD_BODY_TOO_LONG)

    # --- credential leak ---
    for _line_no, line in enumerate(content.splitlines(), start=1):
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
            hit_codes.add(ERR_FW_SKILLMD_CREDENTIAL)

    if hit_codes:
        return min(hit_codes)
    return None

