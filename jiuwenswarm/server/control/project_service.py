# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Project query Control Service. Uses ProjectAdapter disk/git facades."""

from __future__ import annotations

from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.runtime.gateway_adapter.project_adapter import ProjectAdapter

CONTROL_PROJECT_METHODS = frozenset(
    {
        ReqMethod.PROJECT_INFO.value,
        ReqMethod.PROJECT_PINNED_SESSIONS.value,
        ReqMethod.PROJECT_GET_SESSIONS.value,
        ReqMethod.PROJECT_GET_CRON_SESSIONS.value,
        ReqMethod.PROJECT_CRON_RESOLVE_BINDING.value,
        ReqMethod.PROJECT_LIST.value,
        ReqMethod.PROJECT_CREATE.value,
        ReqMethod.PROJECT_RENAME.value,
        ReqMethod.PROJECT_PIN.value,
        ReqMethod.PROJECT_GIT_STATUS.value,
        ReqMethod.PROJECT_GIT_PROBE.value,
        ReqMethod.PROJECT_GIT_INIT.value,
        ReqMethod.PROJECT_GIT_SWITCH_BRANCH.value,
        ReqMethod.PROJECT_GIT_CREATE_BRANCH.value,
        ReqMethod.PROJECT_GIT_COMMIT.value,
        ReqMethod.PROJECT_GIT_PUSH.value,
        ReqMethod.PROJECT_GIT_DIFF_STATUS.value,
        ReqMethod.PROJECT_GIT_TURN_DIFF_LIST.value,
        ReqMethod.PROJECT_GIT_TURN_DIFF.value,
        ReqMethod.PROJECT_GIT_DISCARD_TURN_CHANGES.value,
        ReqMethod.PROJECT_GIT_REDO_TURN_CHANGES.value,
    }
)

_ADAPTER = ProjectAdapter()


async def handle_project_request(request: AgentRequest) -> AgentResponse:
    return await _ADAPTER.handle(request)
