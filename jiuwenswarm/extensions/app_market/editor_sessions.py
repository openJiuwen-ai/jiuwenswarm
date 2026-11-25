"""Create a fresh editing conversation in the app-owned project on every edit."""
from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable

from jiuwenswarm.common.schema.message import ReqMethod

from .app_market.models import AppManifest
from .app_market.storage import MarketStorage

CoreRequest = Callable[[ReqMethod, dict[str, Any]], Awaitable[tuple[bool, dict[str, Any]]]]
CoreCall = Callable[[ReqMethod, dict[str, Any]], Awaitable[dict[str, Any]]]

_GITIGNORE_LINES = (
    "app.log",
    "*.log",
    "__pycache__/",
    "node_modules/",
    ".venv/",
    "*.tmp",
)


class EditorSessionError(Exception):
    def __init__(self, payload: dict[str, Any]):
        super().__init__(payload.get("error", "Cannot open application editor"))
        self.code = payload.get("code", "EDIT_SESSION_FAILED")


def _normalized_dir(value: str | None) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        return os.path.normcase(str(Path(raw).expanduser().resolve()))
    except OSError:
        return os.path.normcase(os.path.abspath(raw))


def _project_info(value: dict[str, Any]) -> dict[str, Any]:
    nested = value.get("project")
    return nested if isinstance(nested, dict) else value


def _safe_project_name(manifest: AppManifest) -> str:
    name = (manifest.name or "").strip() or Path(manifest.work_dir).name or manifest.app_id
    cleaned = "".join(ch for ch in name if ch not in '\\/:*?"<>|').strip()
    return cleaned or manifest.app_id


def _write_gitignore(project_dir: str) -> None:
    target = Path(project_dir) / ".gitignore"
    if target.exists():
        return
    try:
        target.write_text("\n".join(_GITIGNORE_LINES) + "\n", encoding="utf-8")
    except OSError:
        pass


