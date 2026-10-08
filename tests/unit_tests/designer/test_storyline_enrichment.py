# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import json

import pytest

from jiuwenswarm.server.runtime.designer import model_tools, orchestration, script_analysis
from jiuwenswarm.server.runtime.designer.orchestration import Director


@pytest.mark.asyncio
async def test_script_analysis_prompt_develops_sparse_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, str] = {}

    async def fake_model_call(**kwargs: object) -> dict[str, object]:
        captured["system"] = str(kwargs["system"])
        return {
            "ok": True,
            "text": json.dumps(
                {
                    "story_name": "Christmas Spark",
                    "characters": [
                        {
                            "id": "char_1",
                            "name": "Celebrant",
                            "description": (
                                "red knit sweater; dark trousers; black boots"
                            ),
                        }
                    ],
                    "shots": [
                        {
                            "shot_index": 1,
                            "action": "A mysterious gift arrives beneath the tree.",
                            "camera": "wide",
                            "on_screen": ["char_1"],
                            "character_ids": ["char_1"],
                            "setting_id": "set_1",
                            "timeline": "0-7s",
                        }
                    ],
                    "target_duration_sec": 30,
                    "target_shot_count": 4,
                }
            ),
        }

    monkeypatch.setattr(model_tools, "call_model_tool", fake_model_call)

    result = await script_analysis.analyze_creative_brief(
        "Create a 30 second video for Christmas celebration.",
        timeout_sec=5,
    )

    system = captured["system"]
    assert "When the request is sparse" in system
    assert "hook → desire/problem → product demonstration or proof → payoff/CTA" in system
    assert "No filler, duplicate actions" in system
    assert "Each clip covers only its own advancing story window" in system
    assert result["target_duration_sec"] == 30


@pytest.mark.asyncio
async def test_director_brief_prompt_requires_visible_story_and_script_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, str] = {}

    async def fake_model_call(**kwargs: object) -> dict[str, object]:
        captured["system"] = str(kwargs["system"])
        return {
            "ok": True,
            "text": (
                "# Brief\n\n## Creative concept\nA gift reveals shared memories.\n\n"
                "## Narrative/content arc\nSetup, discovery, and payoff.\n\n"
                "## Timed beat plan\n0-8s setup; 8-22s discovery; 22-30s payoff.\n\n"
                "## Script/speech plan\nNarrator at 22s: \"Make this Christmas last.\""
            ),
        }

    monkeypatch.setattr(orchestration, "call_model_tool", fake_model_call)
    graph = {
        "description": "Create a 30 second video for Christmas celebration.",
        "nodes": [{"id": "n_brief", "config": {"role": "brief"}}],
        "metadata": {
            "script_analysis": {
                "characters": [{"id": "char_1", "name": "Celebrant"}],
                "scenes": [{"id": "set_1", "name": "Living room"}],
                "shots": [{"shot_index": 1, "action": "A gift arrives."}],
            }
        },
    }

    await Director().author_creative_brief(graph)

    system = captured["system"]
    assert "Creative concept; Narrative/content arc" in system
    assert "timed shot plan spanning the full requested duration" in system
    assert "Script/speech plan" in system
    assert "Speech is the default" in system
    assert "no filler, repeated action" in system
    assert "approved_brief" in graph["metadata"]


@pytest.mark.asyncio
async def test_brief_review_preserves_approved_enrichment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, str] = {}

    async def fake_model_call(**kwargs: object) -> dict[str, object]:
        captured["system"] = str(kwargs["system"])
        captured["prompt"] = str(kwargs["prompt"])
        return {
            "ok": True,
            "text": json.dumps(
                {
                    "ok": True,
                    "patched_brief_markdown": "",
                    "notes": "Approved enrichment is consistent.",
                    "issues": [],
                }
            ),
        }

    monkeypatch.setattr(orchestration, "call_model_tool", fake_model_call)
    graph = {
        "description": "Create a 30 second video for Christmas celebration.",
        "nodes": [],
        "metadata": {
            "approved_brief": (
                "# Brief\n\n## Creative concept\nA gift reveals shared memories.\n\n"
                "## Narrative/content arc\nSetup, discovery, and payoff.\n\n"
                "## Timed beat plan\n0-8s setup; 8-22s discovery; 22-30s payoff.\n\n"
                "## Script/speech plan\nNarrator: \"Make this Christmas last.\""
            ),
            "script_analysis": {
                "characters": [{"id": "char_1", "name": "Celebrant"}],
                "shots": [{"shot_index": 1, "action": "A gift arrives."}],
            },
        },
    }

    await Director().review_brief(graph)

    assert "Preserve its creative concept, narrative/content arc" in captured["system"]
    assert "not stated verbatim" in captured["system"]
    assert "A gift reveals shared memories" in captured["prompt"]
    assert "A gift reveals shared memories" in graph["metadata"]["approved_brief"]


