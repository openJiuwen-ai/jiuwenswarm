import json
from types import SimpleNamespace

import pytest

from scripts.model_compatibility_smoke import (
    ProviderConfig,
    build_report,
    load_provider_config,
)


def test_provider_config_repr_hides_api_key():
    config = ProviderConfig(
        provider="deepseek",
        api_base="https://api.deepseek.com",
        model="deepseek-flash",
        api_key="secret-value",
    )

    assert "secret-value" not in repr(config)


def test_load_provider_config_requires_api_key(tmp_path):
    env_file = tmp_path / ".env.model-smoke.local"
    env_file.write_text(
        "DEEPSEEK_API_BASE=https://api.deepseek.com\n"
        "DEEPSEEK_MODEL=deepseek-flash\n"
        "DEEPSEEK_API_KEY=\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="DEEPSEEK_API_KEY"):
        load_provider_config("deepseek", env_file)


def test_build_report_keeps_usage_and_omits_model_content():
    secret_reply = "content-that-must-not-be-serialized"
    chat_response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=secret_reply))],
        usage=SimpleNamespace(prompt_tokens=7, completion_tokens=3, total_tokens=10),
    )
    tool_response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content=None,
                    tool_calls=[
                        SimpleNamespace(function=SimpleNamespace(name="record_probe"))
                    ],
                )
            )
        ],
        usage=SimpleNamespace(prompt_tokens=11, completion_tokens=4, total_tokens=15),
    )

    report = build_report(
        provider="deepseek",
        api_base="https://api.deepseek.com",
        model="deepseek-flash",
        model_listed=True,
        chat_response=chat_response,
        tool_response=tool_response,
        chat_latency_ms=120,
        tool_latency_ms=180,
    )
    serialized = json.dumps(report)

    assert report["status"] == "PASS"
    assert report["checks"]["chat"]["usage"] == {
        "input_tokens": 7,
        "output_tokens": 3,
        "total_tokens": 10,
    }
    assert report["checks"]["tool_call"]["called_tool"] == "record_probe"
    assert secret_reply not in serialized
    assert "messages" not in serialized


def test_build_report_fails_when_expected_tool_is_not_called():
    chat_response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))],
        usage=None,
    )
    tool_response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="ok", tool_calls=[]))],
        usage=None,
    )

    report = build_report(
        provider="deepseek",
        api_base="https://api.deepseek.com",
        model="deepseek-flash",
        model_listed=True,
        chat_response=chat_response,
        tool_response=tool_response,
        chat_latency_ms=100,
        tool_latency_ms=100,
    )

    assert report["status"] == "FAIL"
    assert report["checks"]["tool_call"]["status"] == "FAIL"
