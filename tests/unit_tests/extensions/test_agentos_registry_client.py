# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from jiuwenswarm.extensions.agentos.agentos_router.models import AgentInfo, AgentStatus
from jiuwenswarm.extensions.agentos.agentos_router.registry_client import (
    RegistryClient,
    RegistryConfig,
    RegistryConflictError,
    RegistryNotFoundError,
    instance_service_id,
    resolve_instance_kind,
)


def test_instance_service_id_is_deterministic() -> None:
    first = instance_service_id("user-01", "opencode")
    second = instance_service_id("user-01", "opencode")
    other = instance_service_id("user-02", "opencode")
    assert first == second
    assert first.startswith("generic_")
    assert len(first) == len("generic_") + 8
    assert first != other


def test_resolve_instance_kind() -> None:
    assert resolve_instance_kind("opencode") == "三方"
    assert resolve_instance_kind("custom-agent") == "三方"
    assert resolve_instance_kind("jiuwenswarm") == "九问"
    assert resolve_instance_kind("jiuwen-report") == "九问"


@pytest.mark.asyncio
async def test_local_stub_get_image_and_register() -> None:
    client = RegistryClient(RegistryConfig())
    image = await client.get_image_info("opencode")
    assert image.image_name == "opencode"
    assert image.image_uri == "local/stub/opencode:latest"
    assert image.metadata["source"] == "local_stub"
    assert image.metadata["runtime_spec"]["rootfs"]["imageurl"] == image.image_uri
    assert image.metadata["env_vars"] == {}

    agent = AgentInfo(user_id="u1", agent_type="opencode", status=AgentStatus.READY)
    agent.metadata["node"] = "192.168.0.12"
    agent.metadata["address"] = "10.244.1.7:4096"
    await client.register_agent(agent)
    assert agent.agent_id in client._registered_agents  # noqa: SLF001
    assert client._agent_service_ids[agent.agent_id] == instance_service_id(  # noqa: SLF001
        "u1", "opencode"
    )
    await client.close()


@pytest.mark.asyncio
async def test_local_list_user_images_contains_supported_types() -> None:
    client = RegistryClient(RegistryConfig())
    images = await client.list_user_images("user-01")
    names = {item.image_name for item in images}
    assert names == {"jiuwenswarm"}
    assert all(item.metadata.get("user_id") == "user-01" for item in images)
    await client.close()


