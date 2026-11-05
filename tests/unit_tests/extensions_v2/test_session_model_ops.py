# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Model queries through production assembly and real user package loading."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from openjiuwen.core.events import Event, EventHandlerOpsBuilder, SessionContext

from jiuwenswarm.common.model_errors import ModelSelectionError
from jiuwenswarm.extensions_v2 import assembly
from jiuwenswarm.extensions_v2.handler_ops.session_ops import session_model
from jiuwenswarm.server.runtime.session import model_selection_store as store
from .management.support import write_package


MODEL_READER = """\
import asyncio


class Reader:
    def register(self, registrar):
        registrar.register_event_handler("test.model", self.handle, {})

    async def handle(self, event, ops, next_handler):
        first = await ops.session.model()
        if event.event_input.get("mutate") and first:
            first = "user-change"
        await asyncio.sleep(0)
        return {
            "session_id": ops.session_context.session_id,
            "model": first,
            "again": await ops.session.model(),
            "cwd": await ops.session.cwd(),
            "root": await ops.session.root(),
            "ui_available": callable(ops.ui.render),
            "package_id": ops.package_metadata.package_id,
            "receipt": await next_handler(event),
        }


def create_extension():
    return Reader()
"""


async def _model_core(event, ops, next_handler):
    return {"accepted": True}


