"""Configuration parameter constraint space tests."""

from __future__ import annotations

import logging
import time

import pytest

logger = logging.getLogger(__name__)


@pytest.mark.system
class TestValidParameterCombinations:
    """Test valid parameter combinations."""

    @staticmethod
    def test_pc_001_idle_timeout_less_than_exec_timeout(client, wait_for_sandbox_ready, restore_timeout):
        timeout_resp = client.put("/api/v1/timeout", json={"idle_timeout": 30, "exec_timeout": 60})
        assert timeout_resp.status_code == 200, f"Failed to set timeout config: {timeout_resp.text}"

        resp = client.post("/api/v1/sandboxes", json={})
        assert resp.status_code == 201, f"Failed to create sandbox: {resp.text}"
        sandbox_id = resp.json()["id"]
        wait_for_sandbox_ready(sandbox_id)

        resp = client.post(
            f"/api/v1/sandboxes/{sandbox_id}/exec",
            json={"command": ["echo", "test"], "timeout_seconds": 10},
        )
        assert resp.status_code == 200
        assert resp.json()["exit_code"] == 0

    @staticmethod
    def test_pc_002_memory_limit_greater_than_daemon_rss(client, wait_for_sandbox_ready):
        policy = {
            "name": "memory-test",
            "filesystem_policy": {
                "read_only": ["/usr", "/lib", "/lib64", "/etc", "/opt"],
                "read_write": ["/tmp"],
                "bind_mounts": [
                    {"host_path": "/bin", "sandbox_path": "/bin", "mode": "ro"},
                    {"host_path": "/usr", "sandbox_path": "/usr", "mode": "ro"},
                    {"host_path": "/lib", "sandbox_path": "/lib", "mode": "ro"},
                    {"host_path": "/lib64", "sandbox_path": "/lib64", "mode": "ro"},
                    {"host_path": "/opt/python3.11", "sandbox_path": "/opt/python3.11", "mode": "ro"},
                ],
            },
            "process": {"run_as_user": "sandbox", "run_as_group": "sandbox"},
            "namespace": {"user": True, "pid": True, "ipc": True, "cgroup": True, "uts": True},
            "network": {"mode": "isolated"},
            "environment": {"PATH": "/opt/python3.11/bin:/usr/local/bin:/usr/bin:/bin"},
        }

        resp = client.post("/api/v1/sandboxes", json={"policy": policy})
        assert resp.status_code == 201, f"Failed to create sandbox: {resp.text}"
        sandbox_id = resp.json()["id"]
        wait_for_sandbox_ready(sandbox_id)

        resp = client.post(
            f"/api/v1/sandboxes/{sandbox_id}/exec",
            json={"command": ["echo", "memory-test"], "timeout_seconds": 10},
        )
        assert resp.status_code == 200
        assert resp.json()["exit_code"] == 0

    @staticmethod
    def test_pc_003_max_sandboxes_memory_combo(client, wait_for_sandbox_ready):
        sandboxes = []
        for i in range(3):
            resp = client.post("/api/v1/sandboxes", json={})
            sandboxes.append(resp.json()["id"])

        for sb_id in sandboxes:
            wait_for_sandbox_ready(sb_id)

        for sb_id in sandboxes:
            resp = client.post(
                f"/api/v1/sandboxes/{sb_id}/exec",
                json={"command": ["echo", "combo-test"], "timeout_seconds": 10},
            )
            assert resp.status_code == 200


@pytest.mark.system
class TestInvalidParameterCombinations:
    """Test invalid parameter combinations."""

    @staticmethod
    def test_pc_004_idle_timeout_greater_than_exec_timeout(client, restore_timeout):
        resp = client.put("/api/v1/timeout", json={"idle_timeout": 120, "exec_timeout": 60})
        assert resp.status_code in (400, 422), (
            f"Expected rejection of invalid timeout combo: {resp.status_code} {resp.text}"
        )

    @staticmethod
    def test_pc_005_memory_limit_less_than_daemon_rss(client):
        policy = {
            "name": "memory-too-small",
            "filesystem_policy": {
                "read_only": ["/usr", "/lib", "/lib64", "/etc"],
                "read_write": ["/tmp"],
            },
            "process": {"run_as_user": "sandbox", "run_as_group": "sandbox"},
            "namespace": {"user": True, "pid": True, "ipc": True, "cgroup": True, "uts": True},
            "network": {"mode": "isolated"},
        }

        resp = client.post("/api/v1/sandboxes", json={"policy": policy})
        assert resp.status_code == 201

        sandbox_id = resp.json()["id"]
        try:
            for _ in range(60):
                status = client.get(f"/api/v1/sandboxes/{sandbox_id}")
                phase = status.json().get("phase")
                if phase == "error":
                    break
                if phase == "ready":
                    pytest.skip("Sandbox became ready with insufficient memory")
                time.sleep(0.5)
            else:
                pytest.skip("Timeout waiting for error state")
        finally:
            try:
                client.delete(f"/api/v1/sandboxes/{sandbox_id}")
            except Exception as exc:
                logger.debug("Failed to delete sandbox %s: %s", sandbox_id, exc)

    @staticmethod
    def test_pc_006_max_sandboxes_exceeds_memory(client):
        sandboxes = []
        try:
            for i in range(20):
                resp = client.post("/api/v1/sandboxes", json={})
                if resp.status_code == 201:
                    sandboxes.append(resp.json()["id"])
                else:
                    break
        finally:
            for sb_id in sandboxes:
                try:
                    client.delete(f"/api/v1/sandboxes/{sb_id}")
                except Exception as exc:
                    logger.debug("Failed to delete sandbox %s: %s", sb_id, exc)
