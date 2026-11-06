# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from openjiuwen.core.events import Event, EventHandlerOps, EventNext, HandlerLevel
from openjiuwen.core.common.exception.codes import StatusCode
from openjiuwen.core.extension import ActivationReport, ExtensionFailure
from openjiuwen.core.extension.errors import OperationError
from openjiuwen.core.extension.management import QueryResult

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.extensions_v2.management import adapter as adapter_module
from jiuwenswarm.extensions_v2.management.adapter import PackageManagementAdapter
from jiuwenswarm.extensions_v2.management.registry import ExtensionManager
from jiuwenswarm.extensions_v2.management.recovery import query_after_error

from .support import METADATA_EXTENSION, TWO_HANDLERS_EXTENSION, archive_package, make_bus, write_package


@pytest.mark.parametrize("reload_existing", [False, True])
async def test_core_publication_failure_rolls_back_without_repeating_management(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reload_existing: bool,
) -> None:
    bus = make_bus()
    rollbacks: list[str] = []

    async def observe_rollback(event: Event, ops: EventHandlerOps | None, next_handler: EventNext) -> object:
        rollbacks.append(event.event_type)
        return await next_handler(event)

    bus.register_event_handler("extension.rollback", observe_rollback, {}, HandlerLevel.BUILTIN)
    manager = ExtensionManager(tmp_path / "store", report_path=tmp_path / "report.yaml")
    await manager.init(bus)
    source = write_package(tmp_path / "source", module_source=TWO_HANDLERS_EXTENSION)
    if reload_existing:
        good = write_package(tmp_path / "good", module_source=METADATA_EXTENSION)
        await manager.install([good])
        installed = manager.install_dir / "test.pkg" / "1.0.0" / "extension.py"
        installed.write_text(TWO_HANDLERS_EXTENSION, encoding="utf-8")
    monkeypatch.setattr(adapter_module, "get_extension_manager", lambda: manager)
    query = AsyncMock(side_effect=AssertionError("a failure Report must not cause a state query"))
    monkeypatch.setattr(manager, "query", query)
    request = AgentRequest(
        request_id="failed-publish", channel_id="web",
        req_method=ReqMethod.EXTENSIONS_V2_RELOAD if reload_existing else ReqMethod.EXTENSIONS_V2_INSTALL,
        params={"package_id": "test.pkg"} if reload_existing else {"paths": [str(archive_package(source))]},
    )

    response = await PackageManagementAdapter().handle(request)

    assert response.ok and response.payload["reports"][0]["status"] == "FAILED"
    assert rollbacks == ["extension.rollback"]
    query.assert_not_awaited()
    assert source.is_dir()
    if reload_existing:
        assert manager.active_packages()[0].generation == 1
        assert (await bus.trigger(Event("test.event", {})))[0] == "test.pkg"
    else:
        assert manager.active_packages() == ()
        assert not (manager.install_dir / "test.pkg" / "1.0.0").exists()
        assert await bus.trigger(Event("test.event", {})) == ("core", {})


async def test_install_that_loses_report_is_queried_without_reinstalling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = ExtensionManager(tmp_path / "store", report_path=tmp_path / "report.yaml")
    await manager.init(make_bus())
    source = write_package(tmp_path / "source", module_source=METADATA_EXTENSION)
    install = manager.registry.install

    async def install_then_fail(paths: list[Path]) -> object:
        await install(paths)
        raise RuntimeError("outer handler lost the Report")

    operation = AsyncMock(side_effect=install_then_fail)
    monkeypatch.setattr(manager.registry, "install", operation)
    query = AsyncMock(wraps=manager.query)
    monkeypatch.setattr(manager, "query", query)
    monkeypatch.setattr(adapter_module, "get_extension_manager", lambda: manager)
    response = await PackageManagementAdapter().handle(
        AgentRequest(
            request_id="lost-report", channel_id="web", req_method=ReqMethod.EXTENSIONS_V2_INSTALL,
            params={"paths": [str(archive_package(source))]},
        )
    )

    assert not response.ok and response.payload["outcome"] == "unknown"
    assert response.payload["state"]["active"][0]["generation"] == 1
    assert "reports" not in response.payload
    operation.assert_awaited_once()
    query.assert_awaited_once_with(None)
    assert source.is_dir()
    assert (manager.install_dir / "test.pkg" / "1.0.0").is_dir()


