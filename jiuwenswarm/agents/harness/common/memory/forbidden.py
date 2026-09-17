# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
from __future__ import annotations
import logging
import re
from typing import Any, Dict, Iterable

logger = logging.getLogger(__name__)


# ``mask_sensitive`` already contains the product-wide credential/PII rules used
# by logging.  Memory adds the forms most often produced by an LLM (natural
# language and Markdown tables), which are intentionally not log key/value
# syntax.
_MEMORY_SECRET_LABEL_PATTERN = re.compile(
    r"(?ix)"
    r"(?:api[\s_-]*(?:key|token)|access[\s_-]*token|refresh[\s_-]*token|"
    r"password|passwd|pwd|secret(?:[\s_-]*key)?|authorization|"
    r"api\s*密钥|访问令牌|刷新令牌|密码|口令|密钥|令牌)"
    r"[\s*_`\"']*"
    r"(?:[:：=]|\||\bis\b|是|为)"
    r"\s*[*_`\"']*"
    r"(?P<value>[^\s|,，;；<>\[\]{}\"'`]{6,})"
)

_NON_SECRET_PLACEHOLDERS = frozenset(
    {
        "******",
        "<redacted>",
        "redacted",
        "masked",
        "hidden",
        "unknown",
        "none",
        "null",
        "required",
        "未设置",
        "已脱敏",
    }
)


def _get_memory_forbidden_config() -> Dict[str, Any]:
    """从 config.yaml 读取 memory.forbidden_memory_definition 配置."""
    try:
        from jiuwenswarm.common.config import get_config

        config = get_config()
        memory_config = config.get("memory", {})
        forbidden_config = memory_config.get("forbidden_memory_definition", {})
        return {
            "enabled": forbidden_config.get("enabled", False),
            "patterns": forbidden_config.get("patterns", []),
            "description": forbidden_config.get(
                "description",
                {
                    "zh": "以下内容禁止记忆：密码、API密钥、Secret、Token、信用卡号、身份证号、手机号等敏感信息",
                    "en": "The following content is forbidden to remember: passwords, API keys, secrets, tokens, \
                    credit card numbers, ID numbers, phone numbers and other sensitive information",
                },
            ),
        }
    except Exception as e:
        logger.warning("[forbidden] Failed to load memory forbidden config: %s", e)
        return {"enabled": False, "patterns": [], "description": {}}


def _matches_custom_pattern(text: str, patterns: Iterable[Any]) -> bool:
    """Match configured rules as regex, falling back to a literal match."""
    for raw_pattern in patterns:
        if not isinstance(raw_pattern, str) or not raw_pattern:
            continue
        try:
            if re.search(raw_pattern, text, flags=re.IGNORECASE):
                return True
        except re.error:
            if raw_pattern.casefold() in text.casefold():
                return True
    return False


def contains_forbidden_memory_content(text: Any) -> bool:
    """Detect sensitive content before it is persisted as memory.

    The function is deliberately side-effect free and never logs the inspected
    value.  It combines the existing product-wide sensitive-data detector with
    user-supplied memory rules.  When the switch is off it is a strict no-op.
    """
    config = _get_memory_forbidden_config()
    if not config.get("enabled", False) or text is None:
        return False

    value = str(text)
    if not value:
        return False

    try:
        from jiuwenswarm.common.utils import mask_sensitive

        masked = mask_sensitive(value)
        # ``mask_sensitive`` intentionally normalizes quote characters around
        # key/value pairs.  Compare newly introduced masks/fingerprints instead
        # of raw strings so already-redacted memory remains writable.
        if masked.count("******") > value.count("******") or masked.count(
            "(fp:"
        ) > value.count("(fp:"):
            return True
    except Exception as exc:
        # A filtering helper failure must not expose content in logs.  Continue
        # with the memory-local rules and configured patterns.
        logger.warning(
            "[forbidden] Built-in sensitive-data check failed: %s", type(exc).__name__
        )

    for match in _MEMORY_SECRET_LABEL_PATTERN.finditer(value):
        candidate = match.group("value").strip().casefold()
        if candidate not in _NON_SECRET_PLACEHOLDERS and not candidate.startswith(
            "******"
        ):
            return True

    return _matches_custom_pattern(value, config.get("patterns", []))


