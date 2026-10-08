# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
import asyncio
import io
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from openjiuwen.core.common.exception.codes import StatusCode
from jiuwenswarm.agents.harness.common.tools import send_file_to_user as sfu
from jiuwenswarm.agents.harness.common.tools import outbound_file_access as access
from jiuwenswarm.agents.harness.common.tools.xiaoyi_phone_tools import save_tools as save


def operation(content=b"sandbox bytes", error=None):
    result = SimpleNamespace(code=StatusCode.SUCCESS.code, message="ok",
                             data=SimpleNamespace(content=content))
    reader = AsyncMock(return_value=result, side_effect=error)
    return SimpleNamespace(fs=lambda: SimpleNamespace(read_file=reader)), reader


@pytest.fixture
def delivery(monkeypatch, tmp_path):
    monkeypatch.setattr("jiuwenswarm.server.runtime.session.session_history.get_agent_sessions_dir",
                        lambda: tmp_path / "sessions")
    sfu._SENT_FILE_PATHS_BY_SESSION.clear()
    server = SimpleNamespace(send_push=AsyncMock())
    monkeypatch.setattr("jiuwenswarm.server.agent_ws_server.AgentWebSocketServer.get_instance", lambda: server)
    history = Mock()
    monkeypatch.setattr("jiuwenswarm.server.runtime.session.session_history.append_history_record", history)
    monkeypatch.setenv("JIUWENSWARM_FILE_DOWNLOAD_SECRET", "test-only-secret-" * 4)
    return server, history


@pytest.mark.asyncio
@pytest.mark.parametrize("host_exists", [True, False])
async def test_send_delivers_backend_bytes(delivery, tmp_path, host_exists):
    from jiuwenswarm.agents.harness.common.tools.web_file_download import validate_file_download_token
    from jiuwenswarm.channels.web.app_web import _SpaStaticHandler

    source = tmp_path / "report.pdf"
    if host_exists:
        source.write_bytes(b"different host content")
    op, reader = operation()
    toolkit = sfu.SendFileToolkit("r", "s", "web", operation_provider=lambda: op)
    result = await toolkit.get_tools()[0].invoke({"abs_file_path_list": [str(source)]})
    assert result == "Sent 1 files"
    reader.assert_awaited_once_with(str(source.resolve()), mode="bytes")
    entry = delivery[0].send_push.await_args.args[0]["payload"]["files"][0]
    assert entry["path"] != str(source)
    token_path = validate_file_download_token(entry["download_token"])["path"]
    assert token_path == entry["path"]
    source.write_bytes(b"source changed after send")
    assert Path(token_path).read_bytes() == b"sandbox bytes"
    assert entry["size"] == len(b"sandbox bytes")
    assert entry["name"] == source.name
    assert entry["artifact_id"] == sfu.artifact_id_for_path(str(source))
    handler = SimpleNamespace(
        command="GET", headers={}, wfile=io.BytesIO(), connection=Mock(),
        send_response=Mock(), send_header=Mock(), end_headers=Mock(),
        _write_json=Mock(), log_error=Mock(),
    )
    _SpaStaticHandler._handle_file_download(handler, {"token": entry["download_token"]})
    handler.send_response.assert_called_once_with(200)
    assert handler.wfile.getvalue() == b"sandbox bytes"


