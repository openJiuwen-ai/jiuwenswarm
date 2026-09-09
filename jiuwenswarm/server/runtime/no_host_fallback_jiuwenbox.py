# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""JiuwenBox providers that honor one task-local no-host-fallback scope."""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from functools import wraps
from typing import Any, ParamSpec, TypeVar

from openjiuwen.core.sys_operation.sandbox.sandbox_registry import SandboxRegistry
from openjiuwen.extensions.sys_operation.sandbox.providers.jiuwenbox import (
    JiuwenBoxCodeProvider,
    JiuwenBoxFSProvider,
    JiuwenBoxShellProvider,
)

from jiuwenswarm.server.runtime.sandbox_no_host_fallback import (
    clear_no_host_fallback,
    no_host_fallback_required,
)

_P = ParamSpec("_P")
_T = TypeVar("_T")


def _clear_after(
    method: Callable[_P, Awaitable[_T]],
) -> Callable[_P, Awaitable[_T]]:
    @wraps(method)
    async def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _T:
        try:
            return await method(*args, **kwargs)
        finally:
            clear_no_host_fallback()

    return wrapper


async def _close_iterator(
    iterator: AsyncIterator[Any], primary_error: BaseException | None
) -> None:
    close = getattr(iterator, "aclose", None)
    if not callable(close):
        return
    try:
        await close()
    except BaseException:
        if primary_error is None:
            raise


def _clear_stream_scope(
    method: Callable[_P, AsyncIterator[_T]],
) -> Callable[_P, AsyncIterator[_T]]:
    @wraps(method)
    async def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> AsyncIterator[_T]:
        iterator: AsyncIterator[_T] | None = None
        try:
            iterator = method(*args, **kwargs)
            try:
                first = await anext(iterator)
            except StopAsyncIteration:
                return
            finally:
                clear_no_host_fallback()
            yield first
            async for item in iterator:
                yield item
        finally:
            clear_no_host_fallback()
            if iterator is not None:
                await _close_iterator(iterator, sys.exc_info()[1])

    return wrapper


class _NoHostFallbackMixin:
    def _launcher_extra_params(self, *, create: bool = False) -> dict[str, Any]:
        # This is a cooperative MRO override; the instance selects the concrete
        # JiuwenBox provider implementation that owns the base launcher params.
        params = super(_NoHostFallbackMixin, self)._launcher_extra_params(
            create=create
        )
        if not no_host_fallback_required():
            return params
        return {
            **params,
            "excluded_commands": [],
            "fallback_on_failure": False,
        }


class NoHostFallbackJiuwenBoxFSProvider(_NoHostFallbackMixin, JiuwenBoxFSProvider):
    read_file = _clear_after(JiuwenBoxFSProvider.read_file)
    write_file = _clear_after(JiuwenBoxFSProvider.write_file)
    list_files = _clear_after(JiuwenBoxFSProvider.list_files)
    list_directories = _clear_after(JiuwenBoxFSProvider.list_directories)
    upload_file = _clear_after(JiuwenBoxFSProvider.upload_file)
    download_file = _clear_after(JiuwenBoxFSProvider.download_file)
    search_files = _clear_after(JiuwenBoxFSProvider.search_files)
    read_file_stream = _clear_stream_scope(JiuwenBoxFSProvider.read_file_stream)
    upload_file_stream = _clear_stream_scope(JiuwenBoxFSProvider.upload_file_stream)
    download_file_stream = _clear_stream_scope(
        JiuwenBoxFSProvider.download_file_stream
    )


class NoHostFallbackJiuwenBoxShellProvider(
    _NoHostFallbackMixin, JiuwenBoxShellProvider
):
    execute_cmd = _clear_after(JiuwenBoxShellProvider.execute_cmd)
    execute_cmd_stream = _clear_stream_scope(JiuwenBoxShellProvider.execute_cmd_stream)


class NoHostFallbackJiuwenBoxCodeProvider(
    _NoHostFallbackMixin, JiuwenBoxCodeProvider
):
    execute_code = _clear_after(JiuwenBoxCodeProvider.execute_code)
    execute_code_stream = _clear_stream_scope(JiuwenBoxCodeProvider.execute_code_stream)


def install_no_host_fallback_jiuwenbox_providers() -> None:
    """Install JiuwenBox providers that honor the execution-owned scope."""

    SandboxRegistry.register_provider(
        "jiuwenbox", "fs", NoHostFallbackJiuwenBoxFSProvider
    )
    SandboxRegistry.register_provider(
        "jiuwenbox", "shell", NoHostFallbackJiuwenBoxShellProvider
    )
    SandboxRegistry.register_provider(
        "jiuwenbox", "code", NoHostFallbackJiuwenBoxCodeProvider
    )


__all__ = ["install_no_host_fallback_jiuwenbox_providers"]
