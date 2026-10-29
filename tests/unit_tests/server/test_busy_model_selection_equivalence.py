# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A stable reference to the model a busy child was built with is not a switch."""

from __future__ import annotations

from typing import Any

import pytest

from jiuwenswarm.common import model_catalog
from jiuwenswarm.common.model_selection import ModelSelection
from jiuwenswarm.server.runtime.agent_adapter import interface_deep
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import _model_name_names_selection


class _Catalog:
    models: list[dict[str, Any]] = []

    def list_public_models(self) -> list[dict[str, Any]]:
        return self.models


def _install_catalog(monkeypatch: pytest.MonkeyPatch, *models: tuple[str, str, str]) -> None:
    catalog = type("Catalog", (_Catalog,), {
        "models": [
            {"model_id": model_id, "model_name": name, "alias": alias}
            for model_id, name, alias in models
        ],
    })
    monkeypatch.setattr(model_catalog, "ModelCatalog", catalog)


def test_name_matches_selection_of_same_model(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_catalog(monkeypatch, ("mdl_a", "DeepSeek-V4.1-Flash", ""), ("mdl_b", "qwen3-32b", "Qwen"))

    assert _model_name_names_selection("DeepSeek-V4.1-Flash", ModelSelection(type="model", id="mdl_a"))
    assert _model_name_names_selection("Qwen", ModelSelection(type="model", id="mdl_b"))


def test_name_of_other_model_is_a_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_catalog(monkeypatch, ("mdl_a", "DeepSeek-V4.1-Flash", ""), ("mdl_b", "qwen3-32b", ""))

    assert not _model_name_names_selection("DeepSeek-V4.1-Flash", ModelSelection(type="model", id="mdl_b"))


def test_ambiguous_name_group_or_missing_name_is_a_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_catalog(monkeypatch, ("mdl_a", "DeepSeek-V4.1-Flash", ""), ("mdl_c", "DeepSeek-V4.1-Flash", ""))

    assert not _model_name_names_selection("DeepSeek-V4.1-Flash", ModelSelection(type="model", id="mdl_a"))
    assert not _model_name_names_selection("DeepSeek-V4.1-Flash", ModelSelection(type="model_group", id="mdl_a"))
    assert not _model_name_names_selection("", ModelSelection(type="model", id="mdl_a"))


def _install_model_list(monkeypatch: pytest.MonkeyPatch, *models: tuple[str, str, str]) -> None:
    entries = [
        {"model_id": model_id, "alias": alias, "model_client_config": {"model_name": name}}
        for model_id, name, alias in models
    ]
    monkeypatch.setattr(interface_deep, "get_config", lambda: {})
    monkeypatch.setattr(interface_deep, "get_available_models", lambda config: entries)


def test_indexed_name_resolves_through_the_global_model_list(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_model_list(
        monkeypatch,
        ("mdl_a", "qwen3-32b", ""),
        ("mdl_b", "DeepSeek-V4.1-Flash", ""),
        ("mdl_c", "qwen3-32b", "Qwen Backup"),
    )

    assert _model_name_names_selection("qwen3-32b#2", ModelSelection(type="model", id="mdl_c"))
    assert _model_name_names_selection("Qwen Backup#2", ModelSelection(type="model", id="mdl_c"))
    assert not _model_name_names_selection("qwen3-32b#2", ModelSelection(type="model", id="mdl_a"))
    assert not _model_name_names_selection("DeepSeek-V4.1-Flash#2", ModelSelection(type="model", id="mdl_c"))


def test_malformed_or_out_of_range_index_is_a_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_model_list(monkeypatch, ("mdl_a", "qwen3-32b", ""))

    assert not _model_name_names_selection("qwen3-32b#5", ModelSelection(type="model", id="mdl_a"))
    assert not _model_name_names_selection("qwen3-32b#x", ModelSelection(type="model", id="mdl_a"))
