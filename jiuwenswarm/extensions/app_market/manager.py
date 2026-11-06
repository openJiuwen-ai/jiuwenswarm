"""AppMarketManager — 应用市场管理器，封装 MarketService 并提供 WS 响应格式适配."""
from __future__ import annotations

import logging
import asyncio
import hashlib
import base64
from pathlib import Path
from typing import Any
from html.parser import HTMLParser
from urllib.parse import quote

from .app_market.market_service import MarketService

logger = logging.getLogger(__name__)


class _DescriptionParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.description = ""
        self.title = ""
        self.application_name = ""
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
        mime = {".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}.get(icon.suffix.lower())
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
    }


def _description(entry: Path) -> str:
    return _metadata(entry)["description"]


class AppMarketManager:
    """适配 MarketService 到 WS 方法处理器格式."""

    def __init__(self, data_dir: str) -> None:
        self._service = MarketService(data_dir)
        self._recovery_tasks = {}

    async def publish_app(self, entry_path: str, source_session: str, owner_id: str = "") -> dict[str, Any]:
        """Adapt a generated HTML entry after the user confirms publishing."""
        entry = Path(entry_path).expanduser().resolve()
        if not entry.is_file() or entry.suffix.lower() not in (".html", ".htm"):
            raise ValueError("A readable HTML application entry is required")
        app_id = "local-" + hashlib.sha256((owner_id + "\0" + str(entry) if owner_id else str(entry)).encode("utf-8")).hexdigest()[:24]
        metadata = _metadata(entry)
        manifest = await self._service.publish_app(
            str(entry.parent),
            {
                "app_id": app_id,
                "name": metadata["name"],
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
        saved = self._service._storage.load_manifest(app_id)
        if saved is None:
            raise ValueError("Application registration failed")
        return {"app_id": saved.app_id, "name": saved.name}

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
                    "description": a.description or _description(Path(a.work_dir) / (a.page_entry_path or "index.html")),
                    "icon_url": a.icon_url,
                    "run_type": a.run_type,
                    "default_port": a.default_port,
                    "status_mode": a.status_mode,
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
            if state in ("running", "starting", "stopping") and inst.pid and not self._service._supervisor.is_alive(inst.pid):
                task = self._recovery_tasks.get(inst.instance_id)
                if task is None or (task.done() and not inst.detail.startswith("AI 会话")):
                    self._recovery_tasks[inst.instance_id] = asyncio.create_task(self._service.stop_app(inst.instance_id))
                state = "stopping"
            if inst.pid and state in ("running", "starting"):
                if not self._service._supervisor.is_alive(inst.pid):
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
