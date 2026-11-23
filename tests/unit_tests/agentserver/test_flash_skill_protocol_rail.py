# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""FlashSkillProtocolRail 单元测试。

覆盖（对齐 rail 的守卫改写语义）：
1. ``_rewrite_todo_names`` 纯函数：
   - ``todo_modify(action="update", …)`` 示例 → ``todo(action="modify", modify_action="update", …)``；
   - 裸 ``todo_modify`` / ``todo_create`` → ``todo(action="modify"/"create")``；
   - 无旧工具名 → 返回 None（守卫未命中）。
2. ``before_model_call``：
   - 命中即改写并保留 priority / category / 段名；
   - 同时改写 ``skill_execution_rules``（730，接管路径）与 ``skill_protocol``（回退路径）；
   - 守卫未命中（无旧工具名）→ 保留原段、不重注入；
   - 源段缺失 → no-op；
   - 非 dict content → no-op。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from jiuwenswarm.agents.harness.flash.rails.flash_skill_protocol_rail import (
    FlashSkillProtocolRail,
    _rewrite_todo_names,
)


class _FakeBuilder:
    """add_section 按名覆盖，get_section 按名取，get_all_sections 返回段列表。"""

    def __init__(self) -> None:
        self.sections: list = []

    def add_section(self, section) -> None:
        self.sections = [s for s in self.sections if s.name != section.name]
        self.sections.append(section)

    def get_section(self, name):
        for s in self.sections:
            if s.name == name:
                return s
        return None

    def get_all_sections(self) -> list:
        return list(self.sections)


class _FakeAgent:
    def __init__(self, sections=None) -> None:
        self.system_prompt_builder = _FakeBuilder()
        for s in sections or []:
            self.system_prompt_builder.add_section(s)


def _sec(name, priority, content) -> SimpleNamespace:
    return SimpleNamespace(name=name, content=content, priority=priority, category="cat-x")


# ── 1. _rewrite_todo_names 纯函数 ─────────────────────────────────
def test_rewrite_example_modify_update() -> None:
    text = '调用 todo_modify(action="update", task_id=..., status="done") 更新'
    out = _rewrite_todo_names(text)
    assert out == '调用 todo(action="modify", modify_action="update", task_id=..., status="done") 更新'


def test_rewrite_bare_modify_and_create() -> None:
    text = "先 todo_create 建条目，再 todo_modify 改它"
    out = _rewrite_todo_names(text)
    assert out == '先 todo(action="create") 建条目，再 todo(action="modify") 改它'


def test_rewrite_guard_miss_returns_none() -> None:
    assert _rewrite_todo_names("这里没有旧工具名，只有 todo(action=…)") is None


def test_rewrite_preserves_unrelated_text() -> None:
    text = "技能加载走 skill_tool；todo_create 后按 SKILL.md 执行。"
    out = _rewrite_todo_names(text)
    assert "skill_tool" in out
    assert "todo(action=\"create\")" in out
    assert "todo_create" not in out


# ── 2. before_model_call 改写 + 保留 priority ─────────────────────
@pytest.mark.asyncio
async def test_before_model_call_rewrites_and_preserves_priority() -> None:
    agent = _FakeAgent([
        _sec("skill_protocol", 45, {"cn": "先 todo_create，再 todo_modify(action=\"update\", id=1)"}),
    ])
    rail = FlashSkillProtocolRail()
    rail.init(agent)
    await rail.before_model_call(SimpleNamespace(agent=agent))

    section = agent.system_prompt_builder.get_section("skill_protocol")
    assert section.priority == 45
    assert section.category == "cat-x"
    assert section.content["cn"] == '先 todo(action="create")，再 todo(action="modify", modify_action="update", id=1)'


@pytest.mark.asyncio
async def test_before_model_call_rewrites_skill_execution_rules() -> None:
    """接管路径：730 skill_execution_rules 同样被改写（旧名 → 统一 todo）。"""
    agent = _FakeAgent([
        _sec("skill_execution_rules", 730, {"cn": "先 todo_create，再 todo_modify(action=\"update\", id=1)"}),
    ])
    rail = FlashSkillProtocolRail()
    rail.init(agent)
    await rail.before_model_call(SimpleNamespace(agent=agent))

    section = agent.system_prompt_builder.get_section("skill_execution_rules")
    assert section.priority == 730
    assert section.category == "cat-x"
    assert section.content["cn"] == '先 todo(action="create")，再 todo(action="modify", modify_action="update", id=1)'


