# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any, Mapping

from jiuwenswarm.common.e2a.models import E2AEnvelope
from jiuwenswarm.common.schema.agent import AgentResponse, AgentResponseChunk
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.extensions.agentos.agentos_router.agent_manager import (
    BUILTIN_AGENT_TYPE,
    AgentCreatingTimeout,
    AgentDeleted,
    AgentManager,
    AgentRuntime,
    is_third_party_agent_type,
)
from jiuwenswarm.extensions.agentos.agentos_router.config import (
    DEFAULT_AGENT_WORKSPACE_ROOT,
    SshChannelEndpoint,
)
from jiuwenswarm.extensions.agentos.agentos_router.models import (
    AgentInfo,
    AgentStatus,
    ImageInfo,
)
from jiuwenswarm.extensions.agentos.agentos_router.registry_client import RegistryClient
from jiuwenswarm.extensions.agentos.agentos_router.ssh_relay import YuanrongSshRelay
from jiuwenswarm.extensions.yuanrong_frontend_client import (
    AgentRuntimeSpec,
    YuanrongFrontendAgentClient,
)
from jiuwenswarm.gateway.routing.agent_client import AgentServerClient


logger = logging.getLogger(__name__)

_WORKSPACE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


class UnsupportedAgentType(ValueError):
    pass


def build_inline_runtime_spec(image_info: ImageInfo) -> AgentRuntimeSpec:
    """Take registry ``metadata.runtime_spec`` (YuanRong ``RuntimeSpec`` shape)."""
    meta = image_info.metadata if isinstance(image_info.metadata, dict) else {}
    raw_spec = meta.get("runtime_spec")
    if not isinstance(raw_spec, Mapping) or not raw_spec:
        raise ValueError(
            f"runtime_spec is required from registry for agent_type={image_info.image_name}"
        )
    return dict(raw_spec)  # type: ignore[return-value]


def resolve_agent_workspace(user_id: str, *, workspace_root: str | None = None) -> str:
    """Resolve host workspace bind path for one agent user.

    Default: ``/home/agentos/users/<user_id>``. Optional ``workspace_root``
    overrides the parent directory (``{workspace_root}/<user_id>``).

    Best-effort ``mkdir``: permission errors are ignored so callers (and unit
    tests) can still pass the path to YuanRong create; the host/deploy side
    remains responsible for a writable mount source.
    """
    safe_user = _WORKSPACE_NAME_RE.sub("_", str(user_id or "").strip()) or "default"
    root = Path(workspace_root or DEFAULT_AGENT_WORKSPACE_ROOT).expanduser()
    workspace = (root / safe_user).resolve()
    try:
        workspace.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning(
            "[AgentOSRouter] workspace mkdir skipped: path=%s error=%s",
            workspace,
            exc,
        )
    return str(workspace)


