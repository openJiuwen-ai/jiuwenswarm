# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Manage frontend form resources while the host owns conversations and windows."""

import asyncio
import json
import logging
import re
from functools import partial
from pathlib import Path
from typing import Any
from urllib.parse import quote

from openjiuwen.core.extension.data.files import run_file_operation

from agent_gateway.extensions_v2.webui.models import (
    RenderMode, RenderNode, RenderStatus, RenderTarget, UiActionOutput, UiRenderInput,
)
from agent_gateway.extensions_v2.webui.schema import validate_request
from .resources import builtin_resources, import_resource, load_json, setting_value, settings_values, validate_manifest
from .settings import STORE_KEY, import_image, settings_view
from .sound import SOUND_ASSETS, SOUND_STORE_KEY, changed_sound, sound_preferences

PACKAGE_ID = "webui.application-forms"
LOG = logging.getLogger(__name__)
READ_ERROR = {"zh": "无法读取资源或设置，修改已停用。请检查数据后重新打开本页。", "en": "Resources or settings could not be read. Changes are disabled. Check the data and reopen this page."}
WRITE_ERROR = {"zh": "未能保存设置，本次修改没有生效。", "en": "Settings were not saved. This change was not applied."}
UNKNOWN_ERROR = {"zh": "无法确认保存结果。请重新打开设置页核对，不要重复提交。", "en": "The save result is unknown. Reopen settings to verify before submitting again."}
RESOURCE_ERROR = {"zh": "资源或设置校验失败。请核对资源格式、大小、ID 是否重复及设置定义。", "en": "Resource or setting validation failed. Check the package format, size, duplicate ID and setting definitions."}


def read_catalog(root: Path, saved: object) -> tuple[list[dict], dict]:
    """Resolve an exact saved catalog; missing or corrupt imported files are errors."""
    if saved is None:
        saved = {"schemaVersion": 3, "imports": {}, "values": {}}
    if not isinstance(saved, dict):
        raise ValueError("Invalid resource preferences")
    legacy = set(saved) == {"imports", "values"}
    if not legacy and (set(saved) != {"schemaVersion", "imports", "values"}
                       or type(saved["schemaVersion"]) is not int or saved["schemaVersion"] not in {1, 2, 3}):
        raise ValueError("Unsupported resource preference schema")
    imports, stored = saved["imports"], saved["values"]
    if not isinstance(imports, dict) or not isinstance(stored, dict) or len(imports) > 15:
        raise ValueError("Invalid resource preferences")
    stored = dict(stored)
    # Schemas 0–2 exposed settings on the default bee; schema 3 fixes its design.
    if (legacy or saved["schemaVersion"] < 3) and "builtin-bee" in stored:
        bee = stored["builtin-bee"]
        expected = {"image"} if legacy else {"image", "motion"}
        if not isinstance(bee, dict) or set(bee) != expected:
            raise ValueError("Invalid retired bee settings")
        setting_value({"kind": "image"}, bee["image"])
        if "motion" in bee:
            setting_value({"kind": "switch"}, bee["motion"])
        stored["builtin-bee"] = {}
    # Schemas 0 and 1 included two defaults that now require explicit ZIP import.
    if legacy or saved["schemaVersion"] == 1:
        if "builtin-island" in stored:
            capsule = stored.pop("builtin-island")
            if not isinstance(capsule, dict) or set(capsule) != {"appearance"} or capsule["appearance"] not in {"dark", "light"}:
                raise ValueError("Invalid retired capsule settings")
        if "builtin-terminal" in stored and stored.pop("builtin-terminal") != {}:
            raise ValueError("Invalid retired terminal settings")
    catalog = builtin_resources()
    for identity, digest in imports.items():
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("Invalid resource digest")
        definition = validate_manifest(load_json((root / "public" / "resources" / digest / "presentation.json").read_bytes()))
        if identity != definition["id"] or identity.startswith("builtin-"):
            raise ValueError("Invalid imported resource identity")
        catalog.append(definition)
    if set(stored) - {resource["id"] for resource in catalog}:
        raise ValueError("Settings refer to a missing resource")
    values = {resource["id"]: settings_values(resource, stored.get(resource["id"])) for resource in catalog}
    return catalog, {"schemaVersion": 3, "imports": dict(imports), "values": values}


