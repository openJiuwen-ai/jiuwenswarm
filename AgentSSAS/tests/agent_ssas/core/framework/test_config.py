# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""配置模块单元测试。

验证 AgentSSASConfig 默认值、自定义值、storage_path、from_dict、
_resolve_ssas_home 环境变量优先级。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_ssas.core.framework.config import AgentSSASConfig, AgentSSASMode


class TestAgentSSASConfig:
    """AgentSSASConfig 加载与字段默认值。"""

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level0
    def test_default_values() -> None:
        """验证默认配置值。"""
        c = AgentSSASConfig()
        assert c.enabled is True
        assert c.mode == AgentSSASMode.INPROCESS
        assert c.rail_priority == 80
        assert c.event_ttl_days == 30
        assert c.alert_ttl_days == 90

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level0
    def test_custom_values() -> None:
        """验证自定义字段覆盖默认值。"""
        c = AgentSSASConfig(
            enabled=False,
            mode=AgentSSASMode.HTTP,
            rail_priority=50,
            event_ttl_days=7,
            alert_ttl_days=14,
        )
        assert c.enabled is False
        assert c.mode == AgentSSASMode.HTTP
        assert c.rail_priority == 50
        assert c.event_ttl_days == 7
        assert c.alert_ttl_days == 14

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level0
    def test_storage_path_explicit_home(ssas_home: Path) -> None:
        """验证显式 ssas_home 时 storage_path 正确推导。"""
        c = AgentSSASConfig(ssas_home=str(ssas_home))
        assert c.storage_path == str(ssas_home / "ssas")

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level0
    def test_storage_path_none_resolves_env(ssas_home: Path) -> None:
        """验证 ssas_home 为 None 时从 SSAS_HOME 环境变量解析。"""
        c = AgentSSASConfig()
        # 根 conftest 已设置 SSAS_HOME 环境变量
        assert c.storage_path == str(ssas_home / "ssas")

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level0
    def test_from_dict_known_fields() -> None:
        """验证 from_dict 识别已知字段。"""
        c = AgentSSASConfig.from_dict(
            {
                "enabled": False,
                "mode": "http",
                "rail_priority": 100,
                "event_ttl_days": 60,
            }
        )
        assert c.enabled is False
        assert c.mode == AgentSSASMode.HTTP
        assert c.rail_priority == 100
        assert c.event_ttl_days == 60

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level0
    def test_from_dict_ignores_unknown_fields() -> None:
        """验证 from_dict 忽略未知字段。"""
        c = AgentSSASConfig.from_dict({"enabled": False, "unknown_field": "value"})
        assert c.enabled is False
        # 未知字段不会成为 dataclass 字段

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level0
    def test_from_dict_none_returns_default() -> None:
        """验证 from_dict(None) 返回默认配置。"""
        c = AgentSSASConfig.from_dict(None)
        assert c.enabled is True
        assert c.mode == AgentSSASMode.INPROCESS

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level1
    def test_from_dict_invalid_type_raises() -> None:
        """验证 from_dict 传入非 dict 时抛出 TypeError。"""
        with pytest.raises(TypeError):
            AgentSSASConfig.from_dict("not a dict")  # type: ignore[arg-type]

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level1
    def test_resolve_ssas_home_priority(
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """验证环境变量优先级:SSAS_HOME > JIUWENSWARM_DATA_DIR > JIUWENSWARM_HOME。

        设置三个环境变量后,SSAS_HOME 应优先被选中。
        """
        c = AgentSSASConfig()
        monkeypatch.setenv("SSAS_HOME", "/tmp/ssas_home")
        monkeypatch.setenv("JIUWENSWARM_DATA_DIR", "/tmp/data_dir")
        monkeypatch.setenv("JIUWENSWARM_HOME", "/tmp/jw_home")
        resolved = c._resolve_ssas_home()
        assert resolved == Path("/tmp/ssas_home")

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level1
    def test_resolve_ssas_home_fallback_data_dir(
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """验证无 SSAS_HOME 时回退到 JIUWENSWARM_DATA_DIR。"""
        c = AgentSSASConfig()
        monkeypatch.delenv("SSAS_HOME", raising=False)
        monkeypatch.setenv("JIUWENSWARM_DATA_DIR", "/tmp/data_dir")
        monkeypatch.delenv("JIUWENSWARM_HOME", raising=False)
        resolved = c._resolve_ssas_home()
        assert resolved == Path("/tmp/data_dir")

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level1
    def test_resolve_ssas_home_fallback_jw_home(
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """验证无 SSAS_HOME 和 JIUWENSWARM_DATA_DIR 时回退到 JIUWENSWARM_HOME。"""
        c = AgentSSASConfig()
        monkeypatch.delenv("SSAS_HOME", raising=False)
        monkeypatch.delenv("JIUWENSWARM_DATA_DIR", raising=False)
        monkeypatch.setenv("JIUWENSWARM_HOME", "/tmp/jw_home")
        resolved = c._resolve_ssas_home()
        assert resolved == Path("/tmp/jw_home")
