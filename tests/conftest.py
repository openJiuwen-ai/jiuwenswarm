# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Pytest configuration and shared fixtures."""

import inspect
import sys
import tempfile
import warnings
from importlib import import_module, invalidate_caches
from pathlib import Path
from types import ModuleType
from typing import Generator

import pytest


def _prefer_current_checkout() -> None:
    """Keep an editable install from another worktree out of this test run."""
    repo_root = Path(__file__).resolve().parent.parent
    package_root = (repo_root / "jiuwenswarm").resolve()
    repo_root_s = str(repo_root)
    if not sys.path or sys.path[0] != repo_root_s:
        sys.path.insert(0, repo_root_s)

    stale_checkout_loaded = False
    for name, module in list(sys.modules.items()):
        if name != "jiuwenswarm" and not name.startswith("jiuwenswarm."):
            continue
        loaded_file = getattr(module, "__file__", None)
        if loaded_file is not None and not Path(loaded_file).resolve().is_relative_to(
            package_root
        ):
            stale_checkout_loaded = True
            break
    if not stale_checkout_loaded:
        return

    stale_modules = [
        name
        for name in sys.modules
        if name == "jiuwenswarm" or name.startswith("jiuwenswarm.")
    ]
    for name in sorted(stale_modules, key=lambda item: item.count("."), reverse=True):
        sys.modules.pop(name, None)
    invalidate_caches()


_prefer_current_checkout()

install_evolution_rail_kwargs_compat = import_module(
    "jiuwenswarm.common.openjiuwen_rail_compat"
).install_evolution_rail_kwargs_compat


def _install_missing_trajectory_processor_stub() -> None:
    """CI openjiuwen may lack TrajectorySpanProcessor; stub it for collection/runtime."""
    try:
        import_module("openjiuwen.agent_evolving.trajectory.processor")
        return
    except ModuleNotFoundError:
        pass

    from opentelemetry.sdk.trace import SpanProcessor

    module = ModuleType("openjiuwen.agent_evolving.trajectory.processor")

    class TrajectorySpanProcessor(SpanProcessor):
        def on_start(self, span, parent_context=None):
            return None

        def on_end(self, span):
            return None

        def shutdown(self):
            return None

        def force_flush(self, timeout_millis: int = 30000) -> bool:
            del timeout_millis
            return True

        def subscribe(self, *args, **kwargs):
            del args, kwargs
            return object()

        def unsubscribe(self, *args, **kwargs):
            del args, kwargs
            return None

        def drain(self, *args, **kwargs):
            del args, kwargs
            return None, ()

        def suppress(self):
            from contextlib import nullcontext

            return nullcontext()

    module.TrajectorySpanProcessor = TrajectorySpanProcessor
    sys.modules["openjiuwen.agent_evolving.trajectory.processor"] = module


def _ensure_module(name: str) -> ModuleType:
    """Return an existing or newly registered module by dotted name."""
    existing = sys.modules.get(name)
    if existing is not None:
        return existing
    module = ModuleType(name)
    parent_name, _, child = name.rpartition(".")
    if parent_name:
        parent = _ensure_module(parent_name)
        setattr(parent, child, module)
    sys.modules[name] = module
    return module


def _install_missing_extensions_observability_stub() -> None:
    """CI openjiuwen may lack extensions.observability; stub imports used by root spans."""
    try:
        import_module("openjiuwen.extensions.observability.setup")
        import_module("openjiuwen.extensions.observability.semconv")
        import_module("openjiuwen.extensions.observability.span_context")
        return
    except ModuleNotFoundError:
        pass

    _ensure_module("openjiuwen.extensions")
    package = _ensure_module("openjiuwen.extensions.observability")
    package.__path__ = []  # mark as package for nested imports

    setup = _ensure_module("openjiuwen.extensions.observability.setup")

    def _get_tracer(name: str):
        from openjiuwen.agent_teams import observability as core_obs

        return core_obs.get_tracer(name)

    def _is_initialized() -> bool:
        from openjiuwen.agent_teams import observability as core_obs

        return bool(core_obs.is_initialized())

    setup.get_tracer = _get_tracer
    setup.is_initialized = _is_initialized

    semconv = _ensure_module("openjiuwen.extensions.observability.semconv")
    semconv.LANGFUSE_SESSION_ID = "langfuse.session.id"

    span_context = _ensure_module("openjiuwen.extensions.observability.span_context")
    span_context.set_current_session_id = lambda session_id=None: None
    span_context.set_root_span = lambda span, session_id=None, **kwargs: None
    span_context.clear_root_span = lambda: None


