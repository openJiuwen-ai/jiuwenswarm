# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

from jiuwenswarm.server.runtime.designer.handlers.clip import collect_clip_reference_images
from jiuwenswarm.server.runtime.designer.handlers.common import (
    apply_uploaded_outputs_to_run,
    node_output_image_paths,
)
from jiuwenswarm.server.runtime.designer.handlers.types import NodeExecutionContext
from jiuwenswarm.server.runtime.gateway_adapter.designer_adapter import (
    hydrate_graph_node_outputs,
)


def test_uploaded_image_replaces_the_generated_one(tmp_path) -> None:
    old = tmp_path / "generated.png"
    uploaded = tmp_path / "uploaded.png"
    old.write_bytes(b"old")
    uploaded.write_bytes(b"new")
    uploaded_ref = {
        "kind": "image",
        "uri": uploaded.resolve().as_uri(),
        "mime_type": "image/png",
    }
    graph = {
        "nodes": [
            {
                "id": "n_clip_1",
                "type": "image",
                "config": {"role": "frame", "user_replaced_output": True},
                "output_ref": uploaded_ref,
            }
        ]
    }
    run = {
        "node_states": {
            "n_clip_1": {
                "status": "completed",
                "output_ref": {
                    "kind": "image",
                    "uri": old.resolve().as_uri(),
                    "mime_type": "image/png",
                },
            }
        }
    }
    ctx = NodeExecutionContext(graph=graph, run_id="run_1", node_id="n_clip_1", run=run)

    assert node_output_image_paths(ctx, "n_clip_1") == [uploaded.resolve()]
    assert apply_uploaded_outputs_to_run(run, graph) is True
    assert run["node_states"]["n_clip_1"]["output_ref"]["uri"] == uploaded_ref["uri"]
    assert run["node_states"]["n_clip_1"]["status"] == "completed"

    hydrated = hydrate_graph_node_outputs(
        graph,
        {
            "node_states": {
                "n_clip_1": {
                    "output_ref": {
                        "kind": "image",
                        "uri": old.resolve().as_uri(),
                        "mime_type": "image/png",
                    }
                }
            }
        },
    )
    assert hydrated["nodes"][0]["output_ref"]["uri"] == uploaded_ref["uri"]


def test_upload_uri_wins_over_a_stale_generated_output(tmp_path) -> None:
    old = tmp_path / "generated.png"
    uploaded = tmp_path / "uploaded.jpg"
    old.write_bytes(b"old")
    uploaded.write_bytes(b"new")
    graph = {
        "nodes": [
            {
                "id": "n_character_1",
                "type": "image",
                "config": {
                    "role": "character",
                    "user_replaced_output": True,
                    "upload": {
                        "filename": "uploaded.jpg",
                        "mime_type": "image/jpeg",
                        "uri": uploaded.resolve().as_uri(),
                    },
                },
                "output_ref": {
                    "kind": "image",
                    "uri": old.resolve().as_uri(),
                    "mime_type": "image/png",
                },
            }
        ]
    }
    run = {
        "node_states": {
            "n_character_1": {
                "status": "completed",
                "output_ref": graph["nodes"][0]["output_ref"],
            }
        }
    }
    ctx = NodeExecutionContext(graph=graph, run_id="run_1", node_id="n_clip_1", run=run)

    assert node_output_image_paths(ctx, "n_character_1") == [uploaded.resolve()]
    assert apply_uploaded_outputs_to_run(run, graph) is True
    assert run["node_states"]["n_character_1"]["output_ref"]["uri"] == uploaded.resolve().as_uri()


def _replaced_upload_node(node_id: str, kind: str, uploaded, mime: str, stale) -> dict:
    return {
        "id": node_id,
        "type": kind,
        "config": {
            "role": kind,
            "interaction_mode": "upload",
            "user_replaced_output": True,
            "upload": {"filename": uploaded.name, "mime_type": mime, "uri": uploaded.resolve().as_uri()},
        },
        "output_ref": {"kind": kind, "uri": stale.resolve().as_uri(), "mime_type": mime},
    }


