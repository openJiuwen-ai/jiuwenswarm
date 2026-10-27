# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""应用配置同步 API（对齐 Manager application_config_routers：显式 Body schema）。"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from ..core.application_config.audit_log_config import AuditLogConfigService
from ..core.application_config.cron_policy import CronPolicyService
from ..core.application_config.package_allowlist import PackageAllowlistService
from ..core.application_config.log_masking_rule import LogMaskingRuleService
from ..core.application_config.logging_config import LoggingConfigService
from ..core.application_config.memory_config import MemoryConfigService
from ..core.application_config.task_memory_config import TaskMemoryConfigService
from ..schemas.application_config_schemas import (
    AuditLogUpsertRequest,
    CronPolicyUpsertRequest,
    PackageAllowlistUpsertRequest,
    LoggingConfigUpsertRequest,
    LogMaskingRuleCreateRequest,
    LogMaskingRuleUpdateRequest,
    MemoryConfigUpsertRequest,
    TaskMemoryUpsertRequest,
)
from ..schemas.common_schemas import ResponseModel
from ..schemas.sync_schemas import make_sync_body
from .deps import (
    SyncContext,
    VerifySyncEnvelopeOnly,
    sync_write_data,
    verify_sync,
)
from .runtime_notify import trigger_runtime_config_update

application_config_router = APIRouter()

LoggingSyncBody = make_sync_body("LoggingSyncBody", LoggingConfigUpsertRequest)
TaskMemorySyncBody = make_sync_body("TaskMemorySyncBody", TaskMemoryUpsertRequest)
MemorySyncBody = make_sync_body("MemorySyncBody", MemoryConfigUpsertRequest)
AuditLogSyncBody = make_sync_body("AuditLogSyncBody", AuditLogUpsertRequest)
CronPolicySyncBody = make_sync_body("CronPolicySyncBody", CronPolicyUpsertRequest)
PackageAllowlistSyncBody = make_sync_body("PackageAllowlistSyncBody", PackageAllowlistUpsertRequest)
LogMaskingCreateSyncBody = make_sync_body(
    "LogMaskingCreateSyncBody", LogMaskingRuleCreateRequest
)
LogMaskingUpdateSyncBody = make_sync_body(
    "LogMaskingUpdateSyncBody", LogMaskingRuleUpdateRequest
)


@application_config_router.put("/logging", response_model=ResponseModel)
async def upsert_logging(
    sync: Annotated[SyncContext, Depends(verify_sync(LoggingSyncBody))],
):
    try:
        result = await LoggingConfigService().upsert(**sync.business)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    trigger_runtime_config_update()
    return ResponseModel(code=200, message="success", data=sync_write_data(sync, result))


@application_config_router.delete("/logging", response_model=ResponseModel)
async def delete_logging(
    sync: VerifySyncEnvelopeOnly,
):
    try:
        await LoggingConfigService().delete()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    trigger_runtime_config_update()
    return ResponseModel(code=200, message="success", data=sync_write_data(sync, None))


@application_config_router.put("/task-memory", response_model=ResponseModel)
async def upsert_task_memory(
    sync: Annotated[SyncContext, Depends(verify_sync(TaskMemorySyncBody))],
):
    try:
        result = await TaskMemoryConfigService().upsert(sync.business)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    trigger_runtime_config_update()
    return ResponseModel(code=200, message="success", data=sync_write_data(sync, result))


@application_config_router.delete("/task-memory", response_model=ResponseModel)
async def delete_task_memory(
    sync: VerifySyncEnvelopeOnly,
):
    try:
        await TaskMemoryConfigService().delete()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    trigger_runtime_config_update()
    return ResponseModel(code=200, message="success", data=sync_write_data(sync, None))


@application_config_router.put("/memory", response_model=ResponseModel)
async def upsert_memory(
    sync: Annotated[SyncContext, Depends(verify_sync(MemorySyncBody))],
):
    try:
        result = await MemoryConfigService().upsert(**sync.business)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    trigger_runtime_config_update()
    return ResponseModel(code=200, message="success", data=sync_write_data(sync, result))


