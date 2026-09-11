# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""订阅模式(notify/auth)单元测试。

验证订阅模式解析、聚合事件展开规则、引用计数、
get_subscribers_with_mode 返回正确 (module_name, mode) 列表、
has_subscribers 判断。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_ssas.core.framework.config.settings import AgentSSASConfig
from agent_ssas.core.framework.module_manager import DetectionModuleManager


class TestSubscriptionParsing:
    """订阅模式解析(notify 默认、auth 后缀)。"""

    @staticmethod
    def _make_manager() -> DetectionModuleManager:
        """构造一个空的 DetectionModuleManager(不加载模块)。"""
        return DetectionModuleManager(AgentSSASConfig())

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level0
    def test_default_mode_is_notify() -> None:
        """验证无后缀的订阅默认为 notify 模式。"""
        mgr = TestSubscriptionParsing._make_manager()
        mgr._register_subscription(
            "mod_a", "tool_input"
        )
        subs = mgr.get_subscribers_with_mode("tool_input")
        assert ("mod_a", "notify") in subs

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level0
    def test_auth_suffix_parsed() -> None:
        """验证 :auth 后缀解析为 auth 模式。"""
        mgr = TestSubscriptionParsing._make_manager()
        mgr._register_subscription(
            "mod_a", "tool_input:auth"
        )
        subs = mgr.get_subscribers_with_mode("tool_input")
        assert ("mod_a", "auth") in subs

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level1
    def test_mixed_modes_same_event() -> None:
        """验证同一事件类型可被不同模块以不同模式订阅。"""
        mgr = TestSubscriptionParsing._make_manager()
        mgr._register_subscription("mod_a", "tool_input")
        mgr._register_subscription("mod_b", "tool_input:auth")
        subs = mgr.get_subscribers_with_mode("tool_input")
        assert ("mod_a", "notify") in subs
        assert ("mod_b", "auth") in subs


class TestAggregateEventExpansion:
    """聚合事件订阅展开规则。"""

    @staticmethod
    def _make_manager() -> DetectionModuleManager:
        return DetectionModuleManager(AgentSSASConfig())

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level0
    def test_one_toolcall_event_notify_expansion() -> None:
        """验证 one_toolcall_event (notify) 展开为 tool_input:notify + tool_output:notify。"""
        mgr = TestAggregateEventExpansion._make_manager()
        mgr._register_subscription("mod_a", "one_toolcall_event")
        subs_input = mgr.get_subscribers_with_mode("tool_input")
        subs_output = mgr.get_subscribers_with_mode("tool_output")
        assert ("mod_a", "notify") in subs_input
        assert ("mod_a", "notify") in subs_output

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level0
    def test_one_toolcall_event_auth_expansion() -> None:
        """验证 one_toolcall_event:auth 展开为 tool_input:notify + tool_output:auth。"""
        mgr = TestAggregateEventExpansion._make_manager()
        mgr._register_subscription("mod_a", "one_toolcall_event:auth")
        subs_input = mgr.get_subscribers_with_mode("tool_input")
        subs_output = mgr.get_subscribers_with_mode("tool_output")
        # 起始事件为 notify
        assert ("mod_a", "notify") in subs_input
        # 结束事件为 auth
        assert ("mod_a", "auth") in subs_output

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level0
    def test_one_llmcall_event_auth_expansion() -> None:
        """验证 one_llmcall_event:auth 展开:llm_output 为 auth,其余为 notify。"""
        mgr = TestAggregateEventExpansion._make_manager()
        mgr._register_subscription("mod_a", "one_llmcall_event:auth")
        subs_llm_input = mgr.get_subscribers_with_mode("llm_input")
        subs_llm_output = mgr.get_subscribers_with_mode("llm_output")
        subs_tool_input = mgr.get_subscribers_with_mode("tool_input")
        subs_tool_output = mgr.get_subscribers_with_mode("tool_output")
        # 起始和中间事件为 notify
        assert ("mod_a", "notify") in subs_llm_input
        assert ("mod_a", "notify") in subs_tool_input
        assert ("mod_a", "notify") in subs_tool_output
        # 结束事件为 auth
        assert ("mod_a", "auth") in subs_llm_output

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level0
    def test_one_interaction_event_auth_expansion() -> None:
        """验证 one_interaction_event:auth 展开:invoke_end 为 auth,其余为 notify。"""
        mgr = TestAggregateEventExpansion._make_manager()
        mgr._register_subscription("mod_a", "one_interaction_event:auth")
        subs_invoke_start = mgr.get_subscribers_with_mode("invoke_start")
        subs_invoke_end = mgr.get_subscribers_with_mode("invoke_end")
        subs_llm_input = mgr.get_subscribers_with_mode("llm_input")
        # 起始和中间事件为 notify
        assert ("mod_a", "notify") in subs_invoke_start
        assert ("mod_a", "notify") in subs_llm_input
        # 结束事件为 auth
        assert ("mod_a", "auth") in subs_invoke_end


