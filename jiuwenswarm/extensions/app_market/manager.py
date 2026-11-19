"""AppMarketManager — 应用市场管理器，封装 MarketService 并提供 WS 响应格式适配."""
from __future__ import annotations

import logging
import asyncio
import hashlib
import base64
import time
from pathlib import Path
from typing import Any
from html.parser import HTMLParser
from urllib.parse import quote

from .app_market.market_service import MarketService
from .app_market.models import normalize_app_version
from .app_market.storage import MarketStorage
from .resources import AppServiceResources, SystemResources

logger = logging.getLogger(__name__)


class _DescriptionParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.description = ""
        self.title = ""
        self.application_name = ""
        self.application_version = ""
        self.icon_path = ""
        self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data

    def handle_endtag(self, tag):
        if tag.lower() == "title":
            self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "title":
            self._in_title = True
        values = dict(attrs)
        if tag.lower() == "meta" and values.get("name", "").lower() == "description":
            self.description = values.get("content", "")[:500]
        if tag.lower() == "meta" and values.get("name", "").lower() == "application-name":
            self.application_name = values.get("content", "").strip()[:128]
        if tag.lower() == "meta" and values.get("name", "").lower() == "application-version":
            self.application_version = values.get("content", "").strip()
        if tag.lower() == "link" and "icon" in values.get("rel", "").lower().split():
            self.icon_path = values.get("href", "")


def _metadata(entry: Path) -> dict[str, str]:
    parser = _DescriptionParser()
    try:
        with entry.open(encoding="utf-8", errors="replace") as source:
            parser.feed(source.read(65536))
    except OSError:
        pass
    icon_url = ""
    if parser.icon_path:
        icon = (entry.parent / parser.icon_path).resolve()
        mime = {
            ".svg": "image/svg+xml",
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".webp": "image/webp",
        }.get(icon.suffix.lower())
        try:
            icon.relative_to(entry.parent.resolve())
            if mime and icon.is_file() and icon.stat().st_size <= 262144:
                icon_url = "data:" + mime + ";base64," + base64.b64encode(icon.read_bytes()).decode("ascii")
        except (OSError, ValueError):
            pass
    return {
        "name": parser.application_name or parser.title.strip()[:128] or entry.stem,
        "description": parser.description or parser.title.strip()[:500],
        "icon_url": icon_url,
        "version": parser.application_version,
    }


def _description(entry: Path) -> str:
    return _metadata(entry)["description"]


