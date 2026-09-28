# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""APIG模型通道的客户端：模型列表/积分/推理接入点"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from jiuwenswarm.common.auth.net import requests_request
from jiuwenswarm.common.auth.remote_config import get_config as get_remote_config

logger = logging.getLogger(__name__)

_TIMEOUT_S = 10.0

#: 额度耗尽的稳定错误码。前端据此提示"额度已用完"，而不是笼统的网络错误。
QUOTA_EXHAUSTED_CODE = "quota_exhausted"

#: 额度耗尽的 HTTP 状态码：模型网关（llm_gw）余额不足返回 402。
#: **429 不算**：那是限流（APIG.0308 流控等），过一会儿就好，说成"额度已用完"是误导。
_QUOTA_EXHAUSTED_STATUSES = frozenset({402})
_QUOTA_EXHAUSTED_CODES = (
    "quota",
    "exhaust",
    "insufficient",
    "arrears",
)

LOGIN_REQUIRED_CODE = "login_required"  # 登录失效：弹登录框
RATE_LIMITED_CODE = "rate_limited"  # 请求太频繁：稍后再试
SERVICE_UNAVAILABLE_CODE = "free_model_unavailable"  # 服务侧故障：重登没用，稍后重试或换模型

#: APIG错误码映射
_APIG_CODE_MAP = {
    # 认证信息错误——自定义认证器拒绝（id_token 缺失 / 过期 / 验签失败）就是这个码
    "apig.0305": LOGIN_REQUIRED_CODE,
    "apig.0307": LOGIN_REQUIRED_CODE,  # token 需要更新
    "apig.0316": LOGIN_REQUIRED_CODE,  # OIDC token 校验失败
    "apig.0319": LOGIN_REQUIRED_CODE,  # JWT 校验失败
    "apig.0308": RATE_LIMITED_CODE,  # 流控
}

_APIG_CODE_RE = re.compile(r"apig\.\d{4}")
#: HTTP 状态码要带上下文匹配：裸找 "429" 会命中 request_id 之类的十六进制串。
_STATUS_RE = re.compile(r"(?:error code|status(?: code)?)\s*[:=]?\s*(\d{3})")


class ApigError(Exception):

    def __init__(self, message: str, code: str = "apig_error") -> None:
        super().__init__(message)
        self.code = code


class QuotaExhausted(ApigError):

    def __init__(self, message: str = "免费积分已用完", detail: str = "") -> None:
        super().__init__(message, QUOTA_EXHAUSTED_CODE)
        self.detail = detail


@dataclass(frozen=True)
class ApigConfig:

    base_url: str
    models_path: str
    quota_path: str
    invoke_path: str

    @property
    def models_url(self) -> str:
        return _join(self.base_url, self.models_path)

    @property
    def quota_url(self) -> str:
        return _join(self.base_url, self.quota_path)

    @property
    def invoke_base_url(self) -> str:
        return _join(self.base_url, self.invoke_path)


def _join(base: str, path: str) -> str:
    return f"{base.rstrip('/')}/{path.lstrip('/')}" if path else base.rstrip("/")


def resolve_apig_config(allow_refresh: bool = True) -> ApigConfig | None:
    """从官网配置接口取 APIG 接入点。没下发 base URL 返回 ``None``表示 这套部署没有免费模型

    ``allow_refresh=False`` 给不能阻塞的调用方：只读已缓存的配置，不发 HTTP。
    """
    config = get_remote_config(allow_refresh)
    if config is None or not config.gateway.base_url:
        return None
    apig = config.gateway
    return ApigConfig(
        base_url=apig.base_url,
        models_path=apig.models_path,
        quota_path=apig.quota_path,
        invoke_path=apig.invoke_path,
    )


def build_auth_headers(id_token: str) -> dict[str, str]:
    """id_token → 发往 APIG 的鉴权头。"""
    value = (id_token or "").strip()
    if not value:
        raise ApigError("缺少 id_token", "session_expired")
    return {"Authorization": f"Bearer {value}"}


@dataclass
class ModelQuota:
    """免费积分快照。"""

    total: float = -1.0
    balance: float = -1.0
    used: float = -1.0
    #: 已花费达到预算的 LOW_BALANCE_RATIO 时为 True
    low_balance: bool = False
    exhausted: bool = False
    reset_period: str = ""
    reset_at: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    def public_view(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "balance": self.balance,
            "used": self.used,
            "low_balance": self.low_balance,
            "exhausted": self.exhausted,
            "reset_period": self.reset_period,
            "reset_at": self.reset_at,
        }


def _optional_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _pick(payload: dict[str, Any], *names: str) -> Any:
    for name in names:
        if isinstance(payload, dict) and name in payload and payload[name] is not None:
            return payload[name]
    return None


LOW_BALANCE_RATIO = 0.9


def parse_quota(payload: Any) -> ModelQuota:
    info = payload.get("info") if isinstance(payload, dict) else None
    if not isinstance(info, dict):
        return ModelQuota()

    spend = _optional_float(info.get("spend"))
    max_budget = _optional_float(info.get("max_budget"))
    balance = round(max_budget - spend, 6) if max_budget is not None and spend is not None else None

    reset_period = str(info.get("budget_duration") or "").strip()
    return ModelQuota(
        total=-1.0 if max_budget is None else max_budget,
        balance=-1.0 if balance is None else balance,
        used=-1.0 if spend is None else spend,
        low_balance=bool(max_budget and spend is not None and spend >= max_budget * LOW_BALANCE_RATIO),
        # 余额已知且 <= 0 才算用完——包括透支成负数；未知不能当成用完
        exhausted=balance is not None and balance <= 0,
        reset_period=reset_period,
        # 没有周期时 LiteLLM 也可能留着旧的 budget_reset_at，不能拿来说"何时恢复"
        reset_at=str(info.get("budget_reset_at") or "").strip() if reset_period else "",
        raw=info,
    )


