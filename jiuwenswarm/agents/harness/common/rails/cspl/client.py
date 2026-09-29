# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""CSPL HTTP client (ported from xy_channel call_api.ts / cspl_client.py)."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any, Literal

import httpx

from jiuwenswarm.agents.harness.common.rails.cspl.constants import API_URL_SUFFIX
from jiuwenswarm.common.config import get_config
from jiuwenswarm.common.utils import logger

SecurityStatus = Literal["ACCEPT", "REJECT"]

#: CSPL 不可用（未配置/网络异常/网关错误等），由调用方按兜底策略决定放行或拒绝。
class CsplScanUnavailableError(Exception):
    pass


_FALLBACK_POLICIES = frozenset({"auto", "p2_only", "strict"})


def _resolve_fallback_policy(data: dict[str, Any]) -> str:
    """解析 CSPL 兜底策略（spec 7.1：auto|p2_only|strict，默认 p2_only）。

    优先级：新键 ``fallback_policy``（含 ``security.cspl.fallback_policy`` 注入）
    > 旧 ``fail_open`` 布尔兼容（显式配置时生效：true→p2_only、false→strict）
    > 默认 p2_only。
    """
    raw_policy = str(data.get("fallback_policy") or "").strip()
    if raw_policy:
        if raw_policy in _FALLBACK_POLICIES:
            return raw_policy
        logger.warning(
            "[CsplClient] 非法 fallback_policy=%r，按 p2_only 处理", raw_policy
        )
        return "p2_only"
    if "fail_open" in data:
        return "p2_only" if data.get("fail_open") is not False else "strict"
    return "p2_only"


@dataclass
class CsplConfig:
    enabled: bool = False
    service_url: str = ""
    uid: str = ""
    api_key: str = ""
    extra_user_id: str = ""
    skill_id: str = "skill-scope"
    # skill/execute 来源标识（2026-09-02 起与桌面 skill 代理/计费上报同口径 xiaoyiWork）
    request_from: str = "xiaoyiWork"
    package_name: str = "com.huawei.hag"
    text_source: str = "question"
    timeout_ms: int = 5000
    fail_open: bool = True
    # 兜底策略：auto|p2_only|strict（默认 p2_only；auto 由运行时按档位解析）
    fallback_policy: str = "p2_only"
    scan_tool_input: bool = True
    scan_tool_output: bool = True

    @property
    def is_configured(self) -> bool:
        return bool(self.service_url.strip() and self.uid.strip() and self.api_key.strip())

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> CsplConfig:
        data = raw or {}
        return cls(
            enabled=data.get("enabled", False) is True,
            service_url=str(data.get("service_url") or "").strip(),
            uid=str(data.get("uid") or "").strip(),
            api_key=str(data.get("api_key") or "").strip(),
            extra_user_id=str(data.get("extra_user_id") or "").strip(),
            skill_id=str(data.get("skill_id") or "skill-scope"),
            request_from=str(data.get("request_from") or "xiaoyiWork"),
            package_name=str(data.get("package_name") or "com.huawei.hag"),
            text_source=str(data.get("text_source") or "question"),
            timeout_ms=int(data.get("timeout_ms") or 5000),
            fail_open=data.get("fail_open", True) is not False,
            fallback_policy=_resolve_fallback_policy(data),
            scan_tool_input=data.get("scan_tool_input", True) is not False,
            scan_tool_output=data.get("scan_tool_output", True) is not False,
        )

    @classmethod
    def load(cls) -> CsplConfig:
        config = get_config() or {}
        raw = dict(config.get("cspl") or {})

        # 新键 security.cspl.fallback_policy（热读在 rail 侧按次解析；此处注入静态初值，
        # 新键优先于 cspl.fail_open 兼容映射）。
        security_cspl = (config.get("security") or {}).get("cspl") or {}
        if isinstance(security_cspl, dict) and "fallback_policy" not in raw:
            policy = security_cspl.get("fallback_policy")
            if policy:
                raw["fallback_policy"] = policy

        xiaoyi = (config.get("channels") or {}).get("xiaoyi") or {}
        if isinstance(xiaoyi, dict):
            if not str(raw.get("api_key") or "").strip():
                raw["api_key"] = str(xiaoyi.get("api_key") or "").strip()
            if not str(raw.get("extra_user_id") or "").strip():
                raw["extra_user_id"] = str(xiaoyi.get("uid") or "").strip()

        if not str(raw.get("service_url") or "").strip():
            api_base = os.getenv("API_BASE", "").strip()
            if api_base:
                raw["service_url"] = _derive_cspl_service_url(api_base)

        return cls.from_dict(raw)


