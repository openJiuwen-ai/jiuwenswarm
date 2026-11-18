# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""FlashPromptAuditRail 单元测试 + 静态区 hash 基线机制（M0 S1+S3）。

覆盖：
1. 撞号（同 priority 两段）/ 重复段名 / 未渲染变量 / 幽灵工具 / 未登记段号；
2. 静态区 hash 的确定性与漂移检测（会话内 hash 不变的机制验证，S1 快照基线）；
3. fail_closed 分级：运行期只告警、CI 期断言拦截；
4. 已知安全占位符（510 段 JSON schema 示例）不误报。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from openjiuwen.core.single_agent.prompts.builder import PromptSection

from jiuwenswarm.agents.harness.flash.rails.flash_prompt_audit_rail import (
    AuditReport,
    AuditViolation,
    FlashPromptAuditRail,
)
from jiuwenswarm.agents.harness.flash.sections import (
    _SAME_ORDER_GROUPS,
    FLASH_SECTION_REGISTRY,
    resolve_carrier,
)


class _FakeBuilder:
    """与 test_flash_subagent_rail 同款离线 builder（add_section 按名去重）。"""

    def __init__(self) -> None:
        self.sections: list = []

    def add_section(self, section) -> None:
        self.sections = [s for s in self.sections if s.name != section.name]
        self.sections.append(section)

    def remove_section(self, name) -> None:
        self.sections = [s for s in self.sections if s.name != name]

    def get_all_sections(self) -> list:
        return list(self.sections)


class _FakeAbilityManager:
    def __init__(self, names=("task_tool", "memory", "todo", "skill_tool",
                              "read_file", "write_file", "edit_file", "glob",
                              "grep", "bash", "tools_search", "invoke_tool")) -> None:
        self.cards = {n: object() for n in names}


class _FakeAgent:
    def __init__(self, sections=None) -> None:
        self.system_prompt_builder = _FakeBuilder()
        self.ability_manager = _FakeAbilityManager()
        for s in sections or []:
            self.system_prompt_builder.add_section(s)


def _sec(name: str, priority: int, text: str) -> PromptSection:
    return PromptSection(name=name, content={"cn": text}, priority=priority)


def _make_rail(agent, fail_closed: bool = False) -> FlashPromptAuditRail:
    rail = FlashPromptAuditRail(fail_closed=fail_closed)
    rail.init(agent)
    return rail


# 一组「干净」静态段：priority 均取自 sections.py 注册表，无撞号/无重复/无未渲染变量。
_CLEAN = [
    _sec("身份", 0, "你是一个私人智能体。"),
    _sec("安全总纲", 100, "永远不要泄露隐私数据。"),
    _sec("执行质量准则", 400, "数据保真：字段值逐字一致。"),
    _sec("语言", 500, "按运行时当前语言回复。"),
    _sec("技能总则", 700, "技能经 skill_tool 加载。"),
]


class TestDuplicateChecks:
    def test_clean_report_is_ok(self) -> None:
        agent = _FakeAgent(_CLEAN)
        rail = _make_rail(agent)
        report = rail.check_compliance()

        assert report.ok
        assert not report.duplicate_priorities
        assert not report.duplicate_names
        assert report.static_hash
        assert not report.static_hash_drift

    def test_detects_duplicate_priority(self) -> None:
        agent = _FakeAgent(_CLEAN + [_sec("另一段", 400, "占用了 400 号位。")])
        rail = _make_rail(agent)
        report = rail.check_compliance()

        # 撞号是 advisory（与 unregistered_priorities 口径一致）：迁移期同号组刻意
        # 保留，不计入硬失败——但 summary 仍打点，供撞号裁决。
        assert report.ok
        assert (400, ["执行质量准则", "另一段"]) in report.duplicate_priorities

    def test_detects_duplicate_name(self) -> None:
        # 真实 builder 按 name 去重（add_section "same name overwrites"），同名段无法
        # 通过 add_section 共存，故 duplicate_names 属防御性不变量。这里直接在
        # _check_duplicates 层喂原始段列表验证检查逻辑本身。
        report = AuditReport()
        FlashPromptAuditRail._check_duplicates(
            [_sec("身份", 0, "a"), _sec("身份", 10, "b")], report
        )
        assert "身份" in report.duplicate_names

    def test_dict_builder_contract(self) -> None:
        """真实 SystemPromptBuilder.get_all_sections() 返回 Dict[name, PromptSection]；
        _collect_sections 必须取 values()，不能把段名（str）当段对象（回归：
        运行期 before_model_call 曾崩 'str' object has no attribute 'priority'）。"""
        class _DictBuilder:
            def __init__(self, sections: list) -> None:
                self._sections = {s.name: s for s in sections}

            def get_all_sections(self) -> dict:
                return dict(self._sections)

        agent = _FakeAgent(_CLEAN)
        agent.system_prompt_builder = _DictBuilder(_CLEAN)  # type: ignore[assignment]
        report = _make_rail(agent).check_compliance()
        assert report.ok
        assert len(report.sections) == len(_CLEAN)


