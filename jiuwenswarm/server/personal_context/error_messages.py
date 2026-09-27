# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Localized public errors; internal diagnostics never become display text."""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping

_LOGGER = logging.getLogger(__name__)
_MESSAGES = {
    "config_invalid": (
        "PersonalContext 配置无效，请检查来源、时间范围和采集参数。",
        "Invalid PersonalContext configuration. Check the source, time range and collection settings.",
    ),
    "state_invalid": (
        "当前状态无法执行此操作，请等待任务结束后重试。",
        "The current state does not allow this operation. Wait for the task to finish and retry.",
    ),
    "file_error": (
        "文件读取失败，请检查文件是否存在以及访问权限。",
        "Could not read the file. Check that it exists and that access is allowed.",
    ),
    "fetch_error": (
        "数据来源采集失败，请检查来源地址、网络和授权后重试。",
        "Could not collect from the source. Check its address, network and authorization, then retry.",
    ),
    "pipeline_error": (
        "内容处理失败，请检查模型配置和可用性后重试。",
        "Content processing failed. Check the model configuration and availability, then retry.",
    ),
    "publish_error": (
        "内容写入失败，请检查磁盘空间和目录写入权限后重试。",
        "Could not write the content. Check disk space and directory permissions, then retry.",
    ),
    "timeout": (
        "操作超时，请等待当前任务结束后刷新状态并重试。",
        "The operation timed out. Wait for the current task to finish, refresh the status and retry.",
    ),
    "unknown": (
        "PersonalContext 操作失败，请重试；若仍失败，请查看服务日志。",
        "PersonalContext operation failed. Please retry; if it fails again, check the service logs.",
    ),
    "parameter_invalid": (
        "请求参数无效，请检查输入内容后重试。",
        "Invalid request parameter. Check the input and retry.",
    ),
    "positive_integer": (
        "采集间隔和每次采集上限必须为正整数。",
        "The collection interval and item limit must be positive integers.",
    ),
    "duplicate_service": (
        "来源名称已存在，请使用其他名称。",
        "The source name already exists. Use a different name.",
    ),
    "not_configured": (
        "PersonalContext 尚未配置，请先开启总开关。",
        "PersonalContext is not configured. Turn on the master switch first.",
    ),
    "collection_disabled": (
        "采集已关闭，请开启采集开关后重试。",
        "PersonalContext collection is disabled. Turn on collection and retry.",
    ),
    "unknown_service": (
        "来源不存在，请刷新来源列表后重试。",
        "The source does not exist. Refresh the source list and retry.",
    ),
    "service_running": (
        "采集任务正在执行，请停止任务或等待结束后重试。",
        "A collection task is running. Stop it or wait for it to finish, then retry.",
    ),
    "service_limit": (
        "同一数据来源最多可添加 20 项，请删除不再使用的来源后重试。",
        "Each provider supports at most 20 sources. Remove an unused source and retry.",
    ),
    "model_invalid": (
        "所选模型不可用，请重新选择已配置的模型。",
        "The selected model is unavailable. Select a configured model again.",
    ),
    "authorization_required": (
        "请先完成数据来源授权，再创建采集来源。",
        "Authorize the provider before creating a collection source.",
    ),
    "authorization_failed": (
        "数据来源授权失败，请检查凭据和网络后重新授权。",
        "Provider authorization failed. Check the credentials and network, then authorize again.",
    ),
    "authorization_unsupported": (
        "该数据来源不支持此授权方式，请检查来源类型。",
        "This provider does not support this authorization method. Check the provider type.",
    ),
    "credentials_invalid": (
        "授权参数无效，请检查数据来源和所需凭据。",
        "Invalid authorization parameters. Check the provider and required credentials.",
    ),
    "config_file_error": (
        "配置文件读写失败，请检查文件格式、磁盘空间和访问权限。",
        "Could not read or write the configuration file. Check its format, disk space and access permissions.",
    ),
}
_CODE_REASONS = {
    "154000": "config_invalid",
    "154001": "state_invalid",
    "154002": "file_error",
    "154003": "fetch_error",
    "154004": "pipeline_error",
    "154005": "publish_error",
    "154006": "timeout",
    "BAD_REQUEST": "parameter_invalid",
}
_STATUS_REASONS = dict(
    zip(
        (
            "CONTEXT_PROACTIVE_CONFIG_INVALID",
            "CONTEXT_PROACTIVE_STATE_INVALID",
            "CONTEXT_PROACTIVE_FILE_EXECUTION_ERROR",
            "CONTEXT_PROACTIVE_FETCH_EXECUTION_ERROR",
            "CONTEXT_PROACTIVE_PIPELINE_EXECUTION_ERROR",
            "CONTEXT_PROACTIVE_PUBLISH_EXECUTION_ERROR",
            "CONTEXT_PROACTIVE_RUNTIME_TIMEOUT",
        ),
        (
            "config_invalid",
            "state_invalid",
            "file_error",
            "fetch_error",
            "pipeline_error",
            "publish_error",
            "timeout",
        ),
    )
)


