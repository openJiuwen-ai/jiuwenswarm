"""Stable-ID model RPCs shared by Gateway and per-user ConfigAdapter."""

from __future__ import annotations

from jiuwenswarm.common.config import (
    delete_model_resource,
    upsert_model_group_resource,
    upsert_model_resource,
)
from jiuwenswarm.common.model_catalog import ModelCatalog, SelectionReference
from jiuwenswarm.common.model_errors import (
    MODEL_SELECTION_FORBIDDEN,
    MODEL_SELECTION_NOT_FOUND,
    ModelSelectionError,
)
from jiuwenswarm.common.model_selection import ModelSelection

RESOURCE_OPERATIONS = {
    "models.get": "models.get",
    "models.upsert": "models.upsert",
    "models.delete": "models.delete",
    "models.references": "models.references",
    "model_groups.list": "groups.list",
    "model_groups.upsert": "groups.upsert",
    "model_groups.delete": "groups.delete",
}


async def find_resource_references(catalog, selection, *, user_id=""):
    """Read the configured Cron backend, never substitute a local file for etcd."""
    from jiuwenswarm.runtime.cron.factory import create_gateway_cron_store, load_cron_store_settings
    from jiuwenswarm.common.model_migration import migrate_legacy_cron_selections

    references = catalog.find_references(selection, include_cron=False)
    store = await create_gateway_cron_store(catalog.config)
    is_etcd = load_cron_store_settings(catalog.config).backend == "etcd"
    try:
        await migrate_legacy_cron_selections(store, catalog=catalog, user_id=user_id if is_etcd else None)
        for job in await store.list_jobs(strict=True):
            if is_etcd and job.user_id != user_id:
                continue
            value = job.model_selection or {}
            if value.get("type") == selection.type and value.get("id") == selection.id:
                references.append(SelectionReference("cron", job.id))
    finally:
        await store.aclose()
    return references


async def _resource_payload(operation, params, *, user_id=""):
    catalog = ModelCatalog()
    if operation == "groups.list":
        return {"groups": catalog.list_public_groups()}
    if operation == "models.get":
        return {"model": catalog.get_public_model_detail(str(params.get("model_id") or ""), include_api_key=True)}
    if operation == "models.references":
        references = await find_resource_references(catalog, ModelSelection.model_validate(params), user_id=user_id)
        return {"references": [ref.__dict__ for ref in references]}
    if operation in {"models.delete", "groups.delete"}:
        resource_type = "model_group" if operation == "groups.delete" else "model"
        key = "model_group_id" if resource_type == "model_group" else "model_id"
        resource_id = str(params.get(key) or "")
        if resource_type == "model" and catalog.get_model(resource_id)["source"] == "agentos":
            raise ModelSelectionError(MODEL_SELECTION_FORBIDDEN, "AgentOS models are read-only")
        if not delete_model_resource(resource_type, resource_id, cron_references_checked=True):
            raise ModelSelectionError(MODEL_SELECTION_NOT_FOUND, "resource does not exist")
        return {key: resource_id, "deleted": True}
    if operation == "models.upsert":
        saved = upsert_model_resource(params.get("model") or {})
        return {"model": ModelCatalog().get_public_model_detail(saved["model_id"])}
    if operation == "groups.upsert":
        return {"group": upsert_model_group_resource(params.get("group") or {})}
    raise ValueError(f"unsupported model resource operation: {operation}")


async def model_resource_handler(
    channel, ws, req_id, params, session_id, *, operation, on_config_saved=None, agent_client=None, user_id="",
):
    """Mutate once, reload through the existing config boundary, then acknowledge."""
    from jiuwenswarm.common.config_panel.models_handlers import _apply_models_change

    del session_id
    try:
        payload = await _resource_payload(operation, params or {}, user_id=user_id)
        if operation.endswith((".upsert", ".delete")):
            if operation == "models.delete":
                # Model delete may also prune group routes that referenced it.
                changed_keys = ["models.defaults", "models.groups"]
            elif operation.startswith("groups"):
                changed_keys = ["models.groups"]
            else:
                changed_keys = ["models.defaults"]
            applied = await _apply_models_change(
                on_config_saved, agent_client,
                changed_keys=changed_keys,
            )
            payload["applied_without_restart"] = applied
        await channel.send_response(ws, req_id, ok=True, payload=payload)
    except ModelSelectionError as exc:
        await channel.send_response(ws, req_id, ok=False, error=str(exc), code=exc.code, payload=exc.to_payload())
    except Exception as exc:  # noqa: BLE001
        await channel.send_response(ws, req_id, ok=False, error=str(exc), code="BAD_REQUEST")


async def session_selection_handler(channel, ws, req_id, params, session_id):
    from jiuwenswarm.server.runtime.model_routing_registry import ModelSelectionResolver
    from jiuwenswarm.server.runtime.session.model_selection_store import set_session_model_selection

    try:
        params = params or {}
        target = str(params.get("session_id") or session_id or "")
        selection = ModelSelection.model_validate(params.get("model_selection"))
        ModelSelectionResolver().resolve(selection)
        set_session_model_selection(target, selection)
        await channel.send_response(ws, req_id, ok=True, payload={"model_selection": selection.model_dump()})
    except Exception as exc:  # noqa: BLE001
        await channel.send_response(ws, req_id, ok=False, error=str(exc), code=getattr(exc, "code", "BAD_REQUEST"))


def register_model_resource_handlers(channel, *, on_config_saved=None, agent_client=None, user_id=""):
    from functools import partial

    for method, operation in RESOURCE_OPERATIONS.items():
        channel.register_method(method, partial(
            model_resource_handler, channel, operation=operation,
            on_config_saved=on_config_saved, agent_client=agent_client, user_id=user_id,
        ))
    channel.register_method("session.selection.set", partial(session_selection_handler, channel))
