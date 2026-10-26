"""Resource degradation matrix tests."""

from __future__ import annotations

import time

import pytest

from tests.system_tests.test_utils import ResourceDegradationManager


@pytest.mark.resource_degradation
@pytest.mark.system
@pytest.mark.slow
class TestResourceDegradationCPU:
    """CPU resource degradation tests."""

    @staticmethod
    def test_rd_001_cpu_mild_degradation(client, wait_for_sandbox_ready, exec_command):
        resp = client.post("/api/v1/sandboxes", json={})
        sandbox_id = resp.json()["id"]
        wait_for_sandbox_ready(sandbox_id)

        rdm = ResourceDegradationManager()
        try:
            rdm.degrade_cpu(cores=4, duration=30)
            resp = exec_command(sandbox_id, ["echo", "cpu-test"], timeout_seconds=30)
            assert resp.status_code == 200
            assert resp.json()["exit_code"] == 0
        finally:
            rdm.cleanup()

    @staticmethod
    def test_rd_002_cpu_moderate_degradation(client, wait_for_sandbox_ready, exec_command):
        resp = client.post("/api/v1/sandboxes", json={})
        sandbox_id = resp.json()["id"]
        wait_for_sandbox_ready(sandbox_id)

        rdm = ResourceDegradationManager()
        try:
            rdm.degrade_cpu(cores=8, duration=30)
            resp = exec_command(sandbox_id, ["echo", "cpu-test"], timeout_seconds=60)
            assert resp.status_code == 200
            assert resp.json()["exit_code"] == 0
        finally:
            rdm.cleanup()

    @staticmethod
    def test_rd_003_cpu_severe_degradation(client, wait_for_sandbox_ready, exec_command):
        resp = client.post("/api/v1/sandboxes", json={})
        sandbox_id = resp.json()["id"]
        wait_for_sandbox_ready(sandbox_id)

        rdm = ResourceDegradationManager()
        try:
            rdm.degrade_cpu(cores=12, duration=30)
            resp = exec_command(sandbox_id, ["echo", "cpu-test"], timeout_seconds=120)
            assert resp.status_code == 200
            assert resp.json()["exit_code"] == 0
        finally:
            rdm.cleanup()


@pytest.mark.resource_degradation
@pytest.mark.system
@pytest.mark.slow
class TestResourceDegradationMemory:
    """Memory resource degradation tests."""

    @staticmethod
    def test_rd_004_memory_mild_degradation(client, wait_for_sandbox_ready, exec_command):
        resp = client.post("/api/v1/sandboxes", json={})
        sandbox_id = resp.json()["id"]
        wait_for_sandbox_ready(sandbox_id)

        rdm = ResourceDegradationManager()
        try:
            rdm.degrade_memory(vm_bytes="8G", duration=30)
            resp = exec_command(sandbox_id, ["echo", "memory-test"], timeout_seconds=30)
            assert resp.status_code == 200
            assert resp.json()["exit_code"] == 0
        finally:
            rdm.cleanup()

    @staticmethod
    def test_rd_005_memory_moderate_degradation(client, wait_for_sandbox_ready, exec_command):
        resp = client.post("/api/v1/sandboxes", json={})
        sandbox_id = resp.json()["id"]
        wait_for_sandbox_ready(sandbox_id)

        rdm = ResourceDegradationManager()
        try:
            rdm.degrade_memory(vm_bytes="16G", duration=30)
            resp = exec_command(sandbox_id, ["echo", "memory-test"], timeout_seconds=60)
            assert resp.status_code == 200
            assert resp.json()["exit_code"] == 0
        finally:
            rdm.cleanup()

    @staticmethod
    def test_rd_006_memory_severe_degradation(client, wait_for_sandbox_ready, exec_command):
        resp = client.post("/api/v1/sandboxes", json={})
        sandbox_id = resp.json()["id"]
        wait_for_sandbox_ready(sandbox_id)

        rdm = ResourceDegradationManager()
        try:
            rdm.degrade_memory(vm_bytes="20G", duration=30)
            resp = exec_command(sandbox_id, ["echo", "memory-test"], timeout_seconds=120)
            assert resp.status_code == 200
            assert resp.json()["exit_code"] == 0
        finally:
            rdm.cleanup()


