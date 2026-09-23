# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""FlashBashTool: stock BashTool with Windows PATH hardening + not-found hint.

Used by ``SlimSysOperationRail`` and ``FlashBashSysOperationRail`` in place of
the stock ``BashTool``. On Windows, if the caller passes no explicit
``environment``/``env``, the subprocess PATH is hardened with common tool dirs
(bundled python, officecli) so ``python`` / ``officecli`` resolve even when the
agent process PATH is incomplete; a ``[Diagnostic]`` hint is appended when the
command was not found. All other stock semantics are preserved unchanged.
"""

from __future__ import annotations

import os
from typing import Any, AsyncIterator, Dict

from openjiuwen.harness.rails.sys_operation_rail import SysOperationRail
from openjiuwen.harness.tools.base_tool import ToolOutput
from openjiuwen.harness.tools.shell.bash._tool import BashTool

from jiuwenswarm.agents.harness.common.tools.command_tools import (
    _build_subprocess_env,
    _windows_not_found_hint,
)


class FlashBashTool(BashTool):
    """Stock BashTool plus Windows PATH hardening and command-not-found hints."""

    @staticmethod
    def _hardened_inputs(inputs: Dict[str, Any]) -> Dict[str, Any]:
        """Return a copy of *inputs* with hardened env on Windows.

        Only injects when the caller did not provide an explicit environment,
        so skill-injected env vars (e.g. a deliberately full PATH) still win.
        """
        if os.name != "nt":
            return inputs
        if inputs.get("environment") or inputs.get("env"):
            return inputs
        merged = _build_subprocess_env(None)
        if not merged:
            return inputs
        new_inputs = dict(inputs)
        new_inputs["environment"] = merged
        return new_inputs

    @staticmethod
    def _annotate_not_found(inputs: Dict[str, Any], output: ToolOutput) -> ToolOutput:
        """Append a not-found diagnostic hint to a failed rendered output."""
        if output is None or output.success:
            return output
        data = output.data
        if not data or not isinstance(data, dict):
            return output
        content = data.get("content")
        if not isinstance(content, str):
            return output
        command = str((inputs or {}).get("command") or "")
        hint = _windows_not_found_hint(command, content, 1)
        if not hint:
            return output
        new_content = content + hint
        new_data = dict(data)
        new_data["content"] = new_content
        return ToolOutput(
            success=output.success,
            data=new_data,
            error=new_content if output.error else output.error,
        )

    @staticmethod
    def _annotate_final_chunk(
        inputs: Dict[str, Any], chunk: ToolOutput
    ) -> ToolOutput:
        """Inject the not-found hint into a stream-path final ``content`` chunk.

        The stock ``BashTool.stream`` final chunk carries the fully rendered
        tool content in ``data["content"]``; we mirror ``_annotate_not_found``
        so the hint also lands in stream callers (FlashReadFileTool already
        proves the stream path is reachable).
        """
        return FlashBashTool._annotate_not_found(inputs, chunk)

    async def invoke(self, inputs: Dict[str, Any], **kwargs: Any) -> ToolOutput:
        hardened = self._hardened_inputs(inputs)
        result = await super().invoke(hardened, **kwargs)
        return self._annotate_not_found(hardened, result)

    async def stream(
        self, inputs: Dict[str, Any], **kwargs: Any
    ) -> AsyncIterator[ToolOutput]:
        """Stream variant that also hardens PATH and annotates the final chunk."""
        hardened = self._hardened_inputs(inputs)
        final_chunk = None
        async for chunk in super().stream(hardened, **kwargs):
            if self._is_final_rendered_content_chunk(chunk):
                final_chunk = chunk
                continue
            yield chunk
        if final_chunk is not None:
            yield self._annotate_final_chunk(hardened, final_chunk)

    @staticmethod
    def _is_final_rendered_content_chunk(chunk: ToolOutput) -> bool:
        """Return True for the fully-rendered terminal content chunk.

        The stock ``BashTool.stream`` final chunk carries the rendered tool
        content in ``data["content"]``; intermediate chunks carry partial text
        in ``data["text"]``. We collect the final chunk (no ``text`` key,
        ``content`` is a string) and annotate it once streaming completes.
        """
        if chunk is None:
            return False
        data = chunk.data
        if not isinstance(data, dict):
            return False
        content = data.get("content")
        return isinstance(content, str) and not data.get("text")


class FlashBashSysOperationRail(SysOperationRail):
    """Stock SysOperationRail with bash swapped to FlashBashTool.

    The init body delegates to :meth:`SysOperationRail.init` and only swaps
    the bash tool afterwards. This avoids copying the parent's ~50 lines of
    tool assembly so upstream additions to ``SysOperationRail.init`` propagate
    to this rail automatically; a guard test (``test_rails_alignment``)
    asserts the toolset shape stays in sync so silently drifting assemblies
    surface as test failures rather than runtime divergence.
    """

    def init(self, agent) -> None:
        super().init(agent)
        if self.tools is None:
            return
        from openjiuwen.harness.tools.filesystem import ReadFileTool
        from jiuwenswarm.agents.harness.flash.tools.flash_read_tool import (
            FlashReadFileTool,
        )

        lang = agent.system_prompt_builder.language
        agent_id = getattr(getattr(agent, "card", None), "id", None)
        swap_targets: list[tuple[int, Any]] = []
        for idx, tool in enumerate(self.tools):
            if isinstance(tool, BashTool) and not isinstance(tool, FlashBashTool):
                swap_targets.append(
                    (
                        idx,
                        FlashBashTool(
                            self.sys_operation,
                            lang,
                            agent_id=agent_id,
                            deny_patterns=self._bash_deny_patterns,
                        ),
                    )
                )
            elif isinstance(tool, ReadFileTool) and not isinstance(
                tool, FlashReadFileTool
            ):
                swap_targets.append(
                    (idx, FlashReadFileTool(self.sys_operation, lang, agent_id))
                )
        for idx, replacement in swap_targets:
            self.tools[idx] = replacement
            agent.ability_manager.add_ability(replacement.card, replacement)


__all__ = ["FlashBashTool", "FlashBashSysOperationRail"]
