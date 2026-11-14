"""Tests for the production Harness task materialization boundary."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from jiuwenswarm.agents.harness.common.rsi import build_rsi_service_context
from jiuwenswarm.agents.harness.common.rsi.errors import (
    RsiDatasetInvalid,
    RsiModelNotFound,
    RsiPathInvalid,
    RsiUnsupportedParameter,
)
from jiuwenswarm.agents.harness.common.rsi.harness_activation import (
    resolve_native_harness_baseline,
)
from jiuwenswarm.agents.harness.common.rsi.harness_adapter import HarnessEngineAdapter
from jiuwenswarm.agents.harness.common.rsi.materializer import RsiTaskMaterializer
from jiuwenswarm.agents.harness.common.rsi.model_resolver import RsiModelConfigResolver


def test_default_model_builder_uses_public_adapter_entry(monkeypatch):
    from jiuwenswarm.agents.harness.common.rsi import model_resolver
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter

    model = object()
    calls = []

    def build(client_config, request_config):
        calls.append((client_config, request_config))
        return model

    monkeypatch.setattr(JiuWenSwarmDeepAdapter, "build_model_from_entry", staticmethod(build))
    assert model_resolver._default_model_builder({"model_name": "selected"}, {"temperature": 0.2}) is model
    assert calls == [({"model_name": "selected"}, {"temperature": 0.2})]


def _entry(
    name: str,
    *,
    alias: str = "",
    is_default: bool = False,
    api_base: str = "https://example.test/v1",
):
    return {
        "model_client_config": {
            "model_name": name,
            "client_provider": "OpenAI",
            "api_base": api_base,
            "api_key": f"secret-{name}",
        },
        "model_config_obj": {"temperature": 0.2},
        "alias": alias,
        "is_default": is_default,
    }


class _FakeModelConfig:
    def __init__(self, values: dict):
        self.values = values

    def model_dump(self, **_: object) -> dict:
        return dict(self.values)


def _tree_digest(path: Path) -> str:
    digest = hashlib.sha256()
    for item in sorted(
        (candidate for candidate in path.rglob("*") if candidate.is_file()),
        key=lambda candidate: candidate.relative_to(path).as_posix(),
    ):
        relative = item.relative_to(path).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(bytes.fromhex(hashlib.sha256(item.read_bytes()).hexdigest()))
    return digest.hexdigest()


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({}, 3),
        ({"max_repair_rounds": 1}, 1),
        ({"max_repair_rounds": 5}, 5),
        ({"training_options": {"max_repair_rounds": 1}}, 1),
        ({"max_repair_rounds": 2, "training_options": {"max_repair_rounds": 5}}, 2),
    ],
)
def test_repair_round_defaults_preserve_explicit_options(params: dict, expected: int) -> None:
    from jiuwenswarm.agents.harness.common.rsi.materializer import _profile_options
    from jiuwenswarm.agents.harness.common.rsi.services import _harness_profile_options

    options = _harness_profile_options(params)
    assert options["max_repair_rounds"] == expected
    assert _profile_options(options)["max_repair_rounds"] == expected
    assert _profile_options({})["max_repair_rounds"] == 3


def test_model_resolver_uses_models_list_global_origin_index(tmp_path: Path) -> None:
    entries = [
        _entry("same", alias="first", is_default=True, api_base="https://one.test/v1"),
        _entry("same", alias="second", api_base="https://two.test/v1"),
    ]

    def build_model(mcc: dict, mco: dict) -> SimpleNamespace:
        return SimpleNamespace(
            model_client_config=_FakeModelConfig(mcc),
            model_config=_FakeModelConfig({"model_name": mcc["model_name"], **mco}),
        )

    resolver = RsiModelConfigResolver(
        config_loader=lambda: {},
        defaults_loader=lambda _: entries,
        zen_loader=lambda: [],
        model_builder=build_model,
    )

    manifest = resolver.resolve_to_file("same#1", "tester", tmp_path)
    payload = yaml.safe_load((tmp_path / "tester.yaml").read_text(encoding="utf-8"))

    assert manifest["origin_index"] == 1
    assert manifest["model_name"] == "same"
    assert payload["model_client_config"]["api_base"] == "https://two.test/v1"
    assert payload["model_client_config"]["max_retries"] == 0
    assert payload["model_request_config"]["model"] == "same"
    assert payload["model_client_config"]["api_key"] == "secret-same"


def test_model_resolver_rejects_unknown_reference_without_default_fallback(
    tmp_path: Path,
) -> None:
    resolver = RsiModelConfigResolver(
        config_loader=lambda: {},
        defaults_loader=lambda _: [_entry("configured", is_default=True)],
        zen_loader=lambda: [],
        model_builder=lambda mcc, mco: SimpleNamespace(
            model_client_config=_FakeModelConfig(mcc),
            model_config=_FakeModelConfig({"model_name": mcc["model_name"], **mco}),
        ),
    )

    with pytest.raises(RsiModelNotFound):
        resolver.resolve_to_file("missing", "tester", tmp_path)






















