# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Enterprise file delivery goes directly from the workspace to object storage."""

from __future__ import annotations

import mimetypes
import time
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from jiuwenswarm.agents.harness.common.tools.web_file_download import (
    WebFileDownloadManager,
)
from jiuwenswarm.common.obs_files import (
    AttachmentStore,
    DOWNLOAD_TTL_SECONDS,
    download_object_key,
)
from jiuwenswarm.common.request_identity import normalize_routing_identity, web_routing_identity
from jiuwenswarm.common.utils import build_tenant_key
from jiuwenswarm.server.runtime.tenant_context import TenantContext


def build_object_file_download_info(
    file_path: Path, session_id: str, user_id: str,
    *, metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    tenant = TenantContext.current_tenant_key()
    if not tenant or not session_id:
        raise ValueError("object delivery requires a bound tenant and session")
    identity = normalize_routing_identity({"user_id": user_id}, web_routing_identity(metadata))
    if (
        not all(identity.get(key) for key in ("group_id", "bot_id", "user_id"))
        or build_tenant_key(identity["group_id"], identity["bot_id"], identity["user_id"]) != tenant
    ):
        raise ValueError("object delivery identity must match the bound tenant")
    name = file_path.name
    asset_id = uuid.uuid4().hex
    key = download_object_key(tenant, asset_id, name)
    try:
        store = AttachmentStore.from_env()
        uploaded = store.upload_file(file_path, key)
    except Exception as exc:
        raise RuntimeError("enterprise object-store delivery failed") from exc
    manager = WebFileDownloadManager.get_instance()
    expires_at = int(time.time()) + DOWNLOAD_TTL_SECONDS
    token = manager.generate_object_asset_token(
        asset_id=asset_id,
        expires_at=expires_at,
        size=uploaded["size"],
        digest=uploaded["digest"],
        file_name=name,
        session_id=session_id,
        tenant_key=tenant,
        bucket=store.bucket,
        object_key=key,
        etag=uploaded["etag"],
        routing_identity=identity,
    )
    return {
        "name": name,
        "size": uploaded["size"],
        "mime_type": mimetypes.guess_type(name)[0] or "application/octet-stream",
        "download_url": manager.generate_download_url(token, user_id),
        "download_token": token,
        "expires_at": expires_at,
    }
