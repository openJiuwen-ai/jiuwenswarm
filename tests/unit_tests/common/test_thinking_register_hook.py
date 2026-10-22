# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Unit tests for thinking hook registration."""

from __future__ import annotations

from types import SimpleNamespace

from jiuwenswarm.common.thinking import register_hook as rh
from jiuwenswarm.common.thinking.adapter import adapt_thinking


def test_on_subagent_thinking_default_skips_non_glm53_allowlisted():
    added: list = []

    class FakeSub:
        card = SimpleNamespace(id="a1", name="explore_agent")

        def add_rail(self, rail):
            added.append(rail)

    rh._on_subagent_thinking(FakeSub(), thinking="default", model="glm-5")
    assert added == []


def test_on_subagent_thinking_default_attaches_rail_for_glm53():
    added: list = []

    class FakeSub:
        card = SimpleNamespace(id="a1", name="explore_agent")

        def add_rail(self, rail):
            added.append(rail)

    rh._on_subagent_thinking(FakeSub(), thinking="default", model="glm-5.3-test")
    assert len(added) == 1
    assert added[0].__class__.__name__ == "ThinkingInjectRail"
    assert added[0]._profile.thinking == "low"


def test_on_subagent_thinking_default_skips_unsupported():
    added: list = []

    class FakeSub:
        card = SimpleNamespace(id="a1", name="explore_agent")

        def add_rail(self, rail):
            added.append(rail)

    rh._on_subagent_thinking(FakeSub(), thinking="default", model="qwen3-plus")
    assert added == []


def test_on_subagent_thinking_off_attaches_rail(monkeypatch):
    added: list = []

    class FakeSub:
        card = SimpleNamespace(id="a1", name="explore_agent")

        def add_rail(self, rail):
            added.append(rail)

    # Force a vendor that supports thinking toggle
    profile = adapt_thinking("off", model_name="glm-5")
    if not profile.injected:
        # vendor map may not match glm-5 in this env; stub adapter
        from jiuwenswarm.common.thinking.types import ThinkingProfile, freeze_llm_call_kwargs

        monkeypatch.setattr(
            rh,
            "adapt_thinking",
            lambda thinking, model=None, model_name="": ThinkingProfile(
                thinking="off",
                llm_call_kwargs=freeze_llm_call_kwargs(
                    {"extra_body": {"thinking": {"type": "disabled"}}}
                ),
                injected=True,
                degraded=False,
                model_name="glm-5",
            ),
        )

    rh._on_subagent_thinking(FakeSub(), thinking="off", model="glm-5")
    assert len(added) == 1
    assert added[0].__class__.__name__ == "ThinkingInjectRail"
