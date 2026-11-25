"""Local listener lifetime: one per activation, released when the owner dies.

The host never calls back into a package on reload/uninstall, so the lifetime is
anchored on a *weak* reference to the extension instance: the moment the
activation becomes unreachable the listener is closed (contract §5.5 R8 mode A,
≤5s). ``weakref.finalize`` is the second safety net for GC-triggered release.

Mode B (same instance switching ports) is not produced by this build: it follows
R3, one listener per activation, so ``/health.draining_ports`` stays empty.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
import types
import weakref
from typing import Any, Callable

from .http_server import BIND_ADDRESS, ServiceContext, TrackingHTTPServer, build_handler

LOGGER = logging.getLogger(__name__)

#: Registry module name deliberately outside the package prefix: a reload pops
#: every ``team_collab_viz.*`` module, but this one survives so the new
#: activation can see (and retire) the previous listener.
REGISTRY_MODULE = "_tcv_local_services"
WATCHDOG_INTERVAL_SECONDS = 0.2
RETIRE_GRACE_SECONDS = 2.0


def _registry() -> dict[str, Any]:
    """Return the process-wide service registry, creating it on first use."""
    module = sys.modules.get(REGISTRY_MODULE)
    if module is None:
        module = types.ModuleType(REGISTRY_MODULE)
        sys.modules[REGISTRY_MODULE] = module
    services = getattr(module, "services", None)
    if services is None:
        services = {}
        setattr(module, "services", services)
    return services


class LocalService:
    """One HTTP listener owned by one package activation."""

    def __init__(self, context: ServiceContext, owner: Any) -> None:
        """Bind and listen on ``127.0.0.1:0``; the port comes from the socket (R2)."""
        self.context = context
        self._owner_ref = weakref.ref(owner)
        self._closed = threading.Event()
        self._serve_thread: threading.Thread | None = None
        self._watchdog_thread: threading.Thread | None = None
        self.started_at = int(time.time() * 1000)
        self.closed_at: int | None = None
        self.bind_error: str | None = None
        self.httpd: TrackingHTTPServer | None = None
        try:
            self.httpd = TrackingHTTPServer((BIND_ADDRESS, 0), build_handler(context))
        except OSError as error:
            self.bind_error = repr(error)
            context.note_bind_error(self.bind_error)
            LOGGER.error("team-collab-viz: listen failed on %s: %r", BIND_ADDRESS, error)
            raise
        self.httpd.daemon_threads = True
        # R2: the port comes from the listening socket itself, never from config.
        self.port = int(self.httpd.socket.getsockname()[1])
        context.note_bind(self.port)

    # ------------------------------------------------------------- lifetime

    def start(self) -> "LocalService":
        """Start serving and the owner watchdog (listening already happened)."""
        self._serve_thread = threading.Thread(target=self._serve, name=f"tcv-http-{self.port}", daemon=True)
        self._watchdog_thread = threading.Thread(target=self._watch, name=f"tcv-watch-{self.port}", daemon=True)
        self._serve_thread.start()
        self._watchdog_thread.start()
        LOGGER.info("team-collab-viz: listening on http://%s:%s", BIND_ADDRESS, self.port)
        return self

    def _serve(self) -> None:
        if self.httpd is None:  # pragma: no cover - start() is only called after a bind
            return
        try:
            self.httpd.serve_forever(poll_interval=0.1)
        except Exception as error:  # noqa: BLE001 - never crash the host process
            LOGGER.error("team-collab-viz: serve loop failed on %s: %r", self.port, error)

    def _watch(self) -> None:
        """Close as soon as the activation is gone (R8: interval ≤1s)."""
        while not self._closed.wait(WATCHDOG_INTERVAL_SECONDS):
            if self._owner_ref() is None:
                self.close("owner_collected")
                return

    def retire(self, delay: float = RETIRE_GRACE_SECONDS) -> None:
        """Close a superseded listener after a short grace (R4 ordering)."""
        timer = threading.Timer(max(0.0, delay), self.close, args=("superseded",))
        timer.daemon = True
        timer.start()

    def close(self, reason: str = "closed") -> None:
        """Stop accepting connections, release the port and drop the registry entry."""
        if self._closed.is_set():
            return
        self._closed.set()
        self.context.closing.set()
        if self.httpd is not None:
            try:
                self.httpd.shutdown()
            except Exception:  # noqa: BLE001 - shutdown is best effort
                pass
            try:
                self.httpd.server_close()
            except Exception:  # noqa: BLE001 - server_close is best effort
                pass
            # F2: the listener is gone, so no new connection can arrive; cut the
            # accepted ones instead of waiting for the peer or a 25s long poll.
            # Handlers unwind on their own and close their fd (socket.close is
            # idempotent, so the stdlib close that follows is a no-op).
            try:
                self.httpd.close_active_connections()
            except Exception:  # noqa: BLE001 - never fail a release path
                pass
        if self._serve_thread is not None and self._serve_thread.is_alive():
            self._serve_thread.join(timeout=2.0)
        self.closed_at = int(time.time() * 1000)
        services = _registry()
        if services.get("current") is self:
            services.pop("current", None)
        LOGGER.info(
            "team-collab-viz: listener closed port=%s reason=%s age_ms=%s",
            self.port,
            reason,
            self.closed_at - self.started_at,
        )

    @property
    def closed(self) -> bool:
        """True once the listener has been closed."""
        return self._closed.is_set()


def current_service() -> LocalService | None:
    """The listener owned by the currently loaded activation, if any."""
    service = _registry().get("current")
    return service if isinstance(service, LocalService) else None


def start_service(owner: Any, context: ServiceContext) -> LocalService:
    """Create, register and start the single listener for this activation (R3)."""
    previous = _registry().get("current")
    service = LocalService(context, owner)
    if isinstance(previous, LocalService) and previous is not service and not previous.closed:
        # New listener is already up, so retiring the old one cannot strand a page:
        # render has not happened yet for this activation (R4 ordering).
        previous.retire()
    _registry()["current"] = service
    weakref.finalize(owner, service.close, "finalize")
    return service.start()


def drain_hook(service: LocalService) -> Callable[[], None]:
    """Return a no-arg closer, for callers that only need the release action."""
    return lambda: service.close("requested")
