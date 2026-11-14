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
# 渲染视图里不应作为"业务兄弟键"重复输出的 schema 键
_RENDER_SCHEMA_KEYS = frozenset(
    {"status", "success", "error_code", "error_message", "error", "display", "data"}
)
# exit_code 族：与 circuit_breaker_rail._EXIT_KEYS / stream_event 同源四键
_EXIT_KEYS = ("exit_code", "exitCode", "returncode", "return_code")
# 历史占位字面量：工具层曾用 "none" 表示"无错误"，不能当失败文本。
# 与 circuit_breaker_rail 的错误判定保持同一哨兵规则：
#   str(err).strip() and str(err).strip().lower() != "none"
_NON_FAILURE_ERROR_SENTINELS = frozenset({"none"})
# boolish 字符串集合：与 circuit_breaker_rail.ToolResultErrorDetector 逐字同源
_BOOLISH_FALSE_VALUES = frozenset({"false", "0", "no"})
_BOOLISH_TRUE_VALUES = frozenset({"true", "1", "yes"})
_FALLBACK_ERROR_CODE = {
    STATUS_ERROR: "tool_error",
    STATUS_TIMEOUT: "tool_timeout",
    STATUS_CANCELLED: "tool_cancelled",
}
_EMPTY_ERROR_MESSAGE = "tool failed without details"

_EXIT_CODE_PREFIX = re.compile(r"^\s*Exit code (\d+)", re.IGNORECASE)


def boolish_false(value: Any) -> bool:
    """boolish 假值：``False`` 或字符串 "false"/"0"/"no"。

    与 ``circuit_breaker_rail.ToolResultErrorDetector._boolish_false`` /
    ``stream_event_rail._boolish_false`` 逐字同源——三方判定必须一致，
    否则会出现"熔断器计失败、归一判成功"的语义分裂（CR-4）。
    """
    if value is False:
        return True
    return isinstance(value, str) and value.strip().lower() in _BOOLISH_FALSE_VALUES


def boolish_true(value: Any) -> bool:
    """boolish 真值：``True`` 或字符串 "true"/"1"/"yes"。"""
    if value is True:
        return True
    return isinstance(value, str) and value.strip().lower() in _BOOLISH_TRUE_VALUES


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
    if boolish_false(payload.get("ok")):
        return True
    for key in _FAILURE_FLAG_KEYS:
        if boolish_true(payload.get(key)):
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
    # exit_code 族：与 circuit_breaker / stream_event 同源四键（int 且非 0）。
    # 语义必须与 ToolResultErrorDetector._dict_has_error 完全一致（parity 测试
    # 钉住）；exitCode=None 表示软超时，不命中。
    for key in _EXIT_KEYS:
        code = payload.get(key)
        if isinstance(code, int) and code != 0:
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


def _is_failure_payload(payload: dict) -> bool:
    """载荷整体是否为失败：结构化失败信号或显式 ``success=false``。

    不能直接扩展 ``has_failure_signal``（它与熔断器逐字同源）；嵌套 data 的
    ``success=false`` 只在归一判定里按失败处理。
    """
    return has_failure_signal(payload) or boolish_false(payload.get("success"))


