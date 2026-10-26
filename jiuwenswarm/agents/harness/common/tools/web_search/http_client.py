# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import requests

from jiuwenswarm.common.http_proxy_config import requests_request


def http_request(method: str, url: str, **kwargs) -> requests.Response:
    """Issue HTTP request with overlay-aware proxy settings."""
    return requests_request(method, url, **kwargs)
