# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
"""Minimal unit tests for jiuwenswarm.enterprise.edition (gateway feature order 1)."""

from __future__ import annotations

import pytest

from jiuwenswarm.enterprise.edition import is_enterprise


@pytest.mark.parametrize(
    "value",
    [
        None,  # unset
        "",
        "personal",
        "Personal",
        "PERSONAL",
        "  personal  ",
        "garbage",
        "enterprise-ish",
    ],
)
def test_is_enterprise_false(monkeypatch, value: str | None) -> None:
    monkeypatch.delenv("JIUWENSWARM_EDITION", raising=False)
    if value is not None:
        monkeypatch.setenv("JIUWENSWARM_EDITION", value)
    assert is_enterprise() is False


@pytest.mark.parametrize(
    "value",
    [
        "enterprise",
        "Enterprise",
        "ENTERPRISE",
        "  enterprise  ",
        "\tEnterprise\n",
    ],
)
def test_is_enterprise_true(monkeypatch, value: str) -> None:
    monkeypatch.setenv("JIUWENSWARM_EDITION", value)
    assert is_enterprise() is True


def test_is_enterprise_ignores_legacy_gateway_edition(monkeypatch) -> None:
    monkeypatch.delenv("JIUWENSWARM_EDITION", raising=False)
    monkeypatch.setenv("GATEWAY_EDITION", "enterprise")
    assert is_enterprise() is False
