# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Skill 上传资格校验（企业版 manager 授权）。

归属：无状态纯组件（仅依赖 stdlib + env），下沉到 ``gateway.common`` 作为
唯一真源。母仓 ``jiuwenswarm.channels.web.skill_upload_permission`` 提供
转发别名（``sys.modules`` 重定向），gateway 与母仓两侧均经此真源消费。
允许跨层方向：``jiuwenswarm.*`` → ``gateway.common.skill_upload_permission``
（见 ``scripts/check_layering.py`` 的 ``ALLOWED_CROSS_LAYER_PREFIXES``）。

``POST /file-api/skills/upload-temp`` / ``/file-api/skills/import`` /
``/file-api/skills/create-from-knowledge`` 是写入口，在管理面统一授权
（skill_upload_permission 记录：管理员直授 + 审批通过）后按当前用户放行。

启用方式（三端点受同一开关控制）：
- ``SKILL_UPLOAD_PERMISSION_ENFORCE`` 设为 ``1``/``true``/``yes``（默认关闭，
  保险起见个人版/未配置部署不启用）；
- ``MANAGER_API_BASE_URL`` 指向 Manager Server（如 ``http://manager:8000``）。

用户身份来自请求的 ``Authorization: Bearer <jwt>``（前端 identity headers
注入）或前端同步的 ``openjiuwen_access_token`` Cookie；校验方式是携带该令牌
请求 Manager ``GET /v1/user-console/skills/upload-permission``（本地 RS256
验签 + 授权表判定），结果按 token 短缓存。

@Enable 后一律 fail-closed：无令牌 401、未授权 403、Manager 不可达 503；
任何校验异常都不允许代码路径绕过该检查。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

_ENFORCE_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_CHECK_PATH = "/api/v1/user-console/skills/upload-permission"
_INTERNAL_CHECK_PATH = "/api/v1/internal/skills/upload-permission/check"
_HTTP_TIMEOUT_S = 5.0
_CACHE_TTL_S = 30.0
_CACHE_MAX_ENTRIES = 1024


class SkillUploadPermissionError(Exception):
    """校验未放行：由调用方按 (status, payload) 回写 HTTP 响应。"""

    def __init__(self, status: int, payload: dict[str, Any]) -> None:
        super().__init__(str(payload.get("error") or status))
        self.status = status
        self.payload = payload


@dataclass(frozen=True)
class _Verdict:
    allowed: bool
    expires_at: float


_cache_lock = threading.Lock()
_cache: dict[str, _Verdict] = {}


def enforcement_enabled() -> bool:
    """Skill 上传资格强制校验是否启用（默认关闭，环境变量显式打开）。"""
    return str(os.getenv("SKILL_UPLOAD_PERMISSION_ENFORCE") or "").strip().lower() in _ENFORCE_TRUE_VALUES


def _manager_base_url() -> str:
    return (os.getenv("MANAGER_API_BASE_URL") or "").strip().rstrip("/")


def _bearer_token(authorization: str, access_cookie: str | None) -> str:
    header = str(authorization or "").strip()
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return str(access_cookie or "").strip()


def _check_url() -> str:
    return f"{_manager_base_url()}{_CHECK_PATH}"


JIUWENCLAW_ID_COOKIE = "jiuwenclaw_id"


