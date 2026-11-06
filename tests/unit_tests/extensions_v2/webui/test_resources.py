# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""File publication, caller isolation, revocation and HTTP/E2A regression tests."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from openjiuwen.core.events import PackageMetadata, SessionContext

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.common.webui_resources import MAX_RESOURCE_BYTES, decode_resource_route, resource_path
from jiuwenswarm.extensions_v2.webui.adapter import WebUIDispatchAdapter
from jiuwenswarm.extensions_v2.webui.call_context import WebUIConnectionContext, bind_connection
from jiuwenswarm.extensions_v2.webui.contributions import WebUICatalogService
from jiuwenswarm.extensions_v2.webui.resources import WebUIResourceService, complete_before_cancel


def _service(tmp_path):
    record = SimpleNamespace(package_id="pkg.demo", root=tmp_path, version="1.0.0", generation=1)
    records = [record]
    service = WebUIResourceService(WebUICatalogService(lambda: records))
    ops = SimpleNamespace(package_metadata=PackageMetadata("pkg.demo", "1.0.0", tmp_path),
                          session_context=SessionContext("s1", metadata={"user_id": "u1"}))
    return service, ops, record, records


async def _publish(service, ops, files, *, previous=""):
    """Act as a plugin: write files, then request an entry URL."""
    directory = await service.create_directory(ops, previous=previous)
    try:
        async with directory:
            for name, value in files.items():
                target = directory.path / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(value.encode("utf-8") if isinstance(value, str) else value)
        entry = next(iter(files), "page.html")
        url = await service.asset_url(ops, entry, resource_id=directory.resource_id)
    except BaseException:
        await service.release(ops, directory.resource_id)
        raise
    base = url.rsplit("/", len(entry.split("/")))[0] + "/"
    return SimpleNamespace(resource_id=directory.resource_id, path=directory.path, base_url=base, url=lambda path: base + path)


def _parts(url):
    route, resource_id, path = urlsplit(url).path.split("/")[3:]
    return resource_id, path, route


async def _cancel_repeatedly(task, count):
    loop = asyncio.get_running_loop()
    for index in range(count):
        delivered = loop.create_future()
        task.cancel(f"cancel-{index + 1}")
        # The task handles this cancellation before the acknowledgement wakes us.
        loop.call_soon(delivered.set_result, None)
        await delivered


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_count", [0, 1, 3])
@pytest.mark.parametrize("fail", [False, True])
async def test_complete_before_cancel_finishes_once_and_preserves_failure(cancel_count, fail):
    started, finish, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls = []
    failure = OSError("write failed")

    async def operation():
        calls.append(True)
        started.set()
        await finish.wait()
        finished.set()
        if fail:
            raise failure
        return "written"

    task = asyncio.create_task(complete_before_cancel(operation()))
    try:
        await started.wait()
        await _cancel_repeatedly(task, cancel_count)
        assert not task.done() and not finished.is_set()
    finally:
        finish.set()
    if cancel_count:
        with pytest.raises(asyncio.CancelledError, match="cancel-1") as caught:
            await task
        assert caught.value.__cause__ is (failure if fail else None)
        assert task.cancelled()
    elif fail:
        with pytest.raises(OSError) as caught:
            await task
        assert caught.value is failure
    else:
        assert await task == "written"
    assert finished.is_set() and calls == [True]


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_count", [0, 1])
async def test_complete_before_cancel_stops_when_operation_itself_is_cancelled(cancel_count):
    started, finish = asyncio.Event(), asyncio.Event()

    async def operation():
        started.set()
        await finish.wait()
        raise asyncio.CancelledError("operation cancelled")

    task = asyncio.create_task(complete_before_cancel(operation()))
    await started.wait()
    await _cancel_repeatedly(task, cancel_count)
    finish.set()
    with pytest.raises(asyncio.CancelledError) as caught:
        await asyncio.wait_for(task, 5)
    if cancel_count:
        assert str(caught.value) == "cancel-1"
        assert isinstance(caught.value.__cause__, asyncio.CancelledError)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["release", "drop", "shutdown"])
async def test_cleanup_cannot_be_cancelled_while_waiting_for_its_lock(tmp_path, action):
    service, ops, _, _ = _service(tmp_path)
    published = await _publish(service, ops, {"page.html": "ready"})
    if action == "release":
        operation = service.release(ops, published.resource_id)
    elif action == "drop":
        operation = service.drop(session_id="s1")
    else:
        operation = service.shutdown()
    try:
        async with service._lock:
            task = asyncio.create_task(operation)
            started = asyncio.Event()
            asyncio.get_running_loop().call_soon(started.set)
            await started.wait()
            await _cancel_repeatedly(task, 3)
            pending = not task.done()
        with pytest.raises(asyncio.CancelledError, match="cancel-1"):
            await task
        assert pending and not service._sets and not published.path.exists()
    finally:
        await service.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["release", "drop", "shutdown"])
