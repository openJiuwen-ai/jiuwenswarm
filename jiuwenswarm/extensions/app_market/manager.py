"""AppMarketManager — 应用市场管理器，封装 MarketService 并提供 WS 响应格式适配."""
from __future__ import annotations

import logging
import asyncio
import hashlib
import base64
import os
import shutil
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from html.parser import HTMLParser
from urllib.parse import quote

from jiuwenswarm.enterprise.edition import is_enterprise

from .app_ai_roles import load_ai_role_groups
from .app_market.market_service import MarketService
from .app_market.models import AppManifest, normalize_app_version
from .app_market.run_consent import RunConsentStore
from .app_market.storage import MarketStorage
from .editor_sessions import AppEditorSessions, CoreRequest, EditorSessionError
from .resources import AppServiceResources, SystemResources

logger = logging.getLogger(__name__)


class AppMarketError(Exception):
    """Stable business error surfaced through the WS response envelope."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class RunConsentRequired(AppMarketError):
    def __init__(self, artifact_sha256: str) -> None:
        super().__init__(
            "app_run_consent_required",
            "First run requires explicit confirmation for this application artifact",
        )
        self.artifact_sha256 = artifact_sha256


def _owner_identity_required() -> bool:
    """Whether an app operation must carry a non-empty owner identity.

    Personal/desktop deployments have no login: the WebChannel connection
    carries an empty user_id. That empty value is the single local owner, so
    Hub installs and starts must not be rejected for lack of an identity.
    Enterprise keeps the strict rule and never shares an anonymous owner.
    """
    return is_enterprise()


@dataclass
class InstallJob:
    job_id: str
    owner_id: str
    kind: str  # "install" | "update"
    app_id: str = ""
    state: str = "queued"
    phase: str = "queued"
    error_code: str = ""
    error: str = ""
    cancellable: bool = True
    cancel_requested: bool = False
    result: dict[str, Any] | None = None
    task: asyncio.Task | None = field(default=None, repr=False)

    def snapshot(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "kind": self.kind,
            "app_id": self.app_id,
            "state": self.state,
            "phase": self.phase,
            "cancellable": self.cancellable,
            "error": {"code": self.error_code, "message": self.error}
            if self.error_code
            else None,
            "result": self.result,
        }


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


def _local_app_id(entry: Path, owner_id: str) -> str:
    key = f"{owner_id}\0{entry}" if owner_id else str(entry)
    return "local-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]


def get_app_description(app: AppManifest) -> str:
    """Use the registered description, with an HTML fallback for older manifests."""
    if app.description:
        return app.description
    entry = Path(app.work_dir) / (app.page_entry_path or "index.html")
    return _metadata(entry)["description"]


class AppMarketManager:
    """适配 MarketService 到 WS 方法处理器格式."""

    def __init__(self, data_dir: str) -> None:
        self._service = MarketService(data_dir)
        self._data_dir = data_dir
        self._recovery_tasks = {}
        self._resources = AppServiceResources()
        self._system_resources = SystemResources()
        self._resources_lock = asyncio.Lock()
        self._consents = RunConsentStore(data_dir)
        self._jobs: dict[str, InstallJob] = {}
        self._operation_locks: dict[str, asyncio.Lock] = {}
        self._local_work_dirs_lock = asyncio.Lock()
        self.editors = AppEditorSessions(self._service.storage)
        self._service.allow_hub_code = False
        self._operation_dir = Path(data_dir) / "install_ops"
        self._recover_interrupted_installs()

    def _recover_interrupted_installs(self) -> None:
        """Clean crash leftovers for local installs after a restart."""
        from .hub_install import recover_interrupted_operations

        recovered = recover_interrupted_operations(self._operation_dir, self.storage)
        if recovered:
            logger.warning("Recovered %d interrupted app install(s)", len(recovered))

    def _operation_lock(self, app_id: str) -> asyncio.Lock:
        return self._operation_locks.setdefault(app_id, asyncio.Lock())

    def _reject_active_job(self, app_id: str) -> None:
        active = self._active_job(app_id)
        if active is not None:
            raise AppMarketError(
                "app_operation_conflict", f"Application is busy with a {active.kind} operation",
            )

    async def edit_session(self, app_id: str, owner_id: str, request: Any, options: dict[str, Any]) -> dict:
        self._reject_active_job(app_id)
        async with self._operation_lock(app_id):
            self._reject_active_job(app_id)
            manifest = self._service.get_app(app_id)
            if manifest is not None and manifest.owner_id == owner_id and manifest.source == "hub":
                raise AppMarketError("app_convert_required", "Convert this Hub app to a local app before editing")
            return await self.editors.open(app_id, owner_id, request, options)

    @property
    def storage(self) -> MarketStorage:
        """市场持久化存储，供编辑会话等门面组件复用。"""
        return self._service.storage

    @property
    def service(self) -> MarketService:
        """底层市场服务，供 Hub 安装器等门面组件复用。"""
        return self._service

    def configure_gateway(self, gateway_port=None, web_port=None, gateway_host=None, app_host="127.0.0.1") -> None:
        """记录运行中 WorkSwarm 实例注入的网关与 Web 地址。"""
        configured = os.environ.get("APP_MARKET_ALLOW_HUB_CODE")
        if configured is None:
            self._service.allow_hub_code = str(app_host).strip().lower() in {"localhost", "127.0.0.1", "::1"}
        else:
            self._service.allow_hub_code = configured.strip().lower() in {"1", "true", "yes", "on"}
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

        An explicit version must match the HTML version if both are set.
        With neither set, keep the version from the previous registration.
        """
        async with self._local_work_dirs_lock:
            entry = Path(entry_path).expanduser().resolve()
            if not entry.is_file() or entry.suffix.lower() not in (".html", ".htm"):
                raise ValueError("A readable HTML application entry is required")
            if self.storage.contains_hub_install(entry):
                raise ValueError("Convert this Hub app to a local app before registering it")
            app_id = _local_app_id(entry, owner_id)
            existing = self._service.get_app(app_id)
            if existing is None:
                # A converted app keeps its installation ID and project binding.
                existing = self.storage.find_local_app(entry, owner_id)
                if existing is not None:
                    app_id = existing.app_id
            metadata = _metadata(entry)
            requested_version = normalize_app_version(version)
            try:
                declared_version = normalize_app_version(metadata["version"])
            except ValueError as exc:
                raise ValueError(f"invalid application-version meta: {exc}") from exc
            if requested_version and declared_version and requested_version != declared_version:
                raise ValueError("version must match the HTML application-version")
            resolved_version = (
                requested_version
                or declared_version
                or (existing.version if existing is not None else "")
                or "1.0.0"
            )
            uses_ai = bool(load_ai_role_groups(entry.parent)) or (
                "WorkSwarmApp" in entry.read_text(encoding="utf-8", errors="replace")
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
                    "permissions": ["ai"] if uses_ai else [],
                },
            )
            if manifest is None:
                raise ValueError("Application adaptation or validation failed")
            saved = self._service.get_app(app_id)
            if saved is None:
                raise ValueError("Application registration failed")
            return {"app_id": saved.app_id, "name": saved.name, "version": saved.version}

    async def convert_to_local(
        self, app_id: str, owner_id: str, project_request: CoreRequest | None = None
    ) -> dict[str, Any]:
        """Switch one installation to local maintenance without changing its files.

        Preserve project and editor bindings for this directory. Resolve a
        replacement binding when needed, and stop instances using the old one.
        """
        self._reject_active_job(app_id)
        async with self._operation_lock(app_id):
            self._reject_active_job(app_id)
            manifest = self._service.get_app(app_id)
            if manifest is None or manifest.owner_id != owner_id:
                raise AppMarketError("app_work_dir_missing", "Installed application not found")
            if not owner_id and _owner_identity_required():
                raise AppMarketError("app_work_dir_missing", "Installed application not found")
            if manifest.source == "local":
                return {"app_id": app_id, "name": manifest.name, "version": manifest.version}
            if manifest.source != "hub" or not manifest.work_dir or not Path(manifest.work_dir).is_dir():
                raise AppMarketError("app_work_dir_missing", "Installed application not found")
            source = Path(manifest.work_dir)
            if not self._service.allow_hub_code and (source / "scripts/app_server.py").is_file():
                raise AppMarketError("app_run_disabled", "Running Hub server scripts is disabled for this deployment")
            if self.storage.contains_local_app(source):
                raise AppMarketError(
                    "app_operation_conflict", "Remove the duplicate local registration before converting"
                )
            previous_context = (manifest.project_id, manifest.project_dir, manifest.project_work_mode)
            self._detach_stale_project(manifest)
            detached_context = (manifest.project_id, manifest.project_dir, manifest.project_work_mode)
            if project_request is not None and not self._service.has_project_binding(manifest):
                try:
                    await self.bind_app_project(manifest, project_request)
                except Exception as exc:  # noqa: BLE001 - start can retry project resolution
                    manifest.project_id, manifest.project_dir, manifest.project_work_mode = detached_context
                    logger.warning("Project binding failed while converting %s: %s", app_id, exc)
            if (manifest.project_id, manifest.project_dir, manifest.project_work_mode) != previous_context:
                await self._stop_active_instances(app_id)
            latest = self._service.get_app(app_id)
            if latest is None or (latest.owner_id, latest.source, latest.work_dir) != (
                owner_id, "hub", manifest.work_dir,
            ):
                raise AppMarketError("app_operation_conflict", "Application changed during conversion")
            # Pinning can update the registration while Core or an instance is stopping.
            manifest.pinned = latest.pinned
            record = self.storage.load_install_record(app_id)
            if record and (record.get("owner_id"), record.get("work_dir")) == (owner_id, manifest.work_dir):
                manifest.local_release_id = str(record.get("release_id") or "")
            manifest.source = "local"
            manifest.hub_asset_id = ""
            manifest.hub_url = ""
            manifest.artifact_sha256 = ""
            try:
                self.storage.save_manifest(app_id, manifest)
            except OSError as exc:
                raise AppMarketError("app_register_failed", "Could not save the local registration") from exc
            try:
                self.storage.delete_install_record(app_id)
                self._consents.revoke_app(app_id)
            except OSError:
                # The manifest is authoritative once converted.
                logger.warning("Could not clear the old Hub metadata for %s", app_id, exc_info=True)
            return {
                "app_id": app_id,
                "name": manifest.name,
                "version": manifest.version,
                "project_id": manifest.project_id,
            }

    @staticmethod
    def _detach_stale_project(manifest: AppManifest) -> None:
        """Drop bindings that belong to another directory or to an old copy.

        A Hub install can reuse an app_id from an earlier local conversion. That
        project and its editor sessions belong to the old copy, not to this
        directory, so conversion must not silently keep them.
        """
        def normalized(value: str) -> str:
            try:
                return os.path.normcase(str(Path(value).expanduser().resolve()))
            except (OSError, RuntimeError):
                return os.path.normcase(os.path.abspath(value))

        work_dir = normalized(manifest.work_dir)
        project_dir = normalized(manifest.project_dir) if manifest.project_dir else ""
        if manifest.project_id and project_dir == work_dir:
            return
        manifest.project_id = ""
        manifest.project_dir = ""
        manifest.project_work_mode = ""
        manifest.source_session = ""
        manifest.editor_session_id = ""
        manifest.editor_create_token = ""
        manifest.editor_create_pending = False
        manifest.editor_session_ids = []

    @staticmethod
    def _created_at_key(app: AppManifest) -> datetime:
        """Parse timestamps emitted by local and Hub registrations."""
        raw = str(app.created_at or "").strip()
        if not raw:
            return datetime.min.replace(tzinfo=timezone.utc)
        try:
            value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return datetime.min.replace(tzinfo=timezone.utc)
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value

    @classmethod
    def _sorted_apps(cls, apps: list[AppManifest]) -> list[AppManifest]:
        """Pinned first, then newest registrations; stable within equal times."""
        ordered = sorted(apps, key=cls._created_at_key, reverse=True)
        ordered.sort(key=lambda app: not app.pinned)
        return ordered

    def list_apps(self, owner_id: str = "") -> dict[str, Any]:
        apps = self._sorted_apps([a for a in self._service.list_apps() if a.owner_id == owner_id])
        return {
            "apps": [
                {
                    "app_id": a.app_id,
                    "entry_path": str((Path(a.work_dir) / (a.page_entry_path or "index.html")).resolve()),
                    "source_session": a.source_session,
                    "editor_session_id": a.editor_session_id,
                    "name": a.name,
                    "version": a.version,
                    "description": get_app_description(a),
                    "icon_url": a.icon_url,
                    "run_type": a.run_type,
                    "default_port": a.default_port,
                    "status_mode": a.status_mode,
                    "startup_timeout_seconds": a.startup_timeout_seconds,
                    "created_at": a.created_at,
                    "pinned": bool(a.pinned),
                    "source": a.source,
                    "hub_asset_id": a.hub_asset_id,
                    "artifact_sha256": a.artifact_sha256,
                    "requires_project": a.requires_project,
                    "project_id": a.project_id,
                    "project_dir": a.project_dir,
                    "work_dir_missing": not Path(a.work_dir).is_dir(),
                }
                for a in apps
            ]
        }

    async def get_ai_role_groups(self, app_id: str, owner_id: str) -> list[dict[str, Any]]:
        manifest = self._service.get_app(app_id)
        if manifest is None or manifest.owner_id != owner_id:
            raise AppMarketError("not_found", "Application not found")
        if not manifest.work_dir:
            return []
        try:
            return await asyncio.to_thread(load_ai_role_groups, Path(manifest.work_dir), include_avatars=True)
        except ValueError as exc:
            raise AppMarketError("app_ai_roles_invalid", str(exc)) from exc

    def get_editor_app(self, session_id: str, owner_id: str) -> dict[str, Any] | None:
        """Resolve an editor's preview without listing other session bindings."""
        for app in self._service.list_apps():
            if app.owner_id == owner_id and session_id in app.iter_editor_session_ids():
                return {
                    "app_id": app.app_id,
                    "name": app.name,
                    "startup_timeout_seconds": app.startup_timeout_seconds,
                }
        return None

    def set_app_pinned(self, app_id: str, owner_id: str = "", pinned: bool = False) -> dict[str, Any]:
        """Pin or unpin one registration; the list order is recomputed on read."""
        manifest = self._service.get_app(app_id)
        if manifest is None or manifest.owner_id != owner_id:
            raise AppMarketError("not_found", "Application not found")
        manifest.pinned = bool(pinned)
        try:
            self.storage.save_manifest(app_id, manifest)
        except OSError as exc:
            raise AppMarketError("app_register_failed", "Could not save the application") from exc
        return {"app_id": app_id, "pinned": manifest.pinned}

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

    async def start_app(
        self,
        app_id: str,
        owner_id: str = "",
        run_consent: dict[str, Any] | None = None,
        project_request: CoreRequest | None = None,
    ) -> dict[str, Any] | None:
        self._reject_active_job(app_id)
        async with self._operation_lock(app_id):
            self._reject_active_job(app_id)
            manifest = self._service.get_app(app_id)
            if manifest is not None and manifest.owner_id and manifest.owner_id != owner_id:
                # Empty owner is the local single-user identity, so a local app
                # that records an owner stays startable there. Enterprise
                # deployments keep the strict ownership rule.
                if owner_id or _owner_identity_required():
                    raise AppMarketError("permission_denied", "Application belongs to another user")
            if manifest is not None and manifest.source == "hub":
                # Empty owner is the local single-user identity; an app owned by
                # a named user still requires that exact identity.
                if manifest.owner_id != owner_id or (not owner_id and _owner_identity_required()):
                    raise AppMarketError("permission_denied", "Application belongs to another user")
                if not self._service.allow_hub_code and (Path(manifest.work_dir) / "scripts/app_server.py").is_file():
                    raise AppMarketError(
                        "app_run_disabled", "Running Hub server scripts is disabled for this deployment",
                    )
                record = self.storage.load_install_record(app_id)
                if record is None or (
                    record.get("owner_id"),
                    record.get("work_dir"),
                    record.get("version"),
                    record.get("artifact_sha256"),
                ) != (owner_id, manifest.work_dir, manifest.version, manifest.artifact_sha256):
                    raise AppMarketError("app_operation_conflict", "Application install state is incomplete")
                self._require_run_consent(app_id, owner_id, run_consent)
            if manifest is not None:
                if not manifest.work_dir or not Path(manifest.work_dir).is_dir():
                    raise AppMarketError("app_work_dir_missing", "Application working directory is missing")
                previous_context = (manifest.project_id, manifest.project_dir, manifest.project_work_mode)
                await self.bind_app_project(manifest, project_request)
                current_context = (manifest.project_id, manifest.project_dir, manifest.project_work_mode)
                if current_context != previous_context:
                    await self._stop_active_instances(app_id)
                    latest = self.storage.load_manifest(app_id)
                    if latest is None or latest.owner_id != manifest.owner_id or latest.work_dir != manifest.work_dir:
                        raise AppMarketError("app_operation_conflict", "Application changed while binding its project")
                    latest.project_id, latest.project_dir, latest.project_work_mode = current_context
                    self.storage.save_manifest(app_id, latest)
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

    def _require_run_consent(
        self,
        app_id: str,
        owner_id: str,
        run_consent: dict[str, Any] | None,
    ) -> None:
        """Hub apps need consent bound to the installed artifact digest."""
        manifest = self._service.get_app(app_id)
        if manifest is None or manifest.source != "hub":
            return
        expected = (manifest.artifact_sha256 or "").strip().lower()
        if not expected:
            raise AppMarketError("app_artifact_changed", "Application has no registered artifact digest")
        supplied = ""
        if isinstance(run_consent, dict):
            supplied = str(run_consent.get("artifact_sha256") or "").strip().lower()
        if supplied:
            if supplied != expected:
                raise AppMarketError(
                    "app_artifact_changed",
                    "The application artifact changed since the consent was requested",
                )
            self._consents.grant(owner_id, app_id, expected)
            return
        if not self._consents.has(owner_id, app_id, expected):
            raise RunConsentRequired(manifest.artifact_sha256)

    async def bind_app_project(self, manifest: AppManifest, request: CoreRequest | None) -> None:
        """Resolve or create the project and set its binding on the manifest."""
        if request is None:
            if self._service.has_project_binding(manifest):
                return
            raise AppMarketError("app_project_unavailable", "Application project binding is missing")

        async def call(method: Any, params: dict[str, Any]) -> dict[str, Any]:
            ok, payload = await request(method, params)
            if not ok:
                raise EditorSessionError(payload if isinstance(payload, dict) else {"error": str(payload)})
            return payload

        try:
            match = await self.editors.resolve_project(manifest, call)
        except EditorSessionError as exc:
            raise AppMarketError("app_project_unavailable", str(exc)) from exc
        manifest.project_id = str(match.get("project_id") or "").strip()
        manifest.project_work_mode = str(match.get("work_mode") or "").strip()
        manifest.project_dir = str(
            match.get("project_dir") or Path(manifest.work_dir).expanduser().resolve()
        ).strip()
        if not self._service.has_project_binding(manifest):
            raise AppMarketError(
                "app_project_unavailable",
                "Project resolution did not return an available project",
            )

    # ── Hub 应用市场 ──────────────────────────────────────

    @staticmethod
    def _hub_port():
        from jiuwenswarm.server.runtime.marketplace.hub_asset_port import (
            create_default_hub_asset_port,
        )

        return create_default_hub_asset_port()

    def _installed_hub_app(self, asset_id: str, owner_id: str, hub_url: str):
        from .hub_install import app_install_id

        app_id = app_install_id(asset_id, owner_id, hub_url)
        manifest = self._service.get_app(app_id)
        return app_id, manifest if manifest and manifest.source == "hub" else None

    @staticmethod
    def _hub_endpoint():
        from jiuwenswarm.server.runtime.marketplace.hub_asset_port import (
            create_default_hub_asset_port,
        )

        try:
            port = create_default_hub_asset_port()
        except ValueError as exc:
            raise AppMarketError("hub_unavailable", f"Hub 配置无效: {exc}") from exc
        return port, str(getattr(port, "base_url", "") or "")

    @staticmethod
    async def _hub_call(awaitable):
        """Map Hub transport/protocol failures to stable market error codes."""
        from jiuwenswarm.server.runtime.marketplace.hub_client import (
            HubNotFoundError,
            HubProtocolError,
        )

        try:
            return await awaitable
        except HubNotFoundError as exc:
            raise AppMarketError("not_found", f"Hub 上找不到该应用: {exc}") from exc
        except HubProtocolError as exc:
            raise AppMarketError("hub_unavailable", f"无法访问 Hub: {exc}") from exc

    async def market_list(
        self,
        *,
        query: str = "",
        page: int = 1,
        page_size: int = 20,
        owner_id: str = "",
    ) -> dict[str, Any]:
        from jiuwenswarm.server.runtime.marketplace.hub_asset_port import (
            HubSearchRequest,
        )

        port, hub_url = self._hub_endpoint()
        result = await self._hub_call(
            port.search_assets(
                HubSearchRequest(
                    kind="app",
                    query=str(query or ""),
                    page=max(1, int(page)),
                    page_size=max(1, min(int(page_size), 100)),
                )
            )
        )
        items = []
        for item in result.items:
            _, installed = self._installed_hub_app(item.asset_id, owner_id, hub_url)
            missing = installed is not None and not Path(installed.work_dir).is_dir()
            items.append(
                {
                    "asset_id": item.asset_id,
                    "name": item.package_name,
                    "display_name": item.display_name,
                    "short_desc": item.short_description,
                    "public_latest_version": item.public_latest_version,
                    "icon_uri": item.icon_uri,
                    "tags": list(item.tags),
                    "installed": installed is not None,
                    "installed_version": installed.version if installed else "",
                    "installed_app_id": installed.app_id if installed else "",
                    "work_dir_missing": missing,
                }
            )
        return {
            "items": items,
            "total": result.total,
            "page": result.page,
            "page_size": result.page_size,
        }

    async def market_detail(
        self,
        *,
        asset_id: str,
        version: str = "",
        owner_id: str = "",
    ) -> dict[str, Any]:
        from jiuwenswarm.server.runtime.marketplace.hub_asset_port import HubAssetQuery

        port, hub_url = self._hub_endpoint()
        detail = await self._hub_call(
            port.query_asset(
                HubAssetQuery(kind="app", asset_id=asset_id, version=version)
            )
        )
        _, installed = self._installed_hub_app(detail.asset_id, owner_id, hub_url)
        missing = installed is not None and not Path(installed.work_dir).is_dir()
        return {
            "asset_id": detail.asset_id,
            "name": detail.package_name,
            "display_name": detail.display_name,
            "short_desc": detail.short_description,
            "detail_desc": detail.detail_description,
            "version": detail.version,
            "icon_uri": detail.icon_uri,
            "tags": list(detail.tags),
            "file_size": detail.file_size,
            "source_size": detail.source_size,
            "requires_project": detail.requires_project,
            "permissions": list(detail.permissions),
            "has_server_script": detail.has_server_script,
            "checksum_sha256": detail.checksum_sha256,
            "installed": installed is not None,
            "installed_version": installed.version if installed else "",
            "installed_checksum_sha256": installed.artifact_sha256 if installed else "",
            "installed_app_id": installed.app_id if installed else "",
            "installed_work_dir_missing": missing,
        }

    async def install_dir_default(self) -> dict[str, Any]:
        from .hub_install import default_install_root

        root = default_install_root(
            str(os.environ.get("APP_MARKET_INSTALL_ROOT", ""))
        )
        root.mkdir(parents=True, exist_ok=True)
        try:
            free = shutil.disk_usage(root).free
        except OSError:
            free = 0
        return {"path": str(root), "free_bytes": int(free), "choosable": True}

    async def start_install(
        self,
        *,
        asset_id: str,
        version: str = "",
        install_dir: str = "",
        owner_id: str = "",
        expected_sha256: str = "",
        project_request: CoreRequest | None = None,
    ) -> dict[str, Any]:
        from .hub_install import app_install_id, default_install_root

        # Empty owner is valid in local single-user deployments; enterprise
        # deployments still require an authenticated identity.
        if not str(owner_id or "").strip() and _owner_identity_required():
            raise AppMarketError("permission_denied", "Owner identity is required")
        port = self._hub_port()
        hub_url = str(getattr(port, "base_url", "") or "")
        app_id = app_install_id(asset_id, owner_id, hub_url)
        active = self._active_job(app_id)
        if active is not None:
            return {
                "job_id": active.job_id,
                "state": active.state,
                "app_id": app_id,
                "already_installed": False,
            }
        existing = self._service.get_app(app_id)
        if existing is not None:
            if existing.source == "local":
                raise AppMarketError(
                    "install_path_conflict",
                    "To reinstall, remove the local registration and use an empty destination.",
                )
            expected = str(expected_sha256 or "").strip().lower()
            version_matches = not version or existing.version == version
            sha_matches = not expected or existing.artifact_sha256.lower() == expected
            if version_matches and sha_matches:
                return {
                    "job_id": "",
                    "state": "done",
                    "app_id": app_id,
                    "already_installed": True,
                }
            raise AppMarketError(
                "install_path_conflict",
                "The application is already installed; uninstall it or update in place",
            )
        if install_dir.strip() and not Path(install_dir).expanduser().is_absolute():
            raise AppMarketError("install_path_conflict", "Install root must be an absolute path")
        root = (
            Path(install_dir).expanduser().resolve()
            if str(install_dir or "").strip()
            else default_install_root(
                str(os.environ.get("APP_MARKET_INSTALL_ROOT", ""))
            )
        )
        job = self._new_job("install", owner_id, app_id)
        job.task = asyncio.create_task(
            self._run_install_job(
                job,
                port=port,
                asset_id=asset_id,
                version=version,
                install_dir=root,
                expected_sha256=expected_sha256,
                project_request=project_request,
            )
        )
        return {"job_id": job.job_id, "state": job.state, "app_id": app_id}

    async def start_update(
        self,
        *,
        app_id: str,
        owner_id: str = "",
        version: str = "",
        expected_sha256: str = "",
        editor_is_busy: Callable[[str], bool] | None = None,
    ) -> dict[str, Any]:
        manifest = self._service.get_app(app_id)
        if manifest is None or manifest.owner_id != owner_id:
            raise AppMarketError("app_work_dir_missing", "Application not found")
        if manifest.source != "hub":
            raise AppMarketError(
                "app_not_hub_installed",
                "Only Hub-installed applications support in-place update",
            )
        active = self._active_job(app_id)
        if active is not None:
            return {
                "job_id": active.job_id,
                "state": active.state,
                "app_id": app_id,
            }
        job = self._new_job("update", owner_id, app_id)
        job.task = asyncio.create_task(
            self._run_update_job(
                job,
                port=self._hub_port(),
                app_id=app_id,
                owner_id=owner_id,
                version=version,
                expected_sha256=expected_sha256,
                editor_is_busy=editor_is_busy,
            )
        )
        return {"job_id": job.job_id, "state": job.state, "app_id": app_id}

    def _active_job(self, app_id: str) -> InstallJob | None:
        return next(
            (
                job
                for job in self._jobs.values()
                if job.app_id == app_id and job.state in ("queued", "running")
            ),
            None,
        )

    def _new_job(self, kind: str, owner_id: str, app_id: str) -> InstallJob:
        if len(self._jobs) >= 100:
            finished = [
                key for key, job in self._jobs.items()
                if job.state in ("done", "failed", "cancelled")
            ]
            for key in finished[:50]:
                self._jobs.pop(key, None)
        job = InstallJob(
            job_id=uuid.uuid4().hex,
            owner_id=owner_id,
            kind=kind,
            app_id=app_id,
        )
        self._jobs[job.job_id] = job
        return job

    def _get_job(self, job_id: str, owner_id: str) -> InstallJob:
        job = self._jobs.get(str(job_id or ""))
        if job is None or job.owner_id != owner_id:
            raise AppMarketError("not_found", "Install job not found")
        return job

    def install_status(self, job_id: str, owner_id: str = "") -> dict[str, Any]:
        return self._get_job(job_id, owner_id).snapshot()

    def cancel_install(self, job_id: str, owner_id: str = "") -> dict[str, Any]:
        job = self._get_job(job_id, owner_id)
        if job.state in ("done", "failed", "cancelled"):
            return {"accepted": False, "job": job.snapshot()}
        if not job.cancellable:
            return {"accepted": False, "job": job.snapshot()}
        job.cancel_requested = True
        job.phase = "cancelling"
        return {"accepted": True, "job": job.snapshot()}

    @staticmethod
    def _set_job_phase(job: InstallJob, phase: str) -> None:
        job.phase = phase
        if phase in {"committing", "registering", "done"}:
            job.cancellable = False

    async def _run_install_job(
        self,
        job: InstallJob,
        *,
        port: Any,
        asset_id: str,
        version: str,
        install_dir: Path,
        expected_sha256: str,
        project_request: CoreRequest | None,
    ) -> None:
        from .hub_install import AppInstallError, install_hub_app

        committed_result: dict[str, Any] | None = None

        def remember_commit(result: dict[str, Any]) -> None:
            nonlocal committed_result
            committed_result = result

        job.state = "running"
        job.phase = "resolving"
        try:
            async with self._operation_lock(job.app_id):
                result = await install_hub_app(
                    manager=self,
                    asset_id=asset_id,
                    version=version,
                    install_root=install_dir,
                    owner_id=job.owner_id,
                    expected_sha256=expected_sha256,
                    hub_port=port,
                    on_progress=lambda phase: self._set_job_phase(job, phase),
                    cancelled=lambda: job.cancel_requested,
                    operation_dir=self._operation_dir,
                    job_id=job.job_id,
                    project_request=project_request,
                    on_committed=remember_commit,
                )
            job.state = "done"
            job.phase = "done"
            job.cancellable = False
            job.result = result
        except asyncio.CancelledError:
            if committed_result is None:
                job.state = "cancelled"
                job.phase = "cancelled"
            else:
                job.state = "done"
                job.phase = "done"
                job.result = committed_result
            job.cancellable = False
            raise
        except AppInstallError as exc:
            if exc.code == "cancelled" or job.cancel_requested:
                job.state = "cancelled"
                job.phase = "cancelled"
            else:
                job.state = "failed"
                job.error_code = exc.code
                job.error = str(exc)
            job.cancellable = False
        except AppMarketError as exc:
            job.state = "failed"
            job.error_code = exc.code
            job.error = str(exc)
            job.cancellable = False
        except Exception as exc:  # noqa: BLE001
            logger.exception("Hub app install failed: app_id=%s", job.app_id)
            job.state = "failed"
            job.error_code = "app_register_failed"
            job.error = str(exc)
            job.cancellable = False

    async def _run_update_job(
        self,
        job: InstallJob,
        *,
        port: Any,
        app_id: str,
        owner_id: str,
        version: str,
        expected_sha256: str,
        editor_is_busy: Callable[[str], bool] | None = None,
    ) -> None:
        from .hub_install import AppInstallError, update_hub_app

        job.state = "running"
        job.phase = "resolving"
        try:
            async with self._operation_lock(job.app_id):
                result = await update_hub_app(
                    manager=self,
                    app_id=app_id,
                    owner_id=owner_id,
                    version=version,
                    expected_sha256=expected_sha256,
                    hub_port=port,
                    on_progress=lambda phase: self._set_job_phase(job, phase),
                    cancelled=lambda: job.cancel_requested,
                    operation_dir=self._operation_dir,
                    job_id=job.job_id,
                    editor_is_busy=editor_is_busy,
                )
            job.state = "done"
            job.phase = "done"
            job.cancellable = False
            job.result = result
        except asyncio.CancelledError:
            job.state = "cancelled"
            job.phase = "cancelled"
            job.cancellable = False
            raise
        except AppInstallError as exc:
            if exc.code == "cancelled" or job.cancel_requested:
                job.state = "cancelled"
                job.phase = "cancelled"
            else:
                job.state = "failed"
                job.error_code = exc.code
                job.error = str(exc)
            job.cancellable = False
        except Exception as exc:  # noqa: BLE001
            logger.exception("Hub app update failed: app_id=%s", app_id)
            job.state = "failed"
            job.error_code = "app_register_failed"
            job.error = str(exc)
            job.cancellable = False

    async def uninstall_app(
        self,
        app_id: str,
        owner_id: str = "",
        remove_files: bool = False,
    ) -> dict[str, Any]:
        self._reject_active_job(app_id)
        async with self._operation_lock(app_id):
            self._reject_active_job(app_id)
            return await self._uninstall_app_locked(app_id, owner_id, remove_files)

    async def _uninstall_app_locked(self, app_id: str, owner_id: str, remove_files: bool) -> dict[str, Any]:
        from .hub_install import AppInstallError, uninstall_hub_app

        try:
            result = await uninstall_hub_app(
                manager=self,
                app_id=app_id,
                owner_id=owner_id,
                remove_files=remove_files,
            )
        except AppInstallError as exc:
            raise AppMarketError(exc.code, str(exc)) from exc
        self._consents.revoke_app(app_id)
        return result

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

    async def _stop_active_instances(self, app_id: str) -> None:
        for active in self._service.list_instances(app_id):
            if active.state not in ("starting", "running", "stopping"):
                continue
            if not await self._service.stop_app(active.instance_id):
                raise AppMarketError("app_operation_conflict", "Could not stop the active application")

    async def delete_app(
        self, app_id: str, owner_id: str = "", remove_files: bool = False
    ) -> dict[str, Any]:
        """Remove an app from the market, optionally deleting its program files.

        Both modes leave the app project and its editor sessions untouched.
        """
        from .hub_install import cleanup_local_app_files

        async with self._operation_lock(app_id):
            app = self._service.get_app(app_id)
            if not app or app.owner_id != owner_id:
                return {"deleted": False}
            self._reject_active_job(app_id)
            if app.source == "hub":
                result = await self._uninstall_app_locked(app_id, owner_id, remove_files)
                return {"deleted": True, **result}
            if not await self._service.delete_app(app_id) or self._service.get_app(app_id) is not None:
                return {"deleted": False}

            removed_dir = preserved_dir = cleanup_warning = ""
            if remove_files:
                async with self._local_work_dirs_lock:
                    cleanup = asyncio.create_task(asyncio.to_thread(cleanup_local_app_files, app, self.storage))
                    try:
                        removed_dir, preserved_dir, cleanup_warning = await asyncio.shield(cleanup)
                    except asyncio.CancelledError:
                        # Keep both locks until the filesystem work finishes.
                        await cleanup
                        raise
        return {
            "deleted": True,
            "removed_files": bool(removed_dir),
            "removed_dir": removed_dir,
            "preserved_dir": preserved_dir,
            "project_retained": True,
            "work_dir": app.work_dir,
            "cleanup_warning": cleanup_warning,
        }

    async def shutdown(self) -> None:
        tasks = []
        for job in list(self._jobs.values()):
            if job.task is not None and not job.task.done():
                if job.cancellable:
                    job.cancel_requested = True
                tasks.append(job.task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self._service.shutdown()