class _FakeRegistryTransport(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any] | None, dict[str, Any] | None]] = []
        self._instances: dict[str, dict[str, Any]] = {}

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        method = request.method.upper()
        path = request.url.path
        params = dict(request.url.params)
        body: dict[str, Any] | None = None
        if request.content:
            body = json.loads(request.content.decode("utf-8"))
        self.calls.append((method, path, body, params or None))

        if method == "GET" and path.endswith("/launch-spec"):
            framework = path.split("/")[-2]
            version = params.get("version") or "v0.2.0"
            imageurl = f"harbor.local/adapted/{framework}:{version}"
            return httpx.Response(
                200,
                json={
                    "framework": framework,
                    "framework_version": version,
                    "runtime_spec": {
                        "runtime": "python3.11",
                        "sandbox_type": "docker",
                        "rootfs": {
                            "imageurl": imageurl,
                            "user": "agentos",
                            "ports": ["tcp:8080"],
                        },
                        "cpu": 1000,
                        "memory": 2048,
                    },
                    "env_vars": {"A2X_LLM_KEY": "${A2X_LLM_KEY}"},
                },
            )

        if method == "GET" and path.rstrip("/").endswith("/api/images"):
            return httpx.Response(
                200,
                json=[
                    {
                        "framework": "opencode",
                        "framework_version": "v0.1.0",
                        "is_default": False,
                        "imageurl": "harbor.local/adapted/opencode:v0.1.0",
                        "cpu": 500,
                        "memory": 1024,
                        "uploaded_by": "user-01",
                    },
                    {
                        "framework": "opencode",
                        "framework_version": "v0.2.0",
                        "is_default": True,
                        "imageurl": "harbor.local/adapted/opencode:v0.2.0",
                        "cpu": 1000,
                        "memory": 2048,
                        "ports": [{"port": 8080, "protocol": "tcp"}],
                        "env": {"A2X_LLM_KEY": "${A2X_LLM_KEY}"},
                        "uploaded_by": "user-01",
                    },
                ],
            )

        if method == "POST" and path.rstrip("/").endswith("/api/instances"):
            assert body is not None
            record = {**body, "dataset": "default", "status": "运行"}
            self._instances[str(body["service_id"])] = record
            return httpx.Response(200, json=record)

        if method == "PATCH" and "/api/instances/" in path:
            sid = path.rstrip("/").split("/")[-1]
            if sid not in self._instances:
                return httpx.Response(404, json={"detail": "not found"})
            self._instances[sid].update(body or {})
            return httpx.Response(200, json=self._instances[sid])

        if method == "DELETE" and "/api/instances/" in path:
            sid = path.rstrip("/").split("/")[-1]
            existed = sid in self._instances
            self._instances.pop(sid, None)
            return httpx.Response(
                200,
                json={"service_id": sid, "dataset": "default", "deleted": existed},
            )

        if method == "POST" and path.endswith("/heartbeat"):
            node = path.split("/")[-2]
            return httpx.Response(
                200,
                json={
                    "node": node,
                    "state": "healthy",
                    "ttl_seconds": 90,
                    "expires_at": 1751800000.0,
                },
            )

        if method == "GET" and path.endswith("/missing/launch-spec"):
            return httpx.Response(404, json={"detail": "image not found"})

        return httpx.Response(500, json={"detail": f"unhandled {method} {path}"})


@pytest.mark.asyncio
async def test_http_launch_spec_register_update_heartbeat() -> None:
    transport = _FakeRegistryTransport()
    client = RegistryClient(
        RegistryConfig(
            endpoint="http://registry.test",
            request_timeout_s=5.0,
            node="192.168.0.12",
        )
    )
    client._http = httpx.AsyncClient(  # noqa: SLF001
        base_url="http://registry.test/",
        transport=transport,
        timeout=5.0,
    )

    spec = await client.get_launch_spec("opencode")
    assert spec.framework == "opencode"
    assert spec.framework_version == "v0.2.0"
    assert spec.runtime_spec["rootfs"]["imageurl"].endswith("opencode:v0.2.0")
    assert spec.runtime_spec["runtime"] == "python3.11"
    assert spec.runtime_spec["cpu"] == 1000
    assert spec.env_vars["A2X_LLM_KEY"] == "${A2X_LLM_KEY}"

    image = await client.get_image_info("opencode")
    assert image.image_uri == "harbor.local/adapted/opencode:v0.2.0"
    assert image.metadata["framework_version"] == "v0.2.0"
    assert image.metadata["runtime_spec"]["sandbox_type"] == "docker"
    assert image.metadata["env_vars"]["A2X_LLM_KEY"] == "${A2X_LLM_KEY}"

    sid = instance_service_id("user-01", "opencode")
    record = await client.register_instance(
        service_id=sid,
        kind="三方",
        framework="opencode",
        framework_version="v0.2.0",
        node="192.168.0.12",
        address="10.244.1.7:4096",
        user="user-01",
    )
    assert record.status == "运行"
    assert record.service_id == sid

    updated = await client.update_instance(
        sid, node="192.168.0.20", address="10.244.3.9:4096"
    )
    assert updated.node == "192.168.0.20"
    assert updated.address == "10.244.3.9:4096"

    hb = await client.report_node_heartbeat()
    assert hb.node == "192.168.0.12"
    assert hb.state == "healthy"
    assert hb.ttl_seconds == 90

    deleted = await client.unregister_instance(sid)
    assert deleted["deleted"] is True

    methods = [call[0] for call in transport.calls]
    assert "GET" in methods
    assert "POST" in methods
    assert "PATCH" in methods
    assert "DELETE" in methods
    await client.close()