def _derive_cspl_service_url(api_base: str) -> str:
    """Strip SSE/REST suffixes from API_BASE to get CSPL service root."""
    base = api_base.strip().rstrip("/")
    for suffix in (
        "/celia-claw/v1/sse-api",
        "/celia-claw/v1/rest-api",
        "/celia-claw/v1",
    ):
        if base.endswith(suffix):
            return base[: -len(suffix)].rstrip("/")
    return base


def parse_security_result(response: dict[str, Any]) -> SecurityStatus:
    data = response.get("data")
    if not isinstance(data, dict):
        raise ValueError("Response.data is missing or not an object")

    security_result = data.get("securityResult")
    if not isinstance(security_result, str):
        raise ValueError("Response.data.securityResult is missing or not a string")

    security_result = security_result.strip()
    if security_result not in ("ACCEPT", "REJECT"):
        raise ValueError(
            f'Response.data.securityResult must be "ACCEPT" or "REJECT". Actual: "{security_result}"'
        )
    if security_result == "ACCEPT":
        return "ACCEPT"
    return "REJECT"


def _is_retcode_success(ret_code: Any) -> bool:
    return str(ret_code).strip() == "0"


def _first_text(*values: Any) -> str:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _xiaoyi_channel_fallback() -> dict[str, str]:
    config = get_config() or {}
    xiaoyi = (config.get("channels") or {}).get("xiaoyi") or {}
    if not isinstance(xiaoyi, dict):
        return {}
    return {
        "session_id": str(xiaoyi.get("last_session_id") or "").strip(),
        "task_id": str(xiaoyi.get("last_task_id") or "").strip(),
        "interaction_id": str(xiaoyi.get("last_message_id") or "").strip(),
    }


def _split_xiaoyi_task(task_id: str) -> tuple[str, str, str]:
    """Split Xiaoyi task id per xy_channel provider.ts header rules.

    Example: ``uuid&19&ea5d&0`` → session=uuid, interaction=19, trace=full task_id.
    """
    task_id = task_id.strip()
    if not task_id:
        return "", "", ""
    parts = task_id.split("&")
    session_id = parts[0]
    interaction_id = parts[1] if len(parts) > 1 else task_id
    return session_id, interaction_id, task_id


def build_behaviordetect_request(action: str, config: CsplConfig) -> dict[str, Any]:
    """Build behaviordetect.request fields; gateway reads session from HTTP headers."""
    task_id = ""
    message = ""
    trace = None

    try:
        from jiuwenswarm.common.invocation_context import get_current_invocation_context
        from jiuwenswarm.server.xiaoyi_invocation import get_xiaoyi_invocation_extension

        invocation = get_current_invocation_context()
        trace = invocation.trace if invocation is not None else None
        if trace is not None:
            task_id = trace.trace_id
        if invocation is not None and not task_id:
            xiaoyi = get_xiaoyi_invocation_extension(invocation)
            task_id = _first_text(xiaoyi.task_id if xiaoyi else None)
    except Exception:
        logger.debug("[CsplClient] invocation context unavailable", exc_info=True)

    try:
        from jiuwenswarm.server.request_context import get_current_agent_request
        from jiuwenswarm.server.xiaoyi_invocation import get_xiaoyi_request_extension

        request = get_current_agent_request()
        if request is not None:
            params = request.params if isinstance(request.params, dict) else {}
            if not task_id:
                xiaoyi = get_xiaoyi_request_extension(request)
                task_id = _first_text(xiaoyi.task_id if xiaoyi else None)
            if not message:
                message = _first_text(params.get("query"), params.get("message"))
    except Exception:
        logger.debug("[CsplClient] request_context unavailable for behaviordetect", exc_info=True)

    fallback = _xiaoyi_channel_fallback()
    if not task_id:
        task_id = fallback.get("task_id", "")

    package_name = config.package_name or "com.huawei.hag"
    session_id, interaction_id, full_task_id = _split_xiaoyi_task(task_id)
    if trace is not None:
        session_id = trace.conversation_id or session_id
        interaction_id = trace.interaction_id or interaction_id
    return {
        "checkPoint": action,
        "ansDone": 0,
        "packageName": package_name,
        "sessionID": session_id or task_id,
        "reqTime": int(time.time() * 1000),
        "taskID": full_task_id or task_id,
        "message": message or "echo hello",
        "interActionID": interaction_id or task_id,
        "userId": config.uid,
    }


