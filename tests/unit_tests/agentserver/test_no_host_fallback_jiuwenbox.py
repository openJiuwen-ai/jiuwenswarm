# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import pytest

from jiuwenswarm.server.runtime.no_host_fallback_jiuwenbox import (
    _NoHostFallbackMixin,
    _clear_after,
    _clear_stream_scope,
)
from jiuwenswarm.server.runtime.sandbox_no_host_fallback import (
    clear_no_host_fallback,
    no_host_fallback_required,
    require_no_host_fallback,
)


class _BaseProvider:
    def _launcher_extra_params(self, *, create: bool = False):
        return {
            "create": create,
            "excluded_commands": ["bash"],
            "fallback_on_failure": True,
            "unrelated": "kept",
        }


class _Provider(_NoHostFallbackMixin, _BaseProvider):
    pass


@pytest.fixture(autouse=True)
def _clean_scope():
    clear_no_host_fallback()
    yield
    clear_no_host_fallback()


def test_scope_forces_no_host_fallback_without_instance_binding() -> None:
    provider = _Provider()
    assert provider._launcher_extra_params(create=True)["fallback_on_failure"] is True

    require_no_host_fallback()
    params = provider._launcher_extra_params(create=True)

    assert params == {
        "create": True,
        "excluded_commands": [],
        "fallback_on_failure": False,
        "unrelated": "kept",
    }
    assert no_host_fallback_required() is True


@pytest.mark.asyncio
async def test_terminal_operation_clears_scope_on_success_and_failure() -> None:
    async def success() -> str:
        return "ok"

    async def failure() -> None:
        raise RuntimeError("sandbox unavailable")

    require_no_host_fallback()
    assert await _clear_after(success)() == "ok"
    assert no_host_fallback_required() is False

    require_no_host_fallback()
    with pytest.raises(RuntimeError, match="sandbox unavailable"):
        await _clear_after(failure)()
    assert no_host_fallback_required() is False


@pytest.mark.asyncio
async def test_stream_clears_scope_before_first_result_is_consumed() -> None:
    async def stream():
        yield "first"
        yield "second"

    require_no_host_fallback()
    wrapped = _clear_stream_scope(stream)()

    assert await anext(wrapped) == "first"
    assert no_host_fallback_required() is False
    assert await anext(wrapped) == "second"
    await wrapped.aclose()
