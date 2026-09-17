from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from jiuwenswarm.common.e2a.models import E2AEnvelope
from jiuwenswarm.common.schema.agent import AgentResponse, AgentResponseChunk
from jiuwenswarm.extensions.agentos.agentos_router.agent_manager import (
    AgentManager,
    AgentRuntime,
)
from jiuwenswarm.extensions.agentos.agentos_router.config import (
    DEFAULT_AGENT_WORKSPACE_ROOT,
    SshChannelEndpoint,
    agentos_router_selected,
    load_router_config,
)
from jiuwenswarm.extensions.agentos.agentos_router.extension import AgentOSRouter
from jiuwenswarm.extensions.agentos.agentos_router.models import (
    AgentInfo,
    AgentStatus,
    ImageInfo,
)
from jiuwenswarm.extensions.agentos.agentos_router.router_client import (
    AgentOSRouterClient,
    resolve_agent_workspace,
    _is_ws_connect_retryable,
)
from jiuwenswarm.extensions.yuanrong_frontend_client import SandboxInfo


def _ssh_channel(
    *,
    ip: str = "0.0.0.0",
    port: int = 2222,
) -> SshChannelEndpoint:
    return SshChannelEndpoint(ip=ip, port=port)


class FakeYuanRongClient:
    def __init__(self) -> None:
        self.server_ready = True
        self.function_version_urn = "urn:test:function:1"
        self.agent_namespace = "default"
        self.frontend_endpoint = "http://yuanrong.test:8888"
        self.send_calls = 0
        self.create_calls = 0
        self.create_payloads: list[dict[str, Any]] = []
        self.delete_calls: list[str] = []
        self.config: dict[str, Any] = {}
        self.push_handler = None
        self.ws_connect_uris: list[str] = []

    async def connect(self, uri: str) -> None:
        del uri
        return None

    async def disconnect(self) -> None:
        return None

    async def create_sandbox(
        self,
        *,
        namespace: str,
        name: str,
        workspace: str,
        runtime_spec: dict[str, Any],
        env_vars: dict[str, str] | None = None,
        mounts: list[dict[str, Any]] | None = None,
    ) -> SandboxInfo:
        self.create_calls += 1
        payload = {
            "namespace": namespace,
            "name": name,
            "workspace": workspace,
            "runtime_spec": dict(runtime_spec),
            "env_vars": dict(env_vars or {}),
            "mounts": list(mounts or []),
        }
        self.create_payloads.append(payload)
        return SandboxInfo(
            sandbox_id=f"sbx-{self.create_calls}",
            metadata=payload,
        )

    async def delete_sandbox(self, sandbox_id: str) -> None:
        self.delete_calls.append(sandbox_id)

    async def get_agent_info(self, instance_id: str) -> dict:
        return {"instance_id": instance_id, "node_ip": "127.0.0.1", "sandbox_ip": "127.0.0.1"}

    async def send_request(self, envelope: E2AEnvelope) -> AgentResponse:
        self.send_calls += 1
        return AgentResponse(
            request_id=str(envelope.request_id or ""),
            channel_id=str(envelope.channel or ""),
        )

    async def send_request_stream(
        self, envelope: E2AEnvelope
    ) -> AsyncIterator[AgentResponseChunk]:
        self.send_calls += 1
        yield AgentResponseChunk(
            request_id=str(envelope.request_id or ""),
            channel_id=str(envelope.channel or ""),
            is_complete=True,
        )

    def set_or_update_server_config(
        self,
        *,
        config: dict[str, Any],
        env: dict[str, str] | None = None,
    ) -> None:
        del env
        self.config = config

    def set_server_push_handler(self, handler) -> None:
        self.push_handler = handler


class FakeAgentWsClient:
    """create 后 WS 直连 instance 的假客户端：send 委托给 FakeYuanRongClient 计数。"""

    def __init__(self, yuanrong: FakeYuanRongClient) -> None:
        self._yuanrong = yuanrong
        self.connected_uris: list[str] = []
        self.disconnected = False
        self.push_handler = None

    def set_server_push_handler(self, handler) -> None:
        self.push_handler = handler

    def set_or_update_server_config(self, *, config, env=None) -> None:
        del config, env

    async def connect(self, uri: str) -> None:
        self.connected_uris.append(uri)
        self._yuanrong.ws_connect_uris.append(uri)

    async def disconnect(self) -> None:
        self.disconnected = True

    async def send_request(self, envelope: E2AEnvelope) -> AgentResponse:
        return await self._yuanrong.send_request(envelope)

    def send_request_stream(
        self, envelope: E2AEnvelope
    ) -> AsyncIterator[AgentResponseChunk]:
        return self._yuanrong.send_request_stream(envelope)


class _Http502(Exception):
    status_code = 502


class FlakyAgentWsClient(FakeAgentWsClient):
    """前 N 次 connect 回 502，模拟 agentserver 冷启动未就绪."""

    fail_times = 2
    instances: list["FlakyAgentWsClient"] = []

    def __init__(self, yuanrong: FakeYuanRongClient) -> None:
        super().__init__(yuanrong)
        self.connect_attempts = 0
        type(self).instances.append(self)

    async def connect(self, uri: str) -> None:
        self.connect_attempts += 1
        total = sum(c.connect_attempts for c in type(self).instances)
        if total <= self.fail_times:
            raise _Http502("server rejected WebSocket connection: HTTP 502")
        await super().connect(uri)


def _router_client(
    yuanrong: FakeYuanRongClient,
    registry: FakeRegistryClient | None = None,
    agent_manager: AgentManager | None = None,
    **kwargs: Any,
) -> AgentOSRouterClient:
    kwargs.setdefault("ws_client_factory", lambda: FakeAgentWsClient(yuanrong))
    return AgentOSRouterClient(
        yuanrong,
        registry if registry is not None else FakeRegistryClient(),
        agent_manager if agent_manager is not None else AgentManager(),
        **kwargs,
    )


