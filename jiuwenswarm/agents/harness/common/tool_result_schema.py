# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""工具结果统一 schema（T1）：归一 + LLM 渲染的纯函数实现。

设计文档：GitCode openJiuwen/jiuwenswarm#5243（v3.0）§3/§4.2。
权威 schema：

    {"status": "success"|"error"|"timeout"|"cancelled",   # 工具级权威标识
     "success": bool,                                     # 兼容别名 == (status == "success")
     "error_code": str,                                   # 失败时必备（机读）
     "error_message": str,                                # 失败时必备（人读）；双写兼容键 "error"
     "data": Any,                                         # 业务载荷（非 dict 输入的载荷落点）
     "display": str,                                      # 可选展示文本
     ...原始业务键平铺保留（兼容区，阶段 D 收敛后移除）}

语义约束（实跑教训，勿删）：
- S1 status 是工具执行级语义；业务数据中的任务态（a2a forwarded/cancelled、
  deepresearch kind）不得映射为工具 status——原业务 status 键改名 biz_status 保留。
- S3 success 别名由归一点统一产出，覆盖工具自带的"说谎 success"。
- S4 多模态/图片载荷必须平铺保留在顶层（openjiuwen react_agent 提取依赖）。
- S5 兼容键 error 与 error_message 双写同值；ok/error_code 等既有键原样保留。