def _looks_exhausted(status: int, payload: Any) -> bool:
    if status in _QUOTA_EXHAUSTED_STATUSES:
        return True
    if not isinstance(payload, dict):
        return False
    code = str(
        _pick(payload, "error_code", "code", "errorCode", "err_code") or ""
    ).lower()
    return any(fragment in code for fragment in _QUOTA_EXHAUSTED_CODES)


def _apig_error_code(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    raw = str(_pick(payload, "error_code", "code", "errorCode") or "").strip().lower()
    return raw if _APIG_CODE_RE.fullmatch(raw) else ""


def _error_detail(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    # LiteLLM / OpenAI 风格把详情套在 error 对象里：{"error": {"message": "..."}}
    nested = payload.get("error")
    if isinstance(nested, dict) and nested.get("message"):
        return str(nested["message"])
    return str(
        _pick(payload, "error_msg", "message", "error_description", "msg", "detail") or ""
    )


def _request(method: str, url: str, id_token: str) -> Any:
    headers = {"Content-Type": "application/json;charset=utf8", **build_auth_headers(id_token)}
    try:
        response = requests_request(method, url, headers=headers, timeout=_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001 — 网络细节对调用方没意义
        logger.warning("[APIG] 请求模型网关失败 %s %s: %s", method, url, exc)
        raise ApigError("无法连接模型网关，请检查网络后重试", "apig_unreachable") from exc

    try:
        payload = response.json()
    except ValueError:
        payload = None

    if _looks_exhausted(response.status_code, payload):
        raise QuotaExhausted(detail=_error_detail(payload))
    if response.status_code == 429:
        raise ApigError(_error_detail(payload) or "请求太频繁，请稍后再试", RATE_LIMITED_CODE)
    if response.status_code in (401, 403):
        code = _apig_error_code(payload)
        login_failure = (
            _APIG_CODE_MAP.get(code) == LOGIN_REQUIRED_CODE
            if code
            else response.status_code == 401
        )
        if login_failure:
            raise ApigError(_error_detail(payload) or "APIG 拒绝了当前 token", "session_expired")
        raise ApigError(
            _error_detail(payload) or f"APIG 拒绝访问（{code or response.status_code}）",
            "apig_error",
        )
    if response.status_code >= 400:
        raise ApigError(
            _error_detail(payload) or f"APIG 返回 {response.status_code}",
            "apig_error",
        )
    return payload


def fetch_quota(id_token: str, config: ApigConfig | None = None) -> ModelQuota:
    config = config or resolve_apig_config()
    if config is None:
        raise ApigError("APIG 未配置", "apig_not_configured")
    try:
        payload = _request("GET", config.quota_url, id_token)
    except QuotaExhausted as exc:
        logger.info("[APIG] 额度接口直接返回耗尽: %s", exc.detail)
        return ModelQuota(balance=0.0, exhausted=True)
    return parse_quota(payload)


#: 模型服务自己的错误（LiteLLM，以及早先的 llm_gw）。SDK 把响应体转成字符串抛出来，只能按文本认。
#: LiteLLM 预算用完回 400 + ``type: budget_exceeded``（"Budget has been exceeded! ..."），状态码认不出来。
_QUOTA_TEXT_HINTS = (
    "budget_exceeded",
    "budget has been exceeded",
    "exceededbudget",
    "insufficient_quota",
    "insufficient quota",
    "balance is exhausted",
    "欠费",
    "额度",
    "arrears",
)
_RATE_LIMIT_TEXT_HINTS = ("too many requests", "rate limit", "rate_limit", "throttl", "max parallel request", "限流")
#: 这几条出现在免费模型的错误里，说明服务端链路本身配置或上游有问题，用户侧无能为力：
#: LiteLLM 的 key 还没开户 / 过期 / 没放开这个模型，旧网关缺身份头、上游报错、模型不存在。
_SERVICE_TEXT_HINTS = (
    "invalid proxy server token",
    "expired key",
    "not allowed to access model",
    "x-user-profile",
    "upstream provider error",
    "model_not_found",
)


def classify_model_error(text: str, *, login_model: bool) -> str | None:
    if not login_model:
        return None
    lowered = (text or "").lower()
    if not lowered:
        return None

    match = _APIG_CODE_RE.search(lowered)
    if match:
        return _APIG_CODE_MAP.get(match.group(0), SERVICE_UNAVAILABLE_CODE)

    statuses = {int(s) for s in _STATUS_RE.findall(lowered)}
    if 402 in statuses or any(h in lowered for h in _QUOTA_TEXT_HINTS):
        return QUOTA_EXHAUSTED_CODE
    if 429 in statuses or any(h in lowered for h in _RATE_LIMIT_TEXT_HINTS):
        return RATE_LIMITED_CODE
    if statuses & {401, 403, 404}:
        return SERVICE_UNAVAILABLE_CODE
    if any(status >= 500 for status in statuses) or any(h in lowered for h in _SERVICE_TEXT_HINTS):
        return SERVICE_UNAVAILABLE_CODE
    return None


def fetch_models(id_token: str, config: ApigConfig | None = None) -> list[dict[str, Any]]:
    config = config or resolve_apig_config()
    if config is None:
        raise ApigError("APIG 未配置", "apig_not_configured")
    payload = _request("GET", config.models_url, id_token)
    return _extract_model_items(payload)


def _extract_model_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        items = None
        for key in ("data", "models", "items", "result"):
            value = payload.get(key)
            if isinstance(value, list):
                items = value
                break
        if items is None:
            return []
    else:
        return []
    return [item for item in items if isinstance(item, dict)]