class AgentOSRouterClient(AgentServerClient):
    """AgentServerClient implementation backed by YuanRong and AgentManager."""

    def __init__(
        self,
        yuanrong: YuanrongFrontendAgentClient,
        registry: RegistryClient,
        agent_manager: AgentManager,
        ssh_relay: YuanrongSshRelay | None = None,
        ssh_channel_endpoint: SshChannelEndpoint | None = None,
        workspace_root: str = DEFAULT_AGENT_WORKSPACE_ROOT,
        sandbox_idle_timeout_seconds: float = 600.0,
        sandbox_idle_check_interval_seconds: float = 30.0,
    ) -> None:
        self._yuanrong = yuanrong
        self._registry = registry
        self._agent_manager = agent_manager
        self._ssh_relay = ssh_relay
        self._ssh_channel_endpoint = ssh_channel_endpoint
        self._workspace_root = (
            str(workspace_root or "").strip() or DEFAULT_AGENT_WORKSPACE_ROOT
        )
        # <= 0 disables idle sandbox reclamation entirely.
        self._sandbox_idle_timeout_seconds = float(sandbox_idle_timeout_seconds)
        self._sandbox_idle_check_interval_seconds = max(
            1.0, float(sandbox_idle_check_interval_seconds)
        )
        self._idle_reaper_task: asyncio.Task[None] | None = None
        self._server_ready = False
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._closed = False
        # 用户当前 agent_type（3rdagent.switch 成功后更新）；SSH 接入跟随此值。
        self._current_agent_types: dict[str, str] = {}

    def get_current_agent_type(self, user_id: str) -> str:
        """Return the user's current agent_type (default ``jiuwenswarm``)."""
        uid = str(user_id or "").strip()
        return self._current_agent_types.get(uid) or BUILTIN_AGENT_TYPE

    @staticmethod
    def _uses_direct_yuanrong(agent_type: str) -> bool:
        """Builtin swarm uses URN invoke (same as ``agent_client.type=yuanrong``)."""
        return str(agent_type or "").strip().lower() == BUILTIN_AGENT_TYPE

    @property
    def server_ready(self) -> bool:
        return self._server_ready and self._yuanrong.server_ready

    async def connect(self, uri: str) -> None:
        await self._yuanrong.connect(uri)
        self._closed = False
        self._server_ready = True
        self._ensure_idle_reaper_task()

    async def disconnect(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._server_ready = False
        await self._stop_idle_reaper_task()
        await self._drain_background_tasks()
        try:
            await self._yuanrong.disconnect()
        finally:
            await self._registry.close()

    def set_or_update_server_config(
        self,
        *,
        config: dict[str, Any],
        env: dict[str, str] | None = None,
    ) -> None:
        self._yuanrong.set_or_update_server_config(config=config, env=env)

    def set_server_push_handler(
        self,
        handler: Callable[[dict[str, Any]], Awaitable[None]] | None,
    ) -> None:
        setter = getattr(self._yuanrong, "set_server_push_handler", None)
        if callable(setter):
            setter(handler)

    async def send_request(
        self,
        envelope: E2AEnvelope,
        *,
        timeout: float | None = None,
    ) -> AgentResponse:
        # 3rdagent.list / 3rdagent.switch are handled by Gateway ThirdAgent
        # (TUI local_handler), not via E2A send_request.
        if self._is_ssh_relay_request(envelope):
            return await self._handle_ssh_relay(envelope)
        try:
            agent_type = self._extract_agent_type(envelope)
            if self._uses_direct_yuanrong(agent_type):
                envelope.channel_context["agent_type"] = agent_type
                return await self._yuanrong.send_request(envelope)
            runtime = await self._resolve_agent(envelope, acquire=True)
        except (ValueError, AgentCreatingTimeout) as exc:
            return self._routing_error_response(envelope, str(exc))
        try:
            runtime.attach_to_envelope(envelope)
            # create 后通过 YuanRong frontend WS 代理直连 instance，不走 invoke。
            try:
                ws_client = await self._get_ws_client(runtime)
            except ValueError as exc:
                return self._routing_error_response(envelope, str(exc))
            return await ws_client.send_request(envelope, timeout=timeout)
        finally:
            await self._agent_manager.release(runtime.key)

    async def send_request_stream(
        self, envelope: E2AEnvelope
    ) -> AsyncIterator[AgentResponseChunk]:
        try:
            agent_type = self._extract_agent_type(envelope)
            if self._uses_direct_yuanrong(agent_type):
                envelope.channel_context["agent_type"] = agent_type
                async for chunk in self._yuanrong.send_request_stream(envelope):
                    yield chunk
                return
            runtime = await self._resolve_agent(envelope, acquire=True)
        except (ValueError, AgentCreatingTimeout) as exc:
            yield self._routing_error_chunk(envelope, str(exc))
            return
        try:
            runtime.attach_to_envelope(envelope)
            async for chunk in self._yuanrong.send_request_stream(envelope):
                yield chunk
        finally:
            await self._agent_manager.release(runtime.key)

    async def thirdagent_list(
        self,
        *,
        user_id: str,
        current_agent_type: str = "",
    ) -> dict[str, Any]:
        """Handle ``3rdagent.list``: list switchable third-party agent images."""
        uid = str(user_id or "").strip()
        if not uid:
            return {
                "ok": False,
                "error": "user_id is required for AgentOS routing",
                "code": "BAD_REQUEST",
            }
        images = await self._registry.list_user_images(uid)
        agents: list[dict[str, Any]] = []
        for image in images:
            agent_type = str(
                (image.metadata or {}).get("agent_type") or image.image_name or ""
            ).strip()
            if not agent_type:
                continue
            agents.append(
                {
                    "agent_type": agent_type,
                    "image_name": image.image_name,
                    "image_uri": image.image_uri,
                    "metadata": dict(image.metadata or {}),
                }
            )
        current = (
            str(current_agent_type or "").strip()
            or self.get_current_agent_type(uid)
        )
        return {
            "ok": True,
            "payload": {
                "agents": agents,
                "current_agent_type": current,
            },
        }

    def _ssh_endpoint_fields(self) -> dict[str, Any] | None:
        """Northbound ``channels.ssh`` listen ip/port, or None if unavailable."""
        endpoint = self._ssh_channel_endpoint
        if endpoint is None:
            return None
        ip = str(endpoint.ip or "").strip()
        port = int(endpoint.port or 0)
        if not ip or port <= 0:
            return None
        return {"ssh_ip": ip, "ssh_port": port}

    @staticmethod
    def _missing_ssh_endpoint_error() -> dict[str, Any]:
        return {
            "ok": False,
            "error": (
                "ssh channel endpoint is unavailable: enable channels.ssh "
                "and set advertise_host (or listen_host) / listen_port"
            ),
            "code": "SSH_ENDPOINT_UNAVAILABLE",
        }

    async def thirdagent_switch(
        self,
        *,
        user_id: str,
        agent_type: str,
        session_id: str = "",
    ) -> dict[str, Any]:
        """Handle ``3rdagent.switch``: ensure agent exists without forwarding chat.

        Success payload includes northbound SSH channel ``ssh_ip``/``ssh_port``
        (``channels.ssh.advertise_host`` falling back to ``listen_host`` / ``listen_port``).
        Missing values fail.
        """
        uid = str(user_id or "").strip()
        if not uid:
            return {
                "ok": False,
                "error": "user_id is required for AgentOS routing",
                "code": "BAD_REQUEST",
            }
        try:
            normalized = AgentRuntime.normalize_agent_type(agent_type)
        except ValueError as exc:
            return {
                "ok": False,
                "error": str(exc),
                "code": "UNSUPPORTED_AGENT_TYPE",
            }
        # Fail fast before create when northbound SSH channel is not configured.
        ssh_fields = self._ssh_endpoint_fields()
        if ssh_fields is None:
            return self._missing_ssh_endpoint_error()
        # Builtin swarm: no registry / create_sandbox; mark current type only.
        if self._uses_direct_yuanrong(normalized):
            self._current_agent_types[uid] = normalized
            return {
                "ok": True,
                "payload": {
                    "agent_id": "",
                    "agent_type": normalized,
                    "sandbox_id": "",
                    "status": AgentStatus.READY.value,
                    **ssh_fields,
                },
            }
        try:
            runtime = await self._agent_manager.get_or_create_agent(
                uid,
                normalized,
                key_values={"session_id": session_id} if session_id else None,
                creator=self._create_agent,
                metadata={"session_id": session_id} if session_id else None,
            )
        except (ValueError, AgentCreatingTimeout) as exc:
            return {
                "ok": False,
                "error": str(exc),
                "code": "INTERNAL_ERROR",
            }
        info = runtime.info
        status = info.status.value if hasattr(info.status, "value") else str(info.status)
        # 记录用户当前 agent_type，后续 SSH 接入默认跟随
        self._current_agent_types[uid] = normalized
        return {
            "ok": True,
            "payload": {
                "agent_id": info.agent_id,
                "agent_type": info.agent_type,
                "sandbox_id": info.sandbox_id,
                "status": status,
                **ssh_fields,
            },
        }

    async def shutdown(self) -> None:
        await self.disconnect()

    # ---------- SSH relay (northbound SshChannel -> YuanRong instance) ----------

    @staticmethod
    def _is_ssh_relay_request(envelope: E2AEnvelope) -> bool:
        return str(envelope.method or "") == ReqMethod.SSH_RELAY.value

    async def _handle_ssh_relay(self, envelope: E2AEnvelope) -> AgentResponse:
        """Start the southbound SSH relay for an ``ssh.relay`` request.

        Agent resolution (YuanRong instance creation) and the PTY relay run
        in a background task so the gateway forward loop is not blocked for
        the whole SSH session; the northbound channel waits on the relay
        session ``done`` event instead of this response.
        """
        session_id = str(envelope.session_id or "")
        params = envelope.params if isinstance(envelope.params, dict) else {}
        # Live SshRelaySession handed over in-process by the northbound
        # SshChannel; pop it so it never leaks into serialization/logging.
        relay_session = params.pop("relay_session", None)
        if relay_session is None:
            return self._routing_error_response(
                envelope, f"ssh relay session not found in params: {session_id}"
            )
        if self._ssh_relay is None:
            msg = "ssh relay is not configured for AgentOS router"
            relay_session.exit_code = 1
            relay_session.done.set()
            return self._routing_error_response(envelope, msg)

        task = asyncio.create_task(
            self._run_ssh_relay(envelope, relay_session),
            name=f"agentos-ssh-relay-{session_id[:24]}",
        )
        relay_session.relay_task = task
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return AgentResponse(
            request_id=str(envelope.request_id or ""),
            channel_id=str(envelope.channel or ""),
            ok=True,
            payload={"method": ReqMethod.SSH_RELAY.value, "status": "relay_started"},
        )

    async def _run_ssh_relay(self, envelope: E2AEnvelope, relay_session: Any) -> None:
        ssh_relay = self._ssh_relay
        if ssh_relay is None:
            # _handle_ssh_relay already guards this; keep a safe fallback.
            relay_session.exit_code = 1
            relay_session.done.set()
            return
        self._apply_current_agent_type_for_ssh(envelope)
        try:
            agent_type = self._extract_agent_type(envelope)
            if self._uses_direct_yuanrong(agent_type):
                ssh_relay.fail_session(
                    relay_session,
                    "builtin agent_type has no AgentOS sandbox for SSH; run "
                    "3rdagent.switch first or provide a remote command so "
                    "agent_type can be derived from its first token",
                )
                return
            runtime = await self._resolve_agent(envelope, acquire=True)
        except (ValueError, AgentCreatingTimeout, AgentDeleted) as exc:
            ssh_relay.fail_session(
                relay_session, f"agent resolve failed: {exc}"
            )
            return
        except Exception as exc:  # noqa: BLE001 - creation errors must release the client
            logger.exception(
                "[AgentOSRouter] ssh relay agent creation failed: session=%s",
                relay_session.session_id,
            )
            ssh_relay.fail_session(
                relay_session, f"agent creation failed: {exc}"
            )
            return

        # Hold the task count for the whole SSH session so the idle reaper
        # never reclaims a sandbox with a live (even silent) SSH connection.
        try:
            instance_id = str(runtime.info.sandbox_id or "").strip()
            if not instance_id:
                ssh_relay.fail_session(
                    relay_session,
                    f"agent has no yuanrong instance_id: user={runtime.info.user_id}",
                )
                return

            runtime.attach_to_envelope(envelope)
            logger.info(
                "[AgentOSRouter] ssh relay start: session=%s user=%s instance=%s",
                relay_session.session_id,
                runtime.info.user_id,
                instance_id,
            )
            await ssh_relay.run(
                relay_session,
                instance_id,
                user_id=runtime.info.user_id,
            )
        finally:
            await self._agent_manager.release(runtime.key)

    def _apply_current_agent_type_for_ssh(self, envelope: E2AEnvelope) -> None:
        """SSH 接入跟随用户当前 agent_type（由 3rdagent.switch 记录）。

        未 switch / 仍为内置 ``jiuwenswarm`` 时，取 SSH 远程指令首词作为
        agent_type。
        """
        params = envelope.params if isinstance(envelope.params, dict) else {}
        if str(params.get("agent_type") or "").strip():
            return
        user_id = str(envelope.user_id or "").strip()
        current = self.get_current_agent_type(user_id)
        if self._uses_direct_yuanrong(current):
            command = str(params.get("command") or "").strip()
            if not command:
                ctx = envelope.channel_context
                if isinstance(ctx, dict):
                    command = str(ctx.get("command") or "").strip()
            token = command.split(maxsplit=1)[0].lower() if command else ""
            current = token or BUILTIN_AGENT_TYPE
        params = dict(params)
        params["agent_type"] = current
        envelope.params = params
        logger.info(
            "[AgentOSRouter] ssh relay follows user current agent_type: "
            "user=%s agent_type=%s",
            user_id,
            current,
        )

    async def _drain_background_tasks(self) -> None:
        if not self._background_tasks:
            return
        tasks = list(self._background_tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._background_tasks.clear()

    async def _resolve_agent(
        self,
        envelope: E2AEnvelope,
        *,
        acquire: bool = False,
    ) -> AgentRuntime:
        user_id = self._extract_user_id(envelope)
        agent_type = self._extract_agent_type(envelope)
        return await self._agent_manager.get_or_create_agent(
            user_id,
            agent_type,
            key_values={"session_id": envelope.session_id},
            creator=self._create_agent,
            metadata={"session_id": envelope.session_id},
            acquire=acquire,
        )

    # ---------- idle sandbox reclamation ----------

    def _idle_reaper_enabled(self) -> bool:
        return self._sandbox_idle_timeout_seconds > 0

    def _ensure_idle_reaper_task(self) -> None:
        if self._closed or not self._idle_reaper_enabled():
            return
        if self._idle_reaper_task is not None and not self._idle_reaper_task.done():
            return
        self._idle_reaper_task = asyncio.create_task(
            self._idle_reaper_loop(),
            name="agentos-sandbox-idle-reaper",
        )

    async def _stop_idle_reaper_task(self) -> None:
        task = self._idle_reaper_task
        self._idle_reaper_task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _idle_reaper_loop(self) -> None:
        while not self._closed:
            await asyncio.sleep(self._sandbox_idle_check_interval_seconds)
            try:
                await self._reap_idle_once()
            except Exception:  # noqa: BLE001 - one bad pass must not kill the loop
                logger.exception("[AgentOSRouter] idle sandbox reap pass failed")

    async def _reap_idle_once(self) -> int:
        """Reclaim agents idle beyond the timeout; returns the reclaimed count.

        ``pop_if_idle`` re-checks READY / ``task_count == 0`` / staleness under
        the manager lock, so a concurrent acquire can never lose its sandbox.
        """
        if not self._idle_reaper_enabled():
            return 0
        reaped = 0
        for key in await self._agent_manager.list_keys():
            runtime = await self._agent_manager.pop_if_idle(
                key, self._sandbox_idle_timeout_seconds
            )
            if runtime is None:
                continue
            reaped += 1
            info = runtime.info
            logger.info(
                "[AgentOSRouter] reclaiming idle agent sandbox: user=%s "
                "agent_type=%s sandbox_id=%s idle_timeout=%.0fs",
                info.user_id,
                info.agent_type,
                info.sandbox_id,
                self._sandbox_idle_timeout_seconds,
            )
            if not info.sandbox_id:
                continue
            try:
                await self._yuanrong.delete_sandbox(info.sandbox_id)
            except Exception:  # noqa: BLE001 - keep reaping other agents
                logger.exception(
                    "[AgentOSRouter] delete idle sandbox failed: sandbox_id=%s",
                    info.sandbox_id,
                )
        return reaped

    async def _create_agent(self, agent_info: AgentInfo) -> AgentInfo:
        if not is_third_party_agent_type(agent_info.agent_type):
            raise UnsupportedAgentType(
                f"sandbox create is only supported for third-party "
                f"agent_type, got: {agent_info.agent_type}"
            )

        image_info = await self._registry.get_image_info(agent_info.agent_type)
        runtime_spec = build_inline_runtime_spec(image_info)
        workspace = resolve_agent_workspace(
            agent_info.user_id,
            workspace_root=self._workspace_root,
        )
        env_raw = image_info.metadata.get("env_vars")
        env_vars = (
            {str(k): str(v) for k, v in dict(env_raw).items()}
            if isinstance(env_raw, dict) and env_raw
            else None
        )
        sandbox = await self._yuanrong.create_sandbox(
            namespace=self._yuanrong.agent_namespace,
            name=f"{agent_info.user_id}+{agent_info.agent_type}",
            workspace=workspace,
            runtime_spec=runtime_spec,
            env_vars=env_vars,
        )
        instance_id = sandbox.sandbox_id
        agent_info.sandbox_id = instance_id
        agent_info.metadata.update(
            {
                "instance_id": instance_id,
                "workspace": workspace,
                "runtime_spec": dict(runtime_spec),
                "image_info": dict(image_info.metadata),
                "sandbox": dict(sandbox.metadata),
            }
        )
        agent_info.status = AgentStatus.READY

        task = asyncio.create_task(
            self._register_agent(agent_info.copy()),
            name=f"agentos-register-{agent_info.agent_id[:12]}",
        )
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return agent_info

    async def delete_agent(
        self,
        user_id: str,
        agent_type: str,
        *,
        key_values: dict[str, Any] | None = None,
    ) -> None:
        """Delete agent mapping and release its YuanRong sandbox."""
        resolved_key_values = dict(key_values or {})
        runtime = await self._agent_manager.get_agent(
            user_id, agent_type, key_values=resolved_key_values or None
        )
        if runtime is None:
            return
        agent_info = runtime.info
        if (
            "session_id" not in resolved_key_values
            and agent_info.metadata.get("session_id")
        ):
            resolved_key_values["session_id"] = agent_info.metadata.get(
                "session_id"
            )
        if agent_info.sandbox_id:
            await self._yuanrong.delete_sandbox(agent_info.sandbox_id)
        await self._agent_manager.delete_agent(
            agent_info.user_id,
            agent_info.agent_type,
            key_values=resolved_key_values or None,
        )

    async def _register_agent(self, agent_info: AgentInfo) -> None:
        try:
            await self._registry.register_agent(agent_info)
        except Exception:
            logger.exception(
                "[AgentOSRouter] async registry registration failed: agent_id=%s",
                agent_info.agent_id,
            )

    @staticmethod
    def _extract_user_id(envelope: E2AEnvelope) -> str:
        user_id = str(envelope.user_id or "").strip()
        if not user_id:
            raise ValueError("user_id is required for AgentOS routing")
        return user_id

    @staticmethod
    def _extract_agent_type(envelope: E2AEnvelope) -> str:
        raw = envelope.params.get("agent_type")
        if raw is None:
            raw = envelope.channel_context.get("agent_type")
        try:
            return AgentRuntime.normalize_agent_type(raw)
        except ValueError as exc:
            raise UnsupportedAgentType(str(exc)) from exc

    @staticmethod
    def _routing_error_response(
        envelope: E2AEnvelope,
        message: str,
    ) -> AgentResponse:
        return AgentResponse(
            request_id=str(envelope.request_id or ""),
            channel_id=str(envelope.channel or ""),
            ok=False,
            payload={"error": message},
        )

    @staticmethod
    def _routing_error_chunk(
        envelope: E2AEnvelope,
        message: str,
    ) -> AgentResponseChunk:
        return AgentResponseChunk(
            request_id=str(envelope.request_id or ""),
            channel_id=str(envelope.channel or ""),
            payload={"error": message},
            is_complete=True,
        )
