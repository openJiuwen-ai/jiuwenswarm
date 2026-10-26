"""File transfer tests - upload performance and integrity."""

from __future__ import annotations

import hashlib
import random
import time

import pytest

from tests.system_tests.test_config import TestConfig


@pytest.mark.system
@pytest.mark.performance
@pytest.mark.slow
class TestFileTransfer:
    """File transfer test cases."""

    @staticmethod
    def _generate_test_data(size_mb: int) -> bytes:
        return random.randbytes(size_mb * 1024 * 1024)

    @staticmethod
    def _calculate_sha256(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    @staticmethod
    def test_ft_001_file_transfer_1m(client, wait_for_sandbox_ready):
        resp = client.post("/api/v1/sandboxes", json={})
        assert resp.status_code == 201, f"Failed to create sandbox: {resp.text}"
        sandbox_id = resp.json()["id"]
        wait_for_sandbox_ready(sandbox_id)

        file_data = TestFileTransfer._generate_test_data(1)
        original_hash = TestFileTransfer._calculate_sha256(file_data)

        upload_start = time.time()
        resp = client.post(
            f"/api/v1/sandboxes/{sandbox_id}/upload?sandbox_path=/tmp/test_1m.bin",
            files={"file": ("test_1m.bin", file_data, "application/octet-stream")},
            timeout=TestConfig.FILE_TRANSFER_TIMEOUT,
        )
        upload_time = time.time() - upload_start
        assert resp.status_code == 204
        upload_speed = 1 / upload_time
        assert upload_speed >= TestConfig.FILE_UPLOAD_SPEED_THRESHOLD

        resp = client.post(
            f"/api/v1/sandboxes/{sandbox_id}/exec",
            json={"command": ["sha256sum", "/tmp/test_1m.bin"], "timeout_seconds": 10},
        )
        assert resp.status_code == 200
        output = (resp.json().get("stdout") or "").strip()
        parts = output.split()
        assert parts, "sha256sum returned empty output"
        downloaded_hash = parts[0]
        assert downloaded_hash == original_hash

    @staticmethod
    def test_ft_002_file_transfer_10m(client, wait_for_sandbox_ready):
        resp = client.post("/api/v1/sandboxes", json={})
        assert resp.status_code == 201, f"Failed to create sandbox: {resp.text}"
        sandbox_id = resp.json()["id"]
        wait_for_sandbox_ready(sandbox_id)

        file_data = TestFileTransfer._generate_test_data(10)
        original_hash = TestFileTransfer._calculate_sha256(file_data)

        upload_start = time.time()
        resp = client.post(
            f"/api/v1/sandboxes/{sandbox_id}/upload?sandbox_path=/tmp/test_10m.bin",
            files={"file": ("test_10m.bin", file_data, "application/octet-stream")},
            timeout=TestConfig.FILE_TRANSFER_TIMEOUT,
        )
        upload_time = time.time() - upload_start
        assert resp.status_code == 204
        upload_speed = 10 / upload_time
        assert upload_speed >= TestConfig.FILE_UPLOAD_SPEED_THRESHOLD

        resp = client.post(
            f"/api/v1/sandboxes/{sandbox_id}/exec",
            json={"command": ["sha256sum", "/tmp/test_10m.bin"], "timeout_seconds": 10},
        )
        assert resp.status_code == 200
        output = (resp.json().get("stdout") or "").strip()
        parts = output.split()
        assert parts, "sha256sum returned empty output"
        downloaded_hash = parts[0]
        assert downloaded_hash == original_hash

    @staticmethod
    def test_ft_003_file_transfer_50m(client, wait_for_sandbox_ready):
        resp = client.post("/api/v1/sandboxes", json={})
        assert resp.status_code == 201, f"Failed to create sandbox: {resp.text}"
        sandbox_id = resp.json()["id"]
        wait_for_sandbox_ready(sandbox_id)

        file_data = TestFileTransfer._generate_test_data(50)
        original_hash = TestFileTransfer._calculate_sha256(file_data)

        upload_start = time.time()
        resp = client.post(
            f"/api/v1/sandboxes/{sandbox_id}/upload?sandbox_path=/tmp/test_50m.bin",
            files={"file": ("test_50m.bin", file_data, "application/octet-stream")},
            timeout=TestConfig.FILE_TRANSFER_TIMEOUT,
        )
        upload_time = time.time() - upload_start
        assert resp.status_code == 204
        upload_speed = 50 / upload_time
        assert upload_speed >= TestConfig.FILE_UPLOAD_SPEED_THRESHOLD

        resp = client.post(
            f"/api/v1/sandboxes/{sandbox_id}/exec",
            json={"command": ["sha256sum", "/tmp/test_50m.bin"], "timeout_seconds": 10},
        )
        assert resp.status_code == 200
        output = (resp.json().get("stdout") or "").strip()
        parts = output.split()
        assert parts, "sha256sum returned empty output"
        downloaded_hash = parts[0]
        assert downloaded_hash == original_hash