@pytest.mark.network_degradation
@pytest.mark.system
@pytest.mark.slow
class TestResourceDegradationNetwork:
    """Network resource degradation tests."""

    @staticmethod
    def test_rd_007_network_mild_degradation(client, wait_for_sandbox_ready, exec_command):
        resp = client.post("/api/v1/sandboxes", json={})
        sandbox_id = resp.json()["id"]
        wait_for_sandbox_ready(sandbox_id)

        rdm = ResourceDegradationManager()
        try:
            rdm.degrade_network(loss=1, delay=50)
            resp = exec_command(sandbox_id, ["echo", "network-test"], timeout_seconds=30)
            assert resp.status_code == 200
            assert resp.json()["exit_code"] == 0
        finally:
            rdm.restore_network()
            rdm.cleanup()

    @staticmethod
    def test_rd_008_network_moderate_degradation(client, wait_for_sandbox_ready, exec_command):
        resp = client.post("/api/v1/sandboxes", json={})
        sandbox_id = resp.json()["id"]
        wait_for_sandbox_ready(sandbox_id)

        rdm = ResourceDegradationManager()
        try:
            rdm.degrade_network(loss=5, delay=100)
            resp = exec_command(sandbox_id, ["echo", "network-test"], timeout_seconds=60)
            assert resp.status_code == 200
            assert resp.json()["exit_code"] == 0
        finally:
            rdm.restore_network()
            rdm.cleanup()

    @staticmethod
    def test_rd_009_network_severe_degradation(client, wait_for_sandbox_ready, exec_command):
        resp = client.post("/api/v1/sandboxes", json={})
        sandbox_id = resp.json()["id"]
        wait_for_sandbox_ready(sandbox_id)

        rdm = ResourceDegradationManager()
        try:
            rdm.degrade_network(loss=10, delay=200)
            resp = exec_command(sandbox_id, ["echo", "network-test"], timeout_seconds=120)
            assert resp.status_code == 200
            assert resp.json()["exit_code"] == 0
        finally:
            rdm.restore_network()
            rdm.cleanup()


@pytest.mark.resource_degradation
@pytest.mark.system
@pytest.mark.slow
class TestResourceDegradationDisk:
    """Disk resource degradation tests."""

    @staticmethod
    def test_rd_010_disk_mild_degradation(client, wait_for_sandbox_ready, exec_command):
        resp = client.post("/api/v1/sandboxes", json={})
        sandbox_id = resp.json()["id"]
        wait_for_sandbox_ready(sandbox_id)

        rdm = ResourceDegradationManager()
        try:
            rdm.degrade_disk(path="/tmp", size="1G", duration=30)
            resp = exec_command(sandbox_id, ["echo", "disk-test"], timeout_seconds=30)
            assert resp.status_code == 200
            assert resp.json()["exit_code"] == 0
        finally:
            rdm.restore_disk()
            rdm.cleanup()

    @staticmethod
    def test_rd_011_disk_moderate_degradation(client, wait_for_sandbox_ready, exec_command):
        resp = client.post("/api/v1/sandboxes", json={})
        sandbox_id = resp.json()["id"]
        wait_for_sandbox_ready(sandbox_id)

        rdm = ResourceDegradationManager()
        try:
            rdm.degrade_disk(path="/tmp", size="3G", duration=30)
            resp = exec_command(sandbox_id, ["echo", "disk-test"], timeout_seconds=60)
            assert resp.status_code == 200
            assert resp.json()["exit_code"] == 0
        finally:
            rdm.restore_disk()
            rdm.cleanup()

    @staticmethod
    def test_rd_012_disk_severe_degradation(client, wait_for_sandbox_ready, exec_command):
        resp = client.post("/api/v1/sandboxes", json={})
        sandbox_id = resp.json()["id"]
        wait_for_sandbox_ready(sandbox_id)

        rdm = ResourceDegradationManager()
        try:
            rdm.degrade_disk(path="/tmp", size="5G", duration=30)
            resp = exec_command(sandbox_id, ["echo", "disk-test"], timeout_seconds=120)
            assert resp.status_code == 200
            assert resp.json()["exit_code"] == 0
        finally:
            rdm.restore_disk()
            rdm.cleanup()


@pytest.mark.resource_degradation
@pytest.mark.network_degradation
@pytest.mark.system
@pytest.mark.slow
class TestResourceDegradationCombined:
    """Combined resource degradation tests."""

    @staticmethod
    def test_rd_013_degradation_business_flow(client, wait_for_sandbox_ready, exec_command):
        resp = client.post("/api/v1/sandboxes", json={})
        sandbox_id = resp.json()["id"]
        wait_for_sandbox_ready(sandbox_id)

        rdm = ResourceDegradationManager()
        try:
            rdm.degrade_cpu(cores=8, duration=60)
            rdm.degrade_memory(vm_bytes="16G", duration=60)
            rdm.degrade_network(loss=5, delay=100)

            for i in range(5):
                resp = exec_command(sandbox_id, ["echo", f"test-{i}"], timeout_seconds=60)
                assert resp.status_code == 200
                assert resp.json()["exit_code"] == 0
        finally:
            rdm.restore_network()
            rdm.restore_disk()
            rdm.cleanup()

    @staticmethod
    def test_rd_014_degradation_concurrent(client, wait_for_sandbox_ready, exec_command):
        sandboxes = []
        for _ in range(3):
            resp = client.post("/api/v1/sandboxes", json={})
            sandboxes.append(resp.json()["id"])

        for sb_id in sandboxes:
            wait_for_sandbox_ready(sb_id)

        rdm = ResourceDegradationManager()
        try:
            rdm.degrade_cpu(cores=10, duration=30)

            from concurrent.futures import ThreadPoolExecutor, as_completed

            def run_test(sb_id):
                return exec_command(sb_id, ["echo", "concurrent-test"], timeout_seconds=60)

            with ThreadPoolExecutor(max_workers=3) as pool:
                futures = [pool.submit(run_test, sb_id) for sb_id in sandboxes]
                for future in as_completed(futures):
                    resp = future.result()
                    assert resp.status_code == 200
                    assert resp.json()["exit_code"] == 0
        finally:
            rdm.cleanup()