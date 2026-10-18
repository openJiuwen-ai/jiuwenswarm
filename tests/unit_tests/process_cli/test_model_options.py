# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Model flags reuse catalog grammar and never persist connection credentials."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest

from jiuwenswarm.channels.process_cli import app, model_options
from jiuwenswarm.channels.process_cli.main import build_parser
from jiuwenswarm.common import config
from jiuwenswarm.common.runtime_model_config import runtime_model_config


@pytest.fixture
def entries(monkeypatch):
    configured = [
        {
            "alias": "primary",
            "is_default": True,
            "model_client_config": {
                "model_name": "model-a",
                "client_provider": "OpenAI",
                "api_base": "https://old.test/v1",
                "api_key": "old-secret",
                "endpoint_profile": "old-dialect",
                "verify_ssl": False,
                "custom_headers": {
                    "Authorization": "Bearer old-secret",
                    "API-Key": "old-api-key",
                    "X-API-KEY": "old-x-api-key",
                    "Proxy-Authorization": "Bearer old-proxy-key",
                    "X-Title": "app",
                },
            },
            "model_config_obj": {"temperature": 0.2},
        },
        {
            "alias": "secondary",
            "model_client_config": {
                "model_name": "model-a",
                "client_provider": "Anthropic",
                "api_base": "https://second.test",
                "api_key": "second-secret",
            },
        },
    ]
    monkeypatch.setattr(config, "get_default_models", lambda: deepcopy(configured))
    monkeypatch.setattr(model_options, "_validate_connection", lambda entry: None)
    monkeypatch.setenv("CLI_TEST_KEY", "new-secret")
    return configured


def _prepare(*flags):
    args = build_parser().parse_args(["hello", *flags])
    key = model_options.validate_model_options(args)
    return args, model_options.prepare_model_options(args, key)


@pytest.mark.parametrize("flag", ["-h", "--help", "-help"])
def test_help_includes_model_connection_flags(flag, capsys):
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args([flag])
    assert exc.value.code == 0
    text = capsys.readouterr().out
    for option in ("--model", "--api-base", "--api-key-env", "--model-provider"):
        assert option in text
    assert "--_model-selection" not in text


def test_default_does_not_load_configuration(monkeypatch):
    monkeypatch.setattr(
        config, "get_default_models", lambda: pytest.fail("unexpected read")
    )
    args, entry = _prepare()
    assert entry is None and not args.model_selection


@pytest.mark.parametrize(
    "selector,key",
    [
        ("model-a", "model-a#0"),
        ("primary", "model-a#0"),
        ("secondary", "model-a#1"),
        ("model-a#1", "model-a#1"),
    ],
)
def test_configured_selectors_preserve_global_index(entries, selector, key):
    args, entry = _prepare("-m", selector)
    assert entry is None and args.model_selection == key


def test_existing_internal_repl_selector_is_preserved():
    args, entry = _prepare("--_model-selection", "model-a#1")
    assert args.model_selection == "model-a#1" and entry is None


def test_key_override_keeps_selected_endpoint_without_old_auth_header(entries):
    before = deepcopy(entries)
    args, entry = _prepare("--model", "primary", "--api-key-env", "CLI_TEST_KEY")
    client = entry["model_client_config"]
    assert client["api_base"] == "https://old.test/v1"
    assert client["api_key"] == "new-secret"
    assert client["custom_headers"] == {"X-Title": "app"}
    assert entry["model_config_obj"] == {"temperature": 0.2}
    assert args.model_selection == "model-a" and entries == before
    assert "new-secret" not in repr(args)


def test_new_destination_does_not_forward_old_connection_settings(entries):
    args, entry = _prepare(
        "--model",
        "secondary",
        "--api-base",
        "https://new.test/v1",
        "--api-key-env",
        "CLI_TEST_KEY",
        "--model-provider",
        "OpenAI",
    )
    assert entry["model_client_config"] == {
        "model_name": "model-a",
        "client_provider": "OpenAI",
        "api_base": "https://new.test/v1",
        "api_key": "new-secret",
        "verify_ssl": True,
    }
    assert args.model_selection == "model-a" and entry["is_default"]


def test_unknown_model_with_complete_connection_is_supported(entries):
    args, entry = _prepare(
        "-m",
        "new-model",
        "--api-base",
        "http://127.0.0.1:8000/v1",
        "--api-key-env",
        "CLI_TEST_KEY",
    )
    assert entry["model_client_config"]["client_provider"] == "OpenAI"
    assert args.model_selection == "new-model"


@pytest.mark.parametrize(
    "flags",
    [
        ["--model", "missing"],
        [
            "--model",
            "model-a#10",
            "--api-base",
            "https://new.test/v1",
            "--api-key-env",
            "CLI_TEST_KEY",
        ],
        [
            "--model",
            "audio",
            "--api-base",
            "https://new.test/v1",
            "--api-key-env",
            "CLI_TEST_KEY",
        ],
    ],
)
def test_invalid_selector_does_not_fall_back(entries, flags):
    with pytest.raises(model_options.ModelOptionsError):
        _prepare(*flags)


def test_ambiguous_alias_does_not_create_new_model(entries, monkeypatch):
    entries[0]["alias"] = entries[1]["alias"] = "ambiguous"
    with pytest.raises(model_options.ModelOptionsError):
        _prepare(
            "-m",
            "ambiguous",
            "--api-base",
            "https://new.test/v1",
            "--api-key-env",
            "CLI_TEST_KEY",
        )


