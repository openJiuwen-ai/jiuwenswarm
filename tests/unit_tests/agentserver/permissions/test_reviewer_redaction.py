# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for shared AutoReviewer evidence redaction."""

from __future__ import annotations

# TEST ONLY: credential-shaped values are synthetic redaction fixtures. URLs use
# RFC-reserved domains and are parsed only; no external request is performed.

import json

from jiuwenswarm.agents.harness.common.rails.permissions.reviewer_redaction import (
    PERMISSION_UI_REDACTED,
    PERMISSION_UI_TRUNCATED,
    PERMISSION_UI_UNAVAILABLE,
    redact_json_value,
    redact_reviewable_payload_text,
    redact_text,
    redact_url,
    sanitize_permission_ui_payload,
)


def test_redact_text_removes_auth_values_before_assignment_redaction() -> None:
    bearer_value = "A" * 36
    basic_value = "B" * 32
    redacted = redact_text(
        f"Authorization: Bearer {bearer_value} "
        f"authorization=Basic {basic_value}"
    )

    assert bearer_value not in redacted
    assert basic_value not in redacted
    assert "[redacted]" in redacted


def test_redact_text_removes_private_key_assignment_blocks() -> None:
    key_kind = "PRIVATE " + "KEY"
    key_payload = "TEST_ONLY_" + ("K" * 24)
    redacted = redact_text(
        f"private_key: -----BEGIN {key_kind}----- "
        f"{key_payload} "
        f"-----END {key_kind}-----"
    )

    assert f"BEGIN {key_kind}" not in redacted
    assert key_payload not in redacted
    assert f"END {key_kind}" not in redacted
    assert "[redacted]" in redacted


def test_redact_text_preserves_task_relevance_words() -> None:
    redacted = redact_text(
        "Grep is task-relevant and task-related for the current request."
    )

    assert redacted == (
        "Grep is task-relevant and task-related for the current request."
    )
    assert "[redacted]" not in redacted


def test_redact_text_removes_long_sk_tokens() -> None:
    provider_token = "sk-" + ("T" * 36)
    redacted = redact_text(f"api token {provider_token} is present")

    assert provider_token not in redacted
    assert "[redacted]" in redacted


def test_redact_text_removes_cross_platform_user_local_paths() -> None:
    redacted = redact_text(
        r"/home/test-user/.ssh/id_rsa ~/.aws/credentials "
        r"C:\Users\TestUser\.aws\credentials C:/Users/TestUser/Documents/report.md"
    )

    assert "/home/test-user" not in redacted
    assert "~/.aws" not in redacted
    assert r"C:\Users\TestUser" not in redacted
    assert "C:/Users/TestUser" not in redacted
    assert redacted.count("[path]") == 4


def test_redact_reviewable_payload_text_redacts_system_paths() -> None:
    redacted = redact_reviewable_payload_text(
        "open('/etc/passwd').read(); print('/var/log/system.log')"
    )

    assert "[path]" in redacted
    assert "/etc" not in redacted
    assert "passwd" not in redacted
    assert "/var" not in redacted
    assert "system.log" not in redacted


def test_redact_reviewable_payload_text_redacts_workspace_tmp_and_unc_paths() -> None:
    redacted = redact_reviewable_payload_text(
        r"cat /tmp/private-cache /workspace/session-output "
        r"C:\Temp\report.txt \\fileserver\share\quarterly-plan"
    )

    assert redacted.count("[path]") == 4
    assert "/tmp" not in redacted
    assert "private-cache" not in redacted
    assert "/workspace" not in redacted
    assert "session-output" not in redacted
    assert r"C:\Temp" not in redacted
    assert "fileserver" not in redacted
    assert "quarterly-plan" not in redacted


def test_redact_reviewable_payload_text_preserves_non_file_slashes() -> None:
    redacted = redact_reviewable_payload_text(
        r"endpoint='/api/v1/search'; pattern=r'/\d+/'"
    )

    assert "/api/v1/search" in redacted
    assert r"/\d+/" in redacted


def test_redact_reviewable_payload_text_covers_shell_path_shapes() -> None:
    redacted = redact_reviewable_payload_text(
        'cat "/Users/test-user/My Secrets/.env" '
        r"/Users/test-user/My\ Secrets/token.txt "
        'python "scripts/create_ppt.py --out outputs/report.pptx',
        redact_relative_paths=True,
    )

    assert redacted.count("[path]") == 4
    for fragment in (
        "/Users/test-user",
        "My Secrets",
        ".env",
        "token.txt",
        "create_ppt.py",
        "report.pptx",
    ):
        assert fragment not in redacted


