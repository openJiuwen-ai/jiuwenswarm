from __future__ import annotations

import ast
import asyncio
import contextlib
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import BinaryIO, cast

import pytest
import yaml

from openjiuwen.harness.personal_context import PersonalContext

from jiuwenswarm.server.personal_context import host_api as host_module
from jiuwenswarm.server.personal_context.host_api import PersonalContextHostAPI


HOST_API_PATH = (
    Path(__file__).parents[3]
    / "jiuwenswarm"
    / "server"
    / "personal_context"
    / "host_api.py"
)

UNCONFIGURED_PROJECTION = {
    "configured": False,
    "collection_enabled": False,
    "agent_use_enabled": False,
    "strategy_profile": "rules",
    "model_index": None,
    "fetch_services": [],
}


def _config(
    *,
    enabled: bool = True,
    fetching_enabled: bool = True,
    root_dir: Path | None = None,
    interval: float = 60.0,
) -> dict[str, object]:
    root = root_dir or Path.cwd()
    return {
        "collection_enabled": enabled,
        "agent_use_enabled": fetching_enabled,
        "strategy_profile": "rules",
        "fetch_services": [
            {
                "service_id": "local-notes",
                "provider": "local_files",
                "enabled": True,
                "interval_seconds": interval,
                "time_range": {"mode": "all"},
                "source": {"root_dir": str(root)},
                "credentials": {},
            }
        ],
    }


def _local_service(service_id: str, root_dir: Path) -> dict[str, object]:
    return {
        "service_id": service_id,
        "provider": "local_files",
        "enabled": True,
        "interval_seconds": 60.0,
        "max_items_per_run": None,
        "time_range": {"mode": "all"},
        "source": {"root_dir": str(root_dir)},
        "credentials": {},
    }


def _bookmark_service(service_id: str) -> dict[str, object]:
    return {
        "service_id": service_id,
        "provider": "browser_bookmarks",
        "enabled": True,
        "interval_seconds": 60.0,
        "max_items_per_run": None,
        "time_range": {"mode": "all"},
        "source": {},
        "credentials": {},
    }


def _config_with_services(
    services: list[dict[str, object]],
) -> dict[str, object]:
    return {
        "collection_enabled": False,
        "agent_use_enabled": False,
        "strategy_profile": "rules",
        "fetch_services": services,
    }


class _FakeStatus:
    state = "RUNNING"

    def model_dump(self, *, mode: str) -> dict[str, object]:
        assert mode == "json"
        return {"state": self.state, "configured": True}


class FakeCore:
    Config = None

    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.snapshot_result: object = object()
        self.snapshot_error: BaseException | None = None
        self.configured: object | None = None
        self.active = False
        self.deactivate_error: BaseException | None = None
        self.deactivate_changes_active_before_error = False
        self.set_error: BaseException | None = None
        self.activate_error: BaseException | None = None
        self.activate_started: asyncio.Event | None = None
        self.activate_release: asyncio.Event | None = None
        self.deactivate_started: asyncio.Event | None = None
        self.deactivate_release: asyncio.Event | None = None
        self.cursor_payloads: dict[str, bytes] = {}
        self.remove_cursor_error: BaseException | None = None
        self.restore_cursor_error: BaseException | None = None
        self.authorization_status_result: dict[str, object] = {
            "provider": "feishu",
            "state": "authorized",
            "verification_url": None,
            "expires_at": None,
            "error": None,
        }
        self.authorization_status_error: BaseException | None = None
        self.authorization_status_started: asyncio.Event | None = None
        self.authorization_status_release: asyncio.Event | None = None

    async def set_configuration(self, config: object) -> None:
        self.calls.append(("set_configuration", config))
        if self.set_error is not None:
            error = self.set_error
            self.set_error = None
            raise error
        self.configured = config

    async def activate_runtime(self) -> None:
        self.calls.append(("activate_runtime", None))
        if self.activate_started is not None:
            self.activate_started.set()
        if self.activate_release is not None:
            await self.activate_release.wait()
        if self.activate_error is not None:
            error = self.activate_error
            self.activate_error = None
            raise error
        self.active = True

    async def deactivate_runtime(self, *, timeout_seconds: float = 30.0) -> None:
        self.calls.append(("deactivate_runtime", timeout_seconds))
        if self.deactivate_started is not None:
            self.deactivate_started.set()
        if self.deactivate_release is not None:
            await self.deactivate_release.wait()
        if self.deactivate_error is not None:
            error = self.deactivate_error
            self.deactivate_error = None
            if self.deactivate_changes_active_before_error:
                self.active = False
            raise error
        self.active = False

    async def start_collection(self) -> None:
        self.calls.append(("start_collection", None))
        if self.activate_started is not None:
            self.activate_started.set()
        if self.activate_release is not None:
            await self.activate_release.wait()
        if self.activate_error is not None:
            error = self.activate_error
            self.activate_error = None
            raise error
        self.active = True

    async def stop_collection(self, *, timeout_seconds: float = 30.0) -> None:
        self.calls.append(("stop_collection", timeout_seconds))
        if self.deactivate_started is not None:
            self.deactivate_started.set()
        if self.deactivate_release is not None:
            await self.deactivate_release.wait()
        if self.deactivate_error is not None:
            error = self.deactivate_error
            self.deactivate_error = None
            if self.deactivate_changes_active_before_error:
                self.active = False
            raise error
        self.active = False

    async def start_agent_use(self) -> None:
        self.calls.append(("start_agent_use", None))

    async def stop_agent_use(self) -> None:
        self.calls.append(("stop_agent_use", None))

    async def set_fetch_service_enabled(
        self,
        service_id: str,
        enabled: bool,
    ) -> None:
        self.calls.append(("set_fetch_service_enabled", (service_id, enabled)))

    async def snapshot(self) -> object:
        self.calls.append(("snapshot", None))
        if self.snapshot_error is not None:
            error = self.snapshot_error
            self.snapshot_error = None
            raise error
        return self.snapshot_result

    async def authorize_provider(self, provider: str) -> dict[str, object]:
        self.calls.append(("authorize_provider", provider))
        return {
            "provider": provider,
            "state": "authorized",
            "verification_url": None,
            "expires_at": None,
            "error": None,
        }

    async def get_authorization_status(self, provider: str) -> dict[str, object]:
        self.calls.append(("get_authorization_status", provider))
        if self.authorization_status_started is not None:
            self.authorization_status_started.set()
        if self.authorization_status_release is not None:
            await asyncio.wait_for(
                self.authorization_status_release.wait(), timeout=5.0
            )
        if self.authorization_status_error is not None:
            raise self.authorization_status_error
        return dict(self.authorization_status_result)

    async def run_fetch(
        self,
        *,
        service_id: str | None = None,
    ) -> dict[str, object]:
        self.calls.append(("run_fetch", service_id))
        return {
            "state": "accepted",
            "service_ids": [service_id or "local-notes"],
        }

    def remove_fetch_cursor(self, service_id: str) -> bytes | None:
        self.calls.append(("remove_fetch_cursor", service_id))
        if self.remove_cursor_error is not None:
            error = self.remove_cursor_error
            self.remove_cursor_error = None
            raise error
        return self.cursor_payloads.pop(service_id, None)

    def restore_fetch_cursor(
        self,
        service_id: str,
        payload: bytes | None,
    ) -> None:
        self.calls.append(("restore_fetch_cursor", (service_id, payload)))
        if self.restore_cursor_error is not None:
            error = self.restore_cursor_error
            self.restore_cursor_error = None
            raise error
        if payload is None:
            self.cursor_payloads.pop(service_id, None)
        else:
            self.cursor_payloads[service_id] = payload

    async def get_graph(
        self,
        *,
        root_id: str | None = None,
        depth: int = 3,
    ) -> dict[str, object]:
        self.calls.append(("get_graph", (root_id, depth)))
        return {"context_ready": True, "nodes": [], "edges": []}

    async def get_tree(
        self,
        *,
        root_id: str | None = None,
        depth: int = 3,
    ) -> dict[str, object]:
        self.calls.append(("get_tree", (root_id, depth)))
        return {"context_ready": True, "nodes": [], "edges": []}

    async def search_graph(self, query: str) -> dict[str, object]:
        self.calls.append(("search_graph", query))
        return {
            "results": [
                {
                    "node_id": "page:topics/personal_context.md",
                    "title": "主动上下文",
                    "path": "topics/personal_context.md",
                    "snippet": "主动上下文",
                }
            ]
        }

    async def get_graph_page(self, node_id: str) -> dict[str, object]:
        self.calls.append(("get_graph_page", node_id))
        return {
            "node_id": node_id,
            "title": "主动上下文",
            "path": "topics/personal_context.md",
            "markdown": "# 主动上下文\n",
        }

    async def get_source(self, source_id: str) -> dict[str, object]:
        self.calls.append(("get_source", source_id))
        return {"source_id": source_id, "title": "来源"}


@pytest.fixture
def fake_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[PersonalContextHostAPI, FakeCore]:
    host = PersonalContextHostAPI(home=tmp_path / "personal_context")
    fake = FakeCore()
    monkeypatch.setattr(host, "_personal_context", fake)
    return host, fake


def test_host_module_imports_personal_context_from_harness() -> None:
    tree = ast.parse(HOST_API_PATH.read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ImportFrom)
            and node.module == "openjiuwen.harness.personal_context"
        ):
            imported.extend(alias.name for alias in node.names)
    assert imported == ["PersonalContext"]