class TestUnrenderedVars:
    def test_detects_unrendered_placeholder(self) -> None:
        agent = _FakeAgent(_CLEAN + [_sec("幽灵段", 420, "请参考 {unknown_placeholder}。")])
        report = _make_rail(agent).check_compliance()

        assert not report.ok
        assert ("幽灵段", "{unknown_placeholder}") in report.unrendered_vars

    def test_known_safe_placeholder_not_flagged(self) -> None:
        # 510 段 JSON schema 示例占位符：合法保留。
        agent = _FakeAgent(_CLEAN + [
            _sec("输入消息约定", 510, "用户消息含 {channel} / {preferred_response_language}。"),
        ])
        report = _make_rail(agent).check_compliance()

        assert not report.unrendered_vars, report.unrendered_vars

    def test_regex_quantifier_not_flagged(self) -> None:
        # 评审 #4：正则量词 {6} 不应被当作未渲染变量（只识别标识符形态的模板变量）。
        agent = _FakeAgent(_CLEAN + [_sec("输入校验", 520, "匹配 ^[0-9]{6}$ 即可。")])
        report = _make_rail(agent).check_compliance()

        assert not report.unrendered_vars, report.unrendered_vars

    def test_dynamic_section_not_scanned(self) -> None:
        # 评审 #4：动态/relay 段（attachment）不扫未渲染变量——request_system_prompt 可
        # 合法携带正则/JSON 等含 {} 的正文。
        agent = _FakeAgent(_CLEAN + [_sec("request_system_prompt", 999, "正则 {6} 合法。")])
        report = _make_rail(agent).check_compliance()

        assert not report.unrendered_vars, report.unrendered_vars