@pytest.mark.parametrize(
    "flags",
    [
        ["--api-base", "https://new.test/v1"],
        ["--api-key-env", "CLI_TEST_KEY"],
        ["--model", " "],
        ["--model", ""],
        ["--model", "model-a", "--api-base", "https://new.test/v1"],
        ["--model", "model-a", "--model-provider", "OpenAI"],
        ["--model", "model-a", "--api-key-env", "sk-private-key"],
        ["--model", "model-a", "--api-key-env", "UNSET_KEY_FOR_TEST"],
    ],
)
def test_invalid_combinations_have_secret_free_errors(entries, flags, monkeypatch):
    monkeypatch.delenv("UNSET_KEY_FOR_TEST", raising=False)
    with pytest.raises(model_options.ModelOptionsError) as exc:
        _prepare(*flags)
    assert "new-secret" not in str(exc.value)
    assert "old-secret" not in str(exc.value)
    assert "sk-private-key" not in str(exc.value)


@pytest.mark.parametrize(
    "url",
    [
        "file:///tmp/model",
        "https://user:private-key@host.test/v1",
        "https://host.test/v1?key=private-key",
        "https://host.test/v1#private-key",
        "https://[invalid",
        "http://host:0/v1",
        "http://host:999999/v1",
        "https://host.test/a b",
    ],
)
def test_invalid_api_address_is_not_echoed(entries, url):
    with pytest.raises(model_options.ModelOptionsError) as exc:
        _prepare("-m", "model-a", "--api-base", url, "--api-key-env", "CLI_TEST_KEY")
    assert url not in str(exc.value) and "private-key" not in str(exc.value)


@pytest.mark.parametrize("value", ["", " ", "key\r\nvalue"])
def test_empty_or_multiline_key_is_rejected(entries, monkeypatch, value):
    monkeypatch.setenv("CLI_TEST_KEY", value)
    with pytest.raises(model_options.ModelOptionsError):
        _prepare("-m", "model-a", "--api-key-env", "CLI_TEST_KEY")


def test_model_flags_require_noninteractive_prompt(entries):
    args = build_parser().parse_args(["--model", "model-a"])
    with pytest.raises(model_options.ModelOptionsError):
        model_options.validate_model_options(args)


def test_capture_key_prevents_later_dotenv_override(entries, monkeypatch):
    args = build_parser().parse_args(
        ["hello", "-m", "model-a", "--api-key-env", "CLI_TEST_KEY"]
    )
    key = model_options.validate_model_options(args)
    monkeypatch.setenv("CLI_TEST_KEY", "dotenv-secret")
    entry = model_options.prepare_model_options(args, key)
    assert entry["model_client_config"]["api_key"] == "new-secret"


def test_execution_request_contains_no_connection_secrets(entries, tmp_path):
    args, entry = _prepare(
        "-m",
        "new-model",
        "--api-base",
        "https://new.test/v1",
        "--api-key-env",
        "CLI_TEST_KEY",
    )
    args.cwd = str(tmp_path)
    request = app._build_request(args, session_id="session", request_id="request")
    serialized = json.dumps(request.params)
    assert request.params["model_name"] == "new-model"
    for value in (
        "new-secret",
        "api_key",
        "api_base",
        "CLI_TEST_KEY",
        "https://new.test/v1",
    ):
        assert value not in serialized
    with runtime_model_config(entry):
        assert (
            config.get_config()["models"]["defaults"][0]["model_client_config"][
                "api_key"
            ]
            == "new-secret"
        )


def test_invalid_connection_failure_is_secret_free(entries, monkeypatch):
    from jiuwenswarm.server.runtime.agent_adapter import interface_deep

    monkeypatch.undo()

    def fail(*args):
        raise ValueError("api_key=private-validation-secret")

    monkeypatch.setattr(interface_deep, "build_model_from_entry", fail)
    with pytest.raises(model_options.ModelOptionsError) as exc:
        model_options._validate_connection({})
    assert "private-validation-secret" not in str(exc.value)


@pytest.mark.parametrize("provider", ["OpenAI", "Anthropic"])
def test_explicit_provider_uses_real_core_connection_validation(monkeypatch, provider):
    monkeypatch.setattr(config, "get_default_models", lambda: [])
    monkeypatch.setenv("CLI_PROVIDER_KEY", "dummy-provider-test-key")
    args = build_parser().parse_args(
        [
            "hello",
            "--model",
            "new-model",
            "--api-base",
            "http://127.0.0.1:8000/v1",
            "--api-key-env",
            "CLI_PROVIDER_KEY",
            "--model-provider",
            provider,
        ]
    )
    key = model_options.validate_model_options(args)
    entry = model_options.prepare_model_options(args, key)
    assert entry["model_client_config"]["client_provider"] == provider


@pytest.mark.parametrize(
    "entry",
    [["--query-json", "missing.json"], ["--run-json", "missing.json"], ["--run-jsonl"]],
)
def test_model_flags_cannot_enter_machine_protocol_before_validation(tmp_path, entry):
    script = """
import importlib.abc
import sys
class Guard(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith(('jiuwenswarm.runtime', 'jiuwenswarm.common.config')):
            raise AssertionError('execution imported before argument validation')
sys.meta_path.insert(0, Guard())
from jiuwenswarm.channels.process_cli.main import main
main()
"""
    env = dict(os.environ, PYTHONUTF8="1", JIUWENSWARM_DATA_DIR=str(tmp_path))
    result = subprocess.run(
        [sys.executable, "-c", script, *entry, "--model", "new-model"],
        input="",
        capture_output=True,
        encoding="utf-8",
        env=env,
        cwd=Path(__file__).resolve().parents[3],
        timeout=15,
        check=False,
    )
    assert result.returncode == 2, result.stderr
    assert "execution imported" not in result.stderr
