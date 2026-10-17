# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Integration tests for the configurable ``/health`` response body.

Contract under test (``JIUWENBOX_HEALTH_RESPONSE_BODY``):

- unset            -> default JSON ``HealthResponse`` (unchanged behaviour);
- empty string     -> treated as unset;
- non-empty string -> HTTP 200, body emitted verbatim (no JSON quoting,
  no appended newline), ``Content-Type: text/plain``.

The health determination itself, the Bearer-token auth rules and every
other route (``/api/v1/*``, ``/mcp``) must stay unaffected.

These tests are self-contained: each scenario launches its own
``jiuwenbox-server`` subprocess on a free TCP port with a controlled
environment, so they do not depend on the shared session server (whose
env is fixed by the operator).
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time

import httpx
import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="jiuwenbox-server requires a POSIX runtime",
)

CUSTOM_BODY = "@the\\@health\\@is\\@good@"
TEST_TOKEN = "test-token-health-0918"

_SERVER_BOOT_TIMEOUT = 30.0


def _free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("localhost", 0))
        return sock.getsockname()[1]


class _ServerHandle:
    """A self-launched ``jiuwenbox-server`` subprocess on a free TCP port."""

    def __init__(self, env_extra: dict[str, str]) -> None:
        self.port = _free_tcp_port()
        self.url = f"http://localhost:{self.port}"
        self._log_path = os.path.join(
            tempfile.mkdtemp(prefix="jiuwenbox-health-test-"), "server.log"
        )
        env = dict(os.environ)
        env.update(env_extra)
        # 入口用 -c 而不是 console script, 不依赖安装后的 PATH。
        self.process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "from jiuwenbox.server.launcher import main; "
                "raise SystemExit(main())",
                "--listen",
                f"http://localhost:{self.port}",
            ],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=open(self._log_path, "wb"),  # noqa: SIM115 - 子进程生命周期
        )

    def wait_ready(self, timeout: float = _SERVER_BOOT_TIMEOUT) -> None:
        deadline = time.monotonic() + timeout
        last_status: int | None = None
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(
                    f"jiuwenbox-server exited early (code {self.process.returncode}); "
                    f"log: {self._log_path}"
                )
            try:
                resp = httpx.get(f"{self.url}/health", timeout=2.0)
                last_status = resp.status_code
                # 200 = 无鉴权场景就绪; 401 = Token 场景就绪 (中间件已生效)。
                if resp.status_code in (200, 401):
                    return
            except Exception:  # noqa: BLE001 - boot 期间连接被拒属正常
                pass
            time.sleep(0.3)
        raise RuntimeError(
            f"jiuwenbox-server not ready within {timeout}s "
            f"(last /health status: {last_status}); log: {self._log_path}"
        )

    def stop(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=10)


def _launch(**env_extra: str) -> _ServerHandle:
    handle = _ServerHandle(env_extra)
    try:
        handle.wait_ready()
    except Exception:
        handle.stop()
        raise
    return handle


def _json_health_fields(resp: httpx.Response) -> None:
    """Assert the default JSON health contract (fields + content type)."""
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("application/json")
    data = resp.json()
    assert data["status"] == "ok"
    assert data["version"]
    assert isinstance(data["landlock_supported"], bool)
    assert isinstance(data["sandboxes_active"], int)


@pytest.fixture(scope="module")
def default_server():
    handle = _launch()
    try:
        yield handle
    finally:
        handle.stop()


@pytest.fixture(scope="module")
def custom_body_server():
    handle = _launch(JIUWENBOX_HEALTH_RESPONSE_BODY=CUSTOM_BODY)
    try:
        yield handle
    finally:
        handle.stop()


@pytest.fixture(scope="module")
def empty_body_server():
    handle = _launch(JIUWENBOX_HEALTH_RESPONSE_BODY="")
    try:
        yield handle
    finally:
        handle.stop()


@pytest.fixture(scope="module")
def token_server():
    handle = _launch(
        JIUWENBOX_HEALTH_RESPONSE_BODY=CUSTOM_BODY,
        JIUWENBOX_API_TOKEN=TEST_TOKEN,
    )
    try:
        yield handle
    finally:
        handle.stop()


class TestHealthCustomBody:
    @staticmethod
    def test_custom_body_exact_match(custom_body_server):
        resp = httpx.get(f"{custom_body_server.url}/health")
        assert resp.status_code == 200, resp.text
        # 精确匹配: 无 JSON 双引号、无追加换行 (整体相等即覆盖)。
        assert resp.text == CUSTOM_BODY
        assert resp.content == CUSTOM_BODY.encode("utf-8")
        # Content-Type 必须是纯文本, 且不附加 charset。
        assert resp.headers["content-type"] == "text/plain"

    @staticmethod
    def test_empty_value_falls_back_to_json(empty_body_server):
        _json_health_fields(httpx.get(f"{empty_body_server.url}/health"))

    @staticmethod
    def test_default_json_regression(default_server):
        _json_health_fields(httpx.get(f"{default_server.url}/health"))

    @staticmethod
    def test_sandboxes_api_unaffected_by_custom_body(
        custom_body_server, default_server
    ):
        for server in (custom_body_server, default_server):
            resp = httpx.get(f"{server.url}/api/v1/sandboxes")
            assert resp.status_code == 200, resp.text
            assert resp.headers["content-type"].startswith("application/json")
            assert resp.json() == []

    @staticmethod
    def test_mcp_route_unaffected_by_custom_body(
        custom_body_server, default_server
    ):
        # 未携带 MCP 会话的裸请求: 只要两个 server 上行为一致且未被
        # 自定义响应吞掉 (非 404), 即证明 /mcp 路由仍按原样挂载。
        def probe(url: str) -> int:
            resp = httpx.post(f"{url}/mcp", timeout=10.0)
            return resp.status_code

        custom_status = probe(custom_body_server.url)
        assert custom_status != 404
        assert custom_status == probe(default_server.url)


class TestHealthCustomBodyWithToken:
    @staticmethod
    def test_missing_token_rejected(token_server):
        resp = httpx.get(f"{token_server.url}/health")
        assert resp.status_code == 401
        assert resp.headers["content-type"].startswith("application/json")

    @staticmethod
    def test_wrong_token_rejected(token_server):
        resp = httpx.get(
            f"{token_server.url}/health",
            headers={"Authorization": "Bearer wrong-token"},
        )
        assert resp.status_code == 401

    @staticmethod
    def test_valid_token_gets_custom_body(token_server):
        resp = httpx.get(
            f"{token_server.url}/health",
            headers={"Authorization": f"Bearer {TEST_TOKEN}"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.text == CUSTOM_BODY
        assert resp.headers["content-type"] == "text/plain"