@pytest.fixture
async def model_host(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(assembly, "_ASSEMBLY", None)
    monkeypatch.setattr(assembly, "resolve_install_dir", lambda: tmp_path / "packages")
    monkeypatch.setattr(store, "get_agent_sessions_dir", lambda: tmp_path / "sessions")
    instance = await assembly.install_extensions()
    assert instance is not None
    instance.bus.register_event_type("test.model", lambda value, pattern: True, _model_core)
    package = write_package(tmp_path / "source", package_id="model.reader", module_source=MODEL_READER)
    try:
        reports = await instance.manager.install([package])
        assert len(reports) == 1 and reports[0].status == "SUCCESS"
        yield instance
    finally:
        await assembly.shutdown_extensions()


def _save(tmp_path: Path, session_id: str, value: object) -> Path:
    directory = tmp_path / "sessions" / session_id
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "metadata.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def _runtime(name: str) -> dict[str, object]:
    return {
        "selection": None, "model_name": name, "provider": "OpenAI",
        "source": "runtime", "activated": True,
    }


@pytest.mark.asyncio
async def test_user_package_reads_bound_runtime_model_without_resolving_or_reading_storage(
    model_host, tmp_path: Path, monkeypatch,
) -> None:
    def unexpected_read(session_id):
        pytest.fail("runtime model query must not read saved selections")

    from jiuwenswarm.server.runtime.session import query_views
    monkeypatch.setattr(query_views, "get_session_model_info", unexpected_read)
    snapshot = {**_runtime("actual-model"), "api_key": "discard-secret", "client_options": {"token": "discard"}}
    context = SessionContext("s1", str(tmp_path), {
        store.SESSION_MODEL_INFO_KEY: snapshot, "project_root": str(tmp_path),
    })
    result = await model_host.bus.trigger(Event("test.model", {"model": "spoof"}), context)
    assert result["model"] == result["again"] == "actual-model"
    assert result["cwd"] == result["root"] == str(tmp_path)
    assert result["ui_available"] and result["package_id"] == "model.reader"
    assert result["receipt"] == {"accepted": True}
    assert "discard" not in json.dumps(result)


@pytest.mark.asyncio
async def test_two_concurrent_sessions_keep_their_runtime_models(model_host) -> None:
    contexts = [SessionContext("s"+str(i), metadata={store.SESSION_MODEL_INFO_KEY: _runtime("model-"+str(i))})
                for i in range(2)]
    results = await asyncio.gather(*(
        model_host.bus.trigger(Event("test.model", {}), context) for context in contexts
    ))
    assert [r["model"] for r in results] == ["model-0", "model-1"]
    assert [r["session_id"] for r in results] == ["s0", "s1"]


@pytest.mark.asyncio
async def test_saved_stable_selection_has_unknown_activation_and_does_not_use_stale_name(
    model_host, tmp_path: Path,
) -> None:
    path = _save(tmp_path, "saved", {
        "model_selection": {"type": "model_group", "id": "group-a", "route_id": "route-b"},
        "model": "stale-name", "api_key": "secret", "unrelated": {"token": "secret"},
    })
    before = path.read_bytes()
    result = await model_host.bus.trigger(Event("test.model", {"session_id": "other"}), SessionContext("saved"))
    assert result["model"] is None
    assert store.get_session_model_info("saved")["selection"]["id"] == "group-a"
    assert result["model"] == result["again"]
    assert "secret" not in json.dumps(result)
    assert path.read_bytes() == before


@pytest.mark.asyncio
async def test_runtime_override_does_not_report_saved_model(model_host, tmp_path: Path) -> None:
    _save(tmp_path, "override", {"model_selection": {"type": "model", "id": "old-model"}})
    result = await model_host.bus.trigger(Event("test.model", {}), SessionContext("override", metadata={
        store.SESSION_MODEL_INFO_KEY: _runtime("request-override"),
    }))
    assert result["model"] == "request-override"


@pytest.mark.asyncio
async def test_legacy_selection_and_no_facts_are_distinct(model_host, tmp_path: Path) -> None:
    _save(tmp_path, "legacy", {"model": "old-display-name"})
    selected = await model_host.bus.trigger(Event("test.model", {}), SessionContext("legacy"))
    missing = await model_host.bus.trigger(Event("test.model", {"model": "spoof"}), SessionContext("missing"))
    assert selected["model"] == "old-display-name"
    assert missing["model"] is None and missing["again"] is None
    assert not (tmp_path / "sessions" / "missing").exists()


@pytest.mark.asyncio
async def test_returned_data_cannot_mutate_runtime_or_saved_facts(model_host, tmp_path: Path) -> None:
    _save(tmp_path, "saved", {"model_selection": {"type": "model", "id": "unchanged"}})
    saved = await model_host.bus.trigger(Event("test.model", {"mutate": True}), SessionContext("saved"))
    runtime = await model_host.bus.trigger(Event("test.model", {"mutate": True}), SessionContext("runtime", metadata={
        store.SESSION_MODEL_INFO_KEY: _runtime("unchanged"),
    }))
    assert saved["again"] is None
    assert store.get_session_model_info("saved")["selection"]["id"] == "unchanged"
    assert runtime["again"] == "unchanged"


@pytest.mark.asyncio
async def test_reload_and_unload_keep_platform_model_query_available(model_host, tmp_path: Path) -> None:
    _save(tmp_path, "saved", {"model": "before"})
    before = await model_host.bus.trigger(Event("test.model", {}), SessionContext("saved"))
    assert before["model"] == "before"
    assert (await model_host.manager.reload("model.reader")).status == "SUCCESS"
    _save(tmp_path, "saved", {"model": "after"})
    after = await model_host.bus.trigger(Event("test.model", {}), SessionContext("saved"))
    assert after["model"] == "after"
    assert (await model_host.manager.uninstall("model.reader")).status == "SUCCESS"
    assert await model_host.bus.trigger(Event("test.model", {}), SessionContext("saved")) == {"accepted": True}
    ops = EventHandlerOpsBuilder().build(session_context=SessionContext("saved"))
    assert (await session_model(ops))["model_name"] == "after"


@pytest.mark.asyncio
async def test_query_requires_session_and_rejects_path_traversal() -> None:
    with pytest.raises(RuntimeError, match="requires SessionContext"):
        await session_model(EventHandlerOpsBuilder().build())
    with pytest.raises(ValueError, match="invalid session_id"):
        await session_model(EventHandlerOpsBuilder().build(session_context=SessionContext("../other")))


@pytest.mark.asyncio
@pytest.mark.parametrize("metadata", [[], {"model_selection": "invalid"}, {"model_selection": {"type": "model"}}])
async def test_corrupt_saved_metadata_is_not_reported_as_missing(tmp_path: Path, monkeypatch, metadata) -> None:
    monkeypatch.setattr(store, "get_agent_sessions_dir", lambda: tmp_path / "sessions")
    _save(tmp_path, "broken", metadata)
    with pytest.raises(ModelSelectionError):
        await session_model(EventHandlerOpsBuilder().build(session_context=SessionContext("broken")))


@pytest.mark.asyncio
@pytest.mark.parametrize("snapshot", ["invalid", {"source": "global"}, {**_runtime("x"), "activated": 1}])
async def test_invalid_host_snapshot_is_rejected(snapshot) -> None:
    context = SessionContext("bound", metadata={store.SESSION_MODEL_INFO_KEY: snapshot})
    with pytest.raises((TypeError, ValueError)):
        await session_model(EventHandlerOpsBuilder().build(session_context=context))


def test_runtime_capture_only_reads_public_fields_and_copies_values() -> None:
    model = SimpleNamespace(
        model_config=SimpleNamespace(model_name="actual"),
        model_client_config={"client_provider": "OpenAI", "api_key": "secret", "api_base": "private-url"},
    )
    result = store.capture_runtime_model_info(model)
    model.model_config.model_name = "later"
    assert result == _runtime("actual")
    assert "secret" not in json.dumps(result) and "private-url" not in json.dumps(result)
    assert store.capture_runtime_model_info(None) is None
