from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Callable

logger = logging.getLogger("jiuwenswarm.channels.desktop")

TrayState = str  # "idle" | "running" | "waiting" | "error"

_ICON_SIZE = 64
_DOT_RADIUS = 13
_STATE_COLORS: dict[str, tuple[int, int, int, int]] = {
    "idle": (138, 145, 160, 255),
    "running": (18, 99, 224, 255),
    "waiting": (217, 155, 15, 255),
    "error": (214, 62, 62, 255),
}
_STATUS_LABELS: dict[str, str] = {
    "idle": "Idle",
    "running": "Running…",
    "waiting": "Needs your input",
    "error": "Last run failed",
}


class TrayController:
    """System tray icon: idle/running/error glyph, Open/Quit menu, native toasts.

    Must be constructed and started on the main thread, before the host's own
    native event loop starts (pywebview's ``webview.start()``) -- it uses
    pystray's ``run_detached()`` on every platform, which sets up the icon and
    then rides the *host's* main loop rather than running its own. On macOS
    this matters concretely: pystray's Cocoa backend talks to the same shared
    ``NSApplication`` singleton pywebview's own window uses, so anything that
    assumes it owns that run loop (starting it from a background thread,
    stopping it) would take pywebview's window down too.

    Construction and ``start()`` can fail on platforms where no tray backend is
    available (e.g. Linux without GTK/AppIndicator installed) -- callers must
    treat that as non-fatal and continue running without a tray.
    """

    def __init__(
        self,
        display_name: str,
        icon_path: Path | None,
        on_open: Callable[[], None],
        on_quit: Callable[[], None],
    ) -> None:
        import pystray

        self._pystray = pystray
        self._display_name = display_name
        self._on_open = on_open
        self._on_quit = on_quit
        self._base_image = self._load_base_image(icon_path)
        self._state: TrayState = "idle"
        self._icon = pystray.Icon(
            "jiuwenswarm-tray",
            icon=self._render_icon("idle"),
            title=display_name,
            menu=self._build_menu(_STATUS_LABELS["idle"]),
        )

    def _build_menu(self, status_text: str):
        pystray = self._pystray
        return pystray.Menu(
            pystray.MenuItem(self._display_name, self._handle_open, default=True),
            pystray.MenuItem(status_text, None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self._handle_quit),
        )

    @staticmethod
    def _load_base_image(icon_path: Path | None):
        from PIL import Image

        if icon_path is not None and icon_path.is_file():
            try:
                return Image.open(icon_path).convert("RGBA").resize((_ICON_SIZE, _ICON_SIZE))
            except Exception:  # noqa: BLE001
                logger.warning("[desktop-tray] failed to load app icon %s, using placeholder", icon_path)
        return Image.new("RGBA", (_ICON_SIZE, _ICON_SIZE), (91, 100, 242, 255))

    def _render_icon(self, state: TrayState):
        from PIL import ImageDraw

        image = self._base_image.copy()
        draw = ImageDraw.Draw(image)
        color = _STATE_COLORS.get(state, _STATE_COLORS["idle"])
        center = _ICON_SIZE - _DOT_RADIUS - 2
        draw.ellipse(
            (center - _DOT_RADIUS, center - _DOT_RADIUS, center + _DOT_RADIUS, center + _DOT_RADIUS),
            fill=color,
            outline=(255, 255, 255, 255),
            width=3,
        )
        return image

    def _handle_open(self, icon=None, item=None) -> None:
        try:
            self._on_open()
        except Exception:  # noqa: BLE001
            logger.exception("[desktop-tray] open handler failed")

    def _handle_quit(self, icon=None, item=None) -> None:
        try:
            self._on_quit()
        except Exception:  # noqa: BLE001
            logger.exception("[desktop-tray] quit handler failed")

    def start(self) -> None:
        # run_detached() sets the icon up and returns immediately, relying on
        # the host's own main loop (started afterward by webview.start()) to
        # pump its events -- see the class docstring for why this, and not
        # `.run()` on our own thread, is required on macOS and Linux/GTK.
        self._icon.run_detached()

    def stop(self) -> None:
        if sys.platform == "darwin":
            # pystray's darwin backend's stop() calls NSApplication.stop_() on
            # the shared app instance pywebview's window also runs on -- this
            # would end pywebview's own run loop, not just the tray. The
            # status item is removed by the OS when this process exits instead.
            return
        try:
            self._icon.stop()
        except Exception:  # noqa: BLE001
            logger.warning("[desktop-tray] failed to stop tray icon cleanly")

    def update_state(self, state: TrayState, title: str, body: str, job_id: str) -> None:
        del job_id  # not surfaced in the tray UI yet; reserved for a future "recent activity" menu
        if state not in _STATE_COLORS:
            state = "idle"
        self._state = state
        try:
            self._icon.icon = self._render_icon(state)
            self._icon.menu = self._build_menu(_STATUS_LABELS[state])
        except Exception:  # noqa: BLE001
            logger.warning("[desktop-tray] failed to update tray icon state")
        if title:
            self.notify(title, body)

    def notify(self, title: str, body: str) -> None:
        try:
            self._icon.notify(body, title)
        except Exception:  # noqa: BLE001
            logger.warning("[desktop-tray] failed to show notification")