@pytest.mark.parametrize("cancel_count", [1, 3])
async def test_cleanup_finishes_before_propagating_cancellation(tmp_path, monkeypatch, action, cancel_count):
    from jiuwenswarm.extensions_v2.webui import resources

    service, ops, _, _ = _service(tmp_path)
    published = await _publish(service, ops, {"page.html": "ready"})
    root = Path(service._directory.name)
    started, finish = threading.Event(), threading.Event()
    original = resources._delete_directory

    def delete(path):
        started.set()
        assert finish.wait(timeout=5)
        original(path)

    monkeypatch.setattr(resources, "_delete_directory", delete)
    if action == "release":
        operation = service.release(ops, published.resource_id)
    elif action == "drop":
        operation = service.drop(session_id="s1")
    else:
        operation = service.shutdown()
    task = asyncio.create_task(operation)
    try:
        assert await asyncio.to_thread(started.wait, 5)
        await _cancel_repeatedly(task, cancel_count)
        assert not task.done() and published.path.is_dir()
        finish.set()
        with pytest.raises(asyncio.CancelledError, match="cancel-1"):
            await task
        assert not published.path.exists() and not service._sets
        if action == "shutdown":
            assert service._directory is None and not root.exists()
    finally:
        finish.set()
        await service.shutdown()


@pytest.mark.asyncio
async def test_cancelled_cleanup_preserves_io_error_and_ownership_for_retry(tmp_path, monkeypatch):
    from jiuwenswarm.extensions_v2.webui import resources

    service, ops, _, _ = _service(tmp_path)
    published = await _publish(service, ops, {"page.html": "ready"})
    started, finish = threading.Event(), threading.Event()
    original = resources._delete_directory
    failure = OSError("delete failed")

    def delete(path):
        started.set()
        assert finish.wait(timeout=5)
        raise failure

    monkeypatch.setattr(resources, "_delete_directory", delete)
    task = asyncio.create_task(service.release(ops, published.resource_id))
    try:
        assert await asyncio.to_thread(started.wait, 5)
        await _cancel_repeatedly(task, 3)
        finish.set()
        with pytest.raises(asyncio.CancelledError, match="cancel-1") as caught:
            await task
        assert caught.value.__cause__ is failure
        assert service._sets[published.resource_id].revoked and published.path.is_dir()
        resource_id, path, route = _parts(published.url("page.html"))
        with pytest.raises(FileNotFoundError):
            await service.read(resource_id, path, route=route)
    finally:
        finish.set()
        monkeypatch.setattr(resources, "_delete_directory", original)
        await service.shutdown()
    assert not published.path.exists()


@pytest.mark.parametrize("path", [
    "../x.html", "/x.html", "a/../x.html", "./x.html", "a//x.html", "a\\x.html",
    "%2e%2e/x.html", "x.html?user_id=other", "x.txt", "x.html\n", "C:/x.html",
    "CON.html", "a./x.html", "a*/x.html", "a /x.html",
])
def test_resource_paths_reject_traversal_ambiguous_and_nonportable_names(path):
    with pytest.raises(ValueError):
        resource_path(path)


@pytest.mark.asyncio
async def test_published_files_are_immutable_and_routing_does_not_grant_access(tmp_path):
    service, ops, record, records = _service(tmp_path)
    try:
        files = {"zh.html": "<!doctype html><p>用量</p>", "css/page.css": "body{margin:0}"}
        published = await _publish(service, ops, files)
        resource_id, path, route = _parts(published.url("zh.html"))
        assert decode_resource_route(route) == ("u1", "s1", "")
        files["zh.html"] = "mutated"
        assert await service.read(resource_id, path, route=route) == (
            "<!doctype html><p>用量</p>".encode(), "text/html; charset=utf-8")
        for wrong_id, wrong_path, wrong_route in (
            ("X" * 43, path, route), (resource_id, "missing.html", route), (resource_id, path, route + "A"),
        ):
            with pytest.raises(FileNotFoundError):
                await service.read(wrong_id, wrong_path, route=wrong_route)
        record.generation += 1
        with pytest.raises(FileNotFoundError):
            await service.read(resource_id, path, route=route)
    finally:
        await service.shutdown()


