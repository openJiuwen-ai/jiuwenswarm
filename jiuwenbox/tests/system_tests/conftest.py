"""Shared fixtures for system reliability and performance tests."""

from __future__ import annotations

import logging
import threading
import time

import httpx
import pytest

from tests.system_tests.test_config import EnvironmentDetector, TestConfig

logger = logging.getLogger(__name__)


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "chaos: chaos engineering tests requiring Linux, stress-ng, and iproute2",
    )
    config.addinivalue_line(
        "markers",
        "resource_degradation: tests requiring Linux and stress-ng",
    )
    config.addinivalue_line(
        "markers",
        "network_degradation: tests requiring Linux and tc",
    )


def pytest_collection_modifyitems(config, items):
    """Skip environment-specific tests when required tools are unavailable."""
    if not EnvironmentDetector.can_run_chaos_tests():
        skip_chaos = pytest.mark.skip(
            reason="Chaos tests require Linux + stress-ng + iproute2"
        )
        for item in items:
            if item.get_closest_marker("chaos"):
                item.add_marker(skip_chaos)

    if not EnvironmentDetector.can_run_resource_tests():
        skip_resource = pytest.mark.skip(
            reason="Resource degradation tests require Linux + stress-ng"
        )
        for item in items:
            if item.get_closest_marker("resource_degradation"):
                item.add_marker(skip_resource)

    if not EnvironmentDetector.can_run_network_tests():
        skip_network = pytest.mark.skip(
            reason="Network tests require Linux + tc"
        )
        for item in items:
            if item.get_closest_marker("network_degradation"):
                item.add_marker(skip_network)


_UDS_PREFIX = "unix:"
_UDS_PLACEHOLDER_BASE_URL = "https://jiuwenbox"


def _normalize_endpoint(endpoint: str) -> str:
    if endpoint.startswith(_UDS_PREFIX):
        return _UDS_PLACEHOLDER_BASE_URL
    return endpoint if "://" in endpoint else f"https://{endpoint}"


def _build_httpx_client(endpoint: str, *, timeout: float = 30.0) -> httpx.Client:
    if endpoint.startswith(_UDS_PREFIX):
        # Strip the "unix:" prefix; the remainder may be "//<path>" or "/<path>"
        uds_raw = endpoint[len(_UDS_PREFIX):]
        # Remove leading slashes until we find the absolute path
        while uds_raw.startswith("/"):
            uds_raw = uds_raw[1:]
        uds_path = "/" + uds_raw
        if not uds_path.startswith("/"):
            raise ValueError(f"unix endpoint requires absolute path: {endpoint!r}")
        return httpx.Client(
            transport=httpx.HTTPTransport(uds=uds_path),
            base_url=_UDS_PLACEHOLDER_BASE_URL,
            timeout=timeout,
            verify=True,
        )
    return httpx.Client(base_url=_normalize_endpoint(endpoint), timeout=timeout, verify=True)


class SandboxTrackingClient:
    """Track sandboxes created during a test and clean them up afterwards."""

    def __init__(self, endpoint: str, *, timeout: float = 30.0):
        self._endpoint = endpoint
        self._timeout = timeout
        self._local = threading.local()
        self._clients: list[httpx.Client] = []
        self._clients_lock = threading.Lock()
        self._created_ids: list[str] = []
        self._created_ids_lock = threading.Lock()

    def _get_client(self) -> httpx.Client:
        client = getattr(self._local, "client", None)
        if client is None:
            client = _build_httpx_client(self._endpoint, timeout=self._timeout)
            self._local.client = client
            with self._clients_lock:
                self._clients.append(client)
        return client

    def __getattr__(self, name: str):
        return getattr(self._get_client(), name)

    def post(self, url, *args, **kwargs):
        response = self._get_client().post(url, *args, **kwargs)
        if str(url).rstrip("/") == "/api/v1/sandboxes" and response.status_code == 201:
            try:
                sandbox_id = response.json().get("id")
            except Exception as exc:
                logger.debug("Failed to parse sandbox create response: %s", exc)
                sandbox_id = None
            if sandbox_id:
                with self._created_ids_lock:
                    self._created_ids.append(sandbox_id)
        return response

    def delete(self, url, *args, **kwargs):
        response = self._get_client().delete(url, *args, **kwargs)
        sandbox_id = self._sandbox_id_from_delete_url(url)
        if sandbox_id and response.status_code in (200, 202, 204, 404):
            with self._created_ids_lock:
                self._created_ids = [item for item in self._created_ids if item != sandbox_id]
        return response

    def track_sandbox(self, sandbox_id: str) -> None:
        with self._created_ids_lock:
            self._created_ids.append(sandbox_id)

    def cleanup_sandboxes(self) -> None:
        with self._created_ids_lock:
            ids = list(reversed(self._created_ids))
            self._created_ids.clear()
        if not ids:
            return
        with _build_httpx_client(self._endpoint, timeout=self._timeout) as cleanup_client:
            for sandbox_id in ids:
                try:
                    cleanup_client.delete(f"/api/v1/sandboxes/{sandbox_id}")
                except Exception as exc:
                    logger.warning("Failed to cleanup sandbox %s: %s", sandbox_id, exc)

    def close(self) -> None:
        with self._clients_lock:
            clients = self._clients
            self._clients = []
        for client in clients:
            try:
                client.close()
            except Exception as exc:
                logger.debug("Failed to close httpx client: %s", exc)
        self._local.client = None

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


