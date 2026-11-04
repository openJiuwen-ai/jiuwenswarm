"""``skills.import_upload`` 的企业版上传资格守卫。

只在企业版生效。凭据缺失、Manager 不可达或响应不合法都失败关闭。
聊天产物保存走 ``skills.import_local + download_token``，不在本守卫范围内。
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

logger = logging.getLogger(__name__)

_GUARDED_METHOD = "skills.import_upload"


async def assert_skill_upload_permission(
    *,
    authorization: str | None,
    jiuwenclaw_id: str | None,
    method: str,
    params: Mapping[str, Any],
) -> None:
    from gateway.enterprise import is_enterprise
    from gateway.enterprise.capabilities.skill_upload.client import (
        CHECK_UNAVAILABLE,
        PERMISSION_REQUIRED,
        SkillUploadPermissionError,
        query_upload_permission,
    )

    if not is_enterprise() or method != _GUARDED_METHOD:
        return
    token = str(authorization or "").strip()
    cluster = str(jiuwenclaw_id or "").strip()
    if not token.lower().startswith("bearer ") or not cluster:
        logger.warning(
            "[SkillUpload] reject import_upload: missing credentials cluster=%s", bool(cluster)
        )
        raise SkillUploadPermissionError(
            CHECK_UNAVAILABLE,
            "skill upload permission cannot be verified: missing credentials or instance id",
        )
    data = await query_upload_permission(authorization=token, jiuwenclaw_id=cluster)
    if not data.get("allowed"):
        raise SkillUploadPermissionError(
            PERMISSION_REQUIRED, "current user is not allowed to upload skills"
        )
