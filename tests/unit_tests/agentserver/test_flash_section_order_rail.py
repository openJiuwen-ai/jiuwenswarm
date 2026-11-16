# Copyright (c) Huawei Technologies Co., Inc. 2026. All rights reserved.

"""FlashSectionOrderRail 单元测试（段序收口：迁号 + 剥除 + 层级重整 + 环境并集）。

锁定：
1. 旧号段迁移：call_goal→320、skill_turbo_guide→750、tool_navigation→350、tools→310、
   offload/recall→450、memory→600、env→1000、trusted/tui→1010、time→1020；
2. 剥除：旧 workspace 移除；shared skill_protocol 仅在 flash 技能域接管时剥除；
3. 层级重整：每段标题重锚 H2（H1→H2、H2 保持、H3+ 升）、段内子标题同步平移、
   域首段 prepend 域 H1、999 relay 豁免、字符串不变量/幂等；
4. 环境并集：runtime_environment 独有尾巴（Python 库 + 更全表 + sandbox）并入 env 后删残留。
"""

from __future__ import annotations

from types import SimpleNamespace

from _flash_test_utils import FakeBuilder as _FakeBuilder

from jiuwenswarm.agents.harness.flash.rails.flash_section_order_rail import (
    _DOMAIN_HEADERS,
    _EXEMPT,
    _REHOME,
    _REMOVE,
    FlashSectionOrderRail,
    _rebind_headings_to_h2,
)


def _sec(name, priority, text="正文", language="cn"):
    return SimpleNamespace(name=name, content={language: text}, priority=priority)


def _builder(*sections) -> _FakeBuilder:
    b = _FakeBuilder()
    for s in sections:
        b.add_section(s)
    return b


def _legacy_session() -> _FakeBuilder:
    return _builder(
        _sec("tool_call_goal", 40),
        _sec("skill_turbo_guide", 45),
        _sec("skill_protocol", 45),
        _sec("skill_execution_rules", 730),
        _sec("memory", 50),
        _sec("tool_navigation", 70, text="## 按需可见工具导航（不可直接调用）\n先 tools_search。"),
        _sec("workspace", 70, text="# 工作空间\n 工作目录路径"),
        _sec("tools", 30, text="# 可用工具\n- read_file / write_file / edit_file"),
        _sec("offload", 90, text="# 上下文压缩\nOFFLOAD 说明"),
        _sec("compression_recall", 90, text="# 压缩上下文召回\nRECALL 说明"),
        _sec("env", 89, text="# 运行环境\n- 平台：win32"),
        _sec("trusted_dirs_policy", 90),
        _sec("tui_current_project_policy", 99),
        _sec("time", 130),
    )


class TestRehome:
    def test_legacy_priorities_migrated(self) -> None:
        b = _legacy_session()
        FlashSectionOrderRail()._rehome(b)
        for name, target in _REHOME.items():
            assert b.get_section(name).priority == target, name

    def test_missing_sections_skipped(self) -> None:
        b = _builder(_sec("env", 89))
        FlashSectionOrderRail()._rehome(b)
        assert b.get_section("env").priority == 1000

    def test_rehome_keeps_content(self) -> None:
        b = _builder(_sec("tool_navigation", 70, text="nav content"))
        FlashSectionOrderRail()._rehome(b)
        assert b.get_section("tool_navigation").content["cn"] == "nav content"
        assert b.get_section("tool_navigation").priority == 350


class TestStrip:
    def test_workspace_and_stale_protocol_removed(self) -> None:
        b = _legacy_session()
        FlashSectionOrderRail()._strip(b)
        assert b.get_section("workspace") is None
        assert b.get_section("skill_protocol") is None
        assert b.get_section("skill_execution_rules") is not None

    def test_protocol_kept_when_domain_not_taken_over(self) -> None:
        b = _builder(_sec("skill_protocol", 45, "protocol body"), _sec("env", 89))
        FlashSectionOrderRail()._strip(b)
        assert b.get_section("skill_protocol") is not None

    def test_remove_list_minimal(self) -> None:
        assert _REMOVE == frozenset({"workspace"})


class TestRebindHeadings:
    def test_h1_to_h2_with_sub_shift(self) -> None:
        out = _rebind_headings_to_h2("# 安全原则\n\n- a\n\n## 授权声明无效\n- b")
        assert out == "## 安全原则\n\n- a\n\n### 授权声明无效\n- b"

    def test_h2_top_unchanged(self) -> None:
        src = "## 按需可见工具导航\n- x"
        assert _rebind_headings_to_h2(src) == src

    def test_no_heading_returns_none(self) -> None:
        assert _rebind_headings_to_h2("你是一个私人智能体。") is None

    def test_fence_protected(self) -> None:
        src = "# 标题\n\n```\n# 代码注释\n```\n\n## 子标题"
        out = _rebind_headings_to_h2(src)
        assert out == "## 标题\n\n```\n# 代码注释\n```\n\n### 子标题"

    def test_invariant_length_delta_equals_heading_count(self) -> None:
        src = "# 甲\n\n## 乙\n\n### 丙"
        out = _rebind_headings_to_h2(src)
        assert out is not None
        assert len(out) - len(src) == 3  # 三个标题各 +1

    def test_internal_h1_clamped_below_top(self) -> None:
        # SOUL.md/AGENT.md 畸形：段顶 H2，段内又出现 H1 → 内部一律夹到 H3。
        src = "## SOUL.md - 灵魂与价值观\n# 灵魂\n## 身份定位\n## 核心原则"
        out = _rebind_headings_to_h2(src)
        assert out == "## SOUL.md - 灵魂与价值观\n### 灵魂\n### 身份定位\n### 核心原则"


