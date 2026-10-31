# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for the pure Symphony feature-switch resolvers."""

import pytest

from jiuwenswarm.common.symphony_config import (
    DEFAULT_EVOLUTION_ENABLED,
    DEFAULT_SYMPHONY_ENABLED,
    resolve_symphony_enabled,
    resolve_symphony_evolution_enabled,
)

_RESOLVERS = [
    (resolve_symphony_enabled, DEFAULT_SYMPHONY_ENABLED),
    (resolve_symphony_evolution_enabled, DEFAULT_EVOLUTION_ENABLED),
]
_TRUTHY = ["1", "true", "True", " yes ", "ON"]
_FALSY = ["0", "false", "no", "off"]


@pytest.mark.parametrize(("resolver", "default"), _RESOLVERS)
class TestSymphonySwitchResolvers:
    @pytest.mark.parametrize("value", [True, *(_v for _v in _TRUTHY)])
    def test_truthy_values_enable(self, resolver, default, value):
        assert resolver(value) is True

    @pytest.mark.parametrize("value", [False, *(_v for _v in _FALSY)])
    def test_falsy_values_disable(self, resolver, default, value):
        assert resolver(value) is False

    def test_none_inherits_code_default(self, resolver, default):
        assert resolver(None) is default

    def test_unrecognized_text_inherits_code_default(self, resolver, default):
        assert resolver("garbage") is default