class AppMarketManager:
    """适配 MarketService 到 WS 方法处理器格式."""

    def __init__(self, data_dir: str) -> None:
        self._service = MarketService(data_dir)
        self._recovery_tasks = {}
        self._resources = AppServiceResources()
        self._system_resources = SystemResources()
        self._resources_lock = asyncio.Lock()

    @property
    def storage(self) -> MarketStorage:
        """市场持久化存储，供编辑会话等门面组件复用。"""
        return self._service.storage

    def configure_gateway(self, gateway_port=None, web_port=None, gateway_host=None, app_host="127.0.0.1") -> None:
        """记录运行中 WorkSwarm 实例注入的网关与 Web 地址。"""
        self._service.configure_gateway(
            gateway_port=gateway_port,
            web_port=web_port,
            gateway_host=gateway_host,
            app_host=app_host,
        )

    async def publish_app(
        self,
        entry_path: str,
        source_session: str,
        owner_id: str = "",
        version: str | None = None,
    ) -> dict[str, Any]:
        """Adapt a generated HTML entry after the user confirms publishing.

        ``version`` is the explicit publish request. When absent the entry's
        ``application-version`` meta is used; otherwise the previously
        published version is kept so republishing cannot silently reset it.
        """
        entry = Path(entry_path).expanduser().resolve()
        if not entry.is_file() or entry.suffix.lower() not in (".html", ".htm"):
            raise ValueError("A readable HTML application entry is required")
        app_id = "local-" + hashlib.sha256(
            (owner_id + "\0" + str(entry) if owner_id else str(entry)).encode("utf-8")
        ).hexdigest()[:24]
        metadata = _metadata(entry)
        requested_version = normalize_app_version(version)
        try:
            declared_version = normalize_app_version(metadata["version"])
        except ValueError as exc:
            raise ValueError(f"invalid application-version meta: {exc}") from exc
        existing = self._service.get_app(app_id)
        resolved_version = (
            requested_version
            or declared_version
            or (existing.version if existing is not None else "")
            or "1.0.0"
        )
        manifest = await self._service.publish_app(
            str(entry.parent),
            {
                "app_id": app_id,
                "name": metadata["name"],
                "version": resolved_version,
                "run_type": "static",
                "page_entry_path": entry.name,
                "source_session": source_session,
                "description": metadata["description"],
                "icon_url": metadata["icon_url"],
                "owner_id": owner_id,
                "permissions": ["ai"] if "WorkSwarmApp" in entry.read_text(encoding="utf-8", errors="replace") else [],
            },
        )
        if manifest is None:
            raise ValueError("Application adaptation or validation failed")
        saved = self._service.get_app(app_id)
        if saved is None:
            raise ValueError("Application registration failed")
        return {"app_id": saved.app_id, "name": saved.name, "version": saved.version}

    def list_apps(self, owner_id: str = "") -> dict[str, Any]:
        apps = [a for a in self._service.list_apps() if a.owner_id == owner_id]
        return {
            "apps": [
                {
                    "app_id": a.app_id,
                    "entry_path": str((Path(a.work_dir) / (a.page_entry_path or "index.html")).resolve()),
                    "source_session": a.source_session,
                    "editor_session_id": a.editor_session_id,
                    "name": a.name,
                    "version": a.version,
                    "description": a.description
                    or _description(Path(a.work_dir) / (a.page_entry_path or "index.html")),
                    "icon_url": a.icon_url,
                    "run_type": a.run_type,
                    "default_port": a.default_port,
                    "status_mode": a.status_mode,
                    "startup_timeout_seconds": a.startup_timeout_seconds,
                    "created_at": a.created_at,
                }
                for a in apps
            ]
        }

    def list_instances(self, owner_id: str = "") -> dict[str, Any]:
        instances = [i for i in self._service.list_instances() if i.owner_id == owner_id]
        result = []
        for inst in instances:
            state = inst.state
            if (
                state in ("running", "starting", "stopping")
                and inst.pid
                and not self._service.is_process_alive(inst.pid)
            ):
                task = self._recovery_tasks.get(inst.instance_id)
                if task is None or (task.done() and not inst.detail.startswith("AI 会话")):
                    task = asyncio.create_task(self._service.stop_app(inst.instance_id))
                    self._recovery_tasks[inst.instance_id] = task
                    task.add_done_callback(
                        lambda finished, instance_id=inst.instance_id: self._forget_recovery_task(
                            instance_id, finished,
                        )
                    )
                state = "stopping"
            if inst.pid and state in ("running", "starting"):
                if not self._service.is_process_alive(inst.pid):
                    state = "stopped"
            url, web_url = self._instance_urls(inst.app_id, inst.port)
            result.append(
                {
                    "instance_id": inst.instance_id,
                    "app_id": inst.app_id,
                    "port": inst.port,
                    "state": state,
                    "detail": inst.detail,
                    "created_at": inst.created_at,
                    "started_at": inst.started_at,
                    "url": url,
                    "web_url": web_url,
                }
            )
        return {"instances": result}

    def _forget_recovery_task(self, instance_id: str, task: asyncio.Task) -> None:
        """Drop a finished recovery task so the map tracks only live work."""
        if self._recovery_tasks.get(instance_id) is task:
            self._recovery_tasks.pop(instance_id, None)

    async def resources(self, owner_id: str = "") -> dict[str, Any]:
        async with self._resources_lock:
            instances = self._service.list_instances()
            readings = await asyncio.to_thread(
                self._resources.sample, instances,
            )
            allowed = {item.app_id for item in instances if item.owner_id == owner_id}
            apps = [item for item in readings if item["appId"] in allowed]
            system = await asyncio.to_thread(self._system_resources.sample)
            return {"apps": apps, "system": system, "sampledAt": time.time() * 1000}

    async def start_app(self, app_id: str, owner_id: str = "") -> dict[str, Any] | None:
        instance = await self._service.start_app(app_id, owner_id=owner_id)
        if instance is None or instance.state != "running":
            return None
        url, web_url = self._instance_urls(instance.app_id, instance.port)
        return {
            "instance_id": instance.instance_id,
            "app_id": instance.app_id,
            "port": instance.port,
            "state": instance.state,
            "detail": instance.detail,
            "url": url,
            "web_url": web_url,
        }

    def start_error(self, app_id: str) -> str:
        instances = self._service.list_instances(app_id)
        latest = max(instances, key=lambda item: item.created_at, default=None)
        return latest.detail if latest and latest.detail else "启动失败"

    def _instance_urls(self, app_id: str, port: int) -> tuple[str, str]:
        manifest = self._service.get_app(app_id)
        entry = manifest.page_entry_path if manifest else None
        path = quote((entry or "").replace("\\", "/").lstrip("/"), safe="/")
        # Isolate the entire browser origin, including cached assets and service workers.
        digest = hashlib.sha256(app_id.encode("utf-8")).hexdigest()[:24]
        return f"http://127.0.0.1:{port}/{path}", f"http://workswarm-app-{digest}.localhost:{port}/{path}"

    async def stop_app(self, instance_id: str, owner_id: str = "") -> bool:
        instance = self._service.get_instance(instance_id)
        if not instance or instance.owner_id != owner_id:
            return False
        return await self._service.stop_app(instance_id)

    async def delete_app(self, app_id: str, owner_id: str = "") -> bool:
        app = self._service.get_app(app_id)
        if not app or app.owner_id != owner_id:
            return False
        return await self._service.delete_app(app_id)

    async def shutdown(self) -> None:
        await self._service.shutdown()
