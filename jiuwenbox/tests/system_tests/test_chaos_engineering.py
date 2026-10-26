"""Chaos engineering tests for resilience testing."""

from __future__ import annotations

import time

import pytest

from tests.system_tests.test_config import TestConfig
from tests.system_tests.test_utils import ChaosInjector, PerformanceCollector


@pytest.mark.chaos
@pytest.mark.system
@pytest.mark.slow
class TestChaosEngineering:
    """Chaos engineering injection tests."""

    @staticmethod
    def test_ce_001_kill_daemon_during_ipc(client, wait_for_sandbox_ready):
        resp = client.post("/api/v1/sandboxes", json={})
        sandbox_id = resp.json()["id"]
        wait_for_sandbox_ready(sandbox_id)

        chaos = ChaosInjector(client)
        chaos.kill_sandbox_daemon(sandbox_id)

        time.sleep(5)
        status = client.get(f"/api/v1/sandboxes/{sandbox_id}")
        assert status.json()["phase"] == "ready"

    @staticmethod
    def test_ce_002_network_disconnect_during_command(client, wait_for_sandbox_ready, exec_command):
        resp = client.post("/api/v1/sandboxes", json={})
        sandbox_id = resp.json()["id"]
        wait_for_sandbox_ready(sandbox_id)

        chaos = ChaosInjector(client)
        chaos.disconnect_network()

        try:
            resp = exec_command(sandbox_id, ["echo", "test"], timeout_seconds=10)
            assert resp.status_code in (200, 408)
        finally:
            chaos.reconnect_network()

        time.sleep(5)
        status = client.get(f"/api/v1/sandboxes/{sandbox_id}")
        assert status.json()["phase"] == "ready"

    @staticmethod
    def test_ce_003_cpu_exhaustion_during_exec(client, wait_for_sandbox_ready, exec_command):
        resp = client.post("/api/v1/sandboxes", json={})
        sandbox_id = resp.json()["id"]
        wait_for_sandbox_ready(sandbox_id)

        from tests.system_tests.test_utils import ResourceDegradationManager

        rdm = ResourceDegradationManager()
        try:
            rdm.degrade_cpu(cores=12, duration=60)
            resp = exec_command(sandbox_id, ["sleep", "5"], timeout_seconds=120)
            assert resp.status_code == 200
        finally:
            rdm.cleanup()

    @staticmethod
    def test_ce_004_disk_full_during_upload(client, wait_for_sandbox_ready):
        resp = client.post("/api/v1/sandboxes", json={})
        sandbox_id = resp.json()["id"]
        wait_for_sandbox_ready(sandbox_id)

        from tests.system_tests.test_utils import ResourceDegradationManager

        rdm = ResourceDegradationManager()
        try:
            rdm.degrade_disk(path="/tmp", size="10G", duration=60)
            resp = client.post(
                f"/api/v1/sandboxes/{sandbox_id}/files",
                files={"file": ("test.txt", b"test content", "text/plain")},
            )
            assert resp.status_code in (200, 400, 500)
        finally:
            rdm.restore_disk()
            rdm.cleanup()

    @staticmethod
    def test_ce_005_oom_during_concurrent_exec(client, wait_for_sandbox_ready, exec_command):
        sandboxes = []
        for _ in range(3):
            resp = client.post("/api/v1/sandboxes", json={})
            sandboxes.append(resp.json()["id"])

        for sb_id in sandboxes:
            wait_for_sandbox_ready(sb_id)

        from tests.system_tests.test_utils import ResourceDegradationManager

        rdm = ResourceDegradationManager()
        chaos = ChaosInjector(client)
        try:
            rdm.degrade_memory(vm_bytes="22G", duration=60)
            for sb_id in sandboxes:
                chaos.trigger_oom(sb_id, memory_gb=22)

            for sb_id in sandboxes:
                resp = exec_command(sb_id, ["echo", "oom-test"], timeout_seconds=60)
                assert resp.status_code in (200, 500)
        finally:
            rdm.cleanup()

    @staticmethod
    def test_ce_006_memory_pagetable_pollution(client, wait_for_sandbox_ready, exec_command):
        resp = client.post("/api/v1/sandboxes", json={})
        sandbox_id = resp.json()["id"]
        wait_for_sandbox_ready(sandbox_id)

        from tests.system_tests.test_utils import ResourceDegradationManager

        rdm = ResourceDegradationManager()
        try:
            rdm.degrade_memory(vm_bytes="18G", duration=60)
            resp = exec_command(sandbox_id, ["echo", "pagetable-test"], timeout_seconds=60)
            assert resp.status_code == 200
        finally:
            rdm.cleanup()

    @staticmethod
    def test_ce_007_circuit_breaker_healing(client, wait_for_sandbox_ready, exec_command):
        resp = client.post("/api/v1/sandboxes", json={})
        sandbox_id = resp.json()["id"]
        wait_for_sandbox_ready(sandbox_id)

        chaos = ChaosInjector(client)
        for _ in range(3):
            chaos.kill_sandbox_daemon(sandbox_id)
            time.sleep(2)

        time.sleep(10)
        resp = exec_command(sandbox_id, ["echo", "healing-test"], timeout_seconds=30)
        assert resp.status_code == 200
        assert resp.json()["exit_code"] == 0

    @staticmethod
    def test_ce_008_p999_latency_spike(client, wait_for_sandbox_ready, exec_command):
        resp = client.post("/api/v1/sandboxes", json={})
        sandbox_id = resp.json()["id"]
        wait_for_sandbox_ready(sandbox_id)

        collector = PerformanceCollector()
        chaos = ChaosInjector(client)

        for _ in range(10):
            start = time.time()
            exec_command(sandbox_id, ["echo", "normal"], timeout_seconds=10)
            collector.record_latency(time.time() - start)

        chaos.kill_sandbox_daemon(sandbox_id)
        time.sleep(2)

        for _ in range(5):
            start = time.time()
            exec_command(sandbox_id, ["echo", "recovery"], timeout_seconds=30)
            collector.record_latency(time.time() - start)

        metrics = collector.get_metrics()
        assert metrics.p999 < TestConfig.P999_SPIKE_THRESHOLD