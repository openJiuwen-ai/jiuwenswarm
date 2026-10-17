# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Agent-mode web tools must follow ``preferred_language``."""

from __future__ import annotations

import re
from typing import Any

import pytest

from jiuwenswarm.server.runtime.agent_adapter import interface_deep
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
)

_CJK = re.compile(r"[\u4e00-\u9fff]")
_WEB_TOOL_NAMES = ("free_search", "fetch_webpage")


def _make_adapter(
    monkeypatch, preferred_language: str
) -> tuple[JiuWenSwarmDeepAdapter, dict[str, Any]]:
    """Create a bare deep adapter whose tool discovery registers nothing optional."""
    config = {"preferred_language": preferred_language}
    monkeypatch.setattr(interface_deep, "get_config", lambda: config)
    monkeypatch.setattr(interface_deep, "is_paid_search_enabled", lambda: False)
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._config_base_cache = config
    adapter._skill_manager = None
    adapter._vision_model_config = None
    adapter._video_model_config = None
    adapter._image_gen_model_config = None
    adapter._video_gen_model_config = None
    adapter._visual_gen_model_config = None
    adapter._xiaoyi_phone_tools_registered = False
    adapter._iter_runtime_audio_tools = lambda agent_id: []
    adapter._skill_retrieval_tools_enabled_for_runtime = lambda config_base: False
    adapter._register_shared_tool = lambda tool: None
    registered: dict[str, Any] = {}
    adapter._register_agent_owned_tool = lambda tool, owner_id: registered.update(
        {tool.card.name: tool}
    )
    return adapter, registered


def _schema_text(card: Any) -> str:
    return f"{card.description}\n{card.input_params}"


@pytest.mark.asyncio
async def test_agent_mode_web_tools_use_english_schema_for_en(monkeypatch):
    adapter, registered = _make_adapter(monkeypatch, "en")

    await adapter._get_tool_cards("agent-1")

    for name in _WEB_TOOL_NAMES:
        schema = _schema_text(registered[name].card)
        assert not _CJK.search(schema), f"{name} schema is not English: {schema}"


@pytest.mark.asyncio
async def test_agent_mode_web_tools_keep_chinese_schema_for_zh(monkeypatch):
    adapter, registered = _make_adapter(monkeypatch, "zh")

    await adapter._get_tool_cards("agent-1")

    for name in _WEB_TOOL_NAMES:
        assert _CJK.search(registered[name].card.description), name
