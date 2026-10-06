# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import base64
from pathlib import Path
from types import SimpleNamespace

import pytest

from jiuwenswarm.server.runtime.designer.graph_store import DesignerGraphStore
from jiuwenswarm.server.runtime.designer.user_references import (
    UserReferenceError,
    analysis_prompt_with_references,
    attach_user_references_to_graph,
    image_reference_records,
    materialize_user_references_for_analysis,
    normalize_user_references,
    prompt_slot_roster,
    rebase_creative_intent_paths,
    user_reference_image_paths,
)


def _png_bytes() -> bytes:
    return b"\x89PNG\r\n\x1a\n" + b"ref"


@pytest.fixture()
def designer_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> DesignerGraphStore:
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.graph_store.get_agent_root_dir",
        lambda: tmp_path,
    )
    return DesignerGraphStore()


def test_normalize_copies_path_and_assigns_ordered_slots(tmp_path: Path) -> None:
    source = tmp_path / "face.png"
    source.write_bytes(_png_bytes())
    dest = tmp_path / "refs"
    refs = normalize_user_references(
        [{"kind": "image", "path": str(source), "filename": "face.png", "mime_type": "image/png"}],
        dest_dir=dest,
    )
    assert len(refs) == 1
    assert refs[0]["id"] == "ref_01"
    assert refs[0]["kind"] == "image"
    assert refs[0]["role"] == "reference"
    copied = Path(refs[0]["path"])
    assert copied.is_file()
    assert copied.parent == dest.resolve()
    assert copied.read_bytes() == _png_bytes()
    assert "image 1 = " in prompt_slot_roster(refs)
    assert "face.png" in prompt_slot_roster(refs)
    assert str(copied) not in prompt_slot_roster(refs)


def test_normalize_decodes_base64_and_rejects_over_limit(tmp_path: Path) -> None:
    dest = tmp_path / "refs"
    payload = base64.b64encode(_png_bytes()).decode("ascii")
    refs = normalize_user_references(
        [{"kind": "image", "filename": "shot.png", "base64_data": payload, "mime_type": "image/png"}],
        dest_dir=dest,
    )
    assert Path(refs[0]["path"]).read_bytes() == _png_bytes()
    five = normalize_user_references(
        [
            {"kind": "image", "filename": f"{index}.png", "base64_data": payload}
            for index in range(5)
        ],
        dest_dir=dest,
    )
    assert len(five) == 5
    with pytest.raises(UserReferenceError, match="at most 5 image"):
        normalize_user_references(
            [
                {"kind": "image", "filename": f"{index}.png", "base64_data": payload}
                for index in range(6)
            ],
            dest_dir=dest,
        )


def test_materialize_for_analysis_gives_path_to_base64_only_upload() -> None:
    """Enter must not skip classify because base64 preview left path empty."""
    payload = base64.b64encode(_png_bytes()).decode("ascii")
    preview = normalize_user_references(
        [
            {
                "kind": "image",
                "filename": "xiaoyue.png",
                "base64_data": payload,
                "mime_type": "image/png",
            }
        ],
        dest_dir=None,
    )
    assert preview[0]["path"] == ""

    refs = materialize_user_references_for_analysis(
        [
            {
                "kind": "image",
                "filename": "xiaoyue.png",
                "base64_data": payload,
                "mime_type": "image/png",
            }
        ]
    )
    assert len(image_reference_records(refs)) == 1
    path = Path(refs[0]["path"])
    assert path.is_file()
    assert path.read_bytes() == _png_bytes()


def test_rebase_creative_intent_paths_onto_project_refs(tmp_path: Path) -> None:
    payload = base64.b64encode(_png_bytes()).decode("ascii")
    analysis = {
        "creative_intent": {
            "mode": "reference_led",
            "slots": [
                {
                    "slot": 1,
                    "path": "/tmp/analysis-only/xiaoyue.png",
                    "roles": ["character_identity"],
                    "bindings": {"character_identity": "verbatim"},
                    "node_id": "n_ref_01",
                }
            ],
        }
    }
    project_refs = normalize_user_references(
        [
            {
                "kind": "image",
                "filename": "xiaoyue.png",
                "base64_data": payload,
                "mime_type": "image/png",
            }
        ],
        dest_dir=tmp_path / "refs",
    )
    rebase_creative_intent_paths(analysis, project_refs)
    assert analysis["creative_intent"]["slots"][0]["path"] == project_refs[0]["path"]