@pytest.mark.asyncio
async def test_background_publication_retains_original_user_and_scope(tmp_path):
    service, ops, record, records = _service(tmp_path)
    try:
        first = await _publish(service, ops, {"page.html": "first"})
        ops.session_context = SessionContext("s1")
        second = await _publish(service, ops, {"page.html": "second"}, previous=first.resource_id)
        assert second.base_url.split("/")[3] == first.base_url.split("/")[3]
        for context in (SessionContext("s2"), SessionContext("s1", metadata={"user_id": "other"})):
            ops.session_context = context
            with pytest.raises(PermissionError):
                await _publish(service, ops, {"page.html": "forbidden"}, previous=first.resource_id)
            with pytest.raises(PermissionError):
                await service.release(ops, first.resource_id)
        ops.session_context = SessionContext("s1")
        await service.release(ops, first.resource_id)
        resource_id, path, route = _parts(second.url("page.html"))
        assert (await service.read(resource_id, path, route=route))[0] == b"second"
        await service.release(ops, first.resource_id)
    finally:
        await service.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["package", "session", "connection", "shutdown"])
async def test_scope_cleanup_revokes_files_and_closed_scopes_reject_publication(tmp_path, scope):
    service, ops, record, records = _service(tmp_path)
    connection = WebUIConnectionContext("tab1", user_id="u1")
    if scope == "connection":
        ops.session_context = None
    with bind_connection(connection):
        try:
            published = await _publish(service, ops, {"page.html": "content"})
            directory = Path(service._directory.name)
            resource_id, path, route = _parts(published.url("page.html"))
            if scope == "shutdown":
                await service.shutdown()
                assert not directory.exists()
            else:
                kwargs = {f"{scope}_id": {"package": "pkg.demo", "session": "s1", "connection": "tab1"}[scope]}
                await service.drop(**kwargs)
                assert not (directory / resource_id).exists()
            with pytest.raises(FileNotFoundError):
                await service.read(resource_id, path, route=route)
            if scope != "package":
                with pytest.raises(PermissionError):
                    await _publish(service, ops, {"page.html": "late"})
        finally:
            await service.shutdown()


@pytest.mark.asyncio
async def test_budget_type_and_path_conflicts_fail_without_partial_publication(tmp_path):
    service, ops, record, records = _service(tmp_path)
    try:
        for files in ({}, {"page.html": "x" * (MAX_RESOURCE_BYTES + 1)},
                      {"page.html": "x", "page.html/child.css": "x"},
                      {f"{i}.css": "x" for i in range(65)},
                      {f"{i}.css": b"x" * MAX_RESOURCE_BYTES for i in range(5)}):
            with pytest.raises((ValueError, OSError)):
                await _publish(service, ops, files)
        assert service._sets == {}
        assert list(Path(service._directory.name).iterdir()) == []
        exact = await _publish(service, ops, {"page.html": b"x" * MAX_RESOURCE_BYTES})
        resource_id, path, route = _parts(exact.url("page.html"))
        assert len((await service.read(resource_id, path, route=route))[0]) == MAX_RESOURCE_BYTES
        for _ in range(31):
            await _publish(service, ops, {"page.html": "small"})
        with pytest.raises(ValueError, match="Too many"):
            await _publish(service, ops, {"page.html": "overflow"})
    finally:
        await service.shutdown()


@pytest.mark.asyncio
async def test_cleanup_waits_for_plugin_writer_and_unpublished_files_are_inaccessible(tmp_path):
    service, ops, record, records = _service(tmp_path)
    directory = await service.create_directory(ops)
    try:
        async with directory:
            (directory.path / "page.html").write_text("complete", encoding="utf-8")
            with pytest.raises(ValueError, match="Finish"):
                await service.asset_url(ops, "page.html", resource_id=directory.resource_id)
            route = service._sets[directory.resource_id].route
            with pytest.raises(FileNotFoundError):
                await service.read(directory.resource_id, "page.html", route=route)
            cleanup = asyncio.create_task(service.drop(session_id="s1"))
            await asyncio.sleep(0)
            assert not cleanup.done() and directory.path.is_dir()
        await cleanup
        assert not directory.path.exists()
        with pytest.raises(PermissionError):
            async with directory:
                pass
    finally:
        await service.shutdown()


