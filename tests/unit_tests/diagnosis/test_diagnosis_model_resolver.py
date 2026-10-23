# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the diagnosis model resolver's selection sanitization.

The settings panel stores ``reasoning_level`` in ``model_config_obj`` as an
internal UI hint; the selection compiler forwards unknown request keys to the
vendor SDK, which rejects them. The diagnosis resolver must apply the same
``reasoning_injector`` mapping the chat-side ``build_model_from_entry`` path
uses, so diagnosis behaves identically without touching the shared resolver.
"""

from __future__ import annotations

from typing import Any

import pytest

from jiuwenswarm.common.model_selection import ResolvedModel, ResolvedModelGroup, ResolvedRoute
from jiuwenswarm.diagnosis.model_resolver import _sanitize_resolved_selection


def _model(**overrides: Any) -> ResolvedModel:
    base: dict[str, Any] = {
        "model_id": "m1",
        "source": "defaults",
        "model_name": "model-a",
        "provider": "OpenAI",
        "api_base": "https://example.com/v1",
        "api_key": "sk-test",
        "request_defaults": {},
    }
    base.update(overrides)
    return ResolvedModel(**base)


def test_sanitize_strips_reasoning_level_into_reasoning_config() -> None:
    resolved = _model(request_defaults={"reasoning_level": "medium", "temperature": 0.7})

    cleaned = _sanitize_resolved_selection(resolved)

    assert "reasoning_level" not in cleaned.request_defaults
    assert cleaned.request_defaults["reasoning"] == {"mode": "enabled", "effort": "medium"}
    assert cleaned.request_defaults["temperature"] == 0.7
    # Everything else on the model stays untouched.
    assert cleaned.model_id == "m1"
    assert cleaned.model_name == "model-a"


def test_sanitize_passes_models_without_reasoning_hints_through() -> None:
    resolved = _model(request_defaults={"temperature": 0.3})

    cleaned = _sanitize_resolved_selection(resolved)

    assert cleaned.request_defaults["temperature"] == 0.3
    assert "reasoning" not in cleaned.request_defaults


def test_sanitized_selection_compiles_without_vendor_keys() -> None:
    from openjiuwen.core.foundation.llm.routing.compiler import compile_model_selection

    cleaned = _sanitize_resolved_selection(_model(request_defaults={"reasoning_level": "high"}))
    compiled = compile_model_selection(cleaned.model_dump())

    dumped = compiled.model_request_config.model_dump(exclude_none=True)
    assert "reasoning_level" not in dumped
    assert dumped["reasoning"] == {"mode": "enabled", "effort": "high"}
    assert dumped["model_name"] == "model-a"


def test_sanitize_group_routes_overrides_and_request_config() -> None:
    group = ResolvedModelGroup(
        model_group_id="g1",
        routes=[
            ResolvedRoute(
                route_id="r1",
                model=_model(request_defaults={"reasoning_level": "low"}),
                request_overrides={"reasoning_level": "high"},
            ),
        ],
        request_config={"reasoning_level": "medium"},
    )

    cleaned = _sanitize_resolved_selection(group)

    route = cleaned.routes[0]
    assert "reasoning_level" not in route.model.request_defaults
    assert route.model.request_defaults["reasoning"] == {"mode": "enabled", "effort": "low"}
    assert "reasoning_level" not in route.request_overrides
    assert route.request_overrides["reasoning"] == {"mode": "enabled", "effort": "high"}
    assert "reasoning_level" not in cleaned.request_config
    assert cleaned.request_config["reasoning"] == {"mode": "enabled", "effort": "medium"}


def test_resolve_diagnosis_model_applies_sanitization(monkeypatch: pytest.MonkeyPatch) -> None:
    from jiuwenswarm.common.model_selection import ModelSelection
    from jiuwenswarm.diagnosis.model_resolver import resolve_diagnosis_model

    class _FakeResolver:
        def resolve(self, selection: Any) -> ResolvedModel:
            return _model(request_defaults={"reasoning_level": "medium"})

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.model_routing_registry.ModelSelectionResolver",
        _FakeResolver,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.model_selection_store.get_session_model_selection",
        lambda _session_id: ModelSelection(type="model", id="m1"),
    )

    resolved = resolve_diagnosis_model("session-1")

    assert resolved.info.id == "m1"
    assert "reasoning_level" not in resolved.resolved.request_defaults
    assert resolved.resolved.request_defaults["reasoning"] == {"mode": "enabled", "effort": "medium"}
