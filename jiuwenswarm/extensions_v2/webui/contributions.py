# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Discover and validate the ``ui`` section of an active package manifest."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
import hashlib
import json

from openjiuwen.core.extension import ActivePackageRecord

from jiuwenswarm.extensions_v2.manifest_io import read_manifest_document
from jiuwenswarm.extensions_v2.webui.constants import RENDERER_WEBUI_TREE, slot_spec
from jiuwenswarm.extensions_v2.webui.settings import SETTINGS_SLOT, parse_settings

_UI_TYPES = frozenset({"web.slot.contribution", "web.locale"})


@dataclass(frozen=True, slots=True)
class UiContribution:
    """One validated UI declaration from a package manifest."""

    package_id: str
    type: str
    id: str
    slot: str | None = None
    order: int = 0
    panel: Mapping[str, Any] = field(default_factory=dict)
    locale_id: str | None = None
    renderer: Mapping[str, Any] = field(default_factory=dict)
    resources: Mapping[str, str] = field(default_factory=dict)
    settings: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class UiCatalog:
    """Valid UI contributions and per-package validation errors."""

    items: tuple[UiContribution, ...] = ()
    errors: Mapping[str, str] = field(default_factory=dict)

    def allows_slot(self, slot_id: str) -> bool:
        return self.allows_target(slot_id, "")

    def allows_target(self, slot_id: str, package_id: str, *, instance_key: str = "", scope: str | None = None) -> bool:
        """Check the active package's slot contribution, or broadcast ready slot."""
        return any(item.type == "web.slot.contribution" and item.slot == slot_id
                   and (not package_id or item.package_id == package_id)
                   and item.settings.get("kind", "item") == "item"
                   and (not item.settings or (
                       (scope is None or item.settings["scope"] == scope)
                       and (not instance_key or item.settings["instance_key"] == instance_key)))
                   for item in self.items)

    def settings_payload(self) -> dict[str, Any]:
        """Return public metadata, keeping package paths and executable modules private."""
        payload = {"items": [{"package_id": item.package_id, "id": item.id, **item.settings}
                             for item in self.items if item.slot == SETTINGS_SLOT], "errors": dict(self.errors)}
        revision = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        return {"revision": revision, **payload}

    def slots(self) -> frozenset[str]:
        return frozenset(
            item.slot for item in self.items if item.type == "web.slot.contribution" and item.slot is not None
            and item.settings.get("kind", "item") == "item"
        )


class WebUICatalogService:
    """Cache the UI catalog until the active package set changes."""

    def __init__(self, records_provider: Callable[[], Sequence[ActivePackageRecord]]) -> None:
        self._records_provider = records_provider
        self._signature: tuple[tuple[str, str, int], ...] = ()
        self._catalog = UiCatalog()

    def active_package(self, package_id: str) -> ActivePackageRecord | None:
        """Return the current trusted activation, without accepting a caller filesystem root."""
        return next((record for record in self._records_provider() if record.package_id == package_id), None)

    def current(self) -> UiCatalog:
        records = tuple(self._records_provider())
        signature = tuple(
            sorted((record.package_id, record.version, record.generation) for record in records)
        )
        if signature != self._signature:
            self._catalog = load_ui_catalog(records)
            self._signature = signature
        return self._catalog


def load_ui_catalog(records: Sequence[ActivePackageRecord]) -> UiCatalog:
    """Load the UI section from every active package root.

    A package with an invalid UI section contributes nothing; its error is
    reported separately so one bad package cannot take down the others.
    """

    items: list[UiContribution] = []
    errors: dict[str, str] = {}
    for record in records:
        try:
            package_items = load_package_ui(record)
        except (OSError, UnicodeError, ValueError) as exc:
            errors[record.package_id] = str(exc)
            continue
        if package_items is None:
            continue
        items.extend(package_items)
    return UiCatalog(items=tuple(items), errors=errors)