class TestReferenceCounting:
    """引用计数。"""

    @staticmethod
    def _make_manager() -> DetectionModuleManager:
        return DetectionModuleManager(AgentSSASConfig())

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level0
    def test_has_subscribers_after_register() -> None:
        """验证注册订阅后 has_subscribers 返回 True。"""
        mgr = TestReferenceCounting._make_manager()
        assert mgr.has_subscribers("tool_input") is False
        mgr._register_subscription("mod_a", "tool_input")
        assert mgr.has_subscribers("tool_input") is True

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level0
    def test_aggregate_subscription_raises_base_count() -> None:
        """验证聚合事件订阅提升关联基础事件的引用计数。"""
        mgr = TestReferenceCounting._make_manager()
        # 订阅 one_toolcall_event 应提升 tool_input 和 tool_output 的引用计数
        mgr._register_subscription("mod_a", "one_toolcall_event")
        assert mgr.has_subscribers("tool_input") is True
        assert mgr.has_subscribers("tool_output") is True

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level1
    def test_wildcard_subscription_has_subscribers() -> None:
        """验证通配符 * 订阅使所有事件类型 has_subscribers 返回 True。"""
        mgr = TestReferenceCounting._make_manager()
        mgr._register_subscription("mod_a", "*")
        assert mgr.has_subscribers("tool_input") is True
        assert mgr.has_subscribers("any_event_type") is True

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level1
    def test_no_subscribers_returns_false() -> None:
        """验证未订阅的事件类型 has_subscribers 返回 False。"""
        mgr = TestReferenceCounting._make_manager()
        assert mgr.has_subscribers("nonexistent_event") is False


class TestGetSubscribersWithMode:
    """get_subscribers_with_mode 返回值验证。"""

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level0
    async def test_real_modules_load_with_modes(ssas_home: Path) -> None:
        """验证真实检测模块加载后 get_subscribers_with_mode 返回正确模式。

        test_detection 默认 disabled,通过 config 覆盖启用。
        test_detection 订阅 *(notify),security_rail_detection 订阅
        permission_interrupt_tool(notify),agent_moss 订阅
        one_interaction_event + invoke_start + invoke_end(展开后 notify)。
        """
        config = AgentSSASConfig(
            ssas_home=str(ssas_home),
            modules={"test_detection": {"enabled": True}},
        )
        manager = DetectionModuleManager(config)
        await manager.initialize()

        # test_detection 通配订阅(notify)
        subs_tool = manager.get_subscribers_with_mode("tool_input")
        assert ("test_detection", "notify") in subs_tool

        # security_rail_detection 订阅 permission_interrupt_tool(notify 模式)
        subs_risk = manager.get_subscribers_with_mode("permission_interrupt_tool")
        assert ("security_rail_detection", "notify") in subs_risk

        # agent_moss 订阅 invoke_start + invoke_end + one_interaction_event
        # one_interaction_event 展开后包含 invoke_start 和 invoke_end(notify)
        subs_invoke = manager.get_subscribers_with_mode("invoke_start")
        assert ("agent_moss", "notify") in subs_invoke

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level1
    async def test_get_subscribers_compat_returns_names(ssas_home: Path) -> None:
        """验证兼容方法 get_subscribers 返回模块名列表。"""
        config = AgentSSASConfig(
            ssas_home=str(ssas_home),
            modules={"test_detection": {"enabled": True}},
        )
        manager = DetectionModuleManager(config)
        await manager.initialize()
        subs = manager.get_subscribers("tool_input")
        assert "test_detection" in subs

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level1
    def test_dedup_preserves_order() -> None:
        """验证同一模块以不同模式订阅同一事件时去重保序。"""
        mgr = DetectionModuleManager(AgentSSASConfig())
        mgr._register_subscription("mod_a", "tool_input:auth")
        mgr._register_subscription("mod_b", "tool_input")
        mgr._register_subscription("mod_a", "tool_input")
        subs = mgr.get_subscribers_with_mode("tool_input")
        # mod_a 首次出现是 auth(先注册),不重复
        assert subs[0] == ("mod_a", "auth")
        assert subs[1] == ("mod_b", "notify")
        assert len(subs) == 2
