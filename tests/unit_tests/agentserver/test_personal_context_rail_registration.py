"""Agent mode registration uses the single Core PersonalContextRail."""

from __future__ import annotations

import ast
import asyncio
import io
import textwrap
import threading
import tokenize
from pathlib import Path

import pytest

from jiuwenswarm.server.runtime.agent_adapter import interface_deep
from jiuwenswarm.server.runtime.agent_adapter.interface import JiuWenSwarm
from jiuwenswarm.server.runtime.agent_manager import AgentManager
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
)


def _source(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def _parse_imports_without_recursion(source: str) -> ast.Module:
    """Parse import statements individually for old CPython versions.

    CPython 3.11.8 and earlier can fail while constructing the AST of a large
    module, even when the source is valid.  The import statements are small
    independent units, so parsing those units separately preserves the static
    check without exercising that interpreter limit.
    """
    lines = source.splitlines(keepends=True)
    body: list[ast.stmt] = []
    statement_start = True
    import_start_line: int | None = None
    import_start_column = 0
    bracket_depth = 0

    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if import_start_line is not None:
            if token.type == tokenize.OP:
                if token.string in "([{":
                    bracket_depth += 1
                elif token.string in ")]}":
                    bracket_depth -= 1
            if token.type == tokenize.NEWLINE and bracket_depth == 0:
                first_line = lines[import_start_line - 1][import_start_column:]
                continuation = "".join(lines[import_start_line : token.end[0]])
                snippet = textwrap.dedent(
                    first_line + continuation
                ).strip()
                body.extend(ast.parse(snippet).body)
                import_start_line = None
                statement_start = True
            continue

        if token.type in {
            tokenize.INDENT,
            tokenize.DEDENT,
            tokenize.NL,
            tokenize.COMMENT,
        }:
            continue
        if token.type == tokenize.NEWLINE:
            statement_start = True
            continue
        if token.type == tokenize.OP:
            if token.string in "([{":
                bracket_depth += 1
            elif token.string in ")]}":
                bracket_depth -= 1
            if token.string in {":", ";"} and bracket_depth == 0:
                statement_start = True
                continue
        if (
            statement_start
            and token.type == tokenize.NAME
            and token.string in {"from", "import"}
        ):
            import_start_line = token.start[0]
            import_start_column = token.start[1]
            bracket_depth = 0
            statement_start = False
            continue
        statement_start = False

    return ast.Module(body=body, type_ignores=[])


def _parse(source: str) -> ast.Module:
    """在全新线程中执行 ast.parse，规避 CPython gh-106905。

    Python 3.11.8 之前的版本存在已知 bug：调用点递归较深时（pytest 下约
    110 层），大文件 AST 构造会触发内部递归上限，且错误路径漏减计数器，
    最终把正常解析报成 ``SystemError: AST constructor recursion depth
    mismatch``。新线程从接近 0 的递归深度开始；若文件仍触发该错误，则
    逐条解析 import 语句作为兼容回退。
    """
    outcome: dict[str, object] = {}

    def _run() -> None:
        try:
            outcome["tree"] = ast.parse(source)
        except BaseException as exc:  # noqa: BLE001 - 原样传回主线程重新抛出
            outcome["error"] = exc

    worker = threading.Thread(target=_run, daemon=True)
    worker.start()
    worker.join()
    error = outcome.get("error")
    if isinstance(error, BaseException):
        if isinstance(error, SystemError) and "AST constructor recursion depth mismatch" in str(
            error
        ):
            return _parse_imports_without_recursion(source)
        raise error
    tree = outcome.get("tree")
    assert isinstance(tree, ast.Module)
    return tree


def test_deep_adapter_imports_core_personal_context_rail_only() -> None:
    module = (
        Path(__file__).parents[3]
        / "jiuwenswarm"
        / "server"
        / "runtime"
        / "agent_adapter"
        / "interface_deep.py"
    )
    tree = _parse(_source(str(module)))
    imports = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
    ]
    imported_names = {
        alias.name
        for node in imports
        for alias in (node.names if isinstance(node, ast.Import) else node.names)
    }

    assert "PersonalContextRail" in imported_names
    assert "ProactiveContextRail" not in imported_names


