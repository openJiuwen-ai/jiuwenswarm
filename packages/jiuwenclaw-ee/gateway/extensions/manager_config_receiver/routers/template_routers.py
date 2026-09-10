# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""模板同步 API（显式 Body schema，对齐 Manager template_routers）。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, HTTPException, Request

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
from .runtime_notify import trigger_runtime_config_update

templates_router = APIRouter()

_ServiceFactory = Callable[[], Any]


def _http_exc(exc: ValueError) -> HTTPException:
    detail = str(exc)
    status = 404 if "not found" in detail else 400
    return HTTPException(status_code=status, detail=detail)


def _require_secure_a2a_credential_transport(
    request: Request,
    tag: str,
    business: dict[str, Any],
) -> None:
    credential = business.get("credential")
    # G.CTL.03: if 内布尔条件不超过 3 个，isinstance 判断前置
    if tag != "a2a_outbound" or not isinstance(credential, dict):
        return
    if credential.get("operation") == "replace" and request.url.scheme.lower() != "https":
        raise HTTPException(
            status_code=400,
            detail="A2A credentials may only be synchronized over HTTPS",
        )


def _add_template_crud(
    path: str,
    svc_factory: _ServiceFactory,
    tag: str,
    create_body: type,
    update_body: type,
) -> None:
    create_sync = make_sync_body(f"{tag.title().replace('_', '')}TemplateCreateSyncBody", create_body)
    update_sync = make_sync_body(f"{tag.title().replace('_', '')}TemplateUpdateSyncBody", update_body)

    async def create_template(
        request: Request,
        body: Any,
    ):
        sync = await build_sync_context(body, request.method)
        _require_secure_a2a_credential_transport(request, tag, sync.business)
        try:
            result = await svc_factory().create(
                sync.business
            )
        except ValueError as exc:
            raise _http_exc(exc) from exc
        trigger_runtime_config_update()
        return ResponseModel(
            code=200, message="success", data=sync_write_data(sync, result)
        )

    async def update_template(
        request: Request,
        template_id: str,
        body: Any,
    ):
        sync = await build_sync_context(body, request.method)
        _require_secure_a2a_credential_transport(request, tag, sync.business)
        try:
            await svc_factory().update(
                template_id, sync.business
            )
        except ValueError as exc:
            raise _http_exc(exc) from exc
        trigger_runtime_config_update()
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
        trigger_runtime_config_update()
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
_add_template_crud(
    "/extension-config-templates",
    ExtensionConfigTemplateService,
    "extension_config",
    ExtensionConfigTemplateCreateRequest,
    ExtensionConfigTemplateUpdateRequest,
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
_add_template_crud(
    "/agent-templates",
    AgentTemplateService,
    "agent",
    AgentTemplateCreateRequest,
    AgentTemplateUpdateRequest,
)
