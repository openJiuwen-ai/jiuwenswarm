# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Exact-target starts, trustworthy receipts, and local CLI isolation."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.channels.cli import presentation as cli
from jiuwenswarm.common import extension_cli
from jiuwenswarm.gateway.extensions_v2.management.local_cli import start_local_cli
from jiuwenswarm.gateway.extensions_v2.webui.models import RenderNode, RenderTarget, UiRenderInput
from jiuwenswarm.gateway.extensions_v2.webui.presentation_control import PresentationControl
from jiuwenswarm.gateway.extensions_v2.webui.registry import ActiveRenderRegistry, RenderInstance


def setup_control(timeout=0.1):
    target = RenderTarget("", "application.presentation", "test.forms", "bee", "application", "window-1")
    registry = ActiveRenderRegistry()
    record = RenderInstance(target, revision=1)
    registry.commit(record, UiRenderInput(target, RenderNode("ApplicationSurface", {"title": {"zh": "蜜蜂", "en": "Bee"}})))
    registry._instances[target.key] = record
    activation = object()
    send = AsyncMock(return_value=True)
    runtime = SimpleNamespace(registry=registry, current_activation=lambda package_id: activation,
                              channel=SimpleNamespace(ws_by_id=lambda key: object() if key == "window-1" else None),
                              webui=SimpleNamespace(transport=SimpleNamespace(send_presentation_start=send)))
    control = PresentationControl(runtime, timeout=timeout)
    runtime.browser = SimpleNamespace(presentations=control)
    params = {"connection_id": "window-1", "package_id": "test.forms", "instance_key": "bee", "revision": 2}
    return control, runtime, record, send, params


@pytest.mark.asyncio
async def test_start_only_succeeds_after_same_connection_receipt():
    control, _, _, send, params = setup_control()
    task = asyncio.create_task(control.start(params))
    await asyncio.sleep(0)
    assert not task.done()
    connection, frame = send.call_args.args
    assert connection == "window-1" and "src" not in frame and frame["deadline"] > 0
    assert not control.complete("other-window", {"request_id": frame["request_id"], "code": "STARTED"})["accepted"]
    assert not task.done()
    assert control.complete(connection, {"request_id": frame["request_id"], "code": "STARTED"})["accepted"]
    assert (await task) == {"ok": True, "payload": {"status": "started", "code": "STARTED"}}
    assert not control.pending
    assert not control.complete(connection, {"request_id": frame["request_id"], "code": "STARTED"})["accepted"]


@pytest.mark.asyncio
async def test_start_rejects_outdated_or_incomplete_targets_without_delivery():
    control, _, _, send, params = setup_control()
    for changed in [{**params, "revision": 1}, {**params, "connection_id": "other"},
                    {**params, "instance_key": "capsule"}, {**params, "revision": True},
                    {**params, "extra": "value"}]:
        assert not (await control.start(changed))["ok"]
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_one_start_per_connection_and_changed_revision_is_unknown():
    control, _, record, send, params = setup_control()
    task = asyncio.create_task(control.start(params))
    await asyncio.sleep(0)
    assert (await control.start(params))["payload"]["code"] == "BUSY"
    record.revision += 1
    frame = send.call_args.args[1]
    control.complete("window-1", {"request_id": frame["request_id"], "code": "STARTED"})
    assert (await task)["payload"]["status"] == "unknown"
    assert not control.pending


@pytest.mark.asyncio
@pytest.mark.parametrize("invalidated", ["connection", "package", "shutdown"])
async def test_lifecycle_changes_release_waiters_with_unknown_outcome(invalidated):
    control, _, _, _, params = setup_control()
    task = asyncio.create_task(control.start(params))
    await asyncio.sleep(0)
    control.invalidate(**({"connection_id": "window-1"} if invalidated == "connection"
                         else {"package_id": "test.forms"} if invalidated == "package" else {}))
    assert (await task)["payload"]["status"] == "unknown"
    assert not control.pending


@pytest.mark.asyncio
async def test_timeout_and_failed_delivery_do_not_retry():
    control, _, _, send, params = setup_control(timeout=0.001)
    assert (await control.start(params))["payload"]["status"] == "unknown"
    send.assert_awaited_once()
    send.reset_mock(return_value=True)
    send.return_value = False
    assert (await control.start(params))["payload"]["status"] == "unknown"
    send.assert_awaited_once()
    assert not control.pending


@pytest.mark.asyncio
async def test_browser_rejection_is_a_known_failure():
    control, _, _, send, params = setup_control()
    task = asyncio.create_task(control.start(params))
    await asyncio.sleep(0)
    control.complete("window-1", {"request_id": send.call_args.args[1]["request_id"], "code": "DESKTOP_REQUIRED"})
    assert (await task)["payload"] == {"status": "rejected", "code": "DESKTOP_REQUIRED"}


@pytest.mark.asyncio
async def test_cli_uses_gateway_context_and_preserves_unknown(tmp_path, monkeypatch):
    control, runtime, _, send, params = setup_control(timeout=0.001)
    monkeypatch.setenv("GATEWAY_PORT", "19001")
    monkeypatch.setattr("jiuwenswarm.common.utils.get_user_workspace_dir", lambda: tmp_path)
    service = await start_local_cli(runtime)
    try:
        result, status = await cli.execute(cli.parser().parse_args(["query", "test.forms", "--json"]))
        assert status == 0 and result["result"]["candidates"] == [{**params, "title": {"zh": "蜜蜂", "en": "Bee"}}]
        result, status = await cli.execute(cli.parser().parse_args([
            "start", "test.forms", "bee", "--connection-id", "window-1", "--revision", "2", "--json",
        ]))
        assert status == 3 and result["status"] == "UNKNOWN"
        send.assert_awaited_once()
    finally:
        await service.close()
    assert not control.pending


@pytest.mark.asyncio
async def test_default_local_control_does_not_accept_presentation_methods(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SERVER_PORT", "18092")
    monkeypatch.setattr("jiuwenswarm.common.utils.get_user_workspace_dir", lambda: tmp_path)
    handle = AsyncMock()
    service = extension_cli.LocalControl("agent_server", handle)
    await service.start()
    try:
        response = await extension_cli.request(extension_cli.read_context("agent_server"), "presentation.query", {}, 1)
        assert not response["ok"]
        handle.assert_not_awaited()
    finally:
        await service.close()
