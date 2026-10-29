# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Assets management; resources remain owned by its injected host."""

from __future__ import annotations

import logging
from jiuwenswarm.common.schema.agent import AgentResponse

logger = logging.getLogger(__name__)


class AssetsManagement:
    def __init__(self, host):
        self.host = host

    async def _get_asset_publish_api(self):
        host = self.host
        async with host._asset_publish_lock:
            if host._asset_publish_api is None:
                from jiuwenswarm.common.utils import get_workspace_dir
                from jiuwenswarm.server.runtime.marketplace.asset_publish_api import (
                    AssetPublishAPI,
                )

                api = AssetPublishAPI(
                    get_workspace_dir() / "marketplace" / "publishing"
                )
                try:
                    await api.start()
                except BaseException:
                    await api.close()
                    raise
                host._asset_publish_api = api
            return host._asset_publish_api

    async def _start_asset_services(self):
        host = self.host
        available = True
        try:
            await host._get_asset_publish_api()
        except Exception:
            logger.warning(
                "[AssetPublish] startup unavailable; requests may retry initialization"
            )
            available = False
        try:
            from jiuwenswarm.common.utils import get_agent_workspace_dir
            from jiuwenswarm.server.runtime.marketplace.hub_catalog_cache import (
                start_hub_catalog_preload,
            )
            from jiuwenswarm.server.runtime.skill.skill_manager import SkillManager
            await start_hub_catalog_preload(SkillManager(workspace_dir=str(get_agent_workspace_dir())))
        except Exception:
            logger.warning(
                "[HubCatalog] preload unavailable; requests may load on demand"
            )
            available = False
        return available

    async def _handle_asset_publish(self, ws, request, send_lock):
        host = self.host
        from jiuwenswarm.server.runtime.marketplace.asset_publish_api import (
            PublishAPIError,
        )
    
        try:
            if request.channel_id != "web":
                raise PublishAPIError("WEB_CHANNEL_REQUIRED")
            preparation = getattr(host, "_preparation_registry", None)
            if preparation is not None:
                host._asset_start_task = preparation.start("assets")
            api = await host._get_asset_publish_api()
            payload = await api.call(
                request.req_method.value.rsplit(".", 1)[-1],
                request.params or {},
                gateway_user=request.user_id,
            )
            ok = True
        except PublishAPIError as exc:
            payload = {
                "code": exc.code,
                "error": exc.code,
                "can_submit": False,
                "errors": [{"code": exc.code, "field": exc.field}],
            }
            ok = False
        except Exception:
            payload = {
                "code": "PUBLISH_UNAVAILABLE",
                "error": "PUBLISH_UNAVAILABLE",
                "can_submit": False,
            }
            ok = False
        response = AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=ok,
            payload=payload,
            agent_ref=request.agent_ref,
        )
        wire = host.services.encode_response(response, response_id=request.request_id)
        async with send_lock:
            await host.services.send(ws, wire)
