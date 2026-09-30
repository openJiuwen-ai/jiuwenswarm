# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""测试检测模块单元测试。

验证 BlankDataModeler.build_model 透传、BlankThreatAnalyzer.analyze
返回无风险报告。
"""

from __future__ import annotations

import pytest

from agent_ssas.core.framework.data_modeler.plugins.blank_data_modeler import (
    BlankDataModeler,
)
from agent_ssas.core.framework.threat_analyzer.plugins.blank_threat_analyzer import (
    BlankThreatAnalyzer,
)


class TestTestDetection:
    """测试检测模块(空白插件)。"""

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level1
    async def test_blank_modeler_passes_through() -> None:
        """验证 BlankDataModeler 直接透传事件描述 json。"""
        modeler = BlankDataModeler()
        event_desc = {"event_type": "tool_input", "content": "test"}
        model = await modeler.build_model(event_desc)
        assert model == event_desc

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level1
    async def test_blank_modeler_invalid_type_raises() -> None:
        """验证 BlankDataModeler 收到非 dict 时抛出 TypeError。"""
        modeler = BlankDataModeler()
        with pytest.raises(TypeError):
            await modeler.build_model("not a dict")  # type: ignore[arg-type]

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level1
    async def test_blank_analyzer_returns_safe() -> None:
        """验证 BlankThreatAnalyzer 返回无风险报告。"""
        analyzer = BlankThreatAnalyzer()
        report = await analyzer.analyze({})
        assert report["has_risk"] is False
        assert report["risk_level"] == "safe"
        assert report["risk_type"] == ""
        assert report["risk_score"] == 0.0
        assert report["confidence"] == 1.0
        assert report["detected_threats"] == []
        assert report["module_name"] == ""  # 框架预置插件返回空,由流水线注入检测模块名
        assert report["analytic_name"] == "Pass Through Scan"
        assert report["description"] == "No risk detected"
        assert report["evidence"] == {}

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level1
    async def test_blank_analyzer_ignores_model_data() -> None:
        """验证 BlankThreatAnalyzer 对任意输入都返回无风险。"""
        analyzer = BlankThreatAnalyzer()
        # 传入任意类型,都应返回无风险
        report = await analyzer.analyze({"any": "data"})
        assert report["has_risk"] is False

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level1
    def test_model_type_matches() -> None:
        """验证 BlankDataModeler.model_type 与 BlankThreatAnalyzer.expected_model_type 匹配。"""
        assert BlankDataModeler.model_type == "blank"
        assert BlankThreatAnalyzer.expected_model_type == "blank"
        assert BlankDataModeler.model_type == BlankThreatAnalyzer.expected_model_type
