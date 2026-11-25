"""Generic after-tool capture for Auto Permission."""

from __future__ import annotations

from typing import Any

from jiuwenswarm.agents.harness.common.rails.permissions._auto_permission.invocation_context import (
    _extract_invocation,
    _host_session_id,
    _host_tool_call_id,
    _input_value,
    normalize_invocation_tool_args,
)
from jiuwenswarm.agents.harness.common.rails.permissions.root_permission_queue_rail import (
    tool_invocation_key_from_context,
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
        key = tool_invocation_key_from_context(invocation.ctx)
        if self.session_artifact_paths is not None and key is not None:
            try:
                if (
                    _host_tool_call_id(invocation.tool_call) != key.tool_call_id
                    or _host_session_id(invocation.ctx) != key.execution_session_id
                ):
                    self.session_artifact_paths.clear_pending(key)
                else:
                    self.session_artifact_paths.complete(
                        key, tool_name=invocation.tool_name,
                        tool_args=normalize_invocation_tool_args(invocation.tool_name, invocation.tool_args),
                        result=_input_value(getattr(invocation.ctx, "inputs", None), "tool_result"),
                        exception=getattr(invocation.ctx, "exception", None),
                    )
            except Exception:
                # Evidence collection must never alter the tool's result.
                self.session_artifact_paths.clear_pending(key)
        return await self._call_base_after_rail(args, kwargs, invocation)