def test_boolean_switches_use_isinstance_guards() -> None:
    source = HOST_API_PATH.read_text(encoding="utf-8")

    assert source.count("if not isinstance(enabled, bool):") == 3
    assert "type(enabled) is not bool" not in source


def test_authorization_cancellation_is_rethrown_after_exception_handler() -> None:
    tree = ast.parse(HOST_API_PATH.read_text(encoding="utf-8"))
    function = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "get_authorization_status"
    )

    handlers = [
        node
        for node in ast.walk(function)
        if isinstance(node, ast.ExceptHandler)
        and ast.unparse(node.type) == "asyncio.CancelledError"
    ]
    assert len(handlers) == 1
    assert not any(
        isinstance(statement, ast.Raise) and statement.exc is None
        for statement in handlers[0].body
    )


def test_constructor_does_not_read_or_write_yaml(tmp_path: Path) -> None:
    home = tmp_path / "personal_context"
    host = PersonalContextHostAPI(home=home)
    assert not home.exists()
    assert not (home / "personal_context.yaml").exists()
    assert host._config is None


@pytest.mark.asyncio
async def test_start_without_yaml_bootstraps_default_config(tmp_path: Path) -> None:
    home = tmp_path / "personal_context"
    host = PersonalContextHostAPI(home=home)
    await host.start()
    status = await host.get_status()
    # First deployment bootstraps a default config (collection + agent-use ON)
    # so the settings page opens with the toggle enabled by default.
    assert status.configured is True
    assert status.collection_enabled is True
    assert status.agent_use_enabled is True
    assert (home / "personal_context.yaml").is_file()


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_field", ["enabled", "fetching_enabled"])
async def test_start_rejects_yaml_with_legacy_global_switch(
    tmp_path: Path,
    legacy_field: str,
) -> None:
    home = tmp_path / "personal_context"
    home.mkdir()
    raw = _config(enabled=False, root_dir=tmp_path)
    raw[legacy_field] = False
    (home / "personal_context.yaml").write_text(
        yaml.safe_dump(raw, sort_keys=False),
        encoding="utf-8",
    )
    host = PersonalContextHostAPI(home=home)

    with pytest.raises(PersonalContext.Error):
        await host.start()

    assert host._config is None
    assert host._stored_config is None


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_use_enabled", [False, True])
async def test_agent_use_projection_reads_loaded_configuration(
    fake_host: tuple[PersonalContextHostAPI, FakeCore],
    tmp_path: Path,
    agent_use_enabled: bool,
) -> None:
    host, _core = fake_host

    assert await host.is_runtime_enabled() is False

    await host.configure(_config(fetching_enabled=agent_use_enabled, root_dir=tmp_path))

    assert await host.is_runtime_enabled() is agent_use_enabled


@pytest.mark.asyncio
async def test_unconfigured_projection_and_stop_are_read_only_until_first_start(
    fake_host: tuple[PersonalContextHostAPI, FakeCore],
) -> None:
    host, core = fake_host

    assert await host.get_runtime_config() == UNCONFIGURED_PROJECTION
    assert await host.set_collection_enabled(False) == UNCONFIGURED_PROJECTION
    assert not host._home.exists()
    assert core.calls == []

    started = await host.set_collection_enabled(True)

    assert started["collection_enabled"] is True
    assert started["agent_use_enabled"] is True
    assert started["strategy_profile"] == "rules"
    assert started["model_index"] is None
    assert started["fetch_services"] == []
    assert host._config_path.is_file()
    assert [name for name, _value in core.calls] == [
        "set_configuration",
        "activate_runtime",
    ]
    assert core.active is True


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_attribute", ["set_error", "activate_error"])
async def test_first_start_core_failure_rolls_back_to_unconfigured(
    fake_host: tuple[PersonalContextHostAPI, FakeCore],
    failure_attribute: str,
) -> None:
    host, core = fake_host
    setattr(core, failure_attribute, RuntimeError("first start failed"))

    with pytest.raises(PersonalContext.Error):
        await host.set_collection_enabled(True)

    expected_calls = ["set_configuration", "deactivate_runtime"]
    if failure_attribute == "activate_error":
        expected_calls = [
            "set_configuration",
            "activate_runtime",
            "deactivate_runtime",
        ]
    assert [name for name, _value in core.calls] == expected_calls
    assert host._config is None
    assert host._stored_config is None
    assert not host._config_path.exists()
    assert not core.active
    assert list(host._home.glob(".*.tmp")) == []


@pytest.mark.asyncio
async def test_first_start_cancellation_rolls_back_to_unconfigured(
    fake_host: tuple[PersonalContextHostAPI, FakeCore],
) -> None:
    host, core = fake_host
    core.activate_started = asyncio.Event()
    core.activate_release = asyncio.Event()

    task = asyncio.create_task(host.set_collection_enabled(True))
    await asyncio.sleep(0)
    if task.done():
        await task
        pytest.fail("first start completed before Core activation was observed")
    await asyncio.wait_for(core.activate_started.wait(), timeout=1.0)
    task.cancel()
    core.activate_release.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert task.cancelled()
    assert host._config is None
    assert host._stored_config is None
    assert not host._config_path.exists()
    assert not core.active
    assert list(host._home.glob(".*.tmp")) == []


