# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Unsupported low resolutions use the smallest available tier."""

import pytest

from jiuwenswarm.server.runtime.designer.pipeline.model_capacity import capacity_for_model, snap_resolution


@pytest.mark.parametrize("model,requested,expected", [
    ("MiniMax-H3", "480p", "768P"),
    ("MiniMax-H3", "480", "768P"),
    ("MiniMax-H3", "720P", "768P"),
    ("MiniMax-H3", "4K", "2K"),
    ("MiniMax-H3", "2K", "2K"),
    ("MiniMax-H3", "", "768P"),
    ("MiniMax-H3-Max", "480P", "480P"),
    ("wan3.0-video", "480P", "480P"),
])
def test_resolution_snaps_to_the_nearest_lower_or_minimum_tier(model, requested, expected):
    assert snap_resolution(requested, capacity_for_model(model)) == expected
