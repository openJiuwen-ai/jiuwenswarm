"""Regression tests for gateway-injected sandbox command parameters."""

import pytest

from jiuwenswarm.server.handlers.sandbox import _reject_extra_sandbox_files_params


def test_sandbox_files_accepts_gateway_agent_type() -> None:
    _reject_extra_sandbox_files_params(
        {
            "sub": "files.allow",
            "path": "/workspace",
            "mode": "agent",
            "agent_type": "opencode",
        }
    )


def test_sandbox_files_still_rejects_unknown_params() -> None:
    with pytest.raises(ValueError, match="unexpected parameter.*unknown"):
        _reject_extra_sandbox_files_params(
            {"sub": "files.allow", "path": "/workspace", "unknown": True}
        )
