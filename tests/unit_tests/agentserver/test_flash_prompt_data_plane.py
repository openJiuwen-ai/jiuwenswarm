# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Flash 提示词数据面（sections / registry / content / snapshot / offline）最小回归集。

只依赖 ``flash.prompt.*`` 纯函数 + 真实 Deep 提示词构建函数，不 import 任何 flash rail，
保证「先数据面、后控制面」的分批迁移中，数据面可独立回归。
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import sys
import types
from pathlib import Path

import pytest

# ── 本地开发 import 桩（CI 有 agent_shield 时自动跳过）──────────────────────
# 数据面测试不应被 flash 包 ``__init__`` 的 rails 导入链拖累；缺 agent_shield 时
# 给 flash 包打空命名空间，避开 ``flash/__init__.py`` 对 rails 的 eager import。
if importlib.util.find_spec("agent_shield") is None:
    if "openjiuwen.harness.rails.base" not in sys.modules:
        _rails_base = types.ModuleType("openjiuwen.harness.rails.base")
        _rails_base.DeepAgentRail = type("DeepAgentRail", (), {})
        sys.modules["openjiuwen.harness.rails.base"] = _rails_base

    _flash_dir = str(
        Path(__file__).resolve().parents[3]
        / "jiuwenswarm" / "agents" / "harness" / "flash"
    )
    for _name, _path in (
        ("jiuwenswarm.agents.harness.flash", [_flash_dir]),
        ("jiuwenswarm.agents.harness.flash.rails", [os.path.join(_flash_dir, "rails")]),
    ):
        if _name not in sys.modules:
            _pkg = types.ModuleType(_name)
            _pkg.__path__ = _path
            sys.modules[_name] = _pkg

from jiuwenswarm.agents.harness.flash.prompt.content.relay import (  # noqa: E402
    _ANCHOR_MEMORY_CORRECTION,
    _ANCHOR_WORKSPACE_CONSTRAINT,
    _strip_relay_duplicates,
)
from jiuwenswarm.agents.harness.flash.prompt.content.safety import (  # noqa: E402
    _ANCHORS_CN,
    _ANCHORS_EN,
    _transform_safety,
)
from jiuwenswarm.agents.harness.flash.prompt.offline import assemble  # noqa: E402
from jiuwenswarm.agents.harness.flash.prompt.registry import (  # noqa: E402
    PromptRenderContext,
    build_default_context,
    reconcile,
)
from jiuwenswarm.agents.harness.flash.prompt.snapshot import (  # noqa: E402
    build_prompt_snapshot,
)
from jiuwenswarm.agents.harness.flash.sections import (  # noqa: E402
    FLASH_SECTION_REGISTRY,
)
from openjiuwen.harness.prompts.sections.safety import (  # noqa: E402
    build_safety_section,
)

# ── 本仓自有段 golden hash（排除 safety 的 SDK 源段，源正文来自 openjiuwen dev-stable 浮动分支）──
_SAFETY_SECTION_NAMES = {
    "uncertainty_failure",
    "safety_general",
    "refusal_behavior",
    "deletion_protocol",
}

_GOLDEN_OWNED_HASH = {
    "cn": "306e59f0870d7c158a3940d25195357f3f2a45803821cc5483ccf2d8f223d44a",
    "en": "65254ea26b64ce0ec061a5856922505b4aaf8fece9d1388ed3c3499584b303e7",
}


