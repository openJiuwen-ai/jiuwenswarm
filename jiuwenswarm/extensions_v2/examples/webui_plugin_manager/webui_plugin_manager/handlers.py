# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Handle ready and component events for the plugin-manager example."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import replace

from openjiuwen.core.events import Event, EventHandlerOps, EventNext

from jiuwenswarm.extensions_v2.webui.models import (
    RenderTarget,
    UiActionInput,
    UiActionOutput,
    UiRenderInput,
    UiRenderOutput,
)

from .catalog import CATALOG_BY_ID
from .state import (
    FILTER_ALL,
    FILTER_DISABLED,
    FILTER_ENABLED,
    FILTER_UPDATABLE,
    NOTICE_INFO,
    NOTICE_SUCCESS,
    NOTICE_WARNING,
    TAB_ABOUT,
    TAB_PLUGINS,
    ManagerState,
)
from .views import (
    BULK_ACTION,
    CLOSE_DETAILS_ACTION,
    DETAIL_RELOAD_ACTION,
    DETAIL_TOGGLE_ACTION,
    DETAIL_UNINSTALL_ACTION,
    DETAILS_ACTION,
    FILTER_ACTION,
    INSTALL_ACTION,
    INSTANCE,
    MORE_ACTION,
    OPEN_ACTION,
    PACKAGE_ID,
    REFRESH_ACTION,
    SEARCH_ACTION,
    TAB_ACTION,
    TOGGLE_ACTION,
    nav_view,
    page_view,
)

EVENTS = ("ui.click", "ui.change", "ui.select", "ui.search", "ui.clear")
SLOTS = ("application.menu", "application.menu.main")
FILTERS = (FILTER_ALL, FILTER_ENABLED, FILTER_DISABLED, FILTER_UPDATABLE)
TABS = (TAB_PLUGINS, TAB_ABOUT)
MORE_KEYS = ("details", "reload", "uninstall", "folder")
BULK_KEYS = ("enable-all", "disable-all", "reset-demo")


