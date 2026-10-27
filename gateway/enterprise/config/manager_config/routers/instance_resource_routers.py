# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""实例 Agent 资源同步 API。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from ..core.instance_resource.instance_agent_resource import InstanceAgentResourceService
from ..schemas.common_schemas import ResponseModel
from ..schemas.instance_resource_schemas import InstanceAgentResourceUpsertRequest
from ..schemas.sync_schemas import SyncEnvelopeOnlyBody, make_sync_body
from .deps import build_sync_context, sync_write_data
from .runtime_notify import trigger_runtime_config_update

instance_resource_router = APIRouter()

AgentResourceUpsertSyncBody = make_sync_body(
    "AgentResourceUpsertSyncBody",
    InstanceAgentResourceUpsertRequest,
)


def _http_exc(exc: ValueError) -> HTTPException:
    detail = str(exc)
    status = 404 if "not found" in detail else 400
    return HTTPException(status_code=status, detail=detail)


@instance_resource_router.post("/instance-agent-resources", response_model=ResponseModel)
async def upsert_instance_agent_resource(
    request: Request,
    body: AgentResourceUpsertSyncBody,
):
    sync = await build_sync_context(body, request.method)
    try:
        result = await InstanceAgentResourceService().upsert(
            sync.business,
        )
    except ValueError as exc:
        raise _http_exc(exc) from exc
    trigger_runtime_config_update()
    return ResponseModel(code=200, message="success", data=sync_write_data(sync, result))


@instance_resource_router.delete(
    "/instance-agent-resources/{resource_id}",
    response_model=ResponseModel,
)
async def delete_instance_agent_resource(
    request: Request,
    resource_id: str,
    body: SyncEnvelopeOnlyBody,
):
    sync = await build_sync_context(body, request.method)
    try:
        await InstanceAgentResourceService().delete(
            resource_id,
        )
    except ValueError as exc:
        raise _http_exc(exc) from exc
    trigger_runtime_config_update()
    return ResponseModel(code=200, message="success", data=sync_write_data(sync, None))
