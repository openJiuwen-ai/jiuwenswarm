# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Tests for process-start external ModelClient loading."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from jiuwenswarm.common.local_env_config import SPAWN_ENV_KEYS
from jiuwenswarm.common import model_client_extensions


_PROVIDERS = (
    "TestExternalAlpha",
    "TestExternalBeta",
    "TestExternalBroken",
    "TestExternalGood",
    "TestExternalReentrant",
    "UOpenAI",
)


def _client_source(provider: str) -> str:
    return f'''\
from openjiuwen.core.foundation.llm.model_clients.openai_model_client import OpenAIModelClient


class ExternalModelClient(OpenAIModelClient):
    __client_name__ = ["{provider}"]
'''


def _write_client(root: Path, name: str, source: str) -> None:
    directory = root / "model_clients"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.py").write_text(source, encoding="utf-8")


@pytest.fixture(autouse=True)
def _reset_loader_and_registry(monkeypatch: pytest.MonkeyPatch):
    from openjiuwen.core.common.clients import get_client_registry

    registry = get_client_registry()
    model_client_extensions._LOADED_MODULES.clear()
    model_client_extensions._LOADING_MODULES.clear()
    monkeypatch.delenv("AGENT_EXTRA_MODEL_CLIENTS", raising=False)
    monkeypatch.delenv("EXTENSION_DIRS", raising=False)
    for provider in _PROVIDERS:
        if f"llm_{provider}" in registry.list_clients():
            registry.unregister(provider, client_type="llm")
    yield
    model_client_extensions._LOADED_MODULES.clear()
    model_client_extensions._LOADING_MODULES.clear()
    for provider in _PROVIDERS:
        if f"llm_{provider}" in registry.list_clients():
            registry.unregister(provider, client_type="llm")


def test_empty_selection_loads_nothing() -> None:
    assert model_client_extensions.load_extra_model_clients() == []


def test_only_selected_clients_load_and_each_loads_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from openjiuwen.core.common.clients import get_client_registry
    from openjiuwen.core.foundation.llm.schema.config import ModelClientConfig

    _write_client(tmp_path, "alpha_client", _client_source("TestExternalAlpha"))
    _write_client(tmp_path, "beta_client", _client_source("TestExternalBeta"))
    monkeypatch.setenv("EXTENSION_DIRS", str(tmp_path))
    monkeypatch.setenv(
        "AGENT_EXTRA_MODEL_CLIENTS",
        "alpha_client, alpha_client",
    )

    first = model_client_extensions.load_extra_model_clients()
    second = model_client_extensions.load_extra_model_clients()

    assert len(first) == 1
    assert second == first
    assert ModelClientConfig(client_provider="TestExternalAlpha").client_provider == (
        "TestExternalAlpha"
    )
    assert "llm_TestExternalBeta" not in get_client_registry().list_clients()


