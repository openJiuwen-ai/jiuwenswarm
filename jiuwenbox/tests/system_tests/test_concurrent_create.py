"""Concurrent sandbox creation tests."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import httpx
import pytest

from tests.system_tests.test_config import TestConfig


@pytest.mark.system
@pytest.mark.performance
@pytest.mark.slow
class TestConcurrentCreate:
    """Concurrent sandbox creation test cases."""

    @staticmethod
    def _normalize_endpoint(endpoint: str) -> str:
        return endpoint if "://" in endpoint else f"https://{endpoint}"

    @staticmethod
    def _create_sandbox(endpoint: str, tracker) -> str:
        with httpx.Client(
            base_url=TestConcurrentCreate._normalize_endpoint(endpoint),
            timeout=TestConfig.DEFAULT_TIMEOUT,
            verify=True,
        ) as http:
            resp = http.post("/api/v1/sandboxes", json={})
            assert resp.status_code == 201, f"Failed to create sandbox: {resp.text}"
            sandbox_id = resp.json().get("id")
            assert sandbox_id, f"Missing sandbox id in response: {resp.text}"
        tracker.track_sandbox(sandbox_id)
        return sandbox_id

    @staticmethod
    def _wait_all_ready(client, sandbox_ids: list, timeout: int) -> bool:
        start = time.time()
        while time.time() - start < timeout:
            all_ready = True
            for sb_id in sandbox_ids:
                status = client.get(f"/api/v1/sandboxes/{sb_id}")
                if status.status_code != 200:
                    all_ready = False
                    break
                phase = status.json().get("phase")
                if phase == "error":
                    return False
                if phase != "ready":
                    all_ready = False
                    break
            if all_ready:
                return True
            time.sleep(0.5)
        return False

    @staticmethod
    def test_cc_001_concurrent_create_5(client, server_endpoint):
        start_time = time.time()
        sandbox_ids = []

        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = [
                pool.submit(TestConcurrentCreate._create_sandbox, server_endpoint, client)
                for _ in range(5)
            ]
            for future in as_completed(futures):
                sandbox_ids.append(future.result())

        assert len(sandbox_ids) == 5
        assert TestConcurrentCreate._wait_all_ready(client, sandbox_ids, TestConfig.CONCURRENT_CREATE_TIMEOUT_5)

        total_time = time.time() - start_time
        assert total_time < TestConfig.CONCURRENT_CREATE_TIMEOUT_5

    @staticmethod
    def test_cc_002_concurrent_create_10(client, server_endpoint):
        start_time = time.time()
        sandbox_ids = []

        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = [
                pool.submit(TestConcurrentCreate._create_sandbox, server_endpoint, client)
                for _ in range(10)
            ]
            for future in as_completed(futures):
                sandbox_ids.append(future.result())

        assert len(sandbox_ids) == 10
        assert TestConcurrentCreate._wait_all_ready(client, sandbox_ids, TestConfig.CONCURRENT_CREATE_TIMEOUT_10)

        total_time = time.time() - start_time
        assert total_time < TestConfig.CONCURRENT_CREATE_TIMEOUT_10

    @staticmethod
    def test_cc_003_concurrent_create_20(client, server_endpoint):
        start_time = time.time()
        sandbox_ids = []

        with ThreadPoolExecutor(max_workers=20) as pool:
            futures = [
                pool.submit(TestConcurrentCreate._create_sandbox, server_endpoint, client)
                for _ in range(20)
            ]
            for future in as_completed(futures):
                sandbox_ids.append(future.result())

        assert len(sandbox_ids) == 20
        assert TestConcurrentCreate._wait_all_ready(client, sandbox_ids, TestConfig.CONCURRENT_CREATE_TIMEOUT_20)

        total_time = time.time() - start_time
        assert total_time < TestConfig.CONCURRENT_CREATE_TIMEOUT_20
