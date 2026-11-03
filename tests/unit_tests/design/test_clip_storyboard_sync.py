"""Clip prompts must follow the live storyboard shot."""

from __future__ import annotations

from pathlib import Path

import pytest

from jiuwenswarm.server.runtime.designer.handlers.clip import (
    _looks_like_contaminated_prompt,
    build_clip_prompt,
    edge_text_inputs,
)
from jiuwenswarm.server.runtime.designer.handlers.common import file_output_ref
from jiuwenswarm.server.runtime.designer.handlers.text_nodes import (
    StoryboardNodeHandler,
    parse_storyboard_shots,
    render_storyboard_table,
    sync_shot_nodes_from_storyboard_markdown,
)
from jiuwenswarm.server.runtime.designer.handlers.types import NodeExecutionContext


def _sb_md() -> str:
    return """| Shot | Timeline | Camera | Move | On screen | Character action | Speech |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 0-5s | wide | pan left | Pastor | Pastor speaks from pulpit, congregation seated | Pastor: Welcome. |
| 2 | 5-10s | medium | static | Woman | Woman wipes tears, nods |  |
"""


def _graph_with_storyboard(tmp_path: Path, clip_cfg: dict) -> dict:
    story = tmp_path / "storyboard.md"
    story.write_text(_sb_md(), encoding="utf-8")
    return {
        "nodes": [
            {
                "id": "n_storyboard",
                "type": "table",
                "label": "Storyboard",
                "config": {"role": "storyboard", "pipeline": "storyboard"},
                "output_ref": file_output_ref(story, kind="table", mime_type="text/markdown"),
            },
            {
                "id": "n_clip_1",
                "type": "video",
                "config": {"role": "clip", "pipeline": "clip", "clip_index": 1, **clip_cfg},
            },
        ],
        "edges": [{"id": "e_sb_clip", "source": "n_storyboard", "target": "n_clip_1"}],
        "metadata": {},
    }


def _ctx(graph: dict) -> NodeExecutionContext:
    return NodeExecutionContext(graph=graph, run_id="run_test", node_id="n_clip_1", run={})


def test_contaminated_filter_allows_scene_specs_and_long_text():
    assert not _looks_like_contaminated_prompt(
        "Film shot 1. Action: pastor speaks. SCENE SPECS scene=church; STAGING LOCK: left."
    )
    long_ok = "Action: pastor speaks from the pulpit while the congregation listens attentively. " * 20
    assert not _looks_like_contaminated_prompt(long_ok)
    assert _looks_like_contaminated_prompt("PREVIOUS CLIP HAD: something\nYOUR ASSIGNMENT: else")


def test_storyboard_row_wins_over_stale_clip_action(tmp_path, monkeypatch):
    graph = _graph_with_storyboard(
        tmp_path,
        {
            "clip_action": "STALE unrelated beach sunset",
            "generate": {
                "prompt": "Film shot 1. Action: STALE unrelated beach sunset. SCENE SPECS scene=beach."
            },
        },
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.handlers.clip.collect_clip_scene_image",
        lambda *a, **k: None,
    )
    prompt = build_clip_prompt(graph, graph["nodes"][1], _ctx(graph))
    assert "beach sunset" not in prompt.lower()
    assert "pastor speaks from pulpit" in prompt.lower()


def test_clip_reads_only_its_own_row_from_the_edge(tmp_path):
    graph = _graph_with_storyboard(tmp_path, {})
    prompt = build_clip_prompt(graph, graph["nodes"][1], _ctx(graph))
    assert "Pastor speaks from pulpit" in prompt
    assert "Woman wipes tears" not in prompt


def test_clip_without_storyboard_edge_does_not_read_the_storyboard(tmp_path):
    graph = _graph_with_storyboard(tmp_path, {})
    graph["edges"] = []
    prompt = build_clip_prompt(graph, graph["nodes"][1], _ctx(graph))
    assert "Pastor speaks from pulpit" not in prompt


def test_storyboard_is_not_a_connected_text_payload_for_clips(tmp_path):
    graph = _graph_with_storyboard(tmp_path, {})
    node = graph["nodes"][1]
    ctx = _ctx(graph)
    assert [label for label, _ in edge_text_inputs(ctx, node)] == ["Storyboard"]
    assert edge_text_inputs(ctx, node, exclude_roles=frozenset({"storyboard"})) == []


def test_sync_shot_nodes_from_storyboard_markdown():
    graph = {
        "nodes": [
            {
                "id": "n_clip_1",
                "type": "video",
                "config": {
                    "role": "clip",
                    "pipeline": "clip",
                    "clip_index": 1,
                    "clip_action": "stale",
                    "generate": {"prompt": "old"},
                },
            },
            {
                "id": "n_clip_2",
                "type": "image",
                "config": {
                    "role": "frame",
                    "pipeline": "frame",
                    "clip_index": 2,
                    "clip_action": "stale",
                },
            },
        ]
    }
    notes = sync_shot_nodes_from_storyboard_markdown(graph, _sb_md())
    assert notes
    c1 = graph["nodes"][0]["config"]
    f2 = graph["nodes"][1]["config"]
    assert c1["clip_action"] == "Pastor speaks from pulpit, congregation seated"
    assert f2["clip_action"] == "Woman wipes tears, nods"
    assert "Action:" in (c1.get("generate") or {}).get("prompt", "")