class FakeRegistryClient:
    def __init__(self) -> None:
        self.registered: list[AgentInfo] = []
        self.unregistered: list[dict[str, str]] = []
        self.updated_instances: list[dict] = []
        self.image_lookups = 0
        self.list_user_images_calls: list[str] = []

    async def get_image_info(self, image_name: str) -> ImageInfo:
        self.image_lookups += 1
        imageurl = f"harbor.local/adapted/{image_name}:latest"
        runtime_spec = {
            "runtime": "python3.11",
            "sandbox_type": "docker",
            "rootfs": {
                "imageurl": imageurl,
                "user": "agentos",
                "ports": ["tcp:22"],
            },
            "cpu": 1000,
            "memory": 2048,
        }
        return ImageInfo(
            image_name=image_name,
            image_uri=imageurl,
            metadata={
                "agent_type": image_name,
                "runtime_spec": runtime_spec,
                "env_vars": {},
                "source": "local_stub",
            },
        )

    async def list_user_images(self, user_id: str) -> list[ImageInfo]:
        self.list_user_images_calls.append(user_id)
        return [
            ImageInfo(
                image_name="opencode",
                image_uri="registry://opencode:latest",
                metadata={"agent_type": "opencode", "user_id": user_id},
            ),
        ]

    async def register_agent(self, agent_info: AgentInfo) -> None:
        self.registered.append(agent_info)

    async def unregister_agent(
        self,
        agent_id: str,
        *,
        user_id: str | None = None,
        agent_type: str | None = None,
    ) -> None:
        self.unregistered.append(
            {
                "agent_id": str(agent_id or ""),
                "user_id": str(user_id or ""),
                "agent_type": str(agent_type or ""),
            }
        )

    async def update_instance(
        self, service_id: str, *, node: str | None = None, address: str | None = None
    ) -> None:
        self.updated_instances.append(
            {"service_id": service_id, "node": node, "address": address}
        )

    async def close(self) -> None:
        return None


def _envelope(*, agent_type: str | None = None) -> E2AEnvelope:
    params = {"query": "hello"}
    if agent_type is not None:
        params["agent_type"] = agent_type
    return E2AEnvelope(
        request_id="req-1",
        channel="web",
        user_id="u1",
        session_id="sess-1",
        params=params,
    )


@pytest.mark.asyncio
async def test_swarm_request_creates_builtin_supervisor_runtime() -> None:
    """jiuwenswarm (builtin) now goes through _resolve_agent/_create_agent,
    using an inline supervisor+code_path runtime_spec (no registry image lookup)."""
    yuanrong = FakeYuanRongClient()
    registry = FakeRegistryClient()
    agent_manager = AgentManager()
    client = _router_client(yuanrong, registry, agent_manager)
    envelope = _envelope()

    response = await client.send_request(envelope)
    # _register_agent is fire-and-forget; yield so it completes before assertions
    # (shutdown would cancel pending background tasks before they run).
    await asyncio.sleep(0.05)

    assert response.ok
    # Builtin path does not consult the image registry.
    assert registry.image_lookups == 0
    # Builtin path creates a supervisor sandbox (no longer a direct yuanrong invoke).
    assert yuanrong.create_calls == 1
    assert yuanrong.send_calls == 1
    # The builtin runtime_spec uses supervisor + cmds (no registry image lookup).
    spec = yuanrong.create_payloads[0]["runtime_spec"]
    assert spec["sandbox_type"] == "supervisor"
    assert spec["runtime"] == "python3.11"
    # 动态端口：rootfs.ports 与 env_vars.AGENT_SERVER_PORT 必须一致
    ports = spec["rootfs"]["ports"]
    assert len(ports) == 1 and ports[0].startswith("tcp:")
    dyn_port = ports[0][len("tcp:"):]
    assert dyn_port.isdigit()
    # cmds 含动态端口，与 rootfs.ports 一致
    assert spec["cmds"] == [
        ["sh", "-c", f"exec jiuwenswarm-agentserver --port {dyn_port}"]
    ]
    assert spec["cpu"] == 2000
    assert spec["memory"] == 4096
    assert spec["rootfs"]["imageurl"] == "jiuwenswarm-agent-runtime:latest"
    assert spec["rootfs"]["user"] == "agentos"
    env = yuanrong.create_payloads[0]["env_vars"]
    assert env["AGENT_SERVER_HOST"] == "127.0.0.1"
    assert env["AGENT_SERVER_PORT"] == dyn_port
    # create 后通过 frontend WS 代理直连 instance（不走 invoke 链路）。
    assert yuanrong.ws_connect_uris == [
        "ws://yuanrong.test:8888/serverless/v1/ws"
        f"?instance=sbx-1&tenant_id=default&port={dyn_port}"
    ]
    # Agent is registered with the registry (fire-and-forget background task).
    assert len(registry.registered) == 1
    assert registry.registered[0].agent_type == "jiuwenswarm"
    # A builtin runtime is tracked in the agent manager.
    agents = await agent_manager.list_user_agents("u1")
    assert len(agents) == 1
    assert agents[0].info.agent_type == "jiuwenswarm"
    assert agents[0].info.sandbox_id == "sbx-1"
    # attach_to_envelope populated the routing context.
    assert envelope.channel_context["agent_type"] == "jiuwenswarm"
    assert envelope.channel_context["agent_id"] == agents[0].info.agent_id
    assert envelope.channel_context["sandbox_id"] == "sbx-1"

    await client.shutdown()


def test_is_ws_connect_retryable_for_cold_start_proxy_errors() -> None:
    assert _is_ws_connect_retryable(_Http502("HTTP 502"))
    assert _is_ws_connect_retryable(ConnectionRefusedError())
    assert not _is_ws_connect_retryable(ValueError("bad port"))


