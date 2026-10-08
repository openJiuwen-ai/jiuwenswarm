# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from jiuwenswarm.common.schema.designer_graph import (
    compose_required_predecessor_ids,
    is_compose_sink_node,
    is_soft_artifact_dependency,
)
from jiuwenswarm.server.runtime.designer.pipeline.shot_staging_lock import (
    build_action_lock,
    build_positioning_lock,
    build_relationship_lock,
    enrich_shot_staging,
    staging_lock_clause,
)


def test_positioning_action_relationship_locks_from_shot():
    characters = [
        {"id": "char_1", "name": "Alice"},
        {"id": "char_2", "name": "Bob"},
    ]
    shot = {
        "shot_index": 2,
        "setting_id": "set_1",
        "camera": "medium front view",
        "view_key": "front",
        "on_screen": ["char_1", "char_2"],
        "cast_actions": {
            "char_1": "standing, holding phone, speaking",
            "char_2": "sitting across the table, smiling",
        },
        "speech_line": "Shall we order?",
        "speaker": "char_1",
        "looks_at": {"char_1": "char_2", "char_2": "char_1"},
        "blocking": {
            "landmark": "candlelit table",
            "positions": [
                {
                    "character_id": "char_1",
                    "name": "Alice",
                    "zone": "left",
                    "facing": "toward Bob",
                    "pose": "standing",
                    "near": "Bob",
                },
                {
                    "character_id": "char_2",
                    "name": "Bob",
                    "zone": "right",
                    "facing": "toward Alice",
                    "pose": "sitting",
                },
            ],
        },
    }
    pos = build_positioning_lock(shot, characters)
    act = build_action_lock(shot, characters)
    rel = build_relationship_lock(shot, characters)
    assert "camera=medium front view" in pos
    assert "Alice@zone=left" in pos
    assert "standing" in act and "sitting" in act
    assert "looks_at" in rel
    assert "talks_to" in rel or "speaks" in rel
    assert "next_to" in rel or "adjacent" in rel

    locks = enrich_shot_staging(shot, characters)
    clause = staging_lock_clause(
        positioning_lock=locks["positioning_lock"],
        action_lock=locks["action_lock"],
        relationship_lock=locks["relationship_lock"],
        shot_index=2,
        setting_id="set_1",
        for_clip=True,
    )
    assert "STAGING LOCK" in clause
    assert "POSITIONING" in clause
    assert "ACTION/POSTURE" in clause
    assert "RELATIONSHIPS" in clause
    assert "STAGING PRIORITY" in clause
    assert "EQUAL to clothing" in clause


def test_concurrency_soft_clip_hard_compose_unchanged():
    graph = {
        "nodes": [
            {"id": "n_clip_1", "type": "video", "config": {"role": "clip", "shot_index": 1}},
            {
                "id": "n_clip_2",
                "type": "video",
                "config": {
                    "role": "clip",
                    "shot_index": 2,
                    "previous_clip_handoff_ready": True,
                    "previous_clip_action": "prior beat",
                },
            },
            {"id": "n_compose", "type": "video", "config": {"role": "compose"}},
            {"id": "n_speech", "type": "audio", "config": {"role": "speech"}},
        ],
        "edges": [
            {"source": "n_clip_1", "target": "n_clip_2"},
            {"source": "n_clip_1", "target": "n_compose"},
            {"source": "n_clip_2", "target": "n_compose"},
            {"source": "n_speech", "target": "n_compose"},
        ],
    }
    assert is_soft_artifact_dependency(graph, "n_clip_1", "n_clip_2") is True
    assert is_compose_sink_node(graph["nodes"][2]) is True
    assert is_soft_artifact_dependency(graph, "n_clip_1", "n_compose") is False
    preds = set(compose_required_predecessor_ids(graph))
    assert "n_clip_1" in preds and "n_clip_2" in preds and "n_speech" in preds