@pytest.fixture(scope="session")
def server_endpoint(pytestconfig):
    return pytestconfig.getoption("server_endpoint") or TestConfig.SERVER_ENDPOINT


@pytest.fixture
def client(server_endpoint):
    tracking = SandboxTrackingClient(server_endpoint, timeout=TestConfig.DEFAULT_TIMEOUT)
    try:
        yield tracking
    finally:
        tracking.cleanup_sandboxes()
        tracking.close()


@pytest.fixture
def restore_timeout(client):
    try:
        snapshot_resp = client.get("/api/v1/timeout")
        if snapshot_resp.status_code != 200:
            pytest.skip("Timeout API not available")
        snapshot = snapshot_resp.json()
    except Exception:
        pytest.skip("Timeout API not available")
    try:
        yield snapshot
    finally:
        restore_payload = {
            "idle_timeout": snapshot["idle_timeout"],
            "idle_check_interval": snapshot["idle_check_interval"],
        }
        if "exec_timeout" in snapshot:
            restore_payload["exec_timeout"] = snapshot["exec_timeout"]
        try:
            client.put("/api/v1/timeout", json=restore_payload)
        except Exception as exc:
            logger.warning("Failed to restore timeout config: %s", exc)


@pytest.fixture
def create_sandbox(client):
    def factory(**kwargs):
        resp = client.post("/api/v1/sandboxes", json=kwargs)
        if resp.status_code != 201:
            raise RuntimeError(
                f"Failed to create sandbox: {resp.status_code} {resp.text}"
            )
        sandbox = resp.json()
        return sandbox

    return factory


@pytest.fixture
def wait_for_sandbox_ready(client):
    def waiter(sandbox_id, timeout=TestConfig.SANDBOX_READY_TIMEOUT):
        for _ in range(timeout * 2):
            status = client.get(f"/api/v1/sandboxes/{sandbox_id}")
            phase = status.json().get("phase")
            if phase == "ready":
                return True
            if phase == "error":
                raise RuntimeError(f"Sandbox failed: {status.json()}")
            time.sleep(0.5)
        raise TimeoutError(f"Sandbox {sandbox_id} did not become ready")

    return waiter


@pytest.fixture
def exec_command(client):
    def executor(sandbox_id, command, timeout_seconds=TestConfig.EXEC_TIMEOUT):
        resp = client.post(
            f"/api/v1/sandboxes/{sandbox_id}/exec",
            json={"command": command, "timeout_seconds": timeout_seconds},
        )
        return resp

    return executor


@pytest.fixture
def resource_degradation_cleanup():
    processes = []
    try:
        yield processes
    finally:
        import subprocess
        for proc in processes:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                    proc.wait(timeout=5)
                except Exception as kill_exc:
                    logger.warning(
                        "Failed to kill process during cleanup: %s", kill_exc
                    )
            except Exception as exc:
                logger.debug(
                    "Failed to terminate process during cleanup: %s", exc
                )