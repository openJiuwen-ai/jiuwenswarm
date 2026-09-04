# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for the AGENT_EXTRA_TOOLS non-invasive tool extension.

Covers: env/edition gating, per-module failure isolation, process-level
instance caching (register_tools() runs exactly once per module per process),
tool_cards integration with conflict and registration isolation, and the
per-session context contract for shared tool instances.
"""

from __future__ import annotations

import asyncio
import contextvars
import importlib
import logging
import pathlib
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from uuid import uuid4

import pytest

from jiuwenswarm.server.runtime.agent_adapter import interface_deep
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter


class _FakeCard:
    def __init__(self, name: str) -> None:
        self.name = name
        self.id = f"{name}_{uuid4().hex}"
        self.stateless = False


class _FakeTool:
    def __init__(self, name: str) -> None:
        self.card = _FakeCard(name)


# Customer-side module double: mimics a `@tool` module whose factory builds
# fresh instances on every call (card id = uuid4), so only the process-level
# cache can keep repeat builds idempotent.
_TOOL_MODULE_TEMPLATE = '''
from uuid import uuid4


class _Card:
    def __init__(self, name):
        self.name = name
        self.id = "{prefix}_" + name + "_" + uuid4().hex
        self.stateless = False


class _Tool:
    def __init__(self, name):
        self.card = _Card(name)


CALLS = 0


def register_tools():
    global CALLS
    CALLS += 1
    return [_Tool("psbc_system_tool"), _Tool("get_current_user")]
'''

_SINGLE_TOOL_MODULE = '''
from uuid import uuid4


class _Card:
    def __init__(self, name):
        self.name = name
        self.id = "single_" + name + "_" + uuid4().hex
        self.stateless = False


class _Tool:
    def __init__(self, name):
        self.card = _Card(name)


def register_tools():
    return _Tool("solo_tool")
'''

_RAISING_MODULE = '''
CALLS = 0


def register_tools():
    global CALLS
    CALLS += 1
    raise RuntimeError("factory exploded")
'''

_MIXED_MODULE = '''
from uuid import uuid4


class _Card:
    def __init__(self, name):
        self.name = name
        self.id = "mixed_" + (name or "nameless") + "_" + uuid4().hex
        self.stateless = False


class _Tool:
    def __init__(self, name):
        self.card = _Card(name)


def register_tools():
    return [
        _Tool("psbc_system_tool"),
        _Tool("wiki_query"),
        _Tool(""),
    ]
'''


def _write_tool_module(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    body: str,
) -> str:
    """Write an importable module into ``tmp_path`` and return its import name."""
    (tmp_path / f"{name}.py").write_text(body, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    return name


def _make_adapter() -> JiuWenSwarmDeepAdapter:
    """Create a bare adapter, mirroring the other deep-adapter unit tests."""
    return object.__new__(JiuWenSwarmDeepAdapter)


class _LogCapture:
    """Simple in-memory log capture for assertion checks."""

    def __init__(self) -> None:
        self.records: list[logging.LogRecord] = []

    def add_record(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    @property
    def text(self) -> str:
        return "\n".join(record.getMessage() for record in self.records)


def _make_log_capture() -> tuple[_LogCapture, logging.Handler]:
    """Create a custom log capture handler since pytest caplog doesn't capture
    in this project's logging configuration (propagate=False on the root)."""
    capture = _LogCapture()
    handler = logging.Handler()
    handler.emit = capture.add_record
    return capture, handler


@pytest.fixture(autouse=True)
def _reset_extra_tools_cache():
    """The process-level cache must not leak instances between tests."""
    JiuWenSwarmDeepAdapter._EXTRA_TOOLS_CACHE.clear()
    yield
    JiuWenSwarmDeepAdapter._EXTRA_TOOLS_CACHE.clear()


class _FakeResourceMgr:
    """In-memory double of ``Runner.resource_mgr`` (get_tool/add_tool contract)."""

    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}
        self.add_calls = 0

    def get_tool(self, tool_id: str) -> Any:
        return self.tools.get(tool_id)

    def add_tool(self, tool: Any, skip_if_exists: bool = False) -> None:
        self.add_calls += 1
        self.tools[tool.card.id] = tool
        return None


@pytest.fixture
def fake_resource_mgr(monkeypatch: pytest.MonkeyPatch) -> _FakeResourceMgr:
    from openjiuwen.core.runner import Runner

    mgr = _FakeResourceMgr()
    monkeypatch.setattr(Runner, "resource_mgr", mgr, raising=False)
    return mgr


