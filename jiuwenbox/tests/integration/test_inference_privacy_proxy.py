"""Integration tests for HTTP-aware inference privacy proxy with path routing."""

import asyncio
import http.client
import json
import logging
import os
import socket
import threading
import time

import httpx
import pytest
import pytest_asyncio

from jiuwenbox.proxy.inference_privacy_proxy import (
    InferencePrivacyProxyConfig,
    ProxyRoute,
    InferencePrivacyProxy,
    PLACEHOLDER,
)
from jiuwenbox.proxy.inference_privacy_proxy_manager import (
    InferencePrivacyProxyManager,
    ProxyState,
)

NUM_THREADS = 5
logger = logging.getLogger(__name__)


@pytest.fixture
def manager():
    mgr = InferencePrivacyProxyManager()
    mgr.reset()
    return mgr


@pytest.fixture
def proxy_route_factory(http_target_port, proxy_target_host):
    """Factory to create ProxyRoute objects with custom parameters.
    
    Args:
        path_prefix: Route path prefix (e.g., "/test", "/route1")
        api_key: API key for the route (default: "sk-test-key")
    
    Returns:
        ProxyRoute instance configured with dynamic host
    """
    def create_route(path_prefix: str, api_key: str = "sk-test-key"):
        return ProxyRoute(
            path_prefix=path_prefix,
            target_endpoint=f"http://{proxy_target_host}:{http_target_port}",
            api_key=api_key,
        )
    return create_route


@pytest.fixture(scope="session")
def proxy_port_session(pytestconfig) -> int:
    return (
        pytestconfig.getoption("proxy_port")
        or int(os.environ.get("JIUWENBOX_PROXY_PORT", "8322"))
    )


@pytest.fixture(scope="session")
def proxy_target_host(
    server_url_session,
    proxy_host_from_test,
    proxy_port_session,
    simple_http_target,
):
    """Detect target host reachable by proxy without creating sandboxes.

    In proxy-only mode, `/api/v1/sandboxes` is intentionally unavailable.
    We therefore probe candidate host IPs by creating a temporary proxy route
    and checking real forwarding through the proxy listener.
    """
    if server_url_session.startswith("unix://"):
        pytest.skip(
            "proxy integration tests require a TCP API endpoint; "
            f"got {server_url_session!r}",
        )

    candidates = ("127.0.0.1", "172.17.0.1")
    probe_errors: list[str] = []

    with httpx.Client(base_url=server_url_session, timeout=10.0) as api_client:
        for candidate in candidates:
            route_name = f"_probe_{candidate.replace('.', '_')}_{int(time.time() * 1000)}"
            path_prefix = f"/{route_name}"
            target_endpoint = f"http://{candidate}:{simple_http_target}"
            try:
                create_response = api_client.post(
                    "/api/v1/proxies",
                    json={
                        "path_prefix": path_prefix,
                        "target_endpoint": target_endpoint,
                    },
                )
                if create_response.status_code != 201:
                    probe_errors.append(
                        f"{candidate}: create route failed ({create_response.status_code})"
                    )
                    continue

                start_response = api_client.post(f"/api/v1/proxies/{route_name}/start")
                if start_response.status_code != 200:
                    probe_errors.append(
                        f"{candidate}: start route failed ({start_response.status_code})"
                    )
                    continue

                forward_result = _proxy_http_get(
                    proxy_host_from_test,
                    proxy_port_session,
                    f"{path_prefix}/test",
                    timeout=3.0,
                )
                if "200 OK" in forward_result:
                    return candidate
                probe_errors.append(
                    f"{candidate}: forward probe failed ({forward_result.splitlines()[0]})"
                )
            except Exception as exc:
                probe_errors.append(f"{candidate}: exception ({exc})")
            finally:
                api_client.post(f"/api/v1/proxies/{route_name}/stop")
                api_client.delete(f"/api/v1/proxies/{route_name}")

    raise RuntimeError(
        "Failed to detect proxy target host without sandbox. "
        f"Tried {candidates}. Details: {'; '.join(probe_errors)}"
    )


@pytest.fixture
def integration_target_endpoint(simple_http_target, proxy_target_host):
    """Target endpoint for integration tests (proxy runtime perspective)."""
    return f"http://{proxy_target_host}:{simple_http_target}"


def _wait_tcp_ready(host: str, port: int, timeout_seconds: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return True
        except OSError:
            time.sleep(0.1)
    return False


@pytest.fixture(scope="session")
def llm_available(test_llm_endpoint, test_llm_api_key):
    """Best-effort LLM availability probe without sandbox dependency."""
    if not test_llm_endpoint or not test_llm_api_key:
        return False
    try:
        with httpx.Client(timeout=10.0) as client:
            response = client.get(
                f"{test_llm_endpoint.rstrip('/')}/models",
                headers={"Authorization": f"Bearer {test_llm_api_key}"},
            )
            return response.status_code == 200
    except Exception:
        return False


@pytest.fixture(scope="session")
def mock_llm_server_ready(mock_llm_server, proxy_target_host):
    """Ensure mock LLM server is reachable from proxy-side host mapping."""
    http_port = mock_llm_server["http_port"]
    process = mock_llm_server["process"]
    if _wait_tcp_ready(proxy_target_host, http_port, timeout_seconds=10.0):
        return mock_llm_server

    stderr_detail = ""
    if process.poll() is not None and process.stderr is not None:
        try:
            stderr_raw = process.stderr.read()
            stderr_text = stderr_raw.decode(errors="replace") if stderr_raw else ""
            if stderr_text.strip():
                stderr_detail = f" process exited with stderr: {stderr_text.strip()}"
        except Exception:
            logger.debug("process.poll() failed")

    raise RuntimeError(
        f"mock_llm_server not reachable at {proxy_target_host}:{http_port}; "
        f"this usually means the fixture subprocess failed to start or host<->container "
        f"port path is blocked.{stderr_detail}"
    )


def _proxy_http_get(proxy_host: str, proxy_port: int, path: str, timeout: float = 5.0) -> str:
    connection = http.client.HTTPConnection(proxy_host, proxy_port, timeout=timeout)
    try:
        connection.request("GET", path, headers={"Host": "localhost"})
        response = connection.getresponse()
        body = response.read().decode(errors="replace")
        return f"{response.status} {response.reason}\n{body}"
    finally:
        connection.close()


async def validate_proxy_http(
    proxy_port: int,
    path: str,
    expect_forward: bool,
    proxy_host: str = "127.0.0.1",
    timeout: float = 2.0,
):
    """Validate proxy state by making real HTTP call (unit test helper).
    
    Args:
        proxy_port: Port the proxy listens on
        path: Path to request (e.g., "/test/v1/chat")
        expect_forward: True if proxy should forward (200), False if route disabled (404) or proxy stopped
        proxy_host: Host IP for proxy connection (default: 127.0.0.1)
        timeout: Timeout for connection attempt
    """
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(proxy_host, proxy_port),
            timeout=timeout
        )
        
        request = f"GET {path} HTTP/1.1\r\nHost: localhost\r\n\r\n"
        writer.write(request.encode())
        await writer.drain()
        
        response = await asyncio.wait_for(reader.read(1024), timeout=timeout)
        response_text = response.decode()
        
        writer.close()
        await writer.wait_closed()
        
        if expect_forward:
            assert "200 OK" in response_text, f"Expected 200 OK, got: {response_text}"
        else:
            assert "404" in response_text or "No route matched" in response_text, \
                f"Expected 404 for disabled route, got: {response_text}"
            
    except asyncio.TimeoutError as e:
        if expect_forward:
            raise AssertionError(
                f"Expected proxy at {proxy_host}:{proxy_port} to be listening, "
                f"but connection failed: {e}"
            ) from e
        return
    except OSError as e:
        if expect_forward:
            raise AssertionError(
                f"Expected proxy at {proxy_host}:{proxy_port} to be listening, "
                f"but connection failed: {e}"
            ) from e
        return


async def validate_proxy_not_listening(
    proxy_port: int,
    proxy_host: str = "127.0.0.1",
    timeout: float = 1.0,
):
    """Validate that proxy is NOT listening (unit test helper).
    
    Args:
        proxy_port: Port to check
        proxy_host: Host IP for proxy connection (default: 127.0.0.1)
        timeout: Timeout for connection attempt
    """
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(proxy_host, proxy_port),
            timeout=timeout
        )
        writer.close()
        await writer.wait_closed()
        raise AssertionError(
            f"Expected connection to fail at {proxy_host}:{proxy_port}, "
            f"but proxy is listening"
        )
    except asyncio.TimeoutError:
        return
    except OSError:
        return