class TestStaticHashBaseline:
    def test_report_exposes_section_and_band_snapshot_hashes(self) -> None:
        """M0 S1：审计报告同时暴露段级、区级和完整快照 hash。"""
        report = _make_rail(_FakeAgent(_CLEAN)).check_compliance()

        assert report.snapshot_full_hash
        assert report.snapshot_static_hash
        assert report.snapshot_prefix_hash
        assert report.snapshot_prefix_chars > 0
        assert {name for _priority, name, _digest in report.section_hashes} == {
            "身份",
            "安全总纲",
            "执行质量准则",
            "语言",
            "技能总则",
        }
        assert set(report.band_hashes) == {
            "identity",
            "security",
            "execution",
            "interaction",
            "skills",
        }

    def test_hash_deterministic(self) -> None:
        """同一组段 → 同一 hash（S1 快照基线的确定性前提）。"""
        h1 = _make_rail(_FakeAgent(_CLEAN)).check_compliance().static_hash
        h2 = _make_rail(_FakeAgent(_CLEAN)).check_compliance().static_hash
        assert h1 == h2
        assert len(h1) == 64  # sha256 hex

    def test_hash_ignores_dynamic_region(self) -> None:
        """10xx（priority ≥ 1000）动态段不进入静态区 hash。"""
        agent_static = _FakeAgent(_CLEAN)
        agent_with_dyn = _FakeAgent(_CLEAN + [_sec("时间事实", 1020, "当前日期：今天。")])
        h_static = _make_rail(agent_static).check_compliance().static_hash
        h_dyn = _make_rail(agent_with_dyn).check_compliance().static_hash
        assert h_static == h_dyn

    def test_hash_ignores_unregistered_dynamic_priority(self) -> None:
        """未登记段号（如 daily_memory_context@52 / runtime env）是 Deep/运行时注入的
        动态段，不应计入静态区 hash——即便其 priority < 800。"""
        agent_static = _FakeAgent(_CLEAN)
        agent_with_dyn = _FakeAgent(_CLEAN + [_sec("每日记忆上下文", 52, "今日记忆正文……")])
        h_static = _make_rail(agent_static).check_compliance().static_hash
        h_dyn = _make_rail(agent_with_dyn).check_compliance().static_hash
        assert h_static == h_dyn

    @pytest.mark.asyncio
    async def test_drift_detected_after_seed(self) -> None:
        """首轮 before_model_call 记基线，静态区改动后 hash 漂移被检出。"""
        agent = _FakeAgent(_CLEAN)
        rail = _make_rail(agent)
        await rail.before_model_call(SimpleNamespace())  # 记 seed

        # 改动静态区：新增一个静态段
        agent.system_prompt_builder.add_section(_sec("新准则", 460, "新增的静态段。"))
        report = rail.check_compliance()

        assert report.static_hash_drift

    @pytest.mark.asyncio
    async def test_no_drift_when_unchanged(self) -> None:
        agent = _FakeAgent(_CLEAN)
        rail = _make_rail(agent)
        await rail.before_model_call(SimpleNamespace())
        report = rail.check_compliance()
        assert not report.static_hash_drift

    @pytest.mark.asyncio
    async def test_before_model_call_refreshes_builder_from_ctx(self) -> None:
        """热重载重建 system_prompt_builder 后，audit 每轮从 ctx 取活 builder 再审计。"""
        agent = _FakeAgent(_CLEAN)
        rail = _make_rail(agent)
        assert rail.system_prompt_builder is agent.system_prompt_builder

        # 热重载：新 agent 带新 builder（内容不同，模拟重建）。
        rebuilt = _FakeAgent(_CLEAN + [_sec("新准则", 460, "新增的静态段。")])
        await rail.before_model_call(SimpleNamespace(agent=rebuilt))

        assert rail.system_prompt_builder is rebuilt.system_prompt_builder


class TestResolveCarrierNameAware:
    """110 撞号：relay ``request_system_prompt`` 与 flash ``refusal_behavior`` 同占 110。

    ``resolve_carrier(priority, name)`` 必须按段名把 ``request_system_prompt`` 归
    ``attachment``（归属未决、非 flash 静态区），即便其段号 110 恰好命中注册表里
    ``refusal_behavior``（carrier=system）。
    """

    def test_registered_static_section_resolves_system(self) -> None:
        assert resolve_carrier(110, "refusal_behavior") == "system"

    def test_pending_adjudication_name_overrides_priority(self) -> None:
        assert resolve_carrier(110, "request_system_prompt") == "attachment"

    def test_priority_only_lookup_still_system(self) -> None:
        # 不带段名的旧调用仍按 priority 命中注册表（system）。
        assert resolve_carrier(110) == "system"

    def test_request_system_prompt_not_in_static_hash(self) -> None:
        """relay 产品内容包（request_system_prompt@110）不计入静态区 hash。

        前缀按实际渲染顺序在首个动态段处截断（评审 #7）：request_system_prompt@110
        是动态段、插在 100 与 400 之间，故 with_relay 的前缀只含其前的 0/100 两段，
        后面的 400/500/700 不再计入。
        """
        base = _FakeAgent(_CLEAN)
        with_relay = _FakeAgent(
            _CLEAN + [_sec("request_system_prompt", 110, "relay 产品内容包正文……")]
        )
        h_base = _make_rail(base).check_compliance().static_hash
        h_relay = _make_rail(with_relay).check_compliance().static_hash
        assert h_base == h_relay
        report = _make_rail(with_relay).check_compliance()
        leading = _FakeAgent([_CLEAN[0], _CLEAN[1]])  # 身份@0 + 安全总纲@100
        assert report.prefix_hash == _make_rail(leading).check_compliance().prefix_hash

    def test_tools_catalog_not_in_static_hash(self) -> None:
        """评审 #5：工具目录（tools@310，carrier=attachment）不入静态 hash。

        工具目录随 enabled_tools / 渐进加载每请求动态；若计入静态 hash，禁用工具会让
        基线永久漂移、fail_closed 中断后续轮次。
        """
        assert resolve_carrier(310, "tools") == "attachment"
        base = _FakeAgent(_CLEAN)
        with_tools = _FakeAgent(
            _CLEAN + [_sec("tools", 310, "# 可用工具\n- read_file\n- write_file")]
        )
        h_base = _make_rail(base).check_compliance().static_hash
        h_tools = _make_rail(with_tools).check_compliance().static_hash
        assert h_base == h_tools

    def test_tool_navigation_not_in_static_hash(self) -> None:
        """评审 #5（本轮）：渐进式工具导航（tool_navigation@350，carrier=attachment）不入静态 hash。

        导航段随按需工具列表每请求变化，与 tools@310 同属动态面。
        """
        assert resolve_carrier(350, "tool_navigation") == "attachment"
        base = _FakeAgent(_CLEAN)
        with_nav = _FakeAgent(
            _CLEAN + [_sec("tool_navigation", 350, "# 工具导航\n- mcp_a\n- mcp_b")]
        )
        h_base = _make_rail(base).check_compliance().static_hash
        h_nav = _make_rail(with_nav).check_compliance().static_hash
        assert h_base == h_nav

    def test_memory_not_in_static_hash(self) -> None:
        """评审 C2：记忆（memory@600，carrier=durable）不入静态 hash。

        记忆正文随检索 session 级变化；若登记 system，记忆再检索会误判静态漂移。
        """
        assert resolve_carrier(600, "memory") == "durable"
        base = _FakeAgent(_CLEAN)
        with_mem = _FakeAgent(
            _CLEAN + [_sec("memory", 600, "# 记忆\n- 今日记忆正文……")]
        )
        h_base = _make_rail(base).check_compliance().static_hash
        h_mem = _make_rail(with_mem).check_compliance().static_hash
        assert h_base == h_mem


