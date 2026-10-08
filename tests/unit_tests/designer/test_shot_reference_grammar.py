# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shot-index grammar: duration / distributive idioms are not shot ids."""

from __future__ import annotations

import pytest

from jiuwenswarm.server.runtime.designer.chat_shot_references import (
    map_shot_references,
    referenced_shot_indices,
)

# Generic phrases — not product-specific café / cup corpus.
_DURATION_FALSE_POSITIVES = (
    "每个镜头 5 秒",
    "每一镜头 8 秒",
    "各镜头 3 分钟",
    "每 镜头 12 秒",
    "each shot 5 seconds",
    "every shot 4 sec",
    "per shot 6 s",
    "each shot 2 minutes",
    "镜头 5 秒的节奏",
    "shot 7 seconds of hold",
    "shot #9 sec pause",
)

_REAL_REFERENCES = (
    ("### 镜头 3｜开场", {3}),
    ("**Shot 2 | close**", {2}),
    ("分镜 4：推进", {4}),
    ("shot #11 holds", {11}),
    ("镜头1→镜头2→镜头3", {1, 2, 3}),
    ("BGM: shot 1 -> shot 2 -> shot 4", {1, 2, 4}),
    ("承接镜头 2 的站位", {2}),
    ("continuing from shot 8", {8}),
)


@pytest.mark.parametrize("text", _DURATION_FALSE_POSITIVES)
def test_duration_distributive_idioms_are_not_shot_indices(text: str) -> None:
    assert referenced_shot_indices(text) == set()


@pytest.mark.parametrize(("text", "expected"), _REAL_REFERENCES)
def test_real_shot_references_are_kept(text: str, expected: set[int]) -> None:
    assert referenced_shot_indices(text) == expected


def test_mixed_duration_and_real_refs() -> None:
    text = (
        "### 镜头 1｜开场\n"
        "每个镜头 5 秒\n"
        "### 镜头 2｜推进\n"
        "each shot 4 seconds\n"
        "BGM: 镜头1→镜头2→镜头3\n"
    )
    assert referenced_shot_indices(text) == {1, 2, 3}


def test_map_shot_references_skips_duration_idioms() -> None:
    text = "每个镜头 5 秒；镜头 3 已删除"
    out = map_shot_references(text, {3: ("missing:3", None), 5: ("missing:5", None)})
    assert "每个镜头 5 秒" in out
    assert "[[REMOVED_SHOT:missing:3]]" in out
    assert "[[REMOVED_SHOT:missing:5]]" not in out
