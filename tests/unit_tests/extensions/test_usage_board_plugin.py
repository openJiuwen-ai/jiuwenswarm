"""usage_board 插件层单元测试：注册、RPC 契约、参数校验、缓存、预算与路径限制。

数据根一律用 monkeypatch 换成 tmp_path：插件的 ``_data_dir()/_sessions_dir()`` 只是两个
普通方法，替换实例属性即可，既快又不会碰到真实家目录。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from jiuwenswarm.extensions.registry import ExtensionRegistry
from jiuwenswarm.extensions.usage_board import extension as plugin_module
from jiuwenswarm.extensions.usage_board import usage_engine
from jiuwenswarm.extensions.usage_board.extension import (
    BAD_REQUEST,
    CACHE_TTL_SECONDS,
    INTERNAL_ERROR,
    RPC_PREFIX,
    UsageBoardApplicationPlugin,
    _cache_is_fresh,
    register_extensions,
)

# RPC 层没有注入 now 的口子，_scan 用真实 time.time()；因此时间戳取「一小时前」，
# 既能落在默认 30 天窗口内，又远离自然日边界（改按固定 epoch 会被 days 过滤掉）。
TS = time.time() - 3600.0
DAY = usage_engine.day_of(TS)
RPC_METHODS = ("paths", "overview", "session", "export", "prices")


class _FakeChannel:
    """与宿主 tests/unit_tests/test_application_plugins.py 里的假通道保持一致。"""

    def __init__(self) -> None:
        self.methods: dict[str, Any] = {}
        self.local_only: set[str] = set()

    def register_method(self, method, handler, *, local_only=False) -> None:  # noqa: ANN001
        self.methods[method] = handler
        if local_only:
            self.local_only.add(method)

    async def send_response(self, ws, req_id, **payload) -> None:  # noqa: ANN001
        ws.append({"id": req_id, **payload})


def make_registry() -> ExtensionRegistry:
    return ExtensionRegistry(MagicMock(), {}, MagicMock())


def bind(plugin: UsageBoardApplicationPlugin) -> _FakeChannel:
    registry = make_registry()
    registry.register_application_plugin(plugin)
    channel = _FakeChannel()
    registry.bind_application_plugins(channel)
    return channel


async def call(
    channel: _FakeChannel,
    responses: list[dict[str, Any]],
    name: str,
    params: Any = None,
) -> dict[str, Any]:
    await channel.methods[RPC_PREFIX + name](responses, "req-1", params, "session-1")
    return responses[-1]


def make_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    config: dict[str, Any] | None = None,
    with_sessions: bool = True,
) -> tuple[UsageBoardApplicationPlugin, _FakeChannel, list[dict[str, Any]], Path, Path]:
    """造一个插件 + 假通道，数据根指向 tmp_path/home。"""

    data_root = tmp_path / "home"
    sessions_dir = data_root / "agent" / "sessions"
    if with_sessions:
        sessions_dir.mkdir(parents=True, exist_ok=True)
    plugin = UsageBoardApplicationPlugin()
    monkeypatch.setattr(plugin, "_data_dir", lambda: data_root)
    monkeypatch.setattr(plugin, "_sessions_dir", lambda: sessions_dir)
    if config is not None:
        plugin._config = config
    channel = bind(plugin)
    return plugin, channel, [], data_root, sessions_dir


# --------------------------------------------------------------------------
# 记录构造器：与 history.jsonl 的真实形状一致
# --------------------------------------------------------------------------


def summary_record(
    *,
    ts: float = TS,
    model: str | None = "gpt-5",
    input_tokens: int = 100,
    output_tokens: int = 50,
    total_tokens: int = 150,
    cost: float | None = None,
) -> dict[str, Any]:
    usage: dict[str, Any] = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
    }
    if cost is not None:
        usage["total_cost"] = cost
    return {
        "event_type": "chat.usage_summary",
        "session_id": "s1",
        "timestamp": ts,
        "model": model,
        "usage": usage,
    }


def context_record(
    *,
    ts: float = TS,
    limit: int = 2000,
    used: int = 1000,
    occupancy_rate: float = 0.5,
    model: str = "probe-model",
    provider: str = "openai",
) -> dict[str, Any]:
    return {
        "event_type": "context.usage",
        "timestamp": ts,
        "model": model,
        "provider": provider,
        "context_window": {
            "limit_tokens": limit,
            "input_tokens": used,
            "occupancy_rate": occupancy_rate,
        },
    }


def write_history(sessions_dir: Path, session_id: str, records: list[Any]) -> Path:
    folder = sessions_dir / session_id
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / usage_engine.HISTORY_FILENAME
    lines = [
        item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
        for item in records
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    # 让文件 mtime 与日志里最新的时间戳一致（引擎按 mtime 粗筛过期会话）。
    stamps = [
        float(record["timestamp"])
        for record in records
        if isinstance(record, dict)
        and isinstance(record.get("timestamp"), (int, float))
        and not isinstance(record.get("timestamp"), bool)
    ]
    if stamps:
        os.utime(path, (max(stamps), max(stamps)))
    return path


def write_prices(data_root: Path, payload: object) -> Path:
    folder = data_root / "usage_board"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "prices.json"
    if isinstance(payload, str):
        path.write_text(payload, encoding="utf-8")
    else:
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def default_prices_display() -> str:
    return str(Path("usage_board") / "prices.json")


def patch_budget(monkeypatch: pytest.MonkeyPatch, node: Any) -> None:
    import jiuwenswarm.common.config as config_module

    monkeypatch.setattr(config_module, "get_config", lambda: node)


# --------------------------------------------------------------------------
# 注册与元数据
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_extensions_registers_application_plugin() -> None:
    registry = make_registry()

    loaded = await register_extensions(registry)

    assert len(loaded) == 1
    plugin = loaded[0]
    assert isinstance(plugin, UsageBoardApplicationPlugin)
    assert plugin.plugin_id == "usage-board"
    assert registry.get_application_plugin("usage-board") is plugin


def test_metadata_matches_manifest() -> None:
    metadata = UsageBoardApplicationPlugin().metadata

    assert metadata.id == "usage-board"
    assert metadata.name == "Usage Board"
    assert metadata.version == "1.0.0"
    assert metadata.description
    assert metadata.author
    assert metadata.package_type == "application"


def test_frontend_contribution_is_iframe_page_with_existing_asset() -> None:
    plugin = UsageBoardApplicationPlugin()

    contributions = plugin.frontend_contributions()

    assert len(contributions) == 1
    contribution = contributions[0]
    assert contribution.id == "usage-board-page"
    assert contribution.nav_key == "app:usage-board"
    assert contribution.render_mode == "iframe"
    assert contribution.entrypoint == "index.html"
    assets = plugin.frontend_asset_root()
    assert assets is not None
    assert (assets / "index.html").is_file()


def test_plugin_is_enabled_by_default() -> None:
    assert UsageBoardApplicationPlugin().is_enabled() is True


@pytest.mark.asyncio
async def test_initialize_reads_config_and_logger() -> None:
    plugin = UsageBoardApplicationPlugin()
    logger = object()

    class _Config:
        config = {"prices_path": "usage_board/prices.json"}

    _Config.logger = logger  # type: ignore[attr-defined]

    await plugin.initialize(_Config())

    assert plugin._config == {"prices_path": "usage_board/prices.json"}
    assert plugin._logger is logger

    await plugin.initialize(None)
    assert plugin._config == {}


@pytest.mark.asyncio
async def test_shutdown_clears_cache() -> None:
    plugin = UsageBoardApplicationPlugin()
    plugin._cached = (0.0, (30, 50, 200), {"totals": {}})

    await plugin.shutdown()

    assert plugin._cached is None


def test_data_dir_and_sessions_dir_follow_host_utils(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jiuwenswarm.common import utils as host_utils

    root = tmp_path / "home"
    sessions = root / "agent" / "sessions"
    sessions.mkdir(parents=True)
    monkeypatch.setattr(host_utils, "get_user_workspace_dir", lambda: root)
    monkeypatch.setattr(host_utils, "get_agent_sessions_dir", lambda: sessions)
    plugin = UsageBoardApplicationPlugin()

    assert plugin._data_dir() == root
    assert plugin._sessions_dir() == sessions
    assert plugin._count_sessions(sessions) == 0


def test_display_path_shortens_paths_outside_the_data_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin, _channel, _responses, data_root, sessions_dir = make_env(tmp_path, monkeypatch)

    assert plugin._display_path(sessions_dir) == str(Path("agent") / "sessions")
    assert plugin._display_path(tmp_path / "elsewhere" / "prices.json") == "prices.json"
    assert str(data_root) not in plugin._display_path(sessions_dir)


def test_contract_constants_are_frozen() -> None:
    assert RPC_PREFIX == "usage.board."
    assert BAD_REQUEST == "BAD_REQUEST"
    assert INTERNAL_ERROR == "INTERNAL_ERROR"
    assert CACHE_TTL_SECONDS == 15.0


# --------------------------------------------------------------------------
# RPC 注册
# --------------------------------------------------------------------------


def test_bind_web_channel_registers_five_local_methods() -> None:
    channel = bind(UsageBoardApplicationPlugin())

    assert set(channel.methods) == {RPC_PREFIX + name for name in RPC_METHODS}
    assert channel.local_only == set(channel.methods)


@pytest.mark.asyncio
async def test_disabled_plugin_rpc_is_gated_by_host() -> None:
    plugin = UsageBoardApplicationPlugin()
    plugin.is_enabled = lambda: False  # type: ignore[method-assign]
    channel = bind(plugin)

    response = await call(channel, [], "paths")

    assert response["ok"] is False
    assert response["code"] == "APPLICATION_PLUGIN_DISABLED"


# --------------------------------------------------------------------------
# usage.board.paths
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_paths_payload_fields_and_relative_prices_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin, channel, responses, data_root, sessions_dir = make_env(tmp_path, monkeypatch)

    payload = (await call(channel, responses, "paths"))["payload"]

    assert payload["data_dir"] == str(data_root)
    assert payload["sessions_dir"] == str(sessions_dir)
    assert payload["session_count"] == 0
    assert payload["prices_path"] == default_prices_display()
    assert str(data_root) not in payload["prices_path"]
    assert payload["price_table_configured"] is False
    assert payload["price_table_error"] is None
    assert payload["cache_ttl_seconds"] == CACHE_TTL_SECONDS
    assert isinstance(payload["running_version"], str)
    assert plugin._cached is None


@pytest.mark.asyncio
async def test_paths_counts_only_sessions_that_have_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _plugin, channel, responses, _data_root, sessions_dir = make_env(tmp_path, monkeypatch)
    write_history(sessions_dir, "s1", [summary_record()])
    write_history(sessions_dir, "s2", [summary_record(ts=TS + 1)])
    (sessions_dir / "no-history").mkdir(parents=True)
    (sessions_dir / "a-file").write_text("x", encoding="utf-8")

    payload = (await call(channel, responses, "paths"))["payload"]

    assert payload["session_count"] == 2


@pytest.mark.asyncio
async def test_paths_tolerates_missing_sessions_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _plugin, channel, responses, _data_root, sessions_dir = make_env(
        tmp_path, monkeypatch, with_sessions=False
    )

    payload = (await call(channel, responses, "paths"))["payload"]

    assert payload["sessions_dir"] == str(sessions_dir)
    assert payload["session_count"] == 0


@pytest.mark.asyncio
async def test_paths_reports_broken_price_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _plugin, channel, responses, data_root, _sessions_dir = make_env(tmp_path, monkeypatch)
    write_prices(data_root, "{broken")

    payload = (await call(channel, responses, "paths"))["payload"]

    assert payload["price_table_configured"] is False
    assert payload["price_table_error"]


@pytest.mark.asyncio
async def test_paths_reports_configured_price_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _plugin, channel, responses, data_root, _sessions_dir = make_env(tmp_path, monkeypatch)
    write_prices(data_root, {"currency": "USD", "models": {"gpt-*": {"input_per_1m": 5, "output_per_1m": 15}}})

    payload = (await call(channel, responses, "paths"))["payload"]

    assert payload["price_table_configured"] is True
    assert payload["price_table_error"] is None


# --------------------------------------------------------------------------
# usage.board.overview
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_overview_returns_contract_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _plugin, channel, responses, data_root, sessions_dir = make_env(tmp_path, monkeypatch)
    write_history(
        sessions_dir,
        "s1",
        [summary_record(total_tokens=150, cost=0.03), context_record()],
    )
    patch_budget(monkeypatch, {})

    response = await call(channel, responses, "overview", {"days": 30})

    assert response["ok"] is True
    payload = response["payload"]
    assert payload["cached"] is False
    assert payload["days"] == 30
    assert payload["limit"] == 50
    assert payload["session_limit"] == 200
    assert payload["totals"]["sessions"] == 1
    assert payload["totals"]["turns"] == 1
    assert payload["totals"]["total_tokens"] == 150
    assert payload["totals"]["cost_reported"] == 0.03
    assert payload["totals"]["cost_source"] == "reported"
    assert payload["scanned"]["sessions"] == 1
    assert payload["sessions"][0]["session_id"] == "s1"
    assert payload["by_model"][0]["model"] == "gpt-5"
    assert payload["by_day"][0]["day"] == DAY
    assert payload["data_dir"] == str(data_root)
    assert payload["prices_path"] == default_prices_display()
    assert payload["price_table"]["configured"] is False
    assert payload["budget"]["configured"] is False
    assert isinstance(payload["findings"], list)
    assert payload["schema_version"] == usage_engine.SCHEMA_VERSION


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["days", "limit", "session_limit"])
@pytest.mark.parametrize("bad", [-1, "x", True])
async def test_overview_rejects_bad_int_params(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    bad: Any,
) -> None:
    _plugin, channel, responses, _data_root, _sessions_dir = make_env(tmp_path, monkeypatch)

    response = await call(channel, responses, "overview", {name: bad})

    assert response["ok"] is False
    assert response["code"] == BAD_REQUEST
    assert name in response["error"]


@pytest.mark.asyncio
async def test_overview_treats_null_params_as_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _plugin, channel, responses, _data_root, _sessions_dir = make_env(tmp_path, monkeypatch)

    response = await call(channel, responses, "overview", {"days": None, "limit": None})

    assert response["ok"] is True
    assert response["payload"]["days"] == 30
    assert response["payload"]["limit"] == 50


@pytest.mark.asyncio
async def test_overview_accepts_days_zero_and_non_dict_params(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _plugin, channel, responses, _data_root, _sessions_dir = make_env(tmp_path, monkeypatch)

    from_zero = await call(channel, responses, "overview", {"days": 0, "refresh": True})
    from_scalar = await call(channel, responses, "overview", "not-a-dict")

    assert from_zero["ok"] is True
    assert from_zero["payload"]["days"] == 0
    assert from_scalar["ok"] is True


@pytest.mark.asyncio
async def test_overview_second_call_is_served_from_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin, channel, responses, _data_root, sessions_dir = make_env(tmp_path, monkeypatch)
    write_history(sessions_dir, "s1", [summary_record(total_tokens=150)])
    scans: list[dict[str, Any]] = []
    original = plugin._scan

    def counting(**kwargs: Any) -> dict[str, Any]:
        scans.append(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(plugin, "_scan", counting)

    first = await call(channel, responses, "overview")
    second = await call(channel, responses, "overview")

    assert first["payload"]["cached"] is False
    assert second["payload"]["cached"] is True
    assert len(scans) == 1
    assert second["payload"]["totals"] == first["payload"]["totals"]


@pytest.mark.asyncio
async def test_overview_refresh_bypasses_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin, channel, responses, _data_root, sessions_dir = make_env(tmp_path, monkeypatch)
    write_history(sessions_dir, "s1", [summary_record()])
    scans: list[dict[str, Any]] = []
    original = plugin._scan

    def counting(**kwargs: Any) -> dict[str, Any]:
        scans.append(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(plugin, "_scan", counting)

    await call(channel, responses, "overview")
    await call(channel, responses, "overview")
    forced = await call(channel, responses, "overview", {"refresh": True})

    assert forced["payload"]["cached"] is False
    assert len(scans) == 2


@pytest.mark.asyncio
async def test_overview_cache_key_includes_scan_parameters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin, channel, responses, _data_root, sessions_dir = make_env(tmp_path, monkeypatch)
    write_history(sessions_dir, "s1", [summary_record()])
    scans: list[dict[str, Any]] = []
    original = plugin._scan

    def counting(**kwargs: Any) -> dict[str, Any]:
        scans.append(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(plugin, "_scan", counting)

    first = await call(channel, responses, "overview", {"days": 30})
    other = await call(channel, responses, "overview", {"days": 3650})

    assert first["payload"]["cached"] is False
    assert other["payload"]["cached"] is False
    assert len(scans) == 2
    assert plugin._cached is not None
    assert plugin._cached[1] == (3650, 50, 200)


@pytest.mark.asyncio
async def test_overview_cache_expires_after_ttl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin, channel, responses, _data_root, sessions_dir = make_env(tmp_path, monkeypatch)
    write_history(sessions_dir, "s1", [summary_record()])

    class _Clock:
        def __init__(self) -> None:
            self.monotonic_value = 1000.0

        def monotonic(self) -> float:
            return self.monotonic_value

        def time(self) -> float:
            return TS + 60

    clock = _Clock()
    monkeypatch.setattr(plugin_module, "time", clock)
    scans: list[dict[str, Any]] = []
    original = plugin._scan

    def counting(**kwargs: Any) -> dict[str, Any]:
        scans.append(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(plugin, "_scan", counting)

    first = await call(channel, responses, "overview")
    clock.monotonic_value += CACHE_TTL_SECONDS - 1
    still_cached = await call(channel, responses, "overview")
    clock.monotonic_value += 2
    expired = await call(channel, responses, "overview")

    assert first["payload"]["cached"] is False
    assert still_cached["payload"]["cached"] is True
    assert expired["payload"]["cached"] is False
    assert len(scans) == 2


def test_cache_helper_contract() -> None:
    """缓存命中的三个分支：无缓存 / 参数不同 / 超过 TTL。"""

    key = (30, 50, 200)
    fresh = (1000.0, key, {})

    assert _cache_is_fresh(None, key, 1000.0) is False
    assert _cache_is_fresh(fresh, (7, 50, 200), 1000.0) is False
    assert _cache_is_fresh(fresh, key, 1000.0 + CACHE_TTL_SECONDS) is True
    assert _cache_is_fresh(fresh, key, 1000.0 + CACHE_TTL_SECONDS + 0.5) is False


@pytest.mark.asyncio
async def test_overview_clamps_out_of_range_params_instead_of_rejecting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """INTERFACE §3.2 写的是 limit 1..200、session_limit 1..2000；RPC 只拒绝负数，
    超范围值会被引擎 ``_clamp`` 静默收敛。这里把真实行为钉住（见交付报告）。"""

    _plugin, channel, responses, _data_root, sessions_dir = make_env(tmp_path, monkeypatch)
    write_history(sessions_dir, "s1", [summary_record()])

    response = await call(
        channel,
        responses,
        "overview",
        {"limit": 100000, "session_limit": 100000, "refresh": True},
    )

    assert response["ok"] is True
    assert response["payload"]["limit"] == usage_engine.MAX_LIMIT
    assert response["payload"]["session_limit"] == usage_engine.MAX_SESSION_LIMIT


@pytest.mark.asyncio
async def test_overview_scan_failure_maps_to_internal_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin, channel, responses, _data_root, _sessions_dir = make_env(tmp_path, monkeypatch)

    def boom(**kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("扫不动了")

    monkeypatch.setattr(plugin, "_scan", boom)

    response = await call(channel, responses, "overview")

    assert response["ok"] is False
    assert response["code"] == INTERNAL_ERROR
    assert "扫描失败" in response["error"]
    assert "RuntimeError" in response["error"]


@pytest.mark.asyncio
async def test_overview_missing_sessions_dir_reports_ub009(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _plugin, channel, responses, _data_root, _sessions_dir = make_env(
        tmp_path, monkeypatch, with_sessions=False
    )

    response = await call(channel, responses, "overview")

    codes = {item["code"] for item in response["payload"]["findings"]}
    assert response["ok"] is True
    assert "UB009" in codes
    assert response["payload"]["totals"]["sessions"] == 0


@pytest.mark.asyncio
async def test_overview_empty_sessions_dir_reports_ub001(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _plugin, channel, responses, _data_root, _sessions_dir = make_env(tmp_path, monkeypatch)

    response = await call(channel, responses, "overview")

    codes = {item["code"] for item in response["payload"]["findings"]}
    assert response["ok"] is True
    assert "UB001" in codes
    assert response["payload"]["findings"] == sorted(
        response["payload"]["findings"], key=lambda item: (item["code"], item["subject"])
    )


# --------------------------------------------------------------------------
# usage.board.session
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_returns_turns_models_and_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _plugin, channel, responses, _data_root, sessions_dir = make_env(tmp_path, monkeypatch)
    write_history(
        sessions_dir,
        "s1",
        [summary_record(total_tokens=150, cost=0.03), context_record()],
    )

    response = await call(channel, responses, "session", {"session_id": "s1"})

    assert response["ok"] is True
    payload = response["payload"]
    assert payload["session_id"] == "s1"
    assert payload["path"].endswith(usage_engine.HISTORY_FILENAME)
    assert payload["size_bytes"] > 0
    assert payload["truncated"] is False
    assert payload["totals"]["turns"] == 1
    assert payload["totals"]["total_tokens"] == 150
    assert [row["model"] for row in payload["models"]] == ["gpt-5"]
    assert len(payload["turns"]) == 1
    assert payload["turns"][0]["source"] == "usage_summary"
    assert payload["context"]["limit_tokens"] == 2000
    assert payload["context"]["input_tokens"] == 1000
    assert payload["context"]["occupancy_rate"] == 0.5
    assert payload["context"]["model"] == "probe-model"
    assert payload["context"]["provider"] == "openai"
    assert payload["cross_check"] is None
    assert payload["findings"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_id",
    ["../x", "a/b", "a\\b", "", "   ", "x" * 129, None, "with space"],
)
async def test_session_rejects_unsafe_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad_id: Any
) -> None:
    _plugin, channel, responses, _data_root, _sessions_dir = make_env(tmp_path, monkeypatch)

    response = await call(channel, responses, "session", {"session_id": bad_id})

    assert response["ok"] is False
    assert response["code"] == BAD_REQUEST


@pytest.mark.asyncio
async def test_session_rejects_missing_session_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _plugin, channel, responses, _data_root, _sessions_dir = make_env(tmp_path, monkeypatch)

    response = await call(channel, responses, "session", {})

    assert response["ok"] is False
    assert response["code"] == BAD_REQUEST


@pytest.mark.asyncio
async def test_session_missing_returns_bad_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _plugin, channel, responses, _data_root, _sessions_dir = make_env(tmp_path, monkeypatch)

    response = await call(channel, responses, "session", {"session_id": "nope"})

    assert response["ok"] is False
    assert response["code"] == BAD_REQUEST
    assert "会话不存在" in response["error"]


@pytest.mark.asyncio
async def test_session_on_missing_sessions_dir_is_bad_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _plugin, channel, responses, _data_root, _sessions_dir = make_env(
        tmp_path, monkeypatch, with_sessions=False
    )

    response = await call(channel, responses, "session", {"session_id": "s1"})

    assert response["ok"] is False
    assert response["code"] == BAD_REQUEST


@pytest.mark.asyncio
async def test_session_accepts_dotted_and_dashed_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _plugin, channel, responses, _data_root, sessions_dir = make_env(tmp_path, monkeypatch)
    write_history(sessions_dir, "a.b-c_1", [summary_record()])

    response = await call(channel, responses, "session", {"session_id": "a.b-c_1"})

    assert response["ok"] is True
    assert response["payload"]["session_id"] == "a.b-c_1"


# --------------------------------------------------------------------------
# usage.board.export
# --------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fmt", "suffix"),
    [("json", "json"), ("markdown", "md"), ("csv", "csv")],
)
async def test_export_supports_the_three_formats(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fmt: str, suffix: str
) -> None:
    _plugin, channel, responses, _data_root, sessions_dir = make_env(tmp_path, monkeypatch)
    write_history(sessions_dir, "s1", [summary_record(total_tokens=150, cost=0.03)])

    response = await call(channel, responses, "export", {"format": fmt})

    assert response["ok"] is True
    payload = response["payload"]
    assert payload["format"] == fmt
    assert payload["filename"].startswith("usage-board-")
    assert payload["filename"].endswith("." + suffix)
    assert payload["content"]
    assert payload["bytes"] == len(payload["content"].encode("utf-8"))


@pytest.mark.asyncio
async def test_export_json_is_the_overview_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _plugin, channel, responses, _data_root, sessions_dir = make_env(tmp_path, monkeypatch)
    write_history(sessions_dir, "s1", [summary_record(total_tokens=150, cost=0.03)])

    payload = (
        await call(channel, responses, "export", {"format": "json", "days": 30})
    )["payload"]

    document = json.loads(payload["content"])
    assert document["totals"]["total_tokens"] == 150
    assert document["sessions"][0]["session_id"] == "s1"
    assert "budget" in document


@pytest.mark.asyncio
async def test_export_markdown_contains_the_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _plugin, channel, responses, _data_root, sessions_dir = make_env(tmp_path, monkeypatch)
    write_history(sessions_dir, "s1", [summary_record(total_tokens=150, cost=0.03)])

    payload = (await call(channel, responses, "export", {"format": "markdown"}))["payload"]

    assert payload["content"].startswith("# 用量与费用看板")
    assert "| 合计 token | 150 |" in payload["content"]
    assert "s1" in payload["content"]


@pytest.mark.asyncio
async def test_export_csv_starts_with_the_session_header(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _plugin, channel, responses, _data_root, sessions_dir = make_env(tmp_path, monkeypatch)
    write_history(sessions_dir, "s1", [summary_record(total_tokens=150, cost=0.03)])

    payload = (await call(channel, responses, "export", {"format": "csv"}))["payload"]

    assert payload["content"].startswith("session_id,turns,calls,")
    assert payload["content"].endswith("\n")
    assert "s1" in payload["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["pdf", "", "JSON5", None])
async def test_export_rejects_unknown_format(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad: Any
) -> None:
    _plugin, channel, responses, _data_root, sessions_dir = make_env(tmp_path, monkeypatch)
    write_history(sessions_dir, "s1", [summary_record()])

    response = await call(channel, responses, "export", {"format": bad})

    assert response["ok"] is False
    assert response["code"] == BAD_REQUEST


@pytest.mark.asyncio
async def test_export_rejects_bad_scan_parameters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _plugin, channel, responses, _data_root, _sessions_dir = make_env(tmp_path, monkeypatch)

    response = await call(channel, responses, "export", {"format": "json", "limit": "x"})

    assert response["ok"] is False
    assert response["code"] == BAD_REQUEST


# --------------------------------------------------------------------------
# usage.board.prices
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prices_without_a_table_is_not_zero_cost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _plugin, channel, responses, _data_root, _sessions_dir = make_env(tmp_path, monkeypatch)

    payload = (await call(channel, responses, "prices"))["payload"]

    assert payload["configured"] is False
    assert payload["entries"] == []
    assert payload["models"] == 0
    assert payload["error"] is None
    assert payload["currency"] == ""
    assert payload["path"] == default_prices_display()


@pytest.mark.asyncio
async def test_prices_with_a_table_lists_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _plugin, channel, responses, data_root, _sessions_dir = make_env(tmp_path, monkeypatch)
    write_prices(
        data_root,
        {
            "currency": "USD",
            "models": {"gpt-*": {"input_per_1m": 5.0, "output_per_1m": 15.0}},
        },
    )

    payload = (await call(channel, responses, "prices"))["payload"]

    assert payload["configured"] is True
    assert payload["currency"] == "USD"
    assert payload["models"] == 1
    assert payload["error"] is None
    assert payload["entries"][0]["pattern"] == "gpt-*"
    assert payload["entries"][0]["input_per_1m"] == 5.0
    assert payload["entries"][0]["output_per_1m"] == 15.0


@pytest.mark.asyncio
async def test_prices_with_a_corrupt_table_reports_the_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _plugin, channel, responses, data_root, _sessions_dir = make_env(tmp_path, monkeypatch)
    write_prices(data_root, "{broken")

    payload = (await call(channel, responses, "prices"))["payload"]

    assert payload["configured"] is False
    assert payload["entries"] == []
    assert payload["error"]


# --------------------------------------------------------------------------
# budget：来自 modes.team.jiuwen_team.swarmflow_budget
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_budget_uses_the_configured_swarmflow_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _plugin, channel, responses, _data_root, sessions_dir = make_env(tmp_path, monkeypatch)
    write_history(sessions_dir, "s1", [summary_record(total_tokens=150)])
    patch_budget(
        monkeypatch,
        {"modes": {"team": {"jiuwen_team": {"swarmflow_budget": 500000}}}},
    )

    budget = (await call(channel, responses, "overview"))["payload"]["budget"]

    assert budget["configured"] is True
    assert budget["source"] == "modes.team.jiuwen_team.swarmflow_budget"
    assert budget["token_budget"] == 500000
    assert budget["token_used_in_scope"] == 150
    assert budget["token_remaining"] == 499850
    assert budget["token_used_ratio"] == round(150 / 500000, 6)
    assert budget["note"]


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [1000, "1000"])
async def test_budget_accepts_numbers_and_numeric_strings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: Any
) -> None:
    _plugin, channel, responses, _data_root, sessions_dir = make_env(tmp_path, monkeypatch)
    write_history(sessions_dir, "s1", [summary_record(total_tokens=100)])
    patch_budget(
        monkeypatch,
        {"modes": {"team": {"jiuwen_team": {"swarmflow_budget": value}}}},
    )

    budget = (await call(channel, responses, "overview"))["payload"]["budget"]

    assert budget["configured"] is True
    assert budget["token_budget"] == 1000
    assert budget["token_remaining"] == 900


@pytest.mark.asyncio
async def test_budget_absent_from_config_is_not_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _plugin, channel, responses, _data_root, sessions_dir = make_env(tmp_path, monkeypatch)
    write_history(sessions_dir, "s1", [summary_record(total_tokens=150)])
    patch_budget(monkeypatch, {})

    budget = (await call(channel, responses, "overview"))["payload"]["budget"]

    assert budget["configured"] is False
    assert budget["source"] is None
    assert budget["token_budget"] is None
    assert budget["token_remaining"] is None
    assert budget["token_used_ratio"] is None
    assert budget["token_used_in_scope"] == 150


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [0, -5, "  "])
async def test_budget_non_positive_is_not_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: Any
) -> None:
    _plugin, channel, responses, _data_root, sessions_dir = make_env(tmp_path, monkeypatch)
    write_history(sessions_dir, "s1", [summary_record(total_tokens=150)])
    patch_budget(
        monkeypatch,
        {"modes": {"team": {"jiuwen_team": {"swarmflow_budget": value}}}},
    )

    budget = (await call(channel, responses, "overview"))["payload"]["budget"]

    assert budget["configured"] is False


@pytest.mark.asyncio
async def test_budget_survives_config_read_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import jiuwenswarm.common.config as config_module

    _plugin, channel, responses, _data_root, _sessions_dir = make_env(tmp_path, monkeypatch)

    def boom() -> dict[str, Any]:
        raise RuntimeError("配置读不了")

    monkeypatch.setattr(config_module, "get_config", boom)

    response = await call(channel, responses, "overview")

    assert response["ok"] is True
    assert response["payload"]["budget"]["configured"] is False


# --------------------------------------------------------------------------
# prices_path 配置覆盖：只允许留在数据根内
# --------------------------------------------------------------------------


def test_prices_path_defaults_inside_the_data_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin, _channel, _responses, data_root, _sessions_dir = make_env(tmp_path, monkeypatch)

    assert plugin._prices_path() == data_root / "usage_board" / "prices.json"


@pytest.mark.parametrize("override_kind", ["relative", "absolute"])
def test_prices_path_override_inside_the_data_root_is_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, override_kind: str
) -> None:
    plugin, _channel, _responses, data_root, _sessions_dir = make_env(tmp_path, monkeypatch)
    target = data_root / "usage_board" / "custom.json"
    plugin._config = {
        "prices_path": "usage_board/custom.json" if override_kind == "relative" else str(target)
    }

    assert plugin._prices_path() == target.resolve()
    assert plugin._display_path(plugin._prices_path()) == str(Path("usage_board") / "custom.json")


@pytest.mark.parametrize("outside", ["../outside.json", "../../outside.json"])
def test_prices_path_override_escaping_the_data_root_is_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outside: str
) -> None:
    plugin, _channel, _responses, data_root, _sessions_dir = make_env(tmp_path, monkeypatch)
    plugin._config = {"prices_path": outside}

    assert plugin._prices_path() == data_root / "usage_board" / "prices.json"


def test_prices_path_override_with_absolute_outside_path_is_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """绝对路径形式的越界覆盖必须被拒（用平台无关的绝对路径，CI 跑在 Linux 上）。"""

    plugin, _channel, _responses, data_root, _sessions_dir = make_env(tmp_path, monkeypatch)
    outside = tmp_path.parent / "definitely-outside.json"
    assert outside.is_absolute()

    plugin._config = {"prices_path": str(outside)}

    assert plugin._prices_path() == data_root / "usage_board" / "prices.json"


def test_prices_path_windows_style_override_cannot_escape_the_data_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``D:/...`` 只在 Windows 上是绝对路径；在 POSIX 上必须被关在数据根内。"""

    plugin, _channel, _responses, data_root, _sessions_dir = make_env(tmp_path, monkeypatch)
    plugin._config = {"prices_path": "D:/definitely-outside.json"}

    assert plugin._is_within(plugin._prices_path(), [data_root])


