# -*- coding: utf-8 -*-
"""usage_board 的入口脚本：把用量聚合引擎嫁接到宿主的本地 RPC 通道上。

宿主以 ``spec_from_file_location`` 按**文件路径**加载本模块（``extensions/loader.py:146-164``），
插件目录不在 ``sys.path`` 上。这里优先走包内绝对导入；只有第三方插件目录形态（被拷到
``<data-dir>/application_plugins/`` 下）才回退到按文件路径加载同名引擎，
**不修改 ``sys.path``**：既避免 ``insert(0, ...)`` 带来的模块重名风险，又让两种形态都能工作。
"""

from __future__ import annotations

import importlib.util
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

from jiuwenswarm.extensions.sdk import (
    ApplicationPluginExtension,
    ApplicationPluginServices,
    FrontendContribution,
)

_PLUGIN_DIR = Path(__file__).resolve().parent

try:
    from jiuwenswarm.extensions.usage_board import usage_engine
except ImportError:  # 第三方插件目录形态（不在 jiuwenswarm 包内）
    _ENGINE_PATH = _PLUGIN_DIR / "usage_engine.py"
    _spec = importlib.util.spec_from_file_location("usage_board_usage_engine", _ENGINE_PATH)
    if _spec is None or _spec.loader is None:  # pragma: no cover - 引擎文件必然存在
        raise ImportError(f"无法加载用量引擎: {_ENGINE_PATH}") from None
    usage_engine = importlib.util.module_from_spec(_spec)
    # 引擎里全是 dataclass：importlib 解字符串注解要能在 sys.modules 里找到 cls.__module__。
    sys.modules[_spec.name] = usage_engine
    _spec.loader.exec_module(usage_engine)

#: 本地 RPC 方法前缀（与前端页面里的 PREFIX 保持一致）。
RPC_PREFIX = "usage.board."

#: 总览结果的短缓存秒数：面板连续刷新时不至于反复扫盘。
CACHE_TTL_SECONDS = 15.0

BAD_REQUEST = "BAD_REQUEST"
INTERNAL_ERROR = "INTERNAL_ERROR"


def _cache_is_fresh(cached: Any, key: Any, now: float) -> bool:
    """缓存是否命中（拆成早返回，避免把四个条件堆在同一个 if 里）。"""

    if cached is None:
        return False
    if cached[1] != key:
        return False
    return now - cached[0] <= CACHE_TTL_SECONDS


def running_jiuwenswarm_version() -> str:
    """尽力拿到当前宿主版本；拿不到就返回空串（不臆测）。

    优先用宿主的权威来源 ``jiuwenswarm.common.version.get_runtime_version()``：
    发行包名是 ``workswarm``，用 ``importlib.metadata.version("jiuwenswarm")`` 在安装环境里
    必然查不到，会退化成读 ``pyproject.toml``，而装成 wheel 后源码树并不存在。
    """

    try:
        from jiuwenswarm.common.version import get_runtime_version

        found = str(get_runtime_version() or "").strip()
        if found:
            return found
    except Exception:  # noqa: BLE001 - 宿主模块不可用时退化到本地兜底
        pass
    for candidate in (
        _PLUGIN_DIR.parents[2] / "pyproject.toml",
        _PLUGIN_DIR.parents[3] / "pyproject.toml",
    ):
        try:
            text = candidate.read_text(encoding="utf-8")
        except OSError:
            continue
        match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
        if match:
            return match.group(1)
    return ""