def test_attach_rebases_existing_n_ref_path(tmp_path: Path) -> None:
    payload = base64.b64encode(_png_bytes()).decode("ascii")
    project_refs = normalize_user_references(
        [
            {
                "kind": "image",
                "filename": "xiaoyue.png",
                "base64_data": payload,
                "mime_type": "image/png",
            }
        ],
        dest_dir=tmp_path / "refs",
    )
    graph = {
        "nodes": [
            {
                "id": "n_brief",
                "type": "text",
                "config": {"role": "brief", "prompt": "hi"},
            },
            {
                "id": "n_ref_01",
                "type": "image",
                "config": {
                    "role": "image",
                    "user_reference_id": "ref_01",
                    "user_reference_path": "/tmp/stale/xiaoyue.png",
                    "force_handler": True,
                    "skip_llm": True,
                    "immutable_source": True,
                },
                "output_ref": {"kind": "image", "uri": "file:///tmp/stale/xiaoyue.png"},
            },
        ],
        "edges": [],
        "metadata": {},
    }
    attach_user_references_to_graph(graph, project_refs)
    ref = next(n for n in graph["nodes"] if n["id"] == "n_ref_01")
    assert ref["config"]["user_reference_path"] == project_refs[0]["path"]
    assert ref["output_ref"]["uri"].endswith("xiaoyue.png")
    assert "stale" not in ref["config"]["user_reference_path"]


def test_attach_user_references_stays_on_metadata_not_brief_body(tmp_path: Path) -> None:
    source = tmp_path / "look.png"
    source.write_bytes(_png_bytes())
    refs = normalize_user_references(
        [{"kind": "image", "path": str(source), "filename": "look.png"}],
        dest_dir=tmp_path / "refs",
    )
    graph = {
        "nodes": [
            {
                "id": "n_brief",
                "type": "text",
                "config": {"role": "brief", "prompt": "火车站短片", "director_task": "Write a brief."},
            }
        ],
        "metadata": {},
    }
    attached = attach_user_references_to_graph(graph, refs)
    stored = attached["metadata"]["user_references"]
    assert stored[0]["path"] == refs[0]["path"]
    brief = attached["nodes"][0]["config"]
    assert brief["prompt"] == "火车站短片"
    assert brief["user_reference_ids"] == ["ref_01"]
    assert "image 1 =" in brief["director_task"]
    assert user_reference_image_paths(attached)[0].is_file()
    assert "火车站短片" in analysis_prompt_with_references("火车站短片", refs)


def test_carry_user_references_survives_graph_rebuild(tmp_path: Path) -> None:
    from jiuwenswarm.server.runtime.designer.user_references import (
        REFERENCE_NODE_PREFIX,
        carry_user_references,
        is_user_reference_node,
    )

    source = tmp_path / "naiwa.jpg"
    source.write_bytes(_png_bytes())
    refs = normalize_user_references(
        [{"kind": "image", "path": str(source), "filename": "naiwa.jpg", "mime_type": "image/jpeg"}],
        dest_dir=tmp_path / "refs",
    )
    prior = attach_user_references_to_graph(
        {
            "nodes": [{"id": "n_brief", "type": "text", "config": {"role": "brief"}}],
            "metadata": {},
        },
        refs,
    )
    # Director rebuilds drop metadata and replace the brief node wholesale.
    rebuilt = {
        "nodes": [{"id": "n_brief", "type": "text", "config": {"role": "brief"}}],
        "edges": [],
        "metadata": {"script_analysis": {"source": "llm"}},
    }

    seeded = carry_user_references(prior["metadata"], rebuilt)

    assert seeded == [f"{REFERENCE_NODE_PREFIX}01"]
    stored = rebuilt["metadata"]["user_references"]
    assert [item["filename"] for item in stored] == ["naiwa.jpg"]
    assert user_reference_image_paths(rebuilt)[0].is_file()
    ref_node = next(node for node in rebuilt["nodes"] if node["id"] == f"{REFERENCE_NODE_PREFIX}01")
    assert is_user_reference_node(ref_node)
    assert rebuilt["metadata"]["script_analysis"] == {"source": "llm"}


def test_carry_user_references_is_noop_without_attachments() -> None:
    from jiuwenswarm.server.runtime.designer.user_references import carry_user_references

    rebuilt = {"nodes": [], "metadata": {"scenario": "video"}}
    assert carry_user_references({}, rebuilt) == []
    assert carry_user_references(None, rebuilt) == []
    assert carry_user_references({"user_references": []}, rebuilt) == []
    assert rebuilt["nodes"] == []