def test_replaced_audio_and_video_uploads_reach_the_run(tmp_path) -> None:
    old_audio = tmp_path / "first.mp3"
    new_audio = tmp_path / "second.mp3"
    old_video = tmp_path / "first.mp4"
    new_video = tmp_path / "second.mp4"
    for path in (old_audio, new_audio, old_video, new_video):
        path.write_bytes(b"media")
    graph = {
        "nodes": [
            _replaced_upload_node("n_audio_a", "audio", new_audio, "audio/mpeg", old_audio),
            _replaced_upload_node("n_video_a", "video", new_video, "video/mp4", old_video),
        ]
    }
    run = {
        "node_states": {
            "n_audio_a": {
                "status": "completed",
                "output_ref": {"kind": "audio", "uri": old_audio.resolve().as_uri(), "mime_type": "audio/mpeg"},
            },
            "n_video_a": {
                "status": "completed",
                "output_ref": {"kind": "video", "uri": old_video.resolve().as_uri(), "mime_type": "video/mp4"},
            },
        }
    }

    assert apply_uploaded_outputs_to_run(run, graph) is True
    audio_state = run["node_states"]["n_audio_a"]
    video_state = run["node_states"]["n_video_a"]
    assert audio_state["output_ref"] == {
        "kind": "audio",
        "uri": new_audio.resolve().as_uri(),
        "mime_type": "audio/mpeg",
    }
    assert audio_state["output_refs"] == [audio_state["output_ref"]]
    assert video_state["output_ref"]["kind"] == "video"
    assert video_state["output_ref"]["uri"] == new_video.resolve().as_uri()
    assert apply_uploaded_outputs_to_run(run, graph) is False


def test_missing_replacement_file_leaves_the_run_alone(tmp_path) -> None:
    old_audio = tmp_path / "first.mp3"
    old_audio.write_bytes(b"media")
    gone = tmp_path / "gone.mp3"
    old_ref = {"kind": "audio", "uri": old_audio.resolve().as_uri(), "mime_type": "audio/mpeg"}
    node = _replaced_upload_node("n_audio_a", "audio", gone, "audio/mpeg", gone)
    run = {"node_states": {"n_audio_a": {"status": "completed", "output_ref": dict(old_ref)}}}

    assert apply_uploaded_outputs_to_run(run, {"nodes": [node]}) is False
    assert run["node_states"]["n_audio_a"]["output_ref"] == old_ref


def test_compose_mixes_the_replaced_audio_not_the_stale_run_file(tmp_path) -> None:
    from jiuwenswarm.server.runtime.designer.handlers.compose import _collect_role_audio_paths

    old_audio = tmp_path / "first.mp3"
    new_audio = tmp_path / "second.mp3"
    old_audio.write_bytes(b"a" * 128)
    new_audio.write_bytes(b"b" * 128)
    stale_ref = {"kind": "audio", "uri": old_audio.resolve().as_uri(), "mime_type": "audio/mpeg"}
    graph = {
        "nodes": [
            _replaced_upload_node("n_audio_a", "audio", new_audio, "audio/mpeg", old_audio),
            {"id": "n_compose", "type": "video", "config": {"role": "compose"}},
        ],
        "edges": [{"id": "e_a", "source": "n_audio_a", "target": "n_compose", "kind": "data"}],
    }
    stale_run = {"node_states": {"n_audio_a": {"status": "completed", "output_ref": stale_ref}}}
    roles = {"speech", "music", "audio"}

    ctx = NodeExecutionContext(graph=graph, run_id="run_1", node_id="n_compose", run=stale_run)
    assert _collect_role_audio_paths(ctx, roles) == [new_audio.resolve()]

    never_ran = NodeExecutionContext(graph=graph, run_id="run_1", node_id="n_compose", run={"node_states": {}})
    assert _collect_role_audio_paths(never_ran, roles) == [new_audio.resolve()]

    graph["edges"] = []
    unwired = NodeExecutionContext(graph=graph, run_id="run_1", node_id="n_compose", run=stale_run)
    assert _collect_role_audio_paths(unwired, roles) == []