class UsageBoardApplicationPlugin(ApplicationPluginExtension):
    """用量与费用看板（只读统计，不写任何宿主数据）。"""

    plugin_id = "usage-board"

    def __init__(self) -> None:
        self._config: dict[str, Any] = {}
        self._logger: Any = None
        self._channel: Any = None
        self._cached: tuple[float, tuple[Any, ...], dict[str, Any]] | None = None
        # 不依赖 loader 代设目录，自己先设好：metadata / frontend_asset_root 才能用。
        self.set_extension_dir(_PLUGIN_DIR)

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def initialize(self, config: Any) -> None:
        raw = getattr(config, "config", None)
        self._config = raw if isinstance(raw, dict) else {}
        self._logger = getattr(config, "logger", None)

    async def shutdown(self) -> None:
        self._cached = None

    def is_enabled(self) -> bool:
        """只读统计，无副作用，默认开启。"""

        return True

    def frontend_contributions(self) -> tuple[FrontendContribution, ...]:
        return (
            FrontendContribution(
                id="usage-board-page",
                nav_key="app:usage-board",
                title="用量与费用",
                render_mode="iframe",
                entrypoint="index.html",
                position=770,
            ),
        )

    # ------------------------------------------------------------------
    # 本地 RPC
    # ------------------------------------------------------------------

    def bind_web_channel(
        self,
        channel: Any,
        services: ApplicationPluginServices,
    ) -> None:
        del services
        self._channel = channel
        channel.register_method(RPC_PREFIX + "paths", self._handle_paths, local_only=True)
        channel.register_method(
            RPC_PREFIX + "overview", self._handle_overview, local_only=True
        )
        channel.register_method(
            RPC_PREFIX + "session", self._handle_session, local_only=True
        )
        channel.register_method(
            RPC_PREFIX + "export", self._handle_export, local_only=True
        )
        channel.register_method(
            RPC_PREFIX + "prices", self._handle_prices, local_only=True
        )

    async def _respond(
        self,
        ws: Any,
        req_id: Any,
        *,
        ok: bool,
        payload: Any = None,
        error: str | None = None,
        code: str | None = None,
    ) -> None:
        kwargs: dict[str, Any] = {"ok": ok}
        if payload is not None:
            kwargs["payload"] = payload
        if error is not None:
            kwargs["error"] = error
        if code is not None:
            kwargs["code"] = code
        await self._channel.send_response(ws, req_id, **kwargs)

    async def _handle_paths(
        self, ws: Any, req_id: Any, params: Any, session_id: Any
    ) -> None:
        del params, session_id
        sessions_dir = self._sessions_dir()
        prices_path = self._prices_path()
        table = usage_engine.load_price_table(
            prices_path, display_path=self._display_path(prices_path)
        )
        payload = {
            "data_dir": str(self._data_dir()),
            "sessions_dir": str(sessions_dir),
            "session_count": self._count_sessions(sessions_dir),
            "prices_path": self._display_path(prices_path),
            "price_table_configured": table.configured,
            "price_table_error": table.error,
            "cache_ttl_seconds": CACHE_TTL_SECONDS,
            "running_version": running_jiuwenswarm_version(),
        }
        await self._respond(ws, req_id, ok=True, payload=payload)

    async def _handle_prices(
        self, ws: Any, req_id: Any, params: Any, session_id: Any
    ) -> None:
        del params, session_id
        prices_path = self._prices_path()
        table = usage_engine.load_price_table(
            prices_path, display_path=self._display_path(prices_path)
        )
        payload = table.as_dict()
        payload["path"] = self._display_path(prices_path)
        await self._respond(ws, req_id, ok=True, payload=payload)

    async def _handle_overview(
        self, ws: Any, req_id: Any, params: Any, session_id: Any
    ) -> None:
        del session_id
        params = params if isinstance(params, dict) else {}
        try:
            days = _int_param(params, "days", usage_engine.DEFAULT_DAYS)
            limit = _int_param(params, "limit", usage_engine.DEFAULT_LIMIT)
            session_limit = _int_param(
                params, "session_limit", usage_engine.DEFAULT_SESSION_LIMIT
            )
        except ValueError as exc:
            await self._respond(ws, req_id, ok=False, error=str(exc), code=BAD_REQUEST)
            return
        refresh = bool(params.get("refresh", False))
        key = (days, limit, session_limit)
        cached = self._cached
        now = time.monotonic()
        if not refresh and _cache_is_fresh(cached, key, now):
            payload = dict(cached[2])
            payload["cached"] = True
            await self._respond(ws, req_id, ok=True, payload=payload)
            return
        try:
            payload = self._scan(days=days, limit=limit, session_limit=session_limit)
        except Exception as exc:  # noqa: BLE001 - 面板需要看到失败原因
            await self._respond(
                ws,
                req_id,
                ok=False,
                error=f"扫描失败：{type(exc).__name__}: {exc}",
                code=INTERNAL_ERROR,
            )
            return
        payload["cached"] = False
        self._cached = (now, key, payload)
        await self._respond(ws, req_id, ok=True, payload=payload)

    async def _handle_session(
        self, ws: Any, req_id: Any, params: Any, session_id: Any
    ) -> None:
        del session_id
        params = params if isinstance(params, dict) else {}
        try:
            payload = usage_engine.session_detail(
                self._sessions_dir(),
                params.get("session_id"),
                prices=self._price_table(),
            )
        except ValueError as exc:
            await self._respond(ws, req_id, ok=False, error=str(exc), code=BAD_REQUEST)
            return
        await self._respond(ws, req_id, ok=True, payload=payload)

    async def _handle_export(
        self, ws: Any, req_id: Any, params: Any, session_id: Any
    ) -> None:
        del session_id
        params = params if isinstance(params, dict) else {}
        try:
            days = _int_param(params, "days", usage_engine.DEFAULT_DAYS)
            limit = _int_param(params, "limit", usage_engine.DEFAULT_LIMIT)
            session_limit = _int_param(
                params, "session_limit", usage_engine.DEFAULT_SESSION_LIMIT
            )
            overview = self._scan(days=days, limit=limit, session_limit=session_limit)
            payload = usage_engine.build_export(overview, params.get("format"))
        except ValueError as exc:
            await self._respond(ws, req_id, ok=False, error=str(exc), code=BAD_REQUEST)
            return
        except Exception as exc:  # noqa: BLE001 - 面板需要看到失败原因
            await self._respond(
                ws,
                req_id,
                ok=False,
                error=f"导出失败：{type(exc).__name__}: {exc}",
                code=INTERNAL_ERROR,
            )
            return
        await self._respond(ws, req_id, ok=True, payload=payload)

    # ------------------------------------------------------------------
    # 宿主资源解析（全部只读）
    # ------------------------------------------------------------------

    def _scan(self, *, days: int, limit: int, session_limit: int) -> dict[str, Any]:
        sessions_dir = self._sessions_dir()
        prices = self._price_table()
        overview = usage_engine.scan_sessions(
            sessions_dir,
            prices=prices,
            days=days,
            limit=limit,
            session_limit=session_limit,
            now=time.time(),
        )
        totals = overview.get("totals") or {}
        overview["budget"] = self._budget_payload(int(totals.get("total_tokens") or 0))
        overview["data_dir"] = str(self._data_dir())
        overview["prices_path"] = self._display_path(self._prices_path())
        return overview

    def _price_table(self) -> usage_engine.PriceTable:
        prices_path = self._prices_path()
        return usage_engine.load_price_table(
            prices_path, display_path=self._display_path(prices_path)
        )

    def _data_dir(self) -> Path:
        try:
            from jiuwenswarm.common.utils import get_user_workspace_dir

            return Path(get_user_workspace_dir())
        except Exception:  # noqa: BLE001 - 第三方/独立运行时退化到环境变量
            env = os.getenv("JIUWENSWARM_DATA_DIR")
            if env:
                return Path(env)
            return Path.home() / ".jiuwenswarm"

    def _sessions_dir(self) -> Path:
        try:
            from jiuwenswarm.common.utils import get_agent_sessions_dir

            return Path(get_agent_sessions_dir())
        except Exception:  # noqa: BLE001 - 同上
            return self._data_dir() / "agent" / "sessions"

    def _prices_path(self) -> Path:
        """价格表路径：默认数据根下的 ``usage_board/prices.json``。

        可用**全局** ``config.yaml`` 顶层的 ``prices_path`` 覆盖（loader 传给
        ``initialize()`` 的是 ``ExtensionConfig``，即整份全局配置，插件拿不到自己的配置段）；
        越出数据根的覆盖一律忽略，防止越权读盘。
        """

        base = self._data_dir()
        override = self._config.get("prices_path")
        if isinstance(override, str) and override.strip():
            candidate = Path(override.strip()).expanduser()
            if not candidate.is_absolute():
                candidate = base / candidate
            # 必须 resolve()：abspath 不展开符号链接，数据根内指向外部的链接会绕过 _is_within。
            resolved = candidate.resolve()
            if self._is_within(resolved, [base.resolve()]):
                return resolved
        return base / usage_engine.PRICES_RELATIVE_PATH

    def _display_path(self, path: Path) -> str:
        """把数据根缩写掉，避免把本机绝对路径写进前端。"""

        base = self._data_dir()
        try:
            return str(path.relative_to(base))
        except ValueError:
            return path.name

    @staticmethod
    def _count_sessions(sessions_dir: Path) -> int:
        try:
            return sum(
                1
                for item in sessions_dir.iterdir()
                if item.is_dir() and (item / usage_engine.HISTORY_FILENAME).is_file()
            )
        except OSError:
            return 0

    def _budget_payload(self, token_used: int) -> dict[str, Any]:
        """读团队 token 预算配置；读不到就明确说没配置，不猜。"""

        note = (
            "swarmflow_budget 是团队级 token 预算，由该团队所有 swarmflow run 共享，"
            "与扫描范围口径不同，仅作参照"
        )
        payload: dict[str, Any] = {
            "configured": False,
            "source": None,
            "token_budget": None,
            "token_used_in_scope": token_used,
            "token_remaining": None,
            "token_used_ratio": None,
            "note": note,
        }
        try:
            from jiuwenswarm.common.config import (
                SWARMFLOW_BUDGET_CONFIG_PATH,
                get_config,
            )

            node: Any = get_config()
            for key in SWARMFLOW_BUDGET_CONFIG_PATH:
                node = node.get(key) if isinstance(node, dict) else None
            budget = int(node) if isinstance(node, (int, float, str)) and str(node).strip() else None
        except Exception:  # noqa: BLE001 - 配置缺失/不可读都视为未配置
            return payload
        if budget is None or budget <= 0:
            return payload
        payload["configured"] = True
        payload["source"] = ".".join(SWARMFLOW_BUDGET_CONFIG_PATH)
        payload["token_budget"] = budget
        payload["token_remaining"] = max(0, budget - token_used)
        payload["token_used_ratio"] = round(token_used / budget, 6)
        return payload

    @staticmethod
    def _is_within(target: Path, roots: list[Path]) -> bool:
        for root in roots:
            try:
                target.relative_to(root)
            except ValueError:
                continue
            return True
        return False


def _int_param(params: dict[str, Any], name: str, default: int) -> int:
    """取整型参数；给了非法值就报错，而不是静默用默认值。"""

    if name not in params or params.get(name) is None:
        return default
    raw = params.get(name)
    if isinstance(raw, bool):
        raise ValueError(f"{name} 必须是整数")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{name} 必须是整数") from None
    if value < 0:
        raise ValueError(f"{name} 不能为负数")
    return value


async def register_extensions(registry: Any) -> list[UsageBoardApplicationPlugin]:
    """宿主扩展入口。"""

    plugin = UsageBoardApplicationPlugin()
    registry.register_application_plugin(plugin)
    return [plugin]
