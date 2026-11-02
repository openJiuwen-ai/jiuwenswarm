"""HTTP Web puts X-Group/Bot/User into metadata.query; extract_triple must use them."""

from __future__ import annotations

import pytest

from jiuwenswarm.common import utils
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.enterprise.tenant.tenant_agent_pool import TenantAgentPool


@pytest.mark.parametrize(
    "metadata,user_id,expected_parts",
    [
        (
            {
                "routing": {
                    "group_id": "df0b704c-2c3f-4f6a-a998-4c03d569ec67",
                    "bot_id": "169274f4-2f12-4f6a-8993-e1a67bfb6773",
                },
                "user_id": "user1",
            },
            "user1",
            (
                "df0b704c-2c3f-4f6a-a998-4c03d569ec67",
                "169274f4-2f12-4f6a-8993-e1a67bfb6773",
                "user1",
            ),
        ),
        # routing missing (E2A loss): recover from HTTP query bag (list-valued).
        (
            {
                "query": {
                    "user_id": ["user1"],
                    "group_id": ["df0b704c-2c3f-4f6a-a998-4c03d569ec67"],
                    "bot_id": ["169274f4-2f12-4f6a-8993-e1a67bfb6773"],
                },
                "method": "history.get",
                "user_id": "user1",
            },
            "user1",
            (
                "df0b704c-2c3f-4f6a-a998-4c03d569ec67",
                "169274f4-2f12-4f6a-8993-e1a67bfb6773",
                "user1",
            ),
        ),
    ],
)
def test_extract_tenant_key_enterprise_from_routing_or_query(
    monkeypatch, metadata, user_id, expected_parts
):
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    request = AgentRequest(
        request_id="r",
        channel_id="web",
        session_id="web_sid",
        user_id=user_id,
        metadata=metadata,
    )
    assert TenantAgentPool.extract_triple(request) == expected_parts
    assert TenantAgentPool.extract_tenant_key(request) == utils.build_tenant_key(
        *expected_parts
    )
    # Must not collapse to default::default::user1
    assert TenantAgentPool.extract_tenant_key(request) != utils.build_tenant_key(
        "default", "default", "user1"
    )


def test_extract_tenant_key_personal_ignores_http_query(monkeypatch):
    monkeypatch.setenv("JIUWENSWARM_EDITION", "personal")
    request = AgentRequest(
        request_id="r",
        channel_id="web",
        user_id="user1",
        metadata={
            "query": {
                "user_id": ["user1"],
                "group_id": ["g"],
                "bot_id": ["b"],
            },
            "routing": {"group_id": "g", "bot_id": "b"},
        },
    )
    assert TenantAgentPool.extract_tenant_key(request) == utils.build_tenant_key()
