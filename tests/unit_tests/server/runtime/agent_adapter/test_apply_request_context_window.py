# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Request-level window apply without importing interface_deep."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from jiuwenswarm.server.runtime.agent_adapter.deep.context_window import (
    catalog_window_from_entry,
    resolve_catalog_window_tokens,
    run_request_context_window_apply,
)


def test_catalog_window_ignores_model_config_obj():
    entry = {
        "context_window_tokens": 1048576,
        "model_config_obj": {"context_window_tokens": 999},
    }
    assert catalog_window_from_entry(entry) == 1048576
    assert catalog_window_from_entry({"model_config_obj": {"context_window_tokens": 999}}) is None


def test_resolve_catalog_by_model_ref_not_object_identity():
    ref = "model-identity-v1:" + ("a" * 64)
    entry_by_ref = {
        ref: {"context_window_tokens": 1048576, "model_config_obj": {"x": 1}},
    }
    got = resolve_catalog_window_tokens(
        params={"model_ref": ref},
        entry_by_ref=entry_by_ref,
        identity_to_keys={},
        normalize_ref=lambda value: value.lower(),
    )
    assert got == 1048576


def test_resolve_catalog_by_unique_model_name():
    ref = "model-identity-v1:" + ("b" * 64)
    entry_by_ref = {ref: {"context_window_tokens": 300000}}
    got = resolve_catalog_window_tokens(
        params={"model_name": "glm-5.3"},
        entry_by_ref=entry_by_ref,
        identity_to_keys={ref: ["glm-5.3"]},
    )
    assert got == 300000


def test_resolve_catalog_ambiguous_model_name_returns_none():
    r1 = "model-identity-v1:" + ("c" * 64)
    r2 = "model-identity-v1:" + ("d" * 64)
    entry_by_ref = {
        r1: {"context_window_tokens": 100},
        r2: {"context_window_tokens": 200},
    }
    got = resolve_catalog_window_tokens(
        params={"model_name": "dup"},
        entry_by_ref=entry_by_ref,
        identity_to_keys={r1: ["dup"], r2: ["dup"]},
    )
    assert got is None


def test_resolve_catalog_default_entry_when_no_identity():
    ref = "model-identity-v1:" + ("e" * 64)
    entry_by_ref = {
        ref: {"is_default": True, "context_window_tokens": 202752},
    }
    got = resolve_catalog_window_tokens(
        params={},
        entry_by_ref=entry_by_ref,
        identity_to_keys={ref: ["default-model"]},
    )
    assert got == 202752


def test_resolve_catalog_skips_default_when_name_present_but_unmatched():
    ref = "model-identity-v1:" + ("f" * 64)
    entry_by_ref = {
        ref: {"is_default": True, "context_window_tokens": 202752},
    }
    got = resolve_catalog_window_tokens(
        params={"model_name": "other"},
        entry_by_ref=entry_by_ref,
        identity_to_keys={ref: ["default-model"]},
    )
    assert got is None


def test_resolve_catalog_normalize_ref_failure_falls_back_to_raw():
    ref = "not-a-valid-ref"
    entry_by_ref = {ref: {"context_window_tokens": 600000}}

    def _raise(_value: str) -> str:
        raise ValueError("invalid")

    got = resolve_catalog_window_tokens(
        params={"model_ref": ref},
        entry_by_ref=entry_by_ref,
        normalize_ref=_raise,
    )
    assert got == 600000


def test_run_apply_selected_over_catalog():
    context = SimpleNamespace(_context_window_tokens=None)
    engine = SimpleNamespace(_config=SimpleNamespace(context_window_tokens=None))
    snapshots: dict = {}
    run_request_context_window_apply(
        session_id="s1",
        params={"selected_context_window_tokens": 300000},
        catalog_max=1048576,
        context=context,
        context_engine=engine,
        snapshots=snapshots,
        model_name="glm-5.3",
        log=MagicMock(),
    )
    assert context._context_window_tokens == 300000
    assert "s1" in snapshots


def test_run_apply_enable_routing_ignores_selected():
    context = SimpleNamespace(_context_window_tokens=None)
    engine = SimpleNamespace(_config=SimpleNamespace(context_window_tokens=None))
    snapshots: dict = {}
    run_request_context_window_apply(
        session_id="s1",
        params={
            "enable_routing": True,
            "selected_context_window_tokens": 300000,
        },
        catalog_max=1048576,
        context=context,
        context_engine=engine,
        snapshots=snapshots,
        model_name="glm-5.3",
        log=MagicMock(),
    )
    assert context._context_window_tokens == 1048576


def test_run_apply_clears_without_sticky():
    context = SimpleNamespace(_context_window_tokens=None)
    engine = SimpleNamespace(_config=SimpleNamespace(context_window_tokens=None))
    snapshots: dict = {}
    run_request_context_window_apply(
        session_id="s1",
        params={"selected_context_window_tokens": 300000},
        catalog_max=1048576,
        context=context,
        context_engine=engine,
        snapshots=snapshots,
        log=MagicMock(),
    )
    run_request_context_window_apply(
        session_id="s1",
        params={},
        catalog_max=None,
        context=context,
        context_engine=engine,
        snapshots=snapshots,
        log=MagicMock(),
    )
    assert context._context_window_tokens is None
    assert snapshots == {}


def test_run_apply_logs_pending_when_nowhere_to_write():
    log = MagicMock()
    snapshots: dict = {}
    run_request_context_window_apply(
        session_id="s1",
        params={"selected_context_window_tokens": 300000},
        catalog_max=None,
        context=None,
        context_engine=None,
        snapshots=snapshots,
        model_name="glm-5.3",
        log=log,
    )
    assert snapshots["s1"]["pending"] == 300000
    assert log.info.called
    args, _kwargs = log.info.call_args
    assert "pending=%s" in args[0]
    assert args[4] is True