class AppEditorSessions:
    def __init__(self, storage: MarketStorage):
        self._storage = storage
        self._locks: dict[str, asyncio.Lock] = {}

    async def open(self, app_id: str, owner_id: str, request: CoreRequest, options: dict[str, Any]) -> dict[str, Any]:
        """Create a fresh editing conversation in the app-owned project.

        Each edit operation starts one conversation. Keep previous bindings so
        the preview action can be restored from any editor session.
        """
        async def call(method: ReqMethod, params: dict[str, Any]) -> dict[str, Any]:
            ok, payload = await request(method, params)
            if not ok:
                raise EditorSessionError(payload)
            return payload

        async with self._locks.setdefault(app_id, asyncio.Lock()):
            manifest = self._storage.load_manifest(app_id)
            if manifest is None or manifest.owner_id != owner_id:
                raise EditorSessionError({"error": "Application not found", "code": "NOT_FOUND"})

            create_options = dict(options)
            edit_token = create_options.pop("edit_token", "")
            if edit_token != "":
                try:
                    operation_id = uuid.UUID(edit_token).hex
                except (ValueError, AttributeError, TypeError) as exc:
                    raise EditorSessionError({"error": "Invalid edit token", "code": "BAD_REQUEST"}) from exc
                create_token = uuid.uuid5(uuid.NAMESPACE_URL, f"{app_id}/{operation_id}").hex
            elif manifest.editor_create_pending and manifest.editor_create_token:
                create_token = manifest.editor_create_token
            else:
                create_token = uuid.uuid4().hex

            project = await self.resolve_project(manifest, call)
            project_id = str(project.get("project_id") or "")
            project_dir = str(project.get("project_dir") or "")
            if not project_dir:
                project_dir = str(Path(manifest.work_dir).expanduser().resolve())
                project["project_dir"] = project_dir
            project["project_id"] = project_id
            project_work_mode = str(project.get("work_mode") or "")
            latest = self._storage.load_manifest(app_id)
            if latest is None or latest.owner_id != owner_id or latest.work_dir != manifest.work_dir:
                raise EditorSessionError({"error": "Application changed while opening editor", "code": "CONFLICT"})
            manifest = latest
            manifest.project_id = project_id
            manifest.project_dir = project_dir
            manifest.project_work_mode = project_work_mode
            manifest.editor_create_token = create_token
            manifest.editor_create_pending = True
            self._storage.save_manifest(app_id, manifest)

            git_available, git_initialized = await self._ensure_git(
                project, call, app_dir=manifest.work_dir
            )

            created = await call(ReqMethod.SESSION_CREATE, {
                **create_options,
                "create_token": create_token,
                "mode": f"agent.{project['work_mode']}.normal",
                "is_swarm": False,
                "persist_session": False,
                "project_id": project["project_id"],
                "work_mode": project["work_mode"],
            })
            session_id = created["session_id"]
            session = await call(ReqMethod.SESSION_GET_METADATA, {"session_id": session_id})
            # Publishing can update metadata while creation is awaiting Core.
            current = self._storage.load_manifest(app_id)
            conflict = {"error": "Application changed while opening editor", "code": "CONFLICT"}
            if current is None:
                raise EditorSessionError(conflict)
            if (current.owner_id, current.work_dir, current.editor_create_token) != (
                owner_id,
                manifest.work_dir,
                create_token,
            ):
                raise EditorSessionError(conflict)
            if current.editor_session_id and current.editor_session_id not in current.editor_session_ids:
                current.editor_session_ids.append(current.editor_session_id)
            if session_id not in current.editor_session_ids:
                current.editor_session_ids.append(session_id)
                current.editor_session_id = session_id
            current.editor_create_pending = False
            self._storage.save_manifest(app_id, current)
            result = self._payload(current, {**session, "session_id": session_id}, created=True)
            result.update(
                {
                    "git_available": git_available,
                    "git_initialized": git_initialized,
                }
            )
            return result

    @staticmethod
    def _payload(manifest: AppManifest, session: dict[str, Any], *, created: bool) -> dict[str, Any]:
        return {
            "session": session,
            "created": created,
            "project_id": manifest.project_id,
            "project_dir": manifest.project_dir,
            "git_available": None,
            "git_initialized": None,
        }

    async def resolve_project(
        self,
        manifest: AppManifest,
        call: CoreCall,
    ) -> dict[str, Any]:
        """Resolve existing binding → source-session project → app directory project."""
        if manifest.project_id:
            try:
                bound = await call(ReqMethod.PROJECT_INFO, {
                    "project_id": manifest.project_id,
                    "include_hidden": True,
                    "include_session_stats": False,
                })
            except EditorSessionError as exc:
                if exc.code != "NOT_FOUND":
                    raise
            else:
                return await self._restore_if_hidden(_project_info(bound), call)

        projects_all = await call(
            ReqMethod.PROJECT_LIST,
            {"filter": "all", "include_hidden": True},
        )

        def by_id(project_id: str) -> dict[str, Any] | None:
            for item in projects_all.get("projects", []):
                if str(item.get("project_id") or "") == project_id:
                    return item
            return None

        if manifest.source_session and not manifest.project_id:
            try:
                source = await call(
                    ReqMethod.SESSION_GET_METADATA,
                    {"session_id": manifest.source_session},
                )
                project = by_id(str(source.get("project_id") or ""))
                if project is not None:
                    project = await self._restore_if_hidden(project, call)
                    if not project.get("hidden"):
                        return project
            except EditorSessionError:
                pass

        target = _normalized_dir(manifest.work_dir)
        for item in projects_all.get("projects", []):
            if _normalized_dir(item.get("project_dir")) == target:
                project = await self._restore_if_hidden(item, call)
                if not project.get("hidden"):
                    return project

        return await self._create_project(manifest, call, projects_all.get("projects", []))

    @staticmethod
    async def _restore_if_hidden(project: dict[str, Any], call: CoreCall) -> dict[str, Any]:
        if not project.get("hidden"):
            return project
        restored = await call(
            ReqMethod.PROJECT_RESTORE,
            {"project_id": project.get("project_id")},
        )
        return {**project, **_project_info(restored), "hidden": False}

    async def _create_project(
        self,
        manifest: AppManifest,
        call: CoreCall,
        projects: list[dict[str, Any]],
    ) -> dict[str, Any]:
        base_name = _safe_project_name(manifest)
        app_suffix = manifest.app_id[-8:]
        work_dir = str(Path(manifest.work_dir).expanduser().resolve())
        target = os.path.normcase(work_dir)
        occupied = {project.get("name") for project in projects}
        name = base_name
        attempt = 1
        while True:
            while name in occupied:
                suffix = f"-{app_suffix}" if attempt == 1 else f"-{app_suffix}-{attempt}"
                name = f"{base_name[:200 - len(suffix)]}{suffix}"
                attempt += 1
            try:
                created = await call(
                    ReqMethod.PROJECT_CREATE,
                    {
                        "name": name,
                        "project_dir": work_dir,
                        "work_mode": "code",
                    },
                )
            except EditorSessionError as exc:
                if exc.code != "CONFLICT":
                    raise
                latest = await call(ReqMethod.PROJECT_LIST, {"filter": "all", "include_hidden": True})
                projects = latest.get("projects", [])
                for project in projects:
                    if _normalized_dir(project.get("project_dir")) == target:
                        return await self._restore_if_hidden(project, call)
                occupied.update(project.get("name") for project in projects)
                if name not in occupied:
                    raise
            else:
                return _project_info(created)

    async def _ensure_git(
        self,
        project: dict[str, Any],
        call: CoreCall,
        *,
        app_dir: str = "",
    ) -> tuple[bool, bool]:
        """Return (git_available, git_initialized); never blocks editing."""
        project_id = str(project.get("project_id") or "")
        project_dir = str(project.get("project_dir") or "")
        if not project_id:
            return False, False
        try:
            status = await call(ReqMethod.PROJECT_GIT_STATUS, {"project_id": project_id})
        except EditorSessionError as exc:
            # A project that is not a repository yet is exactly the case that
            # should be initialized; only other failures mean Git is unusable.
            if exc.code != "NOT_GIT_REPOSITORY":
                return False, False
            status = {}
        except Exception:  # noqa: BLE001 - Git never blocks editing
            return False, False
        repo = status.get("repo") if isinstance(status.get("repo"), dict) else {}
        if repo.get("is_git"):
            return True, True
        # Only app-owned projects get the generated ignore file. A local app
        # may reuse a user source project; that project must not be modified.
        if app_dir and _normalized_dir(project_dir) == _normalized_dir(app_dir):
            _write_gitignore(project_dir)
        try:
            await call(
                ReqMethod.PROJECT_GIT_INIT,
                {"project_id": project_id, "initial_branch": "main"},
            )
        except Exception:  # noqa: BLE001 - Git never blocks editing
            return False, False
        return True, True
