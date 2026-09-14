# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from inspect import Parameter, signature

import pytest

from jiuwenswarm.runtime import model_catalog
from jiuwenswarm.runtime.model_catalog import (
    ModelCatalogError,
    RuntimeModelDescriptor,
    build_model_catalog,
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
            "api_key": "sk-must-not-leak",
            "api_base": "https://must-not-leak.invalid/v1",
            "custom_headers": {"Authorization": "secret-header"},
        },
        "model_config_obj": {"reasoning_level": "high"},
    }
    if agentos:
        entry["model_config_obj"]["_source"] = "agentos"
    return entry


def test_catalog_uses_original_global_indexes_and_consumes_iterable_once() -> None:
    iterations = 0

    def entries():
        nonlocal iterations
        iterations += 1
        yield _entry("same-model", default=True)
        yield _entry("video")
        yield _entry("same-model", agentos=True)
        yield _entry("provider/unique", alias="friendly")

    catalog = build_model_catalog(entries())

    assert iterations == 1
    assert [item.selection_key for item in catalog.models] == [
        "same-model#0",
        "same-model#2",
        "provider/unique#3",
    ]
    assert catalog.current_selection == "same-model#0"
    assert catalog.models[0].is_current is True
    assert catalog.models[1].is_agentos is True
    assert resolve_model_selection(catalog, "friendly").selection_key == (
        "provider/unique#3"
    )


def test_catalog_output_is_credential_and_endpoint_free() -> None:
    catalog = build_model_catalog([_entry("safe-model")])

    serialized = json.dumps(catalog.to_dict())

    assert "must-not-leak" not in serialized
    assert "secret-header" not in serialized
    assert "api_key" not in serialized
    assert "api_base" not in serialized
    assert "custom_headers" not in serialized


def test_catalog_resolves_default_name_and_rejects_ambiguous_alias() -> None:
    catalog = build_model_catalog(
        [
            _entry("same-model"),
            _entry("same-model", default=True),
            _entry("other-a", alias="duplicate"),
            _entry("other-b", alias="duplicate"),
        ],
        current_selection="same-model",
    )

    assert catalog.current_selection == "same-model#1"
    assert resolve_model_selection(catalog, "same-model").selection_key == (
        "same-model#1"
    )
    with pytest.raises(ModelCatalogError, match="multiple models") as caught:
        resolve_model_selection(catalog, "duplicate")
    assert caught.value.code == "BAD_REQUEST"


def test_catalog_rejects_missing_and_multimodal_selection() -> None:
    catalog = build_model_catalog([_entry("safe-model")])

    with pytest.raises(ModelCatalogError, match="model not found") as missing:
        resolve_model_selection(catalog, "missing")
    assert missing.value.code == "NOT_FOUND"

    with pytest.raises(ModelCatalogError, match="multimodal-only") as multimodal:
        resolve_model_selection(catalog, "vision")
    assert multimodal.value.code == "BAD_REQUEST"


def test_private_entry_resolution_validates_global_index_and_name() -> None:
    first = _entry("same-model", default=True)
    ignored = _entry("other-model")
    second = _entry("same-model", agentos=True)
    entries = [first, ignored, second]

    resolver = model_catalog._resolve_configured_model_entry
    assert resolver(iter(entries), "same-model#2") is second
    assert resolver(entries, "other-model#2") is None
    assert resolver(entries, "same-model#99") is None
    assert resolver(entries, "same-model") is first


def test_original_entry_resolver_is_not_publicly_exported() -> None:
    assert not hasattr(model_catalog, "resolve_configured_model_entry")
    assert "_resolve_configured_model_entry" not in model_catalog.__all__


def test_model_dto_is_frozen_slotted_and_keyword_only() -> None:
    descriptor = RuntimeModelDescriptor(
        selection_key="model#0",
        display_name="model",
        model_name="model",
    )

    assert not hasattr(descriptor, "__dict__")
    with pytest.raises(FrozenInstanceError):
        descriptor.model_name = "changed"  # type: ignore[misc]
    assert all(
        parameter.kind is Parameter.KEYWORD_ONLY
        for parameter in signature(RuntimeModelDescriptor).parameters.values()
    )