@pytest.mark.asyncio
async def test_http_list_images_flat_entries_prefer_default() -> None:
    transport = _FakeRegistryTransport()
    client = RegistryClient(RegistryConfig(endpoint="http://registry.test"))
    client._http = httpx.AsyncClient(  # noqa: SLF001
        base_url="http://registry.test/",
        transport=transport,
        timeout=5.0,
    )

    entries = await client.list_images()
    assert len(entries) == 2
    assert entries[0].framework == "opencode"
    assert entries[0].framework_version == "v0.1.0"
    assert entries[0].is_default is False
    assert entries[1].is_default is True
    assert entries[1].imageurl.endswith("opencode:v0.2.0")

    images = await client.list_user_images("user-01")
    by_name = {item.image_name: item for item in images}
    assert set(by_name) == {"opencode"}
    assert by_name["opencode"].image_uri.endswith("opencode:v0.2.0")
    assert by_name["opencode"].metadata["is_default"] is True
    assert by_name["opencode"].metadata["framework_version"] == "v0.2.0"
    await client.close()


@pytest.mark.asyncio
async def test_http_register_agent_maps_fields() -> None:
    transport = _FakeRegistryTransport()
    client = RegistryClient(RegistryConfig(endpoint="http://registry.test"))
    client._http = httpx.AsyncClient(  # noqa: SLF001
        base_url="http://registry.test/",
        transport=transport,
        timeout=5.0,
    )
    agent = AgentInfo(
        user_id="user-01",
        agent_type="opencode",
        sandbox_id="sbx-1",
        status=AgentStatus.READY,
        metadata={
            "image_info": {"framework_version": "v0.2.0"},
            "sandbox": {"node": "192.168.0.12", "address": "10.244.1.7:4096"},
        },
    )
    await client.register_agent(agent)
    post = next(call for call in transport.calls if call[0] == "POST")
    assert post[1].endswith("/api/instances")
    assert post[2] is not None
    assert post[2]["service_id"] == instance_service_id("user-01", "opencode")
    assert post[2]["kind"] == "三方"
    assert post[2]["node"] == "192.168.0.12"
    assert post[2]["address"] == "10.244.1.7:4096"
    await client.unregister_agent(agent.agent_id)
    delete = next(call for call in transport.calls if call[0] == "DELETE")
    assert delete[1].endswith(f"/api/instances/{instance_service_id('user-01', 'opencode')}")
    await client.close()


@pytest.mark.asyncio
async def test_unregister_agent_resolves_service_id_without_local_map() -> None:
    """Idle delete can race ahead of async register; still DELETE by (user, framework)."""
    transport = _FakeRegistryTransport()
    client = RegistryClient(RegistryConfig(endpoint="http://registry.test"))
    client._http = httpx.AsyncClient(  # noqa: SLF001
        base_url="http://registry.test/",
        transport=transport,
        timeout=5.0,
    )
    await client.unregister_agent(
        "agent-not-yet-mapped",
        user_id="user-01",
        agent_type="opencode",
    )
    delete = next(call for call in transport.calls if call[0] == "DELETE")
    assert delete[1].endswith(
        f"/api/instances/{instance_service_id('user-01', 'opencode')}"
    )
    await client.close()


class _ErrorTransport(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/missing/launch-spec"):
            return httpx.Response(404, json={"detail": "not found"})
        if path.endswith("/busy/v1"):
            return httpx.Response(409, json={"detail": "in use"})
        return httpx.Response(500, json={"detail": "boom"})


@pytest.mark.asyncio
async def test_http_errors_mapped() -> None:
    client = RegistryClient(RegistryConfig(endpoint="http://registry.test"))
    client._http = httpx.AsyncClient(  # noqa: SLF001
        base_url="http://registry.test/",
        transport=_ErrorTransport(),
        timeout=5.0,
    )
    with pytest.raises(RegistryNotFoundError):
        await client.get_launch_spec("missing")
    with pytest.raises(RegistryConflictError):
        await client._request_json("DELETE", "api/images/busy/v1")  # noqa: SLF001
    await client.close()