def _failure_detail_text(payload: dict, *, allow_text_fallback: bool = False) -> str:
    """失败载荷的人读详情：错误键 → message / content。

    message/content 默认只有在该载荷自身可判定为失败时才作为错误文本；否则
    顶层成功文案（如 ``{"status": "success", "message": "found 3 results"}``）
    会覆盖嵌套失败的真实原因。父载荷已判定失败时，嵌套 data 的 message/content
    可经 ``allow_text_fallback`` 作为详情读取。
    """
    text = error_text_from_payload(payload)
    if text:
        return text
    if not allow_text_fallback and not _is_failure_payload(payload):
        return ""
    for key in ("message", "content"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
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
    failure = boolish_false(success)
    if boolish_true(success) and isinstance(data, dict) and _is_failure_payload(data):
        # 说谎 success：data 携带失败信号（含 success=false）时按失败
        # （与 skill_turbo get_tool_success 同语义）
        failure = True
    normalized: dict[str, Any] = {"data": data}
    if isinstance(error, str) and error.strip():
        normalized["error"] = error
    _flatten_data(normalized, data)
    if failure:
        # 失败详情可能只在 data 里（如 SearchOutput(data={"status": ..., "message": ...})，
        # error 默认 None）：不回退会退化成通用文案，模型看不到真实原因。
        message = error if isinstance(error, str) and error.strip() else ""
        if not message and isinstance(data, dict):
            message = error_text_from_payload(data)
            if not message:
                for key in ("message", "content"):
                    value = data.get(key)
                    if isinstance(value, str) and value.strip():
                        message = value
                        break
        return _finalize(normalized, status=STATUS_ERROR, error_message=message or None)
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
        nested = result.get("data")
        nested_failure = isinstance(nested, dict) and _is_failure_payload(nested)
        if status in (STATUS_ERROR, STATUS_SUCCESS) and (
            has_failure_signal({k: v for k, v in result.items() if k != "status"})
            or nested_failure
        ):
            status = STATUS_ERROR
        if status == STATUS_SUCCESS:
            # success 态带非空 error 键自相矛盾，按失败处理（防止假成功逃逸）；
            # "none" 占位字面量不算矛盾（哨兵规则同上）。boolish 的
            # success="false" 同样按失败处理（与熔断器同语义，CR-4）。
            if _is_failure_error_value(normalized.get("error")) or boolish_false(
                normalized.get("success")
            ):
                status = STATUS_ERROR
        nested_error = error_text_from_payload(nested) if nested_failure else ""
        detail_error = ""
        if status == STATUS_ERROR:
            # status 已是失败态时 message/content 也是失败详情（含 success=false
            # 压过 success status 的形状），不能因为没带 error 键就退化成通用文案。
            detail_error = _failure_detail_text(normalized)
            if not detail_error and isinstance(nested, dict):
                # 父载荷已是失败态，嵌套 data 的 message/content 也是详情
                detail_error = _failure_detail_text(nested, allow_text_fallback=True)
        return _finalize(
            normalized,
            status=status,
            error_code=normalized.get("error_code"),
            error_message=(
                normalized.get("error_message")
                or normalized.get("error")
                or nested_error
                or detail_error
            ),
        )

    data = result.get("data")
    nested_failure = isinstance(data, dict) and _is_failure_payload(data)
    failure = _is_failure_payload(result) or nested_failure
    normalized = dict(result)
    if "status" in normalized:
        # S1：业务 status 改名保留，避免与工具级 status 撞键
        normalized.setdefault("biz_status", normalized.pop("status"))
    if failure:
        message = ""
        if nested_failure and isinstance(data, dict):
            # 嵌套失败原因优先于顶层文案：顶层可能是成功 message / 大段 content
            message = _failure_detail_text(data, allow_text_fallback=True)
        if not message:
            message = _failure_detail_text(result)
        if not message and isinstance(data, dict):
            message = _failure_detail_text(data, allow_text_fallback=True)
        return _finalize(
            normalized,
            status=STATUS_ERROR,
            error_code=normalized.get("error_code") if isinstance(normalized.get("error_code"), str) else None,
            error_message=message or None,
        )
    # 无任何失败信号：默认 success（内容经 LLM 视图/前端呈现）。工具失败必须
    # 携带信号——新增工具的 AST 静态扫描守门属阶段 D 计划，尚未实现。
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
    if stripped.startswith("[TOOL_NOT_AVAILABLE]"):
        # skip 拒绝语义（拒绝执行 ≠ 成功）。
        # 取舍：本仓 progressive_tool_rail 已直接产出 schema dict；此处保留
        # 纯文本前缀识别，用于兜底仓库外路径（agent-core 的 security_rail /
        # edit_safety_rail 等 before 拒绝文本，外部依赖不可改）。二者语义一致
        # （status=error / error_code=tool_not_available）。
        return _finalize(
            {"display": stripped, "data": {"content": text}},
            status=STATUS_ERROR,
            error_code="tool_not_available",
            error_message=stripped,
        )
    # 裸 "Error:" 只认「首行短文本」：成功工具正文（文件内容/日志/README）可能
    # 恰好以 Error: 开头，全量前缀会把成功判成失败（与熔断器只登记 [ERROR]: 对齐）。
    error_prefix = stripped.startswith("[ERROR]") or (
        stripped.startswith("Error:")
        and len(stripped.splitlines()[0]) <= 200
        and len(stripped) <= 500
    )
    if error_prefix:
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


def _list_item_failed(item: Any) -> bool:
    """list 结果的单项是否为失败项（boolish success=false 或结构化失败信号）。"""
    if not isinstance(item, dict):
        return False
    return boolish_false(item.get("success")) or has_failure_signal(item)


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
        item_failures = [item for item in items if _list_item_failed(item)]
        # 全部单项失败才算失败；部分成功是真实业务语义
        all_failed = bool(items) and len(item_failures) == len(items)
        normalized: dict[str, Any] = {"results": items, "data": {"results": items}}
        if all_failed:
            first_error = _failure_detail_text(item_failures[0]) if item_failures else ""
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
    """LLM 视图渲染（S6）：失败给错误文本，成功给 display / content / JSON。

    成功路径禁止直接 dump 整个 normalized：``_flatten_data`` 会把 data 的业务键
    平铺到顶层而 data 键本身仍保留完整载荷，整体 dump 会让多模态 base64 /
    write_file 的 original_file 等大载荷在 LLM 上下文中出现两份。这里保持
    框架 ``_build_tool_message_content`` 的 content 提取语义（data["content"]
    优先），其余情况只 dump data 或（无 data 的 dict 输入）normalized。
    """
    display = normalized.get("display")
    if isinstance(display, str) and display.strip():
        return display
    status = normalized.get("status")
    if status != STATUS_SUCCESS:
        message = normalized.get("error_message")
        if isinstance(message, str) and message.strip():
            return message
    data = normalized.get("data")
    if isinstance(data, dict):
        content = data.get("content")
        if isinstance(content, str) and content.strip():
            return content
        # 只 dump data 会丢掉同级的 meta/summary/warnings 等兄弟键（模型看到的
        # 信息比改动前少）；把非 schema/非 data 内已有的兄弟键并回视图。
        merged = dict(data)
        for key, value in normalized.items():
            if key in _RENDER_SCHEMA_KEYS or key in merged:
                continue
            merged[key] = value
        return _json_text(merged)
    if data is not None:
        return _json_text(data)
    return _json_text(normalized)


def _json_text(value: Any) -> str:
    """紧凑 JSON 序列化；非可序列化对象退化为 str。"""
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        return str(value)


__all__ = [
    "STATUS_CANCELLED",
    "STATUS_ERROR",
    "STATUS_SUCCESS",
    "STATUS_TIMEOUT",
    "TRUSTED_INPUT_STATUSES",
    "VALID_STATUSES",
    "boolish_false",
    "boolish_true",
    "error_text_from_payload",
    "has_failure_signal",
    "normalize_tool_result",
    "render_tool_result_for_llm",
]
