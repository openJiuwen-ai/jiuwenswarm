# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Unit tests for IdentityRail (jiuwenswarm product-side).

Covers the _read_identity_md校验顺序修复:
- 填了真实名字(即使保留模板引导行) → 采用文件内容
- 默认模板原样(未填名字) → 回退默认身份段
- 空文件 → 回退默认身份段
- 有实质内容但无名字字段 → 采用文件内容
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from jiuwenswarm.agents.harness.common.rails.identity_rail import IdentityRail

_DEFAULT_TEMPLATE_ZH = """# 身份

_在你们的第一次对话中填写。让它属于你。_

- **名字：**
  _(选一个你喜欢的)_
- **形态：**
  _(AI？机器人？精灵？)_
- **风格：**
  _(你怎么说话？锋利？温暖？混乱？冷静？)_
- **表情符号：**
  _(你的签名 — 选一个感觉对路的)_
- **头像：**
  _(工作空间相对路径、http(s) URL 或 data URI)_

---

这不仅仅是元数据。这是弄清楚你是谁的开始。

备注：

- 把文件保存到工作空间根目录为 `IDENTITY.md`。
"""


def _make_rail(tmp_path: Path, content: str | None) -> IdentityRail:
    """Build an IdentityRail bound to a tmp IDENTITY.md file."""
    identity_path = tmp_path / "IDENTITY.md"
    if content is not None:
        identity_path.write_text(content, encoding="utf-8")
    rail = IdentityRail(language="cn", identity_md_path=str(identity_path))
    rail._system_prompt_builder = MagicMock()
    # 默认当前无 identity section（归 rail 管理），各测试按需覆盖 get_section 返回值
    rail._system_prompt_builder.get_section.return_value = None
    return rail


def test_filled_name_with_template_guidance_returns_content(tmp_path: Path) -> None:
    """填了真实名字但保留了模板引导行 → 应采用文件内容(非回退默认)."""
    content = (
        "# 身份\n\n"
        "_在你们的第一次对话中填写。让它属于你。_\n\n"
        "- **名字：** 小艺\n"
        "- **形态：** AI 助手\n"
    )
    rail = _make_rail(tmp_path, content)
    result = rail._read_identity_md()
    assert result is not None
    assert "小艺" in result
    assert "在你们的第一次对话中填写" in result  # 模板引导行保留也通过


def test_default_template_returns_none(tmp_path: Path) -> None:
    """默认模板原样(未填名字) → 回退默认(返回 None)."""
    rail = _make_rail(tmp_path, _DEFAULT_TEMPLATE_ZH)
    result = rail._read_identity_md()
    assert result is None


def test_empty_file_returns_none(tmp_path: Path) -> None:
    """空文件 → 回退默认(返回 None)."""
    rail = _make_rail(tmp_path, "")
    result = rail._read_identity_md()
    assert result is None


def test_missing_file_returns_none(tmp_path: Path) -> None:
    """文件不存在 → 回退默认(返回 None)."""
    rail = _make_rail(tmp_path, None)
    result = rail._read_identity_md()
    assert result is None


def test_filled_name_without_template_markers_returns_content(tmp_path: Path) -> None:
    """填了名字、无模板引导行 → 采用文件内容."""
    content = "# 身份\n\n- 名字：小艺\n- 形态：AI 助手\n"
    rail = _make_rail(tmp_path, content)
    result = rail._read_identity_md()
    assert result is not None
    assert "小艺" in result


def test_substantive_content_without_name_field_returns_content(tmp_path: Path) -> None:
    """有实质内容但无 ``名字:`` 字段 → 采用文件内容(兜底)."""
    content = (
        "# 我的身份\n\n"
        "你是一个专业的代码助手，专注于 Python 和 TypeScript。\n"
        "回答风格：简洁、直接、代码优先。\n"
    )
    rail = _make_rail(tmp_path, content)
    result = rail._read_identity_md()
    assert result is not None
    assert "代码助手" in result


def test_name_on_separate_line_returns_content(tmp_path: Path) -> None:
    """名字字段后换行、下一行是真实名字 → 采用文件内容.

    默认模板的 ``**名字：**`` 后面换行跟 ``_(选一个你喜欢的)_`` 占位符时,
    ``_identity_has_filled_name`` 返回 False (因 ``_(...)_`` 被 _clean_agent_name
    清洗后为空)。但如果下一行是真实名字(如 ``小艺``), 正则的 ``\\s*`` 跨行匹配
    会把它识别为已填名字 → 采用文件内容。

    这是正则的隐藏宽容行为——对用户有利(名字在下一行也能识别)。
    """
    content = (
        "# 身份\n\n"
        "_在你们的第一次对话中填写。让它属于你。_\n\n"
        "- **名字：**\n"
        "  小艺\n"  # 名字在下一行 — 正则 \s* 跨行匹配,识别为已填
    )
    rail = _make_rail(tmp_path, content)
    result = rail._read_identity_md()
    assert result is not None
    assert "小艺" in result


