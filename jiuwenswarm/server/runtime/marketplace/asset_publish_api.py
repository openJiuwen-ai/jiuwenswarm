# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Web publishing boundary: server-resolved resources and credential-bound history."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
import re
from typing import Callable
from urllib.parse import urlsplit

import yaml

from .asset_package_builder import PackageBuildError
from .asset_publish_adapters import PublishValidationError
from .asset_publish_models import PublishIdentity
from .asset_publish_service import AssetPublishService
from .asset_publish_store import PublishStore, PublishStoreError
from .hub_client import HubClient
from .hub_publish_client import PublishAuth


class PublishAPIError(ValueError):
    def __init__(self, code: str, field: str = ""):
        self.code, self.field = code, field
        super().__init__(code)


def _safe_id(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise PublishAPIError("RESOURCE_NOT_FOUND")
    if (
        len(value) > 256
        or value in {".", ".."}
        or any(c in value for c in "/\\\0")
    ):
        raise PublishAPIError("RESOURCE_NOT_FOUND")
    return value


def resolve_local_asset(kind: str, local_id: str):
    """Only installed roots of this AgentServer; no request path or workspace override."""
    from jiuwenswarm.common.utils import get_agent_skills_dir, get_workspace_dir

    local_id = _safe_id(local_id)
    if kind == "skill":
        root = get_agent_skills_dir()
        candidate = root / local_id
        if (
            candidate.is_symlink()
            or not candidate.is_dir()
            or not (candidate / "SKILL.md").is_file()
        ):
            raise PublishAPIError("RESOURCE_NOT_FOUND")
        return candidate
    if kind in {"agent_template", "plugin"}:
        from jiuwenswarm.server.runtime import extension_package_manager as packages

        runtime_id = packages.resolve_equipment_runtime_id(
            "agent_templates" if kind == "agent_template" else "plugin_packages",
            local_id,
        )
        resolver = (
            packages.resolve_agent_template_dir
            if kind == "agent_template"
            else packages.resolve_plugin_dir
        )
        try:
            return resolver(runtime_id)
        except (ValueError, OSError):
            raise PublishAPIError("RESOURCE_NOT_FOUND") from None
    if kind == "mcp":
        from jiuwenswarm.server.runtime.mcp import registry
        from jiuwenswarm.server.runtime.mcp.state_store import get_mcp_record
        from .hub_install_state import HubInstallStateStore
        from .asset_mcp_publish_converter import CustomMcpSource

        record = HubInstallStateStore(get_workspace_dir() / "mcp").get(local_id)
        package_id = record.package_id if record and record.kind == "mcp" else local_id
        package = registry.resolve_package(package_id)
        if package is not None:
            return package.root
        custom = get_mcp_record(package_id)
        if isinstance(custom, dict):
            return CustomMcpSource(config=dict(custom))
        raise PublishAPIError("RESOURCE_NOT_FOUND")
    raise PublishAPIError("INVALID_KIND")


def _defaults(kind: str, local_id: str, source) -> dict:
    if isinstance(source, Path):
        if source.is_symlink():
            raise PublishAPIError("UNSAFE_PATH")
        path = source / ("SKILL.md" if kind == "skill" else "manifest.json")
        if not path.is_file() or path.is_symlink() or path.stat().st_size > 1024 * 1024:
            raise PublishAPIError("INVALID_PACKAGE")
        text = path.read_text(encoding="utf-8")
        if kind == "skill":
            match = re.match(r"\s*---\s*\n(.*?)\n---(?:\s*\n|$)", text, re.S)
            data = yaml.safe_load(match.group(1)) if match else {}
        else:
            data = json.loads(text)
        if not isinstance(data, dict):
            raise PublishAPIError("INVALID_PACKAGE")
    else:
        # Never return a custom connection record or its credential-bearing fields.
        data = {"name": local_id, "id": local_id, "description": "", "version": "1.0.0"}

    def label(value):
        if isinstance(value, dict):
            value = value.get("zh") or value.get("en") or ""
        return value if isinstance(value, str) else ""

    name = label(data.get("id" if kind in {"plugin", "mcp"} else "name")) or local_id
    version = label(data.get("version")) or "1.0.0"
    if not re.fullmatch(r"(?:\d+\.\d+\.\d+|[0-9a-f]{7})", version):
        version = "1.0.0"
    tags = (
        data.get("tags") or (data.get("metadata") or {}).get("tags", [])
        if isinstance(data.get("metadata", {}), dict)
        else []
    )
    return dict(
        asset_name=name,
        version=version,
        display_name=label(data.get("display_name")) or name,
        description=label(data.get("description"))
        or label(data.get("display_description")),
        tags=[t for t in tags if isinstance(t, str)] if isinstance(tags, list) else [],
        visibility="public",
    )


class AssetPublishAPI:
    def __init__(
        self,
        state_root: Path,
        *,
        publisher=None,
        resolver: Callable = resolve_local_asset,
        hub_url: str | None = None,
    ):
        from .hub_client import HttpHubTransport

        self.root = Path(state_root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.root.chmod(0o700)
        secret = self.root / "scope.key"
        try:
            fd = os.open(secret, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            try:
                stream = os.fdopen(fd, "wb")
            except BaseException:
                os.close(fd)
                raise
            with stream:
                stream.write(os.urandom(32))
        if secret.is_symlink():
            raise PublishAPIError("INVALID_STATE")
        self._secret = secret.read_bytes()
        if len(self._secret) != 32:
            raise PublishAPIError("INVALID_STATE")
        # Same configured destination as the publisher; the browser cannot override it.
        self.hub_url = HttpHubTransport(base_url=hub_url).base_url
        destination = urlsplit(self.hub_url)
        if any((destination.username, destination.password,
                destination.query, destination.fragment)):
            raise PublishAPIError("INVALID_HUB_CONFIGURATION")
        self.store = PublishStore(self.root / "publish.sqlite")
        self._resolver = resolver
        from .hub_catalog_cache import invalidate_hub_catalog

        self.service = AssetPublishService(
            self.store,
            publisher or HubClient(base_url=self.hub_url),
            lambda scope, kind, local_id: resolver(kind, local_id),
            self.root / "packages",
            on_published=lambda request, result: invalidate_hub_catalog(
                request.identity.kind
            ),
        )

    def scope(self, gateway_user: str, auth_data: dict) -> str:
        auth = self._auth(auth_data)
        value = json.dumps(
            [
                str(self.root.resolve()),
                self.hub_url,
                gateway_user,
                auth.oauth_provider,
                auth.access_token,
            ],
            ensure_ascii=False,
        ).encode()
        return hmac.new(self._secret, value, hashlib.sha256).hexdigest()

    @staticmethod
    def _auth(value: object) -> PublishAuth:
        if not isinstance(value, dict):
            raise PublishAPIError("AUTH_REQUIRED")
        if (
            not isinstance(value.get("access_token"), str)
            or not value["access_token"]
            or len(value["access_token"]) > 8192
        ):
            raise PublishAPIError("AUTH_REQUIRED")
        if value.get("oauth_provider") not in {"gitcode", "github"}:
            raise PublishAPIError("AUTH_REQUIRED")
        try:
            return PublishAuth(value["access_token"], value["oauth_provider"])
        except ValueError:
            raise PublishAPIError("AUTH_REQUIRED") from None

    async def start(self):
        await self.service.start()

    async def close(self):
        await self.service.close()
        self.store.close()

    async def call(self, method: str, params: dict, *, gateway_user: str) -> dict:
        forbidden_fields = (
            "path", "scope", "workspace", "hub_url", "market_url", "publisher_id", "user_id"
        )
        if not isinstance(params, dict) or any(k in params for k in forbidden_fields):
            raise PublishAPIError("INVALID_PARAMETERS")
        if method == "local_status":
            kind, local_id = params.get("kind"), _safe_id(params.get("local_id"))
            if kind not in {"skill", "agent_template", "plugin", "mcp"}:
                raise PublishAPIError("INVALID_KIND")
            # Workspace access remains enforced by the Web/Gateway route and local
            # resolver. This read-only summary never grants Hub account access.
            self._resolver(kind, local_id)
            return {"state": self.store.local_status(kind, local_id)}
        auth = self._auth(params.get("auth"))
        scope = self.scope(str(gateway_user or ""), params["auth"])
        try:
            if method in {"describe", "prepare", "records"}:
                kind, local_id = params.get("kind"), _safe_id(params.get("local_id"))
                if kind not in {"skill", "agent_template", "plugin", "mcp"}:
                    raise PublishAPIError("INVALID_KIND")
                if method == "records":
                    return {
                        "records": self.service.records(scope, kind, local_id),
                        "identity_verified": False,
                    }
                source = self._resolver(kind, local_id)
                if method == "describe":
                    return dict(
                        kind=kind,
                        local_id=local_id,
                        defaults=_defaults(kind, local_id, source),
                        can_publish=True,
                        hub_url=self.hub_url,
                        errors=[],
                        records=self.service.records(scope, kind, local_id),
                        identity_verified=False,
                    )
                metadata = params.get("metadata")
                if not isinstance(metadata, dict):
                    raise PublishAPIError("INVALID_METADATA")
                if set(metadata) - {
                    "asset_name",
                    "version",
                    "display_name",
                    "description",
                    "tags",
                    "version_desc",
                    "visibility",
                    "dependency_sources",
                }:
                    raise PublishAPIError("INVALID_METADATA")
                identity = PublishIdentity(
                    kind,
                    metadata.get("asset_name", ""),
                    metadata.get("version", ""),
                    params.get("target_asset_id") or None,
                )
                prepared = await self.service.prepare(
                    scope,
                    local_id,
                    identity,
                    {
                        **{
                            k: v
                            for k, v in metadata.items()
                            if k not in {"asset_name", "version"}
                        },
                        "force": params.get("force", False),
                    },
                )
                return {
                    **prepared,
                    "artifact_sha256": prepared["checksum_sha256"],
                    "can_submit": True,
                    "warnings": [],
                    "errors": [],
                }
            if method == "commit":
                return await self.service.commit(
                    scope, params.get("draft_id"), params.get("request_id"), auth=auth
                )
            if method == "status":
                return self.service.status(scope, params.get("operation_id"))
            raise PublishAPIError("UNKNOWN_METHOD")
        except PublishAPIError:
            raise
        except (PublishStoreError, PackageBuildError) as exc:
            raise PublishAPIError(exc.code) from None
        except PublishValidationError as exc:
            raise PublishAPIError(exc.code, exc.field) from None
        except (ValueError, TypeError, OSError, KeyError, yaml.YAMLError):
            raise PublishAPIError("INVALID_PACKAGE") from None
