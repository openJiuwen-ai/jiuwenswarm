"""Manager 资格查询客户端：透传用户 JWT，不缓存结果。"""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)

PERMISSION_REQUIRED = "SKILL_UPLOAD_PERMISSION_REQUIRED"
CHECK_UNAVAILABLE = "SKILL_UPLOAD_PERMISSION_CHECK_UNAVAILABLE"

_DEFAULT_TIMEOUT_S = 5.0


class SkillUploadPermissionError(Exception):
    """上传资格拒绝；``code`` 与 Gateway HTTP 错误码一致。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _manager_base() -> str:
    from gateway.enterprise.config.manager_config.infrastructure.config import (
        get_settings,
    )

    return (get_settings().gateway_manager_http_url or "").strip().rstrip("/")


def _timeout() -> float:
    import os

    raw = (os.getenv("GATEWAY_MANAGER_PERMISSION_TIMEOUT") or "").strip()
    if not raw:
        return _DEFAULT_TIMEOUT_S
    try:
        value = float(raw)
    except ValueError:
        return _DEFAULT_TIMEOUT_S
    return value if value > 0 else _DEFAULT_TIMEOUT_S


async def query_upload_permission(
    *,
    authorization: str,
    jiuwenclaw_id: str | None,
) -> dict:
    """查询当前用户的上传资格；失败一律抛"暂不可用"（失败关闭）。"""
    base = _manager_base()
    if not base:
        raise SkillUploadPermissionError(
            CHECK_UNAVAILABLE, "manager endpoint is not configured"
        )
    params = {"jiuwenclaw_id": jiuwenclaw_id} if jiuwenclaw_id else None
    try:
        async with httpx.AsyncClient(timeout=_timeout(), trust_env=False) as client:
            response = await client.get(
                f"{base}/api/v1/user-console/skills/upload-permission",
                params=params,
                headers={"authorization": authorization, "accept": "application/json"},
            )
    except httpx.HTTPError as exc:
        logger.warning("[SkillUpload] permission query failed: %s", exc)
        raise SkillUploadPermissionError(
            CHECK_UNAVAILABLE, "permission service unreachable"
        ) from exc
    if response.status_code != 200:
        logger.warning(
            "[SkillUpload] permission query status=%s", response.status_code
        )
        raise SkillUploadPermissionError(
            CHECK_UNAVAILABLE, f"permission service returned {response.status_code}"
        )
    try:
        body = response.json()
    except ValueError as exc:
        raise SkillUploadPermissionError(
            CHECK_UNAVAILABLE, "permission service returned invalid json"
        ) from exc
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict) or "allowed" not in data:
        raise SkillUploadPermissionError(
            CHECK_UNAVAILABLE, "permission response missing allowed"
        )
    return data
