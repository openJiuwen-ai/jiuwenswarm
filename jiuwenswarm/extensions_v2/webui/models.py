# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""WebUI event inputs, outputs and downstream frame models."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from jiuwenswarm.extensions_v2.webui.constants import RENDER_SCHEMA_VERSION


def _empty_props() -> Mapping[str, Any]:
    return MappingProxyType({})


@dataclass(frozen=True, slots=True)
class RenderTarget:
    """Render instance identity: session + slot + package + instance."""

    session_id: str
    slot_id: str
    package_id: str = ""
    instance_key: str = ""

    @property
    def key(self) -> str:
        return "|".join((self.session_id, self.slot_id, self.package_id, self.instance_key))

    def to_payload(self) -> dict[str, str]:
        return {
            "session_id": self.session_id,
            "slot_id": self.slot_id,
            "package_id": self.package_id,
            "instance_key": self.instance_key,
        }


@dataclass(frozen=True, slots=True)
class RenderNode:
    """Render tree node: whitelisted component, props, children and optional action."""

    component: str
    props: Mapping[str, Any] = field(default_factory=_empty_props)
    children: tuple["RenderNode", ...] = ()
    action: Mapping[str, Any] | None = None


class RenderMode(StrEnum):
    """Replace the whole view or clear the instance."""

    REPLACE = "replace"
    CLEAR = "clear"


class RenderStatus(StrEnum):
    """Result of one ui.render event."""

    DELIVERED = "delivered"
    REJECTED = "rejected"
    DELIVERY_FAILED = "delivery_failed"


@dataclass(frozen=True, slots=True)
class UiRenderInput:
    """ui.render event input."""

    target: RenderTarget
    root: RenderNode | None = None
    mode: RenderMode = RenderMode.REPLACE


@dataclass(frozen=True, slots=True)
class UiRenderOutput:
    """ui.render event output: status and the revision assigned to this frame."""

    status: RenderStatus
    revision: int = 0


@dataclass(frozen=True, slots=True)
class UiActionInput:
    """Upstream event input (ui.ready / ui.press / ui.toggle / ui.input / ui.select)."""

    event_type: str
    target: RenderTarget
    payload: Mapping[str, Any] = field(default_factory=_empty_props)


@dataclass(frozen=True, slots=True)
class UiActionOutput:
    """Upstream event output."""

    accepted: bool
    status: str
    code: str | None = None


@dataclass(frozen=True, slots=True)
class ValidationReport:
    """Validation result with a machine-readable code when it fails."""

    ok: bool
    code: str | None = None
    message: str = ""


@dataclass(frozen=True, slots=True)
class RenderFrame:
    """One downstream frame."""

    target: RenderTarget
    revision: int
    mode: RenderMode
    root: RenderNode | None
    schema_version: int = RENDER_SCHEMA_VERSION

    def to_payload(self) -> dict[str, Any]:
        return {
            "target": self.target.to_payload(),
            "revision": self.revision,
            "mode": self.mode.value,
            "root": node_to_payload(self.root),
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True, slots=True)
class ClearFrame:
    """Independent webui.render.clear payload."""

    target: RenderTarget
    revision: int
    reason: str = ""

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "target": self.target.to_payload(),
            "revision": self.revision,
        }
        if self.reason:
            payload["reason"] = self.reason
        return payload


def node_to_payload(node: RenderNode | None) -> dict[str, Any] | None:
    """Convert a render node into the JSON shape sent to the browser."""

    if node is None:
        return None
    payload: dict[str, Any] = {
        "component": node.component,
        "props": dict(node.props),
    }
    if node.action is not None:
        payload["action"] = dict(node.action)
    if node.children:
        payload["children"] = [node_to_payload(child) for child in node.children]
    return payload