def test_prices_path_symlink_escape_is_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """数据根内的符号链接指向根外时必须拒绝（abspath 看不出来，resolve 才行）。"""

    plugin, _channel, _responses, data_root, _sessions_dir = make_env(tmp_path, monkeypatch)
    outside_dir = tmp_path.parent / "outside-prices"
    outside_dir.mkdir(exist_ok=True)
    outside_file = outside_dir / "prices.json"
    outside_file.write_text('{"currency": "USD", "models": {}}', encoding="utf-8")
    link = data_root / "linked.json"
    try:
        link.symlink_to(outside_file)
    except (OSError, NotImplementedError):
        pytest.skip("当前环境不允许创建符号链接")

    plugin._config = {"prices_path": "linked.json"}

    assert plugin._prices_path() == data_root / "usage_board" / "prices.json"


@pytest.mark.parametrize("blank", ["", "   ", None, 123])
def test_prices_path_override_blank_is_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, blank: Any
) -> None:
    plugin, _channel, _responses, data_root, _sessions_dir = make_env(tmp_path, monkeypatch)
    plugin._config = {"prices_path": blank}

    assert plugin._prices_path() == data_root / "usage_board" / "prices.json"


@pytest.mark.asyncio
async def test_prices_override_outside_data_root_still_reads_the_default_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin, channel, responses, data_root, _sessions_dir = make_env(tmp_path, monkeypatch)
    write_prices(
        data_root,
        {"models": {"gpt-*": {"input_per_1m": 5.0, "output_per_1m": 15.0}}},
    )
    plugin._config = {"prices_path": str(tmp_path / "outside.json")}

    payload = (await call(channel, responses, "prices"))["payload"]

    assert payload["configured"] is True
    assert payload["path"] == default_prices_display()


# --------------------------------------------------------------------------
# 工具函数
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("target", "roots", "expected"),
    [
        ("a/b", ["a"], True),
        ("a/b/c", ["a", "z"], True),
        ("b", ["a"], False),
        ("a", ["a/b"], False),
    ],
)
def test_is_within(target: str, roots: list[str], expected: bool) -> None:
    assert (
        UsageBoardApplicationPlugin._is_within(Path(target), [Path(root) for root in roots])
        is expected
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [(True, "整数"), (-1, "不能为负数"), ("x", "整数"), (None, "default")],
)
def test_int_param_contract(value: Any, expected: str) -> None:
    if expected == "default":
        assert plugin_module._int_param({"days": value}, "days", 30) == 30
        return
    with pytest.raises(ValueError, match=expected):
        plugin_module._int_param({"days": value}, "days", 30)


def test_count_sessions_returns_zero_for_unreadable_dir(tmp_path: Path) -> None:
    missing = tmp_path / "nope"

    assert UsageBoardApplicationPlugin._count_sessions(missing) == 0