def load_package_ui(record: ActivePackageRecord) -> tuple[UiContribution, ...] | None:
    """Return validated UI items for one package, or None when it declares none."""

    raw = read_manifest_document(record.root)
    ui = raw.get("ui")
    if ui is None:
        return None
    if not isinstance(ui, Mapping):
        raise ValueError("ui must be a mapping")
    api_version = ui.get("apiVersion")
    if api_version != 1:
        raise ValueError(f"ui.apiVersion must be 1, got {api_version!r}")
    raw_items = ui.get("items")
    if not isinstance(raw_items, list):
        raise ValueError("ui.items must be a list")

    # Locales are collected first so slot contributions can reference them.
    locales: dict[str, UiContribution] = {}
    for raw_item in raw_items:
        item = _parse_item(record, raw_item)
        if item.type == "web.locale":
            if item.id in locales:
                raise ValueError(f"duplicate web.locale id: {item.id}")
            locales[item.id] = item

    contributions: list[UiContribution] = []
    seen: set[str] = set()
    settings_targets: set[tuple[str, str]] = set()
    for raw_item in raw_items:
        item = _parse_item(record, raw_item)
        if item.type != "web.slot.contribution":
            continue
        if item.id in seen:
            raise ValueError(f"duplicate web.slot.contribution id: {item.id}")
        seen.add(item.id)
        if slot_spec(item.slot or "") is None:
            raise ValueError(f"slot is not allowed: {item.slot}")
        renderer = item.renderer
        if item.settings.get("kind", "item") == "item" and renderer.get("kind") != RENDERER_WEBUI_TREE:
            raise ValueError(f"renderer.kind must be {RENDERER_WEBUI_TREE}, got {renderer.get('kind')!r}")
        if item.locale_id is not None and item.locale_id not in locales:
            raise ValueError(f"localeId is not declared by this package: {item.locale_id}")
        if item.settings.get("kind") == "item":
            target = (item.settings["scope"], item.settings["instance_key"])
            if target in settings_targets:
                raise ValueError(f"duplicate settings render target: {target}")
            settings_targets.add(target)
        contributions.append(item)

    # Locale items are part of the catalog too, so callers can resolve titles.
    contributions.extend(locales.values())
    return tuple(contributions)


def _parse_item(record: ActivePackageRecord, raw: object) -> UiContribution:
    if not isinstance(raw, Mapping):
        raise ValueError("ui.items entry must be a mapping")
    item_type = raw.get("type")
    if item_type not in _UI_TYPES:
        raise ValueError(f"unsupported ui item type: {item_type!r}")
    item_id = raw.get("id")
    if not isinstance(item_id, str) or not item_id.strip():
        raise ValueError("ui item id is required")
    order = raw.get("order", 0)
    if not isinstance(order, int) or isinstance(order, bool):
        raise ValueError(f"ui item order must be an integer: {order!r}")
    panel = raw.get("panel") or {}
    if not isinstance(panel, Mapping):
        raise ValueError("ui item panel must be a mapping")
    renderer = raw.get("renderer") or {}
    if not isinstance(renderer, Mapping):
        raise ValueError("ui item renderer must be a mapping")
    locale_id = raw.get("localeId")
    if locale_id is not None and not isinstance(locale_id, str):
        raise ValueError("ui item localeId must be a string")
    resources = _parse_resources(record, raw.get("resources"))
    settings = parse_settings(raw) if item_type == "web.slot.contribution" and raw.get("slot") == SETTINGS_SLOT else {}
    if "location" in raw and raw.get("slot") != SETTINGS_SLOT:
        raise ValueError("location is only supported by application.settings")
    return UiContribution(
        package_id=record.package_id,
        type=item_type,
        id=item_id.strip(),
        slot=raw.get("slot") if isinstance(raw.get("slot"), str) else None,
        order=order,
        panel=dict(panel),
        locale_id=locale_id,
        renderer=dict(renderer),
        resources=resources,
        settings=settings,
    )


def _parse_resources(record: ActivePackageRecord, raw: object) -> dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise ValueError("ui item resources must be a mapping")
    root = record.root.resolve()
    resources: dict[str, str] = {}
    for language, relative in raw.items():
        if not isinstance(language, str) or not isinstance(relative, str) or not relative.strip():
            raise ValueError("ui item resources entries must be string paths")
        path = (root / relative).resolve()
        if not path.is_relative_to(root):
            raise ValueError(f"resource path escapes package root: {relative}")
        if not path.is_file():
            raise ValueError(f"resource file does not exist: {relative}")
        resources[language] = relative
    return resources