class ApplicationFormsExtension:
    """Provide one resource catalog and independently persisted resource settings."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()

    def register(self, registrar: Any) -> None:
        for name in ("ui.ready", "ui.click", "ui.change", "ui.press_enter"):
            registrar.register_event_handler(name, self.on_event, {"target.package_id": PACKAGE_ID})

    async def on_event(self, event: Any, ops: Any, next_handler: Any) -> UiActionOutput:
        request = event.event_input
        target = request.target
        settings = target.slot_id == "application.settings" and target.instance_key == "main"
        if not settings and target.slot_id != "application.presentation":
            return await next_handler(event)
        async with self._lock:
            try:
                root = await ops.data.root()
                saved = await ops.store.get(STORE_KEY, None)
                catalog, prefs = await run_file_operation(partial(read_catalog, root, saved))
                sound = sound_preferences(await ops.store.get(SOUND_STORE_KEY, None))
            except Exception as error:
                LOG.warning("Application form data could not be read: %s", error)
                if settings:
                    await self._settings(ops, [], {"imports": {}, "values": {}}, sound_preferences(), READ_ERROR, disabled=True)
                    return UiActionOutput(accepted=False, status="store_unavailable")
                raise RuntimeError("Application form data could not be read") from error
            if request.event_type == "ui.ready":
                if settings:
                    await self._settings(ops, catalog, prefs, sound)
                else:
                    await self._surfaces(ops, catalog, prefs, sound)
                return UiActionOutput(accepted=True, status="rendered")
            if not settings:
                return await next_handler(event)
            action = request.payload.get("action_id")
            args = request.payload.get("args", {})
            updated = {"schemaVersion": 3, "imports": dict(prefs["imports"]), "values": {key: dict(value) for key, value in prefs["values"].items()}}
            removed = None
            original_catalog = catalog
            original_sound = sound
            store_key = STORE_KEY
            try:
                if action == "sound.set":
                    sound = changed_sound(sound, args["key"], request.payload["data"])
                    store_key = SOUND_STORE_KEY
                elif action == "resource.import":
                    if len(catalog) >= 16:
                        raise ValueError("The resource catalog is full")
                    resource, digest = await run_file_operation(partial(import_resource, root, request.payload["data"], {item["id"] for item in catalog}))
                    updated["imports"][resource["id"]] = digest
                    updated["values"][resource["id"]] = settings_values(resource)
                    catalog = [*catalog, resource]
                elif action == "resource.remove":
                    removed = args["resource"]
                    if removed not in updated["imports"]:
                        raise ValueError("Only imported resources can be removed")
                    del updated["imports"][removed]
                    del updated["values"][removed]
                    catalog = [item for item in catalog if item["id"] != removed]
                elif action in {"setting.set", "setting.image", "setting.reset"}:
                    identity, key = args["resource"], args["key"]
                    resource = next(item for item in catalog if item["id"] == identity)
                    field = next(item for item in resource["settings"] if item["key"] == key)
                    if action == "setting.image":
                        if field["kind"] != "image":
                            raise ValueError("This setting is not an image")
                        value = await run_file_operation(partial(import_image, root, request.payload["data"]))
                    elif action == "setting.reset":
                        value = field["default"]
                    else:
                        data = request.payload["data"]
                        value = data["checked"] if field["kind"] == "switch" else data["value"]
                    updated["values"][identity][key] = setting_value(field, value)
                else:
                    return await next_handler(event)
                # Check the exact final transport payload before acknowledging a persisted change.
                for resource in catalog:
                    await self._config(ops, resource, updated, sound)
                view = await self._settings_root(ops, catalog, updated, sound)
                report = validate_request(UiRenderInput(target=RenderTarget(session_id="", slot_id="application.settings", instance_key="main", scope="application", connection_id=target.connection_id), root=view))
                if not report.ok:
                    raise ValueError(report.message)
            except Exception as error:
                LOG.warning("Application form change was rejected: %s", error)
                await self._settings(ops, original_catalog, prefs, original_sound, RESOURCE_ERROR)
                return UiActionOutput(accepted=False, status="invalid")
            try:
                saved = await ops.store.set(store_key, sound if store_key == SOUND_STORE_KEY else updated)
            except Exception as error:
                LOG.warning("Application form save outcome is unknown: %s", error)
                await self._settings(ops, original_catalog, prefs, original_sound, UNKNOWN_ERROR, disabled=True)
                return UiActionOutput(accepted=False, status="unknown")
            if saved is not True:
                await self._settings(ops, original_catalog, prefs, original_sound, WRITE_ERROR)
                return UiActionOutput(accepted=False, status="not_saved")
            if removed:
                await self._render(ops, "application.presentation", removed, None, RenderMode.CLEAR)
            await self._surfaces(ops, catalog, updated, sound)
            await self._settings(ops, catalog, updated, sound)
            return UiActionOutput(accepted=True, status="saved")

    def _asset(self, prefs: dict, identity: str, path: str) -> tuple[str, str]:
        digest = prefs["imports"].get(identity)
        return (f"resources/{digest}/{path}", "data") if digest else (path, "package")

    async def _sound_config(self, ops: Any, sound: dict) -> dict:
        compact, workspace = SOUND_ASSETS[sound["preset"]]
        return {"enabled": sound["enabled"], "volume": sound["volume"] / 100,
                "compact": await ops.ui.asset_url(compact, source="package"),
                "workspace": await ops.ui.asset_url(workspace, source="package")}

    async def _config(self, ops: Any, resource: dict, prefs: dict, sound: dict) -> dict:
        metadata, source = self._asset(prefs, resource["id"], "presentation.json" if resource["id"] in prefs["imports"] else "presentation.html")
        base = (await ops.ui.asset_url(metadata, source=source)).rsplit("/", 1)[0] + "/"
        values = dict(prefs["values"][resource["id"]])
        for field in resource["settings"]:
            if field["kind"] == "image" and values[field["key"]]:
                values[field["key"]] = await ops.ui.asset_url(values[field["key"]], source="data")
        config = {**resource["config"], "settings": values, "assetBase": base,
                  "transitionSound": await self._sound_config(ops, sound)}
        if len(json.dumps(config, ensure_ascii=False, allow_nan=False).encode("utf-8")) > 16 * 1024:
            raise ValueError("Resource configuration exceeds ApplicationSurface budget")
        return config

    async def _surfaces(self, ops: Any, catalog: list[dict], prefs: dict, sound: dict) -> None:
        for resource in catalog:
            path, source = self._asset(prefs, resource["id"], resource["entry"])
            await self._render(ops, "application.presentation", resource["id"], RenderNode(component="ApplicationSurface", props={
                "src": await ops.ui.asset_url(path, source=source), "title": resource["name"], **resource["window"],
                "config": await self._config(ops, resource, prefs, sound),
            }))

    async def _settings_root(self, ops: Any, catalog: list[dict], prefs: dict, sound: dict, error: dict[str, str] | None = None, disabled: bool = False) -> RenderNode:
        covers = {}
        for resource in catalog:
            path, source = self._asset(prefs, resource["id"], resource["preview"])
            covers[resource["id"]] = await ops.ui.asset_url(path, source=source)
        preview = await ops.ui.asset_url("sound-preview.html", source="package")
        preview += "#" + quote(json.dumps(await self._sound_config(ops, sound), separators=(",", ":")), safe="")
        return settings_view(catalog, prefs["values"], covers, error, disabled, sound=sound, sound_preview=preview)

    async def _settings(self, ops: Any, catalog: list[dict], prefs: dict, sound: dict, error: dict[str, str] | None = None, disabled: bool = False) -> None:
        view = await self._settings_root(ops, catalog, prefs, sound, error, disabled)
        await self._render(ops, "application.settings", "main", view)

    async def _render(self, ops: Any, slot: str, instance: str, root: RenderNode | None, mode: RenderMode = RenderMode.REPLACE) -> None:
        result = await ops.ui.render(UiRenderInput(target=RenderTarget(session_id="", slot_id=slot, instance_key=instance, scope="application"), root=root, mode=mode))
        if result is None or result.status is not RenderStatus.DELIVERED:
            raise RuntimeError("Application form view was not delivered")


def create_extension() -> ApplicationFormsExtension:
    """Create one serialized resource manager per activation."""
    return ApplicationFormsExtension()
