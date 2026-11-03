# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Run-local credentials and policy cannot leak into another execution."""
import asyncio
from dataclasses import replace

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.runtime.run_context import CURRENT_RUN_CONTEXT, RunContext


@pytest.mark.asyncio
async def test_same_request_id_in_different_sessions_does_not_share_credentials():
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter

    async def run(session_id):
        credential = {"private": "not-for-logs", "owner": session_id}
        request = AgentRequest(request_id="same-id", channel_id="web", session_id=session_id, params={})
        context = RunContext(request_id=request.request_id, session_id=session_id, credential_context=credential)
        token = CURRENT_RUN_CONTEXT.set(context)
        try:
            await asyncio.sleep(0)
            assert JiuWenSwarmDeepAdapter._scoped_login_auth(request) is credential
            assert JiuWenSwarmDeepAdapter._scoped_login_auth(replace(request, session_id="foreign")) is None
            assert "not-for-logs" not in repr(context)
        finally:
            CURRENT_RUN_CONTEXT.reset(token)

    await asyncio.gather(run("session-a"), run("session-b"))
    assert CURRENT_RUN_CONTEXT.get() is None
