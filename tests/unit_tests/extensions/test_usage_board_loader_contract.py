"""usage_board 与宿主真实加载链路的契约测试。

这些用例**不 mock loader**：直接跑 ``ExtensionLoader`` + ``ExtensionRegistry`` + FastAPI 资产路由，
把 INTERFACE.md 里「宿主会这样处理」的断言钉在真实实现上：清单无需 import 入口即可读、
真加载后 5 个 RPC 可调用、iframe 资产路由能取到页面且挡住路径穿越。
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import jiuwenswarm
from jiuwenswarm.gateway.extensions.application_host import (
    application_plugin_manifest,
    mount_application_plugin_http_routes,
)
from jiuwenswarm.extensions.loader import ExtensionLoader, _is_extension_root
from jiuwenswarm.extensions.registry import ExtensionRegistry

EXTENSIONS_DIR = Path(jiuwenswarm.__file__).resolve().parent / "extensions"
REPO_ROOT = EXTENSIONS_DIR.parents[1]
PLUGIN_ROOT = EXTENSIONS_DIR / "usage_board"
FRONTEND_PAGE = PLUGIN_ROOT / "frontend" / "dist" / "index.html"
CONTEXT_USAGE_FIXTURE = (
    REPO_ROOT
    / "jiuwenswarm"
    / "channels"
    / "web"
    / "frontend"
    / "tests"
    / "fixtures"
    / "contextUsage.v1.json"
)

RPC_METHODS = ("paths", "overview", "session", "export", "prices")
RPC_NAMES = tuple(f"usage.board.{name}" for name in RPC_METHODS)

#: INTERFACE.md §6 冻结的 data-testid 清单。
REQUIRED_TESTIDS = (
    "usage-board",
    "usage-board-refresh",
    "usage-board-range",
    "usage-board-summary",
    "usage-board-model-table",
    "usage-board-model-item",
    "usage-board-session-table",
    "usage-board-session-item",
    "usage-board-day-item",
    "usage-board-finding-item",
    "usage-board-detail",
    "usage-board-export-json",
    "usage-board-export-markdown",
    "usage-board-export-csv",
    "usage-board-empty",
    "usage-board-error",
    "usage-board-price-hint",
)

TS = time.time() - 3600.0


class _FakeChannel:
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


async def load_plugin(
    registry: ExtensionRegistry | None = None,
) -> tuple[ExtensionRegistry, Any]:
    registry = registry if registry is not None else make_registry()
    loaded = await ExtensionLoader(registry).load_extension(PLUGIN_ROOT)
    assert loaded
    plugin = registry.get_application_plugin("usage-board")
    assert plugin is not None
    return registry, plugin


def load_engine_by_path() -> Any:
    """按文件路径加载引擎——loader 加载 extension.py 的同款方式。

    ``importlib`` 只在 ``sys.modules`` 里找 ``cls.__module__`` 来解字符串注解，
    引擎里全是 dataclass（``from __future__ import annotations``），所以要先登记
    模块名再 exec；跑完撤掉，避免污染本进程的模块表。
    """

    module_name = "usage_board_engine_probe"
    spec = importlib.util.spec_from_file_location(
        module_name, PLUGIN_ROOT / "usage_engine.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(module_name, None)
    return module


def summary_record(*, ts: float = TS, total_tokens: int = 150, cost: float | None = 0.03) -> dict[str, Any]:
    usage: dict[str, Any] = {
        "input_tokens": 100,
        "output_tokens": 50,
        "total_tokens": total_tokens,
    }
    if cost is not None:
        usage["total_cost"] = cost
    return {
        "event_type": "chat.usage_summary",
        "session_id": "s1",
        "timestamp": ts,
        "model": "gpt-5",
        "usage": usage,
    }


def write_history(sessions_dir: Path, session_id: str, records: list[Any]) -> Path:
    folder = sessions_dir / session_id
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "history.jsonl"
    lines = [
        item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
        for item in records
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# 发现与加载
# --------------------------------------------------------------------------


def test_discovery_finds_usage_board_next_to_builtins() -> None:
    loader = ExtensionLoader(make_registry())
    loader.add_search_path(EXTENSIONS_DIR)

    discovered = {root.name for root in loader.discover_extension_roots()}

    assert {"usage_board", "agentos", "agent_client", "video_duplex"} <= discovered


def test_usage_board_root_is_recognised_by_the_loader() -> None:
    assert _is_extension_root(PLUGIN_ROOT) is True
    assert (PLUGIN_ROOT / "extension.yaml").is_file()
    assert (PLUGIN_ROOT / "extension.py").is_file()
    assert PLUGIN_ROOT.name.isidentifier()


class _BlockedImport:
    """拦截指定模块的导入，用来模拟「插件目录不在 jiuwenswarm 包内」的形态。"""

    def __init__(self, name: str) -> None:
        self.name = name

    def find_spec(self, fullname, path=None, target=None):  # noqa: ANN001
        if fullname == self.name:
            raise ImportError(f"blocked: {fullname}")
        return None


def test_third_party_layout_loads_the_engine_without_touching_sys_path(
    tmp_path: Path,
) -> None:
    """第三方目录形态：按文件路径加载同级引擎，且**不修改 sys.path**（CodeCheck G.PSL.03）。"""

    probe = tmp_path / "usage_board_probe"
    probe.mkdir()
    for name in ("extension.py", "usage_engine.py"):
        shutil.copy2(PLUGIN_ROOT / name, probe / name)

    saved = sys.modules.pop("jiuwenswarm.extensions.usage_board", None)
    blocked = _BlockedImport("jiuwenswarm.extensions.usage_board")
    sys.meta_path.insert(0, blocked)
    path_before = list(sys.path)
    try:
        module = ExtensionLoader._import_module(probe)
    finally:
        sys.meta_path.remove(blocked)
        sys.modules.pop("usage_board_usage_engine", None)
        if saved is not None:
            sys.modules["jiuwenswarm.extensions.usage_board"] = saved

    assert Path(module.usage_engine.__file__).resolve().parent == probe.resolve()
    assert module.usage_engine.HISTORY_FILENAME == "history.jsonl"
    assert sys.path == path_before


def test_manifest_is_readable_without_importing_the_entry(tmp_path: Path) -> None:
    probe = tmp_path / "usage_board_probe"
    probe.mkdir()
    shutil.copy2(PLUGIN_ROOT / "extension.yaml", probe / "extension.yaml")
    (probe / "extension.py").write_text(
        "raise RuntimeError('入口不该被 import')\n", encoding="utf-8"
    )

    manifest = ExtensionLoader.load_manifest(probe)

    assert manifest["id"] == "usage-board"
    assert manifest["package_type"] == "application"
    assert manifest["version"] == "1.0.0"


def test_manifest_declares_the_frozen_identity() -> None:
    manifest = ExtensionLoader.load_manifest(PLUGIN_ROOT)

    assert manifest["id"] == "usage-board"
    assert manifest["package_type"] == "application"
    assert manifest["name"] == "Usage Board"
    assert manifest["min_jiuwenswarm_version"] == "0.2.5"


@pytest.mark.asyncio
async def test_real_loader_registers_the_plugin() -> None:
    registry, plugin = await load_plugin()

    assert plugin.plugin_id == "usage-board"
    assert plugin.metadata.id == "usage-board"
    assert plugin.metadata.package_type == "application"
    assert plugin.metadata.version == "1.0.0"
    assert plugin.frontend_asset_root() == PLUGIN_ROOT / "frontend" / "dist"
    assert plugin.is_enabled() is True
    assert plugin in registry.get_application_plugins()


@pytest.mark.asyncio
async def test_real_binding_exposes_five_callable_local_rpcs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry, plugin = await load_plugin()
    data_root = tmp_path / "home"
    sessions_dir = data_root / "agent" / "sessions"
    sessions_dir.mkdir(parents=True)
    write_history(sessions_dir, "s1", [summary_record()])
    monkeypatch.setattr(plugin, "_data_dir", lambda: data_root)
    monkeypatch.setattr(plugin, "_sessions_dir", lambda: sessions_dir)
    channel = _FakeChannel()

    registry.bind_application_plugins(channel)

    assert set(channel.methods) == set(RPC_NAMES)
    assert channel.local_only == set(RPC_NAMES)
    responses: list[dict[str, Any]] = []
    params_by_method: dict[str, Any] = {
        "paths": None,
        "overview": {"days": 0},
        "session": {"session_id": "s1"},
        "export": {"format": "json", "days": 0},
        "prices": None,
    }
    for name in RPC_METHODS:
        await channel.methods[f"usage.board.{name}"](
            responses, f"req-{name}", params_by_method[name], "session-1"
        )
        assert responses[-1]["ok"] is True, responses[-1]
        assert responses[-1]["payload"]


@pytest.mark.asyncio
async def test_real_binding_keeps_the_disabled_gate() -> None:
    registry, plugin = await load_plugin()
    plugin.is_enabled = lambda: False  # type: ignore[method-assign]
    channel = _FakeChannel()
    registry.bind_application_plugins(channel)
    responses: list[dict[str, Any]] = []

    await channel.methods["usage.board.paths"](responses, "req-1", None, "session-1")

    assert responses[-1]["ok"] is False
    assert responses[-1]["code"] == "APPLICATION_PLUGIN_DISABLED"


# --------------------------------------------------------------------------
# 宿主 HTTP 面：清单与 iframe 资产路由
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_manifest_endpoint_exposes_the_iframe_page() -> None:
    registry, _plugin = await load_plugin()

    manifest = application_plugin_manifest(registry)
    entry = next(item for item in manifest["plugins"] if item["plugin_id"] == "usage-board")

    assert manifest["api_version"] == 1
    assert entry["render_mode"] == "iframe"
    assert entry["nav_key"] == "app:usage-board"
    assert entry["id"] == "usage-board-page"
    assert entry["title"] == "用量与费用"
    assert entry["plugin_version"] == "1.0.0"
    assert entry["enabled"] is True
    assert entry["entry_url"].endswith("/usage-board/assets/index.html")


@pytest.mark.asyncio
async def test_asset_route_serves_the_page_and_blocks_traversal() -> None:
    registry, _plugin = await load_plugin()
    app = FastAPI()
    mount_application_plugin_http_routes(app, registry)
    client = TestClient(app)

    page = client.get("/api/application-plugins/usage-board/assets/index.html")
    traversal = client.get(
        "/api/application-plugins/usage-board/assets/%2e%2e%2fextension.py"
    )

    assert page.status_code == 200
    assert 'data-testid="usage-board"' in page.text
    assert traversal.status_code == 404


@pytest.mark.asyncio
async def test_asset_route_returns_404_for_unknown_plugin_and_missing_asset() -> None:
    registry, _plugin = await load_plugin()
    app = FastAPI()
    mount_application_plugin_http_routes(app, registry)
    client = TestClient(app)

    unknown = client.get("/api/application-plugins/no-such/assets/index.html")
    missing = client.get("/api/application-plugins/usage-board/assets/nope.html")

    assert unknown.status_code == 404
    assert missing.status_code == 404


# --------------------------------------------------------------------------
# 前端静态资产（INTERFACE.md §6）
# --------------------------------------------------------------------------


def read_frontend_page() -> str:
    assert FRONTEND_PAGE.is_file(), f"缺少前端页面：{FRONTEND_PAGE}"
    return FRONTEND_PAGE.read_text(encoding="utf-8")


def test_frontend_page_exists_and_is_a_real_document() -> None:
    html = read_frontend_page()

    assert html.strip()
    assert "<html" in html.lower()
    assert len(html) > 1000


def test_frontend_page_exposes_every_frozen_data_testid() -> None:
    html = read_frontend_page()

    missing = [name for name in REQUIRED_TESTIDS if f'data-testid="{name}"' not in html]

    assert missing == []


def test_frontend_page_declares_the_rpc_prefix_and_calls_every_method() -> None:
    html = read_frontend_page()

    assert "usage.board." in html
    for method in RPC_METHODS:
        assert f"call('{method}'" in html or f'call("{method}"' in html, method


def test_frontend_page_is_offline_and_free_of_dynamic_eval() -> None:
    html = read_frontend_page()

    for token in ("http://", "https://", "//cdn", "eval(", "new Function"):
        assert token not in html, token


def test_frontend_page_marks_list_rows_with_data_variant() -> None:
    html = read_frontend_page()

    assert "data-variant" in html


# --------------------------------------------------------------------------
# 引擎按文件路径加载 + pyproject 资产守卫 + dogfood
# --------------------------------------------------------------------------


def test_engine_loads_by_file_path_and_scans_a_temp_dir(tmp_path: Path) -> None:
    module = load_engine_by_path()
    sessions_dir = tmp_path / "agent" / "sessions"
    write_history(sessions_dir, "s1", [summary_record(total_tokens=150)])

    overview = module.scan_sessions(sessions_dir, now=TS + 60)

    assert module.__name__ == "usage_board_engine_probe"
    assert module.SCHEMA_VERSION == 1
    assert overview["totals"]["sessions"] == 1
    assert overview["totals"]["turns"] == 1
    assert overview["totals"]["total_tokens"] == 150
    assert overview["sessions"][0]["session_id"] == "s1"


def test_pyproject_ships_plugin_frontend_assets() -> None:
    """iframe 页面必须随 wheel 发布，否则 entry_url 在生产包里 404。"""

    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert '"extensions/**/frontend/dist/**/*"' in text
    assert '"extensions/**/frontend/dist/*"' in text


def test_dogfood_scans_the_repo_context_usage_fixture(tmp_path: Path) -> None:
    """用仓库真实夹具（contextUsage.v1.json）包一行 history.jsonl 跑一遍。"""

    assert CONTEXT_USAGE_FIXTURE.is_file()
    record = json.loads(CONTEXT_USAGE_FIXTURE.read_text(encoding="utf-8"))
    module = load_engine_by_path()
    stamp = module.parse_epoch(record["timestamp"])
    assert stamp is not None
    sessions_dir = tmp_path / "agent" / "sessions"
    write_history(sessions_dir, "pr5623_contract_probe", [record])

    detail = module.session_detail(sessions_dir, "pr5623_contract_probe")

    context = detail["context"]
    assert context["limit_tokens"] == 2000
    assert context["input_tokens"] == 1000
    assert context["occupancy_rate"] == 0.5
    assert context["model"] == "probe-model"
    assert context["provider"] == "openai"
    assert context["agent_id"] == "probe-agent"
    assert context["ts"] == stamp
    # 只有上下文快照、没有 chat.usage_* 事件
    assert detail["totals"]["turns"] == 0
    assert detail["turns"] == []
    assert detail["findings"] == []

    overview = module.scan_sessions(sessions_dir, now=stamp + 3600)

    # scanned.sessions 只数「有用量事件」的会话，这里没有；但文件确实被读进来了
    assert overview["scanned"]["files_read"] == 1
    assert overview["scanned"]["skipped_by_days"] == 0
    assert overview["totals"]["sessions"] == 0
    assert overview["sessions"] == []
    # 唯一的预期结论：扫描范围内没有可计费的用量事件
    assert [item["code"] for item in overview["findings"]] == ["UB001"]
