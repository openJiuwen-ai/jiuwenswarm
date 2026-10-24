# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""OCSF 格式构建辅助函数。

基于 OCSF(Open Cybersecurity Schema Framework)1.8.0+ 的
Detection Finding 事件类(class_uid=2004)和 ai_operation profile,
通过官方扩展机制构建威胁告警报告。

扩展格式分为三部分:
- 框架固定字段:从事件数据(aux_ids、event_node)和配置自动构建
- 检测模块固定格式字段:检测模块通过 report dict 提供,框架映射到 OCSF 对应字段
- 检测模块自定义字段:顶层 evidences 数组(OCSF 标准 Evidence Artifacts),
  其中 evidence.data 承载 detected_threats、recommended_actions 和检测模块特有信息
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import UTC, datetime
from typing import Any

# 固定常量(见 9.3.3 节)
ACTIVITY_ID = 1
ACTIVITY_NAME = "Create"
CATEGORY_UID = 2
CATEGORY_NAME = "Findings"
CLASS_UID = 2004
CLASS_NAME = "Detection Finding"

# event_type → ai_role_id 映射(见 9.3.7 节)
# 1=User, 2=Assistant, 3=Tool
EVENT_TYPE_TO_AI_ROLE = {
    "invoke_start": 1,
    "invoke_end": 1,
    "llm_input": 2,
    "llm_output": 2,
    "tool_input": 3,
    "tool_output": 3,
    "permission_interrupt_tool": 3,
}

# evidence 序列化后 JSON 最大长度(字符)
EVIDENCE_MAX_JSON_LENGTH = 65536

# analytic_type_id → analytic.type 映射(见 9.3.10 节)
# 1=Rule, 2=Behavior
ANALYTIC_TYPE_MAP = {
    1: "Rule",
    2: "Behavior",
}

# confidence_id 映射(见 9.3.10 节)
# 0=Unknown, 1=Low, 2=Medium, 3=High


def risk_level_to_severity(risk_level: str) -> int:
    """风险等级字符串映射为 OCSF severity_id 整数。

    1=Info, 2=Low, 3=Medium, 4=High, 5=Critical。
    未知风险等级回退为 1(Info)。
    """
    mapping = {"safe": 1, "low": 2, "medium": 3, "high": 4, "critical": 5}
    return mapping.get(risk_level, 1)


def confidence_to_id(confidence: float) -> int:
    """置信度数值映射为 OCSF confidence_id 整数。

    - confidence >= 0.9 → 3 (High)
    - confidence >= 0.7 → 2 (Medium)
    - confidence > 0 → 1 (Low)
    - confidence == 0 → 0 (Unknown)
    """
    if confidence >= 0.9:
        return 3
    if confidence >= 0.7:
        return 2
    if confidence > 0:
        return 1
    return 0


def confidence_id_to_str(confidence_id: int) -> str:
    """confidence_id 整数映射为 OCSF confidence 字符串。

    0=Unknown, 1=Low, 2=Medium, 3=High。
    """
    mapping = {0: "Unknown", 1: "Low", 2: "Medium", 3: "High"}
    return mapping.get(confidence_id, "Unknown")


def build_metadata(module_name: str = "") -> dict:
    """构建 metadata 对象(第一级发现者,框架固定)。

    metadata.product.feature.name 承载第二级发现者(检测模块名),
    通过 module_name 参数传入;profiles 声明使用 ai_operation profile。
    """
    return {
        "product": {
            "name": "AgentSSAS",
            "vendor_name": "AgentSSAS",
            "feature": {"name": module_name or ""},
        },
        "version": "1.0",
        "profiles": ["ai_operation"],
    }


def build_actor(agent_id: str) -> dict:
    """构建 actor 对象(框架固定)。"""
    return {"name": agent_id or "", "type_id": 4, "type": "Application"}


def build_ai_agent(agent_id: str, session_id: str = "") -> dict:
    """构建 ai_agent 对象(框架固定,来自 ai_operation profile)。

    instance_uid 承载会话 ID(session_id),用于关联会话级上下文。
    """
    return {
        "uid": agent_id or "",
        "name": agent_id or "",
        "type_id": 1,
        "type": "Native",
        "instance_uid": session_id or "",
    }


def build_message_context(event_node: dict, risk_source: str = "") -> dict:
    """构建 message_context 对象(框架固定,来自 ai_operation profile)。

    prompt_text 保存 AI 模型的输入(用户原始输入或 LLM prompt),
    response_text 保存 AI 模型的输出(Agent 响应或 LLM response)。
    工具的输入输出不在 message_context 中,而是在 ai_operation 中。
    """
    event_type = event_node.get("event_type", "")
    ai_role_id = EVENT_TYPE_TO_AI_ROLE.get(event_type, 3)
    ai_role_map = {1: "User", 2: "Assistant", 3: "Tool"}

    # prompt_text/response_text 按 event_type 语义填充:
    # - invoke_start: prompt_text = query(用户输入),response_text = ""
    # - invoke_end: prompt_text = "",response_text = result(Agent 输出)
    # - llm_input: prompt_text = messages(模型输入),response_text = ""
    # - llm_output: prompt_text = "",response_text = response(模型输出)
    # - tool_input/tool_output/permission_interrupt_tool: 都为空(工具输入输出在 ai_operation 中)
    prompt_text = ""
    response_text = ""
    if event_type in ("invoke_start", "llm_input"):
        prompt_text = event_node.get("input_content", "")
    elif event_type in ("invoke_end", "llm_output"):
        response_text = event_node.get("output_content", "")

    service_name = risk_source or event_node.get("source", "")
    return {
        "ai_role_id": ai_role_id,
        "ai_role": ai_role_map.get(ai_role_id, "Tool"),
        "prompt_text": prompt_text,
        "response_text": response_text,
        "application": {"name": "JiuwenSwarm"},
        "service": {"name": service_name},
    }


