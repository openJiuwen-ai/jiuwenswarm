# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""SignalDetector - Rules-based signal extraction from conversation messages."""
from __future__ import annotations

import re
from typing import Dict, List, Optional

from jiuwenclaw.evolution.schema import EvolutionSignal, EvolutionType


def _extract_around_match(
    content: str, match: re.Match, before: int = 300, after: int = 300
) -> str:
    """取匹配位置前后一段内容。"""
    start = max(0, match.start() - before)
    end = min(len(content), match.end() + after)
    return content[start:end]


_FAILURE_KEYWORDS = re.compile(
    r"error|exception|traceback|failed|failure|timeout|timed out"
    r"|errno|connectionerror|oserror|valueerror|typeerror"
    r"|错误|异常|失败|超时"
    r"|no such file|permission denied|access denied"
    r"|command not found|not recognized"
    r"|module not found"
    r"|econnrefused|econnreset|enoent|enotfound"
    r"|npm err!",
    re.IGNORECASE,
)

_CORRECTION_PATTERNS = [
    r"不对[，,。!]?",
    r"不是[这那]",
    r"错[了啦]",
    r"应该(是|用|改|换)",
    r"你搞错[了啦]",
    r"这不对",
    r"重新(来|做|执行|尝试)",
    r"你理解错[了啦]",
    r"纠正一下",
    r"我的意思是",
    r"that('s| is) (wrong|incorrect|not right)",
    r"you'?re wrong",
    r"should (be|use|have)",
    r"actually[,，]",
    r"no[,，] (wait|actually)",
    r"correct(ion)?:",
    r"fix(ed)?:",
]
_CORRECTION_PATTERN = re.compile("|".join(_CORRECTION_PATTERNS), re.IGNORECASE)

_SKILL_MD_PATTERN = re.compile(r"[/\\]+([^/\\]+)[/\\]+SKILL\.md", re.IGNORECASE)
_TOOL_SCHEMA_PATTERN = re.compile(r"^---\nname:\s*[^\n]+\ndescription:", re.MULTILINE)


class SignalDetector:
    """Extract evolution signals from conversation messages.

    Returns all deduplicated signals (no truncation -- caller decides).
    Each signal carries an ``evolution_type`` for downstream routing.
    """

    def __init__(
        self,
        skill_dir_map: Optional[Dict[str, str]] = None,
    ) -> None:
        self._skill_dir_map = skill_dir_map or {}

    def detect(self, messages: List[dict]) -> List[EvolutionSignal]:
        """Scan messages and return deduplicated evolution signals."""
        signals: List[EvolutionSignal] = []
        active_skill: Optional[str] = None

        for msg in messages:
            role = msg.get("role", "") if isinstance(msg, dict) else getattr(msg, "role", "")
            content = msg.get("content", "") if isinstance(msg, dict) else getattr(msg, "content", "")
            tool_calls = msg.get("tool_calls", []) if isinstance(msg, dict) else getattr(msg, "tool_calls", [])

            if role == "assistant" and tool_calls:
                active_skill = self._detect_skill_from_tool_calls(
                    tool_calls, active_skill
                )

            if role in ("tool", "function"):
                match = _FAILURE_KEYWORDS.search(content)
                if match:
                    if _TOOL_SCHEMA_PATTERN.match(content):
                        continue
                    tool_name = msg.get("name") or msg.get("tool_name")
                    excerpt = _extract_around_match(content, match)
                    signals.append(EvolutionSignal(
                        type="execution_failure",
                        evolution_type=self._classify_type(active_skill),
                        section="Troubleshooting",
                        excerpt=excerpt,
                        tool_name=tool_name,
                        skill_name=active_skill,
                    ))

            elif role == "user":
                match = _CORRECTION_PATTERN.search(content)
                if match:
                    excerpt = _extract_around_match(content, match)
                    signals.append(EvolutionSignal(
                        type="user_correction",
                        evolution_type=self._classify_type(active_skill),
                        section="Examples",
                        excerpt=excerpt,
                        skill_name=active_skill,
                    ))

        return self._deduplicate(signals)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _classify_type(skill_name: Optional[str]) -> EvolutionType:
        """Classify the evolution type for a signal.

        Currently all signals map to SKILL_EXPERIENCE.
        TODO: route signals without skill attribution to NEW_SKILL when implemented.
        """
        return EvolutionType.SKILL_EXPERIENCE

    def _detect_skill_from_tool_calls(
        self,
        tool_calls: list,
        current_active: Optional[str],
    ) -> Optional[str]:
        """从 tool_calls 里判断是否读过某 SKILL.md。"""
        for tc in tool_calls:
            name = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", "")
            args = tc.get("arguments") if isinstance(tc, dict) else getattr(tc, "arguments", "")

            if "file" in name.lower() or "read" in name.lower():
                m = _SKILL_MD_PATTERN.search(args)
                if m:
                    detected_skill = m.group(1)
                    if not self._skill_dir_map or detected_skill in self._skill_dir_map:
                        return detected_skill

        return current_active

    @staticmethod
    def _deduplicate(signals: List[EvolutionSignal]) -> List[EvolutionSignal]:
        """Deduplicate signals by (type, excerpt[:100])."""
        seen: set[tuple] = set()
        result: List[EvolutionSignal] = []
        for sig in signals:
            key = (sig.type, sig.excerpt[:100])
            if key not in seen:
                seen.add(key)
                result.append(sig)
        return result
