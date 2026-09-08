"""Install public fallback safety rules or private product safety assets."""

from __future__ import annotations

from typing import Dict, Optional

import openjiuwen.harness.prompts.sections.safety as _safety
from openjiuwen.harness.prompts.builder import PromptSection

from jiuwenswarm.agents.harness.common.prompt.private_assets import load_shared_text
from jiuwenswarm.common.utils import logger

_PATCHED = False

SAFETY_PROMPT_CN = """# 安全原则

- 保护隐私和敏感信息
- 对可能造成影响的操作先征求用户确认
- 拒绝违法、有害或侵犯他人权益的请求
"""
SAFETY_PROMPT_EN = """# Safety

- Protect private and sensitive information.
- Ask before consequential actions.
- Refuse illegal, harmful, or rights-infringing requests.
"""
SAFETY_PROMPT: Dict[str, str] = {"cn": SAFETY_PROMPT_CN, "en": SAFETY_PROMPT_EN}

_private_safety_prompt = load_shared_text("safety")
if _private_safety_prompt is not None:
    if not (
        isinstance(_private_safety_prompt, dict)
        and all(isinstance(language, str) and isinstance(text, str) for language, text in _private_safety_prompt.items())
        and {"cn", "en"}.issubset(_private_safety_prompt)
    ):
        raise TypeError("private safety prompt asset must map cn and en to text")
    SAFETY_PROMPT = _private_safety_prompt
    SAFETY_PROMPT_CN = SAFETY_PROMPT["cn"]
    SAFETY_PROMPT_EN = SAFETY_PROMPT["en"]


def build_safety_section(language: str = "en") -> Optional[PromptSection]:
    """Disable the rail's duplicate section; builders provide safety directly."""
    return None


def apply_patch() -> None:
    """Patch agent-core without exposing any private prompt text here."""
    global _PATCHED
    if _PATCHED:
        return
    _safety.SAFETY_PROMPT = SAFETY_PROMPT
    _safety.SAFETY_PROMPT_CN = SAFETY_PROMPT_CN
    _safety.SAFETY_PROMPT_EN = SAFETY_PROMPT_EN
    _safety.build_safety_section = build_safety_section
    try:
        import openjiuwen.harness.rails.security.prompt_security_rail as rail

        rail.build_safety_section = build_safety_section
    except Exception:  # noqa: BLE001
        logger.debug("[safety_override] patch prompt_security_rail failed", exc_info=True)
    _PATCHED = True


apply_patch()

__all__ = [
    "SAFETY_PROMPT",
    "SAFETY_PROMPT_CN",
    "SAFETY_PROMPT_EN",
    "apply_patch",
    "build_safety_section",
]
