# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""SlimSysOperationRail — SysOperationRail minus the retired tools.

powershell is fully covered by bash (shell_type=powershell, identical
parameter surface); list_files is fully covered by glob. Overriding
``init`` keeps both out of ability_manager entirely — they never appear
in tools[], navigation, or the search corpus. Being a SysOperationRail
subclass, subagent rail inheritance still detects it and reuses this
slim rail instead of adding a stock one.

Keep this init in sync with
``openjiuwen.harness.rails.sys_operation_rail.SysOperationRail.init``
when upgrading agent-core.
"""

from __future__ import annotations

from openjiuwen.harness.rails.sys_operation_rail import SysOperationRail


class SlimSysOperationRail(SysOperationRail):
    """SysOperationRail minus powershell / list_files."""

    def init(self, agent) -> None:
        from openjiuwen.harness.rails._multimodal import (
            should_enable_read_image_multimodal,
        )
        from openjiuwen.harness.tools import BashTool
        from openjiuwen.harness.tools.code import CodeTool
        from openjiuwen.harness.tools.filesystem import (
            EditFileTool,
            GlobTool,
            GrepTool,
            ReadFileTool,
            WriteFileTool,
        )

        lang = agent.system_prompt_builder.language
        agent_id = getattr(getattr(agent, "card", None), "id", None)
        enable_read_image_multimodal = should_enable_read_image_multimodal(
            agent,
            self._enable_read_image_multimodal,
        )
        read_tool = ReadFileTool(
            self.sys_operation,
            lang,
            agent_id,
            enable_image_multimodal=enable_read_image_multimodal,
        )
        write_tool = WriteFileTool(self.sys_operation, lang, agent_id)
        edit_tool = EditFileTool(self.sys_operation, lang, agent_id)
        glob_tool = GlobTool(self.sys_operation, lang, agent_id)
        grep_tool = GrepTool(self.sys_operation, lang, agent_id)
        bash_tool = BashTool(
            self.sys_operation,
            lang,
            agent_id=agent_id,
            deny_patterns=self._bash_deny_patterns,
        )

        if self._read_only:
            self.tools = [read_tool, glob_tool, grep_tool, bash_tool]
        else:
            self.tools = [
                read_tool,
                write_tool,
                edit_tool,
                glob_tool,
                grep_tool,
                bash_tool,
            ]

        if self._with_code_tool and not self._read_only:
            self.tools.append(CodeTool(self.sys_operation, lang, agent_id))

        # 工具 id 形如 "write_file_<agent_id>"，与 SysOperation 实例无关；若上一次
        # agent 生命周期里的同名工具仍残留在 resource_mgr，旧工具实例会持有过期的
        # SysOperation 引用，导致 SANDBOX 切换时 fs/shell 调用走 LOCAL 并写穿宿主。
        # add_ability 的 refresh=True 注册（已存在则先 remove 再 add）天然处理这种
        # 残留 rebind。
        for tool in self.tools:
            agent.ability_manager.add_ability(tool.card, tool)


__all__ = ["SlimSysOperationRail"]