def build_ai_operation(event_node: dict, aux_ids: dict) -> dict:
    """从 event_node 和 aux_ids 构建 ai_operation 对象。

    interaction → llm_call → tool_call → action 层次结构。
    seq 使用整数。当前事件按其 event_type 和 aux_ids 中的 seq 值
    填充到对应层级,上层结构以空壳占位保持层次完整。

    当事件具备 tool_call_seq 但缺少 llm_call_seq(如安全检测事件在 LLM
    调用前被拒绝)时,仍需创建 llm_call 层承载 tool_call,避免数据丢失。
    """
    interaction_seq = aux_ids.get("interaction_seq", -1)
    llm_call_seq = aux_ids.get("llm_call_seq", -1)
    tool_call_seq = aux_ids.get("tool_call_seq", -1)

    # 获取内容字段
    input_content = event_node.get("input_content", "")
    output_content = event_node.get("output_content", "")
    action_name = event_node.get("action_name", "")

    # 构建 tool_call(仅当 tool_call_seq 有效时填充内容)
    tool_call = {
        "seq": tool_call_seq,
        "name": action_name,
        "input": input_content,
        "output": output_content,
        "actions": [],
    }

    # 构建 llm_call:仅当无 tool_call 层级时填充 llm 内容
    llm_input = input_content if llm_call_seq != -1 and tool_call_seq == -1 else ""
    llm_output = output_content if llm_call_seq != -1 and tool_call_seq == -1 else ""
    llm_call = {
        "seq": llm_call_seq,
        "input": llm_input,
        "output": llm_output,
        "tool_calls": [tool_call] if tool_call_seq != -1 else [],
    }

    # 构建 interaction:invoke_start/invoke_end 时填充交互级内容
    interaction_input = (
        input_content if event_node.get("event_type") in ("invoke_start",) else ""
    )
    interaction_output = (
        output_content if event_node.get("event_type") in ("invoke_end",) else ""
    )
    # llm_calls 包含条件:llm_call_seq 有效,或 tool_call_seq 有效
    # (后者场景:安全检测事件在 LLM 调用前被拒绝,仅有 tool_call 层级)
    has_llm_call = llm_call_seq != -1 or tool_call_seq != -1
    interaction = {
        "seq": interaction_seq,
        "input": interaction_input,
        "output": interaction_output,
        "llm_calls": [llm_call] if has_llm_call else [],
    }

    return {"interactions": [interaction]}


def build_analytic(report: dict) -> dict:
    """构建 finding_info.analytic 对象(第三级发现者,检测策略名)。

    analytic.type_id 来自检测模块在 module.yaml 中声明的 analytic_type_id,
    由流水线模块注入到 report 中;analytic.name 取自 report 的 analytic_name
    字段(检测模块输出,原 title 字段)。
    """
    analytic_type_id = report.get("analytic_type_id", 0)
    if not isinstance(analytic_type_id, int):
        analytic_type_id = 0
    analytic_type = ANALYTIC_TYPE_MAP.get(analytic_type_id, "")
    # analytic.name 取自检测模块输出的 analytic_name(原 title 字段)
    analytic_name = report.get("analytic_name") or report.get("title", "")
    return {
        "type_id": analytic_type_id,
        "type": analytic_type,
        "name": analytic_name,
    }


def sanitize_evidence(evidence) -> dict:
    """检查 evidence 的类型和长度。

    - evidence 必须是 dict 类型,否则设为 {}
    - 序列化后 JSON 长度限制 65536 字符,超出时添加 _truncated 标记
    """
    if not isinstance(evidence, dict):
        return {}
    try:
        evidence_json = json.dumps(evidence, ensure_ascii=False)
        if len(evidence_json) <= EVIDENCE_MAX_JSON_LENGTH:
            return evidence
        # 超出限制,标记截断
        result = dict(evidence)
        result["_truncated"] = True
        return result
    except (TypeError, ValueError):
        return {}