def test_user_reference_node_is_immutable_passthrough(tmp_path: Path) -> None:
    from jiuwenswarm.server.runtime.designer.handlers import resolve_handler_key
    from jiuwenswarm.server.runtime.designer.smart_graph import apply_runtime_delegate
    from jiuwenswarm.server.runtime.designer.user_references import REFERENCE_NODE_PREFIX

    source = tmp_path / "naiwa.jpg"
    source.write_bytes(_png_bytes())
    refs = normalize_user_references(
        [{"kind": "image", "path": str(source), "filename": "naiwa.jpg", "mime_type": "image/jpeg"}],
        dest_dir=tmp_path / "refs",
    )
    graph = attach_user_references_to_graph(
        {
            "schema_version": "designer-execution-graph.v1",
            "graph_id": "graph_refnode01",
            "project_id": "proj_refnode01",
            "nodes": [{"id": "n_brief", "type": "text", "config": {"role": "brief"}}],
            "edges": [],
            "metadata": {},
        },
        refs,
    )
    node_id = f"{REFERENCE_NODE_PREFIX}01"
    assert resolve_handler_key(next(n for n in graph["nodes"] if n["id"] == node_id)) == (
        "user_reference"
    )

    # Even with LLM agents enabled, an upload must never be regenerated.
    applied = apply_runtime_delegate(graph)

    cfg = next(node for node in applied["nodes"] if node["id"] == node_id)["config"]
    assert cfg["delegate"] == "handler"
    assert cfg["force_handler"] is True
    assert cfg["read_only"] is True