@pytest.mark.asyncio
async def test_agent_fast_and_plan_share_one_rail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._is_code_agent = False  # pylint: disable=protected-access
    adapter._instance = _FakeAgent()  # pylint: disable=protected-access
    adapter._personal_context_rail = None  # pylint: disable=protected-access
    adapter._personal_context_rail_lock = asyncio.Lock()  # pylint: disable=protected-access
    adapter._personal_context_runtime_enabled = True  # pylint: disable=protected-access

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_adapter.interface_deep.PersonalContextRail",
        _FakeRail,
        raising=False,
    )
    monkeypatch.setattr(
        adapter,
        "_personal_context_rail_enabled",
        lambda mode: mode in {"agent.fast", "agent.plan"},
    )

    await adapter._sync_personal_context_rail("agent.fast")  # pylint: disable=protected-access
    first = adapter._personal_context_rail  # pylint: disable=protected-access
    await adapter._sync_personal_context_rail("agent.plan")  # pylint: disable=protected-access

    assert first is adapter._personal_context_rail  # pylint: disable=protected-access
    assert adapter._instance.registered == [first]  # pylint: disable=protected-access


def _adapter(
    agent: _FakeAgent,
    *,
    runtime_enabled: bool = True,
) -> JiuWenSwarmDeepAdapter:
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._is_code_agent = False  # pylint: disable=protected-access
    adapter._instance = agent  # pylint: disable=protected-access
    adapter._personal_context_rail = None  # pylint: disable=protected-access
    adapter._personal_context_rail_lock = asyncio.Lock()  # pylint: disable=protected-access
    adapter._personal_context_runtime_enabled = runtime_enabled  # pylint: disable=protected-access
    adapter._last_mode = "agent"  # pylint: disable=protected-access
    adapter._is_session_scoped_adapter = True  # pylint: disable=protected-access
    adapter._session_adapters = {}  # pylint: disable=protected-access
    return adapter


@pytest.mark.asyncio
async def test_personal_context_rail_disabled_does_not_register() -> None:
    agent = _FakeAgent()
    adapter = _adapter(agent, runtime_enabled=False)

    await adapter._sync_personal_context_rail("agent")  # pylint: disable=protected-access

    assert adapter._personal_context_rail is None  # pylint: disable=protected-access
    assert agent.register_attempts == []


@pytest.mark.asyncio
async def test_personal_context_runtime_refresh_unregisters_and_reregisters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _FakeAgent()
    adapter = _adapter(agent, runtime_enabled=True)
    monkeypatch.setattr(interface_deep, "PersonalContextRail", _FakeRail)

    await adapter._sync_personal_context_rail("agent")  # pylint: disable=protected-access
    rail = adapter._personal_context_rail  # pylint: disable=protected-access
    assert rail is not None

    adapter.set_personal_context_runtime_enabled(False)
    await adapter.refresh_personal_context_rail()
    assert adapter._personal_context_rail is None  # pylint: disable=protected-access
    assert agent.unregister_attempts == [rail]

    adapter.set_personal_context_runtime_enabled(True)
    await adapter.refresh_personal_context_rail()
    assert adapter._personal_context_rail is not None  # pylint: disable=protected-access
    assert len(agent.register_attempts) == 2


@pytest.mark.asyncio
async def test_personal_context_runtime_refresh_reaches_session_adapters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _adapter(_FakeAgent(), runtime_enabled=False)
    root._is_session_scoped_adapter = False  # pylint: disable=protected-access
    child = _adapter(_FakeAgent(), runtime_enabled=False)
    child._last_mode = "agent"  # pylint: disable=protected-access
    root._session_adapters = {"session-1": child}  # pylint: disable=protected-access
    monkeypatch.setattr(interface_deep, "PersonalContextRail", _FakeRail)

    root.set_personal_context_runtime_enabled(True)
    await root.refresh_personal_context_rail()

    assert root._personal_context_rail is not None  # pylint: disable=protected-access
    assert child._personal_context_rail is not None  # pylint: disable=protected-access
    assert child._personal_context_runtime_enabled is True  # pylint: disable=protected-access


@pytest.mark.asyncio
async def test_personal_context_manager_broadcast_isolated_per_agent() -> None:
    first = _FakeManagedAgent()
    failed = _FakeManagedAgent(fail_refresh=True)
    manager = object.__new__(AgentManager)
    manager.agents = {"web": {"first": first, "failed": failed}}
    manager._personal_context_runtime_enabled = False  # pylint: disable=protected-access

    await manager.set_personal_context_runtime_enabled(True)

    assert manager._personal_context_runtime_enabled is True  # pylint: disable=protected-access
    assert first.enabled_values == [True]
    assert first.refresh_count == 1
    assert failed.enabled_values == [True]