def _request_allowed(token: str, jiuwenclaw_id: str = "") -> bool:
    """请求 Manager 查询当前用户上传资格；失败一律抛 ``SkillUploadPermissionError``。

    ``jiuwenclaw_id`` 透传为 ``Cookie: jiuwenclaw_id=...``，与浏览器请求一致——
    Manager 的用户面端点从该 cookie 解析企业实例（``cluster_id``）。
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }
    cid = str(jiuwenclaw_id or "").strip()
    if cid:
        headers["Cookie"] = f"{JIUWENCLAW_ID_COOKIE}={cid}"
    try:
        request = urllib.request.Request(_check_url(), headers=headers)
        with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT_S) as response:
            raw = response.read(65536)
            status = int(response.status)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            logger.warning(
                "[skill_upload_permission] manager 维权失败 status=%s token=%s...",
                exc.code,
                hashlib.sha256(token.encode("utf-8")).hexdigest()[:8],
            )
            raise SkillUploadPermissionError(
                exc.code,
                {
                    "code": "SKILL_UPLOAD_PERMISSION_REQUIRED",
                    "message": "未获得 Skill 上传资格，请在管理面申请或联系管理员授权",
                    "error": "skill upload permission required",
                },
            ) from exc
        raise SkillUploadPermissionError(
            503,
            {
                "code": "SKILL_UPLOAD_PERMISSION_CHECK_UNAVAILABLE",
                "message": "Skill 上传资格校验服务不可用，请稍后重试",
                "error": "skill upload permission service unavailable",
            },
        ) from exc
    except (OSError, ValueError) as exc:
        logger.warning("[skill_upload_permission] manager unreachable: %s", exc)
        raise SkillUploadPermissionError(
            503,
            {
                "code": "SKILL_UPLOAD_PERMISSION_CHECK_UNAVAILABLE",
                "message": "Skill 上传资格校验服务不可用，请稍后重试",
                "error": "skill upload permission service unavailable",
            },
        ) from exc
    if status != 200:
        raise SkillUploadPermissionError(
            503,
            {
                "code": "SKILL_UPLOAD_PERMISSION_CHECK_UNAVAILABLE",
                "message": "Skill 上传资格校验服务异常，请稍后重试",
                "error": "skill upload permission service unavailable",
            },
        )
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        logger.warning("[skill_upload_permission] manager 响应不是合法 JSON: %s", exc)
        raise SkillUploadPermissionError(
            503,
            {
                "code": "SKILL_UPLOAD_PERMISSION_CHECK_UNAVAILABLE",
                "message": "Skill 上传资格校验服务异常，请稍后重试",
                "error": "skill upload permission service unavailable",
            },
        ) from exc
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict):
        raise SkillUploadPermissionError(
            503,
            {
                "code": "SKILL_UPLOAD_PERMISSION_CHECK_UNAVAILABLE",
                "message": "Skill 上传资格校验服务异常，请稍后重试",
                "error": "skill upload permission service unavailable",
            },
        )
    return bool(data.get("allowed"))


def check_skill_upload_permission(
    authorization: str | None,
    access_cookie: str | None = None,
    jiuwenclaw_id: str = "",
) -> None:
    """校验当前请求的 Skill 上传资格；未放行抛 ``SkillUploadPermissionError``。

    Args:
        authorization: 请求 ``Authorization`` 头（``Bearer <jwt>``）。
        access_cookie: 前端同步的 ``openjiuwen_access_token`` Cookie 值，
            经此兜底个人桌面端注入 token。
        jiuwenclaw_id: 当前企业实例 id（来自 ``jiuwenclaw_id`` cookie）；
            Manager 用户面端点从该值解析 ``cluster_id``，缺失时 Manager 短路
            返回 ``allowed=false``。

    Raises:
        SkillUploadPermissionError: 401 / 403 / 503（fail-closed）。
    """
    if not enforcement_enabled():
        return
    token = _bearer_token(authorization, access_cookie)
    if not token:
        raise SkillUploadPermissionError(
            401,
            {
                "code": "SKILL_UPLOAD_PERMISSION_REQUIRED",
                "message": "缺少访问令牌，无法校验 Skill 上传资格",
                "error": "missing access token",
            },
        )
    cid = str(jiuwenclaw_id or "").strip()
    key = hashlib.sha256(f"{token}:{cid}".encode("utf-8")).hexdigest()
    now = time.monotonic()
    with _cache_lock:
        cached = _cache.get(key)
        if cached is not None and cached.expires_at > now:
            if cached.allowed:
                return
            raise SkillUploadPermissionError(
                403,
                {
                    "code": "SKILL_UPLOAD_PERMISSION_REQUIRED",
                    "message": "未获得 Skill 上传资格，请在管理面申请或联系管理员授权",
                    "error": "skill upload permission required",
                },
            )
    allowed = _request_allowed(token, cid)
    with _cache_lock:
        if len(_cache) >= _CACHE_MAX_ENTRIES:
            _cache.clear()
        _cache[key] = _Verdict(allowed=allowed, expires_at=now + _CACHE_TTL_S)
    if allowed:
        return
    raise SkillUploadPermissionError(
        403,
        {
            "code": "SKILL_UPLOAD_PERMISSION_REQUIRED",
            "message": "未获得 Skill 上传资格，请在管理面申请或联系管理员授权",
            "error": "skill upload permission required",
        },
    )


def reset_cache_for_test() -> None:
    """清空校验缓存（单元测试用）。"""
    with _cache_lock:
        _cache.clear()


def _internal_token() -> str:
    return str(os.getenv("MANAGER_INTERNAL_TOKEN") or "").strip()


def check_skill_upload_permission_by_user(*, cluster_id: str, user_id: str) -> None:
    """按 user_id 复查 Skill 上传资格（WS RPC ``skills.import_upload`` 安装前用）。

    WS 链路没有原始 JWT，无法复用 ``check_skill_upload_permission``（按 token 查）；
    此函数改走 Manager 内部接口 ``/v1/internal/skills/upload-permission/check``，
    以 ``X-Internal-Token`` 共享密钥鉴权 + service 层直查授权表。

    Args:
        cluster_id: 企业实例 id（即 ``request.bot_id``）。
        user_id: 当前用户 id（即 ``request.user_id``，由 AgentServer 注入）。

    Raises:
        SkillUploadPermissionError: 401/403/503（fail-closed）。
    """
    if not enforcement_enabled():
        return
    cid = str(cluster_id or "").strip()
    uid = str(user_id or "").strip()
    if not cid or not uid:
        raise SkillUploadPermissionError(
            403,
            {
                "code": "SKILL_UPLOAD_PERMISSION_REQUIRED",
                "message": "未获得 Skill 上传资格，请在管理面申请或联系管理员授权",
                "error": "skill upload permission required",
            },
        )
    token = _internal_token()
    if not token:
        logger.warning("[skill_upload_permission] MANAGER_INTERNAL_TOKEN 未配置，无法复查 import_upload 资格")
        raise SkillUploadPermissionError(
            503,
            {
                "code": "SKILL_UPLOAD_PERMISSION_CHECK_UNAVAILABLE",
                "message": "Skill 上传资格校验服务不可用，请稍后重试",
                "error": "skill upload permission service unavailable",
            },
        )
    url = (
        f"{_manager_base_url()}{_INTERNAL_CHECK_PATH}"
        f"?cluster_id={urllib.parse.quote(cid)}&user_id={urllib.parse.quote(uid)}"
    )
    try:
        request = urllib.request.Request(
            url,
            headers={
                "X-Internal-Token": token,
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT_S) as response:
            raw = response.read(65536)
            status = int(response.status)
    except urllib.error.HTTPError as exc:
        if exc.code == 403:
            logger.info(
                "[skill_upload_permission] import_upload 复查拒绝 cluster_id=%s user_id=%s...",
                cid,
                uid,
            )
            raise SkillUploadPermissionError(
                403,
                {
                    "code": "SKILL_UPLOAD_PERMISSION_REQUIRED",
                    "message": "未获得 Skill 上传资格，请在管理面申请或联系管理员授权",
                    "error": "skill upload permission required",
                },
            ) from exc
        raise SkillUploadPermissionError(
            503,
            {
                "code": "SKILL_UPLOAD_PERMISSION_CHECK_UNAVAILABLE",
                "message": "Skill 上传资格校验服务不可用，请稍后重试",
                "error": "skill upload permission service unavailable",
            },
        ) from exc
    except (OSError, ValueError) as exc:
        logger.warning("[skill_upload_permission] manager unreachable: %s", exc)
        raise SkillUploadPermissionError(
            503,
            {
                "code": "SKILL_UPLOAD_PERMISSION_CHECK_UNAVAILABLE",
                "message": "Skill 上传资格校验服务不可用，请稍后重试",
                "error": "skill upload permission service unavailable",
            },
        ) from exc
    if status != 200:
        raise SkillUploadPermissionError(
            503,
            {
                "code": "SKILL_UPLOAD_PERMISSION_CHECK_UNAVAILABLE",
                "message": "Skill 上传资格校验服务异常，请稍后重试",
                "error": "skill upload permission service unavailable",
            },
        )
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        logger.warning("[skill_upload_permission] manager 响应不是合法 JSON: %s", exc)
        raise SkillUploadPermissionError(
            503,
            {
                "code": "SKILL_UPLOAD_PERMISSION_CHECK_UNAVAILABLE",
                "message": "Skill 上传资格校验服务异常，请稍后重试",
                "error": "skill upload permission service unavailable",
            },
        ) from exc
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict) or not bool(data.get("allowed")):
        raise SkillUploadPermissionError(
            403,
            {
                "code": "SKILL_UPLOAD_PERMISSION_REQUIRED",
                "message": "未获得 Skill 上传资格，请在管理面申请或联系管理员授权",
                "error": "skill upload permission required",
            },
        )


__all__ = (
    "SkillUploadPermissionError",
    "check_skill_upload_permission",
    "check_skill_upload_permission_by_user",
    "enforcement_enabled",
    "reset_cache_for_test",
)