def test_bootstrap_graph_registers_user_references(
    designer_store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jiuwenswarm.server.runtime.gateway_adapter import designer_adapter as adapter

    monkeypatch.setattr(adapter, "_store", designer_store)
    monkeypatch.setattr(
        adapter.project_store,
        "get_project_by_id",
        lambda project_id, cache_bust=False: SimpleNamespace(
            project_id=project_id,
            project_dir=str(tmp_path / "proj"),
            work_mode="work",
            hidden=False,
        ),
    )
    (tmp_path / "proj").mkdir()
    image = tmp_path / "hero.png"
    image.write_bytes(_png_bytes())
    # Sync bootstrap only materializes; LLM analysis is produced on the event loop.
    analysis = {
        "source": "llm",
        "characters": [{"id": "char_1", "name": "Hero", "description": "reference cast"}],
        "scenes": [{"id": "set_1", "name": "Set", "description": "location"}],
        "shots": [
            {
                "shot_index": 1,
                "action": "stands",
                "camera": "medium",
                "on_screen": ["char_1"],
                "setting_id": "set_1",
                "timeline": "0-5s",
            }
        ],
        "target_shot_count": 1,
    }
    payload, error, code = adapter._bootstrap_graph(
        {
            "prompt": "按参考图做一个短片",
            "project_id": "proj_refs01",
            "references": [
                {
                    "kind": "image",
                    "filename": "hero.png",
                    "mime_type": "image/png",
                    "path": str(image),
                }
            ],
        },
        "web",
        analysis,
    )
    assert error is None
    assert code is None
    assert payload is not None
    refs = payload["graph"]["metadata"]["user_references"]
    assert refs[0]["kind"] == "image"
    assert Path(refs[0]["path"]).is_file()
    brief = next(node for node in payload["graph"]["nodes"] if node["id"] == "n_brief")
    assert brief["config"]["prompt"] == "按参考图做一个短片"
    assert brief["config"]["user_reference_ids"] == ["ref_01"]


def test_user_reference_video_and_audio_paths(tmp_path: Path) -> None:
    from jiuwenswarm.server.runtime.designer.user_references import (
        user_reference_audio_path,
        user_reference_video_path,
    )

    video = tmp_path / "motion.mp4"
    audio = tmp_path / "theme.mp3"
    image = tmp_path / "face.png"
    video.write_bytes(b"fake-mp4")
    audio.write_bytes(b"fake-mp3")
    image.write_bytes(_png_bytes())
    refs = normalize_user_references(
        [
            {"kind": "image", "path": str(image), "filename": "face.png", "mime_type": "image/png"},
            {"kind": "video", "path": str(video), "filename": "motion.mp4", "mime_type": "video/mp4"},
            {"kind": "audio", "path": str(audio), "filename": "theme.mp3", "mime_type": "audio/mpeg"},
        ],
        dest_dir=tmp_path / "refs",
    )
    graph = attach_user_references_to_graph({"nodes": [], "metadata": {}}, refs)
    assert user_reference_image_paths(graph)[0].is_file()
    assert user_reference_video_path(graph) is not None
    assert user_reference_video_path(graph).suffix == ".mp4"
    assert user_reference_audio_path(graph) is not None
    assert "video 1 = " in prompt_slot_roster(refs)
    assert "audio 1 = " in prompt_slot_roster(refs)


def test_reference_image_becomes_a_canvas_node_feeding_brief_and_cast(
    tmp_path: Path,
) -> None:
    from jiuwenswarm.common.schema.designer_graph import normalize_execution_graph
    from jiuwenswarm.server.runtime.designer.handlers import resolve_handler_key

    image = tmp_path / "hero.png"
    image.write_bytes(_png_bytes())
    refs = normalize_user_references(
        [{"kind": "image", "path": str(image), "filename": "hero.png", "mime_type": "image/png"}],
        dest_dir=tmp_path / "refs",
    )
    graph = attach_user_references_to_graph(
        {
            "graph_id": "graph_ref",
            "project_id": "p",
            "title": "t",
            "schema_version": "designer-execution-graph.v1",
            "nodes": [
                {"id": "n_brief", "type": "text", "config": {"role": "brief"}},
                {
                    "id": "n_character",
                    "type": "image",
                    "config": {
                        "role": "character_design",
                        "character_ids": ["char_1"],
                    },
                },
            ],
            "edges": [],
            "metadata": {
                "script_analysis": {
                    "reference_reads": [
                        {"slot": 1, "subject": "character", "character_id": "char_1"}
                    ]
                }
            },
        },
        refs,
    )
    ref_node = next(n for n in graph["nodes"] if str(n["id"]).startswith("n_ref_"))
    assert ref_node["output_ref"]["uri"].startswith("file:")
    assert ref_node["config"]["interaction_mode"] == "upload"
    wired = {(e["source"], e["target"]) for e in graph["edges"]}
    assert ("n_ref_01", "n_brief") in wired
    assert ("n_ref_01", "n_character") in wired
    cast = next(n for n in graph["nodes"] if n["id"] == "n_character")
    assert cast["config"]["character_source_reference"] == "ref_01"
    # Survives schema validation and dispatches to the passthrough handler.
    saved = normalize_execution_graph(graph)
    kept = next(n for n in saved["nodes"] if n["id"] == "n_ref_01")
    assert kept["output_ref"]["uri"].startswith("file:")
    assert resolve_handler_key(kept) == "user_reference"


def test_reference_images_follow_what_the_agent_saw(tmp_path: Path) -> None:
    person = tmp_path / "person.png"
    place = tmp_path / "place.png"
    prop = tmp_path / "prop.png"
    for path in (person, place, prop):
        path.write_bytes(_png_bytes())
    refs = normalize_user_references(
        [
            {"kind": "image", "path": str(person), "filename": "person.png", "mime_type": "image/png"},
            {"kind": "image", "path": str(place), "filename": "place.png", "mime_type": "image/png"},
            {"kind": "image", "path": str(prop), "filename": "prop.png", "mime_type": "image/png"},
        ],
        dest_dir=tmp_path / "refs",
    )
    graph = attach_user_references_to_graph(
        {
            "nodes": [
                {"id": "n_brief", "type": "text", "config": {"role": "brief"}},
                {
                    "id": "n_character",
                    "type": "image",
                    "config": {"role": "character_design", "character_ids": ["char_1"]},
                },
                {
                    "id": "n_character_2",
                    "type": "image",
                    "config": {"role": "character_design", "character_ids": ["char_2"]},
                },
                {
                    "id": "n_scene_1",
                    "type": "image",
                    "config": {"role": "scene", "setting_id": "set_1"},
                },
                {"id": "n_clip_1", "type": "video", "config": {"role": "clip"}},
            ],
            "edges": [],
            "metadata": {
                "script_analysis": {
                    "reference_reads": [
                        {"slot": 1, "subject": "character", "character_id": "char_1"},
                        {"slot": 2, "subject": "scene", "setting_id": "set_1"},
                        {"slot": 3, "subject": "object"},
                    ]
                }
            },
        },
        refs,
    )
    wired = {(e["source"], e["target"]) for e in graph["edges"]}
    assert ("n_ref_01", "n_character") in wired
    assert ("n_ref_01", "n_character_2") not in wired
    assert ("n_ref_01", "n_scene_1") not in wired
    assert ("n_ref_01", "n_clip_1") not in wired
    assert ("n_ref_02", "n_scene_1") in wired
    assert ("n_ref_02", "n_character") not in wired
    assert ("n_ref_02", "n_clip_1") not in wired
    assert ("n_ref_03", "n_clip_1") in wired
    assert ("n_ref_03", "n_character") not in wired
    assert ("n_ref_03", "n_scene_1") not in wired
    unread = attach_user_references_to_graph(
        {
            "nodes": [
                {"id": "n_brief", "type": "text", "config": {"role": "brief"}},
                {"id": "n_character", "type": "image", "config": {"role": "character_design"}},
                {"id": "n_scene_1", "type": "image", "config": {"role": "scene", "setting_id": "set_1"}},
                {"id": "n_clip_1", "type": "video", "config": {"role": "clip"}},
            ],
            "edges": [],
            "metadata": {},
        },
        normalize_user_references(
            [{"kind": "image", "path": str(person), "filename": "person.png", "mime_type": "image/png"}],
            dest_dir=tmp_path / "unread",
        ),
    )
    unread_wired = {(e["source"], e["target"]) for e in unread["edges"]}
    assert ("n_ref_01", "n_brief") in unread_wired
    assert ("n_ref_01", "n_character") not in unread_wired
    assert ("n_ref_01", "n_scene_1") not in unread_wired
    assert ("n_ref_01", "n_clip_1") not in unread_wired


def test_product_reference_stays_on_the_canvas_and_feeds_the_clip(tmp_path: Path) -> None:
    from jiuwenswarm.server.runtime.designer.smart_graph import prune_non_contributing_nodes

    image = tmp_path / "01_reference.jpg"
    image.write_bytes(_png_bytes())
    refs = normalize_user_references(
        [{"kind": "image", "path": str(image), "filename": "01_reference.jpg", "mime_type": "image/jpeg"}],
        dest_dir=tmp_path / "refs",
    )
    refs[0]["subject"] = "object"
    graph = attach_user_references_to_graph(
        {
            "nodes": [
                {"id": "n_brief", "type": "text", "config": {"role": "brief"}},
                {"id": "n_clip_1", "type": "video", "config": {"role": "clip"}},
                {"id": "n_compose", "type": "video", "config": {"role": "compose"}},
            ],
            "edges": [{"id": "e_clip_compose", "source": "n_clip_1", "target": "n_compose", "kind": "data"}],
            "metadata": {},
        },
        refs,
    )
    wired = {(e["source"], e["target"]) for e in graph["edges"]}
    assert ("n_ref_01", "n_clip_1") in wired
    ref = next(n for n in graph["nodes"] if n["id"] == "n_ref_01")
    assert ref["config"]["user_reference_id"] == "ref_01"
    assert "user_added" not in ref["config"]
    assert ref["output_ref"]["uri"].startswith("file:")
    graph["edges"] = [
        e for e in graph["edges"] if e["source"] != "n_ref_01" and e["target"] != "n_ref_01"
    ]
    prune_non_contributing_nodes(graph)
    assert any(n["id"] == "n_ref_01" for n in graph["nodes"])


@pytest.mark.asyncio
async def test_classify_reference_images_marks_a_product_as_an_object(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jiuwenswarm.server.runtime.designer.user_references import classify_reference_images

    image = tmp_path / "01_reference.jpg"
    image.write_bytes(_png_bytes())
    seen: dict[str, object] = {}

    async def fake_call_model_tool(**kwargs):
        seen["images"] = kwargs.get("images")
        seen["system"] = kwargs.get("system")
        seen["prompt"] = kwargs.get("prompt")
        return {
            "ok": True,
            "fallback": False,
            "text": '{"reference_reads":[{"slot":1,"subject":"object","character_id":"","setting_id":""}]}',
        }

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.model_tools.call_model_tool",
        fake_call_model_tool,
    )
    reads = await classify_reference_images(
        "给我的产品做30秒中文koc视频",
        [{"kind": "image", "path": str(image), "filename": "01_reference.jpg"}],
        {"characters": [{"id": "char_1", "name": "Lead", "match_terms": ["xiaoyue"]}],
         "scenes": [{"id": "set_1", "name": "Dining"}]},
    )
    assert reads[0]["subject"] == "object"
    assert seen["images"] == [str(image)]
    system = str(seen.get("system") or "")
    assert "suppress_companions" not in system
    assert "solo_subject" not in system
    assert "keyframe_complete" not in system
    body = str(seen.get("prompt") or "")
    assert "char_1" in body
    assert "set_1" in body


@pytest.mark.asyncio
async def test_classify_reference_images_carries_enriched_intent_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jiuwenswarm.server.runtime.designer.user_references import classify_reference_images

    image = tmp_path / "room.png"
    image.write_bytes(_png_bytes())

    async def fake_call_model_tool(**kwargs):
        return {
            "ok": True,
            "fallback": False,
            "text": (
                '{"reference_reads":[{"slot":1,"subject":"scene",'
                '"roles":["scene_source"],"binding":"verbatim","set_lock":true,'
                '"style_authority":true,"medium":"anime","look":"cel-shaded",'
                '"palette":"pastel","rationale":"locked cartoon set"}]}'
            ),
        }

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.model_tools.call_model_tool",
        fake_call_model_tool,
    )
    reads = await classify_reference_images(
        "advertise the dinner setting attached",
        [{"kind": "image", "path": str(image), "filename": "room.png"}],
    )
    assert reads[0]["set_lock"] is True
    assert reads[0]["style_authority"] is True
    assert reads[0]["bindings"]["scene_source"] == "verbatim"
    assert reads[0]["style_read"]["medium"] == "anime"