def test_wired_upload_videos_follow_the_shots_in_wiring_order(tmp_path) -> None:
    from jiuwenswarm.server.runtime.designer.handlers.compose import collect_clip_video_paths

    shot = tmp_path / "shot1.mp4"
    stale = tmp_path / "stale.mp4"
    tail_b = tmp_path / "tail_b.mp4"
    tail_a = tmp_path / "tail_a.mp4"
    loose = tmp_path / "loose.mp4"
    for path in (shot, stale, tail_b, tail_a, loose):
        path.write_bytes(b"v" * 600)

    def video_ref(path) -> dict:
        return {"kind": "video", "uri": path.resolve().as_uri(), "mime_type": "video/mp4"}

    graph = {
        "nodes": [
            {"id": "n_clip_1", "type": "video", "config": {"role": "clip", "clip_index": 1}},
            _replaced_upload_node("n_video_a", "video", tail_a, "video/mp4", stale),
            _replaced_upload_node("n_video_b", "video", tail_b, "video/mp4", tail_b),
            {"id": "n_video_loose", "type": "video", "output_ref": video_ref(loose)},
            {"id": "n_compose", "type": "video", "config": {"role": "compose", "inputs": ["n_clip_1"]}},
        ],
        "edges": [
            {"id": "e_clip", "source": "n_clip_1", "target": "n_compose", "kind": "data"},
            {"id": "e_b", "source": "n_video_b", "target": "n_compose", "kind": "data"},
            {"id": "e_a", "source": "n_video_a", "target": "n_compose", "kind": "data"},
        ],
    }
    run = {
        "node_states": {
            "n_clip_1": {"status": "completed", "output_ref": video_ref(shot)},
            "n_video_a": {"status": "completed", "output_ref": video_ref(stale)},
        }
    }
    ctx = NodeExecutionContext(graph=graph, run_id="run_1", node_id="n_compose", run=run)

    assert collect_clip_video_paths(ctx) == [shot.resolve(), tail_b.resolve(), tail_a.resolve()]


def test_material_upload_is_the_clip_input(tmp_path) -> None:
    clip_image = tmp_path / "clip_image.png"
    material = tmp_path / "material.png"
    clip_image.write_bytes(b"kf")
    material.write_bytes(b"mat")
    graph = {
        "nodes": [
            {
                "id": "n_clip_1",
                "type": "image",
                "config": {"role": "clip", "clip_index": 1},
                "output_ref": {
                    "kind": "image",
                    "uri": clip_image.resolve().as_uri(),
                    "mime_type": "image/png",
                },
            },
            {
                "id": "n_clip_1",
                "type": "video",
                "config": {
                    "role": "clip",
                    "clip_index": 1,
                    "materials": [
                        {
                            "id": "mat_1",
                            "filename": "material.png",
                            "mime_type": "image/png",
                            "uri": material.resolve().as_uri(),
                        }
                    ],
                },
            },
        ]
    }
    run = {
        "node_states": {
            "n_clip_1": {
                "status": "completed",
                "output_ref": graph["nodes"][0]["output_ref"],
            }
        }
    }
    ctx = NodeExecutionContext(graph=graph, run_id="run_1", node_id="n_clip_1", run=run)
    clip = graph["nodes"][1]

    refs = collect_clip_reference_images(ctx, 1, clip)
    assert refs == [material.resolve()]
    assert clip_image.resolve() not in refs


