# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Team filesystem rail: flash read/edit, stock shell and listing tools.

Keep this ``init`` aligned with
``openjiuwen.harness.rails.sys_operation_rail.SysOperationRail.init``.
Only ``read_file`` and ``edit_file`` are swapped. Tool names stay the same,
so progressive loading, permissions, and invoke_tool unwrap keep working.
Bash ``deny_patterns`` and the read-image resolver are copied from the parent
rail; they are not extra behavior.

``FlashEditFileTool`` applies hunks in every operation mode. Its content
cache is local-only; sandbox, Office, and UNC paths fall back to a full read
inside the tool. This rail still mounts in those modes.

Priority is one step below stock ``SysOperationRail`` (100). Rails init
highest-first; if a leftover stock filesystem rail is still mounted, Accel
inits afterward and ``add_ability`` keeps FlashEdit.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from openjiuwen.harness.rails.sys_operation_rail import SysOperationRail
from openjiuwen.harness.tools import BashTool, PowerShellTool
from openjiuwen.harness.tools.code import CodeTool
from openjiuwen.harness.tools.filesystem import (
    GlobTool,
    GrepTool,
    ListDirTool,
    WriteFileTool,
)

from jiuwenswarm.agents.harness.flash.tools.flash_edit_tool import FlashEditFileTool
from jiuwenswarm.agents.harness.flash.tools.flash_read_tool import FlashReadFileTool

logger = logging.getLogger(__name__)


class AccelSysOperationRail(SysOperationRail):
    """SysOperationRail with flash read/edit and the rest of the stock toolset."""

    # Stock SysOperationRail / FlashBashSysOperationRail use 100. Init later so
    # a dual-mounted stock rail cannot refresh stock EditFile over FlashEdit.
    priority = 99

    def init(self, agent) -> None:
        from openjiuwen.harness.rails._multimodal import (
            should_enable_read_image_multimodal,
        )

        lang = agent.system_prompt_builder.language
        agent_id = getattr(getattr(agent, "card", None), "id", None)
        enable_read_image_multimodal = should_enable_read_image_multimodal(
            agent,
            self._enable_read_image_multimodal,
        )
        read_tool = FlashReadFileTool(
            self.sys_operation,
            lang,
            agent_id,
            enable_image_multimodal=enable_read_image_multimodal,
            image_multimodal_resolver=lambda: should_enable_read_image_multimodal(
                agent,
                self._enable_read_image_multimodal,
            ),
        )
        write_tool = WriteFileTool(self.sys_operation, lang, agent_id)
        edit_tool = FlashEditFileTool(self.sys_operation, lang, agent_id)
        glob_tool = GlobTool(self.sys_operation, lang, agent_id)
        list_dir_tool = ListDirTool(self.sys_operation, lang, agent_id)
        grep_tool = GrepTool(self.sys_operation, lang, agent_id)
        bash_tool = BashTool(
            self.sys_operation,
            lang,
            agent_id=agent_id,
            deny_patterns=self._bash_deny_patterns,
        )
        powershell_tool = (
            PowerShellTool(self.sys_operation, lang, agent_id=agent_id)
            if os.name == "nt"
            else None
        )

        shared: list[Any] = [glob_tool, list_dir_tool, grep_tool, bash_tool]
        if self._read_only:
            self.tools = [read_tool, *shared]
        else:
            self.tools = [read_tool, write_tool, edit_tool, *shared]
        if powershell_tool is not None:
            self.tools.append(powershell_tool)

        if self._with_code_tool and not self._read_only:
            self.tools.append(CodeTool(self.sys_operation, lang, agent_id))

        # Same rebind as SysOperationRail: stateful add_ability refreshes a
        # leftover tool that still points at a previous SysOperation.
        for tool in self.tools:
            agent.ability_manager.add_ability(tool.card, tool)

        logger.info(
            "[AccelSysOperationRail] bound flash IO agent_id=%s "
            "edit_file=%s read_file=%s bash=%s deny_patterns=%d",
            agent_id,
            type(edit_tool).__name__,
            type(read_tool).__name__,
            type(bash_tool).__name__,
            len(self._bash_deny_patterns or []),
        )


__all__ = ["AccelSysOperationRail"]
