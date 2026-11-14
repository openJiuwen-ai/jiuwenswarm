"""隔离 RSI 测试的插件目录，并保留同步测试使用的事件循环。"""

import asyncio
import inspect

import pytest

try:
    from jiuwenswarm.server.runtime import extension_package_manager as catalog
except ImportError:  # 插件运行时未随本构建迁移；仅依赖该 fixture 的用例跳过
    catalog = None


@pytest.fixture
def rsi_catalog_workspace(monkeypatch, tmp_path):
    if catalog is None:
        pytest.skip("extension_package_manager unavailable in this build")
    workspace = tmp_path / "agent-workspace"
    monkeypatch.setattr(catalog, "get_agent_workspace_dir", lambda: workspace)
    monkeypatch.setattr(catalog, "get_equipment_resources_plugin_packages_dir", lambda: None)
    return workspace


@pytest.fixture(autouse=True)
def preserve_pytest_loop_for_sync_runner_tests(request, monkeypatch):
    """asyncio.run must not orphan pytest-asyncio's idle Windows loop.

    Older sync integration tests intentionally use multiple independent runs.
    Restore an existing idle policy loop after each run, without creating a
    new loop, changing async tests, or suppressing resource warnings.
    """
    if inspect.iscoroutinefunction(request.function):
        return
    original_run = asyncio.run

    def run(awaitable, **kwargs):
        policy = asyncio.get_event_loop_policy()
        previous = getattr(getattr(policy, "_local", None), "_loop", None)
        try:
            return original_run(awaitable, **kwargs)
        finally:
            if previous is not None and not previous.is_closed():
                asyncio.set_event_loop(previous)

    monkeypatch.setattr(asyncio, "run", run)