@pytest.mark.asyncio
async def test_before_model_call_rewrites_both_sections() -> None:
    """两段同时存在（接管前的中间态）时，两个段名都要改写。"""
    agent = _FakeAgent([
        _sec("skill_execution_rules", 730, {"cn": "先 todo_create"}),
        _sec("skill_protocol", 45, {"cn": "再 todo_modify"}),
    ])
    rail = FlashSkillProtocolRail()
    rail.init(agent)
    await rail.before_model_call(SimpleNamespace(agent=agent))

    exec_sec = agent.system_prompt_builder.get_section("skill_execution_rules")
    proto_sec = agent.system_prompt_builder.get_section("skill_protocol")
    assert exec_sec.content["cn"] == '先 todo(action="create")'
    assert proto_sec.content["cn"] == '再 todo(action="modify")'


@pytest.mark.asyncio
async def test_before_model_call_guard_miss_keeps_original() -> None:
    original = {"cn": "全新文案，没有旧工具名"}
    agent = _FakeAgent([_sec("skill_protocol", 45, original)])
    rail = FlashSkillProtocolRail()
    rail.init(agent)
    await rail.before_model_call(SimpleNamespace(agent=agent))

    # 未改写：content 仍指向原 dict，且未重注入（段列表仍只有一段，且是原对象）。
    section = agent.system_prompt_builder.get_section("skill_protocol")
    assert section.content is original
    assert len(agent.system_prompt_builder.get_all_sections()) == 1


@pytest.mark.asyncio
async def test_before_model_call_missing_section_noop() -> None:
    agent = _FakeAgent([])
    rail = FlashSkillProtocolRail()
    rail.init(agent)
    await rail.before_model_call(SimpleNamespace(agent=agent))
    assert agent.system_prompt_builder.get_all_sections() == []


@pytest.mark.asyncio
async def test_before_model_call_non_dict_content_noop() -> None:
    agent = _FakeAgent([_sec("skill_protocol", 45, "not-a-dict")])
    rail = FlashSkillProtocolRail()
    rail.init(agent)
    await rail.before_model_call(SimpleNamespace(agent=agent))
    section = agent.system_prompt_builder.get_section("skill_protocol")
    assert section.content == "not-a-dict"


# ── 3. todo 禁用时删掉「必须使用 todo」整条（评审 #2） ─────────────────────
def test_build_protocol_text_todo_disabled() -> None:
    """_build_skill_protocol_section_text(todo_disabled=True) 去掉 todo 强制规则。"""
    from jiuwenswarm.agents.harness.common.rails.skill_protocol_prompt_rail import (
        _build_skill_protocol_section_text,
    )

    cn = _build_skill_protocol_section_text("cn", todo_disabled=True)
    en = _build_skill_protocol_section_text("en", todo_disabled=True)
    assert "必须使用 todo" not in cn
    assert "todo_create" not in cn and "todo_modify" not in cn
    assert "Use todo (mandatory)" not in en
    assert "todo_create" not in en and "todo_modify" not in en
    # 未禁用时保留规则 2。
    assert "必须使用 todo" in _build_skill_protocol_section_text("cn")
    assert "Use todo (mandatory)" in _build_skill_protocol_section_text("en")


@pytest.mark.asyncio
async def test_before_model_call_strips_todo_rules_when_disabled() -> None:
    """disabled_tools_provider 报告 todo 被禁时，整段删掉 todo 规则而非只改工具名。"""
    from jiuwenswarm.agents.harness.common.rails.skill_protocol_prompt_rail import (
        _build_skill_protocol_section_text,
    )

    content = _build_skill_protocol_section_text("cn")  # 完整正文（含规则 2）
    agent = _FakeAgent([_sec("skill_protocol", 45, {"cn": content})])
    rail = FlashSkillProtocolRail(disabled_tools_provider=lambda: ["todo"])
    rail.init(agent)
    await rail.before_model_call(SimpleNamespace(agent=agent))

    section = agent.system_prompt_builder.get_section("skill_protocol")
    assert "必须使用 todo" not in section.content["cn"]
    assert "todo_create" not in section.content["cn"]
    assert "todo_modify" not in section.content["cn"]
    assert "3. **严格顺序**" in section.content["cn"]
