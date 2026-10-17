# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""呈现模块单元测试。

验证 render 生成 OCSF Detection Finding 格式文件、
risk_level_to_severity 映射、build_ocsf_report 构建。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_ssas.core.framework.config.settings import AgentSSASConfig
from agent_ssas.core.framework.presentation.ocsf import (
    build_ocsf_report,
    risk_level_to_severity,
)
from agent_ssas.core.framework.presentation.threat_log import AgentSSASThreatLog


class TestAgentSSASThreatLog:
    """威胁日志呈现插件。"""

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level0
    async def test_render_generates_ocsf_file(ssas_config: AgentSSASConfig) -> None:
        """验证 render 生成 OCSF Detection Finding 格式 JSON 文件。"""
        presentation = AgentSSASThreatLog(ssas_config)
        report = {
            "has_risk": True,
            "risk_level": "high",
            "risk_type": "tool_misuse",
            "risk_score": 75.0,
            "confidence": 0.9,
            "detected_threats": ["tool_misuse"],
            "analytic_name": "Tool Permission Denied",
            "analytic_type_id": 1,
            "description": "PermissionInterruptRail denied the tool call to rm",
            "evidence": {"risk_source": "PermissionInterruptRail", "decision": "reject"},
            "recommended_actions": ["log", "alert"],
            "module_name": "security_rail_detection",
            "aux_ids": {
                "trace_id": "trace-1",
                "session_id": "s1",
                "interaction_seq": 0,
                "agent_id": "a1",
                "llm_call_seq": 0,
                "tool_call_seq": 1,
            },
            "event_node": {
                "event_type": "permission_interrupt_tool",
                "action_name": "rm",
                "input_content": '{"command": "rm -rf /"}',
                "output_content": "",
                "source": "AgentSSASSecurityRail",
            },
            "timestamp": 1715000000.0,
        }
        await presentation.render(report)
        # 验证文件已生成
        output_dir = Path(ssas_config.storage_path) / "reports" / "threat_log"
        files = list(output_dir.glob("threat_trace-1_*.json"))
        assert len(files) == 1
        # 验证文件名为可读时间格式(YYYYMMDD_HHMMSS_mmm)
        import re

        file_name = files[0].name
        assert re.search(
            r"_\d{8}_\d{6}_\d{3}\.json$", file_name
        ), f"威胁日志文件名时间格式不符: {file_name!r}"
        # 验证文件内容为 OCSF Detection Finding 格式
        with open(files[0], "r", encoding="utf-8") as f:
            ocsf = json.load(f)
        # 框架固定字段
        assert ocsf["activity_id"] == 1
        assert ocsf["activity_name"] == "Create"
        assert ocsf["category_uid"] == 2
        assert ocsf["category_name"] == "Findings"
        assert ocsf["class_uid"] == 2004
        assert ocsf["class_name"] == "Detection Finding"
        assert ocsf["severity_id"] == 4  # high -> 4
        assert ocsf["status"] == "New"
        assert ocsf["status_id"] == 1
        assert ocsf["trace_id"] == "trace-1"
        # metadata
        assert ocsf["metadata"]["product"]["name"] == "AgentSSAS"
        assert ocsf["metadata"]["product"]["vendor_name"] == "AgentSSAS"
        # metadata.product.feature.name 承载第二级发现者(检测模块名)
        assert ocsf["metadata"]["product"]["feature"]["name"] == "security_rail_detection"
        # metadata.profiles 声明 ai_operation profile
        assert ocsf["metadata"]["profiles"] == ["ai_operation"]
        # actor
        assert ocsf["actor"]["name"] == "a1"
        assert ocsf["actor"]["type"] == "Application"
        assert ocsf["actor"]["type_id"] == 4
        # ai_agent
        assert ocsf["ai_agent"]["uid"] == "a1"
        assert ocsf["ai_agent"]["type"] == "Native"
        # ai_agent.instance_uid 承载 session_id
        assert ocsf["ai_agent"]["instance_uid"] == "s1"
        # 无 resources 字段
        assert "resources" not in ocsf
        # finding_info 只保留 OCSF 标准字段
        assert "evidence" not in ocsf["finding_info"]
        assert "product" not in ocsf["finding_info"]
        # analytic 对象(第三级发现者,检测策略名)
        assert ocsf["finding_info"]["analytic"]["type_id"] == 1
        assert ocsf["finding_info"]["analytic"]["type"] == "Rule"
        assert ocsf["finding_info"]["analytic"]["name"] == "Tool Permission Denied"
        # finding_info.uid 为 UUID 字符串
        assert isinstance(ocsf["finding_info"]["uid"], str)
        assert len(ocsf["finding_info"]["uid"]) > 0
        assert ocsf["finding_info"]["desc"] == (
            "PermissionInterruptRail denied the tool call to rm"
        )
        # confidence 映射为 confidence_id (int) + confidence (str)
        assert ocsf["finding_info"]["confidence_id"] == 3  # 0.9 -> High -> 3
        assert ocsf["finding_info"]["confidence"] == "High"
        # confidence_score 按 OCSF 语义填置信度分值(0-100,与 confidence 0-1 同源)
        assert ocsf["finding_info"]["confidence_score"] == 90
        # types(OCSF 标准字段,原 risk_types)
        assert ocsf["finding_info"]["types"] == ["tool_misuse"]
        # created_time 是毫秒级时间戳
        assert isinstance(ocsf["finding_info"]["created_time"], int)
        assert ocsf["finding_info"]["created_time"] > 1715000000000
        # 顶层 evidences 数组(OCSF 标准 Evidence Artifacts)
        assert isinstance(ocsf["evidences"], list)
        assert len(ocsf["evidences"]) == 1
        evidence_item = ocsf["evidences"][0]
        assert isinstance(evidence_item["uid"], str)
        assert len(evidence_item["uid"]) > 0
        assert evidence_item["name"] == "detection_evidence"
        # evidences[0].data 包含 detected_threats 和 recommended_actions
        assert evidence_item["data"]["detected_threats"] == ["tool_misuse"]
        assert evidence_item["data"]["recommended_actions"] == ["log", "alert"]
        # 检测模块自定义 evidence 字段也合并到 data
        assert evidence_item["data"]["risk_source"] == "PermissionInterruptRail"
        assert evidence_item["data"]["decision"] == "reject"
        # message_context:工具事件的 prompt_text/response_text 为空(工具输入输出在 ai_operation 中)
        assert ocsf["message_context"]["ai_role_id"] == 3  # tool -> 3
        assert ocsf["message_context"]["ai_role"] == "Tool"
        assert ocsf["message_context"]["prompt_text"] == ""
        assert ocsf["message_context"]["response_text"] == ""
        assert ocsf["message_context"]["application"]["name"] == "JiuwenSwarm"
        # risk_source 从 evidence 中提取
        assert ocsf["message_context"]["service"]["name"] == "PermissionInterruptRail"
        # ai_operation
        interactions = ocsf["ai_operation"]["interactions"]
        assert len(interactions) == 1
        llm_calls = interactions[0]["llm_calls"]
        assert len(llm_calls) == 1
        tool_calls = llm_calls[0]["tool_calls"]
        assert len(tool_calls) == 1
        assert tool_calls[0]["name"] == "rm"
        assert tool_calls[0]["input"] == '{"command": "rm -rf /"}'

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level0
    def test_risk_level_to_severity_mapping(ssas_config: AgentSSASConfig) -> None:
        """验证 risk_level_to_severity 映射。"""
        # safe -> 1, low -> 2, medium -> 3, high -> 4, critical -> 5
        assert risk_level_to_severity("safe") == 1
        assert risk_level_to_severity("low") == 2
        assert risk_level_to_severity("medium") == 3
        assert risk_level_to_severity("high") == 4
        assert risk_level_to_severity("critical") == 5
        # 未知风险等级回退为 1 (Info)
        assert risk_level_to_severity("unknown") == 1

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level0
    def test_build_ocsf_report_no_risk(ssas_config: AgentSSASConfig) -> None:
        """验证 build_ocsf_report 构建无风险报告。"""
        report = {
            "has_risk": False,
            "risk_level": "safe",
            "module_name": "test_detection",
            "analytic_name": "Pass Through Scan",
            "analytic_type_id": 1,
            "description": "No risk detected",
            "aux_ids": {"trace_id": "trace-safe", "agent_id": "a1"},
            "event_node": {"event_type": "tool_input"},
            "timestamp": 1715000000.0,
        }
        ocsf = build_ocsf_report(report)
        # status 固定为 OCSF 标准 finding 生命周期状态 New (1)
        assert ocsf["status"] == "New"
        assert ocsf["status_id"] == 1
        assert ocsf["severity_id"] == 1  # safe -> 1
        assert ocsf["class_uid"] == 2004
        # 无 risk_type 时 types 为空列表
        assert ocsf["finding_info"]["types"] == []
        # 顶层 evidences 数组存在(即使无风险也有 data)
        assert isinstance(ocsf["evidences"], list)
        assert len(ocsf["evidences"]) == 1
        # 无 detected_threats 和 recommended_actions 时 data 不含这两个字段
        assert "detected_threats" not in ocsf["evidences"][0]["data"]
        assert "recommended_actions" not in ocsf["evidences"][0]["data"]
        # analytic 对象
        assert ocsf["finding_info"]["analytic"]["name"] == "Pass Through Scan"
        assert ocsf["finding_info"]["analytic"]["type_id"] == 1
        # 无 resources 字段
        assert "resources" not in ocsf

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level0
    def test_build_ocsf_report_sanitizes_non_dict_evidence() -> None:
        """验证非 dict 类型 evidence 被忽略(不影响 evidences 数组结构)。"""
        report = {
            "has_risk": False,
            "risk_level": "safe",
            "module_name": "test_detection",
            "aux_ids": {"trace_id": "trace-x"},
            "event_node": {},
            "evidence": "not a dict",
        }
        ocsf = build_ocsf_report(report)
        # 非 dict evidence 被忽略,evidences 数组仍正常构造
        assert isinstance(ocsf["evidences"], list)
        assert len(ocsf["evidences"]) == 1
        # 无 detected_threats 和 recommended_actions 时 data 不含这两个字段
        assert "detected_threats" not in ocsf["evidences"][0]["data"]
        assert "recommended_actions" not in ocsf["evidences"][0]["data"]
        # finding_info 不再有 evidence 字段
        assert "evidence" not in ocsf["finding_info"]

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level1
    async def test_render_no_risk_status_info(ssas_config: AgentSSASConfig) -> None:
        """验证无风险报告 status 固定为 New(OCSF finding 生命周期状态)。"""
        presentation = AgentSSASThreatLog(ssas_config)
        report = {
            "has_risk": False,
            "risk_level": "safe",
            "module_name": "test_detection",
            "analytic_name": "Pass Through Scan",
            "analytic_type_id": 1,
            "aux_ids": {"trace_id": "trace-2"},
            "event_node": {"event_type": "tool_input"},
            "timestamp": 0.0,
        }
        await presentation.render(report)
        output_dir = Path(ssas_config.storage_path) / "reports" / "threat_log"
        files = list(output_dir.glob("threat_trace-2_*.json"))
        assert len(files) == 1
        with open(files[0], "r", encoding="utf-8") as f:
            ocsf = json.load(f)
        # status 固定为 OCSF 标准 finding 生命周期状态 New (1)
        assert ocsf["status"] == "New"
        assert ocsf["severity_id"] == 1  # safe -> 1
        assert ocsf["status_id"] == 1

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level1
    async def test_render_invalid_report_type(ssas_config: AgentSSASConfig) -> None:
        """验证传入非 dict 时抛出 TypeError。"""
        presentation = AgentSSASThreatLog(ssas_config)
        with pytest.raises(TypeError):
            await presentation.render("not a dict")  # type: ignore[arg-type]

    @staticmethod
    @pytest.mark.unit
    @pytest.mark.level1
    async def test_render_empty_report(ssas_config: AgentSSASConfig) -> None:
        """验证传入空 dict 时抛出 ValueError。"""
        presentation = AgentSSASThreatLog(ssas_config)
        with pytest.raises(ValueError):
            await presentation.render({})