def test_before_model_call_uses_file_content_when_filled(tmp_path: Path) -> None:
    """填了名字 → before_model_call 注入文件内容到 identity section."""
    content = "# 身份\n\n- 名字：小艺\n"
    rail = _make_rail(tmp_path, content)
    # 模拟 init 设置的默认段
    rail._default_section = MagicMock()
    import asyncio

    asyncio.run(rail.before_model_call(MagicMock()))
    # remove_section 应被调用
    rail._system_prompt_builder.remove_section.assert_called_once_with("identity")
    # add_section 应被调用(用文件内容, 不是默认段)
    rail._system_prompt_builder.add_section.assert_called_once()
    added_section = rail._system_prompt_builder.add_section.call_args[0][0]
    assert "小艺" in added_section.content["cn"]


def test_before_model_call_falls_back_to_default_when_template(tmp_path: Path) -> None:
    """默认模板 → before_model_call 加载默认身份段."""
    rail = _make_rail(tmp_path, _DEFAULT_TEMPLATE_ZH)
    default_section = MagicMock()
    rail._default_section = default_section
    import asyncio

    asyncio.run(rail.before_model_call(MagicMock()))
    rail._system_prompt_builder.remove_section.assert_called_once_with("identity")
    # 应该加 default_section, 不是从文件内容构造的新 PromptSection
    rail._system_prompt_builder.add_section.assert_called_once_with(default_section)


def _section_with_content(content: dict) -> MagicMock:
    section = MagicMock()
    section.content = content
    return section


def test_before_model_call_yields_to_expert_persona(tmp_path: Path) -> None:
    """identity section 被专家 persona（replace_existing 热替换）占用 → 整段让路."""
    rail = _make_rail(tmp_path, "# 身份\n\n- 名字：小艺\n")  # 即使 IDENTITY.md 已填
    rail._default_section = _section_with_content({"en": "default identity"})
    expert_persona = {"cn": "# A股研投智囊\n\n你是一名全栈A股投研分析师……", "en": "..."}
    rail._system_prompt_builder.get_section.return_value = _section_with_content(
        expert_persona
    )
    import asyncio

    asyncio.run(rail.before_model_call(MagicMock()))
    rail._system_prompt_builder.remove_section.assert_not_called()
    rail._system_prompt_builder.add_section.assert_not_called()


def test_before_model_call_resumes_after_foreign_identity_released(tmp_path: Path) -> None:
    """专家卸载后 identity 还原为默认身份段 → rail 恢复管理（正常覆盖）."""
    rail = _make_rail(tmp_path, _DEFAULT_TEMPLATE_ZH)
    default_section = _section_with_content({"en": "default identity"})
    rail._default_section = default_section
    # 卸载还原回来的正是默认身份 → 归 rail 管理
    rail._system_prompt_builder.get_section.return_value = _section_with_content(
        {"en": "default identity"}
    )
    import asyncio

    asyncio.run(rail.before_model_call(MagicMock()))
    rail._system_prompt_builder.remove_section.assert_called_once_with("identity")
    rail._system_prompt_builder.add_section.assert_called_once_with(default_section)


def test_before_model_call_refreshes_own_installed_section(tmp_path: Path) -> None:
    """rail 自己装上的 IDENTITY.md 内容仍归它管：文件改了下轮刷新."""
    identity_path = tmp_path / "IDENTITY.md"
    identity_path.write_text("# 身份\n\n- 名字：小艺\n", encoding="utf-8")
    rail = _make_rail(tmp_path, None)
    rail._default_section = _section_with_content({"en": "default identity"})
    import asyncio

    asyncio.run(rail.before_model_call(MagicMock()))
    added_a = rail._system_prompt_builder.add_section.call_args[0][0]
    assert added_a.content == {"cn": "# 身份\n\n- 名字：小艺"}  # _read_identity_md 会 strip

    # 下一轮：当前 section 是 rail 自己装的（受管），文件已改 → 应刷新为乙
    identity_path.write_text("# 身份\n\n- 名字：小艺二号\n", encoding="utf-8")
    rail._system_prompt_builder.reset_mock()
    rail._system_prompt_builder.get_section.return_value = _section_with_content(
        dict(added_a.content)
    )
    asyncio.run(rail.before_model_call(MagicMock()))
    rail._system_prompt_builder.remove_section.assert_called_once_with("identity")
    added_b = rail._system_prompt_builder.add_section.call_args[0][0]
    assert "小艺二号" in added_b.content["cn"]


def test_before_model_call_manages_default_identity_section(tmp_path: Path) -> None:
    """当前 identity 是默认身份段（静态安装）→ 归 rail 管理，正常走覆盖逻辑."""
    rail = _make_rail(tmp_path, "# 身份\n\n- 名字：小艺\n")
    rail._default_section = _section_with_content({"en": "default identity"})
    rail._system_prompt_builder.get_section.return_value = _section_with_content(
        {"en": "default identity"}
    )
    import asyncio

    asyncio.run(rail.before_model_call(MagicMock()))
    added = rail._system_prompt_builder.add_section.call_args[0][0]
    assert "小艺" in added.content["cn"]