class TestAdvisoryChecks:
    def test_unregistered_priority_reported_not_fatal(self) -> None:
        """未登记段号是 advisory（迁移进度度量），不影响 ok。"""
        agent = _FakeAgent(_CLEAN + [_sec("遗留段", 42, "不在注册表的 priority。")])
        report = _make_rail(agent).check_compliance()

        assert 42 in report.unregistered_priorities
        assert report.ok  # advisory 不计入硬违规

    def test_registry_priorities_all_clean(self) -> None:
        """注册表内的 priority 全都不进 unregistered。"""
        agent = _FakeAgent([_sec(str(p), p, "x") for p in (0, 100, 400, 700, 1020)])
        report = _make_rail(agent).check_compliance()
        assert not report.unregistered_priorities

    def test_ghost_tool_detected(self) -> None:
        """正文提到工具面没有的 `read_memory` → 幽灵工具。"""
        agent = _FakeAgent(_CLEAN + [_sec("记忆", 600, "用 `read_memory` 读旧条目。")])
        report = _make_rail(agent).check_compliance()
        assert "read_memory" in report.ghost_tools

    def test_registered_tool_not_ghost(self) -> None:
        agent = _FakeAgent(_CLEAN + [_sec("记忆", 600, "用 `read_file` 读取文件。")])
        report = _make_rail(agent).check_compliance()
        assert "read_file" not in report.ghost_tools


