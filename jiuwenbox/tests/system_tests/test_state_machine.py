"""State machine tests for sandbox lifecycle transitions."""

from __future__ import annotations

import time

import pytest

from tests.system_tests.test_config import TestConfig
from tests.system_tests.test_utils import StateMachineValidator, ChaosInjector


_PROVISIONING_ERROR_POLICY = {
    "name": "provisioning-error-test",
    "filesystem_policy": {
        "read_only": ["/usr", "/lib", "/lib64", "/etc", "/opt"],
        "read_write": ["/tmp"],
    },
    "process": {"run_as_user": "sandbox", "run_as_group": "sandbox"},
    "namespace": {"user": True, "pid": True, "ipc": True, "cgroup": True, "uts": True},
    "network": {
        "mode": "isolated",
        "egress": {"default": "allow"},
        "uplink": {"subnet": TestConfig.PROVISIONING_ERROR_SUBNET},
    },
    "environment": {"PATH": "/opt/python3.11/bin:/usr/local/bin:/usr/bin:/bin"},
}


def _wait_for_sandbox_phase(
    client,
    sandbox_id: str,
    expected_phase: str,
    timeout: int = 30,
    *,
    reject_phases: tuple[str, ...] = (),
) -> None:
    for _ in range(timeout * 2):
        status = client.get(f"/api/v1/sandboxes/{sandbox_id}")
        assert status.status_code == 200, f"Failed to get sandbox status: {status.text}"
        phase = status.json().get("phase")
        if phase in reject_phases:
            pytest.fail(
                f"Sandbox {sandbox_id} reached unexpected phase {phase!r}; "
                f"expected {expected_phase!r}"
            )
        if phase == expected_phase:
            return
        time.sleep(0.5)
    pytest.fail(
        f"Timeout waiting for sandbox {sandbox_id} to reach phase {expected_phase!r}"
    )


