"""Offline acceptance with actual permission evaluation and controlled transport."""

import asyncio
import json
from copy import deepcopy
from pathlib import Path

import httpx
import pytest
import yaml

import jiuwenswarm
from jiuwenswarm.common.behavior_security import BehaviorSecurityBridge, runtime_permission_config
from openjiuwen.harness.security.permission_engine.core import PermissionEngine


def desktop_config():
    return {
        "permissions": {"enabled": True, "package_builtin_rules": False, "tools": {"bash": "allow"}},
        "xiaoyi_work_security": {"cloud_authorization": {"enabled": True, "timeout_ms": 100}},
    }


def tool_event(event_id="offline-case"):
    return {
        "version": 1, "eventId": event_id, "stage": "tool.before", "sessionId": "offline-session",
        "tool": {"id": event_id, "name": "bash", "arguments": {"command": "printf TEST_OFFLINE"}},
    }


@pytest.fixture(autouse=True)
def desktop_host(monkeypatch):
    monkeypatch.setenv("CLAW_BEHAVIOR_SECURITY", "1")
    monkeypatch.setenv("CLAW_SKILL_TOKEN", "test-token")


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [
    "timeout", "connection", "http-503", "invalid-json", "non-object", "missing-decision", "invalid-decision",
])
async def test_transport_failure_falls_back_to_ask_and_is_not_retried(failure):
    events = []

    async def handler(request):
        events.append(json.loads(request.content))
        if failure == "timeout":
            await asyncio.Event().wait()
        if failure == "connection":
            raise httpx.ConnectError("simulated transport failure", request=request)
        if failure == "http-503":
            return httpx.Response(503, text="unavailable")
        if failure == "invalid-json":
            return httpx.Response(200, content=b"not-json")
        body = {"non-object": [], "missing-decision": {}, "invalid-decision": {"decision": "bad"}}[failure]
        return httpx.Response(200, json=body)

    cfg = desktop_config()
    original = deepcopy(cfg)
    engine = PermissionEngine(runtime_permission_config(cfg))
    event = tool_event()
    local = await engine.check_permission("bash", event["tool"]["arguments"])
    assert local.is_undetermined
    bridge = BehaviorSecurityBridge(cfg, transport=httpx.MockTransport(handler))
    try:
        result = await asyncio.wait_for(bridge.decide(event, local), 2)
        assert result.needs_approval and not result.is_allowed
        assert await bridge.decide(event, local) is result
        assert len(events) == 1 and events[0]["mode"] == "authorize"
        assert cfg == original
    finally:
        await bridge.close()


@pytest.mark.asyncio
async def test_cloud_accept_does_not_create_a_remembered_local_grant():
    events = []

    async def handler(request):
        events.append(json.loads(request.content))
        return httpx.Response(200, json={"decision": "allow"})

    cfg = desktop_config()
    original = deepcopy(cfg)
    bridge = BehaviorSecurityBridge(cfg, transport=httpx.MockTransport(handler))
    try:
        for event_id in ("first-call", "new-call"):
            event = tool_event(event_id)
            local = await PermissionEngine(runtime_permission_config(cfg)).check_permission(
                "bash", event["tool"]["arguments"],
            )
            assert local.is_undetermined
            assert (await bridge.decide(event, local)).is_allowed
        assert [event["mode"] for event in events] == ["authorize", "authorize"]
        assert cfg == original
    finally:
        await bridge.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("tool,args,expected", [
    ("bash", {"command": "printf CLOUD_MERGE_1015_A"}, "undetermined"),
    ("bash", {"command": "echo CLOUD_MERGE_1015_B"}, "allow"),
    ("bash", {"command": "echo CLOUD_MERGE_1015_C && printf CLOUD_MERGE_1015_D"}, "undetermined"),
    ("cron", {"action": "list"}, "allow"),
])
async def test_shipped_config_matches_the_manual_acceptance_cases(tool, args, expected):
    template = Path(jiuwenswarm.__file__).parent / "resources" / "config.yaml"
    cfg = yaml.safe_load(template.read_text(encoding="utf-8"))
    assert cfg["xiaoyi_work_security"]["cloud_authorization"]["enabled"] is True
    result = await PermissionEngine(runtime_permission_config(cfg)).check_permission(tool, args)
    assert result.permission.value == expected
