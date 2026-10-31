# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""A story-wide chat edit reaches the approved brief, cast and storyboard."""

import json
from pathlib import Path

import pytest

from jiuwenswarm.agents.harness.design.rails.designer_execution_rail import REPORT_ONLY_REFUSAL
from jiuwenswarm.agents.harness.design.rails.designer_surface_sanitizer_rail import _KEEP_TOOL_NAMES
from jiuwenswarm.agents.swarm.context import SwarmBuildContext
from jiuwenswarm.agents.swarm.providers.designer_tools import (
    STORY_EDIT_TOOL_NAME,
    _build_story_edit_tool,
    build_designer_graph_tools,
)
from jiuwenswarm.common.schema.designer_graph import (
    NODE_TYPE_TABLE,
    NODE_TYPE_TEXT,
    NODE_TYPE_VIDEO,
    normalize_execution_graph,
)
from jiuwenswarm.server.runtime.design.context import StoreNodeSpawner, get_design_context_registry
from jiuwenswarm.server.runtime.designer import story_edit
from jiuwenswarm.server.runtime.designer.graph_store import DesignerGraphStore
from jiuwenswarm.server.runtime.designer.handlers import text_nodes
from jiuwenswarm.server.runtime.designer.handlers.common import path_from_uri
from jiuwenswarm.server.runtime.designer.handlers.types import NodeExecutionContext

_APPROVED_BRIEF = "# Brief\n\n- Logline: 女店员 opens the cafe door on a sunny morning."
_APPROVED_STORYBOARD = (
    "| Shot | Timeline | Camera | Move | On screen | Character action | Speech |\n"
    "| --- | --- | --- | --- | --- | --- | --- |\n"
    "| 1 | 0-5s | wide | static | 店员 | 女店员 unlocks the cafe door | 早上好 |\n"
    "| 2 | 5-10s | close | push | 店员 | 女店员 smiles at the first guest | 欢迎 |\n"
)
_EDIT = "把全片的女店员改成男店员"


def _graph(*, brief: str = _APPROVED_BRIEF, storyboard: str = _APPROVED_STORYBOARD) -> dict:
    metadata: dict = {
        "script_analysis": {
            "characters": [
                {
                    "id": "char_1",
                    "name": "店员",
                    "description": "年轻女店员，长发，围裙",
                    "identity_attrs": {"sex": "female", "wardrobe": "apron"},
                    "reference_ids": ["ref_1"],
                },
            ],
        },
    }
    if brief:
        metadata["approved_brief"] = brief
    if storyboard:
        metadata["approved_storyboard"] = storyboard
    return normalize_execution_graph({
        "schema_version": "designer-execution-graph.v1",
        "graph_id": "g_story",
        "project_id": "p_story",
        "title": "story",
        "description": "A barista opens the cafe door.",
        "nodes": [
            {
                "id": "n_brief",
                "type": NODE_TYPE_TEXT,
                "label": "Brief",
                "config": {"role": "brief", "prompt": "Write the brief."},
            },
            {
                "id": "n_storyboard",
                "type": NODE_TYPE_TABLE,
                "label": "Storyboard",
                "config": {"pipeline": "storyboard", "prompt": "Write the storyboard."},
            },
            {
                "id": "n_clip_1",
                "type": NODE_TYPE_VIDEO,
                "label": "Clip 1",
                "config": {"pipeline": "clip", "clip_index": 1, "prompt": "Shot 1."},
            },
            {
                "id": "n_note",
                "type": NODE_TYPE_TEXT,
                "label": "Note",
                "config": {"prompt": "A note."},
            },
        ],
        "edges": [
            {"id": "e_1", "source": "n_brief", "target": "n_storyboard"},
            {"id": "e_2", "source": "n_storyboard", "target": "n_clip_1"},
        ],
        "metadata": metadata,
    })


def _node(graph: dict, node_id: str) -> dict:
    return next(node for node in graph["nodes"] if node["id"] == node_id)


def _rewrite(text: str) -> str:
    return text.replace("女店员", "男店员").replace("female", "male")


