"""Error boundary tests."""

from __future__ import annotations

import time

import pytest


@pytest.mark.system
class TestErrorBoundaries:
    """Error boundary tests."""

    @staticmethod
    def test_eb_001_permission_denied_in_sandbox(client, wait_for_sandbox_ready, exec_command):
        resp = client.post("/api/v1/sandboxes", json={})
        sandbox_id = resp.json()["id"]
        wait_for_sandbox_ready(sandbox_id)

        resp = exec_command(
            sandbox_id,
            ["touch", "/etc/passwd.new"],
            timeout_seconds=10,
        )
        assert resp.status_code == 200
        assert resp.json()["exit_code"] != 0

    @staticmethod
    def test_eb_002_permission_denied_on_host(client, wait_for_sandbox_ready, exec_command):
        resp = client.post("/api/v1/sandboxes", json={})
        sandbox_id = resp.json()["id"]
        wait_for_sandbox_ready(sandbox_id)

        # Attempt to create a directory in a restricted path;
        # mkdir should fail with permission denied (non-zero exit code)
        resp = exec_command(
            sandbox_id,
            ["mkdir", "-p", "/root/test"],
            timeout_seconds=10,
        )
        assert resp.status_code == 200
        assert resp.json()["exit_code"] != 0, \
            "Expected permission denied when creating /root/test"

    @staticmethod
    def test_eb_003_disk_full_during_upload(client, wait_for_sandbox_ready):
        resp = client.post("/api/v1/sandboxes", json={})
        sandbox_id = resp.json()["id"]
        wait_for_sandbox_ready(sandbox_id)

        from tests.system_tests.test_utils import ResourceDegradationManager

        rdm = ResourceDegradationManager()
        try:
            rdm.degrade_disk(path="/tmp", size="20G", duration=60)
            large_content = b"x" * (100 * 1024 * 1024)
            resp = client.post(
                f"/api/v1/sandboxes/{sandbox_id}/files",
                files={"file": ("large_file.bin", large_content, "application/octet-stream")},
            )
            assert resp.status_code in (400, 413, 500, 507)
        finally:
            rdm.restore_disk()
            rdm.cleanup()

    @staticmethod
    def test_eb_004_disk_full_during_create(client):
        from tests.system_tests.test_utils import ResourceDegradationManager

        rdm = ResourceDegradationManager()
        try:
            rdm.degrade_disk(path="/tmp", size="20G", duration=60)
            resp = client.post("/api/v1/sandboxes", json={})
            assert resp.status_code in (500, 507)
        finally:
            rdm.restore_disk()
            rdm.cleanup()

    @staticmethod
    def test_eb_005_ipc_timeout(client, wait_for_sandbox_ready, exec_command):
        resp = client.post("/api/v1/sandboxes", json={})
        sandbox_id = resp.json()["id"]
        wait_for_sandbox_ready(sandbox_id)

        from tests.system_tests.test_utils import ResourceDegradationManager

        rdm = ResourceDegradationManager()
        try:
            rdm.degrade_cpu(cores=12, duration=30)
            resp = exec_command(
                sandbox_id,
                ["sleep", "3"],
                timeout_seconds=5,
            )
            assert resp.status_code == 200
        finally:
            rdm.cleanup()

    @staticmethod
    def test_eb_006_network_timeout(client, wait_for_sandbox_ready, exec_command):
        resp = client.post("/api/v1/sandboxes", json={})
        sandbox_id = resp.json()["id"]
        wait_for_sandbox_ready(sandbox_id)

        from tests.system_tests.test_utils import ResourceDegradationManager

        rdm = ResourceDegradationManager()
        try:
            rdm.degrade_network(loss=50, delay=500)
            resp = exec_command(
                sandbox_id,
                ["curl", "--connect-timeout", "5", "https://www.baidu.com"],
                timeout_seconds=30,
            )
            assert resp.status_code in (200, 408)
        finally:
            rdm.restore_network()
            rdm.cleanup()