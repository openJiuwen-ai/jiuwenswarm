"""Clip prompts must follow the live storyboard shot."""

from __future__ import annotations

from jiuwenswarm.server.runtime.designer.handlers.clip import (
    _looks_like_contaminated_prompt,
    build_clip_prompt,
)
from jiuwenswarm.server.runtime.designer.handlers.text_nodes import (
    sync_shot_nodes_from_storyboard_markdown,
)


def _sb_md() -> str:
    return """## Storyboard

| Shot | Timeline | Camera | Move | Character action | Continuity | Comment |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 0-5s | wide | pan left | Pastor speaks from pulpit | hold | Church: pastor mid-sermon, congregation seated |
| 2 | 5-10s | medium | static | Woman wipes tears, nods | hold | Close on weeping woman agreeing |
"""


class _FakeCtx:
    def __init__(self, graph: dict):
        self.graph = graph
        self.run_id = "run_test"
        self.node_id = "n_clip_1"
        self.run = {}


def test_contaminated_filter_allows_scene_bible_and_long_text():
    assert not _looks_like_contaminated_prompt(
        "Film shot 1. Action: pastor speaks. SCENE SPECS scene=church; STAGING LOCK: left."
    )
    long_ok = "Action: pastor speaks from the pulpit while the congregation listens attentively. " * 20
    assert not _looks_like_contaminated_prompt(long_ok)
    assert _looks_like_contaminated_prompt("PREVIOUS CLIP HAD: something\nYOUR ASSIGNMENT: else")


def test_storyboard_row_wins_over_stale_shot_action(monkeypatch):
    graph = {
        "nodes": [
            {
                "id": "n_storyboard",
                "type": "table",
                "config": {"role": "storyboard", "pipeline": "storyboard"},
            },
            {
                "id": "n_clip_1",
                "type": "video",
                "config": {
                    "role": "clip",
                    "pipeline": "clip",
                    "shot_index": 1,
                    "shot_action": "STALE unrelated beach sunset",
                    "generate": {
                        "prompt": (
                            "Film shot 1. Action: STALE unrelated beach sunset. "
                            "SCENE SPECS scene=beach."
                        )
                    },
                },
            },
        ],
        "metadata": {},
    }

    def fake_role_output_text(ctx, role):
        return _sb_md()

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.handlers.clip.role_output_text",
        fake_role_output_text,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.handlers.clip.collect_clip_scene_image",
        lambda *a, **k: None,
    )
    node = graph["nodes"][1]
    prompt = build_clip_prompt(graph, node, _FakeCtx(graph))
    assert "beach sunset" not in prompt.lower()
    assert "pastor" in prompt.lower() or "church" in prompt.lower() or "sermon" in prompt.lower()


def test_sync_shot_nodes_from_storyboard_markdown():
    graph = {
        "nodes": [
            {
                "id": "n_clip_1",
                "type": "video",
                "config": {
                    "role": "clip",
                    "pipeline": "clip",
                    "shot_index": 1,
                    "shot_action": "stale",
                    "generate": {"prompt": "old"},
                },
            },
            {
                "id": "n_frame_2",
                "type": "image",
                "config": {
                    "role": "frame",
                    "pipeline": "frame",
                    "shot_index": 2,
                    "shot_action": "stale",
                },
            },
        ]
    }
    notes = sync_shot_nodes_from_storyboard_markdown(graph, _sb_md())
    assert notes
    c1 = graph["nodes"][0]["config"]
    f2 = graph["nodes"][1]["config"]
    assert "pastor" in c1["shot_action"].lower() or "church" in c1["shot_action"].lower()
    assert "woman" in f2["shot_action"].lower() or "tear" in f2["shot_action"].lower()
    assert "Action:" in (c1.get("generate") or {}).get("prompt", "")


def test_parse_hierarchical_smart_graph_storyboard():
    from jiuwenswarm.server.runtime.designer.handlers.text_nodes import parse_storyboard_shots
    from jiuwenswarm.server.runtime.designer.smart_graph import _write_storyboard_markdown

    md = _write_storyboard_markdown(
        [
            {
                "shot_index": 1,
                "title": "Pulpit",
                "action": "Pastor speaks from pulpit",
                "camera": "wide",
                "timeline": "0-5s",
                "setting_id": "set_1",
            },
            {
                "shot_index": 2,
                "title": "Woman",
                "action": "Woman weeps and nods",
                "camera": "close-up",
                "timeline": "5-10s",
                "setting_id": "set_1",
            },
        ],
        [{"id": "char_1", "name": "Man"}],
    )
    shots = parse_storyboard_shots(md)
    assert len(shots) == 2
    assert "pastor" in (shots[0].get("character_action") or "").lower()
    assert "woman" in (shots[1].get("character_action") or "").lower()


def test_clip_prompt_leads_with_storyboard_beat(monkeypatch):
    graph = {
        "nodes": [
            {
                "id": "n_storyboard",
                "type": "table",
                "config": {"role": "storyboard", "pipeline": "storyboard"},
            },
            {
                "id": "n_clip_1",
                "type": "video",
                "config": {
                    "role": "clip",
                    "pipeline": "clip",
                    "shot_index": 1,
                    "shot_action": "ignored stale",
                },
            },
        ],
        "metadata": {},
    }

    def fake_role_output_text(ctx, role):
        return _sb_md()

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.handlers.clip.role_output_text",
        fake_role_output_text,
    )
    prompt = build_clip_prompt(graph, graph["nodes"][1], _FakeCtx(graph))
    head = prompt[:500].lower()
    assert "ignored stale" not in prompt.lower()
    assert "pastor" in head or "sermon" in head or "church" in head
    assert "continue from here" not in prompt.lower()
    assert "primary start blocking" not in prompt.lower()


def test_last_frame_clause_does_not_override_storyboard_plot():
    from jiuwenswarm.server.runtime.designer.pipeline.clip_last_frame_handoff import (
        last_frame_continuity_clause,
    )

    text = last_frame_continuity_clause(
        used=True,
        chain=[
            {"path": "/tmp/a.jpg", "shot_index": 1, "action": "walked to the door"},
        ],
    )
    low = text.lower()
    assert "storyboard shot" in low
    assert "continue from this pose" not in low
    assert "primary start blocking" not in low