@pytest.fixture()
def llm_prompts(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Fake chat model: rewrites the approved text it was given."""
    prompts: list[str] = []

    async def complete(prompt: str, *, delegate: str = "") -> str:
        prompts.append(prompt)
        for marker in ("Approved storyboard:\n", "Approved Brief:\n", "\n\nCast:\n"):
            if marker in prompt:
                return _rewrite(prompt.split(marker, 1)[1])
        raise AssertionError(f"unexpected prompt: {prompt[:80]}")

    monkeypatch.setattr(story_edit, "complete_designer_node_text", complete)
    return prompts


@pytest.fixture()
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.handlers.common.get_agent_workspace_dir",
        lambda: tmp_path,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.graph_store.get_agent_root_dir",
        lambda: tmp_path,
    )
    return tmp_path


class _Coordinator:
    def __init__(self, active: bool = False) -> None:
        self.active = active

    def is_active(self) -> bool:
        return self.active


def _spawner(
    store: DesignerGraphStore,
    coordinator: _Coordinator | None = None,
    pushed: list[dict] | None = None,
) -> StoreNodeSpawner:
    return StoreNodeSpawner(
        store,
        "g_story",
        lambda: coordinator or _Coordinator(),
        on_graph_update=pushed.append if pushed is not None else None,
    )


def _assert_story_revised(graph: dict) -> None:
    meta = graph["metadata"]
    assert "男店员" in meta["approved_brief"] and "女店员" not in meta["approved_brief"]
    assert "男店员" in meta["approved_storyboard"] and "女店员" not in meta["approved_storyboard"]
    cast = meta["script_analysis"]["characters"][0]
    assert cast["description"] == "年轻男店员，长发，围裙"
    assert cast["identity_attrs"] == {"sex": "male", "wardrobe": "apron"}
    assert cast["reference_ids"] == ["ref_1"]
    specs = str(meta.get("production_specs") or "")
    assert "年轻男店员" in specs and "女店员" not in specs
    assert "男店员" in _node(graph, "n_clip_1")["config"]["clip_action"]
    assert all("user_edit_prompt" not in node["config"] for node in graph["nodes"])


async def _run(handler, graph: dict, node_id: str) -> str:
    result = await handler.execute(
        _node(graph, node_id),
        NodeExecutionContext(graph=graph, run_id="run_story", node_id=node_id),
    )
    assert result.output_ref is not None
    path = path_from_uri(result.output_ref["uri"])
    assert path is not None
    return path.read_text(encoding="utf-8")


# ─── leader tool: designer_story_edit ──────────────────────────────────────


@pytest.mark.asyncio
async def test_story_edit_tool_revises_and_saves_brief_cast_and_storyboard(workspace, llm_prompts):
    store = DesignerGraphStore()
    store.save_graph(_graph())
    pushed: list[dict] = []

    reply = json.loads(await _build_story_edit_tool(_spawner(store, pushed=pushed)).invoke({"edit": _EDIT}))

    assert reply["revised"] == ["approved_brief", "approved_storyboard", "script_analysis"]
    assert reply["brief_node_id"] == "n_brief"
    assert "cascade=true" in reply["next"]
    assert len(llm_prompts) == 3 and all(_EDIT in prompt for prompt in llm_prompts)
    saved = store.get_graph("g_story")
    _assert_story_revised(saved)
    assert pushed and pushed[-1]["metadata"]["approved_brief"] == saved["metadata"]["approved_brief"]


@pytest.mark.asyncio
async def test_story_edit_then_cascade_outputs_show_the_edit(workspace, llm_prompts):
    """Regression for #8820: after the edit, rerunning from the brief shows the new facts."""
    store = DesignerGraphStore()
    store.save_graph(_graph())
    await _build_story_edit_tool(_spawner(store)).invoke({"edit": _EDIT})
    graph = store.get_graph("g_story")
    llm_prompts.clear()

    brief = await _run(text_nodes.BriefNodeHandler(), graph, "n_brief")
    storyboard = await _run(text_nodes.StoryboardNodeHandler(), graph, "n_storyboard")

    assert "男店员" in brief and "女店员" not in brief
    assert "男店员" in storyboard and "女店员" not in storyboard
    assert "男店员" in _node(graph, "n_clip_1")["config"]["clip_action"]
    assert llm_prompts == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("active", "brief", "storyboard", "error"),
    [
        (True, _APPROVED_BRIEF, _APPROVED_STORYBOARD, "trigger is executing"),
        (False, "", "", "no Director-approved brief or storyboard"),
    ],
    ids=["trigger_active", "no_approved_story"],
)
async def test_story_edit_tool_refuses_without_touching_the_graph(
    workspace, llm_prompts, active, brief, storyboard, error
):
    store = DesignerGraphStore()
    before = store.save_graph(_graph(brief=brief, storyboard=storyboard))

    reply = await _build_story_edit_tool(_spawner(store, _Coordinator(active))).invoke({"edit": _EDIT})

    assert reply.startswith("error:") and error in reply
    assert llm_prompts == []
    assert store.get_graph("g_story")["metadata"] == before["metadata"]


