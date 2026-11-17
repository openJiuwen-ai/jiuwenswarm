# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""FlashTodoRail 430 todo 纪律段注入的 disabled_tools 门禁。

FlashTodoRail priority=90 在 DisabledToolsRail（priority=1）之前运行，故不能读运行中
工具面来判断 todo 是否被禁用，必须直接比对 ``react.disabled_tools`` 配置。本测试验证：
- disabled_tools 含 "todo" 时不注入 430 段；
- 不含时注入；
- todo 工具未注册（``_tools`` 为空）时同样不注入（既有防御）。
"""

from __future__ import annotations

from jiuwenswarm.agents.harness.flash.rails.flash_todo_rail import FlashTodoRail
from jiuwenswarm.agents.harness.flash.tools.flash_todo import _UNIFIED_TODO_NAME


class _FakeBuilder:
    """离线 builder：只记录 add_section 的 (name, priority)，不依赖 PromptSection。"""

    def __init__(self, language: str = "cn") -> None:
        self.language = language
        self.added: list[tuple[str, int]] = []
        self.contents: list = []

    def add_section(self, section) -> None:
        self.added.append(
            (str(getattr(section, "name", "")), int(getattr(section, "priority", 0) or 0))
        )
        self.contents.append(getattr(section, "content", None))

    def remove_section(self, name) -> None:
        self.added = [(n, p) for n, p in self.added if n != name]

    def get_all_sections(self) -> list:
        return [type("S", (), {"name": n, "priority": p})() for n, p in self.added]


def _make_rail(disabled_tools=None, *, tools_registered: bool = True) -> FlashTodoRail:
    rail = FlashTodoRail(disabled_tools=disabled_tools)
    rail.system_prompt_builder = _FakeBuilder()
    # init() 才真正注册统一 todo 工具；这里模拟 init 成功后的状态。
    rail._tools = [object()] if tools_registered else []
    return rail


def test_todo_section_injected_when_not_disabled() -> None:
    rail = _make_rail(disabled_tools=["read_file"])
    rail._inject_todo_section()
    assert [name for name, _priority in rail.system_prompt_builder.added] == [
        _UNIFIED_TODO_NAME
    ]


def test_todo_section_skipped_when_disabled() -> None:
    rail = _make_rail(disabled_tools=[_UNIFIED_TODO_NAME])
    rail._inject_todo_section()
    assert rail.system_prompt_builder.added == []


def test_todo_section_skipped_when_disabled_as_set() -> None:
    rail = _make_rail(disabled_tools={_UNIFIED_TODO_NAME})
    rail._inject_todo_section()
    assert rail.system_prompt_builder.added == []


def test_todo_section_skipped_without_registered_tool() -> None:
    rail = _make_rail(disabled_tools=None, tools_registered=False)
    rail._inject_todo_section()
    assert rail.system_prompt_builder.added == []


def test_todo_language_zh_normalizes_to_cn() -> None:
    """zh 归一为 cn：正文与模型选择说明都用 CN，不混入 EN 正文。"""
    rail = _make_rail(disabled_tools=None)
    rail.system_prompt_builder = _FakeBuilder(language="zh")
    rail._inject_todo_section()
    content = rail.system_prompt_builder.contents[0]
    assert list(content.keys()) == ["cn"]
    assert "纪律" in content["cn"]
    assert "Discipline" not in content["cn"]


def test_disabled_tools_stored_as_set() -> None:
    rail = FlashTodoRail(disabled_tools=["a", "b"])
    assert rail._disabled_tools == {"a", "b"}
    assert FlashTodoRail()._disabled_tools == set()


def test_update_disabled_tools_resyncs_snapshot() -> None:
    """热更新：disabled_tools 从无 todo 翻成含 todo → 430 停止注入。"""
    rail = _make_rail(disabled_tools=None)
    assert _UNIFIED_TODO_NAME not in rail._disabled_tools

    rail.update_disabled_tools([_UNIFIED_TODO_NAME])
    assert rail._disabled_tools == {_UNIFIED_TODO_NAME}
    rail._inject_todo_section()
    assert rail.system_prompt_builder.added == []


def test_hot_disable_removes_previously_injected_section() -> None:
    """热禁用撤段：430 已注入后 todo 被禁用，再 inject 应撤下残留 430。

    430 由本 rail 每轮 add（upsert），不会被其它 rail 清除；若禁用分支只 return
    不撤段，热禁用后模型仍会看到「要求调 todo」但工具面已无 todo。
    """
    rail = _make_rail(disabled_tools=None)
    rail._inject_todo_section()
    assert [name for name, _priority in rail.system_prompt_builder.added] == [
        _UNIFIED_TODO_NAME
    ]

    rail.update_disabled_tools([_UNIFIED_TODO_NAME])
    rail._inject_todo_section()
    assert rail.system_prompt_builder.added == []


def test_update_disabled_tools_clears_when_empty() -> None:
    """热更新：todo 解除禁用 → 430 恢复注入。"""
    rail = _make_rail(disabled_tools=[_UNIFIED_TODO_NAME])
    assert _UNIFIED_TODO_NAME in rail._disabled_tools
    rail._inject_todo_section()
    assert rail.system_prompt_builder.added == []

    rail.update_disabled_tools([])
    assert rail._disabled_tools == set()
    rail._inject_todo_section()
    assert [name for name, _priority in rail.system_prompt_builder.added] == [
        _UNIFIED_TODO_NAME
    ]
