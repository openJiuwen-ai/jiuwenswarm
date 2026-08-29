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
    compute_backoff_delay,
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


def test_compute_backoff_delay_exponential() -> None:
    # attempt 1 → 1s；attempt 2 → 2s；attempt 3 → 4s；attempt 4 → 8s
    assert compute_backoff_delay(1) == 1.0
    # attempt 10 → base 512s，封顶为 30s
    assert compute_backoff_delay(2) == 2.0
    assert compute_backoff_delay(3) == 4.0
    assert compute_backoff_delay(4) == 8.0
    assert compute_backoff_delay(10) == 30.0
    assert compute_backoff_delay(100) == 30.0
    # 自定义参数生效
    assert compute_backoff_delay(4, initial_delay=1.0, multiplier=2.0, max_delay=16.0) == 8.0
    assert compute_backoff_delay(10, max_delay=8.0) == 8.0
    # attempt <= 1 时按 0 处理
    assert compute_backoff_delay(0) == 1.0


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

        if method == "GET" and path.rstrip("/").endswith("/api/instances"):
            records = list(self._instances.values())
            if params.get("include_unhealthy") not in ("true", "1", "True"):
                records = [row for row in records if row.get("status") == "运行"]
            node = params.get("node")
            if node:
                records = [row for row in records if row.get("node") == node]
            return httpx.Response(200, json=records)

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

        if method == "GET" and path.endswith("/missing/launch-spec"):
            return httpx.Response(404, json={"detail": "image not found"})

        return httpx.Response(500, json={"detail": f"unhandled {method} {path}"})


@pytest.mark.asyncio
async def test_http_launch_spec_register_update_roundtrip() -> None:
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
    assert record.instance_id == ""

    record = await client.register_instance(
        service_id=sid,
        kind="三方",
        framework="opencode",
        framework_version="v0.2.0",
        node="192.168.0.12",
        address="10.244.1.7:4096",
        instance_id="yr-instance-1",
        user="user-01",
    )
    assert record.instance_id == "yr-instance-1"

    listed = await client.list_instances(include_unhealthy=True)
    assert [row.instance_id for row in listed] == ["yr-instance-1"]

    updated = await client.update_instance(
        sid, node="192.168.0.20", address="10.244.3.9:4096"
    )
    assert updated.node == "192.168.0.20"
    assert updated.address == "10.244.3.9:4096"

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
    assert post[2]["instance_id"] == "sbx-1"
    await client.unregister_agent(agent.agent_id)
    delete = next(call for call in transport.calls if call[0] == "DELETE")
    assert delete[1].endswith(f"/api/instances/{instance_service_id('user-01', 'opencode')}")
    await client.close()


@pytest.mark.asyncio
async def test_http_register_agent_address_falls_back_to_instance_id() -> None:
    transport = _FakeRegistryTransport()
    client = RegistryClient(RegistryConfig(endpoint="http://registry.test"))
    client._http = httpx.AsyncClient(  # noqa: SLF001
        base_url="http://registry.test/",
        transport=transport,
        timeout=5.0,
    )
    agent = AgentInfo(
        user_id="user-02",
        agent_type="jiuwenswarm",
        sandbox_id="sbx-pending-ip",
        status=AgentStatus.READY,
    )
    await client.register_agent(agent)
    post = next(call for call in transport.calls if call[0] == "POST")
    assert post[2] is not None
    assert post[2]["instance_id"] == "sbx-pending-ip"
    assert post[2]["address"] == "sbx-pending-ip"
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


@pytest.mark.asyncio
async def test_unregister_instance_missing_is_success() -> None:
    class _NotFoundDelete(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"detail": "not found"})

    client = RegistryClient(RegistryConfig(endpoint="http://registry.test"))
    client._http = httpx.AsyncClient(  # noqa: SLF001
        base_url="http://registry.test/",
        transport=_NotFoundDelete(),
        timeout=5.0,
    )
    result = await client.unregister_instance("generic_deadbeef")
    assert result["deleted"] is False
    await client.close()


@pytest.mark.asyncio
async def test_local_stub_list_instances_includes_instance_id() -> None:
    client = RegistryClient(RegistryConfig())
    agent = AgentInfo(
        user_id="u1",
        agent_type="opencode",
        sandbox_id="sbx-local",
        status=AgentStatus.READY,
        metadata={"node": "10.0.0.1", "address": "10.0.0.8:1"},
    )
    await client.register_agent(agent)
    rows = await client.list_instances(include_unhealthy=True)
    assert len(rows) == 1
    assert rows[0].instance_id == "sbx-local"
    assert rows[0].user == "u1"
    await client.close()
