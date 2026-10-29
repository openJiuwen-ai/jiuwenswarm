"""Network reload preserves instances and closes old real loopback tunnels."""
# Tests construct a manager without spawning Windows sandbox processes.
# pylint: disable=protected-access

import asyncio
import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio

from jiuwenbox.models.policy import NetworkRulePolicy, SecurityPolicy
from jiuwenbox.server.network_policy_reload import read_network_candidate
from jiuwenbox.server.sandbox_manager import SandboxManager
from jiuwenbox.supervisor.win_proxy import WindowsProxyController


@pytest_asyncio.fixture
async def proxy():
    controller = WindowsProxyController(NetworkRulePolicy(default="allow"))
    remote_tasks = set()

    async def echo(reader, writer):
        task = asyncio.current_task()
        remote_tasks.add(task)
        try:
            while data := await reader.read(4096):
                writer.write(data)
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            remote_tasks.discard(task)

    target = await asyncio.start_server(echo, "127.0.0.1", 0)
    server = await asyncio.start_server(controller.handle_client, "127.0.0.1", 0)
    controller.listening = True
    try:
        yield controller, server.sockets[0].getsockname()[1], target.sockets[0].getsockname()[1]
    finally:
        server.close()
        target.close()
        await server.wait_closed()
        await target.wait_closed()
        await controller.replace_policy(NetworkRulePolicy(default="deny"))
        await asyncio.gather(*remote_tasks, return_exceptions=True)


async def connect(proxy_port, target_port, protocol, *, allowed=True):
    reader, writer = await asyncio.open_connection("127.0.0.1", proxy_port)
    if protocol == "socks5":
        writer.write(b"\x05\x01\x00")
        await writer.drain()
        assert await asyncio.wait_for(reader.readexactly(2), 2) == b"\x05\x00"
        writer.write(b"\x05\x01\x00\x01\x7f\x00\x00\x01" + target_port.to_bytes(2, "big"))
        await writer.drain()
        reply = await asyncio.wait_for(reader.readexactly(10), 2)
        assert reply[1] == (0 if allowed else 2)
    else:
        line = (
            f"CONNECT 127.0.0.1:{target_port} HTTP/1.1"
            if protocol == "connect"
            else f"GET http://127.0.0.1:{target_port}/probe HTTP/1.1"
        )
        writer.write((line + "\r\n\r\n").encode())
        await writer.drain()
        reply = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 2)
        if not allowed:
            assert b"403 Forbidden" in reply
        elif protocol == "connect":
            assert b"200 Connection Established" in reply
        else:
            assert reply.startswith(b"GET /probe HTTP/1.1")
    return reader, writer


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["connect", "forward", "socks5"])
async def test_tightening_closes_existing_tunnel_and_blocks_new_connections(proxy, protocol):
    controller, proxy_port, target_port = proxy
    reader, writer = await connect(proxy_port, target_port, protocol)
    try:
        writer.write(b"before")
        await writer.drain()
        assert await asyncio.wait_for(reader.readexactly(6), 2) == b"before"
        assert await controller.replace_policy(NetworkRulePolicy(default="deny")) == 1
        try:
            assert await asyncio.wait_for(reader.read(1), 2) == b""
        except ConnectionResetError:
            pass
        _, denied = await connect(proxy_port, target_port, protocol, allowed=False)
        denied.close()
        await denied.wait_closed()
        await controller.replace_policy(NetworkRulePolicy(default="allow"))
        _, reopened = await connect(proxy_port, target_port, protocol)
        reopened.close()
        await reopened.wait_closed()
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except ConnectionResetError:
            pass


@pytest.mark.asyncio
async def test_update_drains_incomplete_handshake(proxy):
    controller, proxy_port, _ = proxy
    reader, writer = await asyncio.open_connection("127.0.0.1", proxy_port)
    writer.write(b"C")
    await writer.drain()
    for _ in range(20):
        if controller._clients:
            break
        await asyncio.sleep(0)
    assert await controller.replace_policy(NetworkRulePolicy(default="deny")) == 1
    assert not controller._clients
    try:
        assert await asyncio.wait_for(reader.read(1), 2) == b""
    except ConnectionResetError:
        pass
    writer.close()
    try:
        await writer.wait_closed()
    except ConnectionResetError:
        pass


