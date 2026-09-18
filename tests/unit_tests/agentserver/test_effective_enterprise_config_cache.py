# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""EffectiveEnterpriseConfig 装配结果进程缓存."""

from __future__ import annotations

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest


@pytest.fixture(autouse=True)
def _clear_enterprise_config_caches() -> None:
    from jiuwenswarm.server.runtime.enterprise_config.loader import (
        invalidate_enterprise_config_caches,
    )

    invalidate_enterprise_config_caches()
    yield
    invalidate_enterprise_config_caches()


@pytest.mark.asyncio
async def test_effective_config_assembly_cache_hits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.server.runtime.enterprise_config import db_queries
    from jiuwenswarm.server.runtime.enterprise_config.loader import (
        DEFAULT_AGENT_LOAD_SLOTS,
        load_effective_enterprise_config,
    )

    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    monkeypatch.setenv("JIUWENCLAW_ID", "sp-demo")

    resource_id = "bot_main"
    ref_template_id = "agent-tmpl-1"
    model_id = "22222222-2222-4222-8222-222222222222"
    list_calls: list[str] = []

    async def _list_records(
        table: str,
        *,
        filters: dict | None = None,
        order_by: str = "",
    ) -> list[dict]:
        list_calls.append(table)
        scoped = dict(filters or {})
        if table == "instance_agent_resource":
            return [
                {
                    "jiuwenclaw_id": "sp-demo",
                    "resource_id": resource_id,
                    "ref_template_id": ref_template_id,
                    "enabled": True,
                }
            ]
        if table == "agent_template":
            return [
                {
                    "template_id": ref_template_id,
                    "enabled": True,
                    "template_ref": {"default_model": [model_id]},
                }
            ]
        return []

    async def _fetch_templates_by_slot(slot: str, template_ids: list[str]) -> list[dict]:
        list_calls.append(f"slot:{slot}")
        return [
            {
                "template_id": tid,
                "model_id": "m",
                "api_base": "https://example.com",
                "api_key": "k",
            }
            for tid in template_ids
        ]

    monkeypatch.setattr(db_queries, "list_records", _list_records)
    monkeypatch.setattr(db_queries, "fetch_templates_by_slot", _fetch_templates_by_slot)

    def _req(user: str) -> AgentRequest:
        return AgentRequest(
            request_id=f"req-{user}",
            params={},
            metadata={
                "user_id": user,
                "routing": {
                    "bot_id": resource_id,
                    "group_id": "g1",
                },
            },
        )

    first = await load_effective_enterprise_config(_req("u1"), DEFAULT_AGENT_LOAD_SLOTS)
    calls_after_first = len(list_calls)
    second = await load_effective_enterprise_config(_req("u2"), DEFAULT_AGENT_LOAD_SLOTS)

    assert first is not None and second is not None
    assert first.resource_id == resource_id
    assert second.resource_id == resource_id
    # 不同请求的 routing 应各自正确
    assert first.routing.user_id == "u1"
    assert second.routing.user_id == "u2"
    # 第二次整份装配命中缓存，不再打库
    assert len(list_calls) == calls_after_first
    # 调用方改脏不影响缓存
    first.models["default_model"][0]["api_key"] = "mutated"
    third = await load_effective_enterprise_config(_req("u3"), DEFAULT_AGENT_LOAD_SLOTS)
    assert third is not None
    assert third.models["default_model"][0]["api_key"] == "k"
