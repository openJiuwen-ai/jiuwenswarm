# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

from dataclasses import asdict

import pytest

from jiuwenswarm.runtime.model_catalog import (
    ModelCatalogError,
    build_model_catalog,
    resolve_configured_model_entry,
    resolve_model_selection,
)


def _entry(
    model_name: str,
    *,
    alias: str = "",
    provider: str = "openai",
    default: bool = False,
    agentos: bool = False,
) -> dict:
    entry = {
        "alias": alias,
        "is_default": default,
        "model_client_config": {
            "model_name": model_name,
            "client_provider": provider,
            "api_key": "must-not-leak",
            "api_base": "https://must-not-leak.invalid/v1",
        },
        "model_config_obj": {"reasoning_level": "high"},
    }
    if agentos:
        entry["model_config_obj"]["_source"] = "agentos"
    return entry


def test_model_catalog_uses_runtime_compatible_global_selection_keys() -> None:
    catalog = build_model_catalog(
        [
            _entry("same-model", default=True),
            _entry("video"),
            _entry("same-model", agentos=True),
            _entry("provider/unique", alias="friendly"),
        ]
    )

    assert [item.selection_key for item in catalog.models] == [
        "same-model#0",
        "same-model#2",
        "friendly",
    ]
    assert catalog.current_selection == "same-model#0"
    assert catalog.models[0].is_current is True
    assert catalog.models[1].is_agentos is True
    assert resolve_model_selection(catalog, "friendly").model_name == (
        "provider/unique"
    )


def test_model_catalog_rejects_ambiguous_missing_and_multimodal_names() -> None:
    catalog = build_model_catalog(
        [_entry("same-model", default=True), _entry("same-model")]
    )

    with pytest.raises(ModelCatalogError, match="multiple models") as ambiguous:
        resolve_model_selection(catalog, "same-model")
    assert ambiguous.value.code == "BAD_REQUEST"

    with pytest.raises(ModelCatalogError, match="model not found") as missing:
        resolve_model_selection(catalog, "missing")
    assert missing.value.code == "NOT_FOUND"

    with pytest.raises(ModelCatalogError, match="multimodal-only") as multimodal:
        resolve_model_selection(catalog, "vision")
    assert multimodal.value.code == "BAD_REQUEST"


def test_model_catalog_contract_never_contains_credentials_or_endpoint() -> None:
    catalog = build_model_catalog([_entry("safe-model")])

    serialized = repr(asdict(catalog.models[0]))
    assert "must-not-leak" not in serialized
    assert "api_key" not in serialized
    assert "api_base" not in serialized


def test_model_catalog_marks_resolved_selection_current() -> None:
    catalog = build_model_catalog(
        [_entry("model-a", default=True), _entry("model-b", alias="friendly")]
    )

    selected = resolve_model_selection(catalog, "friendly")

    assert selected.selection_key == "friendly"
    assert selected.is_current is True


def test_model_catalog_disables_colliding_aliases_and_reserved_real_names() -> None:
    catalog = build_model_catalog(
        [
            _entry("model-a", alias="shared"),
            _entry("model-b", alias="shared"),
            _entry("shared"),
            _entry("vision", alias="safe-looking-name"),
        ]
    )

    assert [item.model_name for item in catalog.models] == [
        "model-a",
        "model-b",
        "shared",
    ]
    # The exact executable key wins. The two colliding aliases remain display
    # labels only and cannot redirect the request away from model ``shared``.
    selected = resolve_model_selection(catalog, "shared")
    assert selected.model_name == "shared"
    assert [item.selection_key for item in catalog.models] == [
        "model-a",
        "model-b",
        "shared",
    ]


def test_executable_model_entry_uses_exact_global_index_and_validates_name() -> None:
    first = _entry("same-model", default=True)
    other = _entry("other-model")
    second = _entry("same-model", agentos=True)
    entries = [first, other, second]

    assert resolve_configured_model_entry(entries, "same-model#2") is second
    assert resolve_configured_model_entry(entries, "other-model#2") is None
    assert resolve_configured_model_entry(entries, "same-model#99") is None


def test_executable_model_entry_resolves_only_unambiguous_alias() -> None:
    aliased = _entry("model-a", alias="friendly")
    duplicate_alias = _entry("model-b", alias="duplicate")
    entries = [
        aliased,
        duplicate_alias,
        _entry("model-c", alias="duplicate"),
    ]

    assert resolve_configured_model_entry(entries, "friendly") is aliased
    assert resolve_configured_model_entry(entries, "duplicate") is None