@pytest.mark.asyncio
async def test_get_ws_client_retries_http_502_until_ready(monkeypatch) -> None:
    """create 后首连 502 时，_get_ws_client 应退避重试直到 agentserver 就绪."""
    import jiuwenswarm.extensions.agentos.agentos_router.router_client as router_mod

    monkeypatch.setattr(router_mod, "_WS_CONNECT_RETRY_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(router_mod, "_WS_CONNECT_READY_TIMEOUT_SECONDS", 2.0)

    yuanrong = FakeYuanRongClient()
    FlakyAgentWsClient.instances = []
    FlakyAgentWsClient.fail_times = 2
    client = _router_client(
        yuanrong,
        ws_client_factory=lambda: FlakyAgentWsClient(yuanrong),
    )

    response = await client.send_request(_envelope())
    await client.shutdown()

    assert response.ok
    assert yuanrong.send_calls == 1
    assert len(yuanrong.ws_connect_uris) == 1
    assert sum(c.connect_attempts for c in FlakyAgentWsClient.instances) == 3


@pytest.mark.asyncio
async def test_get_ws_client_coalesces_concurrent_first_connect(monkeypatch) -> None:
    """同一 instance 并发首连只跑一轮就绪等待，其它请求等 Future."""
    import jiuwenswarm.extensions.agentos.agentos_router.router_client as router_mod

    monkeypatch.setattr(router_mod, "_WS_CONNECT_RETRY_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(router_mod, "_WS_CONNECT_READY_TIMEOUT_SECONDS", 2.0)

    yuanrong = FakeYuanRongClient()
    FlakyAgentWsClient.instances = []
    FlakyAgentWsClient.fail_times = 1
    client = _router_client(
        yuanrong,
        ws_client_factory=lambda: FlakyAgentWsClient(yuanrong),
    )

    results = await asyncio.gather(
        client.send_request(_envelope()),
        client.send_request(_envelope()),
    )
    await client.shutdown()

    assert all(r.ok for r in results)
    assert yuanrong.create_calls == 1
    assert yuanrong.send_calls == 2
    # leader 重试 2 次 connect；followers 共用结果，不再另起一轮 connect 风暴
    assert sum(c.connect_attempts for c in FlakyAgentWsClient.instances) == 2


@pytest.mark.asyncio
async def test_swarm_request_repeated_reuses_single_runtime() -> None:
    """Repeated jiuwenswarm requests reuse a single runtime (single-flight create)."""
    yuanrong = FakeYuanRongClient()
    registry = FakeRegistryClient()
    agent_manager = AgentManager()
    client = _router_client(yuanrong, registry, agent_manager)

    await client.send_request(_envelope())
    await client.send_request(_envelope())
    await client.shutdown()

    assert registry.image_lookups == 0
    # Single-flight: only the first request creates the sandbox.
    assert yuanrong.create_calls == 1
    assert yuanrong.send_calls == 2
    # Only one runtime is tracked for the user.
    agents = await agent_manager.list_user_agents("u1")
    assert len(agents) == 1
    assert agents[0].info.agent_type == "jiuwenswarm"


def test_resolve_agent_workspace_requires_existing_writable_dir(tmp_path) -> None:
    workspace_root = tmp_path / "ws"
    alice = workspace_root / "alice"
    alice.mkdir(parents=True)
    alice.chmod(0o777)
    assert resolve_agent_workspace("alice", workspace_root=str(workspace_root)) == str(alice)
    # 路径穿越被安全化：sanitized 目录也必须已存在才能通过校验。
    sanitized = workspace_root / "alice_.._bob"
    sanitized.mkdir(parents=True)
    assert resolve_agent_workspace(
        "alice/../bob", workspace_root=str(workspace_root)
    ) == str(sanitized)


def test_resolve_agent_workspace_rejects_missing_dir(tmp_path) -> None:
    workspace_root = tmp_path / "ws"
    with pytest.raises(ValueError, match="does not exist"):
        resolve_agent_workspace("nobody", workspace_root=str(workspace_root))


def test_resolve_agent_workspace_rejects_non_directory(tmp_path) -> None:
    workspace_root = tmp_path / "ws"
    file_path = workspace_root / "file"
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text("x")
    with pytest.raises(ValueError, match="not a directory"):
        resolve_agent_workspace("file", workspace_root=str(workspace_root))


@pytest.mark.asyncio
async def test_third_party_type_creates_via_yuanrong(agentos_workspace_root: str) -> None:
    yuanrong = FakeYuanRongClient()
    agent_manager = AgentManager()
    client = _router_client(yuanrong, FakeRegistryClient(), agent_manager)

    response = await client.send_request(_envelope(agent_type="opencode"))

    assert response.ok
    assert yuanrong.create_calls == 1
    assert yuanrong.create_payloads[0]["workspace"] == str(
        Path(agentos_workspace_root) / "u1"
    )
    assert yuanrong.send_calls == 1
    # 第三方 agent 端口取自 runtime_spec rootfs.ports（tcp:22）。
    assert yuanrong.ws_connect_uris == [
        "ws://yuanrong.test:8888/serverless/v1/ws"
        "?instance=sbx-1&tenant_id=default&port=22"
    ]
    agents = await agent_manager.list_user_agents("u1")
    assert agents[0].info.agent_type == "opencode"
    assert agents[0].info.status is AgentStatus.READY
    assert agents[0].info.sandbox_id == "sbx-1"
    assert agents[0].info.metadata["workspace"] == str(
        Path(agentos_workspace_root) / "u1"
    )


@pytest.mark.asyncio
async def test_agent_switch_creates_without_forwarding_chat() -> None:
    yuanrong = FakeYuanRongClient()
    agent_manager = AgentManager()
    client = _router_client(
        yuanrong, FakeRegistryClient(), agent_manager, ssh_channel_endpoint=_ssh_channel()
    )

    response = await client.thirdagent_switch(
        user_id="u1",
        agent_type="opencode",
        session_id="sess-1",
    )
    await client.shutdown()

    assert response["ok"] is True
    assert yuanrong.create_calls == 1
    assert yuanrong.send_calls == 0
    assert response["payload"]["agent_type"] == "opencode"
    assert response["payload"]["sandbox_id"] == "sbx-1"
    assert response["payload"]["ssh_ip"] == "0.0.0.0"
    assert response["payload"]["ssh_port"] == 2222
    agents = await agent_manager.list_user_agents("u1")
    assert len(agents) == 1
    assert agents[0].info.agent_type == "opencode"


@pytest.mark.asyncio
async def test_agent_switch_fails_without_ssh_endpoint() -> None:
    yuanrong = FakeYuanRongClient()
    agent_manager = AgentManager()
    client = _router_client(yuanrong, FakeRegistryClient(), agent_manager)

    response = await client.thirdagent_switch(
        user_id="u1",
        agent_type="opencode",
        session_id="sess-1",
    )
    await client.shutdown()

    assert response["ok"] is False
    assert response["code"] == "SSH_ENDPOINT_UNAVAILABLE"
    assert "ssh" in response["error"].lower()
    assert yuanrong.create_calls == 0


@pytest.mark.asyncio
async def test_agent_list_returns_registry_images_without_creating() -> None:
    yuanrong = FakeYuanRongClient()
    registry = FakeRegistryClient()
    agent_manager = AgentManager()
    client = _router_client(yuanrong, registry, agent_manager)

    response = await client.thirdagent_list(
        user_id="u1",
        current_agent_type="jiuwenswarm",
    )
    await client.shutdown()

    assert response["ok"] is True
    assert yuanrong.create_calls == 0
    assert yuanrong.send_calls == 0
    assert registry.list_user_images_calls == ["u1"]
    assert response["payload"]["current_agent_type"] == "jiuwenswarm"
    assert [item["agent_type"] for item in response["payload"]["agents"]] == [
        "opencode",
    ]
    assert response["payload"]["agents"][0]["image_uri"] == "registry://opencode:latest"
    assert await agent_manager.list_user_agents("u1") == []


@pytest.mark.asyncio
async def test_agent_switch_reuses_existing_agent() -> None:
    yuanrong = FakeYuanRongClient()
    agent_manager = AgentManager()
    client = _router_client(
        yuanrong, FakeRegistryClient(), agent_manager, ssh_channel_endpoint=_ssh_channel()
    )

    first = await client.thirdagent_switch(
        user_id="u1",
        agent_type="opencode",
        session_id="sess-1",
    )
    second = await client.thirdagent_switch(
        user_id="u1",
        agent_type="opencode",
        session_id="sess-1",
    )
    await client.shutdown()

    assert first["ok"] and second["ok"]
    assert yuanrong.create_calls == 1
    assert yuanrong.send_calls == 0
    assert first["payload"]["agent_id"] == second["payload"]["agent_id"]
    assert first["payload"]["ssh_ip"] == second["payload"]["ssh_ip"]
    assert first["payload"]["ssh_port"] == second["payload"]["ssh_port"]


@pytest.mark.asyncio
async def test_chat_after_switch_reuses_agent() -> None:
    yuanrong = FakeYuanRongClient()
    agent_manager = AgentManager()
    client = _router_client(
        yuanrong, FakeRegistryClient(), agent_manager, ssh_channel_endpoint=_ssh_channel()
    )

    switch_resp = await client.thirdagent_switch(
        user_id="u1",
        agent_type="opencode",
        session_id="sess-1",
    )
    chat_envelope = _envelope(agent_type="opencode")
    chat_resp = await client.send_request(chat_envelope)
    await client.shutdown()

    assert switch_resp["ok"] and chat_resp.ok
    assert yuanrong.create_calls == 1
    assert yuanrong.send_calls == 1
    assert chat_envelope.channel_context["agent_id"] == switch_resp["payload"]["agent_id"]
    assert chat_envelope.channel_context["agent_type"] == "opencode"


@pytest.mark.asyncio
async def test_switch_to_jiuwenswarm_is_direct_without_create() -> None:
    yuanrong = FakeYuanRongClient()
    agent_manager = AgentManager()
    client = _router_client(
        yuanrong, FakeRegistryClient(), agent_manager, ssh_channel_endpoint=_ssh_channel()
    )

    response = await client.thirdagent_switch(
        user_id="u1",
        agent_type="jiuwenswarm",
        session_id="sess-1",
    )
    await client.shutdown()

    assert response["ok"] is True
    assert yuanrong.create_calls == 0
    assert yuanrong.send_calls == 0
    assert response["payload"]["agent_type"] == "jiuwenswarm"
    assert response["payload"]["sandbox_id"] == ""
    assert response["payload"]["ssh_ip"] == "0.0.0.0"
    assert response["payload"]["ssh_port"] == 2222
    assert client.get_current_agent_type("u1") == "jiuwenswarm"
    assert await agent_manager.list_user_agents("u1") == []
    assert "ssh_private_key" not in response["payload"]


@pytest.mark.asyncio
async def test_agent_switch_includes_ephemeral_ssh_private_key_when_issuer_set() -> None:
    yuanrong = FakeYuanRongClient()
    agent_manager = AgentManager()
    client = AgentOSRouterClient(
        yuanrong, FakeRegistryClient(), agent_manager, ssh_channel_endpoint=_ssh_channel()
    )

    class _Issuer:
        def issue_ephemeral_key(self, *, user_id, username, session_id, ttl_sec):
            assert user_id == "u1"
            assert username == "u1"
            assert session_id == "sess-1"
            assert ttl_sec == 120.0
            return "-----BEGIN OPENSSH PRIVATE KEY-----\nTEST\n-----END OPENSSH PRIVATE KEY-----\n"

    client.set_key_issuer(_Issuer(), ephemeral_key_ttl_sec=120.0)
    response = await client.thirdagent_switch(
        user_id="u1",
        agent_type="opencode",
        session_id="sess-1",
    )
    await client.shutdown()

    assert response["ok"] is True
    assert response["payload"]["ssh_private_key"].startswith("-----BEGIN OPENSSH PRIVATE KEY-----")


@pytest.mark.asyncio
async def test_agent_switch_fails_when_key_issuance_raises() -> None:
    yuanrong = FakeYuanRongClient()
    agent_manager = AgentManager()
    client = AgentOSRouterClient(
        yuanrong, FakeRegistryClient(), agent_manager, ssh_channel_endpoint=_ssh_channel()
    )

    class _BrokenIssuer:
        def issue_ephemeral_key(self, *, user_id, username, session_id, ttl_sec):
            raise RuntimeError("asyncssh missing")

    client.set_key_issuer(_BrokenIssuer(), ephemeral_key_ttl_sec=120.0)
    response = await client.thirdagent_switch(
        user_id="u1",
        agent_type="opencode",
        session_id="sess-1",
    )
    await client.shutdown()

    assert response["ok"] is False
    assert response["code"] == "SSH_KEY_ISSUE_FAILED"
    assert "asyncssh missing" in response["error"]
    # Fail fast: no sandbox is created for an unusable switch.
    assert yuanrong.create_calls == 0


@pytest.mark.asyncio
async def test_agent_switch_fails_when_issuer_returns_empty_key() -> None:
    yuanrong = FakeYuanRongClient()
    agent_manager = AgentManager()
    client = AgentOSRouterClient(
        yuanrong, FakeRegistryClient(), agent_manager, ssh_channel_endpoint=_ssh_channel()
    )
    client.set_key_issuer(
        SimpleNamespace(issue_ephemeral_key=lambda **kwargs: ""),
        ephemeral_key_ttl_sec=120.0,
    )

    response = await client.thirdagent_switch(
        user_id="u1",
        agent_type="jiuwenswarm",
        session_id="sess-1",
    )
    await client.shutdown()

    assert response["ok"] is False
    assert response["code"] == "SSH_KEY_ISSUE_FAILED"


@pytest.mark.asyncio
async def test_agent_switch_omits_ssh_private_key_when_issuer_cleared() -> None:
    yuanrong = FakeYuanRongClient()
    agent_manager = AgentManager()
    client = AgentOSRouterClient(
        yuanrong, FakeRegistryClient(), agent_manager, ssh_channel_endpoint=_ssh_channel()
    )
    client.set_key_issuer(
        SimpleNamespace(
            issue_ephemeral_key=lambda **kwargs: "KEY",
        ),
        ephemeral_key_ttl_sec=60.0,
    )
    client.set_key_issuer(None)
    response = await client.thirdagent_switch(
        user_id="u1",
        agent_type="opencode",
        session_id="sess-1",
    )
    await client.shutdown()

    assert response["ok"] is True
    assert "ssh_private_key" not in response["payload"]


@pytest.mark.asyncio
async def test_delete_agent_releases_yuanrong_sandbox() -> None:
    yuanrong = FakeYuanRongClient()
    registry = FakeRegistryClient()
    agent_manager = AgentManager()
    client = _router_client(yuanrong, registry, agent_manager)

    await client.send_request(_envelope(agent_type="opencode"))
    agents = await agent_manager.list_user_agents("u1")
    assert agents[0].info.sandbox_id == "sbx-1"
    agent_id = agents[0].info.agent_id

    assert await client.delete_agent("u1", "opencode") is True

    assert yuanrong.delete_calls == ["sbx-1"]
    assert await agent_manager.list_user_agents("u1") == []
    assert registry.unregistered == [
        {"agent_id": agent_id, "user_id": "u1", "agent_type": "opencode"}
    ]


@pytest.mark.asyncio
async def test_delete_agent_missing_is_noop() -> None:
    client = _router_client(
        FakeYuanRongClient(), FakeRegistryClient(), AgentManager()
    )
    assert await client.delete_agent("u1", "opencode") is False

class StubRelaySession:
    def __init__(self) -> None:
        self.session_id = "ssh_u1_test"
        self.exit_code: int | None = None
        self.done = asyncio.Event()
        self.relay_task = None


class StubSshRelay:
    """Stands in for YuanrongSshRelay: run() blocks until released."""

    def __init__(self) -> None:
        self.run_instance_ids: list[str] = []
        self.failures: list[str] = []
        self.started = asyncio.Event()
        self.finish = asyncio.Event()

    def fail_session(self, session: StubRelaySession, message: str) -> None:
        self.failures.append(message)
        session.exit_code = 1
        session.done.set()

    async def run(
        self,
        session: StubRelaySession,
        instance_id: str,
        *,
        user_id: str,
    ) -> None:
        del user_id
        self.run_instance_ids.append(instance_id)
        self.started.set()
        await self.finish.wait()
        session.exit_code = 0
        session.done.set()


@pytest.mark.asyncio
async def test_reap_idle_once_deletes_idle_sandbox() -> None:
    yuanrong = FakeYuanRongClient()
    registry = FakeRegistryClient()
    agent_manager = AgentManager()
    client = _router_client(
        yuanrong,
        registry,
        agent_manager,
        sandbox_idle_timeout_seconds=0.01,
    )

    await client.send_request(_envelope(agent_type="opencode"))
    assert yuanrong.create_calls == 1
    agents = await agent_manager.list_user_agents("u1")
    agent_id = agents[0].info.agent_id

    await asyncio.sleep(0.05)
    reaped = await client._reap_idle_once()
    await client.shutdown()

    assert reaped == 1
    assert yuanrong.delete_calls == ["sbx-1"]
    assert await agent_manager.list_user_agents("u1") == []
    assert registry.unregistered == [
        {"agent_id": agent_id, "user_id": "u1", "agent_type": "opencode"}
    ]

@pytest.mark.asyncio
async def test_reap_idle_once_skips_recently_active_and_held_agents() -> None:
    yuanrong = FakeYuanRongClient()
    agent_manager = AgentManager()
    client = _router_client(
        yuanrong,
        FakeRegistryClient(),
        agent_manager,
        sandbox_idle_timeout_seconds=60.0,
    )

    # Recently active: idle clock has not expired.
    await client.send_request(_envelope(agent_type="opencode"))
    assert await client._reap_idle_once() == 0

    # Held: a live task pins the agent even when stale.
    held = await agent_manager.get_or_create_agent(
        "u1", "opencode", key_values={"session_id": "sess-1"}, acquire=True
    )
    client._sandbox_idle_timeout_seconds = 0.01
    await asyncio.sleep(0.05)
    assert await client._reap_idle_once() == 0
    assert yuanrong.delete_calls == []

    # Released and stale: reclaimed.
    await agent_manager.release(held.key)
    await asyncio.sleep(0.05)
    assert await client._reap_idle_once() == 1
    assert yuanrong.delete_calls == ["sbx-1"]
    await client.shutdown()


@pytest.mark.asyncio
async def test_ssh_relay_holds_sandbox_until_disconnect() -> None:
    yuanrong = FakeYuanRongClient()
    agent_manager = AgentManager()
    ssh_relay = StubSshRelay()
    client = _router_client(
        yuanrong,
        FakeRegistryClient(),
        agent_manager,
        ssh_relay=ssh_relay,
        sandbox_idle_timeout_seconds=0.01,
    )
    relay_session = StubRelaySession()

    relay_task = asyncio.create_task(
        client._run_ssh_relay(_envelope(agent_type="opencode"), relay_session)
    )
    await ssh_relay.started.wait()
    assert yuanrong.create_calls == 1

    # A silent-but-live SSH session holds the task count: never reclaimed.
    await asyncio.sleep(0.05)
    assert await client._reap_idle_once() == 0
    assert yuanrong.delete_calls == []

    # Disconnect releases the hold; the idle clock starts now.
    ssh_relay.finish.set()
    await relay_task
    assert relay_session.done.is_set()
    await asyncio.sleep(0.05)
    assert await client._reap_idle_once() == 1
    assert yuanrong.delete_calls == ["sbx-1"]
    assert await agent_manager.list_user_agents("u1") == []
    await client.shutdown()


@pytest.mark.asyncio
async def test_idle_reaper_disabled_with_nonpositive_timeout() -> None:
    yuanrong = FakeYuanRongClient()
    agent_manager = AgentManager()
    client = _router_client(
        yuanrong,
        FakeRegistryClient(),
        agent_manager,
        sandbox_idle_timeout_seconds=0,
    )

    await client.connect("http://yuanrong.test")
    assert client._idle_reaper_task is None

    await client.send_request(_envelope(agent_type="opencode"))
    await asyncio.sleep(0.05)
    assert await client._reap_idle_once() == 0
    await client.shutdown()

    assert yuanrong.delete_calls == []
    assert len(await agent_manager.list_user_agents("u1")) == 1


@pytest.mark.asyncio
async def test_idle_reaper_task_lifecycle_on_connect_disconnect() -> None:
    yuanrong = FakeYuanRongClient()
    client = _router_client(
        yuanrong,
        FakeRegistryClient(),
        AgentManager(),
        sandbox_idle_timeout_seconds=600.0,
    )

    await client.connect("http://yuanrong.test")
    task = client._idle_reaper_task
    assert task is not None and not task.done()

    await client.disconnect()
    assert client._idle_reaper_task is None
    assert task.cancelled() or task.done()


@pytest.mark.asyncio
async def test_unsupported_third_agent_returns_unsupported() -> None:
    from jiuwenswarm.gateway.routing.third_agent import get_unsupported_third_agent

    third = get_unsupported_third_agent()
    listed = await third.thirdagent_list(user_id="u1", current_agent_type="jiuwenswarm")
    switched = await third.thirdagent_switch(
        user_id="u1", agent_type="opencode", session_id="s1"
    )

    assert listed["ok"] is False
    assert listed["code"] == "UNSUPPORTED"
    assert switched["ok"] is False
    assert switched["code"] == "UNSUPPORTED"


@pytest.mark.asyncio
async def test_agentos_third_agent_list_and_switch() -> None:
    from jiuwenswarm.extensions.agentos.agentos_router.third_agent import (
        AgentOSThirdAgent,
    )

    yuanrong = FakeYuanRongClient()
    registry = FakeRegistryClient()
    agent_manager = AgentManager()
    client = _router_client(
        yuanrong, registry, agent_manager, ssh_channel_endpoint=_ssh_channel(port=2223)
    )
    third = AgentOSThirdAgent(client)

    listed = await third.thirdagent_list(user_id="u1", current_agent_type="jiuwenswarm")
    switched = await third.thirdagent_switch(
        user_id="u1", agent_type="opencode", session_id="sess-1"
    )
    await client.shutdown()

    assert listed["ok"] is True
    assert [item["agent_type"] for item in listed["payload"]["agents"]] == [
        "opencode",
    ]
    assert switched["ok"] is True
    assert switched["payload"]["agent_type"] == "opencode"
    assert switched["payload"]["ssh_ip"] == "0.0.0.0"
    assert switched["payload"]["ssh_port"] == 2223
    assert yuanrong.create_calls == 1


# ---------------------------------------------------------------------------
# disconnect_cleanup_timeout_seconds: configurable + idle-aware delete path
# ---------------------------------------------------------------------------


async def _ready_creator(agent_info: AgentInfo) -> AgentInfo:
    """Minimal creator that marks the runtime READY with a stub sandbox id."""
    agent_info.sandbox_id = "fake-sbx"
    agent_info.status = AgentStatus.READY
    return agent_info


@pytest.mark.asyncio
async def test_delayed_cleanup_uses_configured_timeout() -> None:
    """The wait must follow ``disconnect_cleanup_timeout_seconds``,
    not the old hardcoded 60s."""
    yuanrong = FakeYuanRongClient()
    agent_manager = AgentManager()
    client = _router_client(
        yuanrong,
        FakeRegistryClient(),
        agent_manager,
        disconnect_cleanup_timeout_seconds=0.05,
    )
    await agent_manager.get_or_create_agent(
        "u1", "jiuwenswarm", creator=_ready_creator
    )

    start = asyncio.get_running_loop().time()
    await client._delayed_cleanup("u1")
    elapsed = asyncio.get_running_loop().time() - start

    # If the wait was still 60s this test would take 60s; assert the new knob
    # actually drove the sleep.
    assert elapsed < 5.0, f"delayed cleanup took {elapsed:.2f}s, expected ~0.05s"


@pytest.mark.asyncio
async def test_delayed_cleanup_skips_held_agents() -> None:
    """Like the idle reaper, a held agent (task_count > 0) must not be killed
    by the disconnect cleanup even after the timeout elapses."""
    yuanrong = FakeYuanRongClient()
    agent_manager = AgentManager()
    client = _router_client(
        yuanrong,
        FakeRegistryClient(),
        agent_manager,
        disconnect_cleanup_timeout_seconds=0.01,
    )
    held = await agent_manager.get_or_create_agent(
        "u1", "jiuwenswarm", creator=_ready_creator, acquire=True
    )
    assert held.is_ready()
    assert held.task_count == 1

    await asyncio.sleep(0.05)
    await client._delayed_cleanup("u1")

    # pop_if_idle must have refused the held agent → nothing deleted.
    assert await agent_manager.get_agent("u1", "jiuwenswarm") is not None
    assert yuanrong.delete_calls == []

    await agent_manager.release(held.key)
    await client.shutdown()


@pytest.mark.asyncio
async def test_delayed_cleanup_deletes_unheld_idle_builtin() -> None:
    """When the task is ended (task_count == 0) and idle past the timeout,
    the disconnect cleanup must delete the agent (mirroring the idle reaper)."""
    yuanrong = FakeYuanRongClient()
    registry = FakeRegistryClient()
    agent_manager = AgentManager()
    client = _router_client(
        yuanrong,
        registry,
        agent_manager,
        disconnect_cleanup_timeout_seconds=0.01,
    )
    runtime = await agent_manager.get_or_create_agent(
        "u1", "jiuwenswarm", creator=_ready_creator
    )
    assert runtime.task_count == 0

    await asyncio.sleep(0.05)
    await client._delayed_cleanup("u1")

    assert await agent_manager.get_agent("u1", "jiuwenswarm") is None
    assert yuanrong.delete_calls == ["fake-sbx"]
    assert registry.unregistered == [
        {
            "agent_id": runtime.info.agent_id,
            "user_id": "u1",
            "agent_type": "jiuwenswarm",
        }
    ]
    await client.shutdown()


@pytest.mark.asyncio
async def test_delayed_cleanup_skips_creating_or_non_builtin_agents() -> None:
    """Cleanup is gated by the same READY+task_count==0+idle triple as the
    idle reaper, so non-READY runtimes are not force-deleted."""
    yuanrong = FakeYuanRongClient()
    agent_manager = AgentManager()
    client = _router_client(
        yuanrong,
        FakeRegistryClient(),
        agent_manager,
        disconnect_cleanup_timeout_seconds=0.01,
    )
    # Seed a CREATING runtime directly: get_or_create_agent() without a
    # creator resolves to READY, so we must insert the runtime ourselves.
    key = AgentRuntime.build_key(
        agent_manager.key_fields, user_id="u1", agent_type="jiuwenswarm"
    )
    agent_manager._runtimes[key] = AgentRuntime.for_key(
        key, user_id="u1", agent_type="jiuwenswarm"
    )
    await asyncio.sleep(0.05)

    await client._delayed_cleanup("u1")

    # Still tracked, not deleted.
    assert await agent_manager.get_agent("u1", "jiuwenswarm") is not None
    assert yuanrong.delete_calls == []
    await client.shutdown()


@pytest.mark.asyncio
async def test_delayed_cleanup_cancelled_on_reconnect() -> None:
    """Reconnect during the wait window must cancel the pending cleanup task."""
    yuanrong = FakeYuanRongClient()
    agent_manager = AgentManager()
    client = _router_client(
        yuanrong,
        FakeRegistryClient(),
        agent_manager,
        # Long enough that the cleanup task is still asleep when we reconnect.
        disconnect_cleanup_timeout_seconds=10.0,
    )
    await agent_manager.get_or_create_agent(
        "u1", "jiuwenswarm", creator=_ready_creator
    )

    # Disconnect while connection count was 0 → schedules the pending task.
    await client._on_channel_event(
        SimpleNamespace(user_id="u1", event_type="disconnected")
    )
    pending = client._pending_cleanups.get("u1")
    assert pending is not None and not pending.done()

    # Reconnect before the timeout elapses → task must be cancelled.
    await client._on_channel_event(
        SimpleNamespace(user_id="u1", event_type="connected")
    )
    # Python 3.11+: even though _delayed_cleanup swallows CancelledError inside
    # its sleep() and returns cleanly, the task itself is still in the
    # "cancelled" state, so awaiting must re-raise CancelledError.
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert "u1" not in client._pending_cleanups
    assert yuanrong.delete_calls == []
    await client.shutdown()


def test_load_router_config_disconnect_cleanup_knobs(monkeypatch) -> None:
    base_agent_client = {
        "type": "agentos_router",
        "frontend_endpoint": "http://yuanrong.test",
        "function_version_urn": "urn:test",
    }
    monkeypatch.delenv("DISCONNECT_CLEANUP_TIMEOUT_SECONDS", raising=False)

    # Default keeps the historical 60s.
    defaults = load_router_config({"gateway": {"agent_client": base_agent_client}})
    assert defaults.disconnect_cleanup_timeout_seconds == 60.0

    # Explicit yaml value is honored.
    loaded = load_router_config(
        {
            "gateway": {
                "agent_client": base_agent_client,
                "agentos": {"disconnect_cleanup_timeout_seconds": 120},
            }
        }
    )
    assert loaded.disconnect_cleanup_timeout_seconds == 120.0

    # Env overrides yaml (incl. yaml=0).
    monkeypatch.setenv("DISCONNECT_CLEANUP_TIMEOUT_SECONDS", "30")
    env_loaded = load_router_config(
        {
            "gateway": {
                "agent_client": base_agent_client,
                "agentos": {"disconnect_cleanup_timeout_seconds": 0},
            }
        }
    )
    assert env_loaded.disconnect_cleanup_timeout_seconds == 30.0

    # Env explicit 0 disables.
    monkeypatch.setenv("DISCONNECT_CLEANUP_TIMEOUT_SECONDS", "0")
    assert (
        load_router_config(
            {"gateway": {"agent_client": base_agent_client}}
        ).disconnect_cleanup_timeout_seconds
        == 0.0
    )


def test_agentos_selected_by_agent_client_type() -> None:
    assert agentos_router_selected(
        {
            "gateway": {
                "agent_client": {"type": "agentos_router"},
            }
        }
    )
    assert not agentos_router_selected(
        {
            "gateway": {
                "agent_client": {"type": "websocket"},
            }
        }
    )
    assert not agentos_router_selected(
        {
            "gateway": {
                "agent_client": {"type": "yuanrong"},
            }
        }
    )


def test_load_router_config_agent_key_fields() -> None:
    config = {
        "gateway": {
            "agent_client": {
                "type": "agentos_router",
                "frontend_endpoint": "http://yuanrong.test",
                "function_version_urn": "urn:test",
            },
            "agentos": {
                "agent_key_fields": ["user_id", "agent_type", "session_id"],
                "workspace_root": "/data/agentos/users",
                "ssh": {"client_keys_dir": "/data/agentos/.ssh"},
                "registry": {
                    "endpoint": "http://127.0.0.1:8000",
                    "node": "192.168.0.12",
                },
            },
        }
    }
    loaded = load_router_config(config)
    assert loaded.agent_key_fields == ("user_id", "agent_type", "session_id")
    assert loaded.workspace_root == "/data/agentos/users"
    assert loaded.ssh.client_keys_dir == "/data/agentos/.ssh"
    assert loaded.registry.endpoint == "http://127.0.0.1:8000"
    assert loaded.registry.node == "192.168.0.12"

    default_loaded = load_router_config(
        {
            "gateway": {
                "agent_client": {
                    "frontend_endpoint": "http://yuanrong.test",
                    "function_version_urn": "urn:test",
                }
            }
        }
    )
    assert default_loaded.agent_key_fields == ("user_id", "agent_type")
    assert default_loaded.workspace_root == DEFAULT_AGENT_WORKSPACE_ROOT
    assert default_loaded.ssh.client_keys_dir == "/root/.ssh"


def test_load_router_config_sandbox_idle_knobs(monkeypatch) -> None:
    base_agent_client = {
        "type": "agentos_router",
        "frontend_endpoint": "http://yuanrong.test",
        "function_version_urn": "urn:test",
    }
    monkeypatch.delenv("SANDBOX_IDLE_TIMEOUT_SECONDS", raising=False)

    defaults = load_router_config({"gateway": {"agent_client": base_agent_client}})
    assert defaults.sandbox_idle_timeout_seconds == 600.0
    assert defaults.sandbox_idle_check_interval_seconds == 30.0

    loaded = load_router_config(
        {
            "gateway": {
                "agent_client": base_agent_client,
                "agentos": {
                    "sandbox_idle_timeout_seconds": 0,
                    "sandbox_idle_check_interval_seconds": 5,
                },
            }
        }
    )
    # Explicit 0 must be honored (disables reclamation), not swallowed.
    assert loaded.sandbox_idle_timeout_seconds == 0.0
    assert loaded.sandbox_idle_check_interval_seconds == 5.0

    # Env overrides yaml (including yaml=0).
    monkeypatch.setenv("SANDBOX_IDLE_TIMEOUT_SECONDS", "120")
    env_loaded = load_router_config(
        {
            "gateway": {
                "agent_client": base_agent_client,
                "agentos": {"sandbox_idle_timeout_seconds": 0},
            }
        }
    )
    assert env_loaded.sandbox_idle_timeout_seconds == 120.0

    # Env explicit 0 also disables.
    monkeypatch.setenv("SANDBOX_IDLE_TIMEOUT_SECONDS", "0")
    assert (
        load_router_config(
            {"gateway": {"agent_client": base_agent_client}}
        ).sandbox_idle_timeout_seconds
        == 0.0
    )


@pytest.mark.asyncio
async def test_create_uses_configured_workspace_root(tmp_path) -> None:
    yuanrong = FakeYuanRongClient()
    ws_user = tmp_path / "ws" / "u1"
    ws_user.mkdir(parents=True)
    ws_user.chmod(0o777)
    client = _router_client(
        yuanrong,
        FakeRegistryClient(),
        AgentManager(),
        ssh_channel_endpoint=_ssh_channel(),
        workspace_root=str(tmp_path / "ws"),
    )
    try:
        response = await client.thirdagent_switch(
            user_id="u1",
            agent_type="opencode",
            session_id="sess-1",
        )
        assert response["ok"] is True
        assert yuanrong.create_payloads[0]["workspace"] == str(ws_user)
    finally:
        await client.shutdown()


@pytest.mark.asyncio
async def test_agentos_extension_is_selected_independently(
    monkeypatch,
) -> None:
    from jiuwenswarm.extensions.agent_client import extension as plain_extension
    from jiuwenswarm.extensions.agentos import extension as agentos_extension
    from jiuwenswarm.extensions.agentos.agentos_router import (
        extension as agentos_router_impl,
    )

    config = {
        "gateway": {
            "agent_client": {
                "type": "agentos_router",
                "frontend_endpoint": "http://yuanrong.test",
                "function_version_urn": "urn:test",
            },
        }
    }
    monkeypatch.setattr(agentos_router_impl, "get_config", lambda: config)
    monkeypatch.setattr(plain_extension, "get_config", lambda: config)

    class Registry:
        registered = None
        third_agent = None

        def register_agent_server_client(self, extension) -> None:
            self.registered = extension

        def register_third_agent(self, extension) -> None:
            self.third_agent = extension

    registry = Registry()
    assert await plain_extension.register_extensions(registry) == []
    registered = await agentos_extension.register_extensions(registry)

    assert len(registered) == 1
    assert isinstance(registered[0], AgentOSRouter)
    assert registry.registered is registered[0]
    assert registry.third_agent is registered[0]
    assert registry.third_agent.get_third_agent() is registered[0].get_third_agent()
    await registered[0].shutdown()
