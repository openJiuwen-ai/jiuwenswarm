# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import pytest


@pytest.mark.asyncio
async def test_compose_handler_refuses_while_clip_still_running(
    tmp_path, monkeypatch
):
    """ffmpeg must not assemble a 0:00 film while clips are still generating."""
    from jiuwenswarm.common.schema.designer_graph import (
        NODE_ROLE_CLIP,
        NODE_ROLE_COMPOSE,
        NODE_STATUS_COMPLETED,
        NODE_STATUS_RUNNING,
        NODE_TYPE_VIDEO,
        SCHEMA_VERSION,
        normalize_execution_graph,
    )
    from jiuwenswarm.server.runtime.designer.handlers.common import file_output_ref
    from jiuwenswarm.server.runtime.designer.handlers.compose import ComposeNodeHandler
    from jiuwenswarm.server.runtime.designer.handlers.types import NodeExecutionContext

    clip1 = tmp_path / "shot1.mp4"
    clip1.write_bytes(b"m" * 600)
    graph = normalize_execution_graph(
        {
            "schema_version": SCHEMA_VERSION,
            "graph_id": "graph_compose_wait",
            "project_id": "proj_compose_wait",
            "title": "成片",
            "nodes": [
                {
                    "id": "n_clip_1",
                    "type": NODE_TYPE_VIDEO,
                    "config": {"role": NODE_ROLE_CLIP, "clip_index": 1},
                },
                {
                    "id": "n_clip_2",
                    "type": NODE_TYPE_VIDEO,
                    "config": {"role": NODE_ROLE_CLIP, "clip_index": 2},
                },
                {
                    "id": "n_compose",
                    "type": NODE_TYPE_VIDEO,
                    "config": {"role": NODE_ROLE_COMPOSE, "inputs": ["n_clip_1", "n_clip_2"]},
                },
            ],
            "edges": [
                {"id": "e1", "source": "n_clip_1", "target": "n_compose"},
                {"id": "e2", "source": "n_clip_2", "target": "n_compose"},
            ],
        }
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.handlers.common.get_agent_workspace_dir",
        lambda: tmp_path,
    )

    async def _fast_sleep(*_a, **_k):
        return None

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.handlers.compose.asyncio.sleep",
        _fast_sleep,
    )

    with pytest.raises(RuntimeError, match="waiting for all clips|clip n_clip_2"):
        await ComposeNodeHandler().execute(
            graph["nodes"][-1],
            NodeExecutionContext(
                graph=graph,
                run_id="run_compose_wait",
                node_id="n_compose",
                run={
                    "node_states": {
                        "n_clip_1": {
                            "status": NODE_STATUS_COMPLETED,
                            "output_ref": file_output_ref(
                                clip1, kind=NODE_TYPE_VIDEO, mime_type="video/mp4"
                            ),
                        },
                        "n_clip_2": {"status": NODE_STATUS_RUNNING},
                    }
                },
            ),
        )
