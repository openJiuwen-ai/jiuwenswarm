# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""WebUI event inputs, outputs and downstream frame models.

Rendering shares the AgentServer vocabulary. Gateway actions additionally
carry the browser request context; the runtimes never import each other.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from jiuwenswarm.gateway.extensions_v2.webui.constants import (
    RENDER_SCHEMA_VERSION,
    SlotScope,
    slot_spec,
)


def _empty_props() -> Mapping[str, Any]:
    return MappingProxyType({})


#: Byte limit of every render target string (S_02).
MAX_TARGET_BYTES = 200


def valid_instance_key(value: Any, *, required: bool = False) -> bool:
    """Apply the single instance-key rule shared by declarations, renders, interactions and imports.

    A key is a string without ``|`` and without surrounding whitespace, encodable
    as UTF-8 in at most 200 bytes. The empty key addresses a slot's default
    instance; settings declarations pass ``required=True`` because their key
    names a page. The rule matches AgentServer S_18 and ``validate_target``.

    Args:
        value: The candidate key, as received.
        required: Refuse the empty key.

    Returns:
        True when the key may address an instance.
    """
    if not isinstance(value, str) or "|" in value or value != value.strip() or (required and not value):
        return False
    try:
        return len(value.encode("utf-8")) <= MAX_TARGET_BYTES
    except UnicodeError:
        return False


@dataclass(frozen=True, slots=True)
class RenderTarget:
    """Render instance identity.

    Session slots are keyed by the chat. Application slots are keyed by the
    browser connection. ``connection_id`` is assigned by the server and is not
    part of the browser payload.
    """

    session_id: str
    slot_id: str
    package_id: str = ""
    instance_key: str = ""
    scope: str = ""
    connection_id: str = ""

    @property
    def resolved_scope(self) -> str:
        declared = self.scope.strip()
        if declared:
            return declared
        spec = slot_spec(self.slot_id)
        if spec is None:
            return SlotScope.SESSION.value
        return spec.scope.value

    @property
    def key(self) -> str:
        if self.resolved_scope == SlotScope.APPLICATION.value:
            return "|".join(
                ("application", self.connection_id, self.slot_id, self.package_id, self.instance_key)
            )
        return "|".join((self.session_id, self.slot_id, self.package_id, self.instance_key))

    def to_payload(self) -> dict[str, str]:
        payload = {
            "scope": self.resolved_scope,
            "slot_id": self.slot_id,
            "package_id": self.package_id,
            "instance_key": self.instance_key,
        }
        if self.resolved_scope != SlotScope.APPLICATION.value:
            payload["session_id"] = self.session_id
        return payload


@dataclass(frozen=True, slots=True)
class RenderNode:
    """Render tree node: whitelisted component, props, children and optional events."""

    component: str
    props: Mapping[str, Any] = field(default_factory=_empty_props)
    children: tuple["RenderNode", ...] = ()
    events: Mapping[str, Mapping[str, Any]] | None = None


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
    visible_when: Mapping[str, Any] | None = field(default=None, kw_only=True)


@dataclass(frozen=True, slots=True)
class UiRenderOutput:
    """ui.render event output: status and the revision assigned to this frame."""

    status: RenderStatus
    revision: int = 0


@dataclass(frozen=True, slots=True)
class UiActionInput:
    """Upstream event input (ui.ready / ui.click / ui.change / ui.press_enter / ui.select / ui.search / ui.clear)."""

    event_type: str
    target: RenderTarget
    payload: Mapping[str, Any] = field(default_factory=_empty_props)
    request_context: Mapping[str, Any] = field(default_factory=_empty_props)


@dataclass(frozen=True, slots=True)
class UiActionOutput:
    """Upstream event output."""

    accepted: bool
    status: str
    code: str | None = None


@dataclass(frozen=True, slots=True)
class UiAppearanceInput:
    """``ui.appearance``: submit, or revoke with ``None``, the caller's appearance on its connection (S_08).

    ``package_id`` and ``generation`` identify the calling activation; the Ops method fills them in.
    """

    package_id: str
    generation: str
    appearance: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class UiDownloadInput:
    """``ui.download``: offer one file of the caller's data directory to its connection (S_10 section 7)."""

    package_id: str
    generation: str
    path: str = ""
    filename: str | None = None


@dataclass(frozen=True, slots=True)
class UiSplashPreviewInput:
    """``ui.splash.preview``: replay the caller's splash once on its connection (S_09 section 3.3)."""

    package_id: str
    generation: str


@dataclass(frozen=True, slots=True)
class HostActionOutput:
    """Answer of a host action chain.

    ``status`` is ``rejected`` (with ``code`` and ``message``) when the host refused the call; a USER
    handler that takes the call over returns its own status, for example ``denied`` with a code.
    """

    status: str
    revision: int | None = None
    code: str | None = None
    message: str | None = None


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
    visible_when: Mapping[str, Any] | None = field(default=None, kw_only=True)

    def to_payload(self) -> dict[str, Any]:
        payload = {
            "target": self.target.to_payload(),
            "revision": self.revision,
            "mode": self.mode.value,
            "root": node_to_payload(self.root),
            "schema_version": self.schema_version,
        }
        if self.visible_when is not None:
            payload["visible_when"] = _content_to_payload(self.visible_when)
        return payload


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
        "props": _content_to_payload(node.props),
    }
    if node.events is not None:
        payload["events"] = _content_to_payload(node.events)
    if node.children:
        payload["children"] = [node_to_payload(child) for child in node.children]
    return payload


def _content_to_payload(value: Any) -> Any:
    if isinstance(value, RenderNode):
        return node_to_payload(value)
    if isinstance(value, Mapping):
        return {key: _content_to_payload(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_content_to_payload(item) for item in value]
    return value
