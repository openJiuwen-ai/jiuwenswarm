# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""配额同步 API（工作区配额策略投影 / 用量只读）。"""

from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query

from ..core.quota import WorkspaceQuotaPolicyService, WorkspaceQuotaUsageService
from ..schemas.common_schemas import ResponseModel
from ..schemas.quota_schemas import PolicyUpsertRequest, TeamQuotaPolicyUpsertRequest
from ..schemas.sync_schemas import make_sync_body
from .deps import (
    SyncContext,
    VerifySyncEnvelopeOnly,
    sync_write_data,
    verify_sync,
)

quota_router = APIRouter()

PolicySyncBody = make_sync_body("PolicySyncBody", PolicyUpsertRequest)


@quota_router.put("/workspace-quota/policies", response_model=ResponseModel)
async def upsert_workspace_quota_policy(
    sync: Annotated[SyncContext, Depends(verify_sync(PolicySyncBody))],
):
    """工作区配额策略投影。管理面不直接调用。"""
    try:
        await WorkspaceQuotaPolicyService().upsert(sync.business)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ResponseModel(code=200, message="success", data=sync_write_data(sync, None))


@quota_router.delete(
    "/workspace-quota/policies/{policy_id}", response_model=ResponseModel
)
async def delete_workspace_quota_policy(
    policy_id: str,
    sync: VerifySyncEnvelopeOnly,
):
    try:
        await WorkspaceQuotaPolicyService().delete(policy_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ResponseModel(code=200, message="success", data=sync_write_data(sync, None))


@quota_router.get("/workspace-quota/usage", response_model=ResponseModel)
async def list_workspace_quota_usage(
    user_id: Annotated[str | None, Query(max_length=64)] = None,
    group_id: Annotated[str | None, Query(max_length=64)] = None,
    bot_id: Annotated[str | None, Query(max_length=64)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
):
    """本集群用量缓存只读。Manager ``gateway_request`` 调用；不访问 Agent。"""
    try:
        data = await WorkspaceQuotaUsageService().list_usage(
            user_id=user_id,
            group_id=group_id,
            bot_id=bot_id,
            limit=limit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return ResponseModel(code=200, message="success", data=data)


TeamQuotaSyncBody = make_sync_body("TeamQuotaSyncBody", TeamQuotaPolicyUpsertRequest)


def _team_quota_service():
    from jiuwenswarm.enterprise.team.quota import get_team_quota_service

    return get_team_quota_service()


@quota_router.put("/team-quota/policies", response_model=ResponseModel)
async def upsert_team_quota_policy(
    sync: Annotated[SyncContext, Depends(verify_sync(TeamQuotaSyncBody))],
):
    """团队配额策略投影。管理面不直接写 AgentServer。"""
    try:
        req = TeamQuotaPolicyUpsertRequest.model_validate(sync.business)
        row = await asyncio.to_thread(_team_quota_service().upsert_policy, req.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ResponseModel(code=200, message="success", data=sync_write_data(sync, row))


@quota_router.delete(
    "/team-quota/policies/{policy_id}", response_model=ResponseModel
)
async def delete_team_quota_policy(
    policy_id: str,
    sync: VerifySyncEnvelopeOnly,
):
    try:
        await asyncio.to_thread(_team_quota_service().delete_policy, policy_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ResponseModel(code=200, message="success", data=sync_write_data(sync, None))


@quota_router.get("/team-quota/usage", response_model=ResponseModel)
async def list_team_quota_usage():
    """本集群团队占用。含按组织汇总的 ``orgs``。"""
    try:
        data = await asyncio.to_thread(_team_quota_service().list_usage)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return ResponseModel(code=200, message="success", data=data)