def get_forbidden_memory_prompt(language: str) -> str:
    """读取 config.yaml 的 memory.forbidden_memory_definition，
    返回格式化的限制提示词。enabled=false 时返回空字符串。

    Args:
        language: 语言代码 (zh/en)

    Returns:
        格式化的禁止记忆提示词，或空字符串
    """
    config = _get_memory_forbidden_config()

    if not config.get("enabled", False):
        return ""

    normalized_language = "zh" if language in ("cn", "zh") else "en"
    description = config.get("description", {})
    desc_text = description.get(
        normalized_language,
        description.get("zh", ""),
    )
    patterns = config.get("patterns", [])

    if normalized_language == "zh":
        prompt_parts = ["### 记忆限制规则", ""]
        if desc_text:
            prompt_parts.append(desc_text)
            prompt_parts.append("")
        if patterns:
            prompt_parts.append("**禁止记忆的敏感信息类型包括：**")
            prompt_parts.append("")
            for i, pattern in enumerate(patterns, 1):
                prompt_parts.append(f"{i}. `{pattern}`")
            prompt_parts.append("")
        prompt_parts.append("**执行要求：**")
        prompt_parts.append(
            "- 在调用 `experience_learn` 或 `write_memory` 存储记忆前，必须检查内容是否包含上述敏感信息"
        )
        prompt_parts.append(
            "- 如果检测到敏感信息，必须对其进行脱敏处理（如替换为 ***）或拒绝存储"
        )
        prompt_parts.append("- 用户明确要求的密码、密钥等敏感信息不得存入记忆系统")
        prompt_parts.append("")
        return "\n".join(prompt_parts)

    prompt_parts = ["### Memory Restriction Rules", ""]
    if desc_text:
        prompt_parts.append(desc_text)
        prompt_parts.append("")
    if patterns:
        prompt_parts.append("**Types of sensitive information forbidden to remember:**")
        prompt_parts.append("")
        for i, pattern in enumerate(patterns, 1):
            prompt_parts.append(f"{i}. `{pattern}`")
        prompt_parts.append("")
    prompt_parts.append("**Requirements:**")
    prompt_parts.append(
        "- Before calling `experience_learn` or `write_memory` to store memories, \
            you must check if the content contains the above sensitive information"
    )
    prompt_parts.append(
        "- If sensitive information is detected, it must be desensitized \
            (e.g., replaced with ***) or storage must be refused"
    )
    prompt_parts.append(
        "- Sensitive information such as passwords and keys explicitly provided by the user \
            must not be stored in the memory system"
    )
    prompt_parts.append("")
    return "\n".join(prompt_parts)


def get_disabled_memory_filter_prompt(language: str) -> str:
    """Explain the disabled state so the model does not invent a rejection.

    The execution guard remains the authority.  This prompt only prevents a
    model from treating previous refusals in the same session as the current
    product policy after the user turns filtering off.
    """
    if _get_memory_forbidden_config().get("enabled", False):
        return ""
    if language in ("cn", "zh"):
        return (
            "### 记忆敏感信息过滤状态\n\n"
            "当前敏感信息记忆过滤开关已关闭。用户明确要求将密码、API 密钥、Token 等敏感信息"
            "写入记忆时，允许保存其原始内容，并应正常调用可用的记忆写入工具；不要仅以内容敏感"
            "为由拒绝，也不要自行脱敏或替用户改变决定。此授权仅适用于记忆写入，不代表可以将"
            "敏感信息写入源码、日志或版本控制；其他工具权限和安全规则保持不变。\n"
        )
    return (
        "### Sensitive-memory filter status\n\n"
        "The sensitive-memory filter is currently disabled. When the user explicitly asks to write a "
        "password, API key, token, or other sensitive value to memory, storing the original value is "
        "allowed: use the available memory-write tools instead of refusing, redacting, or overriding "
        "the user's decision solely because the content is sensitive. This authorization applies only "
        "to memory writes; it does not permit placing secrets in source code, logs, or version control. "
        "All other tool permissions and safety rules remain unchanged.\n"
    )
