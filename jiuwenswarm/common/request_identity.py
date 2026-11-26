# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""请求身份字段契约（拆仓过渡）：真源已迁 ``e2a.identity``。"""

from __future__ import annotations

from e2a.identity import (  # noqa: F401
    ROUTING_METADATA_KEY,
    TENANT_ROUTING_FIELDS,
    USER_ID_FIELD,
    WEB_IDENTITY_FIELDS,
    WEB_ROUTING_ID_FIELDS,
    apply_routing_metadata,
    normalize_routing_identity,
    web_routing_identity,
)

__all__ = [
    "ROUTING_METADATA_KEY",
    "TENANT_ROUTING_FIELDS",
    "USER_ID_FIELD",
    "WEB_IDENTITY_FIELDS",
    "WEB_ROUTING_ID_FIELDS",
    "apply_routing_metadata",
    "normalize_routing_identity",
    "web_routing_identity",
]
