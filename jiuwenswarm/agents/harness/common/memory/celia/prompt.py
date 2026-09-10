"""Static system-prompt instructions for the Celia memory rail."""

from __future__ import annotations

import logging
from functools import lru_cache
from importlib.resources import files

from openjiuwen.harness.prompts.sections import SectionName
from openjiuwen.harness.rails.base import DeepAgentRail
from openjiuwen.harness.rails.memory.external_memory_rail import build_external_memory_section

logger = logging.getLogger(__name__)

_PROMPT_RESOURCE = ("resources", "memory", "celia", "AGENTS.md")


@lru_cache(maxsize=1)
def load_celia_agent_prompt() -> str:
    """Load the packaged Celia instructions, failing open when unavailable."""

    try:
        resource = files("jiuwenswarm")
        for part in _PROMPT_RESOURCE:
            resource = resource.joinpath(part)
        return resource.read_text(encoding="utf-8").strip()
    except (FileNotFoundError, OSError, UnicodeError) as exc:
        logger.warning("[CeliaMemoryPrompt] failed to load packaged instructions: %s", exc)
        return ""


class CeliaMcpPromptRail(DeepAgentRail):
    """Inject memory instructions; the configured GaussPD MCP owns the tools."""

    def init(self, agent) -> None:
        super().init(agent)
        self._agent = agent
        self._inject_prompt()

    def _inject_prompt(self) -> None:
        # Mode changes replace the builder. Re-add by section name, without duplicates.
        builder = getattr(self._agent, "system_prompt_builder", None)
        if builder is not None:
            section = build_external_memory_section(
                load_celia_agent_prompt(), language=getattr(builder, "language", "cn")
            )
            if section is not None:
                section.priority = 15
                builder.add_section(section)

    async def before_model_call(self, ctx) -> None:
        self._inject_prompt()

    def uninit(self, agent) -> None:
        builder = getattr(agent, "system_prompt_builder", None)
        if builder is not None:
            builder.remove_section(SectionName.EXTERNAL_MEMORY)
        self._agent = None
        super().uninit(agent)


__all__ = ["CeliaMcpPromptRail", "load_celia_agent_prompt"]