def test_wiped_product_edge_is_restored_as_a_clip_input(tmp_path: Path) -> None:
    import json

    from jiuwenswarm.server.runtime.designer.handlers.clip import collect_clip_reference_images
    from jiuwenswarm.server.runtime.designer.handlers.types import NodeExecutionContext
    from jiuwenswarm.server.runtime.designer.user_references import reapply_user_reference_routes

    image = tmp_path / "01_reference.jpg"
    image.write_bytes(_png_bytes())
    refs = normalize_user_references(
        [{"kind": "image", "path": str(image), "filename": "01_reference.jpg", "mime_type": "image/jpeg"}],
        dest_dir=tmp_path / "refs",
    )
    refs[0]["subject"] = "object"
    routed = attach_user_references_to_graph(
        {
            "nodes": [
                {"id": "n_brief", "type": "text", "config": {"role": "brief"}},
                {"id": "n_clip_1", "type": "video", "config": {"role": "clip", "shot_index": 1}},
            ],
            "edges": [],
            "metadata": {},
        },
        refs,
    )
    wiped = json.loads(json.dumps(routed))
    wiped["edges"] = [edge for edge in wiped["edges"] if edge["target"] != "n_clip_1"]
    wiped["metadata"]["user_references"][0].pop("subject", None)
    restored = reapply_user_reference_routes(wiped, routed)
    wired = {(edge["source"], edge["target"]) for edge in restored["edges"]}
    assert ("n_ref_01", "n_clip_1") in wired
    clip = next(node for node in restored["nodes"] if node["id"] == "n_clip_1")
    paths = collect_clip_reference_images(
        NodeExecutionContext(graph=restored, run_id="run_product", node_id="n_clip_1", run={}),
        1,
        node=clip,
    )
    assert any(path.name == "01_reference.jpg" for path in paths)