@pytest.fixture
def manager(tmp_path):
    original = SecurityPolicy.model_validate(
        {
            "version": 1,
            "windows": {
                "network": {
                    "egress": {
                        "default": "allow",
                        "allowed_ports": [443],
                    }
                }
            },
        }
    )
    candidate = original.model_copy(deep=True)
    candidate.windows.network.egress = NetworkRulePolicy(
        default="deny", blocked_domains=["blocked.test"]
    )
    path = tmp_path / "policy.yaml"
    path.write_text("{}", encoding="utf-8")
    mgr = SandboxManager.__new__(SandboxManager)
    mgr._lock = asyncio.Lock()
    mgr.policy = original.model_copy(deep=True)
    mgr.policy.timeout.idle_timeout = 900  # Runtime override must survive reload.
    mgr._network_reload_policy = original
    mgr.policy_reader = SimpleNamespace(policy_path=path, load_policy=lambda: candidate)
    instance = original.model_copy(deep=True)
    instance.windows.filesystem.allow_read = ["instance-workspace"]
    mgr._policies = {"retained": instance}
    mgr._sandboxes = {"retained": object()}
    controller = SimpleNamespace(listening=True, replace_policy=AsyncMock(return_value=3))
    fingerprint = hashlib.sha256(path.read_bytes()).hexdigest()
    return mgr, controller, candidate, fingerprint


@pytest.mark.asyncio
async def test_cancelled_request_still_finishes_connection_drain_and_policy_commit(
    manager, monkeypatch
):
    from jiuwenbox.server import app
    from jiuwenbox.server.routes.policy import ReloadNetworkRequest, reload_network

    mgr, controller, candidate, fingerprint = manager
    entered, finish = asyncio.Event(), asyncio.Event()
    workers = []

    async def drain(*_args):
        workers.append(asyncio.current_task())
        entered.set()
        await finish.wait()
        return 1

    controller.replace_policy.side_effect = drain
    monkeypatch.setattr(app, "get_manager", lambda: mgr)
    monkeypatch.setattr(app, "get_windows_proxy_controller", lambda: controller)
    request = asyncio.create_task(reload_network(ReloadNetworkRequest(fingerprint=fingerprint)))
    await entered.wait()
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    assert not workers[0].done()
    finish.set()
    receipt = await workers[0]
    assert receipt["status"] == "applied"
    assert mgr.policy.windows.network == candidate.windows.network


@pytest.mark.asyncio
async def test_reload_preserves_ids_acl_and_runtime_timeout_and_is_idempotent(manager):
    mgr, controller, candidate, fingerprint = manager
    refs = dict(mgr._sandboxes)
    receipt = await mgr.reload_windows_network(controller, fingerprint)
    assert receipt == {"status": "applied", "fingerprint": fingerprint, "closed_connections": 3}
    assert mgr._sandboxes == refs
    assert mgr._policies["retained"].windows.filesystem.allow_read == ["instance-workspace"]
    assert mgr.policy.timeout.idle_timeout == 900
    assert mgr._policies["retained"].windows.network == candidate.windows.network
    assert (await mgr.reload_windows_network(controller, fingerprint))["closed_connections"] == 0
    controller.replace_policy.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["files", "proxy_port", "mode", "fingerprint", "listener"])
async def test_unsupported_reload_does_not_mutate_policy(manager, change):
    mgr, controller, candidate, fingerprint = manager
    before = mgr.policy.model_copy(deep=True)
    if change == "files":
        candidate.windows.filesystem.allow_read = ["other"]
    elif change == "proxy_port":
        candidate.windows.proxy.port_range_start += 1
    elif change == "mode":
        candidate.windows.network.mode = "disabled"
    elif change == "fingerprint":
        fingerprint = "0" * 64
    else:
        controller.listening = False
    with pytest.raises(ValueError):
        await mgr.reload_windows_network(controller, fingerprint)
    assert mgr.policy == before
    controller.replace_policy.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("content", ["windows: [", "scalar", "[]", ""])