@pytest.mark.asyncio
async def test_send_preserves_missing_file_and_relative_path_behavior(delivery, tmp_path, monkeypatch):
    process = tmp_path / "process"
    agent = tmp_path / "agent"
    process.mkdir()
    agent.mkdir()
    monkeypatch.chdir(process)
    monkeypatch.setattr(
        "openjiuwen.harness.security.permission_engine.fileguard.outbound_paths.get_cwd", lambda: str(agent),
    )
    (agent / "report.pdf").write_bytes(b"report")
    op, reader = operation()
    reader.side_effect = [reader.return_value, FileNotFoundError("missing.pdf")]
    toolkit = sfu.SendFileToolkit("r", "s", "web", operation_provider=lambda: op)
    result = await toolkit.send_file(["report.pdf", "missing.pdf"])
    assert "Sent 1 files" in result
    assert "missing.pdf" in result
    assert [call.args[0] for call in reader.await_args_list] == [
        str(agent / "report.pdf"), str(agent / "missing.pdf"),
    ]
    entry = delivery[0].send_push.await_args.args[0]["payload"]["files"][0]
    assert Path(entry["path"]).read_bytes() == b"sandbox bytes"
    assert entry["artifact_id"] == sfu.artifact_id_for_path(str(agent / "report.pdf"))


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["exception", "result", "unbound"])
async def test_send_failure_never_publishes(delivery, tmp_path, failure):
    (tmp_path / "secret.pdf").write_bytes(b"secret")
    op, reader = operation(error=PermissionError("sandbox denied") if failure == "exception" else None)
    if failure == "result":
        reader.return_value = SimpleNamespace(code=-1, message="denied", data=None)
    if failure == "unbound":
        op = None
    result = await sfu.SendFileToolkit("r", "s", "web", operation_provider=lambda: op).send_file(
        str(tmp_path / "secret.pdf"))
    assert result.startswith("success=False")
    delivery[0].send_push.assert_not_awaited()
    delivery[1].assert_not_called()


@pytest.mark.asyncio
async def test_batch_read_failure_does_not_send_partial_files(delivery, tmp_path):
    (tmp_path / "a.pdf").write_bytes(b"a")
    (tmp_path / "secret.pdf").write_bytes(b"secret")
    op, reader = operation()
    good = reader.return_value
    reader.side_effect = [good, PermissionError("denied")]
    result = await sfu.SendFileToolkit("r", "s", "web", operation_provider=lambda: op).send_file(
        [str(tmp_path / "a.pdf"), str(tmp_path / "secret.pdf")])
    assert result.startswith("success=False")
    delivery[0].send_push.assert_not_awaited()


@pytest.fixture
def phone(monkeypatch):
    upload = AsyncMock(return_value="https://example.com/approved.pdf")
    device = AsyncMock(return_value={})
    monkeypatch.setattr(save, "_get_obs_config", lambda: object())
    monkeypatch.setattr(save, "upload_local_file_public_url", upload)
    monkeypatch.setattr(save, "execute_device_command", device)
    return upload, device


@pytest.mark.asyncio
@pytest.mark.parametrize("template,extra", [
    (save.save_media_to_gallery, {}),
    (save.save_file_to_file_manager, {"file_name": "report", "suffix": "pdf"}),
])
@pytest.mark.parametrize("mode", ["allow", "deny", "unbound", "url"])
async def test_save_source_routing(phone, tmp_path, template, extra, mode):
    op, reader = operation(error=PermissionError("sandbox denied") if mode == "deny" else None)
    if mode == "unbound":
        op = None
    bound = save.bind_save_tool(template, lambda: op)
    source = "https://example.com/source.pdf" if mode == "url" else str(tmp_path / "source.pdf")
    inputs = {"url": source, **extra}
    if mode in {"deny", "unbound"}:
        with pytest.raises(RuntimeError):
            await bound.invoke(inputs)
        phone[0].assert_not_awaited()
        phone[1].assert_not_awaited()
    else:
        await bound.invoke(inputs)
        phone[1].assert_awaited_once()
        if mode == "url":
            reader.assert_not_awaited()
            phone[0].assert_not_awaited()
        else:
            reader.assert_awaited_once_with(source, mode="bytes")
            assert phone[0].await_args.kwargs["file_content"] == b"sandbox bytes"
    assert save._SAVE_OPERATION.get() is None
    assert bound.card.stateless is False


@pytest.mark.asyncio
@pytest.mark.parametrize("template,extra", [
    (save.save_media_to_gallery, {}),
    (save.save_file_to_file_manager, {"file_name": "report", "suffix": "pdf"}),
])
async def test_save_relative_path_uses_agent_cwd(phone, tmp_path, monkeypatch, template, extra):
    process = tmp_path / "process"
    process.mkdir()
    monkeypatch.chdir(process)
    monkeypatch.setattr(
        "openjiuwen.harness.security.permission_engine.fileguard.outbound_paths.get_cwd",
        lambda: str(tmp_path / "agent" / "work"),
    )
    op, reader = operation()
    await save.bind_save_tool(template, lambda: op).invoke({"url": "../report.pdf", **extra})
    source = str(tmp_path / "agent" / "report.pdf")
    reader.assert_awaited_once_with(source, mode="bytes")
    assert phone[0].await_args.args[2] == source
    assert phone[0].await_args.kwargs["file_content"] == b"sandbox bytes"


