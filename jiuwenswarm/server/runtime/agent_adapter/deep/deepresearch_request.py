"""请求级 DeepResearch report_type / followup 解析。

职责：
- 从 ``AgentRequest.params`` 严格读取 ``report_type``（professional|brief）
- 从 ``metadata.deepresearch_followup`` 校验结构化后续动作
  （upgrade_latest_brief / refresh_latest_brief）
- 非法结构只告警并视为未携带，不整单拒绝

不含会话态或 rewrite 快路径；rewrite 相关常量仍留在门面 / pure_helpers。
"""

from __future__ import annotations

import logging
from typing import Any, cast

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.perf.context import DeepResearchReportType

logger = logging.getLogger(__name__)

_DEEPRESEARCH_REPORT_TYPES = frozenset({"professional", "brief"})
_INVALID_DEEPRESEARCH_REPORT_TYPE_MESSAGE = (
    "params.report_type must be one of: professional, brief"
)


def _extract_requested_report_type(request: AgentRequest) -> DeepResearchReportType | None:
    """Return the strict request-scoped report type; metadata is not trusted."""
    params = request.params if isinstance(request.params, dict) else {}
    if "report_type" not in params:
        return None
    value = params["report_type"]
    if not isinstance(value, str) or value not in _DEEPRESEARCH_REPORT_TYPES:
        raise ValueError(_INVALID_DEEPRESEARCH_REPORT_TYPE_MESSAGE)
    return cast(DeepResearchReportType, value)


_DEEPRESEARCH_FOLLOWUP_ACTIONS = frozenset({"upgrade_latest_brief", "refresh_latest_brief"})


def _extract_deepresearch_followup(request: AgentRequest) -> dict[str, Any] | None:
    """Return the validated structured DeepResearch follow-up action, if any.

    metadata 是服务端（relay）扩展通道：结构不合法时告警并按未携带处理
    （请求继续，Main Agent 仍可走手动升级路径），不整体拒绝该消息。
    """
    metadata = request.metadata if isinstance(request.metadata, dict) else {}
    value = metadata.get("deepresearch_followup")
    if value is None:
        return None
    if not isinstance(value, dict) or value.get("v") != 1:
        logger.warning(
            "[interface_deep] ignore invalid deepresearch_followup structure "
            "request_id=%s",
            request.request_id,
        )
        return None
    action = value.get("action")
    source_report_id = value.get("source_report_id")
    valid_action = isinstance(action, str) and action in _DEEPRESEARCH_FOLLOWUP_ACTIONS
    if (
        not valid_action
        or not isinstance(source_report_id, str)
        or not source_report_id.strip()
    ):
        logger.warning(
            "[interface_deep] ignore invalid deepresearch_followup action "
            "request_id=%s",
            request.request_id,
        )
        return None
    return {
        "v": 1,
        "action": action,
        "source_report_id": source_report_id.strip(),
    }