@pytest.mark.asyncio
async def test_director_storyboard_materializes_enriched_speech_into_clip_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, str] = {}
    shots = [
        {
            "shot_index": 1,
            "timeline": "0-7s",
            "camera": "wide / slow push",
            "action": "A wrapped product lands beneath the tree as the family notices.",
            "on_screen": ["char_1"],
            "offscreen": [],
            "cast_actions": {"char_1": "turns toward the gift"},
            "featured_cast_ids": ["char_1"],
            "setting_id": "set_1",
            "speech_by_character": {},
            "speech_line": "",
        },
        {
            "shot_index": 2,
            "timeline": "7-14s",
            "camera": "medium / dolly",
            "action": "The celebrant unwraps the product and reveals its key feature.",
            "on_screen": ["char_1"],
            "offscreen": [],
            "cast_actions": {"char_1": "demonstrates the product"},
            "featured_cast_ids": ["char_1"],
            "setting_id": "set_1",
            "speech_by_character": {"char_1": "This brings everyone together."},
            "speech_line": "This brings everyone together.",
        },
        {
            "shot_index": 3,
            "timeline": "14-22s",
            "camera": "close-up / static",
            "action": "The product triggers a warm shared-memory display.",
            "on_screen": ["char_1"],
            "offscreen": [],
            "cast_actions": {"char_1": "watches the memories"},
            "featured_cast_ids": ["char_1"],
            "setting_id": "set_1",
            "speech_by_character": {},
            "speech_line": "",
        },
        {
            "shot_index": 4,
            "timeline": "22-30s",
            "camera": "wide / crane out",
            "action": "The family celebrates around the product for the final payoff.",
            "on_screen": ["char_1"],
            "offscreen": [],
            "cast_actions": {"char_1": "raises the product in celebration"},
            "featured_cast_ids": ["char_1"],
            "setting_id": "set_1",
            "speech_by_character": {"char_1": "Make this Christmas last."},
            "speech_line": "Make this Christmas last.",
        },
    ]

    async def fake_model_call(**kwargs: object) -> dict[str, object]:
        captured["system"] = str(kwargs["system"])
        return {
            "ok": True,
            "text": json.dumps(
                {
                    "characters": [
                        {
                            "id": "char_1",
                            "name": "Celebrant",
                            "description": "red sweater; dark trousers; black boots",
                        }
                    ],
                    "shots": shots,
                    "language_lock": "en",
                    "include_speech": True,
                    "include_music": True,
                    "storyboard_markdown": (
                        "# Storyboard\n\n### Shot 1 — Arrival\n- Timeline: 0-7s\n"
                        "- Action: A gift arrives.\n\n### Shot 2 — Reveal\n"
                        "- Timeline: 7-14s\n- Speech: This brings everyone together.\n\n"
                        "### Shot 4 — Payoff\n- Timeline: 22-30s\n"
                        "- Speech: Make this Christmas last.\n"
                    ),
                }
            ),
        }

    monkeypatch.setattr(orchestration, "call_model_tool", fake_model_call)
    graph = {
        "description": "Create a 30 second advertisement for a Christmas product.",
        "nodes": [
            {"id": "n_storyboard", "config": {"role": "storyboard"}},
            {
                "id": "n_clip_2",
                "config": {"role": "clip", "shot_index": 2},
            },
        ],
        "metadata": {
            "approved_brief": (
                "# Brief\n\n## Narrative/content arc\nHook, reveal, proof, payoff.\n\n"
                "## Script/speech plan\n7-14s: \"This brings everyone together.\""
            ),
            "script_analysis": {
                "source": "llm",
                "characters": [{"id": "char_1", "name": "Celebrant"}],
                "scenes": [{"id": "set_1", "name": "Living room"}],
                "shots": [shots[0]],
                "audio": {},
            },
        },
    }

    await Director().author_storyboard(graph)

    system = captured["system"]
    assert "Materialize the Brief's entire narrative/content arc" in system
    assert "no filler, repeated action" in system
    assert "speech_by_character and speech_line" in system
    assert "later clips can speak them" in system
    analysis = graph["metadata"]["script_analysis"]
    assert len(analysis["shots"]) == 4
    assert graph["nodes"][1]["config"]["speech_line"] == (
        "char_1: This brings everyone together."
    )
    assert graph["nodes"][1]["config"]["speech_by_character"] == {
        "char_1": "This brings everyone together."
    }