def test_product_label_counts_as_an_object_route() -> None:
    from jiuwenswarm.server.runtime.designer.script_analysis import _reference_reads

    reads = _reference_reads(
        {"reference_reads": [{"slot": 1, "subject": "产品"}]}
    )
    assert reads[0]["slot"] == 1
    assert reads[0]["subject"] == "object"
    assert reads[0]["character_id"] == ""
    assert reads[0]["setting_id"] == ""
    assert "product_hero" in reads[0]["roles"]


@pytest.mark.asyncio
async def test_reference_node_returns_the_uploaded_file(tmp_path: Path) -> None:
    from jiuwenswarm.server.runtime.designer.handlers.media_nodes import (
        UserReferenceNodeHandler,
    )
    from jiuwenswarm.server.runtime.designer.handlers.types import NodeExecutionContext

    image = tmp_path / "hero.png"
    image.write_bytes(_png_bytes())
    refs = normalize_user_references(
        [{"kind": "image", "path": str(image), "filename": "hero.png", "mime_type": "image/png"}],
        dest_dir=tmp_path / "refs",
    )
    graph = attach_user_references_to_graph(
        {"nodes": [{"id": "n_brief", "type": "text", "config": {"role": "brief"}}], "metadata": {}},
        refs,
    )
    node = next(n for n in graph["nodes"] if str(n["id"]).startswith("n_ref_"))
    result = await UserReferenceNodeHandler().execute(
        node,
        NodeExecutionContext(graph=graph, run_id="run_ref", node_id=node["id"], run={}),
    )
    assert result.output_ref["uri"] == Path(refs[0]["path"]).resolve().as_uri()


@pytest.mark.asyncio
async def test_character_card_edits_the_reference_instead_of_redesigning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jiuwenswarm.server.runtime.designer.handlers import image_nodes
    from jiuwenswarm.server.runtime.designer.handlers.types import NodeExecutionContext

    image = tmp_path / "hero.png"
    image.write_bytes(_png_bytes())
    refs = normalize_user_references(
        [{"kind": "image", "path": str(image), "filename": "hero.png", "mime_type": "image/png"}],
        dest_dir=tmp_path / "refs",
    )
    graph = attach_user_references_to_graph(
        {
            "nodes": [
                {"id": "n_brief", "type": "text", "config": {"role": "brief"}},
                {
                    "id": "n_character",
                    "type": "image",
                    "label": "Character",
                    "config": {
                        "role": "character_design",
                        "prompt": "a tall knight in red armor",
                    },
                },
            ],
            "edges": [],
            "metadata": {
                "script_analysis": {
                    "reference_reads": [{"slot": 1, "subject": "character"}]
                }
            },
        },
        refs,
    )
    seen: dict[str, object] = {}

    async def fake_image(prompt, size="1K", reference_images=None, max_tries=2, **kwargs):
        seen["prompt"] = prompt
        seen["refs"] = list(reference_images or [])
        out = tmp_path / "out.png"
        out.write_bytes(_png_bytes())
        return {"image_path": str(out)}

    monkeypatch.setattr(image_nodes.handler_io, "generate_designer_image", fake_image)
    node = next(n for n in graph["nodes"] if n["id"] == "n_character")
    await image_nodes.CharacterDesignNodeHandler().execute(
        node,
        NodeExecutionContext(graph=graph, run_id="run_char", node_id="n_character", run={}),
    )
    prompt = str(seen["prompt"])
    assert [Path(p).name for p in seen["refs"]] == [Path(refs[0]["path"]).name]
    assert "hero.png" in prompt
    assert "red armor" in prompt


