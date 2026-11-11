# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""模板同步 API（显式 Body schema，对齐 Manager template_routers）。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from jiuwenswarm.gateway.a2a_manager.outbound.discovery import A2AOutboundDiscoveryService
from jiuwenswarm.gateway.a2a_manager.outbound.errors import A2AOutboundError

from ..core.template.a2a_access_policy_template import A2AAccessPolicyTemplateService
from ..core.template.a2a_outbound_template import A2AOutboundTemplateService
from ..core.template.agent_template import AgentTemplateService
from ..core.template.embedding_template import EmbeddingTemplateService
from ..core.template.extension_config_template import ExtensionConfigTemplateService
from ..core.template.mcp_template import McpTemplateService
from ..core.template.model_template import ModelTemplateService
from ..core.template.permissions_template import PermissionsTemplateService
from ..core.template.skill_prebuilt_template import SkillPrebuiltTemplateService
from ..schemas.common_schemas import ResponseModel
from ..schemas.sync_schemas import SyncEnvelopeOnlyBody, make_sync_body
from ..schemas.template_schemas import (
    A2AAccessPolicyTemplateCreateRequest,
    A2AAccessPolicyTemplateUpdateRequest,
    A2AOutboundTemplateCreateRequest,
    A2AOutboundTemplateUpdateRequest,
    AgentTemplateCreateRequest,
    AgentTemplateUpdateRequest,
    EmbeddingTemplateCreateRequest,
    EmbeddingTemplateUpdateRequest,
    ExtensionConfigTemplateCreateRequest,
    ExtensionConfigTemplateUpdateRequest,
    McpTemplateCreateRequest,
    McpTemplateUpdateRequest,
    ModelTemplateCreateRequest,
    ModelTemplateUpdateRequest,
    PermissionsTemplateCreateRequest,
    PermissionsTemplateUpdateRequest,
    SkillPrebuiltTemplateCreateRequest,
    SkillPrebuiltTemplateUpdateRequest,
)
from .deps import build_sync_context, sync_write_data
from .runtime_notify import (
    invalidate_local_enterprise_config_caches,
    trigger_runtime_config_update,
)

templates_router = APIRouter()

_ServiceFactory = Callable[[], Any]


def _http_exc(exc: ValueError) -> HTTPException:
    detail = str(exc)
    status = 404 if "not found" in detail else 400
    return HTTPException(status_code=status, detail=detail)


async def _require_secure_a2a_credential_transport(
    request: Request,
    tag: str,
    business: dict[str, Any],
) -> None:
    credential = business.get("credential")
    # G.CTL.03: if 内布尔条件不超过 3 个，isinstance 判断前置
    if tag != "a2a_outbound" or not isinstance(credential, dict):
        return
    if credential.get("operation") != "replace":
        return
    data = business.get("data") or {}
    policy = data.get("network_policy") if isinstance(data, dict) else None
    policy = policy if isinstance(policy, dict) else {}
    try:
        await A2AOutboundDiscoveryService().validate_network_target(
            str(request.url), network_policy=policy,
        )
    except A2AOutboundError as exc:
        raise HTTPException(
            status_code=400,
            detail="A2A credential sync blocked by network access settings",
        ) from exc