def resolve_behaviordetect_context(action: str, config: CsplConfig) -> dict[str, Any]:
    """Session/trace context for diagnostics (not sent in API body; see call_api.ts)."""
    request_body = build_behaviordetect_request(action, config)
    return dict(request_body)


def _extra_user_id(config: CsplConfig) -> str:
    """Huawei account uid for extra.userId; x-uid header uses config.uid (gateway)."""
    return (config.extra_user_id or config.uid).strip()


def _build_headers(config: CsplConfig, trace_id: str) -> dict[str, str]:
    """Headers for skill/execute; sandbox E2E requires x-session-id / x-interaction-id."""
    session_id, interaction_id, full_trace = _split_xiaoyi_task(trace_id)
    hag_trace = full_trace or trace_id.replace("-", "")
    headers = {
        "x-hag-trace-id": hag_trace,
        "x-uid": config.uid,
        "x-api-key": config.api_key,
        "x-request-from": config.request_from,
        "x-skill-id": config.skill_id,
        "content-type": "application/json",
    }
    if session_id:
        headers["x-session-id"] = session_id
    if interaction_id and "&" in trace_id:
        headers["x-interaction-id"] = interaction_id
    return headers


def _build_payload(config: CsplConfig, question_text: str, action: str) -> dict[str, Any]:
    """Body aligned with xy_channel call_api.ts — extra is a JSON string, no behaviordetect."""
    return {
        "questionText": question_text,
        "textSource": config.text_source,
        "action": action,
        "extra": json.dumps({"userId": _extra_user_id(config)}, ensure_ascii=False),
    }


def _resolve_trace_id(session_id: str, action: str, config: CsplConfig) -> str:
    """Prefer Xiaoyi taskID for x-hag-trace-id when available."""
    bd = build_behaviordetect_request(action, config)
    task_id = str(bd.get("taskID") or "").strip()
    if task_id:
        return task_id
    return session_id.replace("-", "") if session_id else session_id


async def scan(
    question_text: str,
    action: str,
    session_id: str,
    config: CsplConfig | None = None,
    *,
    raise_on_error: bool = False,
) -> SecurityStatus:
    """Call CSPL API and return ACCEPT or REJECT.

    ``raise_on_error=True`` 时不可用（未配置/请求失败）抛
    ``CsplScanUnavailableError``，由调用方按兜底策略处置；默认保持原契约
    （按 ``fail_open`` 返回 ACCEPT/REJECT）。
    """
    cfg = config or CsplConfig.load()
    if not cfg.is_configured:
        logger.warning("[CsplClient] CSPL not configured (missing service_url/uid/api_key)")
        if raise_on_error:
            raise CsplScanUnavailableError("CSPL not configured")
        return "ACCEPT" if cfg.fail_open else "REJECT"

    url = f"{cfg.service_url.rstrip('/')}{API_URL_SUFFIX}"
    trace_id = _resolve_trace_id(session_id, action, cfg)
    headers = _build_headers(cfg, trace_id)
    payload = _build_payload(cfg, question_text, action)
    timeout = cfg.timeout_ms / 1000.0

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(url, json=payload, headers=headers)
            response.raise_for_status()
            body = response.json()

        logger.debug(
            "[CsplClient] scan trace_id=%s action=%s extra=%s",
            trace_id,
            action,
            payload.get("extra"),
        )

        ret_code = body.get("retCode")
        if ret_code is not None and not _is_retcode_success(ret_code):
            raise ValueError(f"API error: {body.get('retMsg', 'unknown')}")
        if body.get("errorCode"):
            raise ValueError(f"Gateway error: {body.get('errorMsg', body.get('errorCode'))}")
        if ret_code is None and body.get("code"):
            raise ValueError(f"Backend error: {body.get('desc', 'unknown')}")

        result = parse_security_result(body)
        logger.info("[CsplClient] scan action=%s status=%s", action, result)
        return result
    except Exception as exc:
        logger.warning("[CsplClient] scan failed action=%s error=%s", action, exc)
        if raise_on_error:
            raise CsplScanUnavailableError(str(exc)) from exc
        return "ACCEPT" if cfg.fail_open else "REJECT"
