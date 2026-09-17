# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""Shared test fixtures."""

from __future__ import annotations

import os

import pytest

pytest_plugins = ["pytest_asyncio"]


def pytest_addoption(parser):
    parser.addoption(
        "--server-endpoint",
        action="store",
        default=None,
        help=(
            "Server endpoint. Accepts 'host:port', 'http://host:port', or "
            "'unix:///abs/socket/path'. Default: 127.0.0.1:8321"
        ),
    )
    parser.addoption(
        "--proxy-port",
        action="store",
        default=None,
        type=int,
        help="Proxy listen port. Default: 8322",
    )
    parser.addoption(
        "--test-llm-endpoint",
        action="store",
        default=None,
        help="LLM endpoint URL for testing (e.g., https://api.openai.com/v1)",
    )
    parser.addoption(
        "--test-llm-api-key",
        action="store",
        default=None,
        help="LLM API key for testing",
    )
    parser.addoption(
        "--test-llm-model",
        action="store",
        default=None,
        help="LLM model name. Default: gpt-4o-mini",
    )


def _is_uds_endpoint(endpoint: str) -> bool:
    """Whether ``endpoint`` 指向 Unix Domain Socket (scheme ``unix://``)."""
    return endpoint.startswith("unix://")


def _resolve_raw_endpoint(pytestconfig) -> str:
    return (
        pytestconfig.getoption("server_endpoint")
        or os.environ.get("JIUWENBOX_TEST_SERVER")
        or "127.0.0.1:8321"
    )


def _normalize_endpoint_url(endpoint: str) -> str:
    """Turn ``host:port`` / ``http://host:port`` / ``unix:///path`` 一律转成完整 URL."""
    if "://" in endpoint:
        return endpoint
    return f"http://{endpoint}"


@pytest.fixture
def server_endpoint(pytestconfig) -> str:
    """Server endpoint as host:port, http(s)://... or unix:///abs/path string."""
    return _resolve_raw_endpoint(pytestconfig)


@pytest.fixture
def proxy_port(pytestconfig) -> int:
    """Proxy listen port."""
    return (
        pytestconfig.getoption("proxy_port")
        or int(os.environ.get("JIUWENBOX_PROXY_PORT", "8322"))
    )


@pytest.fixture
def server_host_port(server_endpoint):
    """Parse server_endpoint into (host, port) tuple.

    UDS 端点没有 host/port 概念 (socket 文件本身就是 listener); 需要它的
    fixture (proxy / docker-gateway 探测等) 在 UDS 模式下统一 ``skip``,
    避免在源头解析阶段抛 ``ValueError``。
    """
    if _is_uds_endpoint(server_endpoint):
        pytest.skip(
            "fixture requires TCP endpoint; server is configured for UDS "
            f"({server_endpoint!r})",
        )
    endpoint = server_endpoint
    if "://" in endpoint:
        endpoint = endpoint.split("://", 1)[1]
    host, port = endpoint.rsplit(":", 1)
    return host, int(port)


@pytest.fixture
def server_url(server_endpoint):
    """Server endpoint as full URL.

    TCP 端点补 ``http://`` 前缀; UDS 端点 (``unix:///path``) 直接透传——
    下游 httpx fixture 与 jiuwenbox CLI 的 ``--base-url`` 都识别这种 scheme。
    """
    return _normalize_endpoint_url(server_endpoint)


@pytest.fixture(scope="session")
def server_url_session(pytestconfig):
    """Session-scoped server URL (与 :func:`server_url` 同语义)."""
    return _normalize_endpoint_url(_resolve_raw_endpoint(pytestconfig))


@pytest.fixture(scope="session")
def server_host_port_session(pytestconfig):
    """Session-scoped parsed host and port (UDS 模式 skip)."""
    endpoint = _resolve_raw_endpoint(pytestconfig)
    if _is_uds_endpoint(endpoint):
        pytest.skip(
            "fixture requires TCP endpoint; server is configured for UDS "
            f"({endpoint!r})",
        )
    if "://" in endpoint:
        endpoint = endpoint.split("://", 1)[1]
    host, port = endpoint.rsplit(":", 1)
    return host, int(port)


@pytest.fixture(scope="session")
def test_llm_endpoint(pytestconfig):
    """LLM endpoint URL for testing."""
    return (
        pytestconfig.getoption("test_llm_endpoint")
        or os.environ.get("JIUWENBOX_TEST_LLM_ENDPOINT")
    )


@pytest.fixture(scope="session")
def test_llm_api_key(pytestconfig):
    """LLM API key for testing."""
    return (
        pytestconfig.getoption("test_llm_api_key")
        or os.environ.get("JIUWENBOX_TEST_LLM_API_KEY")
    )


@pytest.fixture(scope="session")
def test_llm_model(pytestconfig):
    """LLM model name for testing."""
    return (
        pytestconfig.getoption("test_llm_model")
        or os.environ.get("JIUWENBOX_TEST_LLM_MODEL")
        or "gpt-4o-mini"
    )


@pytest.fixture(scope="session")
def llm_available(docker_gateway_ip):
    """Extract LLM availability from topology check result."""
    return docker_gateway_ip.get("llm_available", False)
