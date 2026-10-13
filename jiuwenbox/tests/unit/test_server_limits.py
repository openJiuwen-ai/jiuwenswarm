# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import asyncio

import pytest

from jiuwenbox.server.limits import SandboxLimitError, SandboxLimits, limit_asgi_receive


def test_limits_load_from_environment(monkeypatch):
    monkeypatch.setenv("JIUWENBOX_MAX_FILE_MB", "12")
    monkeypatch.setenv("JIUWENBOX_EXEC_TIMEOUT_SECONDS", "34")
    limits = SandboxLimits.from_env()
    assert limits.max_file_bytes == 12 * 1024 * 1024
    assert limits.effective_exec_timeout(None) == 34
    assert limits.effective_exec_timeout(5) == 5
    assert limits.effective_exec_timeout(100) == 34


@pytest.mark.parametrize(
    "name,value",
    [
        ("JIUWENBOX_MAX_FILE_MB", "0"),
        ("JIUWENBOX_MAX_FILE_MB", "bad"),
        ("JIUWENBOX_EXEC_TIMEOUT_SECONDS", "3601"),
    ],
)
def test_invalid_environment_is_rejected(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError):
        SandboxLimits.from_env()


def test_file_limit_has_structured_directional_errors():
    limits = SandboxLimits(max_file_mb=1)
    with pytest.raises(SandboxLimitError) as upload:
        limits.enforce_file_size(1024 * 1024 + 1, direction="upload")
    assert upload.value.status_code == 413
    assert upload.value.code == "SANDBOX_UPLOAD_TOO_LARGE"

    with pytest.raises(SandboxLimitError) as download:
        limits.enforce_file_size(1024 * 1024 + 1, direction="download")
    assert download.value.code == "SANDBOX_DOWNLOAD_TOO_LARGE"


def test_non_positive_exec_timeout_is_rejected():
    with pytest.raises(SandboxLimitError) as exc:
        SandboxLimits().effective_exec_timeout(0)
    assert exc.value.code == "SANDBOX_INVALID_TIMEOUT"


def test_internal_response_limits_are_enforced():
    limits = SandboxLimits()
    with pytest.raises(SandboxLimitError) as count:
        limits.enforce_result_count(10_001)
    assert count.value.code == "SANDBOX_RESULT_LIMIT_EXCEEDED"

    with pytest.raises(SandboxLimitError) as output:
        limits.enforce_exec_output("x" * (4 * 1024 * 1024 + 1), "")
    assert output.value.code == "SANDBOX_EXEC_OUTPUT_TOO_LARGE"


def test_chunked_upload_body_is_capped():
    async def receive():
        return {"type": "http.request", "body": b"abcdef", "more_body": False}

    limited = limit_asgi_receive(receive, 4)
    with pytest.raises(SandboxLimitError) as exc:
        asyncio.run(limited())
    assert exc.value.status_code == 413
    assert exc.value.code == "SANDBOX_UPLOAD_TOO_LARGE"


def test_chunked_json_request_is_capped():
    from fastapi.testclient import TestClient

    from jiuwenbox.server.app import create_app
    from jiuwenbox.server.limits import MAX_JSON_BODY_BYTES

    chunk = b" " * (256 * 1024)

    def body():
        yield b'{"command": ["cat"], "stdin_data": "'
        for _ in range(MAX_JSON_BODY_BYTES // len(chunk) + 1):
            yield chunk
        yield b'"}'

    client = TestClient(create_app(), raise_server_exceptions=False)
    response = client.post(
        "/api/v1/sandboxes/sb/exec",
        content=body(),
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 413
    assert response.json()["code"] == "SANDBOX_REQUEST_TOO_LARGE"


def test_chunked_upload_request_is_capped_over_http(monkeypatch):
    from fastapi.testclient import TestClient

    from jiuwenbox.server.app import create_app

    monkeypatch.setenv("JIUWENBOX_MAX_FILE_MB", "1")
    boundary = "jiuwenboxboundary"
    chunk = b"x" * (256 * 1024)

    def body():
        yield (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="file"; filename="big.bin"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode()
        for _ in range(12):
            yield chunk
        yield f"\r\n--{boundary}--\r\n".encode()

    client = TestClient(create_app(), raise_server_exceptions=False)
    response = client.post(
        "/api/v1/sandboxes/sb/upload?sandbox_path=/tmp/big.bin",
        content=body(),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )

    assert response.status_code == 413
    assert response.json()["code"] == "SANDBOX_UPLOAD_TOO_LARGE"
