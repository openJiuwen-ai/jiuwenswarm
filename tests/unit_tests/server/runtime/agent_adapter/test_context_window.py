# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Effective context-window composition and session-slot writes."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from jiuwenswarm.server.runtime.agent_adapter.deep.context_window import (
    apply_context_window_to_session,
    catalog_window_from_model_entries,
    parse_positive_window_tokens,
    resolve_effective_context_window,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (300000, 300000),
        (1, 1),
        (0, None),
        (-1, None),
        (True, None),
        (False, None),
        ("300000", None),
        (None, None),
        (3.5, None),
    ],
)
def test_parse_positive_window_tokens(value, expected):
    assert parse_positive_window_tokens(value) == expected


def test_resolve_prefers_selected_over_catalog():
    effective, source = resolve_effective_context_window(
        selected=300000,
        catalog_max=1048576,
    )
    assert effective == 300000
    assert source == "selected"


def test_resolve_falls_back_to_catalog_max():
    effective, source = resolve_effective_context_window(
        selected=None,
        catalog_max=1048576,
    )
    assert effective == 1048576
    assert source == "catalog_max"


def test_resolve_none_when_both_missing():
    effective, source = resolve_effective_context_window(
        selected=None,
        catalog_max=None,
    )
    assert effective is None
    assert source == "none"


def test_resolve_ignores_invalid_selected_then_catalog():
    effective, source = resolve_effective_context_window(
        selected="300000",
        catalog_max=1048576,
    )
    assert effective == 1048576
    assert source == "catalog_max"


def test_resolve_ignore_selected_for_auto_routing():
    effective, source = resolve_effective_context_window(
        selected=300000,
        catalog_max=1048576,
        ignore_selected=True,
    )
    assert effective == 1048576
    assert source == "catalog_max"


def test_apply_writes_context_slot_and_clears_without_sticky():
    context = SimpleNamespace(_context_window_tokens=None)
    engine_config = SimpleNamespace(context_window_tokens=None)
    engine = SimpleNamespace(_config=engine_config)

    snap = apply_context_window_to_session(
        context=context,
        context_engine=engine,
        effective=300000,
        snapshot=None,
    )
    assert context._context_window_tokens == 300000
    assert engine_config.context_window_tokens == 300000
    assert snap is not None
    assert snap["context_prev"] is None

    # 改档
    snap = apply_context_window_to_session(
        context=context,
        context_engine=engine,
        effective=600000,
        snapshot=snap,
    )
    assert context._context_window_tokens == 600000
    assert snap["context_prev"] is None  # 仍保留首次覆盖前快照

    # 无生效值 → 清除，不粘滞
    snap = apply_context_window_to_session(
        context=context,
        context_engine=engine,
        effective=None,
        snapshot=snap,
    )
    assert snap is None
    assert context._context_window_tokens is None
    assert engine_config.context_window_tokens is None


def test_apply_noop_when_never_overridden_and_no_effective():
    context = SimpleNamespace(_context_window_tokens=202752)
    engine_config = SimpleNamespace(context_window_tokens=None)
    engine = SimpleNamespace(_config=engine_config)

    snap = apply_context_window_to_session(
        context=context,
        context_engine=engine,
        effective=None,
        snapshot=None,
    )
    assert snap is None
    assert context._context_window_tokens == 202752


def test_apply_seeds_engine_when_context_missing():
    engine_config = SimpleNamespace(context_window_tokens=None)
    engine = SimpleNamespace(_config=engine_config)

    snap = apply_context_window_to_session(
        context=None,
        context_engine=engine,
        effective=1048576,
        snapshot=None,
    )
    assert engine_config.context_window_tokens == 1048576
    assert snap is not None
    assert snap["seeded_engine"] is True


def test_apply_pending_then_retries_when_engine_appears():
    snap = apply_context_window_to_session(
        context=None,
        context_engine=None,
        effective=300000,
        snapshot=None,
    )
    assert snap is not None
    assert snap.get("pending") == 300000

    engine_config = SimpleNamespace(context_window_tokens=None)
    engine = SimpleNamespace(_config=engine_config)
    snap = apply_context_window_to_session(
        context=None,
        context_engine=engine,
        effective=300000,
        snapshot=snap,
    )
    assert engine_config.context_window_tokens == 300000
    assert "pending" not in snap
    assert snap["seeded_engine"] is True


def test_catalog_window_from_model_entries_matches_name_and_ignores_mco():
    entries = [
        {
            "model_client_config": {"model_name": "custom-m"},
            "model_config_obj": {"context_window_tokens": 999},
            "context_window_tokens": 32_000,
        }
    ]
    assert catalog_window_from_model_entries(entries, model_name="custom-m") == 32_000
    assert catalog_window_from_model_entries(entries, model_name="other") is None
