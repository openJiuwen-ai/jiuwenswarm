"""Performance metrics tests."""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import pytest

from tests.system_tests.test_config import TestConfig
from tests.system_tests.test_utils import PerformanceCollector

logger = logging.getLogger(__name__)


@pytest.mark.system
@pytest.mark.performance
@pytest.mark.slow
class TestPerformanceMetrics:
    """Performance metrics tests."""

    @staticmethod
    def test_perf_001_qps_single_sandbox(client, wait_for_sandbox_ready):
        resp = client.post("/api/v1/sandboxes", json={})
        sandbox_id = resp.json()["id"]
        wait_for_sandbox_ready(sandbox_id)

        collector = PerformanceCollector()
        collector.start()

        for _ in range(100):
            start = time.time()
            resp = client.post(
                f"/api/v1/sandboxes/{sandbox_id}/exec",
                json={"command": ["echo", "test"], "timeout_seconds": 10},
            )
            assert resp.status_code == 200
            collector.record_latency(time.time() - start)

        collector.stop()
        metrics = collector.get_metrics()
        assert metrics.qps >= TestConfig.QPS_SINGLE_THRESHOLD

    @staticmethod
    def test_perf_002_qps_multi_sandbox(client, wait_for_sandbox_ready):
        sandboxes = []
        for _ in range(3):
            resp = client.post("/api/v1/sandboxes", json={})
            sandboxes.append(resp.json()["id"])

        for sb_id in sandboxes:
            wait_for_sandbox_ready(sb_id)

        collector = PerformanceCollector()
        collector.start()

        def run_command(sb_id):
            start = time.time()
            resp = client.post(
                f"/api/v1/sandboxes/{sb_id}/exec",
                json={"command": ["echo", "test"], "timeout_seconds": 10},
            )
            assert resp.status_code == 200
            return time.time() - start

        with ThreadPoolExecutor(max_workers=3) as pool:
            for _ in range(30):
                futures = [pool.submit(run_command, sb_id) for sb_id in sandboxes]
                for future in as_completed(futures):
                    collector.record_latency(future.result())

        collector.stop()
        metrics = collector.get_metrics()
        assert metrics.qps >= TestConfig.QPS_MULTI_THRESHOLD

    @staticmethod
    def test_perf_003_command_execution_latency(client, wait_for_sandbox_ready):
        resp = client.post("/api/v1/sandboxes", json={})
        sandbox_id = resp.json()["id"]
        wait_for_sandbox_ready(sandbox_id)

        latencies = []
        for _ in range(50):
            start = time.time()
            resp = client.post(
                f"/api/v1/sandboxes/{sandbox_id}/exec",
                json={"command": ["echo", "latency-test"], "timeout_seconds": 10},
            )
            assert resp.status_code == 200
            latencies.append(time.time() - start)

        avg_latency = sum(latencies) / len(latencies)
        assert avg_latency < TestConfig.LATENCY_COMMAND_THRESHOLD

    @staticmethod
    def test_perf_004_sandbox_startup_latency(client):
        latencies = []
        for _ in range(10):
            start = time.time()
            resp = client.post("/api/v1/sandboxes", json={})
            assert resp.status_code == 201
            sandbox_id = resp.json()["id"]

            for _ in range(20):
                status = client.get(f"/api/v1/sandboxes/{sandbox_id}")
                if status.json()["phase"] == "ready":
                    break
                time.sleep(0.05)

            latencies.append(time.time() - start)

        avg_latency = sum(latencies) / len(latencies)
        assert avg_latency < TestConfig.LATENCY_STARTUP_THRESHOLD

    @staticmethod
    def test_perf_005_service_startup_latency(client):
        health_start = time.time()
        for _ in range(20):
            try:
                resp = client.get("/health")
                if resp.status_code == 200:
                    break
            except Exception as exc:
                logger.debug("Health check failed: %s", exc)
            time.sleep(0.1)
        else:
            pytest.fail("Service did not become healthy")

        service_latency = time.time() - health_start
        assert service_latency < TestConfig.LATENCY_SERVICE_THRESHOLD

    @staticmethod
    def test_perf_006_file_upload_download_latency(client, wait_for_sandbox_ready):
        resp = client.post("/api/v1/sandboxes", json={})
        sandbox_id = resp.json()["id"]
        wait_for_sandbox_ready(sandbox_id)

        test_content = b"test" * (10 * 1024)

        upload_start = time.time()
        resp = client.post(
            f"/api/v1/sandboxes/{sandbox_id}/upload?sandbox_path=/tmp/test.txt",
            files={"file": ("test.txt", test_content, "text/plain")},
        )
        assert resp.status_code == 204
        upload_latency = time.time() - upload_start

        download_start = time.time()
        resp = client.get(f"/api/v1/sandboxes/{sandbox_id}/download?sandbox_path=/tmp/test.txt")
        assert resp.status_code == 200
        download_latency = time.time() - download_start

        assert upload_latency < 0.5
        assert download_latency < 0.5

    @staticmethod
    def test_perf_007_p999_latency_spike(client, wait_for_sandbox_ready):
        resp = client.post("/api/v1/sandboxes", json={})
        sandbox_id = resp.json()["id"]
        wait_for_sandbox_ready(sandbox_id)

        collector = PerformanceCollector()

        for _ in range(100):
            start = time.time()
            resp = client.post(
                f"/api/v1/sandboxes/{sandbox_id}/exec",
                json={"command": ["echo", "spike-test"], "timeout_seconds": 10},
            )
            assert resp.status_code == 200
            collector.record_latency(time.time() - start)

        metrics = collector.get_metrics()
        assert metrics.p999 < TestConfig.P999_SPIKE_THRESHOLD