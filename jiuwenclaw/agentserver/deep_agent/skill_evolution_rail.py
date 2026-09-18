# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""JiuwenClaw-specific SkillEvolutionRail extensions."""

from __future__ import annotations

import json
from typing import Any

from openjiuwen.agent_evolving.signal import EvolutionSignal
from openjiuwen.core.common.logging import logger
from openjiuwen.core.foundation.llm import Model
from openjiuwen.harness.rails import SkillEvolutionRail

from jiuwenclaw.agentserver.llm_usage import AuxiliaryUsageReportingModel
from jiuwenclaw.utils import is_bootstrap_builtin_skill, prime_bootstrap_skill_roots


class JiuClawSkillEvolutionRail(SkillEvolutionRail):
    """Exclude official ``BOOTSTRAP.md`` skills from online self-evolution."""

    def __init__(
        self,
        skills_dir: str | list[str],
        *,
        llm: Model,
        model: str,
        auto_save: bool = True,
        **kwargs: Any,
    ) -> None:
        prime_bootstrap_skill_roots(skills_dir)
        super().__init__(
            skills_dir,
            llm=AuxiliaryUsageReportingModel(llm),
            model=model,
            auto_save=auto_save,
            **kwargs,
        )

    def update_llm(self, llm: Model, model: str) -> None:
        """Keep evolution accounting active after a runtime model reload."""
        super().update_llm(AuxiliaryUsageReportingModel(llm), model)

    @staticmethod
    def parse_messages(messages: list[Any]) -> list[dict]:
        """Normalize BaseMessage or dict messages to plain dicts for evolution."""
        result: list[dict] = []
        for message in messages:
            if isinstance(message, dict):
                result.append(message)
                continue

            role = getattr(message, "role", "")
            content = str(getattr(message, "content", "") or "")
            item: dict = {"role": role, "content": content}

            tool_calls = getattr(message, "tool_calls", None)
            if tool_calls:
                item["tool_calls"] = [
                    {
                        "id": getattr(tool_call, "id", ""),
                        "name": getattr(tool_call, "name", ""),
                        "arguments": getattr(tool_call, "arguments", ""),
                    }
                    for tool_call in tool_calls
                ]

            name = getattr(message, "name", None)
            if name:
                item["name"] = name

            result.append(item)
        return result

    @staticmethod
    def dedup_messages(messages: list[dict]) -> list[dict]:
        """Remove duplicate messages while preserving order (keep first occurrence)."""
        seen: set[str] = set()
        result: list[dict] = []
        for message in messages:
            key = json.dumps(message, sort_keys=True, ensure_ascii=False, default=str)
            if key in seen:
                continue
            seen.add(key)
            result.append(message)
        return result

    def _eligible_skill_names(self, skill_names: list[str]) -> list[str]:
        eligible = [name for name in skill_names if not is_bootstrap_builtin_skill(name)]
        excluded = len(skill_names) - len(eligible)
        if excluded:
            logger.info(
                "[JiuClawSkillEvolutionRail] excluded %d builtin skill(s) from evolution",
                excluded,
            )
        return eligible

    def _detect_signals(
        self,
        parsed_messages: list[dict],
        skill_names: list[str],
    ) -> list[EvolutionSignal]:
        return super()._detect_signals(parsed_messages, self._eligible_skill_names(skill_names))

    def _infer_primary_skill(
        self,
        parsed_messages: list[dict],
        skill_names: list[str],
    ) -> str | None:
        return super()._infer_primary_skill(parsed_messages, self._eligible_skill_names(skill_names))

    async def generate_and_emit_experience(
        self,
        skill_name: str,
        signals: list[EvolutionSignal],
        messages: list[dict],
    ) -> bool:
        if is_bootstrap_builtin_skill(skill_name):
            logger.info(
                "[JiuClawSkillEvolutionRail] skip manual evolution for builtin skill=%s",
                skill_name,
            )
            return False
        return await super().generate_and_emit_experience(skill_name, signals, messages)

    async def _generate_experience_for_skill(
        self,
        skill_name: str,
        signals: list[EvolutionSignal],
        messages: list[dict],
    ) -> list[Any]:
        if is_bootstrap_builtin_skill(skill_name):
            return []
        return await super()._generate_experience_for_skill(skill_name, signals, messages)

    async def _generate_experience_via_optimizer(
        self,
        skill_name: str,
        signals: list[EvolutionSignal],
        messages: list[dict],
    ) -> bool:
        if is_bootstrap_builtin_skill(skill_name):
            return False
        return await super()._generate_experience_via_optimizer(skill_name, signals, messages)
