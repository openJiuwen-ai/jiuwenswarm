# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for the pure recommendation switch resolver."""

import pytest

from jiuwenswarm.common.recommendation_switches import resolve_recommendation_switch


class TestResolveRecommendationSwitch:
    @pytest.mark.parametrize("value", [True, "1", "true", "YES", " on ", "TRUE"])
    def test_truthy_values(self, value):
        assert resolve_recommendation_switch(value, default=False) is True

    @pytest.mark.parametrize("value", [False, "0", "false", "no", "off", " Off "])
    def test_falsy_values(self, value):
        assert resolve_recommendation_switch(value, default=True) is False

    def test_bool_short_circuits_default(self):
        assert resolve_recommendation_switch(True, default=False) is True
        assert resolve_recommendation_switch(False, default=True) is False

    def test_none_inherits_default(self):
        assert resolve_recommendation_switch(None, default=True) is True
        assert resolve_recommendation_switch(None, default=False) is False

    def test_unrecognized_text_inherits_default(self):
        assert resolve_recommendation_switch("maybe", default=True) is True
        assert resolve_recommendation_switch("maybe", default=False) is False
