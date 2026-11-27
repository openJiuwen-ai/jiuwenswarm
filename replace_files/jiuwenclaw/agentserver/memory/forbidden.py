# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
from __future__ import annotations
import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)


def _get_memory_forbidden_config() -> Dict[str, Any]:
    """从 config.yaml 读取 memory.forbidden_memory_definition 配置."""
    try:
        from jiuwenclaw.config import get_config
        config = get_config()
        memory_config = config.get("memory", {})
        forbidden_config = memory_config.get("forbidden_memory_definition", {})
        return {
            "enabled": forbidden_config.get("enabled", False),
            "patterns": forbidden_config.get("patterns", []),
            "description": forbidden_config.get("description", {
                "zh": "以下内容禁止记忆：密码、API密钥、Secret、Token、信用卡号、身份证号、手机号等敏感信息",
                "en": "The following content is forbidden to remember: passwords, API keys, secrets, tokens, \
                    credit card numbers, ID numbers, phone numbers and other sensitive information",
            }),
        }
    except Exception as e:
        logger.warning("[forbidden] Failed to load memory forbidden config: %s", e)
        return {"enabled": False, "patterns": [], "description": {}}


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

    description = config.get("description", {})
    desc_text = description.get(language, description.get("zh", ""))
    patterns = config.get("patterns", [])

    if language == "zh":
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
        prompt_parts.append("- 在调用 `experience_learn` 或 `write_memory` 存储记忆前，必须检查内容是否包含上述敏感信息")
        prompt_parts.append("- 如果检测到敏感信息，必须对其进行脱敏处理（如替换为 ***）或拒绝存储")
        prompt_parts.append("- 用户明确要求的密码、密钥等敏感信息不得存入记忆系统")
        prompt_parts.append("")
        return "\n".join(prompt_parts)
    else:
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
        prompt_parts.append("- Before calling `experience_learn` or `write_memory` to store memories, "
                            "you must check if the content contains the above sensitive information")
        prompt_parts.append("- If sensitive information is detected, it must be desensitized "
                            "(e.g., replaced with ***) or storage must be refused")
        prompt_parts.append("- Sensitive information such as passwords and keys explicitly provided by the user "
                            "must not be stored in the memory system")
        prompt_parts.append("")
        return "\n".join(prompt_parts)
