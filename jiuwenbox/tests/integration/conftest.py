"""Shared fixtures for integration tests."""

from __future__ import annotations

import asyncio
import logging
import socket
import subprocess
import time
from socketserver import ThreadingMixIn
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

import httpx
import pytest
import pytest_asyncio

LONG_RUNNING_COMMAND = ["python3", "-c", "import time; time.sleep(36000)"]
SYSTEM_BIND_MOUNTS = [
    {"host_path": "/bin", "sandbox_path": "/bin", "mode": "ro"},
    {"host_path": "/sbin", "sandbox_path": "/sbin", "mode": "ro"},
    {"host_path": "/usr", "sandbox_path": "/usr", "mode": "ro"},
    {"host_path": "/lib", "sandbox_path": "/lib", "mode": "ro"},
    {"host_path": "/lib64", "sandbox_path": "/lib64", "mode": "ro"},
    {"host_path": "/etc/resolv.conf", "sandbox_path": "/etc/resolv.conf", "mode": "ro"},
    {"host_path": "/etc/hosts", "sandbox_path": "/etc/hosts", "mode": "ro"},
    {"host_path": "/etc/nsswitch.conf", "sandbox_path": "/etc/nsswitch.conf", "mode": "ro"},
    {"host_path": "/etc/host.conf", "sandbox_path": "/etc/host.conf", "mode": "ro"},
    {"host_path": "/etc/ssl/certs", "sandbox_path": "/etc/ssl/certs", "mode": "ro"},
    {"host_path": "/etc/ssl/openssl.cnf", "sandbox_path": "/etc/ssl/openssl.cnf", "mode": "ro"},
    {"host_path": "/opt", "sandbox_path": "/opt", "mode": "ro"},
]
TMP_DIRECTORY = {"path": "/tmp", "permissions": "1777"}
logger = logging.getLogger(__name__)

DOCKER_ACCESSIBLE_IP = "172.17.0.1"


def _allocate_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as tcp_socket:
        tcp_socket.bind(("0.0.0.0", 0))
        return tcp_socket.getsockname()[1]


class SandboxTrackingClient:
    """Track sandboxes created during a test and clean them up afterwards."""

    def __init__(self, client):
        self._client = client
        self._created_ids: list[str] = []

    def __getattr__(self, name: str):
        return getattr(self._client, name)

    def post(self, url, *args, **kwargs):
        response = self._client.post(url, *args, **kwargs)
        if str(url).rstrip("/") == "/api/v1/sandboxes" and response.status_code == 201:
            try:
                sandbox_id = response.json().get("id")
            except Exception as exc:
                logger.debug("Failed to parse sandbox create response: %s", exc)
                sandbox_id = None
            if sandbox_id:
                self._created_ids.append(sandbox_id)
        return response

    def delete(self, url, *args, **kwargs):
        response = self._client.delete(url, *args, **kwargs)
        sandbox_id = self._sandbox_id_from_delete_url(url)
        if sandbox_id and response.status_code in (200, 202, 204, 404):
            self._created_ids = [item for item in self._created_ids if item != sandbox_id]
        return response

    def cleanup_sandboxes(self) -> None:
        for sandbox_id in reversed(self._created_ids):
            try:
                self._client.delete(f"/api/v1/sandboxes/{sandbox_id}")
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


_UDS_SCHEME = "unix://"
# httpx 在用 UDS transport 时仍需要一个合法 absolute base_url 才能拼相对路径;
# 实际请求路由完全走 socket transport, 不读 host。
_UDS_PLACEHOLDER_BASE_URL = "http://jiuwenbox"


def _normalize_endpoint(endpoint: str) -> str:
    return endpoint if "://" in endpoint else f"http://{endpoint}"


