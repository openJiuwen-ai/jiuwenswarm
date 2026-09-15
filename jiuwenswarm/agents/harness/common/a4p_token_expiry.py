# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Cache expiry checks matching the A4P SDK token verifier."""

import calendar
import time
from typing import Any


def token_expired(token: dict[str, Any]) -> bool:
    """Reject expired or malformed cache entries; equality matches SDK validity."""
    expire_at = str(token.get("expireAt") or "").strip()
    if not expire_at:
        return True
    try:
        expire_epoch = calendar.timegm(time.strptime(expire_at, "%Y-%m-%dT%H:%M:%SZ"))
    except ValueError:
        return True
    return time.time() > expire_epoch
