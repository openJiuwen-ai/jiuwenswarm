# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shared Ark address validation must protect both discovery and generation."""

import pytest

from jiuwenswarm.common.volcengine_endpoints import normalize_ark_api_base


@pytest.mark.parametrize("host", ["ark.cn-beijing.volces.com", "ark.ap-southeast.bytepluses.com"])
def test_ark_base_preserves_configured_region(host):
    expected = f"https://{host}/api/v3"
    assert normalize_ark_api_base(f"https://{host}") == expected
    assert normalize_ark_api_base(f"{expected}/") == expected
    assert normalize_ark_api_base(f"HTTPS://{host.upper()}/api/v3") == expected


@pytest.mark.parametrize("base", [
    "https://proxy.example/api/v3",
    "https://ark.cn-beijing.volces.com.evil.example/api/v3",
    "https://ark.cn-beijing.volces.com@evil.example/api/v3",
    "http://ark.cn-beijing.volces.com/api/v3",
    "https://ark.cn-beijing.volces.com:8443/api/v3",
    "https://ark.cn-beijing.volces.com/api/coding/v3",
    "https://ark.cn-beijing.volces.com/api/compatible",
    "https://ark.cn-beijing.volces.com/API/V3",
    "https://ark.cn-beijing.volces.com/api/v3?redirect=1",
])
def test_ark_generation_rejects_other_products_and_non_official_addresses(base):
    with pytest.raises(ValueError, match="official regional Ark"):
        normalize_ark_api_base(base)