def test_redact_url_removes_userinfo_and_sensitive_query_values() -> None:
    redacted = redact_url(
        "https://test-user:TEST_ONLY_PASSWORD@example.invalid/docs/open?safe=topic"
        "&oauth_token=TEST_ONLY_TOKEN&signature=TEST_ONLY_SIGNATURE"
        "&code=TEST_ONLY_CODE#token=TEST_ONLY_FRAGMENT"
    )

    assert redacted.startswith("https://example.invalid/docs/open?")
    assert "test-user" not in redacted
    assert "TEST_ONLY_PASSWORD" not in redacted
    assert "TEST_ONLY_TOKEN" not in redacted
    assert "TEST_ONLY_SIGNATURE" not in redacted
    assert "TEST_ONLY_CODE" not in redacted
    assert "TEST_ONLY_FRAGMENT" not in redacted
    assert "safe=topic" in redacted
    assert "#token=[redacted]" in redacted
    assert "[redacted]" in redacted


def test_redact_url_never_exposes_file_uri_path() -> None:
    redacted = redact_url("file:///etc/.env")

    assert redacted == "file://[redacted-path]"
    assert "/etc/.env" not in redacted


def test_reviewable_payload_text_redacts_embedded_file_uri() -> None:
    redacted = redact_reviewable_payload_text(
        '{"callbackUrl":"file:///root/.ssh/id_ed25519"}'
    )

    assert "file://[redacted-path]" in redacted
    assert "/root/.ssh/id_ed25519" not in redacted


def test_permission_ui_redacts_nested_file_uri() -> None:
    redacted = sanitize_permission_ui_payload(
        {"params": {"callbackUrl": "file:///workspace/.env"}}
    )

    assert redacted["params"]["callbackUrl"] == "file://[redacted-path]"
    assert "/workspace/.env" not in str(redacted)


def test_redact_json_value_preserves_url_safe_query_after_redaction() -> None:
    value = {
        "target_urls": [
            (
                "https://test-user:TEST_ONLY_PASSWORD@example.invalid/docs/open?"
                "oauth_token=TEST_ONLY_TOKEN"
                "&signature=TEST_ONLY_SIGNATURE&safe=topic"
            )
        ]
    }

    redacted = redact_json_value(value)

    assert redacted == {
        "target_urls": [
            (
                "https://example.invalid/docs/open?"
                "oauth_token=[redacted]&signature=[redacted]&safe=topic"
            )
        ]
    }


def test_permission_ui_payload_redacts_secret_key_variants_recursively() -> None:
    synthetic_values = {
        "db_password": "TEST_ONLY_DB_PASSWORD",
        "dbPassword": "TEST_ONLY_DB_PASSWORD_CAMEL",
        "x-api-key": "TEST_ONLY_API_KEY",
        "x-auth-token": "TEST_ONLY_AUTH_TOKEN",
        "authHeader": "TEST_ONLY_AUTH_HEADER",
        "proxyAuth": "TEST_ONLY_PROXY_AUTH_SHORT",
        "clientSecret": "TEST_ONLY_CLIENT_SECRET",
        "accessKey": "TEST_ONLY_ACCESS_KEY",
        "awsAccessKeyId": "TEST_ONLY_AWS_ACCESS_KEY",
        "signingKey": "TEST_ONLY_SIGNING_KEY",
        "Authorization": "TEST_ONLY_AUTHORIZATION",
        "proxy-authorization": "TEST_ONLY_PROXY_AUTHORIZATION",
        "cookie": "TEST_ONLY_COOKIE",
        "set-cookie": "TEST_ONLY_SET_COOKIE",
    }
    payload = {
        "db_password": synthetic_values["db_password"],
        "dbPassword": synthetic_values["dbPassword"],
        "x-api-key": synthetic_values["x-api-key"],
        "x-auth-token": synthetic_values["x-auth-token"],
        "authHeader": synthetic_values["authHeader"],
        "proxyAuth": synthetic_values["proxyAuth"],
        "clientSecret": synthetic_values["clientSecret"],
        "accessKey": synthetic_values["accessKey"],
        "awsAccessKeyId": synthetic_values["awsAccessKeyId"],
        "signingKey": synthetic_values["signingKey"],
        "headers": {
            "Authorization": synthetic_values["Authorization"],
            "proxy-authorization": synthetic_values["proxy-authorization"],
            "cookie": synthetic_values["cookie"],
            "set-cookie": synthetic_values["set-cookie"],
        },
        "nested": {"credentials": {"username": "test-user", "value": "plain"}},
        "query": "public search terms",
    }

    redacted = sanitize_permission_ui_payload(payload)

    assert redacted["query"] == "public search terms"
    assert redacted["db_password"] == PERMISSION_UI_REDACTED
    assert redacted["dbPassword"] == PERMISSION_UI_REDACTED
    assert redacted["x-api-key"] == PERMISSION_UI_REDACTED
    assert redacted["x-auth-token"] == PERMISSION_UI_REDACTED
    assert redacted["authHeader"] == PERMISSION_UI_REDACTED
    assert redacted["proxyAuth"] == PERMISSION_UI_REDACTED
    assert redacted["clientSecret"] == PERMISSION_UI_REDACTED
    assert redacted["accessKey"] == PERMISSION_UI_REDACTED
    assert redacted["awsAccessKeyId"] == PERMISSION_UI_REDACTED
    assert redacted["signingKey"] == PERMISSION_UI_REDACTED
    assert set(redacted["headers"].values()) == {PERMISSION_UI_REDACTED}
    assert redacted["nested"]["credentials"] == PERMISSION_UI_REDACTED
    serialized = json.dumps(redacted)
    for secret in synthetic_values.values():
        assert secret not in serialized