def test_user_images_join_r2v_refs_without_replacing_them(tmp_path) -> None:
    character_spec = tmp_path / "character_spec.png"
    scene = tmp_path / "scene.png"
    first = tmp_path / "extra-a.png"
    second = tmp_path / "extra-b.png"
    for path in (character_spec, scene, first, second):
        path.write_bytes(b"img")
    graph = {
        "nodes": [
            {
                "id": "n_character_1",
                "type": "image",
                "config": {
                    "role": "character",
                    "character_ids": ["char_1"],
                },
                "output_ref": {
                    "kind": "image",
                    "uri": character_spec.resolve().as_uri(),
                    "mime_type": "image/png",
                },
            },
            {
                "id": "n_scene_1",
                "type": "image",
                "config": {"role": "scene", "setting_id": "set_1"},
                "output_ref": {
                    "kind": "image",
                    "uri": scene.resolve().as_uri(),
                    "mime_type": "image/png",
                },
            },
            {
                "id": "n_clip_1",
                "type": "video",
                "config": {
                    "role": "clip",
                    "clip_index": 1,
                    "scene_node_id": "n_scene_1",
                    "on_screen": ["char_1"],
                    "character_node_ids": ["n_character_1"],
                    "materials": [
                        {
                            "filename": "extra-a.png",
                            "mime_type": "image/png",
                            "uri": first.resolve().as_uri(),
                        },
                        {
                            "filename": "extra-b.png",
                            "mime_type": "image/png",
                            "uri": second.resolve().as_uri(),
                        },
                    ],
                },
            },
        ]
    }
    run = {
        "node_states": {
            "n_character_1": {
                "status": "completed",
                "output_ref": graph["nodes"][0]["output_ref"],
            },
            "n_scene_1": {
                "status": "completed",
                "output_ref": graph["nodes"][1]["output_ref"],
            },
        }
    }
    ctx = NodeExecutionContext(graph=graph, run_id="run_1", node_id="n_clip_1", run=run)

    assert collect_clip_reference_images(ctx, 1, graph["nodes"][2]) == [
        character_spec.resolve(),
        first.resolve(),
        second.resolve(),
        scene.resolve(),
    ]


def test_upstream_images_follow_edges_and_keep_old_attachments(tmp_path) -> None:
    from jiuwenswarm.server.runtime.designer.handlers.common import predecessor_outputs
    from jiuwenswarm.server.runtime.designer.handlers.media_nodes import _upstream_images

    wired = [tmp_path / "edge_a.png", tmp_path / "edge_b.png"]
    declared = tmp_path / "config_only.png"
    extra = tmp_path / "attached.png"
    replaced = tmp_path / "replaced.png"
    generated = tmp_path / "generated.png"
    saved = tmp_path / "saved.png"
    for path in [*wired, declared, extra, replaced, generated, saved]:
        path.write_bytes(b"img")
    generated_ref = {
        "kind": "image",
        "uri": generated.resolve().as_uri(),
        "mime_type": "image/png",
    }
    replaced_ref = {
        "kind": "image",
        "uri": replaced.resolve().as_uri(),
        "mime_type": "image/png",
    }
    nodes = []
    states = {}
    edges = []
    for index, path in enumerate(wired, start=1):
        node_id = f"n_src_{index}"
        ref = {
            "kind": "image",
            "uri": path.resolve().as_uri(),
            "mime_type": "image/png",
        }
        nodes.append({"id": node_id, "type": "image", "label": node_id, "output_ref": ref})
        states[node_id] = {"status": "completed", "output_ref": ref}
        edges.append({"id": f"e_{index}", "source": node_id, "target": "n_clip_1", "kind": "data"})
    nodes.append(
        {
            "id": "n_src_3",
            "type": "image",
            "label": "config only",
            "output_ref": {
                "kind": "image",
                "uri": declared.resolve().as_uri(),
                "mime_type": "image/png",
            },
        }
    )
    nodes.append(
        {
            "id": "n_replaced",
            "type": "image",
            "label": "Replaced",
            "config": {
                "user_replaced_output": True,
                "upload": {
                    "filename": "replaced.png",
                    "mime_type": "image/png",
                    "uri": replaced.resolve().as_uri(),
                },
            },
            "output_ref": replaced_ref,
        }
    )
    states["n_replaced"] = {"status": "completed", "output_ref": generated_ref}
    edges.append({"id": "e_replaced", "source": "n_replaced", "target": "n_clip_1", "kind": "data"})
    nodes.append(
        {
            "id": "n_saved",
            "type": "image",
            "label": "Saved",
            "output_ref": {
                "kind": "image",
                "uri": saved.resolve().as_uri(),
                "mime_type": "image/png",
            },
        }
    )
    edges.append({"id": "e_saved", "source": "n_saved", "target": "n_clip_1", "kind": "data"})
    clip = {
        "id": "n_clip_1",
        "type": "video",
        "config": {
            "role": "clip",
            "inputs": ["n_src_1", "n_src_2", "n_src_3"],
            "materials": [
                {
                    "filename": "attached.png",
                    "mime_type": "image/png",
                    "uri": extra.resolve().as_uri(),
                }
            ],
        },
    }
    nodes.append(clip)
    ctx = NodeExecutionContext(
        graph={"nodes": nodes, "edges": edges},
        run_id="run_1",
        node_id="n_clip_1",
        run={"node_states": states},
    )

    refs = _upstream_images(ctx, clip)
    assert refs == [
        extra.resolve(),
        wired[0].resolve(),
        wired[1].resolve(),
        replaced.resolve(),
        saved.resolve(),
    ]
    assert declared.resolve() not in refs
    assert generated.resolve() not in refs
    bare = NodeExecutionContext(
        graph={"nodes": nodes, "edges": []},
        run_id="run_1",
        node_id="n_clip_1",
        run={"node_states": states},
    )
    assert predecessor_outputs(bare, clip) is None
    assert _upstream_images(bare, clip) == [extra.resolve()]