def _build_httpx_client(endpoint: str, *, timeout: float = 30.0) -> httpx.Client:
    """按 endpoint scheme 构造 httpx.Client (UDS / TCP 统一入口).

    ``unix:///abs/path`` 走 :class:`httpx.HTTPTransport` 的 ``uds=...``;
    其它形式走 base_url 模式。两种模式对 ``SandboxTrackingClient`` 透明,
    因此 ``test_server_api_default.py`` 业务断言一行不用改。
    """
    if endpoint.startswith(_UDS_SCHEME):
        uds_path = endpoint[len(_UDS_SCHEME):]
        if not uds_path.startswith("/"):
            raise ValueError(
                f"unix endpoint requires absolute path: {endpoint!r}",
            )
        return httpx.Client(
            transport=httpx.HTTPTransport(uds=uds_path),
            base_url=_UDS_PLACEHOLDER_BASE_URL,
            timeout=timeout,
        )
    return httpx.Client(base_url=_normalize_endpoint(endpoint), timeout=timeout)


@pytest.fixture
def client(server_endpoint):
    with _build_httpx_client(server_endpoint, timeout=30.0) as external:
        tracking = SandboxTrackingClient(external)
        try:
            yield tracking
        finally:
            tracking.cleanup_sandboxes()


@pytest.fixture(scope="session")
def mock_llm_server_http_port():
    """Find an available port for mock LLM HTTP server (session-scoped)."""
    return _allocate_tcp_port()


@pytest.fixture(scope="session")
def mock_llm_server_https_port():
    """Find an available port for mock LLM HTTPS server (session-scoped)."""
    return _allocate_tcp_port()