@pytest.mark.asyncio
async def test_story_edit_tool_is_report_only_after_a_run(workspace, llm_prompts):
    store = DesignerGraphStore()
    store.save_graph(_graph())
    spawner = _spawner(store)
    spawner.report_pending = True

    assert await _build_story_edit_tool(spawner).invoke({"edit": _EDIT}) == REPORT_ONLY_REFUSAL
    assert llm_prompts == []


@pytest.mark.asyncio
async def test_story_edit_keeps_the_graph_when_the_storyboard_revision_drops_a_shot(workspace, monkeypatch):
    async def complete(prompt: str, *, delegate: str = "") -> str:
        if "Approved storyboard:\n" in prompt:
            return "\n".join(_APPROVED_STORYBOARD.splitlines()[:3])
        if "\n\nCast:\n" in prompt:
            return _rewrite(prompt.split("\n\nCast:\n", 1)[1])
        return _rewrite(prompt.split("Approved Brief:\n", 1)[1])

    monkeypatch.setattr(story_edit, "complete_designer_node_text", complete)
    store = DesignerGraphStore()
    before = store.save_graph(_graph())

    reply = await _build_story_edit_tool(_spawner(store)).invoke({"edit": _EDIT})

    assert reply.startswith("error:") and "1 shots instead of 2" in reply
    assert store.get_graph("g_story")["metadata"] == before["metadata"]


def test_story_edit_tool_is_on_the_leader_surface(workspace):
    DesignerGraphStore().save_graph(_graph())
    registry = get_design_context_registry()
    registry.bind("s_story", "g_story")
    try:
        tools = build_designer_graph_tools({}, SwarmBuildContext(session_id="s_story"))
    finally:
        registry.drop("s_story")

    assert STORY_EDIT_TOOL_NAME in [tool.card.name for tool in tools]
    assert STORY_EDIT_TOOL_NAME in _KEEP_TOOL_NAMES


@pytest.mark.asyncio
async def test_revised_storyboard_keeps_the_original_shot_numbers(monkeypatch):
    async def complete(prompt: str, *, delegate: str = "") -> str:
        table = prompt.split("Approved storyboard:\n", 1)[1]
        return "```markdown\n" + _rewrite(table).replace("| 1 |", "| 7 |") + "\n```"

    monkeypatch.setattr(story_edit, "complete_designer_node_text", complete)

    revised = await story_edit.revise_storyboard(_APPROVED_STORYBOARD, _EDIT, "")

    shots = text_nodes.parse_storyboard_shots(revised)
    assert [shot["shot_no"] for shot in shots] == ["1", "2"]
    assert shots[0]["character_action"] == "男店员 unlocks the cafe door"


# ─── node prompt patches (designer_graph_patch) ────────────────────────────


def _patch(graph: dict, node_id: str, prompt: str) -> dict:
    store = DesignerGraphStore()
    store.save_graph(graph)
    node = _node(graph, node_id)
    edited = {
        "id": node_id,
        "type": node["type"],
        "label": node["label"],
        "config": {**node["config"], "prompt": prompt},
    }
    saved = _spawner(store).apply_agent_graph_patch("g_story", {"upsert_nodes": [edited]})
    return {node["id"]: node["config"] for node in saved["nodes"]}


@pytest.mark.parametrize("node_id", ["n_brief", "n_storyboard"])
def test_story_node_prompt_patch_is_kept_as_user_edit(workspace, node_id):
    configs = _patch(_graph(), node_id, f"Write it. {_EDIT}")

    assert configs[node_id]["user_edit_prompt"] == f"Write it. {_EDIT}"


@pytest.mark.parametrize(
    ("brief", "storyboard", "node_id", "prompt"),
    [
        ("", "", "n_brief", "Write the brief. Set the story on a rainy day."),
        (_APPROVED_BRIEF, "", "n_storyboard", "Write the storyboard. Rainy day."),
        (_APPROVED_BRIEF, _APPROVED_STORYBOARD, "n_note", "A rainy note."),
        (_APPROVED_BRIEF, _APPROVED_STORYBOARD, "n_brief", "Write the brief."),
        (_APPROVED_BRIEF, _APPROVED_STORYBOARD, "n_clip_1", "Shot 1."),
    ],
    ids=["no_approved_brief", "no_approved_storyboard", "other_node", "same_prompt", "same_clip_prompt"],
)
def test_other_patches_leave_no_user_edit(workspace, brief, storyboard, node_id, prompt):
    configs = _patch(_graph(brief=brief, storyboard=storyboard), node_id, prompt)

    assert all("user_edit_prompt" not in cfg for cfg in configs.values())


