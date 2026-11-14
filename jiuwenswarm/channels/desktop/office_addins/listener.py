# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""The HTTPS listener that serves the task panes, beside the desktop's HTTP one."""

from __future__ import annotations

import logging
import ssl
import threading
from collections.abc import Callable
from http.server import ThreadingHTTPServer
from typing import Any

from jiuwenswarm.channels.desktop.office_addins.certificate import TlsFiles

logger = logging.getLogger(__name__)

_HANDSHAKE_TIMEOUT_SECONDS = 10.0


class _TlsHTTPServer(ThreadingHTTPServer):
    """Runs each TLS handshake on its request thread.

    Wrapping the listening socket would run handshakes inside ``accept()`` on
    the one serving thread, so a preconnect that never sends its ClientHello
    would stall every later request.
    """

    daemon_threads = True

    def __init__(self, address: tuple[str, int], handler: Callable[..., Any], context: ssl.SSLContext) -> None:
        super().__init__(address, handler)
        self.ssl_context = context

    def finish_request(self, request: Any, client_address: Any) -> None:
        request.settimeout(_HANDSHAKE_TIMEOUT_SECONDS)
        try:
            tls_request = self.ssl_context.wrap_socket(request, server_side=True)
        except OSError as exc:
            logger.debug("[office-addins] TLS handshake with %s failed: %s", client_address, exc)
            return
        try:
            tls_request.settimeout(None)
            super().finish_request(tls_request, client_address)
        finally:
            tls_request.close()


class TlsListener:
    """HTTPS on ``127.0.0.1:<port>``, whose certificate can be replaced while it serves."""

    def __init__(self, port: int, handler: Callable[..., Any]) -> None:
        self.port = port
        self._handler = handler
        self._server: _TlsHTTPServer | None = None
        self._lock = threading.Lock()

    @property
    def origin(self) -> str:
        # Office manifests name localhost; the socket binds only the IPv4 loopback.
        return f"https://localhost:{self.port}"

    @property
    def serving(self) -> bool:
        return self._server is not None

    def serve(self, files: TlsFiles) -> bool:
        """Serve with ``files``; False when the port cannot be bound."""
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(files.cert), str(files.key))
        with self._lock:
            if self._server is not None:
                self._server.ssl_context = context
                return True
            try:
                server = _TlsHTTPServer(("127.0.0.1", self.port), self._handler, context)
            except OSError as exc:
                logger.warning("[office-addins] cannot listen on 127.0.0.1:%s: %s", self.port, exc)
                return False
            self._server = server
        threading.Thread(target=server.serve_forever, name="office-addins-tls", daemon=True).start()
        logger.info("[office-addins] serving %s", self.origin)
        return True

    def close(self) -> None:
        with self._lock:
            server, self._server = self._server, None
        if server is not None:
            server.shutdown()
            server.server_close()
