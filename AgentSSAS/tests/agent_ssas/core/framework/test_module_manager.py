# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""检测模块管理器单元测试。

验证 initialize 加载所有模块、get_subscribers 返回正确订阅列表、
通配符 * 订阅、disabled 模块不加载。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_ssas.core.framework.config.settings import AgentSSASConfig
from agent_ssas.core.framework.module_manager import DetectionModuleManager


class TestDetectionModuleManager:
    """检测模块管理器。"""

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level0
    async def test_initialize_loads_all_modules(ssas_home: Path) -> None:
        """验证 initialize 加载所有已启用的检测模块。

        test_detection 默认 disabled,security_rail_detection、agent_moss 默认 enabled。
        通过 config.modules 可覆盖 test_detection 为 enabled。
        """
        # test_detection 默认 disabled,不加载
        config = AgentSSASConfig(ssas_home=str(ssas_home))
        manager = DetectionModuleManager(config)
        await manager.initialize()
        # test_detection 默认未启用
        assert manager.get_module("test_detection") is None
        # security_rail_detection、agent_moss 默认启用
        assert manager.get_module("security_rail_detection") is not None
        assert manager.get_module("agent_moss") is not None
        agent_moss = manager.get_module("agent_moss")
        assert agent_moss is not None
        assert agent_moss.config["analytic_type_id"] == 2
        assert agent_moss.config["analyzer"]["config"]["analysis_methods"] == [
            "rule",
            "behavior_chain",
            "pdg",
        ]

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level0
    async def test_config_override_enables_module(ssas_home: Path) -> None:
        """验证 config.yaml 的 ssas.modules 段可覆盖 module.yaml 的 enabled 字段。"""
        config = AgentSSASConfig(
            ssas_home=str(ssas_home),
            modules={"test_detection": {"enabled": True}},
        )
        manager = DetectionModuleManager(config)
        await manager.initialize()
        # test_detection 通过 config 覆盖启用
        assert manager.get_module("test_detection") is not None

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level0
    async def test_get_subscribers_for_specific_event(
        ssas_home: Path,
    ) -> None:
        """验证 get_subscribers 返回订阅特定事件类型的模块。

        security_rail_detection 订阅 permission_interrupt_tool 事件。
        """
        config = AgentSSASConfig(ssas_home=str(ssas_home))
        manager = DetectionModuleManager(config)
        await manager.initialize()
        subs = manager.get_subscribers("permission_interrupt_tool")
        assert "security_rail_detection" in subs

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level0
    async def test_wildcard_subscription(ssas_home: Path) -> None:
        """验证通配符 * 订阅匹配所有事件。

        test_detection 模块订阅 "*",应对所有事件类型返回。
        test_detection 默认 disabled,通过 config 覆盖启用。
        """
        config = AgentSSASConfig(
            ssas_home=str(ssas_home),
            modules={"test_detection": {"enabled": True}},
        )
        manager = DetectionModuleManager(config)
        await manager.initialize()
        # 对任意事件类型查询,test_detection 都应在结果中
        subs = manager.get_subscribers("tool_input")
        assert "test_detection" in subs
        subs = manager.get_subscribers("llm_output")
        assert "test_detection" in subs
        subs = manager.get_subscribers("invoke_start")
        # invoke_start 被 agent_moss 显式订阅,test_detection 通配订阅
        assert "test_detection" in subs
        assert "agent_moss" in subs

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level1
    async def test_get_subscribers_no_match(ssas_home: Path) -> None:
        """验证无订阅者时返回空列表(仅通配订阅者出现)。"""
        config = AgentSSASConfig(
            ssas_home=str(ssas_home),
            modules={"test_detection": {"enabled": True}},
        )
        manager = DetectionModuleManager(config)
        await manager.initialize()
        # 未知事件类型:只有通配 * 订阅的 test_detection 出现
        subs = manager.get_subscribers("nonexistent_event_type")
        assert "test_detection" in subs
        # 显式订阅该事件的其他模块不存在
        assert "security_rail_detection" not in subs

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level1
    async def test_get_subscribers_dedup(ssas_home: Path) -> None:
        """验证订阅列表去重,同一模块不重复出现。"""
        config = AgentSSASConfig(
            ssas_home=str(ssas_home),
            modules={"test_detection": {"enabled": True}},
        )
        manager = DetectionModuleManager(config)
        await manager.initialize()
        subs = manager.get_subscribers("invoke_start")
        # test_detection 既通配订阅又不应重复出现
        assert subs.count("test_detection") == 1

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level1
    async def test_disabled_module_not_loaded(ssas_home: Path) -> None:
        """验证 disabled 模块不加载。

        test_detection 默认 enabled: false,验证其未被加载。
        security_rail_detection 和 agent_moss 默认 enabled: true,应被加载。
        """
        config = AgentSSASConfig(ssas_home=str(ssas_home))
        manager = DetectionModuleManager(config)
        await manager.initialize()
        # test_detection 默认 disabled,不应被加载
        assert manager.get_module("test_detection") is None
        # security_rail_detection 和 agent_moss 默认 enabled,应被加载
        assert manager.get_module("security_rail_detection") is not None
        assert manager.get_module("agent_moss") is not None

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level1
    async def test_get_storage_returns_manager(ssas_home: Path) -> None:
        """验证 get_storage 返回模块的存储管理器。"""
        config = AgentSSASConfig(
            ssas_home=str(ssas_home),
            modules={"test_detection": {"enabled": True}},
        )
        manager = DetectionModuleManager(config)
        await manager.initialize()
        storage = manager.get_storage("test_detection")
        assert storage is not None
        assert storage.process_store is not None
        assert storage.result_store is not None
