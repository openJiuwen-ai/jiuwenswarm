"""Shared test fixtures."""

from __future__ import annotations

import os

import pytest

from jiuwenbox.models.policy import SecurityPolicy


def pytest_addoption(parser):
    parser.addoption(
        "--server-endpoint",
        action="store",
        default=None,
        help="Run server API tests against an external server endpoint. Defaults to 127.0.0.1:8321.",
    )


@pytest.fixture
def server_endpoint(pytestconfig) -> str:
    return (
        pytestconfig.getoption("server_endpoint")
        or os.environ.get("JIUWENBOX_TEST_SERVER")
        or "127.0.0.1:8321"
    )


@pytest.fixture
def default_policy() -> SecurityPolicy:
    """Return the default policy."""
    return SecurityPolicy()