@pytest.mark.asyncio
async def test_first_start_replace_failure_stops_core_and_removes_temporary_file(
    fake_host: tuple[PersonalContextHostAPI, FakeCore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host, core = fake_host

    def fail_replace(_temporary: Path, _path: Path) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(host_module, "_replace_yaml", fail_replace)

    with pytest.raises(PersonalContext.Error):
        await host.set_collection_enabled(True)

    assert [name for name, _value in core.calls] == [
        "set_configuration",
        "activate_runtime",
        "deactivate_runtime",
    ]
    assert not core.active
    assert host._config is None
    assert host._stored_config is None
    assert not host._config_path.exists()
    assert list(host._home.glob(".*.tmp")) == []


@pytest.mark.asyncio
async def test_first_start_replace_and_rollback_stop_failure_keeps_core_reference(
    fake_host: tuple[PersonalContextHostAPI, FakeCore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host, core = fake_host
    core.deactivate_error = RuntimeError("sensitive rollback stop failure")

    def fail_replace(_temporary: Path, _path: Path) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(host_module, "_replace_yaml", fail_replace)

    with pytest.raises(PersonalContext.Error) as caught:
        await host.set_collection_enabled(True)

    assert caught.value.status.name == "CONTEXT_PROACTIVE_STATE_INVALID"
    assert "previous configuration could not be restored" in str(caught.value)
    assert "sensitive rollback stop failure" not in str(caught.value)
    assert [name for name, _value in core.calls] == [
        "set_configuration",
        "activate_runtime",
        "deactivate_runtime",
    ]
    assert host._personal_context is core
    assert core.active is True
    assert host._config is None
    assert host._stored_config is None
    assert not host._config_path.exists()
    assert list(host._home.glob(".*.tmp")) == []


@pytest.mark.asyncio
async def test_configure_writes_yaml_and_starts_enabled_core(
    fake_host: tuple[PersonalContextHostAPI, FakeCore], tmp_path: Path
) -> None:
    host, core = fake_host
    await host.configure(_config(root_dir=tmp_path))
    assert (host._home / "personal_context.yaml").is_file()
    saved = yaml.safe_load(
        (host._home / "personal_context.yaml").read_text(encoding="utf-8")
    )
    assert saved["collection_enabled"] is True
    assert [name for name, _ in core.calls] == ["set_configuration", "activate_runtime"]


@pytest.mark.asyncio
async def test_authorize_provider_delegates_to_configured_core(
    fake_host: tuple[PersonalContextHostAPI, FakeCore], tmp_path: Path
) -> None:
    host, core = fake_host
    await host.configure(_config(enabled=False, root_dir=tmp_path))
    core.calls.clear()

    result = await host.authorize_provider("feishu")

    assert result["state"] == "authorized"
    assert core.calls == [("authorize_provider", "feishu")]


@pytest.mark.asyncio
async def test_get_authorization_status_delegates_full_result_to_configured_core(
    fake_host: tuple[PersonalContextHostAPI, FakeCore], tmp_path: Path
) -> None:
    host, core = fake_host
    await host.configure(_config(enabled=False, root_dir=tmp_path))
    core.calls.clear()

    result = await host.get_authorization_status("feishu")

    assert result == {
        "provider": "feishu",
        "state": "authorized",
        "verification_url": None,
        "expires_at": None,
        "error": None,
    }
    assert core.calls == [("get_authorization_status", "feishu")]


@pytest.mark.asyncio
async def test_get_authorization_status_rejects_unconfigured_host_without_core_call(
    fake_host: tuple[PersonalContextHostAPI, FakeCore],
) -> None:
    host, core = fake_host

    with pytest.raises(PersonalContext.Error) as caught:
        await host.get_authorization_status("feishu")

    assert caught.value.status.name == "CONTEXT_PROACTIVE_CONFIG_INVALID"
    assert core.calls == []


@pytest.mark.asyncio
async def test_get_authorization_status_safely_converts_core_failure(
    fake_host: tuple[PersonalContextHostAPI, FakeCore], tmp_path: Path
) -> None:
    host, core = fake_host
    await host.configure(_config(enabled=False, root_dir=tmp_path))
    core.calls.clear()
    core.authorization_status_error = RuntimeError(
        "token=top-secret https://example.invalid/auth?device_code=device-secret C:/private/user"
    )

    with pytest.raises(PersonalContext.Error) as caught:
        await host.get_authorization_status("feishu")

    assert caught.value.status.name == "CONTEXT_PROACTIVE_STATE_INVALID"
    serialized = str(caught.value)
    for secret in ("top-secret", "device-secret", "example.invalid", "C:/private/user"):
        assert secret not in serialized
    assert core.calls == [("get_authorization_status", "feishu")]


@pytest.mark.asyncio
async def test_get_authorization_status_uses_operation_lock_and_propagates_cancellation(
    fake_host: tuple[PersonalContextHostAPI, FakeCore], tmp_path: Path
) -> None:
    host, core = fake_host
    await host.configure(_config(enabled=False, root_dir=tmp_path))
    core.calls.clear()

    await host._operation_lock.acquire()
    blocked = asyncio.create_task(host.get_authorization_status("feishu"))
    try:
        await asyncio.sleep(0)
        assert core.calls == []
    finally:
        host._operation_lock.release()
    assert (await asyncio.wait_for(blocked, timeout=1.0))["state"] == "authorized"

    core.calls.clear()
    core.authorization_status_started = asyncio.Event()
    core.authorization_status_release = asyncio.Event()
    cancelled = asyncio.create_task(host.get_authorization_status("feishu"))
    try:
        await asyncio.wait_for(core.authorization_status_started.wait(), timeout=1.0)
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(cancelled, timeout=1.0)
    finally:
        core.authorization_status_release.set()
        if not cancelled.done():
            cancelled.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(cancelled, timeout=1.0)
    assert cancelled.cancelled()
    await asyncio.wait_for(host._operation_lock.acquire(), timeout=1.0)
    host._operation_lock.release()


@pytest.mark.asyncio
async def test_get_graph_delegates_to_core(
    fake_host: tuple[PersonalContextHostAPI, FakeCore],
) -> None:
    host, core = fake_host

    result = await host.get_graph(root_id="page:topics/description.md", depth=1)

    assert result == {"context_ready": True, "nodes": [], "edges": []}
    assert core.calls == [("get_graph", ("page:topics/description.md", 1))]


@pytest.mark.asyncio
async def test_get_tree_delegates_to_core(
    fake_host: tuple[PersonalContextHostAPI, FakeCore],
) -> None:
    host, core = fake_host

    result = await host.get_tree(root_id=None, depth=3)

    assert result == {"context_ready": True, "nodes": [], "edges": []}
    assert core.calls == [("get_tree", (None, 3))]


@pytest.mark.asyncio
async def test_search_graph_delegates_to_core(
    fake_host: tuple[PersonalContextHostAPI, FakeCore],
) -> None:
    host, core = fake_host

    result = await host.search_graph("主动上下文")

    assert result["results"][0]["node_id"] == "page:topics/personal_context.md"
    assert core.calls == [("search_graph", "主动上下文")]


@pytest.mark.asyncio
async def test_get_graph_page_delegates_to_core(
    fake_host: tuple[PersonalContextHostAPI, FakeCore],
) -> None:
    host, core = fake_host

    result = await host.get_graph_page("page:topics/personal_context.md")

    assert result["markdown"] == "# 主动上下文\n"
    assert core.calls == [("get_graph_page", "page:topics/personal_context.md")]


@pytest.mark.asyncio
async def test_get_source_delegates_to_core(
    fake_host: tuple[PersonalContextHostAPI, FakeCore],
) -> None:
    host, core = fake_host

    result = await host.get_source("src_abc")

    assert result["source_id"] == "src_abc"
    assert core.calls == [("get_source", "src_abc")]


@pytest.mark.asyncio
async def test_configure_same_semantics_only_replaces_yaml(
    fake_host: tuple[PersonalContextHostAPI, FakeCore], tmp_path: Path
) -> None:
    host, core = fake_host
    config = _config(root_dir=tmp_path)
    await host.configure(config)
    core.snapshot_result = _FakeStatus()
    core.calls.clear()
    await host.configure(config)
    assert [name for name, _ in core.calls] == ["snapshot"]


@pytest.mark.asyncio
async def test_configure_changed_semantics_stops_sets_and_restarts(
    fake_host: tuple[PersonalContextHostAPI, FakeCore], tmp_path: Path
) -> None:
    host, core = fake_host
    await host.configure(_config(root_dir=tmp_path, interval=60.0))
    core.calls.clear()
    await host.configure(_config(root_dir=tmp_path, interval=120.0))
    assert [name for name, _ in core.calls] == [
        "snapshot",
        "deactivate_runtime",
        "set_configuration",
        "activate_runtime",
    ]


@pytest.mark.asyncio
async def test_disabled_configuration_does_not_activate_or_delete_context(
    fake_host: tuple[PersonalContextHostAPI, FakeCore], tmp_path: Path
) -> None:
    host, core = fake_host
    context = host._home / "workspace" / "context"
    context.mkdir(parents=True)
    description = context / "description.md"
    description.write_text("keep", encoding="utf-8")
    await host.configure(_config(enabled=False, root_dir=tmp_path))
    assert description.read_text(encoding="utf-8") == "keep"
    assert [name for name, _ in core.calls] == ["set_configuration"]


@pytest.mark.asyncio
async def test_start_loads_existing_yaml_only_once(tmp_path: Path) -> None:
    home = tmp_path / "personal_context"
    home.mkdir()
    config = _config(enabled=False, root_dir=tmp_path)
    (home / "personal_context.yaml").write_text(
        yaml.safe_dump(config, sort_keys=False), encoding="utf-8"
    )
    host = PersonalContextHostAPI(home=home)
    await host.start()
    assert host._config is not None
    (home / "personal_context.yaml").write_text(
        "not: a PersonalContext config", encoding="utf-8"
    )
    await host.start()
    assert host._config is not None


@pytest.mark.asyncio
async def test_get_status_does_not_wait_for_operation_lock(
    fake_host: tuple[PersonalContextHostAPI, FakeCore],
) -> None:
    host, core = fake_host
    core.snapshot_result = "status"
    await host._operation_lock.acquire()
    try:
        assert await asyncio.wait_for(host.get_status(), timeout=0.1) == "status"
    finally:
        host._operation_lock.release()


@pytest.mark.asyncio
async def test_get_overview_returns_full_config_including_credentials(
    fake_host: tuple[PersonalContextHostAPI, FakeCore],
    tmp_path: Path,
) -> None:
    host, core = fake_host
    config = _config(enabled=False, root_dir=tmp_path)
    service = cast(list[dict[str, object]], config["fetch_services"])[0]
    service["provider"] = "github"
    service["source"] = {
        "owner": "openjiuwen",
        "repo": "agent-core",
        "resources": ["readme", "issues", "pull_requests", "commits", "code"],
    }
    service["credentials"] = {"token": "plain-token"}
    core.snapshot_result = _FakeStatus()

    await host.configure(config)
    overview = await host.get_overview()

    assert overview["configured"] is True
    assert overview["config"]["fetch_services"][0]["credentials"] == {
        "token": "plain-token"
    }
    assert overview["status"] == {"state": "RUNNING", "configured": True}


@pytest.mark.asyncio
async def test_patch_runtime_configuration_changes_only_strategy_profile(
    fake_host: tuple[PersonalContextHostAPI, FakeCore],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        host_module,
        "get_default_models",
        lambda: [
            {
                "model_client_config": {
                    "client_provider": "OpenAI",
                    "api_key": "key",
                    "api_base": "https://example.invalid/v1",
                    "model_name": "model-a",
                },
                "model_config_obj": {"temperature": 0.2},
            }
        ],
    )
    host, _core = fake_host
    await host.configure(_config(enabled=False, root_dir=tmp_path))
    await host.select_model(model_index=0)
    before = await host.get_runtime_config()

    after = await host.patch_runtime_config({"strategy_profile": "balanced"})

    assert after["strategy_profile"] == "balanced"
    assert after["fetch_services"] == before["fetch_services"]
    assert after["model_index"] == 0


@pytest.mark.asyncio
async def test_patch_runtime_configuration_persists_both_global_switches(
    fake_host: tuple[PersonalContextHostAPI, FakeCore],
    tmp_path: Path,
) -> None:
    host, _core = fake_host
    await host.configure(
        _config(enabled=False, fetching_enabled=False, root_dir=tmp_path)
    )

    after = await host.patch_runtime_config(
        {"collection_enabled": True, "agent_use_enabled": True}
    )

    saved = yaml.safe_load(host._config_path.read_text(encoding="utf-8"))
    assert after["collection_enabled"] is True
    assert after["agent_use_enabled"] is True
    assert saved["collection_enabled"] is True
    assert saved["agent_use_enabled"] is True
    assert "fetching_enabled" not in saved
    assert set(saved).isdisjoint({"enabled", "fetching_enabled"})


@pytest.mark.asyncio
async def test_patch_runtime_configuration_rejects_unknown_field_without_writing(
    fake_host: tuple[PersonalContextHostAPI, FakeCore],
    tmp_path: Path,
) -> None:
    host, _core = fake_host
    await host.configure(_config(enabled=False, root_dir=tmp_path))
    before = host._config_path.read_bytes()

    with pytest.raises(PersonalContext.Error):
        await host.patch_runtime_config({"enabled": True})

    assert host._config_path.read_bytes() == before


def test_resolve_model_reference_forces_personal_context_transport_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_entry = {
        "model_client_config": {
            "client_provider": "OpenAI",
            "api_key": "key",
            "api_base": "https://example.invalid/v1",
            "model_name": "model-a",
            "max_retries": 0,
        },
        "model_config_obj": {"temperature": 0.2},
    }
    monkeypatch.setattr(host_module, "get_default_models", lambda: [model_entry])

    client, request = host_module._resolve_model_reference(0)

    assert client["max_retries"] == 2
    assert request["model"] == "model-a"
    assert model_entry["model_client_config"]["max_retries"] == 0
    assert model_entry["model_client_config"]["model_name"] == "model-a"


@pytest.mark.asyncio
async def test_select_model_persists_only_model_index(
    fake_host: tuple[PersonalContextHostAPI, FakeCore],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        host_module,
        "get_default_models",
        lambda: [
            {
                "model_client_config": {
                    "client_provider": "OpenAI",
                    "api_key": "key",
                    "api_base": "https://example.invalid/v1",
                    "model_name": "model-a",
                },
                "model_config_obj": {"temperature": 0.2},
            }
        ],
    )
    host, core = fake_host
    await host.configure(_config(enabled=False, root_dir=tmp_path))
    core.calls.clear()

    result = await host.select_model(model_index=0)

    saved = yaml.safe_load(host._config_path.read_text(encoding="utf-8"))
    applied = next(
        value for name, value in reversed(core.calls) if name == "set_configuration"
    )
    assert result["model_index"] == 0
    assert saved["model_index"] == 0
    assert "model_origin_index" not in saved
    assert "model_client" not in saved
    assert "model_request" not in saved
    assert applied.model_request.model_name == "model-a"
    assert applied.model_client.max_retries == 2
    assert "max_retries" not in saved


@pytest.mark.asyncio
async def test_configure_rejects_old_model_origin_index(
    fake_host: tuple[PersonalContextHostAPI, FakeCore],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        host_module,
        "get_default_models",
        lambda: [
            {
                "model_client_config": {
                    "client_provider": "OpenAI",
                    "api_key": "key",
                    "api_base": "https://example.invalid/v1",
                    "model_name": "model-a",
                },
                "model_config_obj": {"temperature": 0.2},
            }
        ],
    )
    host, _core = fake_host
    stored = _config(enabled=False, root_dir=tmp_path)
    stored["model_origin_index"] = 0

    with pytest.raises(PersonalContext.Error):
        await host.configure(stored)

    assert not host._config_path.exists()


@pytest.mark.asyncio
async def test_select_model_rejects_missing_model_without_writing(
    fake_host: tuple[PersonalContextHostAPI, FakeCore],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(host_module, "get_default_models", lambda: [])
    host, _core = fake_host
    await host.configure(_config(enabled=False, root_dir=tmp_path))
    before = host._config_path.read_bytes()

    with pytest.raises(PersonalContext.Error):
        await host.select_model(model_index=0)

    assert host._config_path.read_bytes() == before


@pytest.mark.asyncio
async def test_runtime_model_strategy_requires_selected_model_without_writing(
    fake_host: tuple[PersonalContextHostAPI, FakeCore],
    tmp_path: Path,
) -> None:
    host, _core = fake_host
    await host.configure(_config(enabled=False, root_dir=tmp_path))
    before = host._config_path.read_bytes()

    with pytest.raises(PersonalContext.Error):
        await host.patch_runtime_config({"strategy_profile": "agent"})

    assert host._config_path.read_bytes() == before


@pytest.mark.asyncio
async def test_start_rejects_saved_model_index_when_model_was_removed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "personal_context"
    home.mkdir()
    stored = _config(enabled=True, root_dir=tmp_path)
    stored["strategy_profile"] = "balanced"
    stored["model_index"] = 0
    (home / "personal_context.yaml").write_text(
        yaml.safe_dump(stored, sort_keys=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(host_module, "get_default_models", lambda: [])
    host = PersonalContextHostAPI(home=home)

    with pytest.raises(PersonalContext.Error):
        await host.start()

    assert host._config is None
    assert host._stored_config is None


@pytest.mark.asyncio
async def test_set_collection_enabled_persists_and_applies_switch(
    fake_host: tuple[PersonalContextHostAPI, FakeCore],
    tmp_path: Path,
) -> None:
    host, core = fake_host
    core.snapshot_result = _FakeStatus()
    await host.configure(_config(enabled=False, root_dir=tmp_path))
    core.calls.clear()

    started = await host.set_collection_enabled(True)
    stopped = await host.set_collection_enabled(False)

    assert started["collection_enabled"] is True
    assert stopped["collection_enabled"] is False
    saved = yaml.safe_load(host._config_path.read_text(encoding="utf-8"))
    assert saved["collection_enabled"] is False
    assert core.calls == [
        ("start_collection", None),
        ("stop_collection", 30.0),
    ]


@pytest.mark.asyncio
async def test_same_enabled_candidate_restarts_stopped_runtime_before_yaml_publish(
    fake_host: tuple[PersonalContextHostAPI, FakeCore],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host, core = fake_host
    await host.configure(_config(enabled=True, root_dir=tmp_path))
    await host.stop()
    core.snapshot_result = SimpleNamespace(state="STOPPED")
    core.calls.clear()
    active_at_replace: list[bool] = []
    original_replace = host_module._replace_yaml

    def record_replace(temporary: Path, path: Path) -> None:
        active_at_replace.append(core.active)
        original_replace(temporary, path)

    monkeypatch.setattr(host_module, "_replace_yaml", record_replace)

    result = await host.set_collection_enabled(True)

    assert result["collection_enabled"] is True
    assert core.active is True
    assert active_at_replace == [True]
    assert core.calls == [("start_collection", None)]


@pytest.mark.asyncio
async def test_same_disabled_candidate_stops_unexpected_active_runtime_before_publish(
    fake_host: tuple[PersonalContextHostAPI, FakeCore],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host, core = fake_host
    await host.configure(_config(enabled=False, root_dir=tmp_path))
    core.active = True
    core.snapshot_result = SimpleNamespace(state="RUNNING")
    core.calls.clear()
    active_at_replace: list[bool] = []
    original_replace = host_module._replace_yaml

    def record_replace(temporary: Path, path: Path) -> None:
        active_at_replace.append(core.active)
        original_replace(temporary, path)

    monkeypatch.setattr(host_module, "_replace_yaml", record_replace)

    result = await host.set_collection_enabled(False)

    assert result["collection_enabled"] is False
    assert core.active is False
    assert active_at_replace == [True]
    assert core.calls == [("stop_collection", 30.0)]


@pytest.mark.asyncio
async def test_collection_hot_switch_does_not_depend_on_snapshot(
    fake_host: tuple[PersonalContextHostAPI, FakeCore],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host, core = fake_host
    await host.configure(_config(enabled=True, root_dir=tmp_path))
    core.calls.clear()
    core.snapshot_error = RuntimeError("sensitive snapshot detail")
    stage_calls: list[tuple[Path, bytes]] = []
    original_stage = host_module._stage_yaml

    def record_stage(path: Path, payload: bytes) -> Path:
        stage_calls.append((path, payload))
        return original_stage(path, payload)

    monkeypatch.setattr(host_module, "_stage_yaml", record_stage)

    result = await host.set_collection_enabled(False)

    assert result["collection_enabled"] is False
    assert core.calls == [("stop_collection", 30.0)]
    assert core.active is False
    assert len(stage_calls) == 1
    assert host._config is not None
    assert host._config.collection_enabled is False
    assert list(host._home.glob(".*.tmp")) == []


@pytest.mark.asyncio
async def test_disable_publishes_false_before_waiting_for_core_stop(
    fake_host: tuple[PersonalContextHostAPI, FakeCore],
    tmp_path: Path,
) -> None:
    host, core = fake_host
    await host.configure(_config(enabled=True, root_dir=tmp_path))
    core.snapshot_result = _FakeStatus()
    core.deactivate_started = asyncio.Event()
    core.deactivate_release = asyncio.Event()

    task = asyncio.create_task(host.set_collection_enabled(False))
    try:
        await asyncio.wait_for(core.deactivate_started.wait(), timeout=1.0)
        saved_while_waiting = yaml.safe_load(
            host._config_path.read_text(encoding="utf-8")
        )
        assert saved_while_waiting["collection_enabled"] is False
        assert host._stored_config is not None
        assert host._stored_config["collection_enabled"] is True

        core.deactivate_release.set()
        stopped = await task
    finally:
        core.deactivate_release.set()
        if not task.done():
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    assert stopped["collection_enabled"] is False
    assert core.active is False


@pytest.mark.asyncio
async def test_disable_failure_restores_enabled_yaml_memory_and_runtime(
    fake_host: tuple[PersonalContextHostAPI, FakeCore],
    tmp_path: Path,
) -> None:
    host, core = fake_host
    await host.configure(_config(enabled=True, root_dir=tmp_path))
    core.snapshot_result = _FakeStatus()
    old_yaml = host._config_path.read_bytes()
    old_config = host._config
    old_stored = deepcopy(host._stored_config)
    core.calls.clear()
    core.deactivate_changes_active_before_error = True
    core.deactivate_error = RuntimeError("collection stop failed")

    with pytest.raises(PersonalContext.Error):
        await host.set_collection_enabled(False)

    assert host._config == old_config
    assert host._stored_config == old_stored
    assert host._config_path.read_bytes() == old_yaml
    assert yaml.safe_load(old_yaml)["collection_enabled"] is True
    assert core.calls == [
        ("stop_collection", 30.0),
        ("start_collection", None),
    ]
    assert core.active is True
    assert list(host._home.glob(".*.tmp")) == []


@pytest.mark.asyncio
async def test_disable_cancellation_restores_enabled_yaml_memory_and_runtime(
    fake_host: tuple[PersonalContextHostAPI, FakeCore],
    tmp_path: Path,
) -> None:
    host, core = fake_host
    await host.configure(_config(enabled=True, root_dir=tmp_path))
    core.snapshot_result = _FakeStatus()
    old_yaml = host._config_path.read_bytes()
    old_config = host._config
    old_stored = deepcopy(host._stored_config)
    core.calls.clear()
    core.deactivate_started = asyncio.Event()
    core.deactivate_release = asyncio.Event()

    task = asyncio.create_task(host.set_collection_enabled(False))
    try:
        await asyncio.wait_for(core.deactivate_started.wait(), timeout=1.0)
        assert (
            yaml.safe_load(host._config_path.read_text(encoding="utf-8"))[
                "collection_enabled"
            ]
            is False
        )
        task.cancel()
        core.deactivate_release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        core.deactivate_release.set()
        if not task.done():
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    assert task.cancelled()
    assert host._config == old_config
    assert host._stored_config == old_stored
    assert host._config_path.read_bytes() == old_yaml
    assert core.active is True
    assert list(host._home.glob(".*.tmp")) == []


@pytest.mark.asyncio
async def test_runtime_start_failure_rolls_back_file_and_memory(
    fake_host: tuple[PersonalContextHostAPI, FakeCore],
    tmp_path: Path,
) -> None:
    host, core = fake_host
    core.snapshot_result = _FakeStatus()
    await host.configure(_config(enabled=False, root_dir=tmp_path))
    old_yaml = host._config_path.read_bytes()
    old_config = host._config
    old_stored = host._stored_config
    core.activate_error = RuntimeError("start failed")

    with pytest.raises(PersonalContext.Error):
        await host.set_collection_enabled(True)

    assert host._config == old_config
    assert host._stored_config == old_stored
    assert host._config_path.read_bytes() == old_yaml


@pytest.mark.asyncio
async def test_runtime_operations_reject_unconfigured_host(
    fake_host: tuple[PersonalContextHostAPI, FakeCore],
) -> None:
    host, _core = fake_host

    with pytest.raises(PersonalContext.Error):
        await host.patch_runtime_config({"strategy_profile": "rules"})
    with pytest.raises(PersonalContext.Error):
        await host.select_model(0)


@pytest.mark.asyncio
async def test_list_fetch_services_returns_configuration_without_runtime_state(
    fake_host: tuple[PersonalContextHostAPI, FakeCore],
    tmp_path: Path,
) -> None:
    host, core = fake_host
    await host.configure(_config(enabled=False, root_dir=tmp_path))
    core.snapshot_result = SimpleNamespace(
        fetch_service_states={"local-notes": "RUNNING"},
        fetch_service_errors={"local-notes": "last failure"},
    )

    services = await host.list_fetch_services()

    assert services == [
        {
            "service_id": "local-notes",
            "provider": "local_files",
            "enabled": True,
            "interval_seconds": 60.0,
            "max_items_per_run": None,
            "time_range": {"mode": "all"},
            "source": {"root_dir": str(tmp_path)},
            "credentials": {},
        }
    ]
    assert core.calls[-1] != ("snapshot", None)


@pytest.mark.asyncio
async def test_create_fetch_service_normalizes_and_publishes_yaml_immediately(
    fake_host: tuple[PersonalContextHostAPI, FakeCore],
    tmp_path: Path,
) -> None:
    host, _core = fake_host
    await host.configure(_config(enabled=False, root_dir=tmp_path))

    created = await host.create_fetch_service(
        {
            **_local_service(" local-created ", tmp_path),
            "provider": " LOCAL_FILES ",
        }
    )

    assert created["service_id"] == "local-created"
    assert created["provider"] == "local_files"
    assert created["time_range"] == {"mode": "all"}
    saved = yaml.safe_load(host._config_path.read_text(encoding="utf-8"))
    assert [service["service_id"] for service in saved["fetch_services"]] == [
        "local-created",
        "local-notes",
    ]


@pytest.mark.asyncio
async def test_create_and_patch_fetch_service_persist_normalized_time_range(
    fake_host: tuple[PersonalContextHostAPI, FakeCore],
    tmp_path: Path,
) -> None:
    host, _core = fake_host
    await host.configure(_config(enabled=False, root_dir=tmp_path))
    service = _bookmark_service("recent-bookmarks")
    service["time_range"] = {"mode": "recent", "recent_days": 3}

    created = await host.create_fetch_service(service)
    patched = await host.patch_fetch_service(
        "recent-bookmarks",
        {
            "time_range": {
                "mode": "fixed",
                "start_at": "2026-08-01T00:00:00+08:00",
                "end_at": "2026-08-11T00:00:00+08:00",
            }
        },
    )

    assert created["time_range"] == {"mode": "recent", "recent_days": 3}
    assert patched["time_range"] == {
        "mode": "fixed",
        "start_at": "2026-07-31T16:00:00Z",
        "end_at": "2026-08-10T16:00:00Z",
    }
    saved = yaml.safe_load(host._config_path.read_text(encoding="utf-8"))
    saved_service = next(
        item
        for item in saved["fetch_services"]
        if item["service_id"] == "recent-bookmarks"
    )
    assert saved_service["time_range"] == patched["time_range"]


@pytest.mark.asyncio
async def test_create_fetch_service_rejects_duplicate_and_unknown_provider(
    fake_host: tuple[PersonalContextHostAPI, FakeCore],
    tmp_path: Path,
) -> None:
    host, _core = fake_host
    await host.configure(_config(enabled=False, root_dir=tmp_path))
    before = host._config_path.read_bytes()

    with pytest.raises(PersonalContext.Error, match="already exists"):
        await host.create_fetch_service(_local_service("local-notes", tmp_path))
    unknown = _local_service("unknown-provider", tmp_path)
    unknown["provider"] = "unknown"
    with pytest.raises(PersonalContext.Error):
        await host.create_fetch_service(unknown)

    assert host._config_path.read_bytes() == before


@pytest.mark.asyncio
async def test_create_fetch_service_enforces_provider_limit_independently(
    fake_host: tuple[PersonalContextHostAPI, FakeCore],
    tmp_path: Path,
) -> None:
    host, _core = fake_host
    services = [_local_service(f"local-{index:02d}", tmp_path) for index in range(20)]
    await host.configure(_config_with_services(services))
    before = host._config_path.read_bytes()

    with pytest.raises(
        PersonalContext.Error,
        match="local_files fetch service limit of 20 has been reached",
    ):
        await host.create_fetch_service(_local_service("local-20", tmp_path))

    assert host._config_path.read_bytes() == before
    created = await host.create_fetch_service(_bookmark_service("bookmarks-00"))
    assert created["provider"] == "browser_bookmarks"


@pytest.mark.asyncio
async def test_host_configure_cannot_bypass_per_provider_limit(
    fake_host: tuple[PersonalContextHostAPI, FakeCore],
    tmp_path: Path,
) -> None:
    host, core = fake_host
    services = [_local_service(f"local-{index:02d}", tmp_path) for index in range(21)]

    with pytest.raises(PersonalContext.Error):
        await host.configure(_config_with_services(services))

    assert core.calls == []
    assert host._config is None
    assert host._stored_config is None
    assert not host._config_path.exists()


@pytest.mark.asyncio
async def test_create_fetch_service_apply_failure_restores_yaml_memory_and_core(
    fake_host: tuple[PersonalContextHostAPI, FakeCore],
    tmp_path: Path,
) -> None:
    host, core = fake_host
    await host.configure(_config(enabled=False, root_dir=tmp_path))
    old_yaml = host._config_path.read_bytes()
    old_config = host._config
    old_stored = deepcopy(host._stored_config)
    core.set_error = RuntimeError("candidate set failed")

    with pytest.raises(PersonalContext.Error):
        await host.create_fetch_service(_bookmark_service("bookmarks-00"))

    assert host._config is old_config
    assert host._stored_config == old_stored
    assert core.configured == old_config
    assert host._config_path.read_bytes() == old_yaml


@pytest.mark.asyncio
async def test_create_fetch_service_cancellation_restores_yaml_memory_and_core(
    fake_host: tuple[PersonalContextHostAPI, FakeCore],
    tmp_path: Path,
) -> None:
    host, core = fake_host
    await host.configure(_config(enabled=False, root_dir=tmp_path))
    old_yaml = host._config_path.read_bytes()
    old_config = host._config
    old_stored = deepcopy(host._stored_config)
    core.set_error = asyncio.CancelledError()

    task = asyncio.create_task(
        host.create_fetch_service(_bookmark_service("bookmarks-00"))
    )
    with pytest.raises(asyncio.CancelledError):
        await task

    assert task.cancelled()
    assert host._config is old_config
    assert host._stored_config == old_stored
    assert core.configured == old_config
    assert host._config_path.read_bytes() == old_yaml


@pytest.mark.asyncio
async def test_delete_fetch_service_rejects_missing_service_without_changes(
    fake_host: tuple[PersonalContextHostAPI, FakeCore],
    tmp_path: Path,
) -> None:
    host, core = fake_host
    await host.configure(_config(enabled=False, root_dir=tmp_path))
    before = host._config_path.read_bytes()
    core.calls.clear()

    with pytest.raises(
        PersonalContext.Error, match="unknown PersonalContext fetch service"
    ):
        await host.delete_fetch_service("missing")

    assert host._config_path.read_bytes() == before
    assert core.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["STARTING", "RUNNING", "STOPPING", "FAILED", None])
async def test_delete_fetch_service_rejects_service_until_explicitly_stopped(
    fake_host: tuple[PersonalContextHostAPI, FakeCore],
    tmp_path: Path,
    state: str | None,
) -> None:
    host, core = fake_host
    await host.configure(_config(enabled=False, root_dir=tmp_path))
    before = host._config_path.read_bytes()
    old_config = host._config
    old_stored = deepcopy(host._stored_config)
    old_core_config = core.configured
    core.cursor_payloads["local-notes"] = b"old-cursor"
    core.snapshot_result = SimpleNamespace(
        state="CONFIGURED",
        fetch_service_states={"local-notes": state} if state is not None else {},
    )
    core.calls.clear()

    with pytest.raises(PersonalContext.Error, match="正在执行|请先停止"):
        await host.delete_fetch_service("local-notes")

    assert host._config_path.read_bytes() == before
    assert host._config is old_config
    assert host._stored_config == old_stored
    assert core.configured is old_core_config
    assert core.cursor_payloads["local-notes"] == b"old-cursor"
    assert core.calls == [("snapshot", None)]


@pytest.mark.asyncio
async def test_delete_fetch_service_removes_config_cursor_and_preserves_context(
    fake_host: tuple[PersonalContextHostAPI, FakeCore],
    tmp_path: Path,
) -> None:
    host, core = fake_host
    await host.configure(_config(enabled=False, root_dir=tmp_path))
    context_page = host._home / "workspace" / "context" / "description.md"
    source_meta = host._home / "workspace" / "source-meta" / "source.md"
    context_page.parent.mkdir(parents=True)
    source_meta.parent.mkdir(parents=True)
    context_page.write_text("# retained context\n", encoding="utf-8")
    source_meta.write_text("# retained source\n", encoding="utf-8")
    core.cursor_payloads["local-notes"] = b"old-cursor"
    core.snapshot_result = SimpleNamespace(
        state="CONFIGURED",
        fetch_service_states={"local-notes": "STOPPED"},
    )
    core.calls.clear()

    await host.delete_fetch_service(" local-notes ")

    saved = yaml.safe_load(host._config_path.read_text(encoding="utf-8"))
    assert saved["fetch_services"] == []
    assert host._stored_config is not None
    assert host._stored_config["fetch_services"] == []
    assert "local-notes" not in core.cursor_payloads
    assert context_page.read_text(encoding="utf-8") == "# retained context\n"
    assert source_meta.read_text(encoding="utf-8") == "# retained source\n"
    assert [name for name, _value in core.calls].count("snapshot") == 1
    assert [name for name, _value in core.calls].count("remove_fetch_cursor") == 1
    assert [name for name, _value in core.calls].count("restore_fetch_cursor") == 0


@pytest.mark.asyncio
async def test_delete_fetch_service_releases_provider_slot_and_recreation_has_no_cursor(
    fake_host: tuple[PersonalContextHostAPI, FakeCore],
    tmp_path: Path,
) -> None:
    host, core = fake_host
    services = [_local_service(f"local-{index:02d}", tmp_path) for index in range(20)]
    await host.configure(_config_with_services(services))
    core.cursor_payloads["local-00"] = b"old-cursor"
    core.snapshot_result = SimpleNamespace(
        state="CONFIGURED",
        fetch_service_states={service["service_id"]: "STOPPED" for service in services},
    )

    await host.delete_fetch_service("local-00")
    recreated = await host.create_fetch_service(_local_service("local-00", tmp_path))

    assert recreated["service_id"] == "local-00"
    assert "local-00" not in core.cursor_payloads
    assert not any(name == "restore_fetch_cursor" for name, _value in core.calls)


@pytest.mark.asyncio
async def test_delete_fetch_service_cursor_remove_failure_keeps_configuration(
    fake_host: tuple[PersonalContextHostAPI, FakeCore],
    tmp_path: Path,
) -> None:
    host, core = fake_host
    await host.configure(_config(enabled=False, root_dir=tmp_path))
    old_config = host._config
    old_stored = deepcopy(host._stored_config)
    old_yaml = host._config_path.read_bytes()
    core.snapshot_result = SimpleNamespace(
        state="CONFIGURED",
        fetch_service_states={"local-notes": "STOPPED"},
    )
    core.remove_cursor_error = RuntimeError("sensitive cursor remove failure")

    with pytest.raises(PersonalContext.Error) as caught:
        await host.delete_fetch_service("local-notes")

    assert "sensitive cursor remove failure" not in str(caught.value)
    assert host._config is old_config
    assert host._stored_config == old_stored
    assert host._config_path.read_bytes() == old_yaml


@pytest.mark.asyncio
async def test_delete_fetch_service_apply_failure_restores_cursor_and_config(
    fake_host: tuple[PersonalContextHostAPI, FakeCore],
    tmp_path: Path,
) -> None:
    host, core = fake_host
    await host.configure(_config(enabled=False, root_dir=tmp_path))
    old_config = host._config
    old_stored = deepcopy(host._stored_config)
    old_yaml = host._config_path.read_bytes()
    core.cursor_payloads["local-notes"] = b"old-cursor"
    core.snapshot_result = SimpleNamespace(
        state="CONFIGURED",
        fetch_service_states={"local-notes": "STOPPED"},
    )
    core.set_error = RuntimeError("candidate set failure")

    with pytest.raises(PersonalContext.Error):
        await host.delete_fetch_service("local-notes")

    assert core.cursor_payloads["local-notes"] == b"old-cursor"
    assert host._config is old_config
    assert host._stored_config == old_stored
    assert host._config_path.read_bytes() == old_yaml


@pytest.mark.asyncio
async def test_delete_fetch_service_cancellation_restores_cursor_and_propagates(
    fake_host: tuple[PersonalContextHostAPI, FakeCore],
    tmp_path: Path,
) -> None:
    host, core = fake_host
    await host.configure(_config(enabled=False, root_dir=tmp_path))
    old_config = host._config
    old_stored = deepcopy(host._stored_config)
    old_yaml = host._config_path.read_bytes()
    core.cursor_payloads["local-notes"] = b"old-cursor"
    core.snapshot_result = SimpleNamespace(
        state="CONFIGURED",
        fetch_service_states={"local-notes": "STOPPED"},
    )
    core.set_error = asyncio.CancelledError()

    task = asyncio.create_task(host.delete_fetch_service("local-notes"))
    with pytest.raises(asyncio.CancelledError):
        await task

    assert task.cancelled()
    assert core.cursor_payloads["local-notes"] == b"old-cursor"
    assert host._config is old_config
    assert host._stored_config == old_stored
    assert host._config_path.read_bytes() == old_yaml


@pytest.mark.asyncio
async def test_delete_fetch_service_cancellation_wins_when_cursor_restore_fails(
    fake_host: tuple[PersonalContextHostAPI, FakeCore],
    tmp_path: Path,
) -> None:
    host, core = fake_host
    await host.configure(_config(enabled=True, root_dir=tmp_path))
    old_config = host._config
    old_stored = deepcopy(host._stored_config)
    old_yaml = host._config_path.read_bytes()
    core.cursor_payloads["local-notes"] = b"old-cursor"
    core.snapshot_result = SimpleNamespace(
        state="RUNNING",
        fetch_service_states={"local-notes": "STOPPED"},
    )
    core.deactivate_started = asyncio.Event()
    core.deactivate_release = asyncio.Event()
    core.restore_cursor_error = RuntimeError("sensitive cursor restore failure")

    task = asyncio.create_task(host.delete_fetch_service("local-notes"))
    try:
        await asyncio.wait_for(core.deactivate_started.wait(), timeout=1.0)
        task.cancel()
        core.deactivate_release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        core.deactivate_release.set()
        if not task.done():
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError, PersonalContext.Error):
            await task

    assert task.cancelled()
    assert "local-notes" not in core.cursor_payloads
    assert host._config is old_config
    assert host._stored_config == old_stored
    assert host._config_path.read_bytes() == old_yaml


@pytest.mark.asyncio
async def test_delete_fetch_service_restore_failure_is_reported_explicitly(
    fake_host: tuple[PersonalContextHostAPI, FakeCore],
    tmp_path: Path,
) -> None:
    host, core = fake_host
    await host.configure(_config(enabled=False, root_dir=tmp_path))
    old_yaml = host._config_path.read_bytes()
    core.cursor_payloads["local-notes"] = b"old-cursor"
    core.snapshot_result = SimpleNamespace(
        state="CONFIGURED",
        fetch_service_states={"local-notes": "STOPPED"},
    )
    core.set_error = RuntimeError("candidate set failure")
    core.restore_cursor_error = RuntimeError("sensitive cursor restore failure")

    with pytest.raises(
        PersonalContext.Error,
        match="fetch cursor could not be restored",
    ) as caught:
        await host.delete_fetch_service("local-notes")

    assert "sensitive cursor restore failure" not in str(caught.value)
    assert "local-notes" not in core.cursor_payloads
    assert host._config_path.read_bytes() == old_yaml


@pytest.mark.asyncio
async def test_patch_existing_fetch_service_without_changing_identity(
    fake_host: tuple[PersonalContextHostAPI, FakeCore],
    tmp_path: Path,
) -> None:
    host, _core = fake_host
    await host.configure(_config(enabled=False, root_dir=tmp_path))

    result = await host.patch_fetch_service(
        "local-notes",
        {"interval_seconds": 10_800.0, "max_items_per_run": 50},
    )

    assert result["service_id"] == "local-notes"
    assert result["provider"] == "local_files"
    assert result["enabled"] is True
    assert result["interval_seconds"] == 10_800.0
    assert result["max_items_per_run"] == 50


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["service_id", "provider", "enabled"])
async def test_patch_fetch_service_rejects_identity_and_switch_fields(
    fake_host: tuple[PersonalContextHostAPI, FakeCore],
    tmp_path: Path,
    field: str,
) -> None:
    host, _core = fake_host
    await host.configure(_config(enabled=False, root_dir=tmp_path))
    before = host._config_path.read_bytes()

    with pytest.raises(PersonalContext.Error):
        await host.patch_fetch_service("local-notes", {field: "changed"})

    assert host._config_path.read_bytes() == before


@pytest.mark.asyncio
async def test_patch_fetch_service_never_creates_missing_service(
    fake_host: tuple[PersonalContextHostAPI, FakeCore],
    tmp_path: Path,
) -> None:
    host, _core = fake_host
    await host.configure(_config(enabled=False, root_dir=tmp_path))
    before = host._config_path.read_bytes()

    with pytest.raises(
        PersonalContext.Error, match="unknown PersonalContext fetch service"
    ):
        await host.patch_fetch_service(
            "new-service",
            {"interval_seconds": 10_800.0},
        )

    assert host._config_path.read_bytes() == before


@pytest.mark.asyncio
async def test_get_fetch_run_status_returns_all_or_one_service(
    fake_host: tuple[PersonalContextHostAPI, FakeCore],
    tmp_path: Path,
) -> None:
    host, core = fake_host
    await host.configure(_config(enabled=False, root_dir=tmp_path))
    core.snapshot_result = SimpleNamespace(
        fetch_run_progress={
            "local-notes": {
                "service_id": "local-notes",
                "run_state": "running",
                "progress_percent": 15,
                "total_items": 20,
                "completed_items": 3,
                "last_error": None,
            }
        },
    )

    all_status = await host.get_fetch_run_status()
    one_status = await host.get_fetch_run_status("local-notes")

    assert all_status == {
        "services": [
            {
                "service_id": "local-notes",
                "run_state": "running",
                "progress_percent": 15,
                "total_items": 20,
                "completed_items": 3,
                "last_error": None,
            }
        ]
    }
    assert one_status == {
        "service_id": "local-notes",
        "run_state": "running",
        "progress_percent": 15,
        "total_items": 20,
        "completed_items": 3,
        "last_error": None,
    }


@pytest.mark.asyncio
async def test_set_agent_use_enabled_updates_only_agent_use_switch(
    fake_host: tuple[PersonalContextHostAPI, FakeCore],
    tmp_path: Path,
) -> None:
    host, core = fake_host
    core.snapshot_result = _FakeStatus()
    await host.configure(_config(root_dir=tmp_path))
    core.calls.clear()

    await host.set_agent_use_enabled(False)

    assert host._config.agent_use_enabled is False
    saved = yaml.safe_load(
        (host._home / "personal_context.yaml").read_text(encoding="utf-8")
    )
    assert saved["agent_use_enabled"] is False
    assert core.calls == [("stop_agent_use", None)]


@pytest.mark.asyncio
async def test_set_fetch_service_enabled_updates_only_named_service(
    fake_host: tuple[PersonalContextHostAPI, FakeCore],
    tmp_path: Path,
) -> None:
    host, core = fake_host
    core.snapshot_result = _FakeStatus()
    await host.configure(_config(root_dir=tmp_path))

    await host.set_fetch_service_enabled("local-notes", False)

    assert host._config.collection_enabled is True
    assert host._config.agent_use_enabled is True
    assert host._config.fetch_services[0].enabled is False
    assert core.calls[-1] == (
        "set_fetch_service_enabled",
        ("local-notes", False),
    )


@pytest.mark.asyncio
async def test_set_fetch_service_enabled_rejects_unconfigured_or_unknown_service(
    fake_host: tuple[PersonalContextHostAPI, FakeCore],
) -> None:
    host, _core = fake_host
    with pytest.raises(PersonalContext.Error):
        await host.set_fetch_service_enabled("local-notes", False)
    with pytest.raises(PersonalContext.Error):
        await host.set_fetch_service_enabled("missing", False)


@pytest.mark.asyncio
async def test_run_fetch_delegates_without_modifying_yaml(
    fake_host: tuple[PersonalContextHostAPI, FakeCore],
    tmp_path: Path,
) -> None:
    host, core = fake_host
    await host.configure(_config(enabled=False, root_dir=tmp_path))
    config_path = host._home / "personal_context.yaml"
    saved = config_path.read_bytes()
    configured = host._config
    core.calls.clear()

    result = await host.run_fetch(service_id="local-notes")

    assert result == {
        "state": "accepted",
        "service_ids": ["local-notes"],
    }
    assert core.calls == [("run_fetch", "local-notes")]
    assert host._config is configured
    assert config_path.read_bytes() == saved


@pytest.mark.asyncio
async def test_run_fetch_is_serialized_with_configuration_operations(
    fake_host: tuple[PersonalContextHostAPI, FakeCore],
    tmp_path: Path,
) -> None:
    host, core = fake_host
    await host.configure(_config(enabled=False, root_dir=tmp_path))
    core.calls.clear()
    await host._operation_lock.acquire()

    task = asyncio.create_task(host.run_fetch())
    await asyncio.sleep(0)
    assert core.calls == []

    host._operation_lock.release()
    result = await task
    assert result == {
        "state": "accepted",
        "service_ids": ["local-notes"],
    }
    assert core.calls == [("run_fetch", None)]


@pytest.mark.asyncio
async def test_stop_calls_core_and_preserves_configuration(
    fake_host: tuple[PersonalContextHostAPI, FakeCore], tmp_path: Path
) -> None:
    host, core = fake_host
    await host.configure(_config(enabled=True, root_dir=tmp_path))
    saved = host._config_path.read_bytes()
    core.calls.clear()
    await host.stop(timeout_seconds=1.5)
    assert core.calls == [("deactivate_runtime", 1.5)]
    assert host._config is not None
    assert host._config_path.read_bytes() == saved
    assert yaml.safe_load(saved)["collection_enabled"] is True


@pytest.mark.asyncio
async def test_start_rejects_oversized_yaml_before_parsing_or_changing_core(
    fake_host: tuple[PersonalContextHostAPI, FakeCore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host, core = fake_host
    host._home.mkdir(parents=True)
    oversized = b"#" + b"x" * (4 * 1024 * 1024)
    host._config_path.write_bytes(oversized)
    parse_calls = 0

    def record_parse(_text: str) -> object:
        nonlocal parse_calls
        parse_calls += 1
        return {}

    monkeypatch.setattr(host_module.yaml, "safe_load", record_parse)

    with pytest.raises(PersonalContext.Error):
        await host.start()

    assert core.calls == []
    assert host._config is None
    assert host._stored_config is None
    assert host._config_path.read_bytes() == oversized
    assert parse_calls == 0


@pytest.mark.asyncio
async def test_start_bounds_read_when_opened_yaml_grows_after_path_checks(
    fake_host: tuple[PersonalContextHostAPI, FakeCore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host, core = fake_host
    host._home.mkdir(parents=True)
    host._config_path.write_text("enabled: false\n", encoding="utf-8")
    read_sizes: list[int] = []
    parse_calls = 0
    original_open = Path.open

    class GrowingHandle:
        def __init__(self, raw: BinaryIO) -> None:
            self._raw = raw

        def __enter__(self) -> GrowingHandle:
            self._raw.__enter__()
            return self

        def __exit__(
            self,
            exc_type: object,
            exc_value: object,
            traceback: object,
        ) -> object:
            return self._raw.__exit__(exc_type, exc_value, traceback)

        def fileno(self) -> int:
            return self._raw.fileno()

        def read(self, size: int = -1) -> bytes:
            read_sizes.append(size)
            return b"x" * (host_module._MAX_CONFIG_BYTES + 1)

    def growing_open(target: Path, *args: object, **kwargs: object) -> object:
        raw = original_open(target, *args, **kwargs)
        mode = kwargs.get("mode", args[0] if args else "r")
        if target == host._config_path and mode == "rb":
            return GrowingHandle(cast(BinaryIO, raw))
        return raw

    def record_parse(_text: str) -> object:
        nonlocal parse_calls
        parse_calls += 1
        return {}

    monkeypatch.setattr(Path, "open", growing_open)
    monkeypatch.setattr(host_module.yaml, "safe_load", record_parse)

    with pytest.raises(PersonalContext.Error):
        await host.start()

    assert read_sizes == [host_module._MAX_CONFIG_BYTES + 1]
    assert parse_calls == 0
    assert core.calls == []


@pytest.mark.asyncio
async def test_oversized_serialized_candidate_is_rejected_before_file_or_runtime(
    fake_host: tuple[PersonalContextHostAPI, FakeCore],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host, core = fake_host
    oversized_text = "x" * (4 * 1024 * 1024 + 1)
    monkeypatch.setattr(
        host_module.yaml,
        "safe_dump",
        lambda *_args, **_kwargs: oversized_text,
    )

    with pytest.raises(PersonalContext.Error):
        await host.configure(_config(enabled=False, root_dir=tmp_path))

    assert core.calls == []
    assert not host._home.exists()
    assert host._config is None
    assert host._stored_config is None


@pytest.mark.asyncio
async def test_missing_staged_yaml_is_reported_as_host_error(
    fake_host: tuple[PersonalContextHostAPI, FakeCore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host, core = fake_host
    monkeypatch.setattr(host_module, "_stage_yaml", lambda *_args: None)

    with pytest.raises(PersonalContext.Error) as caught:
        await host.set_collection_enabled(True)

    assert "staging" in str(caught.value).lower()
    assert core.active is False
    assert host._config is None
    assert not (host._home / "personal_context.yaml").exists()


@pytest.mark.asyncio
async def test_temporary_yaml_write_failure_does_not_call_core(
    fake_host: tuple[PersonalContextHostAPI, FakeCore],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host, core = fake_host

    def fail_temporary(*_args: object, **_kwargs: object) -> None:
        raise OSError("temporary write failed")

    monkeypatch.setattr(
        "jiuwenswarm.server.personal_context.host_api.tempfile.NamedTemporaryFile",
        fail_temporary,
    )
    with pytest.raises(Exception) as caught:
        await host.configure(_config(enabled=False, root_dir=tmp_path))
    assert core.calls == []
    assert caught.value.status.name == "CONTEXT_PROACTIVE_FILE_EXECUTION_ERROR"
    assert not (host._home / "personal_context.yaml").exists()


@pytest.mark.asyncio
async def test_previous_stop_failure_preserves_old_configuration(
    fake_host: tuple[PersonalContextHostAPI, FakeCore], tmp_path: Path
) -> None:
    host, core = fake_host
    old_config = _config(enabled=True, root_dir=tmp_path)
    await host.configure(old_config)
    old_core_config = host._config
    old_stored_config = host._stored_config
    old_yaml = (host._home / "personal_context.yaml").read_bytes()
    core.calls.clear()
    core.snapshot_result = SimpleNamespace(state="RUNNING")
    core.deactivate_error = RuntimeError("stop failed")
    with pytest.raises(Exception) as caught:
        await host.configure(_config(enabled=True, root_dir=tmp_path, interval=120.0))
    assert [name for name, _ in core.calls] == [
        "snapshot",
        "deactivate_runtime",
        "deactivate_runtime",
        "set_configuration",
        "activate_runtime",
    ]
    assert core.active is True
    assert host._config == old_core_config
    assert host._stored_config == old_stored_config
    assert (host._home / "personal_context.yaml").read_bytes() == old_yaml
    assert caught.value.status.name == "CONTEXT_PROACTIVE_STATE_INVALID"


@pytest.mark.asyncio
async def test_partial_previous_stop_failure_restores_active_runtime(
    fake_host: tuple[PersonalContextHostAPI, FakeCore], tmp_path: Path
) -> None:
    host, core = fake_host
    await host.configure(_config(enabled=True, root_dir=tmp_path))
    old_config = host._config
    old_stored = host._stored_config
    old_yaml = host._config_path.read_bytes()
    core.calls.clear()
    core.snapshot_result = SimpleNamespace(state="RUNNING")
    core.deactivate_changes_active_before_error = True
    core.deactivate_error = RuntimeError("stop failed after state change")

    with pytest.raises(PersonalContext.Error):
        await host.configure(_config(enabled=True, root_dir=tmp_path, interval=120.0))

    assert [name for name, _ in core.calls] == [
        "snapshot",
        "deactivate_runtime",
        "deactivate_runtime",
        "set_configuration",
        "activate_runtime",
    ]
    assert core.active is True
    assert host._config is old_config
    assert host._stored_config == old_stored
    assert host._config_path.read_bytes() == old_yaml


@pytest.mark.asyncio
async def test_partial_previous_stop_cancellation_restores_before_propagating(
    fake_host: tuple[PersonalContextHostAPI, FakeCore], tmp_path: Path
) -> None:
    host, core = fake_host
    await host.configure(_config(enabled=True, root_dir=tmp_path))
    old_config = host._config
    old_stored = host._stored_config
    old_yaml = host._config_path.read_bytes()
    core.calls.clear()
    core.snapshot_result = SimpleNamespace(state="RUNNING")
    core.deactivate_changes_active_before_error = True
    core.deactivate_error = asyncio.CancelledError()

    task = asyncio.create_task(
        host.configure(_config(enabled=True, root_dir=tmp_path, interval=120.0))
    )
    with pytest.raises(asyncio.CancelledError):
        await task

    assert task.cancelled()
    assert [name for name, _ in core.calls] == [
        "snapshot",
        "deactivate_runtime",
        "deactivate_runtime",
        "set_configuration",
        "activate_runtime",
    ]
    assert core.active is True
    assert host._config is old_config
    assert host._stored_config == old_stored
    assert host._config_path.read_bytes() == old_yaml


@pytest.mark.asyncio
async def test_set_failure_restores_old_configuration_and_active_runtime(
    fake_host: tuple[PersonalContextHostAPI, FakeCore], tmp_path: Path
) -> None:
    host, core = fake_host
    old_config = _config(enabled=True, root_dir=tmp_path)
    await host.configure(old_config)
    old_core_config = host._config
    old_stored_config = host._stored_config
    old_yaml = (host._home / "personal_context.yaml").read_bytes()
    core.calls.clear()
    core.snapshot_result = SimpleNamespace(state="RUNNING")
    core.set_error = RuntimeError("set failed")
    with pytest.raises(Exception) as caught:
        await host.configure(_config(enabled=True, root_dir=tmp_path, interval=120.0))
    assert [name for name, _ in core.calls] == [
        "snapshot",
        "deactivate_runtime",
        "set_configuration",
        "deactivate_runtime",
        "set_configuration",
        "activate_runtime",
    ]
    assert host._config == old_core_config
    assert host._stored_config == old_stored_config
    assert (host._home / "personal_context.yaml").read_bytes() == old_yaml
    assert caught.value.status.name == "CONTEXT_PROACTIVE_CONFIG_INVALID"


@pytest.mark.asyncio
async def test_replace_failure_restores_old_configuration_and_active_runtime(
    fake_host: tuple[PersonalContextHostAPI, FakeCore],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host, core = fake_host
    old_config = _config(enabled=True, root_dir=tmp_path)
    await host.configure(old_config)
    old_core_config = host._config
    old_stored_config = host._stored_config
    old_yaml = (host._home / "personal_context.yaml").read_bytes()
    core.calls.clear()
    core.snapshot_result = SimpleNamespace(state="RUNNING")

    def fail_replace(_source: object, _target: object) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(
        "jiuwenswarm.server.personal_context.host_api.os.replace", fail_replace
    )
    with pytest.raises(Exception) as caught:
        await host.configure(_config(enabled=True, root_dir=tmp_path, interval=120.0))
    assert [name for name, _ in core.calls] == [
        "snapshot",
        "deactivate_runtime",
        "set_configuration",
        "activate_runtime",
        "deactivate_runtime",
        "set_configuration",
        "activate_runtime",
    ]
    assert host._config == old_core_config
    assert host._stored_config == old_stored_config
    assert (host._home / "personal_context.yaml").read_bytes() == old_yaml
    assert caught.value.status.name == "CONTEXT_PROACTIVE_FILE_EXECUTION_ERROR"


@pytest.mark.asyncio
async def test_new_start_failure_keeps_host_unconfigured(
    fake_host: tuple[PersonalContextHostAPI, FakeCore], tmp_path: Path
) -> None:
    host, core = fake_host
    core.activate_error = RuntimeError("start failed")
    with pytest.raises(Exception) as caught:
        await host.configure(_config(root_dir=tmp_path))
    assert host._config is None
    assert host._stored_config is None
    assert not (host._home / "personal_context.yaml").exists()
    assert caught.value.status.name == "CONTEXT_PROACTIVE_STATE_INVALID"


@pytest.mark.asyncio
async def test_stop_rejects_non_positive_timeout_without_calling_core(
    fake_host: tuple[PersonalContextHostAPI, FakeCore],
) -> None:
    host, core = fake_host
    with pytest.raises(Exception) as caught:
        await host.stop(timeout_seconds=0)
    assert core.calls == []
    assert caught.value.status.name == "CONTEXT_PROACTIVE_RUNTIME_TIMEOUT"


@pytest.mark.asyncio
async def test_concurrent_operations_are_serialized(
    fake_host: tuple[PersonalContextHostAPI, FakeCore], tmp_path: Path
) -> None:
    host, core = fake_host
    started = asyncio.Event()
    release = asyncio.Event()
    core.deactivate_started = started
    core.deactivate_release = release
    await host.configure(_config(enabled=False, root_dir=tmp_path))
    task = asyncio.create_task(host.stop())
    configure_task: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(started.wait(), timeout=1.0)
        configure_task = asyncio.create_task(
            host.configure(_config(enabled=False, root_dir=tmp_path, interval=120.0))
        )
        await asyncio.sleep(0)
        assert not configure_task.done()
        release.set()
        await task
        await configure_task
    finally:
        release.set()
        pending = [task]
        if configure_task is not None:
            pending.append(configure_task)
        for pending_task in pending:
            if not pending_task.done():
                pending_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pending_task
    assert [name for name, _ in core.calls].count("deactivate_runtime") == 2