@pytest.fixture(scope="session")
def mock_llm_server(mock_llm_server_http_port, mock_llm_server_https_port):
    """Start mock LLM server for testing (session-scoped).
    
    Runs once per test session. All tests share the same server instance.
    Server is thread-safe and handles concurrent requests.
    Binds on 0.0.0.0 so it's accessible from Docker containers.
    
    Returns:
        dict: {"http_port": int, "https_port": int, "process": subprocess.Popen}
    """
    import sys
    from pathlib import Path

    mock_server_path = Path(__file__).parent / "mock_llm_server.py"
    proc = subprocess.Popen(
        [
            sys.executable,
            str(mock_server_path),
            "--http-port", str(mock_llm_server_http_port),
            "--https-port", str(mock_llm_server_https_port),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    time.sleep(0.5)

    try:
        yield {
            "http_port": mock_llm_server_http_port,
            "https_port": mock_llm_server_https_port,
            "process": proc,
        }
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


@pytest.fixture(scope="session")
def http_target_port():
    """Allocate a dynamic port once per test session."""
    return _allocate_tcp_port()


@pytest.fixture
def proxy_listen_port():
    """Allocate a dynamic port for proxy listen address (per test function)."""
    return _allocate_tcp_port()


@pytest.fixture(scope="session")
def simple_http_target(http_target_port):
    """Session-scoped HTTP target server that returns 200 OK.
    
    Starts once at session beginning, runs for all tests, stops at session end.
    Uses threading to avoid pytest-asyncio event loop scope issues.
    
    Returns the port number for tests to use in target_endpoint URLs.
    """
    connection_count = [0]
    
    class ThreadedWSGIServer(ThreadingMixIn, WSGIServer):
        daemon_threads = True

    class LoggingWSGIRequestHandler(WSGIRequestHandler):
        def log_message(self, fmt: str, *args) -> None:  # noqa: A003
            logger.info("[simple_http_target] " + fmt, *args)

    def simple_wsgi_app(environ, start_response):
        content_length = int(environ.get("CONTENT_LENGTH") or "0")
        if content_length:
            _ = environ["wsgi.input"].read(content_length)
        connection_count[0] += 1
        logger.info(
            "[simple_http_target] Connection #%s from %s",
            connection_count[0],
            environ.get("REMOTE_ADDR"),
        )
        start_response("200 OK", [("Content-Length", "2")])
        return [b"OK"]

    server = make_server(
        "0.0.0.0",
        http_target_port,
        simple_wsgi_app,
        server_class=ThreadedWSGIServer,
        handler_class=LoggingWSGIRequestHandler,
    )

    def threaded_server() -> None:
        logger.info(
            "[simple_http_target] Session server started on 0.0.0.0:%s",
            http_target_port,
        )
        server.serve_forever(poll_interval=0.5)
        logger.info(
            "[simple_http_target] Session server stopped, total connections: %s",
            connection_count[0],
        )

    import threading

    server_thread = threading.Thread(target=threaded_server, daemon=True)
    server_thread.start()
    time.sleep(0.2)
    
    try:
        yield http_target_port
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5.0)
        logger.info("[simple_http_target] Session fixture teardown complete")


@pytest.fixture(scope="session")
def docker_gateway_ip(
    http_target_port, 
    simple_http_target, 
    server_url_session,
    test_llm_endpoint,
    test_llm_api_key,
):
    """
    Detect topology AND check LLM availability (reuses same sandbox).
    
    Uses sandbox exec curl to test connectivity from sandbox environment.
    
    Returns dict:
        - "gateway_ip": "127.0.0.1" or "172.17.0.1" (Docker gateway IP for target endpoints)
        - "llm_available": True/False
        
    Raises:
        RuntimeError: If API server unreachable or topology detection fails
    """
    if server_url_session.startswith(_UDS_SCHEME):
        # Docker-gateway 探测 + IPP 端口联通性测试天然依赖 TCP listener; UDS
        # 模式下整套 proxy/IPP 用例都该 skip, 在源头一次性 skip 简化下游。
        pytest.skip(
            "docker gateway detection requires a TCP server endpoint; "
            f"got {server_url_session!r}",
        )

    port = http_target_port
    
    logger.info("[docker_gateway_ip] Starting checks")
    logger.info("[docker_gateway_ip] API server: %s", server_url_session)
    
    try:
        with httpx.Client(base_url=server_url_session, timeout=5.0) as client:
            health = client.get("/health")
            if health.status_code != 200:
                raise RuntimeError(
                    f"API server at {server_url_session} unhealthy (status {health.status_code})"
                )
    except Exception as e:
        raise RuntimeError(
            f"API server at {server_url_session} unreachable. "
            f"Please ensure the server is running. Error: {e}"
        ) from e
    
    sandbox_id = None
    detected = None
    llm_ok = False
    
    try:
        with httpx.Client(base_url=server_url_session, timeout=30.0) as client:
            create = client.post("/api/v1/sandboxes", json={})
            if create.status_code != 201:
                raise RuntimeError(
                    f"Failed to create sandbox via {server_url_session} (status {create.status_code})"
                )
            sandbox_id = create.json()["id"]
            logger.info("[docker_gateway_ip] Created sandbox %s", sandbox_id)
            
            for _ in range(10):
                status = client.get(f"/api/v1/sandboxes/{sandbox_id}")
                if status.json().get("phase") == "ready":
                    break
                time.sleep(0.5)
            
            # ========== Topology detection ==========
            logger.info("[docker_gateway_ip] Testing 127.0.0.1:%s", port)
            exec_127 = client.post(
                f"/api/v1/sandboxes/{sandbox_id}/exec",
                json={
                    "command": ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                                "--connect-timeout", "3", f"http://127.0.0.1:{port}/test"],
                    "timeout_seconds": 10,
                }
            )
            
            if exec_127.status_code == 200:
                result = exec_127.json()
                if result.get("exit_code") == 0 and result.get("stdout", "").strip() == "200":
                    detected = "127.0.0.1"
                    logger.info("[docker_gateway_ip] 127.0.0.1 reachable -> sandbox in WSL")
                else:
                    logger.info(
                        "[docker_gateway_ip] 127.0.0.1 failed (exit=%s, stdout=%s)",
                        result.get("exit_code"),
                        result.get("stdout"),
                    )
                    
                    logger.info("[docker_gateway_ip] Testing 172.17.0.1:%s", port)
                    exec_gw = client.post(
                        f"/api/v1/sandboxes/{sandbox_id}/exec",
                        json={
                            "command": ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                                        "--connect-timeout", "3", f"http://172.17.0.1:{port}/test"],
                            "timeout_seconds": 10,
                        }
                    )
                    if exec_gw.status_code == 200:
                        result = exec_gw.json()
                        if result.get("exit_code") == 0 and result.get("stdout", "").strip() == "200":
                            detected = "172.17.0.1"
                            logger.info(
                                "[docker_gateway_ip] 172.17.0.1 reachable -> sandbox in Docker"
                            )
                        else:
                            raise RuntimeError(
                                f"Both IPs unreachable for target server at port {port}. "
                                f"127.0.0.1: exit={exec_127.json().get('exit_code')}, "
                                f"172.17.0.1: exit={result.get('exit_code')}"
                            )
                    else:
                        raise RuntimeError(
                            f"Gateway exec failed via {server_url_session} (status {exec_gw.status_code})"
                        )
            else:
                raise RuntimeError(
                    f"127.0.0.1 exec failed via {server_url_session} (status {exec_127.status_code})"
                )
            
            # ========== LLM check (conditional) ==========
            if test_llm_endpoint and test_llm_api_key:
                logger.info("[docker_gateway_ip] Testing LLM: %s", test_llm_endpoint)
                exec_llm = client.post(
                    f"/api/v1/sandboxes/{sandbox_id}/exec",
                    json={
                        "command": [
                            "curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                            "--connect-timeout", "10",
                            "-H", f"Authorization: Bearer {test_llm_api_key}",
                            f"{test_llm_endpoint}/models"
                        ],
                        "timeout_seconds": 15,
                    }
                )
                
                if exec_llm.status_code == 200:
                    result = exec_llm.json()
                    http_code = result.get("stdout", "").strip()
                    if result.get("exit_code") == 0 and http_code == "200":
                        llm_ok = True
                        logger.info("[docker_gateway_ip] LLM reachable (HTTP %s)", http_code)
                    else:
                        logger.info(
                            "[docker_gateway_ip] LLM returned HTTP %s (exit=%s)",
                            http_code,
                            result.get("exit_code"),
                        )
            else:
                logger.info("[docker_gateway_ip] LLM not configured, skipping check")
            
            # Cleanup sandbox
            client.delete(f"/api/v1/sandboxes/{sandbox_id}")
            logger.info("[docker_gateway_ip] Deleted sandbox %s", sandbox_id)
            logger.info(
                "[docker_gateway_ip] Complete: gateway_ip=%s, llm_available=%s",
                detected,
                llm_ok,
            )
            
            return {
                "gateway_ip": detected,
                "llm_available": llm_ok,
            }
            
    except RuntimeError:
        raise
    except Exception as e:
        if sandbox_id:
            try:
                with httpx.Client(base_url=server_url_session, timeout=5.0) as client:
                    client.delete(f"/api/v1/sandboxes/{sandbox_id}")
            except Exception as cleanup_exc:
                logger.debug(
                    "[docker_gateway_ip] Failed to cleanup sandbox %s after error: %s",
                    sandbox_id,
                    cleanup_exc,
                )
        raise RuntimeError(f"Checks failed: {e}") from e


@pytest.fixture(scope="session")
def proxy_host_from_test(server_host_port_session):
    """
    IP address to connect FROM test process TO proxy.
    
    When proxy runs in API server (possibly Docker), this returns
    the host where proxy is accessible from test environment.
    
    For local API server: returns 127.0.0.1
    For Docker API server: returns the mapped host (usually 127.0.0.1 if port mapped)
    
    Returns:
        str: Host IP for connecting to proxy from test process
    """
    if server_host_port_session:
        return server_host_port_session[0]
    return "127.0.0.1"


@pytest.fixture(scope="session")
def mock_server_host_for_docker(docker_gateway_ip):
    """
    IP address for mock server TARGET endpoints.
    
    When proxy runs in Docker and needs to connect to mock server
    running in test process, use Docker gateway IP.
    
    For local tests: returns 127.0.0.1
    For Docker tests: returns Docker gateway IP (172.17.0.1 or similar)
    
    Returns:
        str: Host IP for mock server target_endpoint URLs
    """
    if docker_gateway_ip:
        return docker_gateway_ip["gateway_ip"]
    return "127.0.0.1"