def test_first_extension_root_wins(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from openjiuwen.core.common.clients import get_client_registry

    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    _write_client(first_root, "company_client", _client_source("TestExternalAlpha"))
    _write_client(second_root, "company_client", _client_source("TestExternalBeta"))
    monkeypatch.setenv("EXTENSION_DIRS", f"{first_root};{second_root}")
    monkeypatch.setenv("AGENT_EXTRA_MODEL_CLIENTS", "company_client")

    model_client_extensions.load_extra_model_clients()

    registered = get_client_registry().list_clients()
    assert "llm_TestExternalAlpha" in registered
    assert "llm_TestExternalBeta" not in registered


def test_bad_entries_are_isolated(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from openjiuwen.core.common.clients import get_client_registry

    _write_client(tmp_path, "broken_client", "raise RuntimeError('broken')\n")
    _write_client(tmp_path, "good_client", _client_source("TestExternalGood"))
    monkeypatch.setenv("EXTENSION_DIRS", str(tmp_path))
    monkeypatch.setenv(
        "AGENT_EXTRA_MODEL_CLIENTS",
        "../escape,missing_client,broken_client,good_client",
    )

    records: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = records.append  # type: ignore[method-assign]
    model_client_extensions.logger.addHandler(handler)
    try:
        loaded = model_client_extensions.load_extra_model_clients()
    finally:
        model_client_extensions.logger.removeHandler(handler)

    assert len(loaded) == 1
    assert "llm_TestExternalGood" in get_client_registry().list_clients()
    log_text = "\n".join(record.getMessage() for record in records)
    assert "invalid module name" in log_text
    assert "not found" in log_text
    assert "Failed to load broken_client" in log_text


def test_reentrant_load_does_not_import_twice(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from openjiuwen.core.common.clients import get_client_registry

    source = """\
from jiuwenswarm.common.model_client_extensions import load_extra_model_clients
from openjiuwen.core.foundation.llm.model_clients.openai_model_client import OpenAIModelClient

load_extra_model_clients()


class ReentrantModelClient(OpenAIModelClient):
    __client_name__ = ["TestExternalReentrant"]
"""
    _write_client(tmp_path, "reentrant_client", source)
    monkeypatch.setenv("EXTENSION_DIRS", str(tmp_path))
    monkeypatch.setenv("AGENT_EXTRA_MODEL_CLIENTS", "reentrant_client")

    loaded = model_client_extensions.load_extra_model_clients()

    assert len(loaded) == 1
    assert "llm_TestExternalReentrant" in get_client_registry().list_clients()


def test_failed_import_rolls_back_registered_clients(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from openjiuwen.core.common.clients import get_client_registry

    source = _client_source("TestExternalBroken") + "\nraise RuntimeError('broken')\n"
    _write_client(tmp_path, "partially_broken_client", source)
    monkeypatch.setenv("EXTENSION_DIRS", str(tmp_path))
    monkeypatch.setenv("AGENT_EXTRA_MODEL_CLIENTS", "partially_broken_client")

    assert model_client_extensions.load_extra_model_clients() == []
    assert model_client_extensions.load_extra_model_clients() == []
    assert "llm_TestExternalBroken" not in get_client_registry().list_clients()


def test_u_openai_example_preserves_business_metadata(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from openjiuwen.core.foundation.llm.schema.config import (
        ModelClientConfig,
        ModelRequestConfig,
    )

    root = Path(__file__).resolve().parents[3]
    source = (root / "examples/u_openai_model_client.py").read_text(encoding="utf-8")
    _write_client(tmp_path, "u_openai_model_client", source)
    monkeypatch.setenv("EXTENSION_DIRS", str(tmp_path))
    monkeypatch.setenv("AGENT_EXTRA_MODEL_CLIENTS", "u_openai_model_client")

    [module] = model_client_extensions.load_extra_model_clients()
    client = module.UOpenAIModelClient(
        ModelRequestConfig(model_name="test-model"),
        ModelClientConfig(
            client_provider="UOpenAI",
            api_base="https://example.invalid/v1",
            api_key="test-key",
            use_shared_llm_http_client=False,
        ),
    )
    business_metadata = {
        "servRespCd": "0000000000000000",
        "resCode": "0000",
        "servRespDescInfo": "success",
        "globalBusiTrackNo": "track-1",
    }
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content="ok", tool_calls=None),
                finish_reason="stop",
                token_ids=None,
                logprobs=None,
            )
        ],
        usage=None,
        id="response-1",
        model="test-model",
        model_extra=business_metadata,
    )

    message = asyncio.run(client._parse_response(response))

    assert message.provider_metadata == business_metadata

    error_response = SimpleNamespace(
        choices=[],
        model_extra={
            "servRespCd": "xxxxx01",
            "resCode": "xxxxxx02",
            "servRespDescInfo": "sensitive content",
            "globalBusiTrackNo": "track-2",
        },
    )
    with pytest.raises(Exception) as error:
        asyncio.run(client._parse_response(error_response))

    assert error.value.details["provider_metadata"] == error_response.model_extra


def test_deployment_wires_model_client_env_to_both_processes() -> None:
    root = Path(__file__).resolve().parents[3]
    agentserver_template = (
        root / "deploy/enterprise/templates/agentserver.template.env"
    ).read_text(encoding="utf-8")
    gateway_template = (
        root / "deploy/enterprise/templates/gateway.template.env"
    ).read_text(encoding="utf-8")

    assert "AGENT_EXTRA_MODEL_CLIENTS=<<AGENT_EXTRA_MODEL_CLIENTS>>" in (
        agentserver_template
    )
    assert "AGENT_EXTRA_MODEL_CLIENTS=<<AGENT_EXTRA_MODEL_CLIENTS>>" in gateway_template
    assert "AGENT_EXTRA_MODEL_CLIENTS" in SPAWN_ENV_KEYS