@pytest.mark.asyncio
async def test_personal_context_facade_forwards_runtime_refresh() -> None:
    facade = object.__new__(JiuWenSwarm)
    adapter = _FakeManagedAgent()
    facade._adapter = adapter  # pylint: disable=protected-access
    facade._personal_context_runtime_enabled = False  # pylint: disable=protected-access

    facade.set_personal_context_runtime_enabled(True)
    await facade.refresh_personal_context_rail()

    assert facade._personal_context_runtime_enabled is True  # pylint: disable=protected-access
    assert adapter.enabled_values == [True]
    assert adapter.refresh_count == 1


@pytest.mark.asyncio
async def test_personal_context_rail_constructor_failure_is_fail_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _FakeAgent()
    adapter = _adapter(agent)

    def _failed_constructor(_home: str | Path) -> object:
        raise RuntimeError("construction failed")

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_adapter.interface_deep.PersonalContextRail",
        _failed_constructor,
    )

    await adapter._sync_personal_context_rail("agent.fast")  # pylint: disable=protected-access

    assert adapter._personal_context_rail is None  # pylint: disable=protected-access
    assert await agent.normal_request() == "ok"


@pytest.mark.asyncio
async def test_personal_context_rail_registration_and_cleanup_failure_is_fail_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _FakeAgent(fail_register=True, fail_unregister=True)
    adapter = _adapter(agent)
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_adapter.interface_deep.PersonalContextRail",
        _FakeRail,
    )

    await adapter._sync_personal_context_rail("agent.fast")  # pylint: disable=protected-access

    assert len(agent.register_attempts) == 1
    assert agent.unregister_attempts == agent.register_attempts
    assert adapter._personal_context_rail is None  # pylint: disable=protected-access
    assert await agent.normal_request() == "ok"


@pytest.mark.asyncio
async def test_personal_context_rail_unregister_failure_keeps_reference_and_does_not_reregister() -> (
    None
):
    rail = _FakeRail(Path("personal_context"))
    agent = _FakeAgent(fail_unregister=True)
    agent.registered.append(rail)
    adapter = _adapter(agent)
    adapter._personal_context_rail = rail  # pylint: disable=protected-access

    await adapter._sync_personal_context_rail("code.normal")  # pylint: disable=protected-access

    assert agent.unregister_attempts == [rail]
    assert adapter._personal_context_rail is rail  # pylint: disable=protected-access
    assert await agent.normal_request() == "ok"

    agent.fail_unregister = False
    await adapter._sync_personal_context_rail("agent")  # pylint: disable=protected-access

    assert adapter._personal_context_rail is rail  # pylint: disable=protected-access
    assert agent.registered == [rail]
    assert agent.register_attempts == []


@pytest.mark.asyncio
async def test_personal_context_rail_cancellation_still_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _FakeAgent(cancel_register=True)
    adapter = _adapter(agent)
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_adapter.interface_deep.PersonalContextRail",
        _FakeRail,
    )

    with pytest.raises(asyncio.CancelledError):
        await adapter._sync_personal_context_rail("agent.fast")  # pylint: disable=protected-access


class _FakeAgent:
    def __init__(
        self,
        *,
        fail_register: bool = False,
        fail_unregister: bool = False,
        cancel_register: bool = False,
    ) -> None:
        self.registered: list[object] = []
        self.register_attempts: list[object] = []
        self.unregister_attempts: list[object] = []
        self.fail_register = fail_register
        self.fail_unregister = fail_unregister
        self.cancel_register = cancel_register

    async def register_rail(self, rail: object) -> None:
        self.register_attempts.append(rail)
        self.registered.append(rail)
        if self.cancel_register:
            raise asyncio.CancelledError
        if self.fail_register:
            raise RuntimeError("register failed")

    async def unregister_rail(self, rail: object) -> None:
        self.unregister_attempts.append(rail)
        if self.fail_unregister:
            raise RuntimeError("unregister failed")
        if rail in self.registered:
            self.registered.remove(rail)

    async def normal_request(self) -> str:
        return "ok"


class _FakeRail:
    def __init__(self, home: str | Path) -> None:
        self.home = Path(home)

    def set_language(self, language: str) -> None:
        del language


class _FakeManagedAgent:
    def __init__(self, *, fail_refresh: bool = False) -> None:
        self.enabled_values: list[bool] = []
        self.refresh_count = 0
        self.fail_refresh = fail_refresh

    def set_personal_context_runtime_enabled(self, enabled: bool) -> None:
        self.enabled_values.append(enabled)

    async def refresh_personal_context_rail(self) -> None:
        self.refresh_count += 1
        if self.fail_refresh:
            raise RuntimeError("refresh failed")