class TestFailClosed:
    @pytest.mark.asyncio
    async def test_warn_only_by_default(self) -> None:
        """默认 fail_closed=False：有硬违规也不抛，只走告警分支。"""
        # {name} 不在已知安全占位符内 → 未渲染变量，属硬违规。
        agent = _FakeAgent(_CLEAN + [_sec("未渲染测试段", 110, "你好 {name}。")])
        rail = _make_rail(agent, fail_closed=False)
        # 不应抛异常
        await rail.before_model_call(SimpleNamespace())

    @pytest.mark.asyncio
    async def test_fail_closed_raises(self) -> None:
        # {name} 不在已知安全占位符内 → 未渲染变量，属硬违规。
        agent = _FakeAgent(_CLEAN + [_sec("未渲染测试段", 110, "你好 {name}。")])
        rail = _make_rail(agent, fail_closed=True)
        with pytest.raises(AuditViolation):
            await rail.before_model_call(SimpleNamespace())

    @pytest.mark.asyncio
    async def test_fail_closed_passes_on_clean(self) -> None:
        agent = _FakeAgent(_CLEAN)
        rail = _make_rail(agent, fail_closed=True)
        await rail.before_model_call(SimpleNamespace())  # 不应抛

    @pytest.mark.asyncio
    async def test_fail_closed_provider_refreshed_per_round(self) -> None:
        """评审 #6：fail_closed 经 provider 每轮现读，热重载后下一轮即生效。"""
        state = {"fc": False}
        agent = _FakeAgent(_CLEAN + [_sec("未渲染测试段", 110, "你好 {name}。")])
        rail = FlashPromptAuditRail(fail_closed_provider=lambda: state["fc"])
        rail.init(agent)
        await rail.before_model_call(SimpleNamespace(agent=agent))  # fc=False → 不抛

        state["fc"] = True
        with pytest.raises(AuditViolation):
            await rail.before_model_call(SimpleNamespace(agent=agent))  # fc=True → 抛

    @pytest.mark.asyncio
    async def test_config_fingerprint_resets_baseline(self) -> None:
        """评审 #2：配置指纹变化（合法配置变更）重置静态 hash 基线；同版本内异常漂移仍拦截。"""
        state = {"fp": "v1"}
        agent = _FakeAgent(_CLEAN)
        rail = FlashPromptAuditRail(
            fail_closed=True,
            config_fingerprint_provider=lambda: state["fp"],
        )
        rail.init(agent)

        # 第 1 轮：记基线（干净）。
        await rail.before_model_call(SimpleNamespace(agent=agent))

        # 第 2 轮：指纹变（合法配置变更）+ 静态内容变 → 重置基线，不抛。
        state["fp"] = "v2"
        agent.system_prompt_builder.add_section(_sec("新准则", 460, "新增静态段。"))
        await rail.before_model_call(SimpleNamespace(agent=agent))  # 指纹变化 → 不抛

        # 第 3 轮：指纹和内容都不变 → 不抛。
        await rail.before_model_call(SimpleNamespace(agent=agent))

        # 第 4 轮：指纹不变但内容异常变 → 抛 AuditViolation。
        agent.system_prompt_builder.add_section(_sec("异常段", 450, "异常新增。"))
        with pytest.raises(AuditViolation):
            await rail.before_model_call(SimpleNamespace(agent=agent))

    @pytest.mark.asyncio
    async def test_fail_closed_abort_propagates_from_callback_manager(self) -> None:
        """从回调管理器入口验证：AbortError 从 trigger() 传播、不被记录后继续（评审 #7）。"""
        from uuid import uuid4

        from openjiuwen.core.single_agent.agent_callback_manager import AgentCallbackManager
        from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, AgentCallbackEvent

        # {name} 不在已知安全占位符内 → 未渲染变量，属硬违规。
        agent = _FakeAgent(_CLEAN + [_sec("未渲染测试段", 110, "你好 {name}。")])
        rail = FlashPromptAuditRail(fail_closed=True)
        rail.init(agent)

        callbacks = AgentCallbackManager("flash-audit-test-" + uuid4().hex)
        try:
            await callbacks.register_rail(rail, agent)
            ctx = AgentCallbackContext(agent=agent)
            with pytest.raises(AuditViolation):
                await callbacks.execute(AgentCallbackEvent.BEFORE_MODEL_CALL, ctx)
        finally:
            # 全局回调框架是单例：用后清理，避免泄漏到其它用例。
            await callbacks.clear()


class TestRegistryConsistency:
    def test_registry_numbers_unique(self) -> None:
        """目标注册表以段号（= priority）为唯一主键——段号不复用。"""
        assert len(FLASH_SECTION_REGISTRY) == len(set(FLASH_SECTION_REGISTRY))

    def test_same_order_groups_renumbered_out_of_collision(self) -> None:
        """_SAME_ORDER_GROUPS 记录的是存量同号（code_priority 40/45/70/85 = 当前
        StreamEventRail/ContextAssembleRail 等的 priority），目标注册表已重排为
        三位域号（0-850），故这些存量 code_priority 不应再作为目标段号出现。"""
        for group in _SAME_ORDER_GROUPS:
            assert group["code_priority"] not in FLASH_SECTION_REGISTRY, (
                f"存量同号组 {group['code_priority']} 仍占用目标段号，未完成重排"
            )
