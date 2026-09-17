"""Shared fixtures for jiuwenbox performance tests."""

from __future__ import annotations

import asyncio
import logging
import os

import httpx
import pytest
import pytest_asyncio

logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


def _normalize_endpoint(endpoint: str) -> str:
    return endpoint if "://" in endpoint else f"http://{endpoint}"


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    raw_value = os.environ.get(name)
    if raw_value is None:
        return default
    try:
        value = int(raw_value)
    except ValueError:
        logger.warning("Ignoring invalid %s=%r; using %s", name, raw_value, default)
        return default
    return max(minimum, value)


@pytest.fixture(scope="session")
def perf_sandbox_count() -> int:
    """Number of sandboxes to create for the performance workload."""
    return _env_int("JIUWENBOX_PERF_SANDBOX_COUNT", 1)


@pytest.fixture(scope="session")
def perf_concurrency() -> int:
    """Number of concurrent tasks to run inside each sandbox."""
    return _env_int("JIUWENBOX_PERF_CONCURRENCY", 4)


@pytest.fixture(scope="session")
def perf_loop() -> int:
    """Number of workload loops each concurrent task runs."""
    return _env_int("JIUWENBOX_PERF_LOOP", 8)


@pytest.fixture(scope="session")
def perf_exec_timeout_seconds() -> int:
    """Timeout for each in-sandbox office edit command."""
    return _env_int("JIUWENBOX_PERF_EXEC_TIMEOUT_SECONDS", 180)


TRANSIENT_HTTP_ERRORS = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
    httpx.ReadError,
    httpx.ReadTimeout,
    httpx.RemoteProtocolError,
    httpx.WriteError,
    httpx.WriteTimeout,
)


class AsyncSandboxTrackingClient:
    """Track sandboxes created during a performance test and clean them up."""

    def __init__(self, client: httpx.AsyncClient):
        self._client = client
        self._created_ids: list[str] = []

    def __getattr__(self, name: str):
        return getattr(self._client, name)

    async def _request(self, method: str, url, *args, **kwargs):
        retry_count = 3
        for attempt in range(retry_count + 1):
            try:
                return await self._client.request(method, url, *args, **kwargs)
            except TRANSIENT_HTTP_ERRORS:
                if attempt >= retry_count:
                    raise
                await asyncio.sleep(0.1 * (2 ** attempt))

        raise RuntimeError("unreachable retry state")

    async def post(self, url, *args, **kwargs):
        is_create_sandbox = str(url).rstrip("/") == "/api/v1/sandboxes"
        if is_create_sandbox:
            response = await self._client.post(url, *args, **kwargs)
        else:
            response = await self._request("POST", url, *args, **kwargs)
        if is_create_sandbox and response.status_code == 201:
            try:
                sandbox_id = response.json().get("id")
            except Exception as exc:
                logger.debug("Failed to parse sandbox create response: %s", exc)
                sandbox_id = None
            if sandbox_id:
                self._created_ids.append(sandbox_id)
        return response

    async def delete(self, url, *args, **kwargs):
        response = await self._request("DELETE", url, *args, **kwargs)
        sandbox_id = self._sandbox_id_from_delete_url(url)
        if sandbox_id and response.status_code in (200, 202, 204, 404):
            self._created_ids = [item for item in self._created_ids if item != sandbox_id]
        return response

    async def get(self, url, *args, **kwargs):
        return await self._request("GET", url, *args, **kwargs)

    async def cleanup_sandboxes(self) -> None:
        for sandbox_id in reversed(self._created_ids):
            try:
                await self._client.delete(f"/api/v1/sandboxes/{sandbox_id}")
            except Exception as exc:
                logger.warning("Failed to cleanup sandbox %s: %s", sandbox_id, exc)
        self._created_ids.clear()

    @staticmethod
    def _sandbox_id_from_delete_url(url) -> str | None:
        path = str(url).split("?", 1)[0].rstrip("/")
        prefix = "/api/v1/sandboxes/"
        if not path.startswith(prefix):
            return None
        suffix = path[len(prefix):]
        if "/" in suffix:
            return None
        return suffix or None


@pytest_asyncio.fixture
async def perf_client(server_endpoint, perf_sandbox_count, perf_concurrency):
    timeout = httpx.Timeout(connect=10.0, read=180.0, write=180.0, pool=180.0)
    max_connections = max(16, perf_sandbox_count * perf_concurrency * 4)
    limits = httpx.Limits(
        max_connections=max_connections,
        max_keepalive_connections=0,
    )
    async with httpx.AsyncClient(
        base_url=_normalize_endpoint(server_endpoint),
        timeout=timeout,
        limits=limits,
        trust_env=False,
    ) as external:
        tracking = AsyncSandboxTrackingClient(external)
        try:
            yield tracking
        finally:
            await tracking.cleanup_sandboxes()
