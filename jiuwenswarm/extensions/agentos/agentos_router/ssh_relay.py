# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Southbound SSH relay into YuanRong agent instances.

Bridges a northbound ``SshRelaySession`` (accepted by the gateway
``SshChannel`` as an interactive shell) to the YuanRong frontend SSH
endpoint::

    ssh -p 2222 'yr:instance:<instance_id>'@<frontend-host>

Client private keys are loaded from ``client_keys_dir`` (default
``/root/.ssh``).

``<instance_id>`` is the instance id returned by the YuanRong agent
create API (``POST /api/agent``), resolved by the AgentOS Router.
"""

from __future__ import annotations

import asyncio
import logging
import re
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_SSH_PORT = 2222
DEFAULT_SSH_USER_TEMPLATE = "yr:instance:{instance}"
DEFAULT_CLIENT_KEYS_DIR = "/root/.ssh"
_RELAY_BUFFER_SIZE = 32768
_USER_ID_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")
# OpenSSH default IdentityFile basenames (skip *.pub / config / known_hosts).
_DEFAULT_CLIENT_KEY_NAMES = (
    "id_ed25519",
    "id_ed25519_sk",
    "id_ecdsa",
    "id_ecdsa_sk",
    "id_rsa",
    "id_dsa",
    "id_ed448",
    "agent_key",
)
# create_sandbox 返回后 frontend bastion / 实例 sshd 可能尚未就绪，对端会
# 立刻掐连接（asyncssh: "SSH connection closed"）。对齐 chat WS 冷启动重试。
_SSH_CONNECT_READY_TIMEOUT_SECONDS = 60.0
_SSH_CONNECT_RETRY_INTERVAL_SECONDS = 1.0
_SSH_CONNECT_RETRYABLE_TEXT_TOKENS = (
    "ssh connection closed",
    "connection closed",
    "connection reset",
    "connection refused",
    "temporarily unavailable",
    "connect call failed",
    "timeout",
)


def _is_ssh_connect_retryable(exc: BaseException) -> bool:
    """冷启动期间 frontend/sshd 未就绪的可重试错误."""
    if isinstance(exc, (ConnectionError, TimeoutError, asyncio.TimeoutError, OSError)):
        return True
    text = str(exc).lower()
    return any(token in text for token in _SSH_CONNECT_RETRYABLE_TEXT_TOKENS)


def _raise_missing_asyncssh(exc: ImportError) -> None:
    raise RuntimeError(
        "SSH relay requires optional dependency `asyncssh>=2.14.0,<2.24`. "
        "Install with `pip install -e \".[ssh]\"` or "
        '`uv sync --extra ssh` / `pip install "jiuwenswarm[ssh]"`.'
    ) from exc


def _import_asyncssh() -> Any:
    try:
        import asyncssh
    except ImportError as exc:
        _raise_missing_asyncssh(exc)
    return asyncssh


def resolve_client_keys_dir(template: str, user_id: str = "") -> Path:
    """Resolve ``client_keys_dir``; substitute ``{user_id}`` when present."""
    safe_user = _USER_ID_SAFE_RE.sub("_", str(user_id or "").strip()) or "default"
    raw = str(template or DEFAULT_CLIENT_KEYS_DIR).strip() or DEFAULT_CLIENT_KEYS_DIR
    try:
        rendered = raw.format(user_id=safe_user)
    except (KeyError, ValueError) as exc:
        raise ValueError(
            f"invalid client_keys_dir template {raw!r}: {exc}"
        ) from exc
    return Path(rendered).expanduser()


def list_client_key_paths(keys_dir: Path) -> list[str]:
    """Return existing private-key paths under *keys_dir* (OpenSSH defaults)."""
    if not keys_dir.is_dir():
        return []
    paths: list[str] = []
    for name in _DEFAULT_CLIENT_KEY_NAMES:
        path = keys_dir / name
        if path.is_file():
            paths.append(str(path))
    return paths


@dataclass(frozen=True)
class YuanrongSshSettings:
    """Southbound SSH access settings for the YuanRong frontend."""

    port: int = DEFAULT_SSH_PORT
    user_template: str = DEFAULT_SSH_USER_TEMPLATE
    connect_timeout_s: float = 30.0
    client_keys_dir: str = DEFAULT_CLIENT_KEYS_DIR


def load_yuanrong_ssh_settings(raw: Any) -> YuanrongSshSettings:
    """Build settings from the ``gateway.agentos.ssh`` config block."""
    if not isinstance(raw, dict):
        raw = {}
    return YuanrongSshSettings(
        port=int(raw.get("port") or DEFAULT_SSH_PORT),
        user_template=str(
            raw.get("user_template") or DEFAULT_SSH_USER_TEMPLATE
        ).strip(),
        connect_timeout_s=float(raw.get("connect_timeout_s") or 30.0),
        client_keys_dir=str(
            raw.get("client_keys_dir") or DEFAULT_CLIENT_KEYS_DIR
        ).strip()
        or DEFAULT_CLIENT_KEYS_DIR,
    )


class YuanrongSshRelay:
    """Relay PTY/exec traffic between a northbound SSH session and YuanRong."""

    def __init__(
        self,
        settings: YuanrongSshSettings,
        *,
        frontend_endpoint: str = "",
    ) -> None:
        self._settings = settings
        self._frontend_endpoint = (frontend_endpoint or "").strip()

    @property
    def backend_host(self) -> str:
        parsed = urllib.parse.urlparse(self._frontend_endpoint)
        return parsed.hostname or ""

    @property
    def backend_port(self) -> int:
        return self._settings.port

    @property
    def client_keys_dir(self) -> str:
        return self._settings.client_keys_dir

    def backend_username(self, instance_id: str) -> str:
        instance = str(instance_id or "").strip()
        if not instance:
            raise ValueError("instance_id is required for YuanRong SSH relay")
        return self._settings.user_template.format(instance=instance)

    async def run(
        self,
        session: Any,
        instance_id: str,
        *,
        user_id: str = "",
    ) -> int:
        """Relay *session* to the YuanRong instance; returns the exit code.

        Always resolves ``session.done`` and ``session.exit_code`` so the
        northbound channel waiting in ``_wait_relay_done`` is released.
        Cancellation (northbound timeout / router disconnect) also releases
        the waiter and tears down the southbound connection.
        """
        exit_code = 1
        try:
            exit_code = await self._relay(session, instance_id, user_id=user_id)
        except asyncio.CancelledError:
            logger.info(
                "[YuanrongSshRelay] relay cancelled: session=%s instance=%s",
                session.session_id,
                instance_id,
            )
            exit_code = 130
            raise
        except Exception as exc:  # noqa: BLE001 - report any relay failure to the client
            logger.exception(
                "[YuanrongSshRelay] relay failed: session=%s instance=%s",
                session.session_id,
                instance_id,
            )
            self._write_client_error(session, f"yuanrong ssh relay failed: {exc}")
        finally:
            session.exit_code = exit_code
            session.done.set()
        return exit_code

    def fail_session(self, session: Any, reason: str) -> None:
        """Mark *session* failed and release the northbound waiter."""
        self._write_client_error(session, reason)
        session.exit_code = 1
        session.done.set()

    @staticmethod
    def _write_client_error(session: Any, reason: str) -> None:
        try:
            session.process.stdout.write(f"[ssh-relay] {reason}\r\n".encode())
        except Exception:  # noqa: BLE001 - client may already be gone
            logger.debug("[YuanrongSshRelay] client write failed", exc_info=True)

    def _resolve_client_keys(self, user_id: str) -> list[str]:
        keys_dir = resolve_client_keys_dir(
            self._settings.client_keys_dir, user_id
        )
        key_paths = list_client_key_paths(keys_dir)
        if not key_paths:
            raise ValueError(
                f"no SSH private keys found in {keys_dir} "
                f"(expected one of: {', '.join(_DEFAULT_CLIENT_KEY_NAMES)})"
            )
        return key_paths

    async def wait_until_ready(
        self,
        instance_id: str,
        *,
        user_id: str = "",
    ) -> None:
        """Probe southbound SSH until sshd accepts, then close the probe.

        Used by ``3rdagent.switch`` after create so the client does not SSH
        before the YuanRong instance is reachable.
        """
        conn = await self._connect_until_ready(instance_id, user_id=user_id)
        conn.close()
        try:
            await conn.wait_closed()
        except Exception:  # noqa: BLE001
            logger.debug("[YuanrongSshRelay] probe close failed", exc_info=True)

    async def _connect_until_ready(
        self,
        instance_id: str,
        *,
        user_id: str = "",
        session_id: str = "",
    ) -> Any:
        asyncssh = _import_asyncssh()

        host = self.backend_host
        if not host:
            raise ValueError(
                "yuanrong ssh host is empty "
                "(set gateway.agent_client.frontend_endpoint with a hostname)"
            )
        username = self.backend_username(instance_id)
        client_keys = self._resolve_client_keys(user_id)
        keys_dir = resolve_client_keys_dir(self._settings.client_keys_dir, user_id)
        deadline = (
            asyncio.get_running_loop().time() + _SSH_CONNECT_READY_TIMEOUT_SECONDS
        )
        attempt = 0
        while True:
            attempt += 1
            logger.info(
                "[YuanrongSshRelay] connecting: %s@%s:%s session=%s "
                "attempt=%s keys_dir=%s keys=%s",
                username,
                host,
                self._settings.port,
                session_id or "-",
                attempt,
                keys_dir,
                len(client_keys),
            )
            try:
                conn = await asyncio.wait_for(
                    asyncssh.connect(
                        host,
                        port=self._settings.port,
                        username=username,
                        client_keys=client_keys,
                        agent_path=None,
                        known_hosts=None,
                    ),
                    timeout=self._settings.connect_timeout_s,
                )
            except Exception as exc:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0 or not _is_ssh_connect_retryable(exc):
                    raise
                sleep_for = min(_SSH_CONNECT_RETRY_INTERVAL_SECONDS, remaining)
                logger.warning(
                    "[YuanrongSshRelay] ssh not ready, retrying: "
                    "session=%s instance=%s attempt=%s sleep=%.1fs error=%s",
                    session_id or "-",
                    instance_id,
                    attempt,
                    sleep_for,
                    exc,
                )
                await asyncio.sleep(sleep_for)
                continue
            if attempt > 1:
                logger.info(
                    "[YuanrongSshRelay] ssh ready after retry: "
                    "session=%s instance=%s attempts=%s",
                    session_id or "-",
                    instance_id,
                    attempt,
                )
            return conn

    async def _relay(
        self,
        session: Any,
        instance_id: str,
        *,
        user_id: str = "",
    ) -> int:
        conn = await self._connect_until_ready(
            instance_id,
            user_id=user_id,
            session_id=str(getattr(session, "session_id", "") or ""),
        )
        try:
            return await self._relay_over_connection(session, conn)
        finally:
            conn.close()
            try:
                await conn.wait_closed()
            except Exception:  # noqa: BLE001
                logger.debug("[YuanrongSshRelay] close failed", exc_info=True)

    async def _relay_over_connection(self, session: Any, conn: Any) -> int:
        process = session.process

        # Interactive shell, or forward northbound exec command into the sandbox.
        kwargs: dict[str, Any] = {"encoding": None}
        command = str(getattr(session, "command", None) or "").strip() or None
        if command:
            kwargs["command"] = command
        term_type = process.get_terminal_type() or "xterm"
        kwargs["term_type"] = term_type
        term_size = process.get_terminal_size()
        if term_size and term_size[0]:
            kwargs["term_size"] = term_size
        backend = await conn.create_process(**kwargs)

        # Either side dying must tear down the other: wait FIRST_COMPLETED,
        # then cancel remaining pumps and close both ends.
        pumps = [
            asyncio.create_task(
                self._pump_client_to_backend(session, backend),
                name=f"ssh-pump-n2s-{session.session_id[:16]}",
            ),
            asyncio.create_task(
                self._pump_backend_to_client(backend.stdout, process.stdout),
                name=f"ssh-pump-s2n-out-{session.session_id[:16]}",
            ),
            asyncio.create_task(
                self._pump_backend_to_client(backend.stderr, process.stderr),
                name=f"ssh-pump-s2n-err-{session.session_id[:16]}",
            ),
        ]
        try:
            done, pending = await asyncio.wait(
                pumps, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                exc = task.exception() if not task.cancelled() else None
                if exc is not None:
                    logger.debug(
                        "[YuanrongSshRelay] pump ended with error: %s",
                        exc,
                        exc_info=exc,
                    )
        except asyncio.CancelledError:
            for task in pumps:
                task.cancel()
            await asyncio.gather(*pumps, return_exceptions=True)
            raise
        finally:
            await self._close_backend(backend)
            self._exit_northbound(process, backend.exit_status)

        exit_status = backend.exit_status
        return int(exit_status) if exit_status is not None else 0

    @staticmethod
    async def _close_backend(backend: Any) -> None:
        try:
            backend.close()
        except Exception:  # noqa: BLE001
            logger.debug("[YuanrongSshRelay] backend.close failed", exc_info=True)
        try:
            await backend.wait_closed()
        except Exception:  # noqa: BLE001
            logger.debug(
                "[YuanrongSshRelay] backend.wait_closed failed", exc_info=True
            )

    @staticmethod
    def _exit_northbound(process: Any, exit_status: Any) -> None:
        """Force-close the northbound SSH process when the southbound ends."""
        if process is None:
            return
        code = int(exit_status) if exit_status is not None else 0
        try:
            process.exit(code)
        except Exception:  # noqa: BLE001 - channel may already be closed
            logger.debug(
                "[YuanrongSshRelay] northbound process.exit failed",
                exc_info=True,
            )

    @staticmethod
    async def _pump_client_to_backend(session: Any, backend: Any) -> None:
        asyncssh = _import_asyncssh()

        process = session.process
        while True:
            try:
                data = await process.stdin.read(_RELAY_BUFFER_SIZE)
            except asyncssh.TerminalSizeChanged as exc:
                try:
                    backend.change_terminal_size(
                        exc.width, exc.height, exc.pixwidth, exc.pixheight
                    )
                except Exception:  # noqa: BLE001 - exec channels have no PTY
                    logger.debug(
                        "[YuanrongSshRelay] change_terminal_size failed",
                        exc_info=True,
                    )
                continue
            except asyncssh.BreakReceived:
                try:
                    backend.stdin.write(b"\x03")
                except (asyncssh.ConnectionLost, ConnectionError):
                    break
                continue
            except (asyncssh.ConnectionLost, ConnectionError):
                break
            if not data:
                try:
                    backend.stdin.write_eof()
                except Exception:  # noqa: BLE001 - backend may already be closed
                    logger.debug(
                        "[YuanrongSshRelay] write_eof failed", exc_info=True
                    )
                break
            try:
                backend.stdin.write(data)
                await backend.stdin.drain()
            except (asyncssh.ConnectionLost, ConnectionError):
                break

    @staticmethod
    async def _pump_backend_to_client(reader: Any, writer: Any) -> None:
        asyncssh = _import_asyncssh()

        while True:
            try:
                data = await reader.read(_RELAY_BUFFER_SIZE)
            except (asyncssh.ConnectionLost, ConnectionError):
                break
            if not data:
                break
            try:
                writer.write(data)
                await writer.drain()
            except (asyncssh.ConnectionLost, ConnectionError):
                break