def _repo_owned_hash(snapshot) -> str:
    """按 snapshot 段 hash 同款拼接格式，对本仓自有段（排除 safety）算 hash。"""
    payload = "\n".join(
        f"{priority}\0{name}\0{sha256}"
        for priority, name, sha256 in snapshot.section_hashes
        if name not in _SAFETY_SECTION_NAMES
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()

_EXPECTED_ORDER = [
    (30, "uncertainty_failure"),
    (100, "safety_general"),
    (110, "refusal_behavior"),
    (120, "deletion_protocol"),
    (300, "tool_selection"),
    (330, "bash_discipline"),
    (340, "tool_result"),
    (410, "thinking_discipline"),
    (430, "todo"),
    (440, "task_tool"),
    (500, "language_output"),
    (510, "response"),
    (520, "final_visible_reply"),
    (540, "runtime.model_answer_policy"),
]


def _section_text(section, language: str) -> str:
    content = getattr(section, "content", None)
    if isinstance(content, dict):
        value = content.get(language)
        if not value:
            value = next((item for item in content.values() if item), "")
        return str(value)
    return str(content or "")


# ── 1. registry：reconcile 领号对账 + 工具域门控（全开 14 段 / 关闭 12 段）──
def test_reconcile_outputs_registered_priorities() -> None:
    sections = reconcile(build_default_context("cn"))
    assert [(s.priority, s.name) for s in sections] == _EXPECTED_ORDER
    for section in sections:
        assert section.priority in FLASH_SECTION_REGISTRY, (
            f"段号 {section.priority}（{section.name}）未在领号表登记"
        )


def test_reconcile_gates_tool_domains() -> None:
    empty = reconcile(PromptRenderContext(lang="cn", enabled_tools=frozenset()))
    assert len(empty) == 12
    names = {section.name for section in empty}
    assert "todo" not in names
    assert "task_tool" not in names


def test_reconcile_gates_browser_routing_on_task_tool() -> None:
    # 关 task_tool：340 仍产出，但不得残留 browser_agent 强制路由指令。
    empty = reconcile(PromptRenderContext(lang="cn", enabled_tools=frozenset()))
    result = next(s for s in empty if s.name == "tool_result")
    assert "browser_agent" not in result.content["cn"]
    assert "浏览器任务强制路由" not in result.content["cn"]

    # 开 task_tool（默认上下文）：browser_agent 路由保留。
    full = reconcile(build_default_context("cn"))
    result = next(s for s in full if s.name == "tool_result")
    assert "browser_agent" in result.content["cn"]
    assert "浏览器任务强制路由" in result.content["cn"]


# ── 2. content.safety：_transform_safety 双语锚点命中 / 缺失回退 ─────────────
@pytest.mark.parametrize(
    ("language", "anchors"),
    [("cn", _ANCHORS_CN), ("en", _ANCHORS_EN)],
)
def test_transform_safety_hits_all_anchors(language: str, anchors: dict) -> None:
    src = build_safety_section(language)
    assert src is not None, "upstream safety source missing"
    result = _transform_safety(_section_text(src, language), anchors)
    assert result is not None
    assert [(name, priority) for name, priority, _text in result] == [
        ("uncertainty_failure", 30),
        ("safety_general", 100),
        ("refusal_behavior", 110),
        ("deletion_protocol", 120),
    ]


def test_transform_safety_missing_anchor_returns_none() -> None:
    assert _transform_safety("没有锚点的正文", _ANCHORS_CN) is None


# ── 3. content.relay：_strip_relay_duplicates 三分支（含边界回退）─────────────
def test_strip_relay_duplicates_missing_anchor_returns_none() -> None:
    assert _strip_relay_duplicates("只有正文没有锚点") is None
    assert _strip_relay_duplicates("") is None


def test_strip_relay_duplicates_no_tail_anchor_returns_none() -> None:
    # [文件输出约束] 后无 [记忆纠正规则] 收尾 → 无法确定块边界，回退而非盲删到段尾。
    assert _strip_relay_duplicates(f"{_ANCHOR_WORKSPACE_CONSTRAINT}\n文件约束正文\n") is None


def test_strip_relay_duplicates_clean_span() -> None:
    content = (
        "前置内容\n"
        f"{_ANCHOR_WORKSPACE_CONSTRAINT}\n文件约束正文\n"
        f"{_ANCHOR_MEMORY_CORRECTION}\n记忆正文\n"
    )
    result = _strip_relay_duplicates(content)
    assert result is not None
    assert _ANCHOR_WORKSPACE_CONSTRAINT not in result
    assert _ANCHOR_MEMORY_CORRECTION in result
    assert result.startswith("前置内容")


def test_strip_relay_duplicates_foreign_header_returns_none() -> None:
    content = (
        "前置内容\n"
        f"{_ANCHOR_WORKSPACE_CONSTRAINT}\n文件约束正文\n"
        "[其它规则]\n其它正文\n"
        f"{_ANCHOR_MEMORY_CORRECTION}\n记忆正文\n"
    )
    assert _strip_relay_duplicates(content) is None


# ── 4. snapshot / offline：本仓自有段 golden hash 锁定（零漂移）────────────────
@pytest.mark.parametrize("language", ["cn", "en"])
def test_offline_assembly_matches_golden_hash(language: str) -> None:
    snapshot = build_prompt_snapshot(assemble(language), language=language)
    assert len(snapshot.sections) == 14
    # safety 域源正文来自 SDK（openjiuwen dev-stable 浮动分支），只校验产出 4 段存在，
    # 不锁精确 hash，否则 CI 会随上游推进随机红。
    safety_names = {
        name for _priority, name, _sha in snapshot.section_hashes
        if name in _SAFETY_SECTION_NAMES
    }
    assert safety_names == _SAFETY_SECTION_NAMES
    assert _repo_owned_hash(snapshot) == _GOLDEN_OWNED_HASH[language]


def test_offline_assembly_is_deterministic() -> None:
    first = build_prompt_snapshot(assemble("cn"), language="cn")
    second = build_prompt_snapshot(assemble("cn"), language="cn")
    assert first.full_hash == second.full_hash
    assert first.section_hashes == second.section_hashes