def localize_error(error: object, language: str, *, reason: str | None = None) -> str:
    """Resolve only explicit reason identifiers and stable Core status/code values."""
    if isinstance(error, Mapping):
        code = error.get("code")
        status = error.get("status")
    else:
        code = getattr(error, "code", None)
        status = getattr(getattr(error, "status", None), "name", None)
        reason = reason or getattr(error, "pcs_reason", None)
    if isinstance(error, str):
        # BaseError.__str__ uses this exact wire-independent numeric prefix.
        match = re.match(r"^\[(\d+)\] ", error)
        code = match.group(1) if match else None
    key = reason or _CODE_REASONS.get(str(code)) or _STATUS_REASONS.get(str(status))
    if key not in _MESSAGES:
        key = "unknown"
    return _MESSAGES[key][1 if language == "en" else 0]


def localize_payload(value: object, language: str) -> object:
    """Copy bounded status/history/authorization projections and translate errors only."""
    if isinstance(value, list):
        return [localize_payload(item, language) for item in value]
    if not isinstance(value, Mapping):
        return value
    result = {}
    for key, item in value.items():
        if key == "fetch_service_errors" and isinstance(item, Mapping):
            result[key] = {
                service: localize_error(error, language)
                for service, error in item.items()
            }
        elif key in {"last_error", "error"} and item:
            result[key] = (
                {**item, "message": localize_error(item, language)}
                if isinstance(item, Mapping)
                else localize_error(item, language)
            )
        elif key == "message" and "code" in value:
            result[key] = localize_error(value, language)
        else:
            result[key] = localize_payload(item, language)
    return result


def log_error(error: BaseException) -> None:
    """Retain a redacted diagnostic without credentials or credential-bearing causes."""
    # Header values can contain spaces (Basic auth) and semicolons (cookies).
    # Drop the rest of that diagnostic line rather than leaking a second value.
    message = re.sub(
        r"(?i)[\"']?(?:authorization|cookie)[\"']?\s*[:=][^\r\n]*",
        "[REDACTED]",
        str(error),
    ).replace("\r", " ").replace("\n", " ")
    message = re.sub(r"(?i)bearer\s+[^\s,;]+", "[REDACTED]", message)
    message = re.sub(
        r"""(?ix)["']?(?:api[_\ -]?key|(?:access[_\ -]?|refresh[_\ -]?)?token|pat|secret|password|authorization|cookie|credentials?)["']?\s*[:=]\s*(?:"[^"]*"|'[^']*'|[^\s,;]+)""",
        "[REDACTED]",
        message,
    )
    message = re.sub(r"https?://[^\s]+", "[URL REDACTED]", message)
    _LOGGER.warning(
        "PersonalContext error (%s): %s", type(error).__name__, message[:512]
    )