@pytest.mark.asyncio
async def test_file_changed_after_publication_is_not_served(tmp_path):
    service, ops, record, records = _service(tmp_path)
    try:
        published = await _publish(service, ops, {"page.html": "expected"})
        (Path(service._directory.name) / published.resource_id / "page.html").write_bytes(b"changed")
        resource_id, path, route = _parts(published.url("page.html"))
        with pytest.raises(FileNotFoundError, match="changed"):
            await service.read(resource_id, path, route=route)
    finally:
        await service.shutdown()


@pytest.mark.asyncio
async def test_http_uses_e2a_for_html_relative_css_head_and_revocation(tmp_path, monkeypatch):
    from jiuwenswarm.extensions_v2 import assembly
    from jiuwenswarm.gateway.channel_manager.web import webui_resource_http
    from jiuwenswarm.gateway.extensions_v2.webui.http import attach_browser_routes

    service, ops, record, records = _service(tmp_path)
    monkeypatch.setattr(assembly, "get_webui_runtime", lambda: SimpleNamespace(resources=service))
    adapter = WebUIDispatchAdapter()

    async def fetch(**kwargs):
        assert kwargs["req_method"] == ReqMethod.WEBUI_RESOURCE_READ
        assert kwargs["user_id"] == "u1" and kwargs["session_id"] == "s1"
        response = await adapter.handle(AgentRequest(
            "read", channel_id=kwargs["channel_id"], req_method=kwargs["req_method"],
            user_id=kwargs["user_id"], session_id=kwargs["session_id"], params=kwargs["params"],
        ))
        return response.ok, response.payload

    fetch_mock = AsyncMock(side_effect=fetch)
    monkeypatch.setattr(webui_resource_http, "fetch_agent_unary", fetch_mock)
    app = FastAPI()
    webui_resource_http.attach_webui_resource_routes(app, SimpleNamespace(agent_client=object()))
    attach_browser_routes(app, runtime_provider=lambda: None)
    try:
        files = {"page.html": '<!doctype html><link rel="stylesheet" href="page.css">' + "x" * 20000,
                 "page.css": "body{margin:0}"}
        published = await _publish(service, ops, files)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            html = await client.get(published.url("page.html"))
            assert html.status_code == 200 and html.text == files["page.html"]
            assert html.headers["content-type"] == "text/html; charset=utf-8"
            assert html.headers["cache-control"] == "no-store"
            assert html.headers["access-control-allow-origin"] == "*"
            assert "access-control-allow-credentials" not in html.headers
            assert "allow-same-origin" not in html.headers["content-security-policy"]
            css = await client.get(str(html.url).replace("page.html", "page.css"))
            assert css.status_code == 200 and css.text == files["page.css"]
            assert css.headers["access-control-allow-origin"] == "*"
            head = await client.head(published.url("page.html"))
            assert head.status_code == 200 and head.content == b""
            assert head.headers["content-length"] == str(len(files["page.html"].encode()))
            assert head.headers["access-control-allow-origin"] == "*"
            calls = fetch_mock.await_count
            invalid = await client.get("/extension-assets/_agent/invalid/bad/x.html")
            assert invalid.status_code == 404 and invalid.headers["access-control-allow-origin"] == "*"
            assert fetch_mock.await_count == calls
            await service.release(ops, published.resource_id)
            revoked = await client.get(published.url("page.html"))
            assert revoked.status_code == 404 and revoked.headers["access-control-allow-origin"] == "*"
            fetch_mock.return_value = (False, {"code": "SERVICE_UNAVAILABLE"})
            fetch_mock.side_effect = None
            unavailable = await client.get(published.url("page.html"))
            assert unavailable.status_code == 503 and unavailable.headers["access-control-allow-origin"] == "*"
    finally:
        await service.shutdown()


@pytest.mark.asyncio
async def test_revoked_activation_cannot_publish_and_a_new_activation_can(tmp_path):
    service, ops, record, records = _service(tmp_path)
    try:
        old = await _publish(service, ops, {"page.html": "old"})
        await service.drop(package_id="pkg.demo")
        with pytest.raises(PermissionError):
            await _publish(service, ops, {"page.html": "late"})
        records[0] = SimpleNamespace(**vars(record))
        new = await _publish(service, ops, {"page.html": "new"})
        assert old.resource_id != new.resource_id
        resource_id, path, route = _parts(old.url("page.html"))
        with pytest.raises(FileNotFoundError):
            await service.read(resource_id, path, route=route)
    finally:
        await service.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("identity", ["missing", "package", "root", "version", "scope"])
