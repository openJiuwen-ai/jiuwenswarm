# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""SwarmSkillHub provider search mapping guards."""

from __future__ import annotations

from typing import Any

import pytest

from jiuwenswarm.extensions.sdk.skill_source import (
    ProviderInvocationContext,
    SkillSearchRequest,
)
from jiuwenswarm.extensions.swarm_skill_hub.provider import SwarmSkillHubProvider


def _context() -> ProviderInvocationContext:
    return ProviderInvocationContext(request_id="req-1")


def _plugin(**overrides: Any) -> dict[str, Any]:
    item = {
        "asset_id": "demo-skill",
        "name": "Demo Skill",
        "latest_version_id": "1.2.3",
        "latest_version": "1.2.3",
    }
    item.update(overrides)
    return item


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("versions", "expected"),
    [
        (None, ()),
        ("1.2.3", ()),
        ({"version_id": "1.2.3"}, ()),
        ([], ()),
        ([{"version_id": "1.0.0"}, {"version": "1.1.0"}, "2.0.0", "", None], ("1.0.0", "1.1.0", "2.0.0")),
    ],
)
async def test_search_tolerates_null_or_non_list_versions(
    versions: Any,
    expected: tuple[str, ...],
) -> None:
    provider = SwarmSkillHubProvider(base_url="https://example.invalid")

    async def _fake_get_data(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        del path, params
        return {"items": [_plugin(versions=versions)], "total": 1}

    provider._get_data = _fake_get_data  # type: ignore[method-assign]
    result = await provider.search(SkillSearchRequest(page=1, page_size=20), _context())
    assert len(result.items) == 1
    assert result.items[0].all_versions == expected