def test_style_only_reference_does_not_lock_character_identity(tmp_path: Path) -> None:
    from jiuwenswarm.server.runtime.designer.user_references import (
        identity_reference_image_paths,
    )

    image = tmp_path / "palette.png"
    image.write_bytes(_png_bytes())
    refs = normalize_user_references(
        [
            {
                "kind": "image",
                "role": "style",
                "path": str(image),
                "filename": "palette.png",
                "mime_type": "image/png",
            }
        ],
        dest_dir=tmp_path / "refs",
    )
    graph = attach_user_references_to_graph(
        {"nodes": [{"id": "n_brief", "type": "text", "config": {"role": "brief"}}], "metadata": {}},
        refs,
    )
    assert user_reference_image_paths(graph)
    assert identity_reference_image_paths(graph) == []


def test_vision_user_content_keeps_original_image(tmp_path: Path) -> None:
    from jiuwenswarm.server.runtime.designer.model_tools import vision_user_content

    image = tmp_path / "hero.png"
    image.write_bytes(_png_bytes())
    content = vision_user_content(
        "Slots: image 1 = hero.png. Do not summarize the picture.",
        [str(image)],
    )
    assert isinstance(content, list)
    assert content[0]["type"] == "text"
    assert "hero.png" in content[0]["text"]
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")


@pytest.mark.asyncio
async def test_analyze_creative_brief_sends_reference_images(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jiuwenswarm.server.runtime.designer import script_analysis as analysis

    image = tmp_path / "hero.png"
    image.write_bytes(_png_bytes())
    seen: dict[str, object] = {}

    async def fake_call_model_tool(**kwargs):
        seen["images"] = kwargs.get("images")
        seen["prompt"] = kwargs.get("prompt")
        return {
            "ok": True,
            "fallback": False,
            "text": (
                '{"characters":[{"id":"char_1","name":"Hero","description":"from image 1"}],'
                '"scenes":[{"id":"scene_1","name":"Street","description":"urban"}],'
                '"shots":[{"shot_index":1,"title":"Walk","action":"walks","camera":"medium",'
                '"character_ids":["char_1"],"keyframe_prompt":"Hero walks","timeline":"0.0-5.0s"}],'
                '"cast_layout":"single","target_shot_count":1,"prefer_combined_cast":false,'
                '"prefer_split_cast":false,'
                '"audio":{"policy":"optional_music","include_speech":false,"include_music":true,"notes":""},'
                '"reference_reads":[{"slot":1,"subject":"character","character_id":"char_1","setting_id":""}],'
                '"summary":"hero walks"}'
            ),
        }

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.model_tools.call_model_tool",
        fake_call_model_tool,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.skills_loader.load_orchestration_skill",
        lambda name: "",
    )
    result = await analysis.analyze_creative_brief(
        "按参考图做一个短片",
        timeout_sec=5.0,
        reference_images=[str(image)],
    )
    assert seen["images"] == [str(image)]
    assert result.get("source") == "llm"
    names = [item.get("name") for item in result.get("characters") or []]
    assert "Hero" in names
    reads = result.get("reference_reads") or []
    assert reads[0]["subject"] == "character"
    assert reads[0]["slot"] == 1


