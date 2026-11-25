# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""to_runtime_language: UI preference tokens → harness cn/en."""

from __future__ import annotations

from jiuwenswarm.common.i18n import to_runtime_language


def test_to_runtime_language_maps_zh_aliases_to_cn():
    assert to_runtime_language("zh") == "cn"
    assert to_runtime_language("zh-CN") == "cn"
    assert to_runtime_language("cn") == "cn"


def test_to_runtime_language_keeps_en():
    assert to_runtime_language("en") == "en"
    assert to_runtime_language("en-US") == "en"


def test_to_runtime_language_unknown_returns_none():
    assert to_runtime_language(None) is None
    assert to_runtime_language("") is None
    assert to_runtime_language("fr") is None
    assert to_runtime_language(123) is None
