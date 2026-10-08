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


def test_uploaded_still_replaces_the_generated_one(tmp_path) -> None:
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
                "id": "n_frame_1",
                "type": "image",
                "config": {"role": "frame", "user_replaced_output": True},
                "output_ref": uploaded_ref,
            }
        ]
    }
    run = {
        "node_states": {
            "n_frame_1": {
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

    assert node_output_image_paths(ctx, "n_frame_1") == [uploaded.resolve()]
    assert apply_uploaded_outputs_to_run(run, graph) is True
    assert run["node_states"]["n_frame_1"]["output_ref"]["uri"] == uploaded_ref["uri"]
    assert run["node_states"]["n_frame_1"]["status"] == "completed"

    hydrated = hydrate_graph_node_outputs(
        graph,
        {
            "node_states": {
                "n_frame_1": {
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
                    "role": "character_design",
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


def test_material_upload_is_the_clip_input(tmp_path) -> None:
    keyframe = tmp_path / "keyframe.png"
    material = tmp_path / "material.png"
    keyframe.write_bytes(b"kf")
    material.write_bytes(b"mat")
    graph = {
        "nodes": [
            {
                "id": "n_frame_1",
                "type": "image",
                "config": {"role": "frame", "shot_index": 1},
                "output_ref": {
                    "kind": "image",
                    "uri": keyframe.resolve().as_uri(),
                    "mime_type": "image/png",
                },
            },
            {
                "id": "n_clip_1",
                "type": "video",
                "config": {
                    "role": "clip",
                    "shot_index": 1,
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
            "n_frame_1": {
                "status": "completed",
                "output_ref": graph["nodes"][0]["output_ref"],
            }
        }
    }
    ctx = NodeExecutionContext(graph=graph, run_id="run_1", node_id="n_clip_1", run=run)
    clip = graph["nodes"][1]

    refs = collect_clip_reference_images(ctx, 1, clip)
    assert refs == [material.resolve()]
    assert keyframe.resolve() not in refs


def test_user_stills_join_r2v_refs_without_replacing_them(tmp_path) -> None:
    solo = tmp_path / "solo.png"
    scene = tmp_path / "scene.png"
    first = tmp_path / "extra-a.png"
    second = tmp_path / "extra-b.png"
    for path in (solo, scene, first, second):
        path.write_bytes(b"img")
    graph = {
        "nodes": [
            {
                "id": "n_character_1",
                "type": "image",
                "config": {
                    "role": "character_design",
                    "character_ids": ["char_1"],
                },
                "output_ref": {
                    "kind": "image",
                    "uri": solo.resolve().as_uri(),
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
                    "shot_index": 1,
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
        solo.resolve(),
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

    solo = tmp_path / "solo.png"
    scene = tmp_path / "scene.png"
    officer = tmp_path / "officer.png"
    unconnected = tmp_path / "unconnected.png"
    for path in (solo, scene, officer, unconnected):
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
                "config": {"role": "character_design", "character_ids": ["char_1"]},
                "output_ref": {
                    "kind": "image",
                    "uri": solo.resolve().as_uri(),
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
                "config": {"role": "character_design", "character_ids": ["char_2"]},
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
                    "shot_index": 2,
                    "scene_node_id": "n_scene_1",
                    "on_screen": ["char_1"],
                    "character_node_ids": ["n_character_1"],
                    "shot_action": "the officer enters",
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
    assert refs == [solo.resolve(), officer.resolve(), scene.resolve()]
    assert unconnected.resolve() not in refs
    prompt = build_clip_prompt(graph, graph["nodes"][4], ctx)
    assert "Image 5" in prompt
    assert "officer.png" in prompt


def test_attach_order_names_every_edge_input(tmp_path) -> None:
    from jiuwenswarm.server.runtime.designer.handlers.clip import attach_order_clause

    solo = tmp_path / "solo.png"
    officer = tmp_path / "officer.png"
    scene = tmp_path / "scene.png"
    for path in (solo, officer, scene):
        path.write_bytes(b"img")
    flow = [
        ("character_design", "Character 1", solo),
        ("image", "Image 5", officer),
        ("scene", "Scene 1", scene),
    ]
    clause = attach_order_clause([solo, officer, scene], flow)
    assert "Image 1 = Character 1 (solo.png)" in clause
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