def build_evidence(report: dict) -> dict:
    """构建 finding_info.evidence(合并 detected_threats + recommended_actions + 检测模块自定义 evidence)。

    合并规则:
    - detected_threats:列表转 evidence.detected_threats
    - recommended_actions:列表转 evidence.recommended_actions
    - 检测模块自定义 evidence:浅合并到 evidence(覆盖同名字段)
    """
    evidence: dict[str, Any] = {}
    # detected_threats 合并到 evidence
    detected_threats = report.get("detected_threats", [])
    if isinstance(detected_threats, list):
        evidence["detected_threats"] = detected_threats
    # recommended_actions 合并到 evidence
    recommended_actions = report.get("recommended_actions", ["log"])
    if isinstance(recommended_actions, list):
        evidence["recommended_actions"] = recommended_actions
    # 检测模块自定义 evidence 浅合并(覆盖同名字段)
    custom_evidence = report.get("evidence", {})
    if isinstance(custom_evidence, dict):
        evidence.update(custom_evidence)
    return sanitize_evidence(evidence)


def build_evidences(report: dict) -> list[dict]:
    """构建顶层 evidences 数组(OCSF 标准 Evidence Artifacts)。

    将 detected_threats、recommended_actions 和检测模块自定义 evidence 合并到
    单个 evidence 项的 data 字段中,不包含 ai_agent(顶层已有,避免冗余)。
    """
    from agent_ssas.core.framework.utils.id_utils import new_uuid

    data: dict[str, Any] = {}
    # 合并 detected_threats 和 recommended_actions 到 data
    if report.get("detected_threats"):
        data["detected_threats"] = report["detected_threats"]
    if report.get("recommended_actions"):
        data["recommended_actions"] = report["recommended_actions"]
    # 合并检测模块自定义 evidence
    custom_evidence = report.get("evidence", {})
    if isinstance(custom_evidence, dict):
        data.update(custom_evidence)
    return [{
        "uid": new_uuid(),
        "name": "detection_evidence",
        "data": data,
    }]


def build_ocsf_report(report: dict) -> dict:
    """从检测模块 report 构建完整的 OCSF Detection Finding 格式报告。

    框架固定字段从 aux_ids、event_node 自动构建;检测模块固定格式字段
    从 report 映射到 finding_info;检测模块自定义字段 evidence 通过
    顶层 evidences 数组(OCSF 标准 Evidence Artifacts)承载。

    finding_info.analytic 为 OCSF 标准对象,承载第三级发现者(检测策略名);
    metadata.product.feature.name 承载第二级发现者(检测模块名);
    detected_threats 和 recommended_actions 合并到 evidences[0].data 中。

    status 采用 OCSF Detection Finding 标准的 finding 生命周期状态:
    固定为 "New"(status_id=1),表示新创建的检测发现。
    """
    aux_ids = report.get("aux_ids", {})
    event_node = report.get("event_node", {})
    if not isinstance(event_node, dict):
        event_node = {}
    # risk_source 优先从 evidence 中提取,用于 message_context.service.name
    custom_evidence = report.get("evidence", {})
    risk_source = ""
    if isinstance(custom_evidence, dict):
        risk_source = custom_evidence.get("risk_source", "")

    # status 采用 OCSF 标准的 finding 生命周期状态:New (1)
    status = "New"
    status_id = 1

    # 第二级发现者:检测模块名,传入 metadata.product.feature.name
    module_name = report.get("module_name", "")

    now = time.time()
    # created_time 改为毫秒级时间戳
    created_time = int(now * 1000)
    created_time_dt = datetime.fromtimestamp(now, tz=UTC).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z"
    )

    # confidence 映射为 confidence_id (int) + confidence (str)
    confidence = report.get("confidence", 0.0)
    if not isinstance(confidence, (int, float)):
        confidence = 0.0
    confidence_id = confidence_to_id(float(confidence))
    confidence_str = confidence_id_to_str(confidence_id)

    # types 从 risk_type 构建(OCSF 标准字段)
    risk_type = report.get("risk_type", "")
    types = [risk_type] if risk_type else []

    # finding_info.uid 使用 UUID
    finding_uid = str(uuid.uuid4())

    return {
        "activity_id": ACTIVITY_ID,
        "activity_name": ACTIVITY_NAME,
        "category_uid": CATEGORY_UID,
        "category_name": CATEGORY_NAME,
        "class_uid": CLASS_UID,
        "class_name": CLASS_NAME,
        "severity_id": risk_level_to_severity(report.get("risk_level", "safe")),
        "status": status,
        "status_id": status_id,
        "time": int(report.get("timestamp", now) * 1000),
        "trace_id": aux_ids.get("trace_id", ""),
        "actor": build_actor(aux_ids.get("agent_id", "")),
        "metadata": build_metadata(module_name),
        "finding_info": {
            "uid": finding_uid,
            "desc": report.get("description", ""),
            "created_time": created_time,
            "created_time_dt": created_time_dt,
            "confidence_id": confidence_id,
            "confidence": confidence_str,
            # confidence_score 按 OCSF 语义填置信度分值(0-100),
            # 与 confidence(0-1)同源换算(risk_score 不再占用此字段)
            "confidence_score": round(float(confidence) * 100),
            "types": types,
            "analytic": build_analytic(report),
        },
        "evidences": build_evidences(report),
        "ai_agent": build_ai_agent(
            aux_ids.get("agent_id", ""), aux_ids.get("session_id", "")
        ),
        "message_context": build_message_context(event_node, risk_source),
        "ai_operation": build_ai_operation(event_node, aux_ids),
    }