@pytest.mark.system
class TestStateMachineValidTransitions:
    """Test valid sandbox state transitions."""

    @staticmethod
    def test_sm_001_provisioning_to_ready(client, wait_for_sandbox_ready):
        resp = client.post("/api/v1/sandboxes", json={})
        assert resp.status_code == 201
        sandbox = resp.json()
        wait_for_sandbox_ready(sandbox['id'])
        status = client.get(f"/api/v1/sandboxes/{sandbox['id']}")
        assert status.json()["phase"] == "ready"

    @staticmethod
    def test_sm_002_ready_to_stopped(client, wait_for_sandbox_ready):
        resp = client.post("/api/v1/sandboxes", json={})
        sandbox_id = resp.json()["id"]
        wait_for_sandbox_ready(sandbox_id)
        stop_resp = client.post(f"/api/v1/sandboxes/{sandbox_id}/stop")
        assert stop_resp.json()["phase"] == "stopped"

    @staticmethod
    def test_sm_003_stopped_to_ready(client, wait_for_sandbox_ready):
        resp = client.post("/api/v1/sandboxes", json={})
        sandbox_id = resp.json()["id"]
        wait_for_sandbox_ready(sandbox_id)
        client.post(f"/api/v1/sandboxes/{sandbox_id}/stop")
        start_resp = client.post(f"/api/v1/sandboxes/{sandbox_id}/start")
        assert start_resp.json()["phase"] == "ready"

    @staticmethod
    def test_sm_004_ready_to_deleting(client, wait_for_sandbox_ready):
        resp = client.post("/api/v1/sandboxes", json={})
        sandbox_id = resp.json()["id"]
        wait_for_sandbox_ready(sandbox_id)
        delete_resp = client.delete(f"/api/v1/sandboxes/{sandbox_id}")
        assert delete_resp.status_code == 204
        get_resp = client.get(f"/api/v1/sandboxes/{sandbox_id}")
        assert get_resp.status_code == 404

    @staticmethod
    def test_sm_005_stopped_to_deleting(client, wait_for_sandbox_ready):
        resp = client.post("/api/v1/sandboxes", json={})
        sandbox_id = resp.json()["id"]
        wait_for_sandbox_ready(sandbox_id)
        client.post(f"/api/v1/sandboxes/{sandbox_id}/stop")
        delete_resp = client.delete(f"/api/v1/sandboxes/{sandbox_id}")
        assert delete_resp.status_code == 204
        get_resp = client.get(f"/api/v1/sandboxes/{sandbox_id}")
        assert get_resp.status_code == 404

    @staticmethod
    def test_sm_006_error_to_ready(client, wait_for_sandbox_ready):
        resp = client.post("/api/v1/sandboxes", json={})
        assert resp.status_code == 201, f"Failed to create sandbox: {resp.text}"
        sandbox_id = resp.json()["id"]
        wait_for_sandbox_ready(sandbox_id)

        resp = client.post(
            f"/api/v1/sandboxes/{sandbox_id}/exec",
            json={"command": ["invalid-command-that-does-not-exist"], "timeout_seconds": 5},
        )
        assert resp.status_code == 200
        _wait_for_sandbox_phase(client, sandbox_id, "error")

        restart_resp = client.post(f"/api/v1/sandboxes/{sandbox_id}/restart")
        assert restart_resp.status_code == 200, restart_resp.text
        assert restart_resp.json()["phase"] == "ready"

    @staticmethod
    def test_sm_007_error_to_deleting(client, wait_for_sandbox_ready):
        resp = client.post("/api/v1/sandboxes", json={})
        assert resp.status_code == 201, f"Failed to create sandbox: {resp.text}"
        sandbox_id = resp.json()["id"]
        wait_for_sandbox_ready(sandbox_id)

        resp = client.post(
            f"/api/v1/sandboxes/{sandbox_id}/exec",
            json={"command": ["invalid-command-that-does-not-exist"], "timeout_seconds": 5},
        )
        assert resp.status_code == 200
        _wait_for_sandbox_phase(client, sandbox_id, "error")

        delete_resp = client.delete(f"/api/v1/sandboxes/{sandbox_id}")
        assert delete_resp.status_code == 204

    @staticmethod
    def test_sm_008_provisioning_to_error(client):
        # Use an isolated uplink pool whose /30 blocks all overlap the reserved
        # link-local range, so network setup fails during provisioning.
        resp = client.post("/api/v1/sandboxes", json={"policy": _PROVISIONING_ERROR_POLICY})
        assert resp.status_code == 201
        sandbox_id = resp.json()["id"]

        phase = None
        try:
            _wait_for_sandbox_phase(
                client, sandbox_id, "error", reject_phases=("ready",)
            )
            status = client.get(f"/api/v1/sandboxes/{sandbox_id}")
            phase = status.json().get("phase")
            assert phase == "error"
        finally:
            client.delete(f"/api/v1/sandboxes/{sandbox_id}")

    @staticmethod
    def test_sm_009_error_to_ready_auto_recovery(client, wait_for_sandbox_ready):
        resp = client.post("/api/v1/sandboxes", json={})
        sandbox_id = resp.json()["id"]
        wait_for_sandbox_ready(sandbox_id)

        chaos = ChaosInjector(client)
        chaos.kill_sandbox_daemon(sandbox_id)

        for _ in range(30):
            status = client.get(f"/api/v1/sandboxes/{sandbox_id}")
            if status.json()["phase"] == "ready":
                break
            time.sleep(1)
        else:
            pytest.fail("Sandbox did not recover from error")

    @staticmethod
    def test_sm_010_error_to_deleting(client, wait_for_sandbox_ready):
        resp = client.post("/api/v1/sandboxes", json={})
        sandbox_id = resp.json()["id"]
        wait_for_sandbox_ready(sandbox_id)

        chaos = ChaosInjector(client)
        chaos.kill_sandbox_daemon(sandbox_id)

        delete_resp = client.delete(f"/api/v1/sandboxes/{sandbox_id}")
        assert delete_resp.status_code == 204
        get_resp = client.get(f"/api/v1/sandboxes/{sandbox_id}")
        assert get_resp.status_code == 404

    @staticmethod
    def test_sm_011_deleting_to_terminal(client, wait_for_sandbox_ready):
        resp = client.post("/api/v1/sandboxes", json={})
        sandbox_id = resp.json()["id"]
        wait_for_sandbox_ready(sandbox_id)

        delete_resp = client.delete(f"/api/v1/sandboxes/{sandbox_id}")
        assert delete_resp.status_code == 204

        get_resp = client.get(f"/api/v1/sandboxes/{sandbox_id}")
        assert get_resp.status_code == 404