@pytest.mark.asyncio
async def test_clip_passes_user_video_as_file_not_first_frame(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jiuwenswarm.common.schema.designer_graph import (
        NODE_ROLE_CLIP,
        NODE_ROLE_FRAME,
        NODE_TYPE_IMAGE,
        NODE_TYPE_VIDEO,
    )
    from jiuwenswarm.server.runtime.designer.handlers.clip import (
        ClipNodeHandler,
        build_clip_prompt,
    )
    from jiuwenswarm.server.runtime.designer.handlers.common import file_output_ref
    from jiuwenswarm.server.runtime.designer.handlers.types import NodeExecutionContext

    frame = tmp_path / "keyframe.png"
    video = tmp_path / "ref.mp4"
    out = tmp_path / "clip.mp4"
    frame.write_bytes(_png_bytes())
    video.write_bytes(b"fake-mp4")
    out.write_bytes(b"out-mp4")
    refs = normalize_user_references(
        [{"kind": "video", "path": str(video), "filename": "ref.mp4", "mime_type": "video/mp4"}],
        dest_dir=tmp_path / "refs",
    )
    graph = {
        "nodes": [
            {
                "id": "n_frame",
                "type": NODE_TYPE_IMAGE,
                "label": "frame",
                "config": {"role": NODE_ROLE_FRAME, "shot_index": 1},
            },
            {
                "id": "n_clip",
                "type": NODE_TYPE_VIDEO,
                "label": "clip",
                "config": {"role": NODE_ROLE_CLIP, "shot_index": 1},
            },
        ],
        "metadata": {},
    }
    graph = attach_user_references_to_graph(graph, refs)
    prompt = build_clip_prompt(graph, graph["nodes"][-1])
    assert prompt  # R2V story-form; user video is a reference_file, not prompt text
    seen: dict[str, object] = {}

    async def fake_generate(
        prompt: str,
        save_dir: str | None = None,
        first_frame: str | None = None,
        reference_images: list[str] | None = None,
        reference_file: str | None = None,
        duration: int = 5,
        audio: bool | None = None,
        **kwargs,
    ) -> dict[str, str]:
        seen["first_frame"] = first_frame
        seen["reference_file"] = reference_file
        seen["force_reference_mode"] = kwargs.get("force_reference_mode")
        return {"video_path": str(out), "revised_prompt": prompt}

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.handlers.clip.generate_clip_video",
        fake_generate,
    )
    ctx = NodeExecutionContext(
        graph=graph,
        run_id="run_user_video",
        node_id="n_clip",
        run={
            "node_states": {
                "n_frame": {
                    "status": "completed",
                    "output_ref": file_output_ref(
                        frame, kind=NODE_TYPE_IMAGE, mime_type="image/png"
                    ),
                }
            }
        },
    )
    await ClipNodeHandler().execute(graph["nodes"][-1], ctx)
    assert seen.get("first_frame") in (None, "")
    assert seen.get("force_reference_mode") is True
    assert seen["reference_file"]
    assert Path(str(seen["reference_file"])).suffix == ".mp4"
    assert Path(str(seen["reference_file"])) != frame.resolve()


@pytest.mark.asyncio
async def test_music_handler_copies_user_audio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jiuwenswarm.common.schema.designer_graph import NODE_TYPE_AUDIO
    from jiuwenswarm.server.runtime.designer.handlers.audio_nodes import MusicNodeHandler
    from jiuwenswarm.server.runtime.designer.handlers.types import NodeExecutionContext

    audio = tmp_path / "theme.mp3"
    audio.write_bytes(b"user-audio-bytes")
    refs = normalize_user_references(
        [{"kind": "audio", "path": str(audio), "filename": "theme.mp3", "mime_type": "audio/mpeg"}],
        dest_dir=tmp_path / "refs",
    )
    graph = attach_user_references_to_graph(
        {
            "nodes": [{"id": "n_music", "type": NODE_TYPE_AUDIO, "config": {"role": "music"}}],
            "metadata": {},
        },
        refs,
    )
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.handlers.audio_nodes.graph_workspace_dir",
        lambda _graph: workspace,
    )
    result = await MusicNodeHandler().execute(
        graph["nodes"][0],
        NodeExecutionContext(graph=graph, run_id="run_audio", node_id="n_music", run={}),
    )
    produced = next(path for path in workspace.iterdir() if path.suffix.lower() == ".mp3")
    assert produced.read_bytes() == b"user-audio-bytes"
    assert "user audio" in result.message


def test_analysis_prompt_reference_roster_is_not_a_shot() -> None:
    from jiuwenswarm.server.runtime.designer.script_analysis import heuristic_analysis
    from jiuwenswarm.server.runtime.designer.smart_graph import default_spatial_lock

    prompt = analysis_prompt_with_references(
        "Generate a 10-second video: a troop charges the Demon King's castle.",
        [{"filename": "castle-1.png", "kind": "image"}],
    )
    analysis = heuristic_analysis(prompt)
    actions = " ".join(str(shot.get("action") or "") for shot in analysis["shots"])
    assert "User attached" not in actions
    assert "REFERENCE_MEDIA" not in actions
    names = " ".join(str(item.get("name") or "") for item in analysis["characters"])
    assert "User attached" not in names
    assert "castle-1" not in names
    scenes = " ".join(str(item.get("name") or "") for item in analysis["scenes"])
    assert "User attached" not in scenes
    assert "castle-1" not in scenes
    lock = default_spatial_lock({"name": "Mountain pass", "description": "dusk ridges"})
    blob = " ".join(lock.values()).lower()
    assert "pulpit" not in blob
    assert "pew" not in blob
    assert "mountain pass" in blob