def test_parse_reads_every_row_of_the_table():
    rows = "\n".join(f"| {i} | {i}-{i + 1}s | wide | static | A | act {i} |  |" for i in range(1, 11))
    text = (
        "| Shot | Timeline | Camera | Move | On screen | Character action | Speech |\n"
        "| --- | --- | --- | --- | --- | --- | --- |\n"
        f"{rows}\n"
    )
    shots = parse_storyboard_shots(text)
    assert [shot["shot_no"] for shot in shots] == [str(i) for i in range(1, 11)]
    assert shots[9]["character_action"] == "act 10"


def test_render_storyboard_table_round_trips_through_the_parser():
    characters = [{"id": "char_1", "name": "Mia"}, {"id": "char_2", "name": "Leo"}]
    shots = [
        {
            "clip_index": 1,
            "timeline": "0-4s",
            "camera": "wide",
            "camera_move": "dolly in",
            "on_screen": ["char_1", "char_2"],
            "action": "Mia hands Leo a letter",
            "cast_actions": {"char_2": "reads | frowns"},
            "speech_by_character": {"char_1": "Read it."},
            "continuity_lock": {"forbid": "no rain"},
        },
        {
            "clip_index": 2,
            "timeline": "4-8s",
            "camera": "close-up",
            "on_screen": ["char_2"],
            "clip_prompt": "Leo looks up",
            "speech_line": "Leo: Why?",
        },
    ]
    text = render_storyboard_table(shots, characters)
    assert text.splitlines()[0] == (
        "| Shot | Timeline | Camera | Move | On screen | Character action | Speech |"
    )
    parsed = parse_storyboard_shots(text)
    assert len(parsed) == 2
    assert parsed[0]["move"] == "dolly in"
    assert parsed[0]["on_screen"] == "Mia, Leo"
    assert parsed[0]["character_action"] == "Mia hands Leo a letter Doing: Leo: reads / frowns"
    assert parsed[0]["speech"] == "Mia: Read it."
    assert "no rain" not in text
    assert parsed[1]["character_action"] == "Leo looks up"
    assert parsed[1]["speech"] == "Leo: Why?"


@pytest.mark.asyncio
async def test_storyboard_handler_writes_only_the_table(tmp_path, monkeypatch):
    def write(stem: str, text: str) -> Path:
        path = tmp_path / f"{stem}.md"
        path.write_text(text, encoding="utf-8")
        return path

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.handlers.text_nodes.write_workspace_text",
        write,
    )
    node = {
        "id": "n_storyboard",
        "type": "table",
        "config": {
            "role": "storyboard",
            "planned_shots": [{"clip_index": 1, "timeline": "0-5s", "action": "Door opens"}],
        },
    }
    graph = {"nodes": [node], "edges": [], "metadata": {"production_specs": "SPECS"}}
    ctx = NodeExecutionContext(graph=graph, run_id="r", node_id="n_storyboard", run={})
    result = await StoryboardNodeHandler().execute(node, ctx)
    assert result.output_ref is not None
    assert result.output_ref["kind"] == "table"
    text = (tmp_path / "designer_storyboard_r_n_storyboard.md").read_text(encoding="utf-8")
    assert all(line.startswith("|") for line in text.splitlines())
    assert "SPECS" not in text
    assert parse_storyboard_shots(text)[0]["character_action"] == "Door opens"


@pytest.mark.asyncio
async def test_storyboard_handler_rejects_graph_not_built_by_director():
    node = {"id": "n_sb", "type": "text", "config": {"role": "storyboard", "prompt": "单镜头分镜"}}
    graph = {"nodes": [node], "edges": [], "metadata": {}}
    ctx = NodeExecutionContext(graph=graph, run_id="r", node_id="n_sb", run={})
    with pytest.raises(RuntimeError, match="not built by the Director chain; rebuild the graph"):
        await StoryboardNodeHandler().execute(node, ctx)


def test_clip_prompt_leads_with_storyboard_beat(tmp_path):
    graph = _graph_with_storyboard(tmp_path, {"clip_action": "ignored stale"})
    prompt = build_clip_prompt(graph, graph["nodes"][1], _ctx(graph))
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
            {"path": "/tmp/a.jpg", "clip_index": 1, "action": "walked to the door"},
        ],
    )
    low = text.lower()
    assert "storyboard shot" in low
    assert "continue from this pose" not in low
    assert "primary start blocking" not in low
