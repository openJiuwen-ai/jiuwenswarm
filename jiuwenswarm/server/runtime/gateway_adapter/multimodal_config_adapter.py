# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Personal-settings multimodal RPCs, executed by the owning Runtime Worker."""

import asyncio
import inspect
import logging

from jiuwenswarm.common.config import get_config
from jiuwenswarm.common.multimodal_models import MultimodalError
from jiuwenswarm.common.multimodal_store import MultimodalStore
from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.runtime.gateway_adapter.base import GatewayAdapter

logger = logging.getLogger(__name__)


class MultimodalConfigAdapter(GatewayAdapter):
    methods = frozenset({
        ReqMethod.MULTIMODAL_MODELS_LIST.value, ReqMethod.MULTIMODAL_MODELS_GET.value,
        ReqMethod.MULTIMODAL_MODELS_UPSERT.value, ReqMethod.MULTIMODAL_MODELS_DELETE.value,
        ReqMethod.MULTIMODAL_TOOLS_UPDATE.value, ReqMethod.MULTIMODAL_CONFIG_APPLY.value,
    })

    def __init__(self, on_config_saved=None) -> None:
        self._store = MultimodalStore()
        self._on_config_saved = on_config_saved
        self._save_lock = asyncio.Lock()

    async def handle(self, request: AgentRequest) -> AgentResponse:
        params = request.params or {}
        operation = request.req_method.value.removeprefix("multimodal.")
        try:
            if operation == "models.list":
                payload = self._store.read()
            elif operation == "models.get":
                model_id = params.get("model_id")
                if not isinstance(model_id, str) or not model_id:
                    raise MultimodalError("MM_VALIDATION_FAILED")
                payload = self._store.read(model_id)
            else:
                async with self._save_lock:
                    # Match ConfigAdapter: finish the small synchronous file
                    # transaction before yielding to another config request.
                    payload, updates = self._store.mutate(operation, params)
                    applied = False
                    if self._on_config_saved is not None:
                        try:
                            callback = self._on_config_saved(
                                list(updates), env_updates=updates, config_payload=get_config(),
                                reload_options={"reload_scopes": ["multimodal"]},
                            )
                            applied = bool(await callback) if inspect.isawaitable(callback) else bool(callback)
                        except Exception:
                            logger.warning("Multimodal reload failed after save")
                    payload["applied_without_restart"] = applied
            return AgentResponse(request_id=request.request_id, channel_id=request.channel_id,
                                 ok=True, payload=payload, metadata=request.metadata)
        except MultimodalError as exc:
            return AgentResponse(request_id=request.request_id, channel_id=request.channel_id,
                                 ok=False, payload=exc.payload, metadata=request.metadata)
        except OSError:
            code = "MM_STORAGE_FAILED"
            return AgentResponse(request_id=request.request_id, channel_id=request.channel_id, ok=False,
                                 payload=MultimodalError(code).payload, metadata=request.metadata)