@pytest.mark.parametrize("method", [ReqMethod.EXTENSIONS_V2_RELOAD, ReqMethod.EXTENSIONS_V2_UNINSTALL])
async def test_missing_single_package_report_only_queries_the_target(
    monkeypatch: pytest.MonkeyPatch, method: ReqMethod,
) -> None:
    operation = AsyncMock(side_effect=RuntimeError("missing Report"))
    query = AsyncMock(return_value=QueryResult())
    manager = SimpleNamespace(initialized=True, reload=operation, uninstall=operation, query=query)
    monkeypatch.setattr(adapter_module, "get_extension_manager", lambda: manager)
    response = await PackageManagementAdapter().handle(
        AgentRequest(request_id="one", channel_id="web", req_method=method, params={"package_id": "test.pkg"})
    )
    assert response.payload["outcome"] == "unknown"
    operation.assert_awaited_once_with("test.pkg")
    query.assert_awaited_once_with("test.pkg")


async def test_query_failure_keeps_original_operation_error(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = SimpleNamespace(
        initialized=True,
        reload=AsyncMock(side_effect=RuntimeError("write error")),
        query=AsyncMock(side_effect=RuntimeError("query error")),
    )
    monkeypatch.setattr(adapter_module, "get_extension_manager", lambda: manager)
    response = await PackageManagementAdapter().handle(
        AgentRequest(
            request_id="unknown", channel_id="web", req_method=ReqMethod.EXTENSIONS_V2_RELOAD,
            params={"package_id": "test.pkg"},
        )
    )
    assert response.payload["code"] == "INTERNAL_ERROR"
    assert response.payload["error"] == "V2 package management failed"
    assert response.payload["query_error"]
    assert "state" not in response.payload
    manager.query.assert_awaited_once()


async def test_query_errors_do_not_start_recursive_queries(monkeypatch: pytest.MonkeyPatch) -> None:
    query = AsyncMock(side_effect=RuntimeError("query failed"))
    monkeypatch.setattr(adapter_module, "get_extension_manager", lambda: SimpleNamespace(initialized=True, query=query))
    response = await PackageManagementAdapter().handle(
        AgentRequest(request_id="query", channel_id="web", req_method=ReqMethod.EXTENSIONS_V2_QUERY)
    )
    assert not response.ok and "outcome" not in response.payload
    query.assert_awaited_once_with(None)


async def test_cancelled_management_request_does_not_retry_or_query(monkeypatch: pytest.MonkeyPatch) -> None:
    query = AsyncMock()
    manager = SimpleNamespace(initialized=True, reload=AsyncMock(side_effect=asyncio.CancelledError), query=query)
    monkeypatch.setattr(adapter_module, "get_extension_manager", lambda: manager)
    with pytest.raises(asyncio.CancelledError):
        await PackageManagementAdapter().handle(
            AgentRequest(
                request_id="cancel", channel_id="web", req_method=ReqMethod.EXTENSIONS_V2_RELOAD,
                params={"package_id": "test.pkg"},
            )
        )
    query.assert_not_awaited()


async def test_report_encoding_failure_does_not_query_completed_operation(monkeypatch: pytest.MonkeyPatch) -> None:
    report = ActivationReport(
        "plan", "test.pkg", "reload", "FAILED", 1, 1,
        OperationError(StatusCode.EXTENSION_LOAD_FAILED, "bad diagnostics", {"unsupported": object()}),
    )
    query = AsyncMock()
    manager = SimpleNamespace(initialized=True, reload=AsyncMock(return_value=report), query=query)
    monkeypatch.setattr(adapter_module, "get_extension_manager", lambda: manager)
    response = await PackageManagementAdapter().handle(
        AgentRequest(
            request_id="encoding", channel_id="web", req_method=ReqMethod.EXTENSIONS_V2_RELOAD,
            params={"package_id": "test.pkg"},
        )
    )
    assert not response.ok and "outcome" not in response.payload
    query.assert_not_awaited()


async def test_recovery_can_be_used_without_network_adapter() -> None:
    state = QueryResult()
    manager = SimpleNamespace(query=AsyncMock(return_value=state))
    result, error = await query_after_error(manager, "test.pkg")
    assert result is state and error is None
    manager.query.assert_awaited_once_with("test.pkg")


async def test_core_exception_code_survives_state_reconciliation(monkeypatch: pytest.MonkeyPatch) -> None:
    error = ExtensionFailure(StatusCode.EXTENSION_LOAD_FAILED, reason="original write failure")
    manager = SimpleNamespace(
        initialized=True,
        reload=AsyncMock(side_effect=error),
        query=AsyncMock(return_value=QueryResult()),
    )
    monkeypatch.setattr(adapter_module, "get_extension_manager", lambda: manager)
    response = await PackageManagementAdapter().handle(
        AgentRequest(
            request_id="core-error", channel_id="web", req_method=ReqMethod.EXTENSIONS_V2_RELOAD,
            params={"package_id": "test.pkg"},
        )
    )
    assert response.payload["code"] == "EXTENSION_LOAD_FAILED"
    assert response.payload["error"] == error.message
    assert response.payload["outcome"] == "unknown"
    manager.query.assert_awaited_once_with("test.pkg")