@pytest.mark.asyncio
async def test_save_concurrent_agents_keep_separate_operations(phone, tmp_path):
    op_a, read_a = operation(b"a")
    op_b, read_b = operation(b"b")
    first = save.bind_save_tool(save.save_media_to_gallery, lambda: op_a)
    second = save.bind_save_tool(save.save_media_to_gallery, lambda: op_b)
    await asyncio.gather(first.invoke({"url": str(tmp_path / "a.png")}),
                         second.invoke({"url": str(tmp_path / "b.png")}))
    assert {call.kwargs["file_content"] for call in phone[0].await_args_list} == {b"a", b"b"}
    read_a.assert_awaited_once()
    read_b.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("denied", [False, True])
async def test_reader_uses_real_jiuwenbox_fs_provider(tmp_path, denied):
    from openjiuwen.extensions.sys_operation.sandbox.providers import jiuwenbox as jb

    provider = object.__new__(jb.JiuwenBoxFSProvider)
    provider._get_sandbox_id = Mock(return_value="sandbox-a")
    client = Mock()
    client.download_bytes.return_value = b"from sandbox"
    if denied:
        client.download_bytes.side_effect = PermissionError("sandbox denied")
    provider._get_client = Mock(return_value=client)
    source = str(tmp_path / "file.bin")
    op = SimpleNamespace(fs=lambda: provider)
    if denied:
        with pytest.raises(RuntimeError, match="sandbox denied"):
            await access.read_outbound_file(source, op)
    else:
        assert await access.read_outbound_file(source, op) == b"from sandbox"
    client.download_bytes.assert_called_once_with("sandbox-a", source)


@pytest.mark.asyncio
async def test_team_providers_bind_lazily_to_member_operation(phone, delivery, tmp_path):
    from jiuwenswarm.agents.swarm.context import SwarmBuildContext
    from jiuwenswarm.agents.swarm.providers.runtime_tools import build_send_file_tools
    from jiuwenswarm.agents.swarm.providers.tools import _build_xiaoyi_phone_tools

    ctx = SwarmBuildContext(request_id="r", session_id="s", channel_id="web",
                            config={"channels": {"xiaoyi": {"phone_tools_enabled": True}}})
    send = build_send_file_tools({}, ctx)[0]
    saves = [t for t in _build_xiaoyi_phone_tools(ctx) if t.card.name.startswith("save_")]
    ctx.extras["sys_operation"], reader = operation()
    (tmp_path / "a.pdf").write_bytes(b"a")
    await send.invoke({"abs_file_path_list": str(tmp_path / "a.pdf")})
    for tool in saves:
        args = {"url": str(tmp_path / "a.pdf")}
        if tool.card.name == "save_file_to_file_manager":
            args.update(file_name="a", suffix="pdf")
        await tool.invoke(args)
    assert len(saves) == 2
    assert reader.await_count == 3


@pytest.mark.asyncio
async def test_upload_supplied_empty_bytes_never_opens_source(monkeypatch):
    from jiuwenswarm.agents.harness.common.tools.xiaoyi_phone_tools import file_upload_helpers as upload

    control = AsyncMock(side_effect=[
        {"code": "0", "objectId": "o", "draftId": "d", "uploadInfos": [{"url": "https://example.com/upload"}]},
        {"fileDetailInfo": {"url": "https://example.com/result"}},
    ])
    monkeypatch.setattr(upload, "_post_control_plane", control)
    monkeypatch.setattr(upload, "open", Mock(side_effect=AssertionError("no source reopen")), raising=False)
    response = AsyncMock()
    response.__aenter__.return_value = SimpleNamespace(ok=True)
    session = SimpleNamespace(request=Mock(return_value=response))
    result = await upload.upload_local_file_public_url(
        session, upload.XiaoyiObsUploadConfig("https://example.com", "test", "test"),
        "nonexistent.pdf", file_content=b"",
    )
    assert result == "https://example.com/result"
    assert session.request.call_args.kwargs["data"] == b""