class TestProxyManagerCRUD:
    """Test create, read, update, delete operations."""

    @pytest.mark.asyncio
    async def test_create_proxy_with_zero_port_raises(self, manager, proxy_route_factory):
        route = proxy_route_factory("/test")
        config = InferencePrivacyProxyConfig(listen_port=0, routes=[route])
        
        with pytest.raises(ValueError, match="listen_port=0"):
            await manager.create_proxy("test", config)

    @pytest.mark.asyncio
    async def test_create_proxy_on_disabled_proxy_raises(self, manager, proxy_route_factory, proxy_listen_port):
        route = proxy_route_factory("/test")
        config1 = InferencePrivacyProxyConfig(listen_port=proxy_listen_port, routes=[route])
        await manager.create_proxy("test", config1)
        
        global_instance = manager.get_global_instance()
        global_instance.config.listen_port = 0
        
        route2 = proxy_route_factory("/route2", api_key="key2")
        config2 = InferencePrivacyProxyConfig(routes=[route2])
        
        with pytest.raises(ValueError, match="listen_port=0"):
            await manager.create_proxy("route2", config2)

    @pytest.mark.asyncio
    async def test_create_proxy(self, manager, proxy_route_factory, proxy_listen_port):
        route = proxy_route_factory("/test")
        config = InferencePrivacyProxyConfig(listen_port=proxy_listen_port, routes=[route])
        result = await manager.create_proxy("test", config)
        
        assert result["name"] == "test"
        assert result["state"] == "stopped"

    @pytest.mark.asyncio
    async def test_create_duplicate_proxy_raises(self, manager, proxy_route_factory, proxy_listen_port):
        route = proxy_route_factory("/test")
        config = InferencePrivacyProxyConfig(listen_port=proxy_listen_port, routes=[route])
        await manager.create_proxy("test", config)
        
        with pytest.raises(ValueError, match="already exists"):
            await manager.create_proxy("test", config)

    @pytest.mark.asyncio
    async def test_list_proxies(self, manager, proxy_route_factory, proxy_listen_port):
        route1 = proxy_route_factory("/route1", api_key="key1")
        config1 = InferencePrivacyProxyConfig(listen_port=proxy_listen_port, routes=[route1])
        await manager.create_proxy("route1", config1)
        
        route2 = proxy_route_factory("/route2", api_key="key2")
        config2 = InferencePrivacyProxyConfig(routes=[route2])
        await manager.create_proxy("route2", config2)
        
        proxies = await manager.list_proxies()
        assert len(proxies) == 2
        names = [p["name"] for p in proxies]
        assert "route1" in names
        assert "route2" in names

    @pytest.mark.asyncio
    async def test_get_proxy(self, manager, proxy_route_factory, proxy_listen_port):
        route = proxy_route_factory("/test")
        config = InferencePrivacyProxyConfig(listen_port=proxy_listen_port, routes=[route])
        await manager.create_proxy("test", config)
        
        result = await manager.get_proxy("test")
        assert result is not None
        assert result["name"] == "test"
        assert result["state"] == "stopped"
        assert result["route"]["path_prefix"] == "/test"

    @pytest.mark.asyncio
    async def test_get_nonexistent_proxy_returns_none(self, manager):
        result = await manager.get_proxy("nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_delete_proxy(self, manager, proxy_route_factory, proxy_listen_port):
        route = proxy_route_factory("/test")
        config = InferencePrivacyProxyConfig(listen_port=proxy_listen_port, routes=[route])
        await manager.create_proxy("test", config)
        
        result = await manager.delete_proxy("test")
        assert result["deleted"] == True
        
        proxies = await manager.list_proxies()
        assert len(proxies) == 0

    @pytest.mark.asyncio
    async def test_delete_nonexistent_proxy_raises(self, manager):
        with pytest.raises(ValueError, match="not found"):
            await manager.delete_proxy("nonexistent")

    @pytest.mark.asyncio
    async def test_update_proxy(
        self,
        manager,
        proxy_route_factory,
        proxy_listen_port,
        http_target_port,
        proxy_target_host,
    ):
        route = proxy_route_factory("/test")
        config = InferencePrivacyProxyConfig(listen_port=proxy_listen_port, routes=[route])
        await manager.create_proxy("test", config)
        
        new_route = ProxyRoute(
            path_prefix="/test",
            target_endpoint=f"http://{proxy_target_host}:{http_target_port}",
            api_key="sk-new-key",
        )
        new_config = InferencePrivacyProxyConfig(routes=[new_route])
        
        result = await manager.update_proxy("test", new_config)
        assert result["name"] == "test"
        
        proxy = await manager.get_proxy("test")
        assert proxy["route"]["target_endpoint"] == f"http://{proxy_target_host}:{http_target_port}"


class TestProxyManagerLifecycle:
    """Test start, stop operations."""

    @pytest.mark.asyncio
    async def test_start_proxy_with_zero_port_returns_stopped(
        self,
        manager,
        proxy_route_factory,
        simple_http_target,
        proxy_listen_port,
    ):
        route = proxy_route_factory("/test")
        config = InferencePrivacyProxyConfig(listen_port=proxy_listen_port, routes=[route])
        await manager.create_proxy("test", config)
        
        global_instance = manager.get_global_instance()
        global_instance.config.listen_port = 0
        
        result = await manager.start_proxy("test")
        assert result["state"] == "stopped"
        
        proxy = await manager.get_proxy("test")
        assert proxy["state"] == "stopped"
        
        logs = await manager.get_proxy_logs("test")
        assert "listen_port=0" in logs["logs"]
        assert "disabled" in logs["logs"].lower()
        
        await validate_proxy_not_listening(proxy_listen_port)

    @pytest.mark.asyncio
    async def test_start_proxy(self, manager, proxy_route_factory, simple_http_target, proxy_listen_port):
        route = proxy_route_factory("/test")
        config = InferencePrivacyProxyConfig(listen_port=proxy_listen_port, routes=[route])
        await manager.create_proxy("test", config)
        
        result = await manager.start_proxy("test")
        assert result["state"] == "running"
        
        proxy = await manager.get_proxy("test")
        assert proxy["state"] == "running"
        
        await validate_proxy_http(proxy_listen_port, "/test/v1/chat", expect_forward=True)
        
        await manager.stop_proxy("test")

    @pytest.mark.asyncio
    async def test_start_nonexistent_proxy_raises(self, manager):
        with pytest.raises(ValueError, match="not found"):
            await manager.start_proxy("nonexistent")

    @pytest.mark.asyncio
    async def test_stop_proxy(self, manager, proxy_route_factory, simple_http_target, proxy_listen_port):
        route = proxy_route_factory("/test")
        config = InferencePrivacyProxyConfig(listen_port=proxy_listen_port, routes=[route])
        await manager.create_proxy("test", config)
        await manager.start_proxy("test")
        
        await validate_proxy_http(proxy_listen_port, "/test/v1/chat", expect_forward=True)
        
        result = await manager.stop_proxy("test")
        assert result["state"] == "stopped"
        
        proxy = await manager.get_proxy("test")
        assert proxy["state"] == "stopped"
        
        await validate_proxy_not_listening(proxy_listen_port)

    @pytest.mark.asyncio
    async def test_independent_route_states(self, manager, proxy_route_factory, simple_http_target, proxy_listen_port):
        route1 = proxy_route_factory("/route1", api_key="key1")
        route2 = proxy_route_factory("/route2", api_key="key2")
        config = InferencePrivacyProxyConfig(
            listen_port=proxy_listen_port,
            routes=[route1, route2],
        )
        await manager.create_proxy("route1", config)
        
        await manager.start_proxy("route1")
        
        proxy1 = await manager.get_proxy("route1")
        proxy2 = await manager.get_proxy("route2")
        
        assert proxy1["state"] == "running"
        assert proxy2["state"] == "stopped"
        
        await validate_proxy_http(proxy_listen_port, "/route1/v1/chat", expect_forward=True)
        await validate_proxy_http(proxy_listen_port, "/route2/v1/chat", expect_forward=False)
        
        await manager.start_proxy("route2")
        
        proxy1 = await manager.get_proxy("route1")
        proxy2 = await manager.get_proxy("route2")
        
        assert proxy1["state"] == "running"
        assert proxy2["state"] == "running"
        
        await validate_proxy_http(proxy_listen_port, "/route1/v1/chat", expect_forward=True)
        await validate_proxy_http(proxy_listen_port, "/route2/v1/chat", expect_forward=True)
        
        await manager.stop_proxy("route1")
        await manager.stop_proxy("route2")

    @pytest.mark.asyncio
    async def test_stop_one_route_keeps_proxy_running(
        self,
        manager,
        proxy_route_factory,
        simple_http_target,
        proxy_listen_port,
    ):
        route1 = proxy_route_factory("/route1", api_key="key1")
        route2 = proxy_route_factory("/route2", api_key="key2")
        config = InferencePrivacyProxyConfig(
            listen_port=proxy_listen_port,
            routes=[route1, route2],
        )
        await manager.create_proxy("route1", config)
        
        await manager.start_proxy("route1")
        await manager.start_proxy("route2")
        
        await validate_proxy_http(proxy_listen_port, "/route1/v1/chat", expect_forward=True)
        await validate_proxy_http(proxy_listen_port, "/route2/v1/chat", expect_forward=True)
        
        await manager.stop_proxy("route1")
        
        proxy1 = await manager.get_proxy("route1")
        proxy2 = await manager.get_proxy("route2")
        
        assert proxy1["state"] == "stopped"
        assert proxy2["state"] == "running"
        
        await validate_proxy_http(proxy_listen_port, "/route1/v1/chat", expect_forward=False)
        await validate_proxy_http(proxy_listen_port, "/route2/v1/chat", expect_forward=True)
        
        await manager.stop_proxy("route2")


class TestProxyManagerLogs:
    """Test log operations."""

    @pytest.mark.asyncio
    async def test_get_proxy_logs(self, manager, proxy_route_factory, proxy_listen_port):
        route = proxy_route_factory("/test")
        config = InferencePrivacyProxyConfig(listen_port=proxy_listen_port, routes=[route])
        await manager.create_proxy("test", config)
        
        result = await manager.get_proxy_logs("test")
        assert "logs" in result
        assert "created" in result["logs"].lower() or "route" in result["logs"].lower()

    @pytest.mark.asyncio
    async def test_get_proxy_logs_with_lines_limit(self, manager, proxy_route_factory, proxy_listen_port):
        route = proxy_route_factory("/test")
        config = InferencePrivacyProxyConfig(listen_port=proxy_listen_port, routes=[route])
        await manager.create_proxy("test", config)
        
        result = await manager.get_proxy_logs("test", lines=5)
        assert "logs" in result

    @pytest.mark.asyncio
    async def test_get_logs_nonexistent_proxy_raises(self, manager):
        with pytest.raises(ValueError, match="not found"):
            await manager.get_proxy_logs("nonexistent")


class TestInferencePrivacyProxyUnit:
    """Unit tests for HTTP-aware proxy."""

    @pytest.mark.asyncio
    async def test_proxy_starts_and_stops(self):
        config = InferencePrivacyProxyConfig(
            listen_port=18080,
            routes=[
                ProxyRoute(
                    path_prefix="/test",
                    target_endpoint="http://127.0.0.1:9999",
                    api_key="sk-test-key",
                ),
            ],
        )
        proxy = InferencePrivacyProxy(config)
        proxy.enable_route("/test")
        await proxy.start()

        assert proxy.is_running
        assert proxy.server is not None

        await proxy.stop()
        assert not proxy.is_running

    @pytest.mark.asyncio
    async def test_route_matching(self):
        route1 = ProxyRoute(path_prefix="/openai", target_endpoint="https://api.openai.com")
        route2 = ProxyRoute(path_prefix="/anthropic", target_endpoint="https://api.anthropic.com")
        
        config = InferencePrivacyProxyConfig(routes=[route1, route2])
        proxy = InferencePrivacyProxy(config)
        
        proxy.enable_route("/openai")
        proxy.enable_route("/anthropic")
        
        assert proxy.match_route("/openai/v1/chat") == route1
        assert proxy.match_route("/anthropic/v1/messages") == route2
        assert proxy.match_route("/other/path") is None

    @pytest.mark.asyncio
    async def test_route_enable_disable(self):
        route1 = ProxyRoute(path_prefix="/openai", target_endpoint="https://api.openai.com")
        route2 = ProxyRoute(path_prefix="/anthropic", target_endpoint="https://api.anthropic.com")
        
        config = InferencePrivacyProxyConfig(routes=[route1, route2])
        proxy = InferencePrivacyProxy(config)
        
        proxy.enable_route("/openai")
        assert proxy.match_route("/openai/v1/chat") == route1
        assert proxy.match_route("/anthropic/v1/messages") is None
        
        proxy.enable_route("/anthropic")
        assert proxy.match_route("/anthropic/v1/messages") == route2
        
        proxy.disable_route("/openai")
        assert proxy.match_route("/openai/v1/chat") is None
        assert proxy.match_route("/anthropic/v1/messages") == route2

    @pytest.mark.asyncio
    async def test_path_rewrite(self):
        route = ProxyRoute(
            path_prefix="/llm-proxy",
            target_endpoint="https://api.example.com/v1",
        )
        
        config = InferencePrivacyProxyConfig(routes=[route])
        proxy = InferencePrivacyProxy(config)
        
        assert proxy.rewrite_path("/llm-proxy/chat/completions", route) == "/v1/chat/completions"
        assert proxy.rewrite_path("/llm-proxy/", route) == "/v1"
        assert proxy.rewrite_path("/llm-proxy", route) == "/v1"

    @pytest.mark.asyncio
    async def test_api_key_injection_openai_format(self):
        route = ProxyRoute(
            path_prefix="/test",
            target_endpoint="http://127.0.0.1:9999",
            api_key="sk-sandbox-key",
        )
        
        config = InferencePrivacyProxyConfig(routes=[route])
        proxy = InferencePrivacyProxy(config)
        
        headers = f"Authorization: Bearer {PLACEHOLDER}\r\nHost: example.com\r\n".encode()
        modified = proxy.inject_api_key(headers, route)
        
        assert "sk-sandbox-key" in modified.decode()
        assert PLACEHOLDER not in modified.decode()

    @pytest.mark.asyncio
    async def test_api_key_injection_anthropic_format(self):
        route = ProxyRoute(
            path_prefix="/test",
            target_endpoint="http://127.0.0.1:9999",
            api_key="sk-ant-api03-test-key",
        )
        
        config = InferencePrivacyProxyConfig(routes=[route])
        proxy = InferencePrivacyProxy(config)
        
        headers = f"X-Api-Key: {PLACEHOLDER}\r\nHost: example.com\r\n".encode()
        modified = proxy.inject_api_key(headers, route)
        
        assert "sk-ant-api03-test-key" in modified.decode()
        assert PLACEHOLDER not in modified.decode()


class TestInferencePrivacyProxyHTTPRouting:
    """Tests for actual HTTP request routing."""

    @pytest.mark.asyncio
    async def test_http_request_routing(self, proxy_listen_port, proxy_target_host):
        """Test HTTP request routing with mock target server."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("0.0.0.0", 0))
            target_port = s.getsockname()[1]

        async def target_server(reader, writer):
            data = await reader.read(8192)
            response = "HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nOK"
            writer.write(response.encode())
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_server(target_server, "0.0.0.0", target_port)
        server_task = asyncio.create_task(server.serve_forever())

        config = InferencePrivacyProxyConfig(
            listen_port=proxy_listen_port,
            routes=[
                ProxyRoute(
                    path_prefix="/test",
                    target_endpoint=f"http://{proxy_target_host}:{target_port}",
                    api_key="sk-test-key",
                ),
            ],
        )

        proxy = InferencePrivacyProxy(config)
        proxy.enable_route("/test")
        await proxy.start()

        try:
            request = (
                "POST /test/v1/chat HTTP/1.1\r\n"
                f"Authorization: Bearer {PLACEHOLDER}\r\n"
                "Content-Length: 13\r\n"
                "\r\n"
                '{"test": true}'
            )

            reader, writer = await asyncio.open_connection("127.0.0.1", proxy_listen_port)
            writer.write(request.encode())
            await writer.drain()

            response = await reader.read(8192)
            assert "200 OK" in response.decode()

            writer.close()
            await writer.wait_closed()
        finally:
            await proxy.stop()
            server.close()
            await server.wait_closed()
            server_task.cancel()

    @pytest.mark.asyncio
    async def test_no_route_matched_returns_404(self, proxy_listen_port):
        """Test that unmatched paths return 404."""
        config = InferencePrivacyProxyConfig(
            listen_port=proxy_listen_port,
            routes=[
                ProxyRoute(
                    path_prefix="/openai",
                    target_endpoint="https://api.openai.com",
                ),
            ],
        )

        proxy = InferencePrivacyProxy(config)
        proxy.enable_route("/openai")
        await proxy.start()

        try:
            request = "GET /other/path HTTP/1.1\r\nHost: localhost\r\n\r\n"

            reader, writer = await asyncio.open_connection("127.0.0.1", proxy_listen_port)
            writer.write(request.encode())
            await writer.drain()

            response = await reader.read(8192)
            assert "404 Not Found" in response.decode()
            assert "No route matched" in response.decode()

            writer.close()
            await writer.wait_closed()
        finally:
            await proxy.stop()

    @pytest.mark.asyncio
    async def test_disabled_route_returns_404(self, proxy_listen_port):
        """Test that disabled routes return 404."""
        config = InferencePrivacyProxyConfig(
            listen_port=proxy_listen_port,
            routes=[
                ProxyRoute(
                    path_prefix="/openai",
                    target_endpoint="https://api.openai.com",
                ),
            ],
        )

        proxy = InferencePrivacyProxy(config)
        await proxy.start()

        try:
            request = "GET /openai/v1/chat HTTP/1.1\r\nHost: localhost\r\n\r\n"

            reader, writer = await asyncio.open_connection("127.0.0.1", proxy_listen_port)
            writer.write(request.encode())
            await writer.drain()

            response = await reader.read(8192)
            assert "404 Not Found" in response.decode()
            assert "No route matched" in response.decode()

            writer.close()
            await writer.wait_closed()
        finally:
            await proxy.stop()


class TestInferencePrivacyProxyWithMockServer:
    """Tests using the mock_llm_server fixture."""

    @pytest.mark.asyncio
    async def test_http_proxy_with_mock_server(self, mock_llm_server_ready, proxy_listen_port, proxy_target_host):
        """Test HTTP proxy forwarding to mock LLM server."""
        http_port = mock_llm_server_ready["http_port"]

        config = InferencePrivacyProxyConfig(
            listen_port=proxy_listen_port,
            routes=[
                ProxyRoute(
                    path_prefix="/mock",
                    target_endpoint=f"http://{proxy_target_host}:{http_port}",
                    api_key="sk-sandbox-key",
                ),
            ],
        )

        proxy = InferencePrivacyProxy(config)
        proxy.enable_route("/mock")
        await proxy.start()

        try:
            request = (
                "POST /mock/v1/chat/completions HTTP/1.1\r\n"
                f"Authorization: Bearer {PLACEHOLDER}\r\n"
                "Content-Type: application/json\r\n"
                "Content-Length: 43\r\n"
                "\r\n"
                '{"model": "gpt-3.5-turbo", "messages": []}'
            )

            reader, writer = await asyncio.open_connection("127.0.0.1", proxy_listen_port)
            writer.write(request.encode())
            await writer.drain()

            response = await reader.read(4096)
            response_text = response.decode()

            assert "200 OK" in response_text
            assert "sk-sandbox-key" in response_text

            writer.close()
            await writer.wait_closed()
        finally:
            await proxy.stop()

    @pytest.mark.asyncio
    async def test_https_proxy_with_mock_server(self, mock_llm_server_ready, proxy_listen_port, proxy_target_host):
        """Test HTTPS proxy forwarding to mock LLM server."""
        https_port = mock_llm_server_ready["https_port"]

        config = InferencePrivacyProxyConfig(
            listen_port=proxy_listen_port,
            routes=[
                ProxyRoute(
                    path_prefix="/mock",
                    target_endpoint=f"https://{proxy_target_host}:{https_port}",
                    api_key="sk-sandbox-key",
                    skip_cert_verify=True,
                ),
            ],
        )

        proxy = InferencePrivacyProxy(config)
        proxy.enable_route("/mock")
        await proxy.start()

        try:
            request = (
                "POST /mock/v1/chat/completions HTTP/1.1\r\n"
                f"Authorization: Bearer {PLACEHOLDER}\r\n"
                "Content-Type: application/json\r\n"
                "Content-Length: 43\r\n"
                "\r\n"
                '{"model": "gpt-3.5-turbo", "messages": []}'
            )

            reader, writer = await asyncio.open_connection("127.0.0.1", proxy_listen_port)
            writer.write(request.encode())
            await writer.drain()

            response = await reader.read(4096)
            response_text = response.decode()

            assert "HTTP/1.1" in response_text
            assert "sk-sandbox-key" in response_text

            writer.close()
            await writer.wait_closed()
        finally:
            await proxy.stop()

    @pytest.mark.asyncio
    async def test_anthropic_api_key_injection(self, mock_llm_server_ready, proxy_listen_port, proxy_target_host):
        """Test Anthropic-style X-Api-Key header injection."""
        http_port = mock_llm_server_ready["http_port"]

        config = InferencePrivacyProxyConfig(
            listen_port=proxy_listen_port,
            routes=[
                ProxyRoute(
                    path_prefix="/anthropic",
                    target_endpoint=f"http://{proxy_target_host}:{http_port}",
                    api_key="sk-ant-api03-test-key",
                ),
            ],
        )

        proxy = InferencePrivacyProxy(config)
        proxy.enable_route("/anthropic")
        await proxy.start()

        try:
            request = (
                "POST /anthropic/v1/messages HTTP/1.1\r\n"
                f"X-Api-Key: {PLACEHOLDER}\r\n"
                "anthropic-version: 2023-06-01\r\n"
                "Content-Type: application/json\r\n"
                "Content-Length: 52\r\n"
                "\r\n"
                '{"model": "claude-3-opus", "max_tokens": 100, "messages": []}'
            )

            reader, writer = await asyncio.open_connection("127.0.0.1", proxy_listen_port)
            writer.write(request.encode())
            await writer.drain()

            response = await reader.read(4096)
            response_text = response.decode()

            assert "HTTP/1.1" in response_text
            assert "sk-ant-api03-test-key" in response_text

            writer.close()
            await writer.wait_closed()
        finally:
            await proxy.stop()


# =============================================================================
# HTTP API Integration Tests
# =============================================================================
# Tests below use real HTTP calls to the API server and proxy.
# These validate behavior from end-to-end perspective.


@pytest_asyncio.fixture(name="api_client")
async def _api_client_fixture(server_url):
    """Async HTTP client for API server."""
    async with httpx.AsyncClient(base_url=server_url, timeout=30.0) as client:
        yield client


@pytest_asyncio.fixture(name="test_route_cleanup")
async def _test_route_cleanup_fixture(api_client):
    """Track and cleanup test routes after each integration test."""
    created_routes = []
    yield created_routes
    for name in reversed(created_routes):
        try:
            await api_client.delete(f"/api/v1/proxies/{name}")
        except Exception as exc:
            logger.debug("Failed to cleanup route %s: %s", name, exc)


@pytest.fixture
def llm_test_settings(
    llm_available,
    test_llm_endpoint,
    test_llm_api_key,
    test_llm_model,
):
    return {
        "available": llm_available,
        "endpoint": test_llm_endpoint,
        "api_key": test_llm_api_key,
        "model": test_llm_model,
    }


@pytest.fixture
def llm_proxy_runtime(server_host_port, proxy_port, api_client, test_route_cleanup):
    return {
        "proxy_host": server_host_port[0],
        "proxy_port": proxy_port,
        "api_client": api_client,
        "route_cleanup": test_route_cleanup,
    }


async def _validate_proxy_forward(path: str, proxy_host: str, proxy_port: int, timeout: float = 2.0):
    """Validate route forwards request through proxy."""
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(proxy_host, proxy_port),
            timeout=timeout
        )
        
        request = f"GET {path} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n"
        writer.write(request.encode())
        await writer.drain()
        
        response = await asyncio.wait_for(reader.read(1024), timeout=timeout)
        response_text = response.decode()
        
        writer.close()
        await writer.wait_closed()
        
        assert "200 OK" in response_text, f"Expected 200 OK, got: {response_text[:200]}"
        
    except asyncio.TimeoutError:
        raise AssertionError(
            f"Timeout connecting to proxy at {proxy_host}:{proxy_port}. "
            f"Ensure proxy server is running and accessible."
        ) from None
    except ConnectionRefusedError:
        raise AssertionError(
            f"Proxy at {proxy_host}:{proxy_port} not listening. "
            f"Ensure proxy server is running."
        ) from None
    except OSError as e:
        raise AssertionError(
            f"Connection error to proxy at {proxy_host}:{proxy_port}: {e}"
        ) from e


async def _validate_route_disabled(path: str, proxy_host: str, proxy_port: int, timeout: float = 2.0):
    """Validate route is disabled (returns 404 or connection fails)."""
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(proxy_host, proxy_port),
            timeout=timeout
        )
        
        request = f"GET {path} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n"
        writer.write(request.encode())
        await writer.drain()
        
        response = await asyncio.wait_for(reader.read(1024), timeout=timeout)
        response_text = response.decode()
        
        writer.close()
        await writer.wait_closed()
        
        assert "404" in response_text or "No route matched" in response_text, \
            f"Expected 404 for disabled route, got: {response_text[:200]}"
        
    except asyncio.TimeoutError:
        return
    except OSError:
        return


class TestIntegrationProxyCRUD:
    """Test CRUD operations via HTTP API."""

    @pytest.mark.asyncio
    async def test_int_create_route(self, api_client, integration_target_endpoint, test_route_cleanup):
        response = await api_client.post(
            "/api/v1/proxies",
            json={
                "path_prefix": "/int-create",
                "target_endpoint": integration_target_endpoint,
            }
        )
        assert response.status_code == 201
        data = response.json()
        assert data["name"] == "int-create"
        assert data["state"] == "stopped"
        
        test_route_cleanup.append("int-create")
        
        response = await api_client.get("/api/v1/proxies/int-create")
        assert response.status_code == 200
        assert response.json()["name"] == "int-create"

    @pytest.mark.asyncio
    async def test_int_list_routes(self, api_client, integration_target_endpoint, test_route_cleanup):
        await api_client.post(
            "/api/v1/proxies",
            json={
                "path_prefix": "/int-list1",
                "target_endpoint": integration_target_endpoint,
            }
        )
        test_route_cleanup.append("int-list1")
        
        await api_client.post(
            "/api/v1/proxies",
            json={
                "path_prefix": "/int-list2",
                "target_endpoint": integration_target_endpoint,
            }
        )
        test_route_cleanup.append("int-list2")
        
        response = await api_client.get("/api/v1/proxies")
        assert response.status_code == 200
        routes = response.json()
        names = [r["name"] for r in routes]
        assert "int-list1" in names
        assert "int-list2" in names

    @pytest.mark.asyncio
    async def test_int_get_route(self, api_client, integration_target_endpoint, test_route_cleanup):
        await api_client.post(
            "/api/v1/proxies",
            json={
                "path_prefix": "/int-get",
                "target_endpoint": integration_target_endpoint,
            }
        )
        test_route_cleanup.append("int-get")
        
        response = await api_client.get("/api/v1/proxies/int-get")
        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "int-get"
        assert data["route"]["path_prefix"] == "/int-get"
        assert data["route"]["target_endpoint"] == integration_target_endpoint

    @pytest.mark.asyncio
    async def test_int_delete_route(self, api_client, integration_target_endpoint):
        await api_client.post(
            "/api/v1/proxies",
            json={
                "path_prefix": "/int-delete",
                "target_endpoint": integration_target_endpoint,
            }
        )
        
        response = await api_client.delete("/api/v1/proxies/int-delete")
        assert response.status_code == 200
        assert response.json()["deleted"] == True
        
        response = await api_client.get("/api/v1/proxies/int-delete")
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_int_update_route(self, api_client, integration_target_endpoint, test_route_cleanup):
        await api_client.post(
            "/api/v1/proxies",
            json={
                "path_prefix": "/int-update",
                "target_endpoint": integration_target_endpoint,
                "api_key": "old-key",
            }
        )
        test_route_cleanup.append("int-update")
        
        response = await api_client.put(
            "/api/v1/proxies/int-update",
            json={
                "path_prefix": "/int-update",
                "target_endpoint": integration_target_endpoint,
                "api_key": "new-key",
            }
        )
        assert response.status_code == 200
        
        response = await api_client.get("/api/v1/proxies/int-update")
        assert response.json()["route"]["api_key"] == "new-key"


class TestIntegrationProxyLifecycle:
    """Test lifecycle operations via HTTP API with real HTTP validation."""

    @pytest.mark.asyncio
    async def test_int_start_route_http_validation(
        self, api_client, integration_target_endpoint, proxy_host_from_test, proxy_port, test_route_cleanup
    ):
        await api_client.post(
            "/api/v1/proxies",
            json={
                "path_prefix": "/int-life-start",
                "target_endpoint": integration_target_endpoint,
            }
        )
        test_route_cleanup.append("int-life-start")
        
        response = await api_client.post("/api/v1/proxies/int-life-start/start")
        assert response.status_code == 200
        assert response.json()["state"] == "running"
        
        await _validate_proxy_forward("/int-life-start/v1/chat", proxy_host_from_test, proxy_port)

    @pytest.mark.asyncio
    async def test_int_stop_route_http_validation(
        self, api_client, integration_target_endpoint, proxy_host_from_test, proxy_port, test_route_cleanup
    ):
        await api_client.post(
            "/api/v1/proxies",
            json={
                "path_prefix": "/int-life-stop",
                "target_endpoint": integration_target_endpoint,
            }
        )
        test_route_cleanup.append("int-life-stop")
        
        await api_client.post("/api/v1/proxies/int-life-stop/start")
        
        await _validate_proxy_forward("/int-life-stop/v1/chat", proxy_host_from_test, proxy_port)
        
        response = await api_client.post("/api/v1/proxies/int-life-stop/stop")
        assert response.status_code == 200
        assert response.json()["state"] == "stopped"
        
        await _validate_route_disabled("/int-life-stop/v1/chat", proxy_host_from_test, proxy_port)

    @pytest.mark.asyncio
    async def test_int_start_already_running(
        self, api_client, integration_target_endpoint, test_route_cleanup
    ):
        await api_client.post(
            "/api/v1/proxies",
            json={
                "path_prefix": "/int-life-dup",
                "target_endpoint": integration_target_endpoint,
            }
        )
        test_route_cleanup.append("int-life-dup")
        
        await api_client.post("/api/v1/proxies/int-life-dup/start")
        
        response = await api_client.post("/api/v1/proxies/int-life-dup/start")
        assert response.status_code == 200
        assert response.json()["state"] == "running"

    @pytest.mark.asyncio
    async def test_int_operations_on_nonexistent(self, api_client):
        response = await api_client.get("/api/v1/proxies/nonexistent-route")
        assert response.status_code == 404
        
        response = await api_client.delete("/api/v1/proxies/nonexistent-route")
        assert response.status_code == 404
        
        response = await api_client.post("/api/v1/proxies/nonexistent-route/start")
        assert response.status_code == 404
        
        response = await api_client.post("/api/v1/proxies/nonexistent-route/stop")
        assert response.status_code == 404


class TestIntegrationProxyIndependentRoutes:
    """Test independent route states with HTTP validation."""

    @pytest.mark.asyncio
    async def test_int_independent_start_stop(
        self, api_client, integration_target_endpoint, proxy_host_from_test, proxy_port, test_route_cleanup
    ):
        await api_client.post(
            "/api/v1/proxies",
            json={
                "path_prefix": "/int-ind1",
                "target_endpoint": integration_target_endpoint,
            }
        )
        test_route_cleanup.append("int-ind1")
        
        await api_client.post(
            "/api/v1/proxies",
            json={
                "path_prefix": "/int-ind2",
                "target_endpoint": integration_target_endpoint,
            }
        )
        test_route_cleanup.append("int-ind2")
        
        await api_client.post("/api/v1/proxies/int-ind1/start")
        
        await _validate_proxy_forward("/int-ind1/v1/chat", proxy_host_from_test, proxy_port)
        await _validate_route_disabled("/int-ind2/v1/chat", proxy_host_from_test, proxy_port)
        
        await api_client.post("/api/v1/proxies/int-ind2/start")
        
        await _validate_proxy_forward("/int-ind1/v1/chat", proxy_host_from_test, proxy_port)
        await _validate_proxy_forward("/int-ind2/v1/chat", proxy_host_from_test, proxy_port)

    @pytest.mark.asyncio
    async def test_int_stop_one_keeps_other_running(
        self, api_client, integration_target_endpoint, proxy_host_from_test, proxy_port, test_route_cleanup
    ):
        await api_client.post(
            "/api/v1/proxies",
            json={
                "path_prefix": "/int-ind3",
                "target_endpoint": integration_target_endpoint,
            }
        )
        test_route_cleanup.append("int-ind3")
        
        await api_client.post(
            "/api/v1/proxies",
            json={
                "path_prefix": "/int-ind4",
                "target_endpoint": integration_target_endpoint,
            }
        )
        test_route_cleanup.append("int-ind4")
        
        await api_client.post("/api/v1/proxies/int-ind3/start")
        await api_client.post("/api/v1/proxies/int-ind4/start")
        
        await _validate_proxy_forward("/int-ind3/v1/chat", proxy_host_from_test, proxy_port)
        await _validate_proxy_forward("/int-ind4/v1/chat", proxy_host_from_test, proxy_port)
        
        await api_client.post("/api/v1/proxies/int-ind3/stop")
        
        await _validate_route_disabled("/int-ind3/v1/chat", proxy_host_from_test, proxy_port)
        await _validate_proxy_forward("/int-ind4/v1/chat", proxy_host_from_test, proxy_port)

    @pytest.mark.asyncio
    async def test_int_shared_port_validation(
        self, api_client, integration_target_endpoint, proxy_host_from_test, proxy_port, test_route_cleanup
    ):
        await api_client.post(
            "/api/v1/proxies",
            json={
                "path_prefix": "/int-share1",
                "target_endpoint": integration_target_endpoint,
            }
        )
        test_route_cleanup.append("int-share1")
        
        await api_client.post(
            "/api/v1/proxies",
            json={
                "path_prefix": "/int-share2",
                "target_endpoint": integration_target_endpoint,
            }
        )
        test_route_cleanup.append("int-share2")
        
        await api_client.post(
            "/api/v1/proxies",
            json={
                "path_prefix": "/int-share3",
                "target_endpoint": integration_target_endpoint,
            }
        )
        test_route_cleanup.append("int-share3")
        
        await api_client.post("/api/v1/proxies/int-share1/start")
        await api_client.post("/api/v1/proxies/int-share2/start")
        await api_client.post("/api/v1/proxies/int-share3/start")
        
        await _validate_proxy_forward("/int-share1/v1/chat", proxy_host_from_test, proxy_port)
        await _validate_proxy_forward("/int-share2/v1/chat", proxy_host_from_test, proxy_port)
        await _validate_proxy_forward("/int-share3/v1/chat", proxy_host_from_test, proxy_port)


class TestIntegrationProxyErrorHandling:
    """Test error handling via HTTP API."""

    @pytest.mark.asyncio
    async def test_int_create_duplicate_route(self, api_client, integration_target_endpoint, test_route_cleanup):
        await api_client.post(
            "/api/v1/proxies",
            json={
                "path_prefix": "/int-dup",
                "target_endpoint": integration_target_endpoint,
            }
        )
        test_route_cleanup.append("int-dup")
        
        response = await api_client.post(
            "/api/v1/proxies",
            json={
                "path_prefix": "/int-dup",
                "target_endpoint": integration_target_endpoint,
            }
        )
        assert response.status_code == 400
        detail = response.json().get("detail", "")
        error = response.json().get("error", "")
        assert "already exists" in detail or "already exists" in error

    @pytest.mark.asyncio
    async def test_int_create_invalid_target_endpoint(self, api_client):
        response = await api_client.post(
            "/api/v1/proxies",
            json={
                "path_prefix": "/int-invalid",
                "target_endpoint": "not-a-valid-url",
            }
        )
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_int_create_empty_path_prefix(self, api_client, integration_target_endpoint):
        response = await api_client.post(
            "/api/v1/proxies",
            json={
                "path_prefix": "",
                "target_endpoint": integration_target_endpoint,
            }
        )
        assert response.status_code == 400


class TestThreadingProxyConnection:
    """Test concurrent proxy connections using threading."""

    @staticmethod
    def test_01_concurrent_forward_strict(
        integration_target_endpoint, server_url, proxy_host_from_test, proxy_port
    ):
        """5 threads with unique routes - all must succeed."""
        results = []

        def worker(thread_id):
            with httpx.Client(base_url=server_url, timeout=30.0) as client:
                try:
                    resp = client.post("/api/v1/proxies", json={
                        "path_prefix": f"/thread-strict-{thread_id}",
                        "target_endpoint": integration_target_endpoint,
                    })
                    if resp.status_code != 201:
                        results.append((thread_id, "create_failed", resp.status_code))
                        return

                    resp = client.post(f"/api/v1/proxies/thread-strict-{thread_id}/start")
                    if resp.status_code != 200:
                        results.append((thread_id, "start_failed", resp.status_code))
                        return

                    response = _proxy_http_get(
                        proxy_host_from_test,
                        proxy_port,
                        f"/thread-strict-{thread_id}/v1/chat",
                    )

                    if "200 OK" not in response:
                        results.append((thread_id, "forward_failed", response[:100]))
                        return

                    client.delete(f"/api/v1/proxies/thread-strict-{thread_id}")
                    results.append((thread_id, "success", None))
                except Exception as e:
                    results.append((thread_id, "exception", str(e)))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(NUM_THREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        successes = [r for r in results if r[1] == "success"]
        assert len(successes) == NUM_THREADS, f"Expected {NUM_THREADS} successes, got {len(successes)}: {results}"

    @staticmethod
    def test_02_concurrent_forward_relaxed(
        integration_target_endpoint, server_url, proxy_host_from_test, proxy_port
    ):
        """5 threads competing for same route - validate error handling."""
        results = []
        create_winner_found = [False]
        route_started = threading.Event()

        def worker(thread_id):
            with httpx.Client(base_url=server_url, timeout=30.0) as client:
                try:
                    resp = client.post("/api/v1/proxies", json={
                        "path_prefix": "/thread-relaxed",
                        "target_endpoint": integration_target_endpoint,
                    })

                    if resp.status_code == 201:
                        if not create_winner_found[0]:
                            create_winner_found[0] = True
                            results.append((thread_id, "create_success", 201))

                            resp = client.post("/api/v1/proxies/thread-relaxed/start")
                            if resp.status_code != 200:
                                results.append((thread_id, "start_failed", resp.status_code))
                                return
                            route_started.set()
                        else:
                            results.append((thread_id, "create_duplicate_success", 201))
                    elif resp.status_code == 400:
                        detail = resp.json().get("detail", "")
                        if "already exists" in detail:
                            results.append((thread_id, "create_failed_expected", detail))
                            route_started.wait(timeout=5.0)
                        else:
                            results.append((thread_id, "create_failed_unexpected", detail))
                            return
                    else:
                        results.append((thread_id, "create_unexpected", resp.status_code))
                        return

                    for attempt in range(10):
                        response = _proxy_http_get(
                            proxy_host_from_test,
                            proxy_port,
                            "/thread-relaxed/v1/chat",
                        )
                        if "200 OK" in response:
                            results.append((thread_id, "connect_success", None))
                            break
                        if "404 Not Found" in response:
                            time.sleep(0.1)
                            continue
                        results.append((thread_id, "connect_failed", response[:100]))
                        break
                    else:
                        results.append(
                            (thread_id, "connect_failed", "route did not become ready in time")
                        )
                except Exception as e:
                    results.append((thread_id, "exception", str(e)))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(NUM_THREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        with httpx.Client(base_url=server_url, timeout=30.0) as client:
            try:
                client.delete("/api/v1/proxies/thread-relaxed")
            except Exception as exc:
                logger.debug("Cleanup for thread-relaxed failed: %s", exc)

        create_successes = [r for r in results if r[1] == "create_success"]
        assert len(create_successes) >= 1, f"Expected at least 1 create success, got: {results}"

        create_failures = [r for r in results if r[1] == "create_failed_expected"]
        assert len(create_failures) >= 1, f"Expected at least 1 'already exists' failure, got: {results}"

        connect_successes = [r for r in results if r[1] == "connect_success"]
        assert len(connect_successes) == NUM_THREADS, f"Expected {NUM_THREADS} connect successes, got: {results}"


class TestThreadingCRUD:
    """Test concurrent CRUD operations using threading."""

    @staticmethod
    def test_03_concurrent_crud_strict(
        integration_target_endpoint, server_url, proxy_host_from_test, proxy_port
    ):
        """5 threads with unique routes - all must succeed."""
        results = []

        def worker(thread_id):
            with httpx.Client(base_url=server_url, timeout=30.0) as client:
                try:
                    resp = client.post("/api/v1/proxies", json={
                        "path_prefix": f"/crud-strict-{thread_id}",
                        "target_endpoint": integration_target_endpoint,
                    })
                    if resp.status_code != 201:
                        results.append((thread_id, "create_failed", resp.status_code))
                        return

                    resp = client.post(f"/api/v1/proxies/crud-strict-{thread_id}/start")
                    if resp.status_code != 200:
                        results.append((thread_id, "start_failed", resp.status_code))
                        return

                    resp = client.get(f"/api/v1/proxies/crud-strict-{thread_id}")
                    if resp.status_code != 200:
                        results.append((thread_id, "get_failed", resp.status_code))
                        return

                    data = resp.json()
                    if data.get("state") != "running":
                        results.append((thread_id, "state_wrong", data.get("state")))
                        return

                    resp = client.post(f"/api/v1/proxies/crud-strict-{thread_id}/stop")
                    if resp.status_code != 200:
                        results.append((thread_id, "stop_failed", resp.status_code))
                        return

                    resp = client.delete(f"/api/v1/proxies/crud-strict-{thread_id}")
                    if resp.status_code != 200:
                        results.append((thread_id, "delete_failed", resp.status_code))
                        return

                    results.append((thread_id, "success", None))
                except Exception as e:
                    results.append((thread_id, "exception", str(e)))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(NUM_THREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        successes = [r for r in results if r[1] == "success"]
        assert len(successes) == NUM_THREADS, f"Expected {NUM_THREADS} successes, got {len(successes)}: {results}"

    @staticmethod
    def test_04_concurrent_crud_relaxed(
        integration_target_endpoint, server_url, proxy_host_from_test, proxy_port
    ):
        """5 threads competing for same route - validate error handling."""
        results = []
        create_winner_found = [False]

        def worker(thread_id):
            with httpx.Client(base_url=server_url, timeout=30.0) as client:
                try:
                    resp = client.post("/api/v1/proxies", json={
                        "path_prefix": "/crud-relaxed",
                        "target_endpoint": integration_target_endpoint,
                    })

                    if resp.status_code == 201:
                        if not create_winner_found[0]:
                            create_winner_found[0] = True
                            results.append((thread_id, "create_success", 201))

                            resp = client.post("/api/v1/proxies/crud-relaxed/start")
                            if resp.status_code != 200:
                                results.append((thread_id, "start_failed", resp.status_code))
                                return
                        else:
                            results.append((thread_id, "create_duplicate_success", 201))
                    elif resp.status_code == 400:
                        detail = resp.json().get("detail", "")
                        if "already exists" in detail:
                            results.append((thread_id, "create_failed_expected", detail))
                        else:
                            results.append((thread_id, "create_failed_unexpected", detail))
                            return
                    else:
                        results.append((thread_id, "create_unexpected", resp.status_code))
                        return

                    resp = client.get("/api/v1/proxies/crud-relaxed")
                    if resp.status_code == 200:
                        data = resp.json()
                        results.append((thread_id, "get_success", data.get("state")))
                    elif resp.status_code == 404:
                        results.append((thread_id, "get_notfound", 404))
                    else:
                        results.append((thread_id, "get_failed", resp.status_code))
                except Exception as e:
                    results.append((thread_id, "exception", str(e)))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(NUM_THREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        with httpx.Client(base_url=server_url, timeout=30.0) as client:
            try:
                client.post("/api/v1/proxies/crud-relaxed/stop")
                client.delete("/api/v1/proxies/crud-relaxed")
            except Exception as exc:
                logger.debug("Cleanup for crud-relaxed failed: %s", exc)

        create_successes = [r for r in results if r[1] == "create_success"]
        assert len(create_successes) >= 1, f"Expected at least 1 create success, got: {results}"

        create_failures = [r for r in results if r[1] == "create_failed_expected"]
        assert len(create_failures) >= 1, f"Expected at least 1 'already exists' failure, got: {results}"

        get_successes = [r for r in results if r[1] == "get_success"]
        assert len(get_successes) == NUM_THREADS, f"Expected {NUM_THREADS} GET successes, got: {results}"


class TestInputValidation:
    """Test input validation for path_prefix, api_key, and target_endpoint."""

    @pytest.mark.asyncio
    async def test_path_prefix_with_crlf_rejected(self, api_client, integration_target_endpoint):
        response = await api_client.post(
            "/api/v1/proxies",
            json={
                "path_prefix": "/test\r\nInjected: header",
                "target_endpoint": integration_target_endpoint,
            }
        )
        assert response.status_code == 400
        assert "invalid characters" in response.json().get("detail", "").lower()

    @pytest.mark.asyncio
    async def test_path_prefix_with_null_byte_rejected(self, api_client, integration_target_endpoint):
        response = await api_client.post(
            "/api/v1/proxies",
            json={
                "path_prefix": "/test\x00hidden",
                "target_endpoint": integration_target_endpoint,
            }
        )
        assert response.status_code == 400
        assert "invalid characters" in response.json().get("detail", "").lower()

    @pytest.mark.asyncio
    async def test_path_prefix_with_path_traversal_rejected(self, api_client, integration_target_endpoint):
        response = await api_client.post(
            "/api/v1/proxies",
            json={
                "path_prefix": "/test/../escape",
                "target_endpoint": integration_target_endpoint,
            }
        )
        assert response.status_code == 400
        assert "path traversal" in response.json().get("detail", "").lower()

    @pytest.mark.asyncio
    async def test_path_prefix_with_url_encoded_crlf_rejected(self, api_client, integration_target_endpoint):
        response = await api_client.post(
            "/api/v1/proxies",
            json={
                "path_prefix": "/test%0d%0aInjected: header",
                "target_endpoint": integration_target_endpoint,
            }
        )
        assert response.status_code == 400
        assert "invalid characters" in response.json().get("detail", "").lower()

    @pytest.mark.asyncio
    async def test_path_prefix_with_url_encoded_path_traversal_rejected(self, api_client, integration_target_endpoint):
        response = await api_client.post(
            "/api/v1/proxies",
            json={
                "path_prefix": "/test%2e%2e/escape",
                "target_endpoint": integration_target_endpoint,
            }
        )
        assert response.status_code == 400
        assert "path traversal" in response.json().get("detail", "").lower()

    @pytest.mark.asyncio
    async def test_path_prefix_normalization(self, api_client, integration_target_endpoint, test_route_cleanup):
        response = await api_client.post(
            "/api/v1/proxies",
            json={
                "path_prefix": "test/",  # missing / prefix, has trailing /
                "target_endpoint": integration_target_endpoint,
            }
        )
        assert response.status_code == 201
        
        data = response.json()
        assert data["name"] == "test"
        
        test_route_cleanup.append("test")
        
        get_response = await api_client.get("/api/v1/proxies/test")
        assert get_response.json()["route"]["path_prefix"] == "/test"

    @pytest.mark.asyncio
    async def test_api_key_with_crlf_rejected(self, api_client, integration_target_endpoint):
        response = await api_client.post(
            "/api/v1/proxies",
            json={
                "path_prefix": "/test-crlf-key",
                "target_endpoint": integration_target_endpoint,
                "api_key": "key\r\nX-Injected: malicious",
            }
        )
        assert response.status_code == 400
        assert "invalid characters" in response.json().get("detail", "").lower()

    @pytest.mark.asyncio
    async def test_api_key_with_null_byte_rejected(self, api_client, integration_target_endpoint):
        response = await api_client.post(
            "/api/v1/proxies",
            json={
                "path_prefix": "/test-null-key",
                "target_endpoint": integration_target_endpoint,
                "api_key": "key\x00hidden",
            }
        )
        assert response.status_code == 400
        assert "invalid characters" in response.json().get("detail", "").lower()

    @pytest.mark.asyncio
    async def test_api_key_with_url_encoded_crlf_rejected(self, api_client, integration_target_endpoint):
        response = await api_client.post(
            "/api/v1/proxies",
            json={
                "path_prefix": "/test-url-key",
                "target_endpoint": integration_target_endpoint,
                "api_key": "key%0d%0aInjected",
            }
        )
        assert response.status_code == 400
        assert "invalid characters" in response.json().get("detail", "").lower()

    @pytest.mark.asyncio
    async def test_valid_target_endpoint_accepted(self, api_client, integration_target_endpoint, test_route_cleanup):
        response = await api_client.post(
            "/api/v1/proxies",
            json={
                "path_prefix": "/valid-target",
                "target_endpoint": integration_target_endpoint,
            }
        )
        assert response.status_code == 201
        
        test_route_cleanup.append("valid-target")

    @pytest.mark.asyncio
    async def test_update_validates_path_prefix(self, api_client, integration_target_endpoint, test_route_cleanup):
        await api_client.post(
            "/api/v1/proxies",
            json={
                "path_prefix": "/update-test",
                "target_endpoint": integration_target_endpoint,
            }
        )
        test_route_cleanup.append("update-test")
        
        response = await api_client.put(
            "/api/v1/proxies/update-test",
            json={
                "path_prefix": "/update-test\r\nbad",
                "target_endpoint": integration_target_endpoint,
            }
        )
        assert response.status_code == 400
        assert "invalid characters" in response.json().get("detail", "").lower()

    @pytest.mark.asyncio
    async def test_update_validates_api_key(self, api_client, integration_target_endpoint, test_route_cleanup):
        await api_client.post(
            "/api/v1/proxies",
            json={
                "path_prefix": "/update-key",
                "target_endpoint": integration_target_endpoint,
            }
        )
        test_route_cleanup.append("update-key")
        
        response = await api_client.put(
            "/api/v1/proxies/update-key",
            json={
                "path_prefix": "/update-key",
                "target_endpoint": integration_target_endpoint,
                "api_key": "key\r\nbad",
            }
        )
        assert response.status_code == 400
        assert "invalid characters" in response.json().get("detail", "").lower()

    @pytest.mark.asyncio
    async def test_proxy_name_with_crlf_rejected(self, api_client, integration_target_endpoint, test_route_cleanup):
        await api_client.post(
            "/api/v1/proxies",
            json={
                "path_prefix": "/test-proxy-name",
                "target_endpoint": integration_target_endpoint,
            }
        )
        test_route_cleanup.append("test-proxy-name")
        
        response = await api_client.get("/api/v1/proxies/test-proxy%0d%0aname")
        assert response.status_code == 400
        assert "invalid characters" in response.json().get("detail", "").lower()

    @pytest.mark.asyncio
    async def test_proxy_name_with_special_chars_rejected(
        self,
        api_client,
        integration_target_endpoint,
        test_route_cleanup,
    ):
        await api_client.post(
            "/api/v1/proxies",
            json={
                "path_prefix": "/test-proxy-special",
                "target_endpoint": integration_target_endpoint,
            }
        )
        test_route_cleanup.append("test-proxy-special")
        
        response = await api_client.get("/api/v1/proxies/test-proxy!@#$")
        assert response.status_code == 400
        assert "alphanumeric" in response.json().get("detail", "").lower()

    @pytest.mark.asyncio
    async def test_proxy_name_with_space_rejected(self, api_client):
        response = await api_client.get("/api/v1/proxies/test proxy")
        assert response.status_code == 400
        assert "alphanumeric" in response.json().get("detail", "").lower()

    @pytest.mark.asyncio
    async def test_proxy_name_with_dot_rejected(self, api_client):
        response = await api_client.get("/api/v1/proxies/test.proxy")
        assert response.status_code == 400
        assert "alphanumeric" in response.json().get("detail", "").lower()

    @pytest.mark.asyncio
    async def test_proxy_name_valid_accepted(self, api_client, integration_target_endpoint, test_route_cleanup):
        await api_client.post(
            "/api/v1/proxies",
            json={
                "path_prefix": "/test-valid-name_123",
                "target_endpoint": integration_target_endpoint,
            }
        )
        test_route_cleanup.append("test-valid-name_123")
        
        response = await api_client.get("/api/v1/proxies/test-valid-name_123")
        assert response.status_code == 200
        assert response.json()["name"] == "test-valid-name_123"


class TestModelValidation:
    """Test Pydantic model validation (YAML + API both validated)."""

    @staticmethod
    def test_listen_port_out_of_range_rejected():
        from jiuwenbox.models.policy import InferencePrivacyProxyPolicy
        
        with pytest.raises(ValueError, match="listen_port must be between"):
            InferencePrivacyProxyPolicy(listen_port=70000, listen_host="0.0.0.0")

    @staticmethod
    def test_listen_port_negative_rejected():
        from jiuwenbox.models.policy import InferencePrivacyProxyPolicy
        
        with pytest.raises(ValueError, match="listen_port must be between"):
            InferencePrivacyProxyPolicy(listen_port=-1, listen_host="0.0.0.0")

    @staticmethod
    def test_listen_port_valid_accepted():
        from jiuwenbox.models.policy import InferencePrivacyProxyPolicy
        
        policy = InferencePrivacyProxyPolicy(listen_port=8080, listen_host="127.0.0.1")
        assert policy.listen_port == 8080

    @staticmethod
    def test_path_prefix_empty_rejected():
        from jiuwenbox.models.policy import ProxyRouteEntry
        
        with pytest.raises(ValueError, match="path_prefix cannot be empty"):
            ProxyRouteEntry(path_prefix="", target_endpoint="https://api.openai.com")

    @staticmethod
    def test_path_prefix_with_crlf_rejected():
        from jiuwenbox.models.policy import ProxyRouteEntry
        
        with pytest.raises(ValueError, match="invalid characters"):
            ProxyRouteEntry(path_prefix="/test\r\nbad", target_endpoint="https://api.openai.com")

    @staticmethod
    def test_path_prefix_with_control_chars_rejected():
        from jiuwenbox.models.policy import ProxyRouteEntry
        
        with pytest.raises(ValueError, match="invalid characters"):
            ProxyRouteEntry(path_prefix="/test\x01bad", target_endpoint="https://api.openai.com")

    @staticmethod
    def test_api_key_with_crlf_rejected():
        from jiuwenbox.models.policy import ProxyRouteEntry
        
        with pytest.raises(ValueError, match="invalid characters"):
            ProxyRouteEntry(
                path_prefix="/test",
                target_endpoint="https://api.openai.com",
                api_key="key\r\nInjected: bad"
            )

    @staticmethod
    def test_api_key_with_control_chars_rejected():
        from jiuwenbox.models.policy import ProxyRouteEntry
        
        with pytest.raises(ValueError, match="invalid characters"):
            ProxyRouteEntry(
                path_prefix="/test",
                target_endpoint="https://api.openai.com",
                api_key="key\x01hidden"
            )

    @staticmethod
    def test_target_endpoint_invalid_scheme_rejected():
        from jiuwenbox.models.policy import ProxyRouteEntry
        
        with pytest.raises(ValueError, match="http or https"):
            ProxyRouteEntry(path_prefix="/test", target_endpoint="ftp://example.com")

    @staticmethod
    def test_target_endpoint_empty_rejected():
        from jiuwenbox.models.policy import ProxyRouteEntry
        
        with pytest.raises(ValueError, match="target_endpoint cannot be empty"):
            ProxyRouteEntry(path_prefix="/test", target_endpoint="")

    @staticmethod
    def test_target_endpoint_with_crlf_rejected():
        from jiuwenbox.models.policy import ProxyRouteEntry
        
        with pytest.raises(ValueError, match="invalid characters"):
            ProxyRouteEntry(path_prefix="/test", target_endpoint="https://api.openai.com\r\nbad")

    @staticmethod
    def test_listen_host_invalid_ip_rejected():
        from jiuwenbox.models.policy import InferencePrivacyProxyPolicy
        
        with pytest.raises(ValueError, match="valid IP address"):
            InferencePrivacyProxyPolicy(listen_port=8080, listen_host="invalid-host")

    @staticmethod
    def test_listen_host_ipv6_accepted():
        from jiuwenbox.models.policy import InferencePrivacyProxyPolicy
        
        policy = InferencePrivacyProxyPolicy(listen_port=8080, listen_host="::1")
        assert policy.listen_host == "::1"

    @staticmethod
    def test_path_prefix_root_rejected():
        """Root path '/' should be rejected to prevent catch-all blocking other routes."""
        from jiuwenbox.models.policy import ProxyRouteEntry
        
        with pytest.raises(ValueError, match="root path"):
            ProxyRouteEntry(path_prefix="/", target_endpoint="http://example.com")

    @staticmethod
    def test_path_prefix_multi_level_rejected():
        """Path prefix with internal slashes should be rejected (single-level only)."""
        from jiuwenbox.models.policy import ProxyRouteEntry
        
        invalid_prefixes = [
            "/api/v1",
            "/openai/chat",
            "/v1/models",
            "api/v1",
        ]
        
        for prefix in invalid_prefixes:
            with pytest.raises(ValueError, match="single-level"):
                ProxyRouteEntry(path_prefix=prefix, target_endpoint="http://example.com")

    @staticmethod
    def test_path_prefix_single_level_accepted():
        """Single-level path prefix should be accepted."""
        from jiuwenbox.models.policy import ProxyRouteEntry
        
        valid_prefixes = [
            "/api",
            "/api-v2",
            "/v1",
            "/llm-proxy",
            "api",
        ]
        
        for prefix in valid_prefixes:
            route = ProxyRouteEntry(path_prefix=prefix, target_endpoint="http://example.com")
            expected = prefix if prefix.startswith("/") else "/" + prefix
            assert route.path_prefix == expected


class TestRouteLongestPrefixMatching:
    """Test that longest-prefix matching prevents route collision."""

    @pytest.mark.asyncio
    async def test_longer_prefix_wins_over_string_prefix_collision(
        self, mock_llm_server_ready, proxy_listen_port, proxy_target_host
    ):
        """Test '/api-v2' wins over '/api' for request '/api-v2/chat'."""
        http_port = mock_llm_server_ready["http_port"]

        config = InferencePrivacyProxyConfig(
            listen_port=proxy_listen_port,
            routes=[
                ProxyRoute(
                    path_prefix="/api",
                    target_endpoint=f"http://{proxy_target_host}:{http_port}",
                    api_key="generic-key",
                ),
                ProxyRoute(
                    path_prefix="/api-v2",
                    target_endpoint=f"http://{proxy_target_host}:{http_port}",
                    api_key="specific-key",
                ),
            ],
        )

        proxy = InferencePrivacyProxy(config)
        proxy.enable_route("/api")
        proxy.enable_route("/api-v2")
        await proxy.start()

        try:
            request = (
                "POST /api-v2/chat HTTP/1.1\r\n"
                f"Authorization: Bearer {PLACEHOLDER}\r\n"
                "Content-Length: 13\r\n"
                "\r\n"
                '{"test": true}'
            )

            reader, writer = await asyncio.open_connection("127.0.0.1", proxy_listen_port)
            writer.write(request.encode())
            await writer.drain()

            response = await reader.read(4096)
            response_text = response.decode()

            assert "specific-key" in response_text
            assert "generic-key" not in response_text

            writer.close()
            await writer.wait_closed()
        finally:
            await proxy.stop()

    @pytest.mark.asyncio
    async def test_shorter_prefix_matches_when_no_longer_match(
        self, mock_llm_server_ready, proxy_listen_port, proxy_target_host
    ):
        """Test '/api' matches '/api/chat' when '/api-v2' doesn't match."""
        http_port = mock_llm_server_ready["http_port"]

        config = InferencePrivacyProxyConfig(
            listen_port=proxy_listen_port,
            routes=[
                ProxyRoute(
                    path_prefix="/api",
                    target_endpoint=f"http://{proxy_target_host}:{http_port}",
                    api_key="generic-key",
                ),
                ProxyRoute(
                    path_prefix="/api-v2",
                    target_endpoint=f"http://{proxy_target_host}:{http_port}",
                    api_key="specific-key",
                ),
            ],
        )

        proxy = InferencePrivacyProxy(config)
        proxy.enable_route("/api")
        proxy.enable_route("/api-v2")
        await proxy.start()

        try:
            request = (
                "POST /api/chat HTTP/1.1\r\n"
                f"Authorization: Bearer {PLACEHOLDER}\r\n"
                "Content-Length: 13\r\n"
                "\r\n"
                '{"test": true}'
            )

            reader, writer = await asyncio.open_connection("127.0.0.1", proxy_listen_port)
            writer.write(request.encode())
            await writer.drain()

            response = await reader.read(4096)
            response_text = response.decode()

            assert "generic-key" in response_text
            assert "specific-key" not in response_text

            writer.close()
            await writer.wait_closed()
        finally:
            await proxy.stop()

    @pytest.mark.asyncio
    async def test_route_order_does_not_affect_matching(
        self, mock_llm_server_ready, proxy_listen_port, proxy_target_host
    ):
        """Route order should not affect longest-prefix matching."""
        http_port = mock_llm_server_ready["http_port"]

        config = InferencePrivacyProxyConfig(
            listen_port=proxy_listen_port,
            routes=[
                ProxyRoute(
                    path_prefix="/api-v2",
                    target_endpoint=f"http://{proxy_target_host}:{http_port}",
                    api_key="specific-key",
                ),
                ProxyRoute(
                    path_prefix="/api",
                    target_endpoint=f"http://{proxy_target_host}:{http_port}",
                    api_key="generic-key",
                ),
            ],
        )

        proxy = InferencePrivacyProxy(config)
        proxy.enable_route("/api")
        proxy.enable_route("/api-v2")
        await proxy.start()

        try:
            request = (
                "POST /api-v2/chat HTTP/1.1\r\n"
                f"Authorization: Bearer {PLACEHOLDER}\r\n"
                "Content-Length: 13\r\n"
                "\r\n"
                '{"test": true}'
            )

            reader, writer = await asyncio.open_connection("127.0.0.1", proxy_listen_port)
            writer.write(request.encode())
            await writer.drain()

            response = await reader.read(4096)
            response_text = response.decode()

            assert "specific-key" in response_text
            assert "generic-key" not in response_text

            writer.close()
            await writer.wait_closed()
        finally:
            await proxy.stop()


@pytest.mark.skipif(
    os.environ.get("JIUWENBOX_TEST_LLM_ENDPOINT") is None,
    reason="LLM endpoint not configured (set JIUWENBOX_TEST_LLM_ENDPOINT)"
)
class TestRealLLMIntegration:
    """Test proxy with real LLM endpoints (skipped if not configured)."""

    @pytest.mark.asyncio
    @staticmethod
    async def test_llm_chat_completion(
        llm_test_settings,
        llm_proxy_runtime,
    ):
        """Test chat completion through proxy via socket."""
        if not llm_test_settings["available"]:
            pytest.skip("LLM endpoint not reachable (health check failed)")

        proxy_host = llm_proxy_runtime["proxy_host"]
        proxy_port = llm_proxy_runtime["proxy_port"]
        api_client = llm_proxy_runtime["api_client"]
        test_route_cleanup = llm_proxy_runtime["route_cleanup"]

        await api_client.post(
            "/api/v1/proxies",
            json={
                "path_prefix": "/llm-test",
                "target_endpoint": llm_test_settings["endpoint"],
                "api_key": llm_test_settings["api_key"],
            }
        )
        test_route_cleanup.append("llm-test")
        await api_client.post("/api/v1/proxies/llm-test/start")
        
        request_body = {
            "model": llm_test_settings["model"],
            "messages": [{"role": "user", "content": "Say 'proxy test ok'"}],
            "max_tokens": 20,
        }
        body_json = json.dumps(request_body)
        
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(30.0)
        sock.connect((proxy_host, proxy_port))
        
        request = (
            f"POST /llm-test/chat/completions HTTP/1.1\r\n"
            f"Host: {proxy_host}:{proxy_port}\r\n"
            f"Authorization: Bearer {PLACEHOLDER}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(body_json)}\r\n"
            f"\r\n"
            f"{body_json}"
        )
        sock.send(request.encode())
        
        response = b""
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            response += chunk
            if b"\r\n\r\n" in response and len(response) > 200:
                break
        
        sock.close()
        response_text = response.decode()
        
        assert "200 OK" in response_text or "201 Created" in response_text, \
            f"Expected success, got: {response_text[:500]}"
        
        logger.info("[test_llm_chat_completion] LLM chat completion successful")

    @pytest.mark.asyncio
    @staticmethod
    async def test_llm_api_key_injection_anthropic(
        llm_test_settings,
        llm_proxy_runtime,
    ):
        """Test Anthropic-style X-Api-Key injection through proxy."""
        if not llm_test_settings["available"]:
            pytest.skip("LLM endpoint not reachable (health check failed)")

        proxy_host = llm_proxy_runtime["proxy_host"]
        proxy_port = llm_proxy_runtime["proxy_port"]
        api_client = llm_proxy_runtime["api_client"]
        test_route_cleanup = llm_proxy_runtime["route_cleanup"]

        await api_client.post(
            "/api/v1/proxies",
            json={
                "path_prefix": "/anthropic-test",
                "target_endpoint": llm_test_settings["endpoint"],
                "api_key": llm_test_settings["api_key"],
            }
        )
        test_route_cleanup.append("anthropic-test")
        await api_client.post("/api/v1/proxies/anthropic-test/start")
        
        request_body = {
            "model": llm_test_settings["model"],
            "max_tokens": 10,
            "messages": [{"role": "user", "content": "test"}],
        }
        body_json = json.dumps(request_body)
        
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(30.0)
        sock.connect((proxy_host, proxy_port))
        
        request = (
            f"POST /anthropic-test/messages HTTP/1.1\r\n"
            f"Host: {proxy_host}:{proxy_port}\r\n"
            f"X-Api-Key: {PLACEHOLDER}\r\n"
            f"anthropic-version: 2023-06-01\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(body_json)}\r\n"
            f"\r\n"
            f"{body_json}"
        )
        sock.send(request.encode())
        
        response = b""
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            response += chunk
            if b"\r\n\r\n" in response and len(response) > 200:
                break
        
        sock.close()
        response_text = response.decode()
        
        assert "200 OK" in response_text or "201 Created" in response_text, \
            f"Expected success, got: {response_text[:500]}"
        
        logger.info(
            "[test_llm_api_key_injection_anthropic] Anthropic X-Api-Key injection works"
        )
