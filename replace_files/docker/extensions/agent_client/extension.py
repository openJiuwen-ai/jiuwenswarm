from __future__ import annotations

import threading
from typing import Any

from jiuwenclaw.config import get_config
from jiuwenclaw.extensions.sdk.base import BaseExtension
from jiuwenclaw.extensions.types import ExtensionConfig

from jiuwenclaw.extensions.agent_client.app import create_app


class AgentClientRestExtension(BaseExtension):
    def __init__(self, host: str, port: int) -> None:
        self._host = host
        self._port = port
        self._server: Any | None = None
        self._server_thread: threading.Thread | None = None

    async def initialize(self, config: ExtensionConfig) -> None:
        if self._server is not None:
            return

        try:
            import uvicorn
        except ImportError as exc:
            raise RuntimeError(
                "FastAPI extension requires uvicorn. Please install extension dependencies first."
            ) from exc

        app = create_app()
        uvicorn_config = uvicorn.Config(
            app=app,
            host=self._host,
            port=self._port,
            log_level="info",
        )
        self._server = uvicorn.Server(uvicorn_config)
        self._server_thread = threading.Thread(target=self._server.run, daemon=True)
        self._server_thread.start()

    async def shutdown(self) -> None:
        if self._server is not None:
            self._server.should_exit = True

        if self._server_thread is not None and self._server_thread.is_alive():
            self._server_thread.join(timeout=3.0)

        self._server = None
        self._server_thread = None


def _resolve_rest_config() -> tuple[bool, str, int]:
    cfg = get_config()
    ext_cfg = cfg.get("extensions") if isinstance(cfg, dict) else {}
    rest_cfg = ext_cfg.get("agent_client_rest") if isinstance(ext_cfg, dict) else {}
    if not isinstance(rest_cfg, dict):
        rest_cfg = {}

    enabled = bool(rest_cfg.get("enabled", True))
    host = str(rest_cfg.get("host") or "127.0.0.1").strip()
    port = int(rest_cfg.get("port") or 18080)
    return enabled, host, port


async def register_extensions(registry):
    enabled, host, port = _resolve_rest_config()
    if not enabled:
        return []

    ext = AgentClientRestExtension(host=host, port=port)
    await ext.initialize(registry.config)
    return [ext]
