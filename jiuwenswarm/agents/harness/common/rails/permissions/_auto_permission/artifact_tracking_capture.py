"""Generic after-tool capture for Auto Permission."""

from __future__ import annotations

from typing import Any

from jiuwenswarm.agents.harness.common.rails.permissions._auto_permission.invocation_context import (
    _extract_invocation,
)
from jiuwenswarm.server.runtime.sandbox_no_host_fallback import (
    clear_no_host_fallback,
)


class AutoPermissionArtifactCaptureMixin:
    """Record bounded runtime context without inferring generated artifacts."""

    async def after_tool_call(self, *args: Any, **kwargs: Any) -> Any:
        """Capture one bounded result before delegating to the base rail."""

        clear_no_host_fallback()
        invocation = _extract_invocation(args, kwargs)
        return await self._call_base_after_rail(args, kwargs, invocation)
