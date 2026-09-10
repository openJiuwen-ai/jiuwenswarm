"""Tests for Celia's packaged system-prompt instructions."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from openjiuwen.harness.prompts.sections import SectionName

from jiuwenswarm.agents.harness.common.memory.celia import prompt as prompt_module
from jiuwenswarm.agents.harness.common.memory.celia import provider as provider_module


def test_packaged_celia_prompt_contains_current_memory_contract():
    prompt_module.load_celia_agent_prompt.cache_clear()

    content = prompt_module.load_celia_agent_prompt()

    assert content.startswith("## Memory")
    assert "`mcp_gausspdmcp_memory_store` only for three specific circumstances" in content
    assert "`mcp_gausspdmcp_memory_scene_load`" in content
    assert "`mcp_gausspdmcp_memory_record_search`" in content
    assert "searchType='atomic_fact'" in content
    assert "USER.md" not in content and "MEMORY.md" not in content
    assert "CELIA_MEMORY_OVERVIEW" not in content and "CELIA_MEMORY_SCENES" not in content


def test_celia_prompt_loader_fails_open(monkeypatch):
    warnings = []

    class MissingResource:
        def joinpath(self, part):
            return self

        def read_text(self, *, encoding):
            raise FileNotFoundError("missing Celia prompt")

    prompt_module.load_celia_agent_prompt.cache_clear()
    monkeypatch.setattr(prompt_module, "files", lambda package: MissingResource())
    monkeypatch.setattr(
        prompt_module.logger,
        "warning",
        lambda *args: warnings.append(args),
    )
    try:
        assert prompt_module.load_celia_agent_prompt() == ""
        assert warnings
    finally:
        prompt_module.load_celia_agent_prompt.cache_clear()


def test_provider_compatibility_prompt_preserves_dynamic_runtime_path(monkeypatch):
    monkeypatch.setattr(
        provider_module,
        "load_celia_agent_prompt",
        lambda: "packaged Celia instructions",
    )
    provider = SimpleNamespace(
        config=SimpleNamespace(runtime_state_path="/custom/.xiaoyiruntime")
    )

    content = provider_module.CeliaMemoryProvider.system_prompt_block(provider)

    assert content.startswith("packaged Celia instructions")
    assert "The real compatibility state is at /custom/.xiaoyiruntime" in content
    assert "MEMORYSTATE=false disables L1/L2 extraction but keeps L3" in content


@pytest.mark.asyncio
async def test_celia_rail_owns_prompt_injection_and_removal(monkeypatch):
    prompt_text = "## Memory\n\nRail-owned Celia instructions."
    captured = []

    class PromptBuilder:
        language = "en"

        def __init__(self):
            self.added = []
            self.removed = []

        def add_section(self, section):
            self.added.append(section)

        def remove_section(self, name):
            self.removed.append(name)

    builder = PromptBuilder()
    agent = SimpleNamespace(
        system_prompt_builder=builder,
        prompt_attachment_manager=None,
    )

    monkeypatch.setattr(prompt_module.DeepAgentRail, "init", lambda self, current_agent: None)
    monkeypatch.setattr(prompt_module, "load_celia_agent_prompt", lambda: prompt_text)

    def build_section(content, *, language):
        captured.append((content, language))
        return SimpleNamespace(name=SectionName.EXTERNAL_MEMORY, content=content)

    monkeypatch.setattr(prompt_module, "build_external_memory_section", build_section)

    rail = prompt_module.CeliaMcpPromptRail()
    rail.init(agent)

    assert captured == [(prompt_text, "en")]
    assert len(builder.added) == 1
    assert builder.added[0].priority == 15

    rail.uninit(agent)
    await asyncio.sleep(0)

    assert SectionName.EXTERNAL_MEMORY in builder.removed
