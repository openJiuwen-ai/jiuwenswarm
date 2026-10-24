"""plugin_doctor 的入口脚本：把体检引擎嫁接到宿主的本地 RPC 通道上。

宿主以 ``spec_from_file_location`` 按**文件路径**加载本模块（``extensions/loader.py:146-164``），
插件目录不在 ``sys.path`` 上，因此这里先把自己的目录插进去，再导入同级引擎模块——这样
既能作为内置插件（包内绝对导入）工作，也能被拷到 ``<data-dir>/application_plugins/`` 下
作为第三方插件工作。
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from jiuwenswarm.extensions.sdk import (
    ApplicationPluginExtension,
    ApplicationPluginServices,
    FrontendContribution,
)

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

try:  # noqa: E402 - 见模块 docstring：必须在 sys.path 处理之后
    from jiuwenswarm.extensions.plugin_doctor import doctor_engine  # noqa: E402
except ImportError:  # 第三方插件目录形态（不在 jiuwenswarm 包内）
    import doctor_engine  # type: ignore[no-redef]  # noqa: E402

#: 本地 RPC 方法前缀（与前端页面里的 PREFIX 保持一致）。
RPC_PREFIX = "plugin.doctor."
#: 单次扫描的插件数量上限：畸形目录再多也不至于把面板拖死。
MAX_PLUGINS = 200
#: 宿主的默认包内搜索目录（``extensions/manager.py:10-11``）。
DEFAULT_EXTENSION_DIR = "jiuwenswarm/extensions"

BAD_REQUEST = "BAD_REQUEST"
PATH_NOT_ALLOWED = "PATH_NOT_ALLOWED"


def running_jiuwenswarm_version() -> str:
    """尽力拿到当前宿主版本；拿不到就返回空串（引擎会因此跳过版本比较，不臆测）。"""

    try:
        from importlib.metadata import version

        return version("jiuwenswarm")
    except Exception:  # noqa: BLE001 - 未安装 dist-info（源码直跑）时退化
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


class PluginDoctorApplicationPlugin(ApplicationPluginExtension):
    """应用插件体检台。"""

    plugin_id = "plugin-doctor"

    def __init__(self) -> None:
        self._config: dict[str, Any] = {}
        self._logger: Any = None
        self._channel: Any = None
        self._cached: doctor_engine.DoctorReport | None = None
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
        """只读体检，无副作用，默认开启。"""

        return True

    def frontend_contributions(self) -> tuple[FrontendContribution, ...]:
        return (
            FrontendContribution(
                id="plugin-doctor-page",
                nav_key="app:plugin-doctor",
                title="插件体检",
                render_mode="iframe",
                entrypoint="index.html",
                position=760,
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
        channel.register_method(
            RPC_PREFIX + "paths", self._handle_paths, local_only=True
        )
        channel.register_method(
            RPC_PREFIX + "rules", self._handle_rules, local_only=True
        )
        channel.register_method(
            RPC_PREFIX + "report", self._handle_report, local_only=True
        )
        channel.register_method(
            RPC_PREFIX + "inspect", self._handle_inspect, local_only=True
        )
        channel.register_method(
            RPC_PREFIX + "export", self._handle_export, local_only=True
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
        payload = {
            "search_paths": [str(p) for p in self._resolve_search_paths(None)],
            "user_plugin_dir": str(self._user_plugin_dir()),
            "default_extension_dir": DEFAULT_EXTENSION_DIR,
            "max_plugins": MAX_PLUGINS,
            "running_version": running_jiuwenswarm_version(),
        }
        await self._respond(ws, req_id, ok=True, payload=payload)

    async def _handle_rules(
        self, ws: Any, req_id: Any, params: Any, session_id: Any
    ) -> None:
        del params, session_id
        await self._respond(ws, req_id, ok=True, payload=doctor_engine.rules_payload())

    async def _handle_report(
        self, ws: Any, req_id: Any, params: Any, session_id: Any
    ) -> None:
        del session_id
        params = params if isinstance(params, dict) else {}
        try:
            override = params.get("search_paths")
            search_paths = (
                None if override is None else self._as_dir_list(override)
            )
        except ValueError as exc:
            await self._respond(
                ws, req_id, ok=False, error=str(exc), code=BAD_REQUEST
            )
            return

        refresh = bool(params.get("refresh", False))
        if override is None and not refresh and self._cached is not None:
            report = self._cached
            cached = True
        else:
            report = self._scan(search_paths)
            cached = False
            if override is None:
                self._cached = report

        payload = report.to_dict()
        payload["cached"] = cached
        payload["max_plugins"] = MAX_PLUGINS
        await self._respond(ws, req_id, ok=True, payload=payload)

    async def _handle_inspect(
        self, ws: Any, req_id: Any, params: Any, session_id: Any
    ) -> None:
        del session_id
        params = params if isinstance(params, dict) else {}
        raw_path = params.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            await self._respond(
                ws,
                req_id,
                ok=False,
                error="path must be a non-empty string",
                code=BAD_REQUEST,
            )
            return
        target = Path(raw_path.strip()).expanduser()
        if not target.is_absolute():
            target = self._user_plugin_dir() / target
        resolved = Path(os.path.abspath(str(target)))
        allowed = self._resolve_search_paths(None)
        if not self._is_within(resolved, allowed):
            await self._respond(
                ws,
                req_id,
                ok=False,
                error="path is outside the extension search paths",
                code=PATH_NOT_ALLOWED,
            )
            return
        report = doctor_engine.inspect_directory(
            resolved,
            running_version=running_jiuwenswarm_version(),
            is_installed=self._dependency_installed,
        )
        await self._respond(ws, req_id, ok=True, payload=report.to_dict())

    async def _handle_export(
        self, ws: Any, req_id: Any, params: Any, session_id: Any
    ) -> None:
        del session_id
        params = params if isinstance(params, dict) else {}
        fmt = str(params.get("format", "json")).strip().lower()
        report = self._cached or self._scan(None)
        if fmt == "markdown":
            content = doctor_engine.render_markdown(report)
            filename, mime = "plugin-doctor-report.md", "text/markdown"
        elif fmt == "json":
            content = json.dumps(report.to_dict(), ensure_ascii=False, indent=2)
            filename, mime = "plugin-doctor-report.json", "application/json"
        else:
            await self._respond(
                ws,
                req_id,
                ok=False,
                error="format must be 'json' or 'markdown'",
                code=BAD_REQUEST,
            )
            return
        await self._respond(
            ws,
            req_id,
            ok=True,
            payload={
                "format": fmt,
                "filename": filename,
                "mime": mime,
                "content": content,
            },
        )

    # ------------------------------------------------------------------
    # 搜索目录（复刻 extensions/manager.py 的语义）
    # ------------------------------------------------------------------

    def _scan(self, search_paths: Any) -> doctor_engine.DoctorReport:
        paths = self._resolve_search_paths(search_paths)
        report = doctor_engine.scan_search_paths(
            paths,
            running_version=running_jiuwenswarm_version(),
            is_installed=self._dependency_installed,
        )
        if len(report.plugins) > MAX_PLUGINS:
            trimmed = report.plugins[:MAX_PLUGINS]
            return doctor_engine.DoctorReport(
                search_paths=report.search_paths,
                plugins=tuple(trimmed),
                scan_findings=report.scan_findings,
                missing_paths=report.missing_paths,
                running_version=report.running_version,
                generated_at=report.generated_at,
                score=report.score,
            )
        return report

    def _resolve_search_paths(self, override: Any) -> list[Path]:
        if override is None:
            values = [
                str(self._user_plugin_dir()),
                *self._config_extension_dirs(),
                DEFAULT_EXTENSION_DIR,
            ]
        else:
            values = list(override)
        result: list[Path] = []
        seen: set[str] = set()
        for value in values:
            for candidate in self._candidate_paths(value):
                if not candidate.is_dir():
                    continue
                key = str(candidate.resolve()).lower()
                if key in seen:
                    continue
                seen.add(key)
                result.append(candidate.resolve())
        return result

    def _candidate_paths(self, value: str) -> list[Path]:
        """复刻 ``manager._extension_search_path_candidates`` 的兜底行为。"""

        path = Path(value)
        if path.is_absolute():
            return [path]
        parts = tuple(part.lower() for part in path.parts if part not in ("", "."))
        candidates = [path]
        if parts == ("jiuwenswarm", "extensions"):
            candidates.append(Path(__file__).resolve().parents[1])
        return candidates

    def _config_extension_dirs(self) -> list[str]:
        extensions = self._config.get("extensions")
        if not isinstance(extensions, dict):
            return []
        dirs = extensions.get("extension_dirs")
        if not isinstance(dirs, str):
            return []
        return [part.strip() for part in dirs.split(";") if part.strip()]

    def _user_plugin_dir(self) -> Path:
        try:
            from jiuwenswarm.common.utils import get_root_dir

            return Path(get_root_dir()) / "application_plugins"
        except Exception:  # noqa: BLE001 - 独立运行/裁剪环境下的兜底
            data_dir = os.environ.get("JIUWENSWARM_DATA_DIR") or str(
                Path.home() / ".jiuwenswarm"
            )
            return Path(data_dir) / "application_plugins"

    @staticmethod
    def _as_dir_list(value: Any) -> list[str]:
        if isinstance(value, str):
            parts = [part.strip() for part in value.split(";")]
        elif isinstance(value, (list, tuple)):
            parts = [str(item).strip() for item in value]
        else:
            raise ValueError("search_paths must be a list of strings or a ';' string")
        parts = [part for part in parts if part]
        if not parts:
            raise ValueError("search_paths must not be empty")
        return parts

    @staticmethod
    def _is_within(target: Path, roots: list[Path]) -> bool:
        for root in roots:
            try:
                target.relative_to(root)
            except ValueError:
                continue
            return True
        return False

    @staticmethod
    def _dependency_installed(name: str) -> bool:
        from importlib.metadata import PackageNotFoundError, version

        try:
            version(name)
        except PackageNotFoundError:
            return False
        except Exception:  # noqa: BLE001 - 包名不合法时也视为查不到
            return False
        return True


async def register_extensions(registry: Any) -> list[PluginDoctorApplicationPlugin]:
    """宿主扩展入口。"""

    plugin = PluginDoctorApplicationPlugin()
    registry.register_application_plugin(plugin)
    return [plugin]
