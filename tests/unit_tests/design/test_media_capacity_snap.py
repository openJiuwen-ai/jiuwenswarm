# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for the designer media capacity clamping helpers."""

import pytest

from jiuwenswarm.server.runtime.designer.pipeline.model_capacity import (
    VideoModelCapacity,
    default_ratio,
    ratio_value,
    snap_ratio,
    snap_resolution,
)


def _capacity(
    resolutions=("720P", "1080P", "2K"),
    default_resolution="1080P",
    ratios=("16:9", "9:16", "4:3", "1:1"),
) -> VideoModelCapacity:
    return VideoModelCapacity(
        model_id="m",
        provider="p",
        min_sec=3,
        max_sec=60,
        default_sec=5,
        resolutions=resolutions,
        default_resolution=default_resolution,
        max_reference_images=3,
        supports_reference_images=True,
        ratios=ratios,
    )


class TestSnapResolution:
    def test_exact_allowed_tier_is_kept(self):
        cap = _capacity()
        assert snap_resolution("1080P", cap) == "1080P"

    def test_case_and_spacing_normalized(self):
        cap = _capacity()
        assert snap_resolution(" 2k ", cap) == "2K"

    def test_blank_request_uses_default(self):
        cap = _capacity()
        assert snap_resolution("", cap) == "1080P"
        assert snap_resolution(None, cap) == "1080P"

    @pytest.mark.parametrize(
        ("requested", "expected"),
        [
            ("1080", "1080P"),
            ("720", "720P"),
            ("4K", "1080P"),
            ("1K", "720P"),
        ],
    )
    def test_aliases_map_to_allowed_tiers(self, requested: str, expected: str):
        assert snap_resolution(requested, _capacity()) == expected

    def test_above_top_tier_clamps_to_top(self):
        # A model whose top tier is 2K clamps a 4K request down to 2K.
        cap = _capacity(resolutions=("720P", "2K"), default_resolution="720P")
        assert snap_resolution("4K", cap) == "2K"

    def test_below_lowest_tier_rounds_up_to_lowest(self):
        cap = _capacity()
        assert snap_resolution("480P", cap) == "720P"

    def test_unknown_token_falls_back_to_default(self):
        cap = _capacity()
        assert snap_resolution("3840x2160", cap) == "1080P"
        assert snap_resolution("banana", cap) == "1080P"


class TestRatioValue:
    @pytest.mark.parametrize(
        ("ratio", "expected"),
        [
            ("16:9", 16 / 9),
            (" 16 : 9 ", 16 / 9),
            ("1.5:1", 1.5),
        ],
    )
    def test_parses_width_over_height(self, ratio: str, expected: float):
        assert ratio_value(ratio) == pytest.approx(expected)

    @pytest.mark.parametrize("ratio", ["0:9", "9:0", "abc", "16:9:1", "", None])
    def test_unparsable_ratios_return_none(self, ratio):
        assert ratio_value(ratio) is None


class TestSnapRatio:
    def test_exact_ratio_is_kept(self):
        cap = _capacity()
        assert snap_ratio("9:16", cap) == "9:16"

    def test_closest_allowed_ratio_wins(self):
        cap = _capacity()
        # 16:10 = 1.6 is closer to 16:9 (1.78) than to 4:3 (1.33).
        assert snap_ratio("16:10", cap) == "16:9"
        assert snap_ratio("3:2", cap) == "4:3"

    def test_unparsable_request_uses_default_ratio(self):
        cap = _capacity()
        assert snap_ratio("banana", cap) == "16:9"

    def test_blank_request_uses_default_ratio(self):
        cap = _capacity()
        assert snap_ratio("", cap) == "16:9"


class TestDefaultRatio:
    def test_prefers_16_to_9(self):
        assert default_ratio(_capacity()) == "16:9"

    def test_falls_back_to_first_when_no_16_to_9(self):
        cap = _capacity(ratios=("1:1", "4:3"))
        assert default_ratio(cap) == "1:1"