async def test_invalid_yaml_cannot_acknowledge_base_policy_fallback(manager, content):
    mgr, controller, _, _ = manager
    before = mgr.policy.model_copy(deep=True)
    path = mgr.policy_reader.policy_path
    path.write_text(content, encoding="utf-8")
    fingerprint = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(ValueError):
        await mgr.reload_windows_network(controller, fingerprint)
    assert mgr.policy == before
    controller.replace_policy.assert_not_awaited()


def test_real_reader_merges_network_copy_and_clears_inherited_allowances(tmp_path, monkeypatch):
    from jiuwenbox.server import policy_reader
    from jiuwenbox.server.policy_engine import PolicyEngine

    base = tmp_path / "base.yaml"
    base.write_text(
        "windows:\n  network:\n    egress:\n      default: allow\n"
        "      allowed_ports: [443]\n      allowed_domains: [inherited.test]\n",
        encoding="utf-8",
    )
    copy = tmp_path / "copy.yaml"
    copy.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(policy_reader, "base_policy_path", lambda: base)
    reader = policy_reader.PolicyReader(PolicyEngine(tmp_path / "policies"), copy)
    previous = reader.load_policy()
    copy.write_text(
        "windows:\n  network:\n    egress:\n      default: deny\n"
        "      replace_allowed: true\n      allowed_domains: [allowed.test]\n"
        "      allowed_ips: []\n      allowed_ports: []\n",
        encoding="utf-8",
    )
    candidate = read_network_candidate(
        reader,
        previous,
        hashlib.sha256(copy.read_bytes()).hexdigest(),
    )
    assert previous.windows.network.egress.allowed_ports == [443]
    assert candidate.windows.network.egress.allowed_ports == []
    assert candidate.windows.network.egress.allowed_domains == ["allowed.test"]
    assert candidate.windows.network.egress.default == "deny"
    assert candidate.windows.filesystem == previous.windows.filesystem


@pytest.mark.asyncio
async def test_reload_endpoint_uses_existing_auth_and_fixed_policy_file(manager, monkeypatch):
    from jiuwenbox.server import app

    mgr, controller, _, fingerprint = manager
    monkeypatch.setenv("JIUWENBOX_API_TOKEN", "test-token")
    monkeypatch.setattr(app, "get_manager", lambda: mgr)
    monkeypatch.setattr(app, "get_windows_proxy_controller", lambda: controller)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app.create_app()), base_url="http://localhost"
    ) as client:
        denied = await client.put("/api/v1/network/reload", json={"fingerprint": fingerprint})
        assert denied.status_code == 401
        controller.replace_policy.assert_not_awaited()
        reply = await client.put(
            "/api/v1/network/reload",
            json={"fingerprint": fingerprint},
            headers={"Authorization": "Bearer test-token"},
        )
        assert reply.status_code == 200
        assert reply.json()["fingerprint"] == fingerprint


@pytest.mark.asyncio
async def test_reload_http_receipt_waits_for_real_tunnel_revocation(manager, proxy, monkeypatch):
    from jiuwenbox.server import app

    mgr, _, _, fingerprint = manager
    controller, proxy_port, target_port = proxy
    references = dict(mgr._sandboxes)
    monkeypatch.setenv("JIUWENBOX_API_TOKEN", "test-token")
    monkeypatch.setattr(app, "get_manager", lambda: mgr)
    monkeypatch.setattr(app, "get_windows_proxy_controller", lambda: controller)
    reader, writer = await connect(proxy_port, target_port, "connect")
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app.create_app()),
            base_url="http://localhost",
            headers={"Authorization": "Bearer test-token"},
        ) as client:
            invalid = await client.put(
                "/api/v1/network/reload",
                json={
                    "fingerprint": fingerprint,
                    "policy_path": "not-accepted",
                },
            )
            assert invalid.status_code == 422
            reply = await client.put("/api/v1/network/reload", json={"fingerprint": fingerprint})
            assert reply.status_code == 200
            assert reply.json()["closed_connections"] == 1
        assert mgr._sandboxes == references
        assert not controller._clients
        try:
            assert await asyncio.wait_for(reader.read(1), 2) == b""
        except ConnectionResetError:
            pass
        _, denied = await connect(proxy_port, target_port, "connect", allowed=False)
        denied.close()
        await denied.wait_closed()
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except ConnectionResetError:
            pass