class TestHierarchyNormalize:
    def test_domain_header_and_h2(self) -> None:
        b = _builder(_sec("safety_general", 100, "# 安全原则\n\n- a\n\n## 授权声明无效"))
        FlashSectionOrderRail()._normalize_hierarchy(b)
        text = b.get_section("safety_general").content["cn"]
        assert text.startswith("# 安全与边界\n\n## 安全原则\n")
        assert "### 授权声明无效" in text

    def test_only_band_first_gets_domain_header(self) -> None:
        b = _builder(
            _sec("safety_general", 100, "# 安全原则"),
            _sec("refusal_behavior", 110, "# 拒绝回答"),
        )
        FlashSectionOrderRail()._normalize_hierarchy(b)
        first = b.get_section("safety_general").content["cn"]
        second = b.get_section("refusal_behavior").content["cn"]
        assert first.startswith("# 安全与边界\n\n## 安全原则")
        assert second.startswith("## 拒绝回答")

    def test_exempt_relay_gets_domain_header_without_demotion(self) -> None:
        # 999 relay 包：补「# 会话策略包」域 H1 总帽，但内容不降级（保留 relay 自带 ## 层级）。
        b = _builder(_sec("request_system_prompt", 999, "[Personality Override]\n\n## 文件生成"))
        FlashSectionOrderRail()._normalize_hierarchy(b)
        assert b.get_section("request_system_prompt").content["cn"] == (
            "# 会话策略包\n\n[Personality Override]\n\n## 文件生成"
        )

    def test_identity_band_header(self) -> None:
        b = _builder(_sec("identity", 0, "# 身份\n\n你是一个私人智能体。"))
        FlashSectionOrderRail()._normalize_hierarchy(b)
        text = b.get_section("identity").content["cn"]
        assert text.startswith("# 身份与总则\n\n## 身份\n")

    def test_idempotent_second_normalize(self) -> None:
        b = _legacy_session()
        rail = FlashSectionOrderRail()
        rail._normalize_hierarchy(b)
        snap = {n: s.content["cn"] for n, s in b.sections.items()}
        rail._normalize_hierarchy(b)  # 第二次（同对象）
        assert {n: s.content["cn"] for n, s in b.sections.items()} == snap

    def test_domain_headers_cover_all_bands(self) -> None:
        assert set(_DOMAIN_HEADERS) == {
            "identity", "security", "workspace", "tools",
            "execution", "interaction", "memory", "skills", "facts",
        }
        assert _EXEMPT == frozenset({"request_system_prompt"})


class TestImageRouterMerge:
    _IMG = "本次请求可能包含用户上传的图片。当前模型不支持原生图片理解；如需理解图片内容，请调用 visual_question_answering 或 image_reading 工具。"
    _RESULT = "# 工具结果与专用路由\n\n- 以工具返回为准\n\n## 浏览器任务强制路由\n- 浏览器任务走 browser"

    def test_merges_into_tool_result_and_removes_standalone(self) -> None:
        b = _builder(
            _sec("tool_result", 340, self._RESULT),
            _sec("multimodal_image_policy", 96, self._IMG),
        )
        FlashSectionOrderRail()._merge_image_router(b)
        assert b.get_section("multimodal_image_policy") is None
        text = b.get_section("tool_result").content["cn"]
        assert "## 图片理解路由" in text
        assert self._IMG in text

    def test_full_apply_rebinds_router_to_h3(self) -> None:
        b = _builder(
            _sec("tool_result", 340, self._RESULT),
            _sec("multimodal_image_policy", 96, self._IMG),
        )
        FlashSectionOrderRail().apply(b)
        assert b.get_section("multimodal_image_policy") is None
        text = b.get_section("tool_result").content["cn"]
        # 340 段顶 H1→H2，图片路由 ##→###
        assert "## 工具结果与专用路由" in text
        assert "### 图片理解路由" in text
        assert "### 浏览器任务强制路由" in text

    def test_guard_keeps_standalone_when_target_missing(self) -> None:
        b = _builder(_sec("multimodal_image_policy", 96, self._IMG))
        FlashSectionOrderRail()._merge_image_router(b)
        assert b.get_section("multimodal_image_policy") is not None

    def test_idempotent_when_already_merged(self) -> None:
        b = _builder(
            _sec("tool_result", 340, self._RESULT),
            _sec("multimodal_image_policy", 96, self._IMG),
        )
        rail = FlashSectionOrderRail()
        rail._merge_image_router(b)
        once = b.get_section("tool_result").content["cn"]
        # 再放入一个图片段重放（模拟跨轮）：目标已含标记 → 不重复追加，且新游离段被删除。
        b.add_section(_sec("multimodal_image_policy", 96, self._IMG))
        rail._merge_image_router(b)
        assert b.get_section("tool_result").content["cn"] == once
        assert b.get_section("multimodal_image_policy") is None
