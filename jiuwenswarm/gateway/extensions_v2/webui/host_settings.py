# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Host-owned application settings: splash preferences and host methods (S_10).

The splash order is a cross-package setting, so it belongs to the host and not
to any package store. The host methods are answered by a BUILTIN
``gateway.request`` handler: USER request policies decide first, so a package
that requires a login also guards these methods (S_01 section 6).
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from jiuwenswarm.gateway.extensions_v2.models import GatewayRequestInput, GatewayRequestOutput, GatewayRoute

if TYPE_CHECKING:
    from jiuwenswarm.gateway.extensions_v2.runtime import GatewayRuntime

logger = logging.getLogger(__name__)

HOST_DIR = "host"
SPLASH_FILE = "splash.json"
METHOD_SETTINGS_PAGES = "webui.settings.pages"
METHOD_SPLASH_SETTINGS = "webui.splash.settings"
METHOD_SPLASH_UPDATE = "webui.splash.settings.update"
METHOD_UPLOAD_BEGIN = "webui.upload.begin"
HOST_METHODS = frozenset({METHOD_SETTINGS_PAGES, METHOD_SPLASH_SETTINGS, METHOD_SPLASH_UPDATE, METHOD_UPLOAD_BEGIN})
CODE_BAD_REQUEST = "BAD_REQUEST"


class HostRequestError(ValueError):
    """A host method refused its parameters; ``code`` goes to the deny error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _ids(value: Any) -> list[str] | None:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        return None
    return value if len(set(value)) == len(value) else None


class SplashPreferences:
    """Install order, user order and disabled splash providers (S_10 section 4)."""

    def __init__(self, data_dir: Path) -> None:
        self.path = Path(data_dir) / HOST_DIR / SPLASH_FILE
        self.installed: list[str] = []
        self.order: list[str] | None = None
        self.disabled: list[str] = []

    @property
    def customized(self) -> bool:
        """Whether the user saved an order."""
        return self.order is not None

    def load(self) -> None:
        """Read the saved record; an unreadable record counts as empty."""
        self.installed, self.order, self.disabled = [], None, []
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or raw.get("version") != 1:
                raise ValueError("unexpected splash preferences")
            installed, disabled, order = _ids(raw.get("installed")), _ids(raw.get("disabled")), raw.get("order")
            if installed is None or disabled is None or (order is not None and _ids(order) is None):
                raise ValueError("unexpected splash preferences")
        except (OSError, UnicodeError, ValueError) as exc:
            logger.warning("[GatewayExtensions] ignore splash preferences %s: %s", self.path, exc)
            return
        self.installed, self.order, self.disabled = installed, order, disabled

    def _save(self) -> None:
        directory = self.path.parent
        try:
            if directory.is_symlink():
                raise OSError("host settings directory must not be a symlink")
            directory.mkdir(parents=True, exist_ok=True)
            temporary = directory / f".{SPLASH_FILE}.{uuid4().hex}.tmp"
            temporary.write_text(json.dumps({
                "version": 1, "installed": self.installed, "order": self.order, "disabled": self.disabled,
            }, ensure_ascii=False), encoding="utf-8")
            os.replace(temporary, self.path)
        except OSError as exc:
            logger.warning("[GatewayExtensions] cannot save splash preferences %s: %s", self.path, exc)

    def note_installed(self, package_id: str) -> None:
        """Append a provider the first time it is published."""
        if package_id not in self.installed:
            self.installed.append(package_id)
            self._save()

    def forget(self, package_id: str) -> None:
        """Drop an uninstalled package from every record."""
        changed = package_id in self.installed or package_id in self.disabled or package_id in (self.order or ())
        if not changed:
            return
        self.installed = [item for item in self.installed if item != package_id]
        self.disabled = [item for item in self.disabled if item != package_id]
        if self.order is not None:
            self.order = [item for item in self.order if item != package_id]
        self._save()

    def ordered(self, active: Iterable[str]) -> list[str]:
        """Active providers in effective order: user order first, then install order."""
        remaining = list(active)
        rank = {package_id: index for index, package_id in enumerate(self.installed)}
        remaining.sort(key=lambda package_id: rank.get(package_id, len(rank)))
        if self.order is None:
            return remaining
        first = [package_id for package_id in self.order if package_id in remaining]
        return first + [package_id for package_id in remaining if package_id not in first]

    def enabled(self, package_id: str) -> bool:
        return package_id not in self.disabled

    def update(self, params: Mapping[str, Any], active: list[str]) -> set[str]:
        """Apply a validated update and return the providers it newly disabled."""
        if not params or set(params) - {"order", "disabled"}:
            raise HostRequestError(CODE_BAD_REQUEST, "update accepts order and disabled")
        order = self.order
        if "order" in params:
            order = params["order"]
            if order is not None and (_ids(order) is None or set(order) - set(active)):
                raise HostRequestError(CODE_BAD_REQUEST, "order lists unique active splash providers")
        disabled = self.disabled
        if "disabled" in params:
            chosen = _ids(params["disabled"])
            if chosen is None or set(chosen) - set(active):
                raise HostRequestError(CODE_BAD_REQUEST, "disabled lists unique active splash providers")
            # Inactive packages keep their record until they are uninstalled.
            disabled = [item for item in self.disabled if item not in active] + chosen
        newly = set(disabled) - set(self.disabled)
        self.order = list(order) if order is not None else None
        self.disabled = list(disabled)
        self._save()
        return newly


def _empty(params: Mapping[str, Any]) -> None:
    if params:
        raise HostRequestError(CODE_BAD_REQUEST, "this method takes no parameters")


def splash_settings(runtime: GatewayRuntime) -> dict[str, Any]:
    """Every splash provider in effective order, with its enabled flag (S_10 section 3.2)."""
    browser = runtime.browser
    preferences = browser.splash_preferences
    providers = []
    for provider in browser.splash_providers(include_disabled=True):
        package_id = provider["package_id"]
        providers.append({**provider, "title": browser.package_title(package_id),
                          "enabled": preferences.enabled(package_id)})
    return {"providers": providers, "customized": preferences.customized}


async def answer_host_method(runtime: GatewayRuntime, request: GatewayRequestInput) -> dict[str, Any]:
    """Answer one host method or raise :class:`HostRequestError`."""
    params = dict(request.params)
    browser = runtime.browser
    if request.method == METHOD_SETTINGS_PAGES:
        _empty(params)
        return {"pages": browser.settings_pages()}
    if request.method == METHOD_SPLASH_SETTINGS:
        _empty(params)
        return splash_settings(runtime)
    if request.method == METHOD_SPLASH_UPDATE:
        active = [provider["package_id"] for provider in browser.splash_providers(include_disabled=True)]
        newly = browser.splash_preferences.update(params, active)
        for package_id in sorted(newly):
            await browser.close_splashes(package_id, "disabled")
        return splash_settings(runtime)
    return await browser.transfers.begin_upload(request)


def build_host_request_handler(runtime: GatewayRuntime):
    """BUILTIN ``gateway.request`` handler for host methods, registered after the gate."""

    async def host_request(event, ops, next_handler):
        del ops
        request = event.event_input
        if (not isinstance(request, GatewayRequestInput) or request.transport != "websocket"
                or request.method not in HOST_METHODS):
            return await next_handler(event)
        try:
            response = await answer_host_method(runtime, request)
        except HostRequestError as exc:
            return GatewayRequestOutput(handled=True, route=GatewayRoute.DENY.value,
                                        error={"code": exc.code, "message": str(exc)})
        return GatewayRequestOutput(handled=True, route=GatewayRoute.CONSUME.value, response=response)

    return host_request
