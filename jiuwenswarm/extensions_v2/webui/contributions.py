# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Discover and validate the ``ui`` section of an active package manifest."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import yaml
from openjiuwen.core.extension import ActivePackageRecord

from jiuwenswarm.extensions_v2.webui.constants import RENDERER_WEBUI_TREE, SESSION_SLOTS

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


@dataclass(frozen=True, slots=True)
class UiCatalog:
    """Valid UI contributions and per-package validation errors."""

    items: tuple[UiContribution, ...] = ()
    errors: Mapping[str, str] = field(default_factory=dict)

    def allows_slot(self, slot_id: str) -> bool:
        return any(item.type == "web.slot.contribution" and item.slot == slot_id for item in self.items)

    def slots(self) -> frozenset[str]:
        return frozenset(
            item.slot for item in self.items if item.type == "web.slot.contribution" and item.slot is not None
        )


class WebUICatalogService:
    """Cache the UI catalog until the active package set changes."""

    def __init__(self, records_provider: Callable[[], Sequence[ActivePackageRecord]]) -> None:
        self._records_provider = records_provider
        self._signature: tuple[tuple[str, str, int], ...] = ()
        self._catalog = UiCatalog()

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
        except (OSError, UnicodeError, yaml.YAMLError, ValueError) as exc:
            errors[record.package_id] = str(exc)
            continue
        if package_items is None:
            continue
        items.extend(package_items)
    return UiCatalog(items=tuple(items), errors=errors)


def load_package_ui(record: ActivePackageRecord) -> tuple[UiContribution, ...] | None:
    """Return validated UI items for one package, or None when it declares none."""

    manifest_path = record.root / "manifest.yaml"
    raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        raise ValueError("manifest.yaml top level must be a mapping")
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
    for raw_item in raw_items:
        item = _parse_item(record, raw_item)
        if item.type != "web.slot.contribution":
            continue
        if item.id in seen:
            raise ValueError(f"duplicate web.slot.contribution id: {item.id}")
        seen.add(item.id)
        if item.slot not in SESSION_SLOTS:
            raise ValueError(f"slot is not allowed: {item.slot}")
        renderer = item.renderer
        if renderer.get("kind") != RENDERER_WEBUI_TREE:
            raise ValueError(f"renderer.kind must be {RENDERER_WEBUI_TREE}, got {renderer.get('kind')!r}")
        if item.locale_id is not None and item.locale_id not in locales:
            raise ValueError(f"localeId is not declared by this package: {item.locale_id}")
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
