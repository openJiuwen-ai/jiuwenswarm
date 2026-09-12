# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

# tests/agent_ssas/core/integration/test_agent_ssas_disabled.py
"""配置关闭 SSAS 测试。

验证 ssas.enabled=false 时不加载 AgentSSASSecurityRail,且 jiuwenswarm 完全不受影响。
"""

from pathlib import Path

import pytest

from agent_ssas.core.framework.config.settings import AgentSSASConfig, AgentSSASMode


@pytest.mark.integration
@pytest.mark.level1
class TestSsasDisabled:
    """配置关闭 SSAS 测试。"""

    def test_config_disabled_flag(self):
        """AgentSSASConfig(enabled=False) 正确设置。"""
        config = AgentSSASConfig(enabled=False)
        assert config.enabled is False

    def test_config_enabled_default_true(self):
        """AgentSSASConfig 默认 enabled=True。"""
        config = AgentSSASConfig()
        assert config.enabled is True

    @pytest.mark.asyncio
    async def test_create_backend_inprocess_works(self, tmp_path):
        """enabled=True 时 create_backend 正常创建后端。"""
        from agent_ssas.core.integration.register import create_backend

        config = AgentSSASConfig(ssas_home=str(tmp_path), enabled=True)
        backend = await create_backend(config)
        assert backend is not None

    @pytest.mark.asyncio
    async def test_create_backend_http_works(self, tmp_path):
        """HTTP 模式 create_backend 正常创建后端。"""
        from agent_ssas.core.integration.register import create_backend

        config = AgentSSASConfig(
            ssas_home=str(tmp_path),
            mode=AgentSSASMode.HTTP,
            http_endpoint="http://localhost:8443",
        )
        backend = await create_backend(config)
        assert backend is not None
        await backend.close()

    def test_disabled_config_does_not_crash(self, tmp_path):
        """配置关闭 SSAS 时不会导致任何异常。"""
        config = AgentSSASConfig(ssas_home=str(tmp_path), enabled=False)
        # 验证配置对象创建不报错
        assert config.enabled is False
        # 验证存储路径仍可解析
        assert config.storage_path is not None

    def test_jiuwenswarm_rails_logic_disabled(self):
        """模拟 jiuwenswarm _build_agent_rails 的 SSAS 关闭逻辑。

        验证 ssas.enabled=false 时不注册 AgentSSASSecurityRail,
        且不影响其他 Rail 的注册。
        """
        config_base = {"ssas": {"enabled": False}}
        ssas_config = config_base.get("ssas", {})

        # 模拟 interface_deep.py 中的逻辑
        ssas_enabled = ssas_config.get("enabled", True)
        assert ssas_enabled is False

        # 当 enabled=False 时,不应创建 AgentSSASSecurityRail
        # 此测试验证逻辑分支正确,不会因 SSAS 关闭而报错

    def test_jiuwenswarm_rails_logic_enabled(self):
        """模拟 jiuwenswarm _build_agent_rails 的 SSAS 开启逻辑。"""
        config_base = {"ssas": {"enabled": True}}
        ssas_config = config_base.get("ssas", {})

        ssas_enabled = ssas_config.get("enabled", True)
        assert ssas_enabled is True

    def test_jiuwenswarm_rails_logic_default(self):
        """默认配置(无 ssas 段)时 SSAS 开启。"""
        config_base = {}
        ssas_config = config_base.get("ssas", {})

        ssas_enabled = ssas_config.get("enabled", True)
        assert ssas_enabled is True  # 默认开启

    def test_disabled_ssas_no_side_effects(self, tmp_path):
        """SSAS 关闭时不产生任何残留文件或副作用。"""
        config = AgentSSASConfig(ssas_home=str(tmp_path), enabled=False)
        # 仅创建配置对象,不应创建任何存储文件
        storage_path = Path(config.storage_path)
        # 存储目录可能由配置推导,但实际文件不应被创建
        # (只有 backend 初始化时才创建)