def test_permission_ui_payload_redacts_url_credentials_and_query_secrets() -> None:
    redacted = sanitize_permission_ui_payload(
        {
            "url": (
                "https://test-user:TEST_ONLY_PASSWORD@example.invalid/open?safe=topic"
                "&access_token=TEST_ONLY_TOKEN&signature=TEST_ONLY_SIGNATURE"
            )
        }
    )

    assert redacted["url"].startswith("https://example.invalid/open?")
    assert "safe=topic" in redacted["url"]
    assert "test-user" not in redacted["url"]
    assert "TEST_ONLY_TOKEN" not in redacted["url"]
    assert "TEST_ONLY_SIGNATURE" not in redacted["url"]
    assert PERMISSION_UI_REDACTED in redacted["url"]


def test_permission_ui_payload_omits_secret_context_at_every_depth() -> None:
    redacted = sanitize_permission_ui_payload(
        {
            "secret_context": "outer-secret",
            "nested": {"secretContext": "inner-secret", "safe": "visible"},
        }
    )

    assert redacted == {"nested": {"safe": "visible"}}


def test_permission_ui_payload_marks_all_structural_limits() -> None:
    redacted = sanitize_permission_ui_payload(
        {
            "deep": {"level": {"value": "hidden-by-depth"}},
            "many": [1, 2, 3, 4],
            "long": "x" * 80,
        },
        max_depth=2,
        max_items=3,
        max_string_length=32,
        max_total_bytes=1024,
    )

    assert redacted["deep"]["level"] == PERMISSION_UI_TRUNCATED
    assert redacted["many"][-1] == PERMISSION_UI_TRUNCATED
    assert redacted["long"].endswith(PERMISSION_UI_TRUNCATED)


def test_permission_ui_payload_total_size_includes_truncation_marker() -> None:
    redacted = sanitize_permission_ui_payload(
        {"items": [f"value-{index}-{'x' * 40}" for index in range(20)]},
        max_total_bytes=192,
    )
    serialized = json.dumps(redacted, ensure_ascii=False, separators=(",", ":"))

    assert len(serialized.encode("utf-8")) <= 192
    assert PERMISSION_UI_TRUNCATED in serialized


def test_permission_ui_payload_handles_cycles_and_non_finite_numbers() -> None:
    cyclic: dict[str, object] = {}
    cyclic["self"] = cyclic

    assert sanitize_permission_ui_payload(cyclic) == {"self": PERMISSION_UI_UNAVAILABLE}
    assert sanitize_permission_ui_payload(
        {"nan": float("nan"), "positive": float("inf"), "negative": -float("inf")}
    ) == {
        "nan": PERMISSION_UI_UNAVAILABLE,
        "positive": PERMISSION_UI_UNAVAILABLE,
        "negative": PERMISSION_UI_UNAVAILABLE,
    }


def test_permission_ui_payload_never_stringifies_unsupported_values() -> None:
    class SecretObject:
        def __str__(self) -> str:
            return "RAW-CUSTOM-SECRET"

    class ThrowingString:
        def __str__(self) -> str:
            raise RuntimeError("must not escape")

    redacted = sanitize_permission_ui_payload(
        {
            "safe": "visible",
            "opaque": SecretObject(),
            "throwing": ThrowingString(),
            "bytes": b"RAW-BYTE-SECRET",
            "buffer": bytearray(b"RAW-BUFFER-SECRET"),
            "set": {"RAW-SET-SECRET"},
        }
    )

    assert redacted == {
        "safe": "visible",
        "opaque": PERMISSION_UI_UNAVAILABLE,
        "throwing": PERMISSION_UI_UNAVAILABLE,
        "bytes": PERMISSION_UI_UNAVAILABLE,
        "buffer": PERMISSION_UI_UNAVAILABLE,
        "set": PERMISSION_UI_UNAVAILABLE,
    }
    serialized = json.dumps(redacted)
    assert "RAW-" not in serialized


def test_permission_ui_payload_never_stringifies_non_string_mapping_keys() -> None:
    class SecretKey:
        def __str__(self) -> str:
            return "RAW-CUSTOM-KEY-SECRET"

    redacted = sanitize_permission_ui_payload(
        {SecretKey(): "hidden", 7: "also-hidden", "safe": True}
    )

    assert redacted == {
        "[UNAVAILABLE]": PERMISSION_UI_UNAVAILABLE,
        "[UNAVAILABLE] (2)": PERMISSION_UI_UNAVAILABLE,
        "safe": True,
    }
    assert "RAW-CUSTOM-KEY-SECRET" not in json.dumps(redacted)
