# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""FlashPersonaRail 单元测试（M3 ① identity 巨石拆分 + 0xx/2xx/4xx 改写）。

锁定：
1. ``_split_identity`` 纯函数：零内容损失（按切分序拼接 == 原内容）、段名/段号正确、
   相邻同段自动合并（workspace_model 三块合一）；
2. 守卫：标题缺失 / 乱序 / 空内容 → 返回 None（整体回退保留原生 identity 段）；
3. ``_maybe_split_identity``：拆分成功后 200/220 改写为 §七 正文、400 改写为通用+场景化准则、
   020 注入六级裁决表、210/420/460 原样保留；幂等（第二次调用不重拆）。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from _flash_test_utils import FakeAgent as _FakeAgent
from _flash_test_utils import FakeCtx as _FakeCtx

from jiuwenswarm.agents.harness.flash.rails.flash_persona_rail import (
    _DIRECTORY_MODEL_CN,
    _EXEC_PRINCIPLES_CN,
    _EXEC_PRIORITY_CN,
    _IDENTITY_BLOCKS_CN,
    _PATH_PERMISSION_CN,
    _WRITE_LOCATION_CN,
    FlashPersonaRail,
    _split_identity,
)

_INTRO = "你是 JiuwenSwarm 智能体。\n\n"
_B0 = "# JiuwenSwarm 内部数据\n内部数据说明\n\n"
_B1 = "## 目录理解规则\n目录规则\n\n"
_B2 = "## JiuwenSwarm 启动配置\n启动配置\n\n"
_B3 = "## 运行环境\n运行环境说明\n\n"
_B4 = "## 任务执行准则\n执行准则\n\n"
_B5 = "## 输出文件放置规范\n放置规范\n\n"
_B6 = "## 产物格式偏好\n格式偏好\n\n"
_B7 = "## 文件发送\n文件发送说明\n"

_IDENTITY_CN = _INTRO + _B0 + _B1 + _B2 + _B3 + _B4 + _B5 + _B6 + _B7


def _h1(text: str) -> str:
    """把 ``## `` 段顶标题提升为 ``# ``（对齐 content.persona._promote_heading）。"""
    return "#" + text[2:] if text.startswith("## ") else text


class TestSplitIdentity:
    def test_names_and_priorities(self) -> None:
        split = _split_identity(_IDENTITY_CN, _IDENTITY_BLOCKS_CN)
        assert split is not None
        assert [n for n, _, _ in split] == [
            "identity", "workspace_model", "runtime_environment",
            "execution_principles", "output_placement", "artifact_format", "file_delivery",
        ]
        assert [p for _, p, _ in split] == [0, 200, 1001, 400, 220, 420, 460]

    def test_zero_content_loss(self) -> None:
        split = _split_identity(_IDENTITY_CN, _IDENTITY_BLOCKS_CN)
        assert split is not None
        # 标题层级归一化只把独立段段顶 ## 提升为 #（200 段内的「目录理解规则」/
        # 「启动配置」是子标题，不动）。把提升过的段顶还原回 ## 后应逐字还原原内容。
        standalone = ("## 运行环境", "## 任务执行准则", "## 输出文件放置规范",
                      "## 产物格式偏好", "## 文件发送")
        expected = _IDENTITY_CN
        for h in standalone:
            expected = expected.replace(h, _h1(h))
        assert "".join(text for _, _, text in split) == expected

    def test_heading_level_normalization(self) -> None:
        split = _split_identity(_IDENTITY_CN, _IDENTITY_BLOCKS_CN)
        byname = {n: t for n, _, t in split}
        # 独立段段顶提升为 H1。
        assert byname["runtime_environment"].startswith("# 运行环境")
        assert byname["artifact_format"].startswith("# 产物格式偏好")
        assert byname["file_delivery"].startswith("# 文件发送")
        # 200 目录模型顶层本就 H1，其段内 ## 子标题保留不动。
        assert byname["workspace_model"].startswith("# JiuwenSwarm 内部数据")
        assert "## 目录理解规则" in byname["workspace_model"]
        assert "## JiuwenSwarm 启动配置" in byname["workspace_model"]

    def test_workspace_model_merged_from_three_blocks(self) -> None:
        split = _split_identity(_IDENTITY_CN, _IDENTITY_BLOCKS_CN)
        byname = {n: t for n, _, t in split}
        assert byname["workspace_model"] == _B0 + _B1 + _B2

    def test_intro_preserved_when_nonempty(self) -> None:
        split = _split_identity(_IDENTITY_CN, _IDENTITY_BLOCKS_CN)
        byname = {n: t for n, _, t in split}
        assert byname["identity"] == _INTRO