注意：``has_failure_signal`` / ``error_text_from_payload`` 是全仓失败信号判定
的**唯一实现**——消费方（``server/runtime/skill_turbo/runtime/tool_utils.py``
经委托、circuit_breaker / stream_event rail 经同语义增量识别）一律不得私有
复制判定逻辑；语义演进只改本模块，契约由 test_tool_utils_bash_parsing 与
test_tool_result_normalize_rail 共同钉住。
"""

from __future__ import annotations

import json
import re
from typing import Any

STATUS_SUCCESS = "success"
STATUS_ERROR = "error"
STATUS_TIMEOUT = "timeout"
STATUS_CANCELLED = "cancelled"
VALID_STATUSES = frozenset({STATUS_SUCCESS, STATUS_ERROR, STATUS_TIMEOUT, STATUS_CANCELLED})
# 工具返回值可自报的 status 子集。cancelled 不在其中（S2）：它只能由框架取消
# 路径（gateway 取消 / AgentServer 中断）产出——a2a 等业务数据的任务态
# cancelled 与工具级取消同形不同义，信任输入会把业务取消误标为工具取消。
TRUSTED_INPUT_STATUSES = frozenset({STATUS_SUCCESS, STATUS_ERROR, STATUS_TIMEOUT})

# 失败枚举（status 键的失败同义词）。合法业务态（forwarded/cancelled/persisted_only）
# 不在其中——见 skill_turbo tool_utils 同名约定的注释。
FAILURE_STATUS_VALUES = frozenset({"error", "failed", "failure"})
_FAILURE_FLAG_KEYS = ("is_error", "isError")
_FAILURE_TYPE_KEYS = ("result_type", "kind")

_ERROR_TEXT_KEYS = ("error", "error_summary", "error_msg", "errMessage")
# 历史占位字面量：工具层曾用 "none" 表示"无错误"，不能当失败文本。
# 与 circuit_breaker_rail 的错误判定保持同一哨兵规则：
#   str(err).strip() and str(err).strip().lower() != "none"
_NON_FAILURE_ERROR_SENTINELS = frozenset({"none"})
_FALLBACK_ERROR_CODE = {
    STATUS_ERROR: "tool_error",
    STATUS_TIMEOUT: "tool_timeout",
    STATUS_CANCELLED: "tool_cancelled",
}
_EMPTY_ERROR_MESSAGE = "tool failed without details"

_EXIT_CODE_PREFIX = re.compile(r"^\s*Exit code (\d+)", re.IGNORECASE)


def _is_failure_error_value(value: Any) -> bool:
    """错误键是否携带失败文本：非空且排除占位字面量 "none"（大小写不敏感）。

    与 ``circuit_breaker_rail.ToolResultErrorDetector._dict_has_error`` 的
    错误文本判定保持一致（两处容忍度必须相同，否则会出现"归一判失败、
    熔断器判成功"的语义分裂）。
    """
    if value is None:
        return False
    text = str(value).strip()
    return bool(text) and text.lower() not in _NON_FAILURE_ERROR_SENTINELS


def has_failure_signal(payload: dict) -> bool:
    """dict 载荷是否携带 success 键之外的结构化失败信号。"""
    if payload.get("ok") is False:
        return True
    for key in _FAILURE_FLAG_KEYS:
        if payload.get(key) is True:
            return True
    status = payload.get("status")
    if isinstance(status, str) and status.strip().lower() in FAILURE_STATUS_VALUES:
        return True
    for key in _FAILURE_TYPE_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value.strip().lower() == "error":
            return True
    # 错误文本键非空才算失败文本；"none" 占位字面量不算（哨兵规则与
    # circuit_breaker_rail 一致，见 _is_failure_error_value）
    for key in _ERROR_TEXT_KEYS:
        if _is_failure_error_value(payload.get(key)):
            return True
    return False


def error_text_from_payload(payload: dict) -> str:
    """从 dict 载荷提取人读错误文本（error 之外的兜底键链）。

    仅返回有意义的文本：占位字面量 "none" 不当作错误信息（与
    has_failure_signal 的哨兵规则一致）。
    """
    for key in _ERROR_TEXT_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and _is_failure_error_value(value):
            return value
    if has_failure_signal(payload):
        for key in ("message", "content"):
            value = payload.get(key)
            if isinstance(value, str) and _is_failure_error_value(value):
                return value
    return ""


def _finalize(
    normalized: dict[str, Any],
    *,
    status: str,
    error_code: str | None = None,
    error_message: str | None = None,
) -> dict[str, Any]:
    """填充 success 别名与失败字段（schema 收口唯一出口）。"""
    normalized["status"] = status
    normalized["success"] = status == STATUS_SUCCESS
    if status != STATUS_SUCCESS:
        fallback_code = _FALLBACK_ERROR_CODE[STATUS_ERROR]
        normalized["error_code"] = str(
            error_code or _FALLBACK_ERROR_CODE.get(status, fallback_code)
        )
        message = error_message if isinstance(error_message, str) and error_message.strip() else _EMPTY_ERROR_MESSAGE
        normalized["error_message"] = message
        # S5：兼容键 error 双写（存量消费方读 error 键）；"none" 占位字面量
        # 视为无意义，替换为真实错误文本
        if not _is_failure_error_value(normalized.get("error")):
            normalized["error"] = message
    return normalized


def _flatten_data(normalized: dict[str, Any], data: Any) -> None:
    """把 dict 形态的 data 业务键平铺到顶层（不覆盖工具级键，S4 依赖）。"""
    if not isinstance(data, dict):
        return
    for key, value in data.items():
        if key in ("status", "success", "error_code", "error_message", "data", "display"):
            continue
        normalized.setdefault(key, value)


def _normalize_tool_output(result: Any) -> dict[str, Any]:
    """openjiuwen ToolOutput（属性型 success/data/error）平铺展开。"""
    success = getattr(result, "success", None)
    data = getattr(result, "data", None)
    error = getattr(result, "error", None)
    failure = success is False
    if success is True and isinstance(data, dict) and has_failure_signal(data):
        # 说谎 success：data 携带失败信号时按失败（与 skill_turbo get_tool_success 同语义）
        failure = True
    normalized: dict[str, Any] = {"data": data}
    if isinstance(error, str) and error.strip():
        normalized["error"] = error
    _flatten_data(normalized, data)
    if failure:
        return _finalize(normalized, status=STATUS_ERROR, error_message=error if isinstance(error, str) else None)
    return _finalize(normalized, status=STATUS_SUCCESS)


def _normalize_dict(result: dict) -> dict[str, Any]:
    """dict 输入归一（含业务 status 改名 biz_status，S1/S2）。"""
    original_status = result.get("status")
    conforming = isinstance(original_status, str) and original_status.strip().lower() in TRUSTED_INPUT_STATUSES
    if conforming:
        # 已符合 schema：透传 + 补齐；显式 cancelled/timeout 尊重工具自报，
        # 但 success/error 态仍受失败信号压制（S3）
        normalized = dict(result)
        status = str(original_status).strip().lower()
        if status in (STATUS_ERROR, STATUS_SUCCESS) and has_failure_signal(
            {k: v for k, v in result.items() if k != "status"}
        ):
            status = STATUS_ERROR
        if status == STATUS_SUCCESS:
            # success 态带非空 error 键自相矛盾，按失败处理（防止假成功逃逸）；
            # "none" 占位字面量不算矛盾（哨兵规则同上）
            if _is_failure_error_value(normalized.get("error")):
                status = STATUS_ERROR
        return _finalize(
            normalized,
            status=status,
            error_code=normalized.get("error_code"),
            error_message=normalized.get("error_message") or normalized.get("error"),
        )

    failure = result.get("success") is False or has_failure_signal(result)
    normalized = dict(result)
    if "status" in normalized:
        # S1：业务 status 改名保留，避免与工具级 status 撞键
        normalized.setdefault("biz_status", normalized.pop("status"))
    if failure:
        return _finalize(
            normalized,
            status=STATUS_ERROR,
            error_code=normalized.get("error_code") if isinstance(normalized.get("error_code"), str) else None,
            error_message=error_text_from_payload(result),
        )
    # 无任何失败信号：默认 success（内容经 LLM 视图/前端呈现；工具失败必须携带
    # 信号，新增工具由静态扫描守门拦截）
    return _finalize(normalized, status=STATUS_SUCCESS)


def _normalize_text(text: str) -> dict[str, Any]:
    """文本输入归一：JSON 对象 / [ERROR] 前缀 / Exit code N。

    JSON 字符串必须先解析再按 dict 规则归一：上游不可重试契约
    （``tool_result_contract.build_non_retryable_tool_result``）与
    deepresearch 等工具以 JSON 字符串携带 ``success: false``，按纯文本
    处理会被误判为成功（假成功）。
    """
    stripped = text.strip()
    if not stripped:
        return _finalize({"data": {"content": text}}, status=STATUS_SUCCESS)
    if stripped.startswith("{"):
        try:
            parsed = json.loads(stripped)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            return _normalize_dict(parsed)
    if stripped.startswith("[ERROR]") or stripped.startswith("Error:"):
        return _finalize(
            {"display": stripped, "data": {"content": text}},
            status=STATUS_ERROR,
            error_code="tool_error",
            error_message=stripped,
        )
    exit_match = _EXIT_CODE_PREFIX.match(stripped)
    if exit_match is not None and int(exit_match.group(1)) != 0:
        return _finalize(
            {"display": stripped, "data": {"content": text}},
            status=STATUS_ERROR,
            error_code="tool_exit_code",
            error_message=stripped,
        )
    return _finalize({"display": stripped, "data": {"content": text}}, status=STATUS_SUCCESS)


def normalize_tool_result(result: Any) -> dict[str, Any]:
    """把任意工具返回形状归一为统一 schema dict（v3.0 §4.2 行为矩阵）。"""
    if result is None:
        # 空结果非失败；调用方（rail）对 None 保持原样，不进入本函数
        return _finalize({"data": None}, status=STATUS_SUCCESS)
    if isinstance(result, BaseException):
        # 异常对象作为返回值（沙箱 fs 链路实测：build_error 异常对象成为
        # tool_result，str() 即错误文本）——按定义是失败，零误伤风险
        return _finalize(
            {"data": None},
            status=STATUS_ERROR,
            error_code="tool_exception",
            error_message=str(result) or result.__class__.__name__,
        )
    if isinstance(result, dict):
        return _normalize_dict(result)
    if isinstance(result, str):
        return _normalize_text(result)
    if isinstance(result, list):
        items = result
        item_failures = [
            item for item in items if isinstance(item, dict) and has_failure_signal(item)
        ]
        # 全部单项失败才算失败；部分成功是真实业务语义
        all_failed = bool(items) and len(item_failures) == len(items)
        normalized: dict[str, Any] = {"results": items, "data": {"results": items}}
        if all_failed:
            first_error = error_text_from_payload(item_failures[0]) if item_failures else ""
            return _finalize(
                normalized,
                status=STATUS_ERROR,
                error_code="tool_batch_error",
                error_message=first_error or f"all {len(items)} items failed",
            )
        return _finalize(normalized, status=STATUS_SUCCESS)
    if hasattr(result, "success"):
        return _normalize_tool_output(result)
    # 其余对象退化为文本路径
    return _normalize_text(str(result))


def render_tool_result_for_llm(normalized: dict[str, Any]) -> str:
    """LLM 视图渲染（S6）：失败给错误文本，成功给 display 或紧凑 JSON。

    取代框架对 dict/ToolOutput 的 ``str(result)`` repr——模型不再看到
    Python repr，失败时直接看到人读错误。
    """
    display = normalized.get("display")
    if isinstance(display, str) and display.strip():
        return display
    status = normalized.get("status")
    if status != STATUS_SUCCESS:
        message = normalized.get("error_message")
        if isinstance(message, str) and message.strip():
            return message
    try:
        return json.dumps(normalized, ensure_ascii=False, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        return str(normalized)


__all__ = [
    "STATUS_CANCELLED",
    "STATUS_ERROR",
    "STATUS_SUCCESS",
    "STATUS_TIMEOUT",
    "TRUSTED_INPUT_STATUSES",
    "VALID_STATUSES",
    "error_text_from_payload",
    "has_failure_signal",
    "normalize_tool_result",
    "render_tool_result_for_llm",
]
