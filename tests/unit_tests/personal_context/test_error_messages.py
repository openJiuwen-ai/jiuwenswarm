from __future__ import annotations

import pytest


@pytest.mark.parametrize(
    "code,zh,en",
    [
        (154000, "配置", "configuration"),
        (154001, "状态", "state"),
        (154002, "文件", "file"),
        (154003, "数据来源", "source"),
        (154004, "模型", "model"),
        (154005, "写入", "write"),
        (154006, "超时", "timed out"),
        (999999, "重试", "retry"),
    ],
)
def test_error_codes_provide_actionable_messages_in_both_languages(code, zh, en):
    from jiuwenswarm.server.personal_context.error_messages import localize_error

    for language, expected in (("zh", zh), ("en", en)):
        assert expected in localize_error(
            {"code": code, "message": "token=secret"}, language
        )
        assert "secret" not in localize_error(
            {"code": code, "message": "token=secret"}, language
        )


def test_serialized_history_errors_use_only_stable_code_prefix():
    from jiuwenswarm.server.personal_context.error_messages import localize_payload

    payload = {
        "fetch_service_errors": {"one": "[154003] raw English"},
        "last_error": {
            "code": 154006,
            "status": "CONTEXT_PROACTIVE_RUNTIME_TIMEOUT",
            "message": "raw",
            "operation": "stop",
        },
    }
    translated = localize_payload(payload, "zh")
    assert "数据来源" in translated["fetch_service_errors"]["one"]
    assert "超时" in translated["last_error"]["message"]
    assert translated["last_error"]["code"] == 154006
    assert translated["last_error"]["operation"] == "stop"
    assert payload["last_error"]["message"] == "raw"


@pytest.mark.parametrize(
    "text",
    [
        "api_key=private-value",
        '{"token": "private-value"}',
        "Authorization: Bearer private-value",
        "Authorization: Basic private-value",
        "cookie=session=private-value",
        "Cookie: first=one; session=private-value",
        "credential: private-value",
        "https://name:private-value@example.invalid/path?token=private-value",
    ],
)
def test_error_logs_redact_credential_values(monkeypatch, text):
    from jiuwenswarm.server.personal_context import error_messages

    messages = []
    monkeypatch.setattr(
        error_messages._LOGGER,
        "warning",
        lambda fmt, *args: messages.append(fmt % args),
    )
    error_messages.log_error(RuntimeError(text))
    assert messages
    assert "private-value" not in messages[0]