def _install_missing_openjiuwen_runtime_db_utils_stub() -> None:
    """CI may lack ``openjiuwen_runtime.foundation.db.utils``; stub DB-type predicates.

    Production deployments ship ``openjiuwen_runtime`` as the runtime foundation;
    CI test images may only install ``openjiuwen``. The gateway/adapter code lazily
    imports ``is_sqlite`` / ``is_mysql`` / ``is_postgresql`` from that module for
    DB-type branching (e.g. ``assert_replicas_db_compat``、checkpoint 选库). Stub
    them with the same normalization the callers already apply so unit tests that
    exercise these branches don't fail on ``ModuleNotFoundError``.
    """
    try:
        import_module("openjiuwen_runtime.foundation.db.utils")
        return
    except ModuleNotFoundError:
        pass

    utils = _ensure_module("openjiuwen_runtime.foundation.db.utils")
    # 标记父包，避免后续 ``from ...db.handler import ...`` 报
    # ``'openjiuwen_runtime.foundation.db' is not a package``。
    db_pkg = sys.modules.get("openjiuwen_runtime.foundation.db")
    if db_pkg is not None and not hasattr(db_pkg, "__path__"):
        db_pkg.__path__ = []  # type: ignore[attr-defined]
    if getattr(utils, "_jiuwenswarm_db_utils_stubbed", False):
        return

    _SQLITE_TYPES = frozenset({"sqlite", "aiosqlite"})
    _MYSQL_TYPES = frozenset({"mysql", "mariadb"})
    _POSTGRESQL_TYPES = frozenset({"postgresql", "postgres", "psql"})

    def _norm(value: object) -> str:
        return str(value or "").strip().lower()

    def is_sqlite(db_type: object) -> bool:
        return _norm(db_type) in _SQLITE_TYPES

    def is_mysql(db_type: object) -> bool:
        return _norm(db_type) in _MYSQL_TYPES

    def is_postgresql(db_type: object) -> bool:
        return _norm(db_type) in _POSTGRESQL_TYPES

    utils.is_sqlite = is_sqlite
    utils.is_mysql = is_mysql
    utils.is_postgresql = is_postgresql
    utils._jiuwenswarm_db_utils_stubbed = True  # type: ignore[attr-defined]


def _install_span_context_session_compat() -> None:
    """Older agent-core set_root_span has no session_id; keep telemetry tests working."""
    try:
        span_context = import_module("openjiuwen.extensions.observability.span_context")
    except ModuleNotFoundError:
        return

    if not hasattr(span_context, "set_current_session_id"):
        span_context.set_current_session_id = lambda session_id=None: None

    original = getattr(span_context, "set_root_span", None)
    if original is None:
        span_context.set_root_span = lambda span, session_id=None, **kwargs: None
        return

    def _set_root_span(span, session_id=None, **kwargs):
        try:
            return original(span, session_id=session_id, **kwargs)
        except TypeError:
            return original(span)

    span_context.set_root_span = _set_root_span


def _filter_unsupported_kwargs(func, kwargs: dict) -> dict:
    """Drop kwargs that ``func`` cannot accept unless it already takes **kwargs."""
    try:
        signature = inspect.signature(func)
    except (TypeError, ValueError):
        return kwargs
    if any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    ):
        return kwargs
    allowed = set(signature.parameters)
    return {key: value for key, value in kwargs.items() if key in allowed}


