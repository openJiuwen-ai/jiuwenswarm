# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""Generic skill download HMAC helper for SkillManager.

Hub-private SkillHub artifact trust / verify lives in
``jiuwenswarm.extensions.swarm_skill_hub.hub_verify``.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def verify_skill_download_hmac(
    content: bytes,
    signature: str,
    *,
    secret: str | None = None,
) -> bool:
    """HMAC-SHA256(content) hex; key is ``SKILL_DOWNLOAD_HMAC_SECRET``."""
    key = (secret if secret is not None else os.getenv("SKILL_DOWNLOAD_HMAC_SECRET", "")).strip()
    expected = str(signature or "").strip().lower()
    if not key or not expected or not content:
        return False
    if not _SHA256_RE.fullmatch(expected):
        return False
    digest = hmac.new(key.encode("utf-8"), content, hashlib.sha256).hexdigest().lower()
    return hmac.compare_digest(digest, expected)


__all__ = [
    "verify_skill_download_hmac",
]