def test_wired_user_image_joins_clip_refs_before_the_scene(tmp_path) -> None:
    from jiuwenswarm.server.runtime.designer.handlers.clip import build_clip_prompt

    character_spec = tmp_path / "character_spec.png"
    scene = tmp_path / "scene.png"
    officer = tmp_path / "officer.png"
    unconnected = tmp_path / "unconnected.png"
    for path in (character_spec, scene, officer, unconnected):
        path.write_bytes(b"img")
    officer_ref = {
        "kind": "image",
        "uri": officer.resolve().as_uri(),
        "mime_type": "image/png",
    }
    graph = {
        "nodes": [
            {
                "id": "n_character_1",
                "type": "image",
                "config": {"role": "character", "character_ids": ["char_1"]},
                "output_ref": {
                    "kind": "image",
                    "uri": character_spec.resolve().as_uri(),
                    "mime_type": "image/png",
                },
            },
            {
                "id": "n_scene_1",
                "type": "image",
                "config": {"role": "scene", "setting_id": "set_1"},
                "output_ref": {
                    "kind": "image",
                    "uri": scene.resolve().as_uri(),
                    "mime_type": "image/png",
                },
            },
            {
                "id": "n_character_2",
                "type": "image",
                "label": "Character 2",
                "config": {"role": "character", "character_ids": ["char_2"]},
                "output_ref": {
                    "kind": "image",
                    "uri": unconnected.resolve().as_uri(),
                    "mime_type": "image/png",
                },
            },
            {
                "id": "n_image_5",
                "type": "image",
                "label": "Image 5",
                "config": {"role": "image", "user_added": True, "user_replaced_output": True},
                "output_ref": officer_ref,
            },
            {
                "id": "n_clip_2",
                "type": "video",
                "config": {
                    "role": "clip",
                    "clip_index": 2,
                    "scene_node_id": "n_scene_1",
                    "on_screen": ["char_1"],
                    "character_node_ids": ["n_character_1"],
                    "clip_action": "the officer enters",
                },
            },
        ],
        "edges": [
            {"id": "e_char", "source": "n_character_1", "target": "n_clip_2", "kind": "data"},
            {"id": "e_scene", "source": "n_scene_1", "target": "n_clip_2", "kind": "data"},
            {"id": "e_image", "source": "n_image_5", "target": "n_clip_2", "kind": "data"},
        ],
    }
    ctx = NodeExecutionContext(
        graph=graph,
        run_id="run_1",
        node_id="n_clip_2",
        run={
            "node_states": {
                "n_character_1": {
                    "status": "completed",
                    "output_ref": graph["nodes"][0]["output_ref"],
                },
                "n_scene_1": {
                    "status": "completed",
                    "output_ref": graph["nodes"][1]["output_ref"],
                },
                "n_image_5": {"status": "completed", "output_ref": officer_ref},
            }
        },
    )

    refs = collect_clip_reference_images(ctx, 2, graph["nodes"][4])
    assert refs == [character_spec.resolve(), officer.resolve(), scene.resolve()]
    assert unconnected.resolve() not in refs
    prompt = build_clip_prompt(graph, graph["nodes"][4], ctx)
    assert "Image 5" in prompt
    assert "officer.png" in prompt