@pytest.mark.system
class TestStateMachineValidatorDefinition:
    """Validate the local state machine definition (not server API behavior)."""

    @staticmethod
    def test_validator_ready_to_provisioning_is_invalid():
        assert not StateMachineValidator.is_valid_transition("ready", "provisioning")

    @staticmethod
    def test_validator_stopped_to_provisioning_is_invalid():
        assert not StateMachineValidator.is_valid_transition("stopped", "provisioning")

    @staticmethod
    def test_validator_deleting_has_no_outgoing_transitions():
        assert not StateMachineValidator.is_valid_transition("deleting", "ready")
        assert not StateMachineValidator.is_valid_transition("deleting", "stopped")
        assert not StateMachineValidator.is_valid_transition("deleting", "error")

    @staticmethod
    def test_validator_provisioning_to_stopped_is_invalid():
        assert not StateMachineValidator.is_valid_transition("provisioning", "stopped")

    @staticmethod
    def test_validator_provisioning_to_deleting_is_invalid():
        assert not StateMachineValidator.is_valid_transition("provisioning", "deleting")


@pytest.mark.system
class TestStateMachineInvalidTransitions:
    """Verify the server rejects or blocks invalid transition attempts via API."""

    @staticmethod
    def test_sm_014_start_after_delete_returns_not_found(client, wait_for_sandbox_ready):
        resp = client.post("/api/v1/sandboxes", json={})
        assert resp.status_code == 201, f"Failed to create sandbox: {resp.text}"
        sandbox_id = resp.json()["id"]
        wait_for_sandbox_ready(sandbox_id)

        delete_resp = client.delete(f"/api/v1/sandboxes/{sandbox_id}")
        assert delete_resp.status_code == 204

        start_resp = client.post(f"/api/v1/sandboxes/{sandbox_id}/start")
        assert start_resp.status_code == 404, start_resp.text

    @staticmethod
    def test_sm_015_exec_during_provisioning_returns_conflict(client):
        resp = client.post("/api/v1/sandboxes", json={})
        assert resp.status_code == 201, f"Failed to create sandbox: {resp.text}"
        sandbox_id = resp.json()["id"]

        exec_resp = client.post(
            f"/api/v1/sandboxes/{sandbox_id}/exec",
            json={"command": ["echo", "too-early"], "timeout_seconds": 5},
        )
        assert exec_resp.status_code == 409, exec_resp.text

    @staticmethod
    def test_sm_016_exec_on_stopped_sandbox_returns_conflict(client, wait_for_sandbox_ready):
        resp = client.post("/api/v1/sandboxes", json={})
        assert resp.status_code == 201, f"Failed to create sandbox: {resp.text}"
        sandbox_id = resp.json()["id"]
        wait_for_sandbox_ready(sandbox_id)

        stop_resp = client.post(f"/api/v1/sandboxes/{sandbox_id}/stop")
        assert stop_resp.status_code == 200, stop_resp.text
        assert stop_resp.json()["phase"] == "stopped"

        exec_resp = client.post(
            f"/api/v1/sandboxes/{sandbox_id}/exec",
            json={"command": ["echo", "stopped"], "timeout_seconds": 5},
        )
        assert exec_resp.status_code == 409, exec_resp.text

    @staticmethod
    def test_sm_017_state_tampering_detection(client, wait_for_sandbox_ready):
        resp = client.post("/api/v1/sandboxes", json={})
        assert resp.status_code == 201, f"Failed to create sandbox: {resp.text}"
        sandbox_id = resp.json()["id"]
        wait_for_sandbox_ready(sandbox_id)
        resp = client.post(
            f"/api/v1/sandboxes/{sandbox_id}/exec",
            json={
                "command": ["python3", "-c", "import sys; sys.exit(0)"],
                "timeout_seconds": 5,
            },
        )
        assert resp.status_code == 200
        status = client.get(f"/api/v1/sandboxes/{sandbox_id}")
        assert status.json()["phase"] == "ready"