# ─── clip prompt patches ───────────────────────────────────────────────────

_RED_CUP = "店员 hands a red cup to the guest."


def _clip_graph() -> dict:
    graph = _graph()
    _node(graph, "n_clip_1")["config"].update({
        "clip_action": "店员 hands a white cup to the guest",
        "camera": "medium shot",
        "generate": {"prompt": "Shot 1.", "prompt_origin": "llm"},
    })
    return graph


def _assert_user_origin(cfg: dict, text: str) -> None:
    assert cfg["prompt"] == text
    assert cfg["user_edit_prompt"] == text
    assert cfg["generate"] == {"prompt": text, "prompt_origin": "user"}
    assert cfg["regenerate_packet"]["prompt"] == text


def test_clip_prompt_patch_gets_user_origin(workspace):
    configs = _patch(_clip_graph(), "n_clip_1", _RED_CUP)

    _assert_user_origin(configs["n_clip_1"], _RED_CUP)
    assert "user_edit_prompt" not in configs["n_brief"]


def test_clip_generate_prompt_patch_gets_user_origin(workspace):
    graph = _clip_graph()
    store = DesignerGraphStore()
    store.save_graph(graph)
    node = _node(graph, "n_clip_1")
    edited = {
        **node,
        "config": {**node["config"], "generate": {"prompt": _RED_CUP, "prompt_origin": "llm"}},
    }
    saved = _spawner(store).apply_agent_graph_patch("g_story", {"upsert_nodes": [edited]})

    _assert_user_origin(_node(saved, "n_clip_1")["config"], _RED_CUP)


def test_patched_clip_prompt_survives_repeated_video_calls(workspace, monkeypatch):
    from jiuwenswarm.server.runtime.designer.pipeline import video_prompt_practice
    from jiuwenswarm.server.runtime.designer.pipeline.wan_call_locks import apply_wan_call_locks

    monkeypatch.setattr(video_prompt_practice, "current_content_language", lambda: "en")
    store = DesignerGraphStore()
    store.save_graph(_clip_graph())
    clip = _node(_clip_graph(), "n_clip_1")
    saved = _spawner(store).apply_agent_graph_patch(
        "g_story",
        {"upsert_nodes": [{**clip, "config": {**clip["config"], "prompt": _RED_CUP}}]},
    )
    cfg = _node(saved, "n_clip_1")["config"]

    for _ in range(2):
        sent = apply_wan_call_locks(cfg["prompt"], cfg=cfg, graph=saved, clip_index=1)
        assert "red cup" in sent and "white cup" not in sent
        assert cfg["user_edit_prompt"] == _RED_CUP


# ─── brief / storyboard reruns ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_brief_rerun_with_an_edit_revises_brief_cast_and_storyboard(workspace, llm_prompts):
    graph = _graph()
    _node(graph, "n_brief")["config"]["user_edit_prompt"] = _EDIT

    brief = await _run(text_nodes.BriefNodeHandler(), graph, "n_brief")

    assert "男店员" in brief and "女店员" not in brief
    assert len(llm_prompts) == 3
    _assert_story_revised(graph)


@pytest.mark.asyncio
async def test_storyboard_rerun_with_an_edit_revises_only_the_storyboard(workspace, llm_prompts):
    graph = _graph()
    _node(graph, "n_storyboard")["config"]["user_edit_prompt"] = _EDIT

    storyboard = await _run(text_nodes.StoryboardNodeHandler(), graph, "n_storyboard")

    assert "男店员" in storyboard and "女店员" not in storyboard
    assert len(llm_prompts) == 1 and _APPROVED_BRIEF in llm_prompts[0]
    assert graph["metadata"]["approved_brief"] == _APPROVED_BRIEF
    assert "user_edit_prompt" not in _node(graph, "n_storyboard")["config"]
    assert "男店员" in _node(graph, "n_clip_1")["config"]["clip_action"]


@pytest.mark.asyncio
async def test_reruns_without_an_edit_replay_the_approved_texts(workspace, llm_prompts):
    graph = _graph()

    brief = await _run(text_nodes.BriefNodeHandler(), graph, "n_brief")
    storyboard = await _run(text_nodes.StoryboardNodeHandler(), graph, "n_storyboard")

    assert "女店员" in brief and "女店员" in storyboard
    assert llm_prompts == []
