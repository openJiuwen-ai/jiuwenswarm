# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""RailStateMachineBase 盲区补测。

``test_design_rail.py`` 已覆盖主路径（注册/转换/门禁/复位/注入三态/缓存/
文件索引）。本文件补齐其余分支：

- ``_handle_advance`` 永不抛出契约（内部异常 → ok:False）与无特性目录
  错误分支、非字符串 stage；
- ``_resolve_feature_name`` 语义：最新 mtime 优先、忽略隐藏目录/非目录、
  无根目录返回 None；
- ``_is_safe_feature_name`` 校验矩阵（路径穿越/分隔符/控制字符）；
- ``_strip_front_matter`` 单元（标准/缺失/未闭合）；
- 注册与卸载容错：add_ability/remove_ability 抛异常、缺 ability_manager、
  缺 system_prompt_builder；
- advance 工具 schema（stage 必填 + feature_name 仅复位用）与描述文案；
- ``_ensure_feature_dir`` 正常创建与 OSError 优雅降级。
"""
from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from jiuwenswarm.agents.harness.code.rails.sdd.design_rail.rail import DesignRail

pytestmark = [pytest.mark.unit]

VALID_CONFIG_DIR = Path(__file__).resolve().parents[4] / "jiuwenswarm" / "agents" / "harness" / "code" / "rails" / "sdd" / "design_rail"


def _make_builder() -> MagicMock:
    """A MagicMock system_prompt_builder tracking added/removed sections."""
    builder = MagicMock()
    builder.added_sections = []

    def _add(section):
        builder.added_sections = [s for s in builder.added_sections if s.name != section.name]
        builder.added_sections.append(section)
        return builder

    def _remove(name):
        builder.added_sections = [s for s in builder.added_sections if s.name != name]
        return builder

    builder.add_section = MagicMock(side_effect=_add)
    builder.remove_section = MagicMock(side_effect=_remove)
    return builder


def _make_agent(builder: MagicMock) -> MagicMock:
    agent = MagicMock()
    agent.system_prompt_builder = builder
    agent.ability_manager = MagicMock()
    agent.ability_manager.add_ability = MagicMock(return_value=SimpleNamespace(added=True))
    return agent


def _make_rail(project_dir: Path) -> DesignRail:
    return DesignRail(rail_pkg_dir=VALID_CONFIG_DIR, project_dir=project_dir, priority=60)


def _make_ctx() -> SimpleNamespace:
    return SimpleNamespace(inputs=SimpleNamespace(), extra={}, tool_result="")


def _touch_mtime(path: Path, t: float) -> None:
    os.utime(path, (t, t))


# ── _handle_advance：容错分支 ──


def test_advance_rejects_non_string_stage(tmp_path: Path) -> None:
    """非字符串 stage（LLM 传整数/漏参数）→ 明确拒绝而非崩溃。"""
    rail = _make_rail(tmp_path)
    rail._stage = "analysis"

    for bad in (None, 42, ["analysis"]):
        result = rail._handle_advance({"stage": bad})
        assert result["ok"] is False
        assert "invalid stage" in result["error"]
    assert rail._stage == "analysis"


def test_advance_error_branch_no_feature_dir(tmp_path: Path) -> None:
    """有产物门禁但 .aet/features/ 不存在 → 错误分支引导 LLM 先建目录。"""
    rail = _make_rail(tmp_path)
    rail._stage = "analysis"

    result = rail._handle_advance({"stage": "analysis_review"})

    assert result["ok"] is False
    assert "no feature directory found" in result["error"]
    assert "requirements-analysis.md" in result["error"]  # 指明要产出什么
    assert rail._stage == "analysis"


def test_advance_never_raises_on_internal_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """内部异常必须被吞掉并转成 ok:False —— 工具调用不能炸掉 agent 会话。"""

    def boom(stage: str) -> bool:
        raise RuntimeError("internal boom")

    rail = _make_rail(tmp_path)
    rail._stage = "analysis"
    monkeypatch.setattr(rail, "_check_artifacts", boom)

    result = rail._handle_advance({"stage": "analysis_review"})

    assert result["ok"] is False
    assert "internal boom" in result["error"]


# ── _check_artifacts ──


def test_check_artifacts_vacuous_when_stage_declares_none(tmp_path: Path) -> None:
    """init 无产物声明 → 恒 True（空项目也放行）。"""
    rail = _make_rail(tmp_path)
    assert rail._check_artifacts("init") is True


def test_check_artifacts_true_when_artifact_present(tmp_path: Path) -> None:
    rail = _make_rail(tmp_path)
    design_dir = tmp_path / ".aet" / "features" / "feat" / "design"
    design_dir.mkdir(parents=True)
    (design_dir / "requirements-analysis.md").write_text("# RAS", encoding="utf-8")
    assert rail._check_artifacts("analysis") is True


def test_check_artifacts_false_when_no_feature_dir(tmp_path: Path) -> None:
    rail = _make_rail(tmp_path)
    assert rail._check_artifacts("analysis") is False


# ── _resolve_feature_name：特性解析语义 ──


def test_resolve_feature_name_picks_latest_mtime(tmp_path: Path) -> None:
    features = tmp_path / ".aet" / "features"
    old = features / "feat-old"
    new = features / "feat-new"
    old.mkdir(parents=True)
    new.mkdir()
    _touch_mtime(old, 1_000_000_000)
    _touch_mtime(new, 1_000_000_100)

    rail = _make_rail(tmp_path)
    assert rail._resolve_feature_name() == "feat-new"


def test_resolve_feature_name_ignores_hidden_dirs_and_files(tmp_path: Path) -> None:
    features = tmp_path / ".aet" / "features"
    features.mkdir(parents=True)
    (features / ".git").mkdir()
    (features / "notes.md").write_text("not a dir", encoding="utf-8")
    (features / "feat").mkdir()

    rail = _make_rail(tmp_path)
    assert rail._resolve_feature_name() == "feat"

    # 只剩隐藏目录与文件 → None
    (features / "feat").rmdir()
    assert rail._resolve_feature_name() is None


def test_resolve_feature_name_no_root_returns_none(tmp_path: Path) -> None:
    rail = _make_rail(tmp_path)
    assert rail._resolve_feature_name() is None


# ── _is_safe_feature_name：校验矩阵 ──


@pytest.mark.parametrize(
    "name",
    ["my-feature", "feat_1", "F2", "中文特性", "a.b"],
)
def test_is_safe_feature_name_accepts_simple_names(name: str) -> None:
    assert DesignRail._is_safe_feature_name(name) is True


@pytest.mark.parametrize(
    "name",
    ["", "   ", ".", "..", "a/b", "a\\b", ".hidden", "x\ny", "x\ry", "x\0y"],
)
def test_is_safe_feature_name_rejects_dangerous_names(name: str) -> None:
    """路径穿越/分隔符/控制字符一律拒绝 —— feature_name 来自 LLM。"""
    assert DesignRail._is_safe_feature_name(name) is False


# ── _strip_front_matter ──


def test_strip_front_matter_standard_block() -> None:
    raw = "---\nname: aet-req-analysis\n---\n\n按步骤完成需求分析"
    assert DesignRail._strip_front_matter(raw) == "按步骤完成需求分析"


def test_strip_front_matter_no_block_unchanged() -> None:
    raw = "正文以 --- 开头以外的内容"
    assert DesignRail._strip_front_matter(raw) == raw


def test_strip_front_matter_unclosed_block_unchanged() -> None:
    """未闭合的 front-matter（无结束 ---）→ 原样返回，不吞正文。"""
    raw = "---\nname: broken\n正文内容"
    assert DesignRail._strip_front_matter(raw) == raw


# ── 注册 / 卸载容错 ──


def test_init_survives_ability_manager_failure(tmp_path: Path) -> None:
    """add_ability 抛异常 → init 不崩、不登记 owned（agent 创建绝不因 rail 失败）。"""
    builder = _make_builder()
    agent = _make_agent(builder)
    agent.ability_manager.add_ability.side_effect = RuntimeError("registry down")
    rail = _make_rail(tmp_path)

    rail.init(agent)  # 不抛出

    assert rail._owned_tool_names == set()


def test_init_without_ability_manager(tmp_path: Path) -> None:
    """agent 没有 ability_manager 属性 → 记 warning、正常完成 init。"""
    builder = _make_builder()
    agent = SimpleNamespace(system_prompt_builder=builder)  # 无 ability_manager
    rail = _make_rail(tmp_path)

    rail.init(agent)

    assert rail._owned_tool_names == set()
    assert rail._system_prompt_builder is builder


def test_uninit_continues_when_remove_ability_raises(tmp_path: Path) -> None:
    """remove_ability 抛异常 → 逐个捕获，owned 集合仍清空。"""
    builder = _make_builder()
    agent = _make_agent(builder)
    rail = _make_rail(tmp_path)
    rail.init(agent)
    rail._owned_tool_names.add("extra_tool")  # 模拟多个 owned 工具
    agent.ability_manager.remove_ability.side_effect = RuntimeError("remove failed")

    rail.uninit(agent)  # 不抛出

    assert rail._owned_tool_names == set()


@pytest.mark.asyncio
async def test_init_without_system_prompt_builder_disables_injection(
    tmp_path: Path,
) -> None:
    """无 system_prompt_builder → 注入禁用，before_model_call 静默跳过。"""
    agent = MagicMock()
    agent.system_prompt_builder = None
    agent.ability_manager = MagicMock()
    agent.ability_manager.add_ability = MagicMock(return_value=SimpleNamespace(added=True))
    rail = _make_rail(tmp_path)
    rail.init(agent)

    assert rail._system_prompt_builder is None

    await rail.before_model_call(_make_ctx())  # 静默跳过，不抛出


# ── before_model_call 边缘分支 ──


@pytest.mark.asyncio
async def test_before_model_call_unknown_stage_removes_section_only(tmp_path: Path) -> None:
    """stage 被外部篡改为非法值 → 只移除旧 section，不再注入（不崩、不误导 LLM）。"""
    builder = _make_builder()
    agent = _make_agent(builder)
    rail = _make_rail(tmp_path)
    rail.init(agent)
    rail._stage = "analysis"
    await rail.before_model_call(_make_ctx())
    assert any(s.name == "sdd_skill" for s in builder.added_sections)

    rail._stage = "bogus_stage"
    await rail.before_model_call(_make_ctx())

    assert not any(s.name == "sdd_skill" for s in builder.added_sections)


@pytest.mark.asyncio
async def test_before_model_call_replaces_previous_stage_section(tmp_path: Path) -> None:
    """阶段推进后旧方法论被替换，不会双份注入（add-or-replace 语义）。"""
    builder = _make_builder()
    agent = _make_agent(builder)
    rail = _make_rail(tmp_path)
    rail.init(agent)
    rail._stage = "analysis"
    await rail.before_model_call(_make_ctx())
    rail._stage = "analysis_review"
    await rail.before_model_call(_make_ctx())

    sdd = [s for s in builder.added_sections if s.name == "sdd_skill"]
    assert len(sdd) == 1
    assert "Review target" in sdd[0].content["cn"]


# ── advance 工具 schema 与描述 ──


def test_advance_tool_input_params_schema(tmp_path: Path) -> None:
    """stage 必填、feature_name 可选且仅复位场景使用。"""
    rail = _make_rail(tmp_path)
    params = rail._advance_tool_input_params()

    assert params["type"] == "object"
    assert params["required"] == ["stage"]
    assert set(params["properties"]) == {"stage", "feature_name"}
    assert "analysis" in params["properties"]["stage"]["description"]  # 复位提示含 init-next


def test_advance_tool_description_mentions_flow_entry(tmp_path: Path) -> None:
    """描述告诉 LLM 从首阶段进入流程、到 done 才允许实现。"""
    rail = _make_rail(tmp_path)
    desc = rail._advance_tool_description()

    assert "sdd_advance" in desc
    assert "'analysis'" in desc
    assert "done" in desc


# ── _ensure_feature_dir ──


def test_ensure_feature_dir_creates_nested_path(tmp_path: Path) -> None:
    rail = _make_rail(tmp_path)
    rail._ensure_feature_dir("feat")

    assert (tmp_path / ".aet" / "features" / "feat" / "design").is_dir()


def test_ensure_feature_dir_graceful_on_oserror(tmp_path: Path) -> None:
    """.aet 被同名文件占用（mkdir 必败）→ 记 warning 不抛出。"""
    (tmp_path / ".aet").write_text("i am a file", encoding="utf-8")
    rail = _make_rail(tmp_path)

    rail._ensure_feature_dir("feat")  # 不抛出

    assert not (tmp_path / ".aet" / "features").exists()


# ── _current_stage ──


def test_current_stage_defaults_to_init(tmp_path: Path) -> None:
    rail = _make_rail(tmp_path)
    assert rail._current_stage() == "init"
    rail._stage = "design"
    assert rail._current_stage() == "design"