def _install_init_observability_kwargs_compat() -> None:
    """Older openjiuwen init_observability rejects additional_span_processors."""
    try:
        observability = import_module("openjiuwen.agent_teams.observability")
    except ModuleNotFoundError:
        return

    original = getattr(observability, "init_observability", None)
    if original is None or getattr(original, "_jiuwenswarm_kwargs_compat", False):
        return

    def _compat(config, *args, **kwargs):
        return original(config, *args, **_filter_unsupported_kwargs(original, kwargs))

    _compat._jiuwenswarm_kwargs_compat = True  # type: ignore[attr-defined]
    observability.init_observability = _compat


def pytest_configure() -> None:
    """Preload pysbd while suppressing only its known Python 3.12 escapes."""
    _install_missing_trajectory_processor_stub()
    _install_missing_extensions_observability_stub()
    _install_missing_openjiuwen_runtime_db_utils_stub()
    _install_span_context_session_compat()
    _install_init_observability_kwargs_compat()
    install_evolution_rail_kwargs_compat()
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message=r"invalid escape sequence '\\[.s]'",
            category=SyntaxWarning,
        )
        import_module("pysbd")


@pytest.fixture
def temp_workspace() -> Generator[Path, None, None]:
    """Create a temporary workspace directory for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir)
        # Create basic structure
        (workspace / "config").mkdir(parents=True, exist_ok=True)
        (workspace / "workspace").mkdir(parents=True, exist_ok=True)
        (workspace / "workspace" / "agent").mkdir(parents=True, exist_ok=True)
        (workspace / "workspace" / "agent" / "skills").mkdir(parents=True, exist_ok=True)
        (workspace / "logs").mkdir(parents=True, exist_ok=True)

        yield workspace


@pytest.fixture
def temp_config_file(temp_workspace: Path) -> Generator[Path, None, None]:
    """Create a temporary config.yaml file."""
    config_content = """
# Test configuration
model:
  provider: "test_provider"
  name: "test_model"
  api_base: "https://test.api.com"
  api_key: "${TEST_API_KEY:-default_key}"

channels:
  web:
    enabled: true

evolution:
  enabled: true
  skill_base_dir: "workspace/agent/skills"

heartbeat:
  every: "30 * * * *"
  target: "web"
"""
    config_file = temp_workspace / "config" / "config.yaml"
    config_file.write_text(config_content, encoding="utf-8")
    yield config_file


@pytest.fixture
def mock_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set up mock environment variables."""
    monkeypatch.setenv("MODEL_PROVIDER", "test_provider")
    monkeypatch.setenv("MODEL_NAME", "test_model")
    monkeypatch.setenv("API_BASE", "https://test.api.com")
    monkeypatch.setenv("API_KEY", "test_api_key")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")


@pytest.fixture
def sample_skill_md(temp_workspace: Path) -> Path:
    """Create a sample SKILL.md file."""
    skills_dir = temp_workspace / "workspace" / "agent" / "skills"
    skill_dir = skills_dir / "test-skill"
    skill_dir.mkdir(parents=True, exist_ok=True)

    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text("""---
name: test-skill
description: A test skill for unit testing
version: 1.0.0
author: Test Author
tags: [test]
---

# Test Skill

This is a test skill for unit testing purposes.

## Instructions

- Use this skill for testing
- Follow the examples below

## Examples

### Example 1: Basic usage

Input: "test"
Output: "test result"

## Troubleshooting

### Common issues

- Issue: Test fails
  Solution: Check configuration
""", encoding="utf-8")

    return skill_md


@pytest.fixture
def sample_messages():
    """Sample message list for testing signal detection."""
    return [
        {
            "role": "user",
            "content": "Help me with a task",
        },
        {
            "role": "assistant",
            "content": "I'll help you with that task",
            "tool_calls": [
                {
                    "name": "file.read",
                    "arguments": '{"file_path": "/path/to/test-skill/SKILL.md"}',
                }
            ],
        },
        {
            "role": "tool",
            "content": "Error: File not found",
            "name": "file.read",
        },
        {
            "role": "user",
            "content": "不对，应该这样做",
        },
    ]