def _add_template_crud(
    path: str,
    svc_factory: _ServiceFactory,
    tag: str,
    create_body: type,
    update_body: type,
    *,
    notify_runtime: bool = True,
    gated_service: bool = False,
) -> None:
    """注册模板 CRUD 三端点。

    notify_runtime：写完后是否通知 runtime 重建（delete 固定按此值）。
    gated_service：要不要通知由 service 按变更内容判断——create 额外返回
    (业务结果, 是否通知)，update 的返回值即是否通知（用于 agent 模板）。
    """
    create_sync = make_sync_body(f"{tag.title().replace('_', '')}TemplateCreateSyncBody", create_body)
    update_sync = make_sync_body(f"{tag.title().replace('_', '')}TemplateUpdateSyncBody", update_body)

    async def create_template(
        request: Request,
        body: Any,
    ):
        sync = await build_sync_context(body, request.method)
        await _require_secure_a2a_credential_transport(request, tag, sync.business)
        try:
            result = await svc_factory().create(
                sync.business
            )
        except ValueError as exc:
            raise _http_exc(exc) from exc
        notify = notify_runtime
        if gated_service:
            # create 返回 (业务结果, 是否需要通知 runtime)
            result, notify = result
        if notify:
            trigger_runtime_config_update()
        else:
            # 不通知 runtime 重建，只清掉本进程的企业配置缓存。
            invalidate_local_enterprise_config_caches()
        return ResponseModel(
            code=200, message="success", data=sync_write_data(sync, result)
        )

    async def update_template(
        request: Request,
        template_id: str,
        body: Any,
    ):
        sync = await build_sync_context(body, request.method)
        await _require_secure_a2a_credential_transport(request, tag, sync.business)
        try:
            update_result = await svc_factory().update(
                template_id, sync.business
            )
        except ValueError as exc:
            raise _http_exc(exc) from exc
        notify = notify_runtime
        if gated_service:
            # update 的返回值即是否需要通知
            notify = bool(update_result)
        if notify:
            trigger_runtime_config_update()
        else:
            # 不通知 runtime 重建，只清掉本进程的企业配置缓存。
            invalidate_local_enterprise_config_caches()
        return ResponseModel(
            code=200, message="success", data=sync_write_data(sync, None)
        )

    async def delete_template(
        request: Request,
        template_id: str,
        body: SyncEnvelopeOnlyBody,
    ):
        sync = await build_sync_context(body, request.method)
        try:
            await svc_factory().delete(template_id)
        except ValueError as exc:
            raise _http_exc(exc) from exc
        if notify_runtime:
            trigger_runtime_config_update()
        else:
            # 不通知 runtime 重建，只清掉本进程的企业配置缓存。
            invalidate_local_enterprise_config_caches()
        return ResponseModel(
            code=200, message="success", data=sync_write_data(sync, None)
        )

    create_template.__name__ = f"create_{tag}_template"
    update_template.__name__ = f"update_{tag}_template"
    delete_template.__name__ = f"delete_{tag}_template"
    create_template.__annotations__["body"] = create_sync
    update_template.__annotations__["body"] = update_sync

    templates_router.add_api_route(
        path, create_template, methods=["POST"], response_model=ResponseModel
    )
    templates_router.add_api_route(
        f"{path}/{{template_id}}",
        update_template,
        methods=["PATCH"],
        response_model=ResponseModel,
    )
    templates_router.add_api_route(
        f"{path}/{{template_id}}",
        delete_template,
        methods=["DELETE"],
        response_model=ResponseModel,
    )


# 模板写完后要不要通知 runtime 重建，按模板类型区分：
# - 扩展配置模板：不通知。agentserver 读取该表自带缓存过期，写入端点
#   只需清掉本进程的缓存。
# - agent 模板：按变更内容判断（gated_service）。只改 extension_config
#   引用或名称等元数据时不通知；其余槽位引用、启停、data 变化时通知。
# - 其余模板：写入即通知。
_add_template_crud(
    "/a2a-outbound-templates",
    A2AOutboundTemplateService,
    "a2a_outbound",
    A2AOutboundTemplateCreateRequest,
    A2AOutboundTemplateUpdateRequest,
)
_add_template_crud(
    "/a2a-access-policies",
    A2AAccessPolicyTemplateService,
    "a2a_access_policy",
    A2AAccessPolicyTemplateCreateRequest,
    A2AAccessPolicyTemplateUpdateRequest,
)
_add_template_crud(
    "/model-templates",
    ModelTemplateService,
    "model",
    ModelTemplateCreateRequest,
    ModelTemplateUpdateRequest,
)
_add_template_crud(
    "/embedding-templates",
    EmbeddingTemplateService,
    "embedding",
    EmbeddingTemplateCreateRequest,
    EmbeddingTemplateUpdateRequest,
)
# 扩展配置模板：写后不通知 runtime（理由见上方注释块）。
_add_template_crud(
    "/extension-config-templates",
    ExtensionConfigTemplateService,
    "extension_config",
    ExtensionConfigTemplateCreateRequest,
    ExtensionConfigTemplateUpdateRequest,
    notify_runtime=False,
)
_add_template_crud(
    "/skill-prebuilt-templates",
    SkillPrebuiltTemplateService,
    "skill_prebuilt",
    SkillPrebuiltTemplateCreateRequest,
    SkillPrebuiltTemplateUpdateRequest,
)
_add_template_crud(
    "/permissions-templates",
    PermissionsTemplateService,
    "permissions",
    PermissionsTemplateCreateRequest,
    PermissionsTemplateUpdateRequest,
)
_add_template_crud(
    "/mcp-templates",
    McpTemplateService,
    "mcp",
    McpTemplateCreateRequest,
    McpTemplateUpdateRequest,
)
# agent 模板：是否通知由变更内容决定（见上方注释块）；delete 无新旧内容
# 可比对，固定按 notify_runtime 通知。
_add_template_crud(
    "/agent-templates",
    AgentTemplateService,
    "agent",
    AgentTemplateCreateRequest,
    AgentTemplateUpdateRequest,
    gated_service=True,
)
