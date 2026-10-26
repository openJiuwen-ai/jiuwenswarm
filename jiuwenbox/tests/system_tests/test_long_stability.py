"""Long stability tests with load."""

from __future__ import annotations

import logging
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List

import pytest

from tests.system_tests.test_config import TestConfig

logger = logging.getLogger(__name__)


def read_system_metrics_from_proc() -> dict:
    """Read CPU and memory metrics from /proc filesystem (no subprocess needed)."""
    try:
        # Read memory info from /proc/meminfo
        mem_total = 0.0
        mem_available = 0.0
        with open("/proc/meminfo", "r") as f:
            for line in f:
                parts = line.split()
                if parts[0] == "MemTotal:":
                    mem_total = float(parts[1])
                elif parts[0] == "MemAvailable:":
                    mem_available = float(parts[1])
                if mem_total > 0 and mem_available > 0:
                    break

        mem_used_percent = 0.0
        if mem_total > 0:
            mem_used_percent = ((mem_total - mem_available) / mem_total) * 100.0

        # Read CPU usage from /proc/stat
        with open("/proc/stat", "r") as f:
            line = f.readline()
        parts = line.split()[1:]  # skip "cpu" prefix
        # user, nice, system, idle, iowait, irq, softirq, steal, guest, guest_nice
        cpu_values = [float(v) for v in parts]
        idle = cpu_values[3] + (cpu_values[4] if len(cpu_values) > 4 else 0)
        total = sum(cpu_values)
        cpu_percent = ((total - idle) / total) * 100.0 if total > 0 else 0.0

        return {"cpu": cpu_percent, "memory": mem_used_percent}
    except Exception as exc:
        logger.debug("Failed to read system metrics from /proc: %s", exc)
        return {"cpu": 0.0, "memory": 0.0}


@pytest.mark.system
@pytest.mark.performance
@pytest.mark.slow
class TestLongStability:
    """Long stability test cases."""

    @staticmethod
    def get_system_metrics() -> dict:
        """Get current system CPU and memory metrics."""
        return read_system_metrics_from_proc()

    @staticmethod
    def _run_load_on_sandbox(client, sandbox_id: str, duration: int, results: List[dict]):
        start_time = time.time()
        interval = TestConfig.LONG_STABILITY_REQUEST_INTERVAL_MS / 1000.0

        while time.time() - start_time < duration:
            req_start = time.time()
            try:
                resp = client.post(
                    f"/api/v1/sandboxes/{sandbox_id}/exec",
                    json={"command": ["echo", "hello"], "timeout_seconds": 10},
                )
                latency = time.time() - req_start
                results.append({
                    "success": resp.status_code == 200,
                    "latency": latency,
                    "timestamp": time.time(),
                })
            except Exception as e:
                results.append({
                    "success": False,
                    "latency": time.time() - req_start,
                    "timestamp": time.time(),
                    "error": str(e),
                })

            elapsed = time.time() - req_start
            if elapsed < interval:
                time.sleep(interval - elapsed)

    @staticmethod
    def test_ls_001_long_stability_30min(client, wait_for_sandbox_ready):
        sandboxes = []
        for _ in range(TestConfig.LONG_STABILITY_SANDBOX_COUNT):
            resp = client.post("/api/v1/sandboxes", json={})
            sandbox_id = resp.json()["id"]
            wait_for_sandbox_ready(sandbox_id)
            sandboxes.append(sandbox_id)

        results = []
        threads = []

        for sb_id in sandboxes:
            thread = threading.Thread(
                target=TestLongStability._run_load_on_sandbox,
                args=(client, sb_id, TestConfig.LONG_STABILITY_DURATION_30MIN, results),
                daemon=True,
            )
            threads.append(thread)
            thread.start()

        for thread in threads:
            thread.join()

        success_count = sum(1 for r in results if r["success"])
        success_rate = success_count / len(results) if results else 0
        assert success_rate >= TestConfig.LONG_STABILITY_SUCCESS_RATE_THRESHOLD

        latencies = [r["latency"] for r in results if r["success"]]
        if latencies:
            avg_latency = sum(latencies) / len(latencies)
            assert avg_latency < TestConfig.LONG_STABILITY_AVG_LATENCY_THRESHOLD

            sorted_latencies = sorted(latencies)
            p99_index = int(len(sorted_latencies) * 0.99) - 1
            p99_latency = sorted_latencies[p99_index] if p99_index >= 0 else 0
            assert p99_latency < TestConfig.LONG_STABILITY_P99_LATENCY_THRESHOLD

        for sb_id in sandboxes:
            status = client.get(f"/api/v1/sandboxes/{sb_id}")
            assert status.json().get("phase") == "ready"

    @staticmethod
    def test_ls_002_long_stability_1hour(client, wait_for_sandbox_ready):
        sandboxes = []
        for _ in range(TestConfig.LONG_STABILITY_SANDBOX_COUNT):
            resp = client.post("/api/v1/sandboxes", json={})
            sandbox_id = resp.json()["id"]
            wait_for_sandbox_ready(sandbox_id)
            sandboxes.append(sandbox_id)

        results = []
        threads = []

        for sb_id in sandboxes:
            thread = threading.Thread(
                target=TestLongStability._run_load_on_sandbox,
                args=(client, sb_id, TestConfig.LONG_STABILITY_DURATION_1HOUR, results),
                daemon=True,
            )
            threads.append(thread)
            thread.start()

        for thread in threads:
            thread.join()

        success_count = sum(1 for r in results if r["success"])
        success_rate = success_count / len(results) if results else 0
        assert success_rate >= TestConfig.LONG_STABILITY_SUCCESS_RATE_THRESHOLD

        latencies = [r["latency"] for r in results if r["success"]]
        if latencies:
            avg_latency = sum(latencies) / len(latencies)
            assert avg_latency < TestConfig.LONG_STABILITY_AVG_LATENCY_THRESHOLD

            sorted_latencies = sorted(latencies)
            p99_index = int(len(sorted_latencies) * 0.99) - 1
            p99_latency = sorted_latencies[p99_index] if p99_index >= 0 else 0
            assert p99_latency < TestConfig.LONG_STABILITY_P99_LATENCY_THRESHOLD

        for sb_id in sandboxes:
            status = client.get(f"/api/v1/sandboxes/{sb_id}")
            assert status.json().get("phase") == "ready"
