# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""flash 单测共享离线 fake（builder / agent / ctx / ability_manager）。

各 rail 测试原先各自定义一份近乎相同的 ``_FakeBuilder``/``_FakeAgent``/``_FakeCtx``，
这里统一收口，避免重复。
"""

from types import SimpleNamespace


class FakeBuilder:
    """离线 SystemPromptBuilder 替身：add_section 按 name 去重（dict 存储）。

    ``get_all_sections`` 返回 ``dict[name, section]``，对齐真实
    SystemPromptBuilder.get_all_sections() 的返回形态。
    """

    def __init__(self, language: str = "cn") -> None:
        self.language = language
        self.sections: dict[str, SimpleNamespace] = {}

    def add_section(self, section) -> None:
        self.sections[section.name] = section

    def remove_section(self, name) -> None:
        self.sections.pop(name, None)

    def get_section(self, name):
        return self.sections.get(name)

    def get_all_sections(self):
        return dict(self.sections)


class FakeAgent:
    def __init__(self, language: str = "cn", sections=None, ability_manager=None) -> None:
        self.system_prompt_builder = FakeBuilder(language)
        self.ability_manager = ability_manager
        for s in sections or []:
            self.system_prompt_builder.add_section(s)


class FakeCtx:
    def __init__(self, agent) -> None:
        self.agent = agent


class FakeAbilityManager:
    def __init__(
        self,
        names=(
            "task_tool", "memory", "todo", "skill_tool",
            "read_file", "write_file", "edit_file", "glob",
            "grep", "bash", "tools_search", "invoke_tool",
        ),
    ) -> None:
        self.cards = {n: object() for n in names}
