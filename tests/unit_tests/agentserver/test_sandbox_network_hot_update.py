"""Owned-service receipt verification and strict fallback for network-only apply."""
# Test-only inspection of runner ownership/fingerprint state.
# pylint: disable=protected-access

import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from jiuwenswarm.server import sandbox_config_rpc
from jiuwenswarm.server.sandbox import jiuwenbox_runner as runners


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response_kind", ["applied", "old_service", "mismatch", "changed_file", "transport"]
)
async def test_runner_updates_fingerprint_only_after_matching_receipt(
    tmp_path, monkeypatch, response_kind
):
    from openjiuwen.extensions.sys_operation.sandbox.providers import jiuwenbox as box

    path = tmp_path / "policy.yaml"
    path.write_text("new network policy", encoding="utf-8")
    fingerprint = hashlib.sha256(path.read_bytes()).hexdigest()
    runner = runners.JiuwenBoxRunner()
    runner.process = SimpleNamespace(returncode=None)
    runner.owns_process = True
    runner.spawned_policy_path = path
    runner._spawned_policy_fingerprint = "previous"
    runner._startup_extra_env = {"JIUWENBOX_API_TOKEN": "scoped-test-token"}
    monkeypatch.setattr(runners, "sys", SimpleNamespace(platform="win32"))
    requests = []

    def reply(request):
        requests.append(request)
        assert request.headers["Authorization"] == "Bearer scoped-test-token"
        assert request.url.path == "/api/v1/network/reload"
        if response_kind == "transport":
            raise httpx.ReadTimeout("no confirmation")
        if response_kind == "old_service":
            return httpx.Response(404)
        if response_kind == "changed_file":
            path.write_text("a newer update", encoding="utf-8")
        return httpx.Response(
            200,
            json={
                "status": "applied",
                "fingerprint": ("wrong" if response_kind == "mismatch" else fingerprint),
                "closed_connections": 2,
            },
        )

    def client(url, **kwargs):
        return httpx.Client(
            base_url=url,
            transport=httpx.MockTransport(reply),
            headers={"Authorization": f"Bearer {kwargs['api_token']}"},
        )

    monkeypatch.setattr(box, "build_jiuwenbox_http_client", client)
    assert await runner.try_reload_network_policy(path) == (response_kind == "applied")
    assert len(requests) == 1
    assert runner._spawned_policy_fingerprint == (
        fingerprint if response_kind == "applied" else "previous"
    )
    assert runner.owns_process and runner.process.returncode is None


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,platform", [("external", "win32"), ("internal", "linux")])
async def test_non_owned_or_non_windows_service_uses_existing_rebuild(monkeypatch, mode, platform):
    import sys
    from jiuwenswarm.common import config

    monkeypatch.setattr(config, "get_sandbox_startup_mode", lambda: mode)
    # Function-local platform import; restore immediately after this awaited call.
    monkeypatch.setattr(sys, "platform", platform)
    assert await sandbox_config_rpc.try_apply_sandbox_network_policy() is False


@pytest.mark.asyncio
async def test_saved_rules_are_rendered_before_reload(monkeypatch, tmp_path):
    from jiuwenswarm.common import config, net_guard_config

    monkeypatch.setattr(config, "get_sandbox_startup_mode", lambda: "internal")
    events = []
    path = tmp_path / "policy.yaml"

    async def reload_policy(received):
        assert received == path and events == ["render"]
        return True

    runner = SimpleNamespace(
        owns_process=True,
        process=object(),
        spawned_policy_path=path,
        try_reload_network_policy=AsyncMock(side_effect=reload_policy),
    )
    monkeypatch.setattr(runners.JiuwenBoxRunner, "instance", lambda: runner)
    monkeypatch.setattr(
        net_guard_config, "render_saved_sandbox_urls", lambda received: events.append("render")
    )
    import sys

    monkeypatch.setattr(sys, "platform", "win32")
    assert await sandbox_config_rpc.try_apply_sandbox_network_policy() is True