class PluginManagerHandlers:
    """Keep one manager state per real conversation inside this activation."""

    def __init__(self) -> None:
        self._sessions: dict[str, ManagerState] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def handlers(self) -> tuple:
        return tuple(
            (event, self.on_event, {"target.slot_id": slot})
            for slot in SLOTS
            for event in ("ui.ready", *EVENTS)
        )

    async def on_event(
        self,
        event: Event,
        ops: EventHandlerOps | None,
        next_handler: EventNext,
    ) -> UiActionOutput:
        """Render the rail entry and page, and apply component actions."""

        request = event.event_input
        if not isinstance(request, UiActionInput) or request.target.slot_id not in SLOTS:
            return await next_handler(event)
        target = request.target
        if target.package_id not in ("", PACKAGE_ID) or not target.session_id:
            return await next_handler(event)
        if request.event_type != "ui.ready" and target.package_id != PACKAGE_ID:
            return await next_handler(event)
        if ops is None:
            return UiActionOutput(accepted=False, status="unavailable")
        lock = self._locks.setdefault(target.session_id, asyncio.Lock())
        async with lock:
            state = self._sessions.setdefault(target.session_id, ManagerState())
            if request.event_type == "ui.ready":
                return await self._on_ready(ops, target, state)
            return await self._on_action(ops, target, state, request)

    async def _on_ready(
        self,
        ops: EventHandlerOps,
        target: RenderTarget,
        state: ManagerState,
    ) -> UiActionOutput:
        if target.instance_key not in ("", INSTANCE):
            return UiActionOutput(accepted=False, status="invalid_instance")
        root = nav_view() if target.slot_id == "application.menu" else page_view(state)
        output = await self._render(ops, target, root)
        delivered = output is not None and output.status == "delivered"
        state.mounted.add((target.slot_id, INSTANCE))
        return UiActionOutput(
            accepted=delivered,
            status="rendered" if delivered else (output.status if output is not None else "unavailable"),
        )

    async def _on_action(
        self,
        ops: EventHandlerOps,
        target: RenderTarget,
        state: ManagerState,
        request: UiActionInput,
    ) -> UiActionOutput:
        if target.instance_key != INSTANCE:
            return UiActionOutput(accepted=False, status="invalid_instance")
        payload = request.payload
        action = payload.get("action_id")
        data = payload.get("data", {})
        args = payload.get("args", {})
        if not isinstance(action, str) or not isinstance(data, Mapping) or not isinstance(args, Mapping):
            return UiActionOutput(accepted=False, status="invalid")
        if target.slot_id == "application.menu":
            if request.event_type == "ui.click" and action == OPEN_ACTION:
                return UiActionOutput(accepted=True, status="handled")
            return UiActionOutput(accepted=False, status="invalid")
        if not self._apply(request, state, action, data, args):
            return UiActionOutput(accepted=False, status="invalid")
        state.operations += 1
        output = await self._render(ops, target, page_view(state))
        delivered = output is not None and output.status == "delivered"
        return UiActionOutput(
            accepted=delivered,
            status="rendered" if delivered else (output.status if output is not None else "unavailable"),
        )

    def _apply(
        self,
        request: UiActionInput,
        state: ManagerState,
        action: str,
        data: Mapping,
        args: Mapping,
    ) -> bool:
        """Apply one validated action; unknown or mismatched actions are rejected."""

        if action == REFRESH_ACTION and request.event_type == "ui.click":
            state.notify("已刷新插件目录 / Plugin catalog refreshed", NOTICE_SUCCESS)
            return True
        if action == INSTALL_ACTION and request.event_type == "ui.click":
            state.notify(
                "演示模式：真实安装请在输入框执行 /extension install <ZIP 路径>。 / "
                "Demo only: run /extension install <zip path> to install for real.",
                NOTICE_WARNING,
            )
            return True
        if action == SEARCH_ACTION:
            value = data.get("value")
            if request.event_type != "ui.change" or not isinstance(value, str):
                return False
            state.query = value
            return True
        if action == FILTER_ACTION:
            value = data.get("value")
            if request.event_type != "ui.change" or value not in FILTERS:
                return False
            state.status_filter = value
            return True
        if action == TAB_ACTION:
            key = data.get("activeKey")
            if request.event_type != "ui.change" or key not in TABS:
                return False
            state.tab = key
            return True
        if action == TOGGLE_ACTION:
            entry = self._entry_arg(args)
            checked = data.get("checked")
            if request.event_type != "ui.change" or entry is None or type(checked) is not bool:
                return False
            if entry.incompatible_reason:
                state.notify(f"{entry.name} 不兼容当前宿主 / incompatible with this host", NOTICE_WARNING)
                return True
            state.set_enabled(entry.plugin_id, checked)
            state.notify(
                f"已{'启用' if checked else '停用'} {entry.name} / "
                f"{'Enabled' if checked else 'Disabled'} {entry.name}",
                NOTICE_SUCCESS,
            )
            return True
        if action == DETAILS_ACTION and request.event_type == "ui.click":
            entry = self._entry_arg(args)
            if entry is None:
                return False
            state.selected_id = entry.plugin_id
            state.notify(f"正在查看 {entry.name} / Viewing {entry.name}", NOTICE_INFO)
            return True
        if action == CLOSE_DETAILS_ACTION and request.event_type == "ui.click":
            state.selected_id = None
            state.notify("已关闭详情 / Details closed", NOTICE_INFO)
            return True
        if action == MORE_ACTION and request.event_type == "ui.select":
            entry = self._entry_arg(args)
            key = data.get("key")
            if entry is None or key not in MORE_KEYS:
                return False
            return self._apply_more(state, entry, key)
        if action == BULK_ACTION and request.event_type == "ui.select":
            return self._apply_bulk(state, data.get("key"))
        if action == DETAIL_TOGGLE_ACTION and request.event_type == "ui.click":
            entry = self._entry_arg(args)
            if entry is None:
                return False
            if entry.incompatible_reason:
                state.notify(f"{entry.name} 不兼容当前宿主 / incompatible with this host", NOTICE_WARNING)
                return True
            checked = not state.is_enabled(entry)
            state.set_enabled(entry.plugin_id, checked)
            state.notify(
                f"已{'启用' if checked else '停用'} {entry.name} / "
                f"{'Enabled' if checked else 'Disabled'} {entry.name}",
                NOTICE_SUCCESS,
            )
            return True
        if action == DETAIL_RELOAD_ACTION and request.event_type == "ui.click":
            entry = self._entry_arg(args)
            if entry is None:
                return False
            state.notify(f"已请求重新加载 {entry.name}（演示） / Reload requested (demo)", NOTICE_SUCCESS)
            return True
        if action == DETAIL_UNINSTALL_ACTION and request.event_type == "ui.click":
            entry = self._entry_arg(args)
            if entry is None:
                return False
            return self._uninstall(state, entry)
        return False

    def _apply_more(self, state: ManagerState, entry, key: str) -> bool:
        if key == "details":
            state.selected_id = entry.plugin_id
            state.notify(f"正在查看 {entry.name} / Viewing {entry.name}", NOTICE_INFO)
            return True
        if key == "reload":
            state.notify(f"已请求重新加载 {entry.name}（演示） / Reload requested (demo)", NOTICE_SUCCESS)
            return True
        if key == "folder":
            state.notify(f"安装目录 / Install path: {entry.install_path}", NOTICE_INFO)
            return True
        return self._uninstall(state, entry)

    @staticmethod
    def _uninstall(state: ManagerState, entry) -> bool:
        removed = state.uninstall(entry.plugin_id)
        if removed is None:
            state.notify(
                f"{entry.name} 为内置或不兼容插件，演示中不可卸载 / builtin or incompatible; uninstall is blocked",
                NOTICE_WARNING,
            )
            return True
        state.notify(
            f"已卸载 {removed.name}（演示，重新加载后恢复） / Uninstalled (demo; reload restores the catalog)",
            NOTICE_SUCCESS,
        )
        return True

    @staticmethod
    def _apply_bulk(state: ManagerState, key: object) -> bool:
        if key not in BULK_KEYS:
            return False
        if key == "reset-demo":
            state.reset_demo()
            return True
        checked = key == "enable-all"
        changed = state.set_all(checked)
        state.notify(
            f"已{'启用' if checked else '停用'} {changed} 个插件 / "
            f"{'Enabled' if checked else 'Disabled'} {changed} plugin(s)",
            NOTICE_SUCCESS,
        )
        return True

    @staticmethod
    def _entry_arg(args: Mapping):
        plugin_id = args.get("id")
        if not isinstance(plugin_id, str) or plugin_id not in CATALOG_BY_ID:
            return None
        return CATALOG_BY_ID[plugin_id]

    @staticmethod
    async def _render(
        ops: EventHandlerOps,
        target: RenderTarget,
        root,
    ) -> UiRenderOutput | None:
        return await ops.ui.render(UiRenderInput(
            target=replace(target, package_id=PACKAGE_ID, instance_key=INSTANCE),
            root=root,
        ))