def test_attach_order_names_every_edge_input(tmp_path) -> None:
    from jiuwenswarm.server.runtime.designer.handlers.clip import attach_order_clause

    character_spec = tmp_path / "character_spec.png"
    officer = tmp_path / "officer.png"
    scene = tmp_path / "scene.png"
    for path in (character_spec, officer, scene):
        path.write_bytes(b"img")
    flow = [
        ("character", "Character 1", character_spec),
        ("image", "Image 5", officer),
        ("scene", "Scene 1", scene),
    ]
    clause = attach_order_clause([character_spec, officer, scene], flow)
    assert "Image 1 = Character 1 (character_spec.png)" in clause
    assert "Image 2 = Image 5 (officer.png)" in clause
    assert "Image 3 = Scene 1 (scene.png)" in clause
    assert "canvas edges" in clause


def test_edge_flow_carries_text_and_video(tmp_path) -> None:
    from jiuwenswarm.server.runtime.designer.handlers.clip import (
        connected_payload_clause,
        edge_text_inputs,
        edge_video_inputs,
    )

    note = tmp_path / "note.md"
    note.write_text("the officer wears a red coat", encoding="utf-8")
    film = tmp_path / "motion.mp4"
    film.write_bytes(b"vid")
    graph = {
        "nodes": [
            {
                "id": "n_text",
                "type": "text",
                "label": "Note",
                "output_ref": {"kind": "text", "uri": note.resolve().as_uri(), "mime_type": "text/markdown"},
            },
            {
                "id": "n_video",
                "type": "video",
                "label": "Motion",
                "output_ref": {"kind": "video", "uri": film.resolve().as_uri(), "mime_type": "video/mp4"},
            },
            {"id": "n_clip", "type": "video", "config": {"role": "clip"}},
        ],
        "edges": [
            {"id": "e_text", "source": "n_text", "target": "n_clip", "kind": "data"},
            {"id": "e_video", "source": "n_video", "target": "n_clip", "kind": "data"},
        ],
    }
    ctx = NodeExecutionContext(
        graph=graph,
        run_id="run_1",
        node_id="n_clip",
        run={
            "node_states": {
                "n_text": {"status": "completed", "output_ref": graph["nodes"][0]["output_ref"]},
                "n_video": {"status": "completed", "output_ref": graph["nodes"][1]["output_ref"]},
            }
        },
    )
    clip = graph["nodes"][2]
    texts = edge_text_inputs(ctx, clip)
    videos = edge_video_inputs(ctx, clip)
    assert texts == [("Note", "the officer wears a red coat")]
    assert videos == [("Motion", film.resolve())]
    clause = connected_payload_clause(texts, videos)
    assert "Connected text inputs" in clause
    assert "red coat" in clause
    assert "motion.mp4" in clause


def test_node_agent_query_includes_live_canvas_edits() -> None:
    from jiuwenswarm.server.runtime.designer.node_agent import build_node_user_query

    graph = {
        "nodes": [
            {"id": "n_clip", "type": "video", "label": "Clip 2", "config": {"role": "clip"}},
        ],
        "edges": [{"id": "e1", "source": "n_image_5", "target": "n_clip", "kind": "data"}],
        "metadata": {
            "user_canvas_edits": [
                {"op": "connect", "node_id": "n_image_5", "peer_id": "n_clip"},
            ]
        },
    }
    ctx = NodeExecutionContext(
        graph=graph,
        run_id="run_1",
        node_id="n_clip",
        run={"node_states": {}},
    )
    query = build_node_user_query(graph["nodes"][0], ctx)
    assert '"op": "connect"' in query
    assert "n_image_5" in query
    assert "Disconnected nodes are not inputs" in query


def test_reload_graph_inplace_picks_up_saved_canvas(monkeypatch) -> None:
    from jiuwenswarm.server.runtime.designer.graph_store import (
        DesignerGraphStore,
        reload_graph_inplace,
    )

    stale = {"graph_id": "g1", "nodes": [], "edges": []}
    fresh = {
        "graph_id": "g1",
        "nodes": [{"id": "n_image_5"}],
        "edges": [{"source": "n_image_5", "target": "n_clip"}],
    }
    monkeypatch.setattr(DesignerGraphStore, "get_graph", lambda self, graph_id: dict(fresh))
    assert reload_graph_inplace(stale) is True
    assert stale["edges"][0]["target"] == "n_clip"
