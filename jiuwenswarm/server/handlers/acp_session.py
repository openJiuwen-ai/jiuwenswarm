# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Native ACP session operations carried by Gateway's initialize route."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from uuid import uuid4

from jiuwenswarm.acp.session_protocol import ACP_SESSION_EXTENSION
from jiuwenswarm.runtime.model_catalog import ModelCatalogError, resolve_model_selection
from jiuwenswarm.server.runtime.session import lifecycle
from jiuwenswarm.server.runtime.session.session_metadata import (
    get_session_metadata,
    init_session_metadata,
    patch_session_metadata,
)

_NATIVE_SESSION_ID = re.compile(r"acp_[0-9a-f]{32}")


def _session_payload(runtime, user_id: str, args: dict) -> dict:
    operation = args.get("operation")
    if operation == "capabilities":
        return {"loadSession": True}
    if operation not in ("new", "resume", "set_model"):
        raise ValueError("Unknown ACP session operation")

    session_id = args.get("sessionId")
    stored = {}
    if operation != "new":
        if not isinstance(session_id, str) or not _NATIVE_SESSION_ID.fullmatch(session_id):
            raise FileNotFoundError
        lifecycle.guard(session_id)
        stored = get_session_metadata(session_id, cache_bust=True, enable_writeback=False)
        if (stored.get("channel_id") != "acp" or stored.get("user_id") != user_id
                or stored.get("client_app") != "jiuwenswarm-acp"):
            raise FileNotFoundError

    if operation != "set_model":
        cwd = args.get("cwd")
        if not isinstance(cwd, str) or not Path(cwd).is_absolute() or not Path(cwd).is_dir():
            raise ValueError("cwd must be an existing absolute directory")
        cwd = str(Path(cwd).resolve())
        if stored and stored.get("project_dir") != cwd:
            raise FileNotFoundError

    catalog = runtime.list_model_capabilities(current_selection=stored.get("model") or "")
    current = catalog.current_selection
    if operation == "new":
        session_id = f"acp_{uuid4().hex}"
        init_session_metadata(
            session_id=session_id, channel_id="acp", user_id=user_id,
            project_dir=cwd, persist_session=True, model=current,
            client_app="jiuwenswarm-acp",
        )
    elif operation == "set_model":
        try:
            current = resolve_model_selection(catalog, args.get("modelId")).selection_key
        except ModelCatalogError as exc:
            raise ValueError(exc.message) from exc
        patch_session_metadata(session_id, {"model": current})

    from jiuwenswarm.server.runtime.acp_thinking import thinking_config_options

    result = {"sessionId": session_id, "configOptions": thinking_config_options(current) if current else []}
    if catalog.models:
        result["models"] = {
            "currentModelId": current,
            "availableModels": [
                {"modelId": model.selection_key, "name": model.display_name}
                for model in catalog.models
            ],
        }
    return result


async def native_acp_session_response(runtime, request) -> dict | None:
    params = request.params
    if request.channel_id != "acp" or not isinstance(params, dict):
        return None
    args = params.get(ACP_SESSION_EXTENSION)
    if not isinstance(args, dict):
        return None
    try:
        if args.get("operation") in ("new", "resume", "set_model"):
            await runtime.start()
        payload = await asyncio.to_thread(_session_payload, runtime, request.user_id or "", args)
    except FileNotFoundError:
        payload = {"error": {"code": -32002, "message": "Session not found or expired"}}
    except lifecycle.LifecycleError as exc:
        if exc.code in ("NOT_FOUND", "SESSION_ARCHIVED"):
            payload = {"error": {"code": -32002, "message": "Session not found or expired"}}
        else:
            payload = {"error": {"code": -32603, "message": "Session lifecycle operation in progress"}}
    except ValueError as exc:
        payload = {"error": {"code": -32602, "message": str(exc)}}
    except ModelCatalogError as exc:
        payload = {"error": {"code": -32603, "message": exc.message}}
    return {ACP_SESSION_EXTENSION: payload}