class TestSplitGuards:
    def test_missing_header_returns_none(self) -> None:
        # 缺「## 文件发送」标题。
        content = _INTRO + _B0 + _B1 + _B2 + _B3 + _B4 + _B5 + _B6
        assert _split_identity(content, _IDENTITY_BLOCKS_CN) is None

    def test_out_of_order_returns_none(self) -> None:
        # 「## 文件发送」放在最前 → 乱序。
        content = _B7 + _INTRO + _B0 + _B1 + _B2 + _B3 + _B4 + _B5 + _B6
        assert _split_identity(content, _IDENTITY_BLOCKS_CN) is None

    def test_empty_returns_none(self) -> None:
        assert _split_identity("", _IDENTITY_BLOCKS_CN) is None


class TestMaybeSplitIdentity:
    def _run(self, language: str = "cn"):
        section = SimpleNamespace(
            name="identity",
            content={"cn": _IDENTITY_CN, "en": _IDENTITY_CN},
            priority=10,
        )
        agent = _FakeAgent(language, [section])
        rail = FlashPersonaRail()
        rail.init(agent)
        rail._maybe_split_identity(agent.system_prompt_builder)
        return agent, rail

    def test_full_split_and_rewrite(self) -> None:
        agent, _ = self._run("cn")
        b = agent.system_prompt_builder

        assert b.get_section("identity").priority == 0
        assert b.get_section("identity").content["cn"] == "# 身份\n\n" + _INTRO
        assert b.get_section("execution_priority").priority == 20
        assert b.get_section("execution_priority").content["cn"] == _EXEC_PRIORITY_CN
        assert b.get_section("workspace_model").priority == 200
        assert b.get_section("workspace_model").content["cn"] == _DIRECTORY_MODEL_CN
        assert b.get_section("output_placement").priority == 220
        assert b.get_section("output_placement").content["cn"] == _WRITE_LOCATION_CN
        # 210 新增路径与权限规则段；420/460 原样保留（仅段顶归一化 H1）；
        # runtime_environment 由 FlashSectionOrderRail 收口到 1001（persona 不剥）。
        assert b.get_section("path_permission").priority == 210
        assert b.get_section("path_permission").content["cn"] == _PATH_PERMISSION_CN
        assert b.get_section("runtime_environment").content["cn"] == _h1(_B3)
        assert b.get_section("execution_principles").content["cn"] == _EXEC_PRINCIPLES_CN
        assert b.get_section("artifact_format").content["cn"] == _h1(_B6)
        assert b.get_section("file_delivery").content["cn"] == _h1(_B7)

    def test_idempotent_second_call(self) -> None:
        agent, rail = self._run("cn")
        before = dict(agent.system_prompt_builder.sections)
        rail._maybe_split_identity(agent.system_prompt_builder)  # 第二次
        after = dict(agent.system_prompt_builder.sections)
        assert before == after

    def test_resplits_after_builder_rebuild(self) -> None:
        """热重载：同一 rail 实例、新 builder 重新带入完整 identity 巨石段 → 再次拆分。"""
        agent, rail = self._run("cn")
        assert agent.system_prompt_builder.get_section("workspace_model").priority == 200

        # 重建 builder：重新带入完整 identity 巨石段（未拆分状态）。
        section = SimpleNamespace(
            name="identity", content={"cn": _IDENTITY_CN}, priority=10
        )
        rebuilt_agent = _FakeAgent("cn", [section])
        rail.init(rebuilt_agent)
        rail._maybe_split_identity(rebuilt_agent.system_prompt_builder)

        rb = rebuilt_agent.system_prompt_builder
        assert rb.get_section("identity").priority == 0
        assert rb.get_section("workspace_model").priority == 200
        assert rb.get_section("execution_priority").priority == 20
        assert rb.get_section("output_placement").priority == 220

    def test_guard_fallback_keeps_original(self) -> None:
        # 缺「## 文件发送」标题 → 守卫回退，保留原生 identity 段原样。
        content = _INTRO + _B0 + _B1 + _B2 + _B3 + _B4 + _B5 + _B6
        agent = _FakeAgent("cn", [
            SimpleNamespace(name="identity", content={"cn": content}, priority=10)
        ])
        rail = FlashPersonaRail()
        rail.init(agent)
        rail._maybe_split_identity(agent.system_prompt_builder)
        sec = agent.system_prompt_builder.get_section("identity")
        assert sec is not None
        assert sec.priority == 10  # 原段未拆
        assert agent.system_prompt_builder.get_section("workspace_model") is None

    @pytest.mark.asyncio
    async def test_splits_via_before_model_call(self) -> None:
        section = SimpleNamespace(name="identity", content={"cn": _IDENTITY_CN}, priority=10)
        agent = _FakeAgent("cn", [section])
        rail = FlashPersonaRail()
        rail.init(agent)
        await rail.before_model_call(_FakeCtx(agent))
        assert agent.system_prompt_builder.get_section("workspace_model").priority == 200
