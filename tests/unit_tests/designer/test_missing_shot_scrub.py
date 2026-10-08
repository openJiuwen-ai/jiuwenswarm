# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Missing-shot scrub + prepare_document_update before fail-closed validate."""

from __future__ import annotations

from copy import deepcopy

import pytest

from jiuwenswarm.server.runtime.designer.chat_document_sync import (
    ChatDocument,
    prepare_document_update,
)
from jiuwenswarm.server.runtime.designer.chat_shot_references import (
    referenced_shot_indices,
    scrub_missing_shot_references,
)


def _stale_brief(*, live: set[int], missing: int, duration_n: int) -> str:
    sections = []
    for index in sorted(live | {missing}):
        sections.append(f"### Shot {index} | beat-{index}\naction for shot {index}")
    sections.append(f"each shot {duration_n} seconds")
    chain = " -> ".join(f"shot {i}" for i in sorted(live | {missing}))
    sections.append(f"BGM: {chain}")
    return "\n".join(sections)


def test_scrub_drops_missing_heading_keeps_live_and_duration() -> None:
    text = _stale_brief(live={1, 2}, missing=3, duration_n=5)
    out = scrub_missing_shot_references(text, {1, 2})
    refs = referenced_shot_indices(out)
    assert refs <= {1, 2}
    assert 3 not in refs
    assert 5 not in refs
    assert "each shot 5 seconds" in out
    assert "Shot 1" in out and "Shot 2" in out
    assert "Shot 3" not in out
    assert "[[REMOVED_SHOT:" not in out


def test_scrub_neutralizes_inline_missing_without_redirect() -> None:
    text = "Continue from shot 3 into shot 2. each shot 5 seconds."
    out = scrub_missing_shot_references(text, {1, 2})
    assert 3 not in referenced_shot_indices(out)
    assert 5 not in referenced_shot_indices(out)
    # Scrub removes the missing index; it must not rewrite continuity onto shot 2.
    assert "from shot 3" not in out.lower()
    assert "continuing from shot 2" not in out.lower()


def test_prepare_document_update_scrubs_stale_shot_and_duration_fp() -> None:
    brief = (
        "### Shot 1 | open\n"
        "wide establishing\n"
        "### Shot 2 | mid\n"
        "medium framing\n"
        "### Shot 3 | gone\n"
        "stale body\n"
        "each shot 5 seconds\n"
        "BGM shot 1 -> shot 2 -> shot 3\n"
    )
    before = {
        "nodes": [
            {"id": "n_brief", "type": "text", "config": {"role": "brief", "prompt": brief}},
            {"id": "n_clip_1", "type": "video", "config": {"role": "clip", "shot_index": 1}},
            {"id": "n_clip_2", "type": "video", "config": {"role": "clip", "shot_index": 2}},
        ],
        "edges": [],
        "metadata": {},
    }
    candidate = deepcopy(before)
    candidate["nodes"][2]["config"]["camera"] = "close-up"
    docs = {"n_brief": ChatDocument("n_brief", "brief", brief, None)}
    edits = [
        {
            "node_id": "n_brief",
            "replacements": [{"old": "medium framing", "new": "close-up framing"}],
        }
    ]
    _graph, texts, changed = prepare_document_update(before, candidate, docs, edits)
    assert changed
    assert "close-up framing" in texts["n_brief"]
    refs = referenced_shot_indices(texts["n_brief"])
    assert refs <= {1, 2}
    assert 3 not in refs
    assert 5 not in refs


@pytest.mark.parametrize("duration_n", [3, 5, 8, 12])
def test_duration_only_brief_does_not_fail_validate(duration_n: int) -> None:
    brief = (
        f"### Shot 1 | open\nwide\n"
        f"### Shot 2 | mid\nmedium\n"
        f"each shot {duration_n} seconds\n"
    )
    before = {
        "nodes": [
            {"id": "n_brief", "type": "text", "config": {"role": "brief", "prompt": brief}},
            {"id": "n_clip_1", "type": "video", "config": {"role": "clip", "shot_index": 1}},
            {"id": "n_clip_2", "type": "video", "config": {"role": "clip", "shot_index": 2}},
        ],
        "edges": [],
        "metadata": {},
    }
    candidate = deepcopy(before)
    candidate["nodes"][2]["config"]["shot_action"] = "lean in"
    docs = {"n_brief": ChatDocument("n_brief", "brief", brief, None)}
    edits = [
        {"node_id": "n_brief", "replacements": [{"old": "medium", "new": "tight medium"}]}
    ]
    _graph, texts, changed = prepare_document_update(before, candidate, docs, edits)
    assert changed
    assert referenced_shot_indices(texts["n_brief"]) <= {1, 2}
    assert duration_n not in referenced_shot_indices(texts["n_brief"])