async def test_publication_requires_the_actual_active_caller(tmp_path, identity):
    service, ops, record, records = _service(tmp_path)
    if identity == "missing":
        records.clear()
    elif identity == "package":
        ops.package_metadata = PackageMetadata("other", "1.0.0", tmp_path)
    elif identity == "root":
        ops.package_metadata = PackageMetadata("pkg.demo", "1.0.0", tmp_path / "other")
    elif identity == "version":
        ops.package_metadata = PackageMetadata("pkg.demo", "2.0.0", tmp_path)
    else:
        ops.session_context = None
    try:
        with pytest.raises(PermissionError):
            await _publish(service, ops, {"page.html": "content"})
        assert service._sets == {} and service._directory is None
    finally:
        await service.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    {"data": "!invalid!", "media_type": "text/html; charset=utf-8"},
    {"data": "", "media_type": "unrecognized"},
    {"data": None, "media_type": "text/css; charset=utf-8"},
    {"data": "A" * (4 * ((MAX_RESOURCE_BYTES + 2) // 3) + 4), "media_type": "image/png"},
])
async def test_http_rejects_invalid_or_oversized_internal_file_responses(tmp_path, monkeypatch, payload):
    from jiuwenswarm.gateway.channel_manager.web import webui_resource_http
    service, ops, record, records = _service(tmp_path)
    try:
        resource = await _publish(service, ops, {"page.html": "content"})
        monkeypatch.setattr(webui_resource_http, "fetch_agent_unary", AsyncMock(return_value=(True, payload)))
        app = FastAPI()
        webui_resource_http.attach_webui_resource_routes(app, SimpleNamespace(agent_client=object()))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(resource.url("page.html"))
            assert response.status_code == 502 and response.headers["access-control-allow-origin"] == "*"
    finally:
        await service.shutdown()


@pytest.mark.asyncio
async def test_static_assets_are_read_in_place_and_release_preserves_the_package(tmp_path):
    service, ops, record, records = _service(tmp_path)
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "page.html").write_text('<link rel="stylesheet" href="page.css">', encoding="utf-8")
    (assets / "page.css").write_text("body{margin:0}", encoding="utf-8")
    try:
        url = await service.asset_url(ops, "page.html")
        resource_id, path, route = _parts(url)
        assert service._directory is None
        assert (await service.read(resource_id, "page.css", route=route))[0] == b"body{margin:0}"
        await service.release(ops, resource_id)
        assert (assets / "page.html").is_file() and (assets / "page.css").is_file()
        with pytest.raises(FileNotFoundError):
            await service.read(resource_id, path, route=route)
    finally:
        await service.shutdown()


@pytest.mark.asyncio
async def test_published_output_rejects_writes_and_unregistered_paths(tmp_path):
    service, ops, record, records = _service(tmp_path)
    try:
        output = await _publish(service, ops, {"page.html": "first"})
        with pytest.raises(ValueError, match="published"):
            await service.begin_write(output.resource_id, service._sets[output.resource_id])
        with pytest.raises(ValueError, match="registered"):
            await service.asset_url(ops, "new.html", resource_id=output.resource_id)
    finally:
        await service.shutdown()


@pytest.mark.asyncio
async def test_output_symlinks_are_rejected(tmp_path):
    service, ops, record, records = _service(tmp_path)
    output = await service.create_directory(ops)
    outside = tmp_path / "private.html"
    outside.write_text("private", encoding="utf-8")
    try:
        async with output:
            (output.path / "page.html").symlink_to(outside)
        with pytest.raises(ValueError, match="symlinks"):
            await service.asset_url(ops, "page.html", resource_id=output.resource_id)
    finally:
        await service.shutdown()


@pytest.mark.asyncio
async def test_writer_misuse_is_rejected_while_scope_cleanup_is_waiting(tmp_path, monkeypatch):
    service, ops, record, records = _service(tmp_path)
    directory = await service.create_directory(ops)
    waiting = asyncio.Event()
    retire = service._retire

    async def wait_for_writer(items):
        waiting.set()
        await retire(items)

    monkeypatch.setattr(service, "_retire", wait_for_writer)
    try:
        async with directory:
            with pytest.raises(ValueError, match="Exit"):
                await service.release(ops, directory.resource_id)
            cleanup = asyncio.create_task(service.drop(session_id="s1"))
            await waiting.wait()
            with pytest.raises(PermissionError):
                await service.asset_url(ops, "page.html", resource_id=directory.resource_id)
        await cleanup
        assert not directory.path.exists()
    finally:
        await service.shutdown()
