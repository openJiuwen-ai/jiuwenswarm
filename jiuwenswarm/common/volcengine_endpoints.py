# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Official Ark endpoints shared by model presets, discovery and generation."""

import re
from urllib.parse import urlsplit

ARK_API_BASE = "https://ark.cn-beijing.volces.com/api/v3"
ARK_CODING_API_BASE = "https://ark.cn-beijing.volces.com/api/coding/v3"
ARK_ANTHROPIC_BASE = "https://ark.cn-beijing.volces.com/api/compatible"
ARK_CODING_ANTHROPIC_BASE = "https://ark.cn-beijing.volces.com/api/coding"


def normalize_ark_api_base(api_base: str) -> str:
    """Resolve a regular regional Ark base, excluding Coding Plan and proxies.

    Generation and its model catalogue use the same regional /api/v3 API.
    Accept a host root for older configurations, but never reinterpret a
    Coding Plan URL or switch the configured region.
    """
    parsed = urlsplit(api_base.strip().rstrip("/"))
    host = parsed.hostname or ""
    if (
        parsed.scheme != "https"
        or parsed.netloc.lower() != host
        or not re.fullmatch(r"ark\.[\w-]+\.(?:volces\.com|bytepluses\.com)", host)
        or parsed.path not in {"", "/api/v3"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Use an official regional Ark HTTPS /api/v3 address; Coding Plan does not support generation")
    return f"https://{host}/api/v3"