@pytest.fixture
def enterprise(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(interface_deep, "is_enterprise", lambda: True)


# ---------------------------------------------------------------------------
# loader gating and per-module isolation
# ---------------------------------------------------------------------------


def test_not_enterprise_ignores_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """The env var only takes effect under the enterprise edition."""
    _write_tool_module(
        tmp_path, monkeypatch, "xtool_valid", _TOOL_MODULE_TEMPLATE.format(prefix="gating")
    )
    monkeypatch.setattr(interface_deep, "is_enterprise", lambda: False)
    monkeypatch.setenv("AGENT_EXTRA_TOOLS", "xtool_valid")

    assert JiuWenSwarmDeepAdapter._load_extra_tools_from_env() == []


@pytest.mark.parametrize("env_value", [None, "", "   "])
def test_empty_env_returns_empty(
    monkeypatch: pytest.MonkeyPatch,
    enterprise: None,
    env_value: str | None,
) -> None:
    """Missing, empty, and whitespace-only env values all load nothing."""
    if env_value is None:
        monkeypatch.delenv("AGENT_EXTRA_TOOLS", raising=False)
    else:
        monkeypatch.setenv("AGENT_EXTRA_TOOLS", env_value)

    assert JiuWenSwarmDeepAdapter._load_extra_tools_from_env() == []


def test_module_without_register_tools_is_skipped(
    monkeypatch: pytest.MonkeyPatch,
    enterprise: None,
    tmp_path: pathlib.Path,
) -> None:
    """A module without a register_tools() factory is skipped; the rest load."""
    _write_tool_module(tmp_path, monkeypatch, "xtool_no_factory", "PLACEHOLDER = 1\n")
    _write_tool_module(
        tmp_path, monkeypatch, "xtool_valid", _TOOL_MODULE_TEMPLATE.format(prefix="nofactory")
    )
    monkeypatch.setenv("AGENT_EXTRA_TOOLS", "xtool_no_factory;xtool_valid")

    tools = JiuWenSwarmDeepAdapter._load_extra_tools_from_env()

    assert [tool.card.name for tool in tools] == ["psbc_system_tool", "get_current_user"]


def test_single_tool_factory_result_is_wrapped(
    monkeypatch: pytest.MonkeyPatch,
    enterprise: None,
    tmp_path: pathlib.Path,
) -> None:
    """A factory returning a bare tool (not a list) is auto-wrapped."""
    monkeypatch.setenv(
        "AGENT_EXTRA_TOOLS",
        _write_tool_module(tmp_path, monkeypatch, "xtool_single", _SINGLE_TOOL_MODULE),
    )

    tools = JiuWenSwarmDeepAdapter._load_extra_tools_from_env()

    assert [tool.card.name for tool in tools] == ["solo_tool"]


def test_factory_exception_is_skipped_and_retried(
    monkeypatch: pytest.MonkeyPatch,
    enterprise: None,
    tmp_path: pathlib.Path,
) -> None:
    """A raising factory is isolated and — unlike success — not cached."""
    raising = _write_tool_module(tmp_path, monkeypatch, "xtool_raises", _RAISING_MODULE)
    _write_tool_module(
        tmp_path, monkeypatch, "xtool_valid", _TOOL_MODULE_TEMPLATE.format(prefix="raises")
    )
    monkeypatch.setenv("AGENT_EXTRA_TOOLS", "xtool_raises;xtool_valid")

    first = JiuWenSwarmDeepAdapter._load_extra_tools_from_env()
    second = JiuWenSwarmDeepAdapter._load_extra_tools_from_env()

    assert [tool.card.name for tool in first] == ["psbc_system_tool", "get_current_user"]
    assert second == first
    # Failures stay retryable per build: the factory ran on both loader calls.
    assert importlib.import_module(raising).CALLS == 2


# ---------------------------------------------------------------------------
# _get_tool_cards integration
# ---------------------------------------------------------------------------


def test_extra_tool_cards_enter_tool_cards_with_conflict_and_nameless_skip(
    monkeypatch: pytest.MonkeyPatch,
    enterprise: None,
    tmp_path: pathlib.Path,
    fake_resource_mgr: _FakeResourceMgr,
) -> None:
    """Extra cards join tool_cards; conflicts and nameless tools are skipped."""
    monkeypatch.setenv(
        "AGENT_EXTRA_TOOLS",
        _write_tool_module(tmp_path, monkeypatch, "xtool_mixed", _MIXED_MODULE),
    )
    adapter = _make_adapter()
    tool_cards: list[Any] = [_FakeCard("wiki_query")]
    capture, handler = _make_log_capture()
    adapter_logger = logging.getLogger(
        "jiuwenswarm.server.runtime.agent_adapter.interface_deep"
    )
    adapter_logger.addHandler(handler)
    try:
        adapter._append_extra_tool_cards(tool_cards)
    finally:
        adapter_logger.removeHandler(handler)

    # The conflicting "wiki_query" and the nameless tool never made it in.
    assert [card.name for card in tool_cards] == ["wiki_query", "psbc_system_tool"]
    assert len(fake_resource_mgr.tools) == 1
    registered = next(iter(fake_resource_mgr.tools.values()))
    assert registered.card.name == "psbc_system_tool"
    assert registered.card.stateless is True  # process-shared via mark_stateless
    assert "conflicts with existing tool" in capture.text
    assert "without card.name" in capture.text


# ---------------------------------------------------------------------------
# registration-stage isolation
# ---------------------------------------------------------------------------


def test_single_registration_failure_does_not_block_others(
    monkeypatch: pytest.MonkeyPatch,
    enterprise: None,
    tmp_path: pathlib.Path,
    fake_resource_mgr: _FakeResourceMgr,
) -> None:
    """One tool blowing up in _register_shared_tool must not fail the build."""
    monkeypatch.setenv(
        "AGENT_EXTRA_TOOLS",
        _write_tool_module(
            tmp_path, monkeypatch, "xtool_pair", _TOOL_MODULE_TEMPLATE.format(prefix="pair")
        ),
    )
    adapter = _make_adapter()
    original = JiuWenSwarmDeepAdapter._register_shared_tool

    def _flaky_register(tool: Any) -> Any:
        if tool.card.name == "psbc_system_tool":
            raise RuntimeError("resource_mgr exploded")
        return original(tool)

    monkeypatch.setattr(
        JiuWenSwarmDeepAdapter, "_register_shared_tool", staticmethod(_flaky_register)
    )

    tool_cards: list[Any] = []
    adapter._append_extra_tool_cards(tool_cards)  # must not raise

    assert [card.name for card in tool_cards] == ["get_current_user"]
    assert len(fake_resource_mgr.tools) == 1


# ---------------------------------------------------------------------------
# instance stability across repeat / concurrent builds
# ---------------------------------------------------------------------------


def test_repeat_and_concurrent_builds_share_cached_instances(
    monkeypatch: pytest.MonkeyPatch,
    enterprise: None,
    tmp_path: pathlib.Path,
) -> None:
    """register_tools() runs once per process; every build gets the same batch."""
    name = _write_tool_module(
        tmp_path, monkeypatch, "xtool_fresh", _TOOL_MODULE_TEMPLATE.format(prefix="fresh")
    )
    monkeypatch.setenv("AGENT_EXTRA_TOOLS", name)

    first = JiuWenSwarmDeepAdapter._load_extra_tools_from_env()
    second = JiuWenSwarmDeepAdapter._load_extra_tools_from_env()

    # The factory builds fresh instances per call; only the cache makes the
    # second build identical instead of a new-registration leak.
    assert second == first
    assert importlib.import_module(name).CALLS == 1

    with ThreadPoolExecutor(max_workers=8) as pool:
        batches = list(
            pool.map(lambda _: JiuWenSwarmDeepAdapter._load_extra_tools_from_env(), range(8))
        )

    assert all(batch == first for batch in batches)
    assert importlib.import_module(name).CALLS == 1


def test_two_session_adapters_register_the_tool_once(
    monkeypatch: pytest.MonkeyPatch,
    enterprise: None,
    tmp_path: pathlib.Path,
    fake_resource_mgr: _FakeResourceMgr,
) -> None:
    """A second session-scoped adapter must not re-register the shared tools."""
    monkeypatch.setenv(
        "AGENT_EXTRA_TOOLS",
        _write_tool_module(
            tmp_path, monkeypatch, "xtool_shared", _TOOL_MODULE_TEMPLATE.format(prefix="shared")
        ),
    )
    first = _make_adapter()
    second = _make_adapter()
    cards_first: list[Any] = []
    cards_second: list[Any] = []

    first._append_extra_tool_cards(cards_first)
    second._append_extra_tool_cards(cards_second)

    # Two tools registered once each; the second adapter's ensure_tool_registered
    # hit the existing ids instead of adding again.
    assert fake_resource_mgr.add_calls == 2
    assert [card.id for card in cards_second] == [card.id for card in cards_first]


# ---------------------------------------------------------------------------
# per-session context contract for shared instances
# ---------------------------------------------------------------------------


_CURRENT_USER: contextvars.ContextVar[str] = contextvars.ContextVar("current_user")


@pytest.mark.asyncio
async def test_shared_tool_reads_per_session_context_isolation() -> None:
    """Contract regression: per-session data only via contextvars.

    One shared tool instance serves concurrent sessions; a contextvar keeps
    each session's value isolated. Module-level mutable state would conflate
    alice and bob here.
    """

    async def get_current_user() -> str:
        return _CURRENT_USER.get()

    async def session(user: str) -> str:
        _CURRENT_USER.set(user)
        await asyncio.sleep(0.01)  # interleave with the other session
        return await get_current_user()

    results = await asyncio.gather(session("alice"), session("bob"))

    assert results == ["alice", "bob"]