@application_config_router.delete("/memory", response_model=ResponseModel)
async def delete_memory(
    sync: VerifySyncEnvelopeOnly,
):
    try:
        await MemoryConfigService().delete()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    trigger_runtime_config_update()
    return ResponseModel(code=200, message="success", data=sync_write_data(sync, None))


@application_config_router.put("/package-allowlists", response_model=ResponseModel)
async def upsert_package_allowlist(
    sync: Annotated[SyncContext, Depends(verify_sync(PackageAllowlistSyncBody))],
):
    try:
        result = await PackageAllowlistService().upsert(sync.business)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ResponseModel(code=200, message="success", data=sync_write_data(sync, result))


@application_config_router.delete("/package-allowlists", response_model=ResponseModel)
async def delete_package_allowlist(
    sync: VerifySyncEnvelopeOnly,
):
    try:
        await PackageAllowlistService().delete()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ResponseModel(code=200, message="success", data=sync_write_data(sync, None))


@application_config_router.get("/package-allowlists/catalog", response_model=ResponseModel)
async def list_package_allowlist_catalog():
    """本集群 Hub 目录。不做允许名单过滤，失败时 data 里带空 items 和 error。"""
    from jiuwenswarm.server.runtime.equipment_hub_catalog import list_hub_equipment_catalog

    payload = await list_hub_equipment_catalog()
    return ResponseModel(code=200, message="success", data=payload)


@application_config_router.put("/cron-policy", response_model=ResponseModel)
async def upsert_cron_policy(
    sync: Annotated[SyncContext, Depends(verify_sync(CronPolicySyncBody))],
):
    try:
        result = await CronPolicyService().upsert(sync.business)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ResponseModel(code=200, message="success", data=sync_write_data(sync, result))


@application_config_router.delete("/cron-policy", response_model=ResponseModel)
async def delete_cron_policy(
    sync: VerifySyncEnvelopeOnly,
):
    try:
        await CronPolicyService().delete()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ResponseModel(code=200, message="success", data=sync_write_data(sync, None))


@application_config_router.put("/audit-log", response_model=ResponseModel)
async def upsert_audit_log(
    sync: Annotated[SyncContext, Depends(verify_sync(AuditLogSyncBody))],
):
    try:
        result = await AuditLogConfigService().upsert(**sync.business)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    trigger_runtime_config_update()
    return ResponseModel(code=200, message="success", data=sync_write_data(sync, result))


@application_config_router.delete("/audit-log", response_model=ResponseModel)
async def delete_audit_log(
    sync: VerifySyncEnvelopeOnly,
):
    try:
        await AuditLogConfigService().delete()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    trigger_runtime_config_update()
    return ResponseModel(code=200, message="success", data=sync_write_data(sync, None))


@application_config_router.post("/log-masking-rules", response_model=ResponseModel)
async def create_log_masking_rule(
    sync: Annotated[SyncContext, Depends(verify_sync(LogMaskingCreateSyncBody))],
):
    try:
        result = await LogMaskingRuleService().create(sync.business)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    trigger_runtime_config_update()
    return ResponseModel(code=200, message="success", data=sync_write_data(sync, result))


@application_config_router.patch(
    "/log-masking-rules/{rule_id}", response_model=ResponseModel
)
async def patch_log_masking_rule(
    rule_id: str,
    sync: Annotated[SyncContext, Depends(verify_sync(LogMaskingUpdateSyncBody))],
):
    try:
        result = await LogMaskingRuleService().update(rule_id, sync.business)
    except ValueError as exc:
        detail = str(exc)
        status = 404 if "not found" in detail else 400
        raise HTTPException(status_code=status, detail=detail) from exc
    trigger_runtime_config_update()
    return ResponseModel(code=200, message="success", data=sync_write_data(sync, result))


@application_config_router.delete(
    "/log-masking-rules/{rule_id}", response_model=ResponseModel
)
async def delete_log_masking_rule(
    rule_id: str,
    sync: VerifySyncEnvelopeOnly,
):
    try:
        await LogMaskingRuleService().delete(rule_id)
    except ValueError as exc:
        detail = str(exc)
        status = 404 if "not found" in detail else 400
        raise HTTPException(status_code=status, detail=detail) from exc
    trigger_runtime_config_update()
    return ResponseModel(code=200, message="success", data=sync_write_data(sync, None))
