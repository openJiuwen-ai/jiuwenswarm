"""Gateway entry point of 主题背景 (DESIGN.md section 4).

One handler serves the three contributions of this package: the application background (plus the
appearance of the same connection), the startup splash (including previews) and the settings page.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import shutil
from pathlib import Path
from typing import Any

from agent_gateway.extensions_v2.webui.call_context import trusted_connection
from agent_gateway.extensions_v2.webui.models import (
    RenderMode, RenderNode, RenderTarget, UiActionOutput, UiRenderInput,
)

from . import BUILTIN_THEMES, PACKAGE_ID
from .catalog import Catalog, Theme, load_catalog
from .media import appearance, background_node, splash_node, with_query
from .prefs import (
    PLAYED_KEY, PREFS_KEY, defaults, normalize, record_played, splash_decision, validate_setting,
)
from .schema import ThemeValidationError
from .transfer import BACKUP_NAME, build_backup, import_staged, remove_theme, restore_staged, stage_upload
from .views import settings_page

BACKGROUND = "application.background"
SPLASH = "application.splash"
#: Shared with AgentServer packages (S_18); the Gateway hands this package only its declared ``main`` item.
SETTINGS = "application.settings"
SWITCHES = {"boot_enabled", "boot_audio"}
PERCENT_SLIDERS = {"boot_volume", "surface_opacity", "background_dim"}
#: What each slider changes on the calling connection; the page itself is not re-rendered while
#: dragging, because the host rejects actions that carry an older revision of the page.
SLIDER_REFRESH = {"boot_volume": None, "surface_opacity": "appearance",
                  "background_dim": "background", "background_blur": "background"}
logger = logging.getLogger("workswarm_theme")


def _ok(status: str) -> UiActionOutput:
    return UiActionOutput(accepted=True, status=status)


class ThemeExtension:
    """Per-process state; preferences live in ``ops.store`` and themes on disk."""

    def __init__(self) -> None:
        self.package_root = Path(__file__).resolve().parents[1]
        self.catalog: Catalog | None = None
        self._prefs_lock = asyncio.Lock()
        self.notices: dict[str, tuple[str, str]] = {}
        self.previews: dict[str, str] = {}
        self.splashes: dict[str, dict[str, Any]] = {}
        self.background_keys: dict[str, str] = {}
        self.settings_keys: dict[str, str] = {}
        self.busy: set[str] = set()
        self._tasks: set[asyncio.Task] = set()

    def register(self, registrar) -> None:
        match = {"target.package_id": PACKAGE_ID}
        for event in ("ui.ready", "ui.click", "ui.change", "ui.closed"):
            registrar.register_event_handler(event, self.on_ui, match)
        registrar.register_event_handler("gateway.disconnect", self.on_disconnect)

    async def on_disconnect(self, event, ops, next_handler):
        connection = event.event_input.connection_id
        for table in (self.notices, self.previews, self.splashes, self.background_keys, self.settings_keys):
            table.pop(connection, None)
        self.busy.discard(connection)
        return await next_handler(event)

    # ── shared state ─────────────────────────────────────────────────────

    async def _catalog(self, ops, *, refresh: bool = False) -> Catalog:
        if self.catalog is None or refresh:
            data_root = await ops.data.root()
            self.catalog = await asyncio.to_thread(load_catalog, self.package_root, data_root, BUILTIN_THEMES)
        return self.catalog

    async def _prefs(self, ops) -> dict[str, Any]:
        return normalize(await ops.store.get(PREFS_KEY, None))

    async def _update_prefs(self, ops, change) -> dict[str, Any]:
        async with self._prefs_lock:
            prefs = await self._prefs(ops)
            change(prefs)
            await ops.store.set(PREFS_KEY, prefs)
            return prefs

    @staticmethod
    def _url_for(ops, theme: Theme):
        async def url(relative: str) -> str:
            if theme.source == "builtin":
                return await ops.ui.asset_url(f"{theme.asset_prefix}/{relative}")
            address = await ops.ui.asset_url(f"{theme.asset_prefix}/{relative}", source="data")
            stamp = theme.content_stamp
            return with_query(address, {"content": stamp}) if stamp else address
        return url

    @staticmethod
    async def _render(ops, slot: str, instance_key: str, root: RenderNode | None) -> None:
        await ops.ui.render(UiRenderInput(
            target=RenderTarget(session_id="", slot_id=slot, instance_key=instance_key),
            root=root, mode=RenderMode.REPLACE if root is not None else RenderMode.CLEAR,
        ))

    # ── background and appearance ────────────────────────────────────────

    async def _show_background(self, ops, connection: str, *, render: bool = True, apply_appearance: bool = True) -> None:
        """Render the background (when that slot is mounted here) and apply the matching appearance."""
        catalog = await self._catalog(ops)
        prefs = await self._prefs(ops)
        theme = catalog.get(prefs["active_theme"])
        root = await background_node(theme, prefs["settings"], self._url_for(ops, theme)) if theme else None
        if render and connection in self.background_keys:
            await self._render(ops, BACKGROUND, self.background_keys[connection], root)
        if apply_appearance:
            await ops.appearance.apply(appearance(theme, prefs["settings"], has_background=root is not None))

    # ── splash ───────────────────────────────────────────────────────────

    async def _splash_ready(self, ops, connection: str, target, payload) -> UiActionOutput:
        catalog = await self._catalog(ops)
        prefs = await self._prefs(ops)
        settings = prefs["settings"]
        preview = payload.get("preview") is True
        chosen = self.previews.pop(connection, None) if preview else None
        theme = catalog.get(chosen) or catalog.get(prefs["active_theme"])
        if theme is None:
            return _ok("skipped")
        reduced = payload.get("reduced_motion") is True or settings["motion"] == "reduced"
        played = await ops.store.get(PLAYED_KEY, {})
        decision = splash_decision(
            theme.pack.manifest["boot"], theme.id, theme.pack.version, settings, played if isinstance(played, dict) else {},
            today=datetime.date.today().isoformat(), preview=preview, reduced=reduced,
        )
        root = None
        if decision != "skip":
            root = await splash_node(theme, settings, self._url_for(ops, theme), decision=decision, reduced=reduced)
        if root is None:
            self.splashes.pop(connection, None)
            return _ok("skipped")
        await self._render(ops, SPLASH, target.instance_key, root)
        self.splashes[connection] = {"theme": theme.id, "version": theme.pack.version, "preview": preview}
        return _ok("playing")

    async def _splash_closed(self, ops, connection: str, payload) -> UiActionOutput:
        info = self.splashes.pop(connection, None)
        if info and not info["preview"] and payload.get("reason") in ("complete", "skip"):
            async with self._prefs_lock:
                played = await ops.store.get(PLAYED_KEY, {})
                await ops.store.set(PLAYED_KEY, record_played(played if isinstance(played, dict) else {}, info["theme"],
                                                              info["version"], datetime.date.today().isoformat()))
        return _ok("closed")

    # ── settings page ────────────────────────────────────────────────────

    async def _show_settings(self, ops, connection: str) -> None:
        if connection not in self.settings_keys:
            return
        catalog = await self._catalog(ops)
        prefs = await self._prefs(ops)
        covers = {}
        for theme in catalog.ordered():
            preview = theme.pack.manifest.get("preview")
            if preview:
                covers[theme.id] = await self._url_for(ops, theme)(preview)
        root = settings_page(catalog, prefs, covers, notice=self.notices.get(connection),
                             busy=connection in self.busy)
        await self._render(ops, SETTINGS, self.settings_keys[connection], root)

    def _notice(self, connection: str, tone: str, message: str) -> None:
        self.notices[connection] = (tone, message)

    async def _apply_setting(self, ops, key: str, value: Any) -> None:
        normalized = validate_setting(key, value)
        await self._update_prefs(ops, lambda prefs: prefs["settings"].__setitem__(key, normalized))

    async def _action(self, ops, connection: str, event_type: str, payload) -> str:
        action = payload.get("action_id")
        args = payload.get("args") or {}
        data = payload.get("data") or {}
        catalog = await self._catalog(ops)
        theme = catalog.get(args.get("theme"))
        if action == "use" and theme:
            await self._update_prefs(ops, lambda prefs: prefs.__setitem__("active_theme", theme.id))
            self._notice(connection, "success", f"已切换到“{theme.pack.name}”")
        elif action == "original":
            await self._update_prefs(ops, lambda prefs: prefs.__setitem__("active_theme", None))
            self._notice(connection, "success", "已恢复原版界面")
        elif action == "preview" and theme:
            self.previews[connection] = theme.id
            answer = await ops.splash.preview()
            if answer.get("status") != "requested":
                self.previews.pop(connection, None)
                self._notice(connection, "error", "无法播放预览，请稍后重试")
            return "previewing"
        elif action == "delete" and theme and theme.source == "imported":
            await asyncio.to_thread(remove_theme, await ops.data.root(), theme.id)
            await self._update_prefs(ops, lambda prefs: prefs.__setitem__(
                "active_theme", None if prefs["active_theme"] == theme.id else prefs["active_theme"]))
            await self._catalog(ops, refresh=True)
            self._notice(connection, "success", f"已删除“{theme.pack.name}”")
        elif action in SWITCHES and event_type == "ui.change":
            await self._apply_setting(ops, action, data.get("checked"))
        elif action == "boot_policy":
            await self._apply_setting(ops, action, None if data.get("value") == "theme" else data.get("value"))
        elif action == "motion":
            await self._apply_setting(ops, action, data.get("value"))
        elif action == "operator":
            await self._apply_setting(ops, "operator_name", data.get("operator_name"))
            self._notice(connection, "success", "研究员代号已保存")
        elif action in SLIDER_REFRESH:
            value = data.get("value")
            if isinstance(value, (int, float)) and not isinstance(value, bool) and action in PERCENT_SLIDERS:
                value = value / 100
            await self._apply_setting(ops, action, value)
            return "slider"
        elif action == "reset":
            await self._update_prefs(ops, lambda prefs: prefs.update(defaults()))
            await ops.store.set(PLAYED_KEY, {})
            self._notice(connection, "success", "已恢复默认设置")
        elif action in ("import", "restore", "export"):
            await self._start_transfer(ops, connection, action, data)
            return "transfer"
        else:
            return "ignored"
        return "updated"

    # ── import, restore and export (DESIGN.md section 4.4) ───────────────

    async def _start_transfer(self, ops, connection: str, action: str, data) -> None:
        """Show the busy page, then run the transfer in the background.

        The busy page is rendered before the task starts so it can never arrive after the task's final
        page: two renders in flight would make a click in between carry an outdated page revision.
        """
        if connection in self.busy:
            self._notice(connection, "error", "上一项操作还没有完成")
            await self._show_settings(ops, connection)
            return
        data_root = await ops.data.root()
        staged = stage_upload(data_root, data) if action in ("import", "restore") else None
        self.busy.add(connection)
        self._notice(connection, "info", {"import": "正在导入主题……", "restore": "正在从备份恢复……",
                                          "export": "正在生成备份……"}[action])
        try:
            await self._show_settings(ops, connection)
        except Exception:
            self.busy.discard(connection)
            if staged is not None:
                shutil.rmtree(staged, ignore_errors=True)
            raise
        task = asyncio.get_running_loop().create_task(self._run_transfer(ops, connection, action, staged, data_root))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run_transfer(self, ops, connection: str, action: str, staged: Path | None, data_root: Path) -> None:
        reserved = {theme.id for theme in (await self._catalog(ops)).themes.values() if theme.source == "builtin"}
        try:
            if action == "import":
                pack, notes = await asyncio.to_thread(import_staged, staged, data_root, reserved)
                message = f"已导入“{pack.name}” {pack.version}"
                self._notice(connection, "success", message + ("；" + "；".join(notes) if notes else ""))
            elif action == "restore":
                stored, restored, messages = await asyncio.to_thread(restore_staged, staged, data_root, reserved)
                await self._update_prefs(ops, lambda prefs: prefs.update(normalize(stored)))
                summary = f"已从备份恢复设置与 {len(restored)} 个主题"
                self._notice(connection, "error" if messages else "success",
                             summary + ("；未能恢复：" + "；".join(messages) if messages else ""))
            else:
                prefs = await self._prefs(ops)
                path = await asyncio.to_thread(build_backup, data_root, prefs, await self._catalog(ops))
                answer = await ops.ui.download(path, filename=BACKUP_NAME)
                ok = answer.get("status") == "offered"
                self._notice(connection, "success" if ok else "error", "备份已生成，请选择保存位置" if ok else "备份未能送达浏览器")
        except ThemeValidationError as exc:
            self._notice(connection, "error", "；".join(exc.errors[:4]))
        except Exception as exc:  # noqa: BLE001 - report on the page instead of failing silently
            logger.exception("[workswarm-theme] %s failed", action)
            self._notice(connection, "error", f"操作失败：{exc}")
        finally:
            self.busy.discard(connection)
        await self._catalog(ops, refresh=True)
        try:
            await self._show_background(ops, connection)
            await self._show_settings(ops, connection)
        except Exception:  # noqa: BLE001 - the browser may have gone away meanwhile
            logger.warning("[workswarm-theme] cannot refresh connection %s after %s", connection, action)

    # ── dispatch ─────────────────────────────────────────────────────────

    async def on_ui(self, event, ops, next_handler):
        request = event.event_input
        target = request.target
        connection = trusted_connection(ops)
        payload = request.payload
        name = request.event_type
        if target.slot_id == BACKGROUND and name == "ui.ready":
            self.background_keys[connection] = target.instance_key
            await self._show_background(ops, connection)
            return _ok("ready")
        if target.slot_id == SPLASH and name == "ui.ready":
            return await self._splash_ready(ops, connection, target, payload)
        if target.slot_id == SPLASH and name == "ui.closed":
            return await self._splash_closed(ops, connection, payload)
        if target.slot_id == SETTINGS:
            if name == "ui.ready":
                self.settings_keys[connection] = target.instance_key
                await self._show_settings(ops, connection)
                return _ok("ready")
            action = payload.get("action_id")
            if action not in SLIDER_REFRESH:
                self.notices.pop(connection, None)
            try:
                status = await self._action(ops, connection, name, payload)
            except ValueError as exc:
                self._notice(connection, "error", str(exc))
                status = "invalid"
            if status == "slider":
                refresh = SLIDER_REFRESH[action]
                if refresh:
                    # Dim and blur only change the background; the appearance depends on the panel opacity alone.
                    await self._show_background(ops, connection, render=refresh == "background",
                                                apply_appearance=refresh == "appearance")
            elif status != "transfer":
                if status == "updated":
                    await self._show_background(ops, connection)
                if status != "ignored" or connection in self.notices:
                    await self._show_settings(ops, connection)
            return _ok(status)
        return await next_handler(event)


def create_extension() -> ThemeExtension:
    return ThemeExtension()
