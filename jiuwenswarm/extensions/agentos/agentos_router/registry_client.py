# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""Gateway-side Agent OS registry SDK.

Wraps the appliance registry HTTP API (httpx keep-alive):
- images: launch-spec / list
- instances: register / update / unregister (write-only for gateway)
- nodes: heartbeat

When ``RegistryConfig.endpoint`` is empty the client stays local-only so
AgentOS can boot without a live registry process.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import httpx

from jiuwenswarm.extensions.agentos.agentos_router.agent_manager import (
    BUILTIN_AGENT_TYPE,
)
from jiuwenswarm.extensions.agentos.agentos_router.models import AgentInfo, ImageInfo

logger = logging.getLogger(__name__)

KIND_THIRD_PARTY = "三方"
KIND_JIUWEN = "九问"
_JIUWEN_FRAMEWORKS = frozenset({"jiuwenswarm", "jiuwen-report"})
# Offline registry stub default list when no framework filter is given.
_LOCAL_STUB_FRAMEWORKS = frozenset({BUILTIN_AGENT_TYPE})


@dataclass(frozen=True)
class RegistryConfig:
    """Gateway → registry connection settings.

    ``endpoint`` empty → local stub (no HTTP). ``node`` is this machine's
    nodeIP used for ``POST /api/nodes/{node}/heartbeat``.
    """

    endpoint: str = ""
    request_timeout_s: float = 10.0
    node: str = ""


@dataclass(frozen=True)
class LaunchSpec:
    """Image launch-spec from registry ``GET .../launch-spec``.

    Top-level response fields used for agent create:
    ``runtime_spec`` (YuanRong RuntimeSpec) and ``env_vars``.
    """

    framework: str
    framework_version: str
    runtime_spec: dict[str, Any] = field(default_factory=dict)
    env_vars: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LaunchSpec:
        payload = dict(data or {})
        runtime_spec = payload.get("runtime_spec")
        env_vars = payload.get("env_vars")
        return cls(
            framework=str(payload.get("framework") or "").strip(),
            framework_version=str(payload.get("framework_version") or "").strip(),
            runtime_spec=dict(runtime_spec)
            if isinstance(runtime_spec, dict)
            else {},
            env_vars=dict(env_vars) if isinstance(env_vars, dict) else {},
            raw=payload,
        )


@dataclass(frozen=True)
class InstanceRecord:
    """Instance registration row returned by the registry."""

    service_id: str
    kind: str = ""
    framework: str = ""
    framework_version: str = ""
    node: str = ""
    address: str = ""
    user: str = ""
    status: str = ""
    dataset: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> InstanceRecord:
        payload = dict(data or {})
        return cls(
            service_id=str(payload.get("service_id") or "").strip(),
            kind=str(payload.get("kind") or "").strip(),
            framework=str(payload.get("framework") or "").strip(),
            framework_version=str(payload.get("framework_version") or "").strip(),
            node=str(payload.get("node") or "").strip(),
            address=str(payload.get("address") or "").strip(),
            user=str(payload.get("user") or "").strip(),
            status=str(payload.get("status") or "").strip(),
            dataset=str(payload.get("dataset") or "").strip(),
            raw=payload,
        )


@dataclass(frozen=True)
class HeartbeatResult:
    node: str
    state: str = ""
    ttl_seconds: int | None = None
    expires_at: float | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> HeartbeatResult:
        payload = dict(data or {})
        ttl = payload.get("ttl_seconds")
        expires = payload.get("expires_at")
        return cls(
            node=str(payload.get("node") or "").strip(),
            state=str(payload.get("state") or "").strip(),
            ttl_seconds=int(ttl) if ttl is not None else None,
            expires_at=float(expires) if expires is not None else None,
            raw=payload,
        )


@dataclass(frozen=True)
class ImageEntry:
    """One row from registry ``GET /api/images`` (flat: one framework version)."""

    framework: str
    framework_version: str = ""
    is_default: bool = False
    imageurl: str = ""
    workdir: str = ""
    mounts: list[dict[str, Any]] = field(default_factory=list)
    cpu: int | None = None
    memory: int | None = None
    ports: list[Any] = field(default_factory=list)
    env: dict[str, Any] = field(default_factory=dict)
    uploaded_by: str = ""
    image_module_version: str = ""
    created_at: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ImageEntry:
        payload = dict(data or {})
        mounts = payload.get("mounts")
        ports = payload.get("ports")
        env = payload.get("env")
        return cls(
            framework=str(payload.get("framework") or "").strip(),
            framework_version=str(payload.get("framework_version") or "").strip(),
            is_default=bool(payload.get("is_default")),
            imageurl=str(payload.get("imageurl") or "").strip(),
            workdir=str(payload.get("workdir") or "").strip(),
            mounts=list(mounts) if isinstance(mounts, list) else [],
            cpu=_optional_int(payload.get("cpu")),
            memory=_optional_int(payload.get("memory")),
            ports=list(ports) if isinstance(ports, list) else [],
            env=dict(env) if isinstance(env, dict) else {},
            uploaded_by=str(payload.get("uploaded_by") or "").strip(),
            image_module_version=str(payload.get("image_module_version") or "").strip(),
            created_at=str(payload.get("created_at") or "").strip(),
            raw=payload,
        )


class RegistryError(Exception):
    """Base error for registry client failures."""


class RegistryConnectionError(RegistryError):
    """Transport / connectivity failure talking to the registry."""


class RegistryHTTPError(RegistryError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        payload: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload


class RegistryNotFoundError(RegistryHTTPError):
    pass


class RegistryValidationError(RegistryHTTPError):
    pass


class RegistryConflictError(RegistryHTTPError):
    pass


def instance_service_id(user: str, framework: str) -> str:
    """Deterministic ``service_id`` for one instance per (user, framework)."""
    uid = str(user or "").strip()
    fw = str(framework or "").strip()
    digest = hashlib.sha256(f"{uid}\0{fw}".encode("utf-8")).hexdigest()[:8]
    return f"generic_{digest}"


def resolve_instance_kind(framework: str) -> str:
    """Map framework name to registry ``kind`` (三方 / 九问)."""
    name = str(framework or "").strip().lower()
    if name in _JIUWEN_FRAMEWORKS or name.startswith("jiuwen"):
        return KIND_JIUWEN
    return KIND_THIRD_PARTY


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _normalize_base_url(endpoint: str) -> str:
    text = str(endpoint or "").strip().rstrip("/")
    return f"{text}/" if text else ""


def _encode(segment: str) -> str:
    return quote(str(segment or ""), safe="")


def _parse_error_payload(resp: httpx.Response) -> dict[str, Any] | None:
    try:
        data = resp.json()
    except ValueError:
        return None
    return data if isinstance(data, dict) else {"detail": data}


def _wrap_http_error(resp: httpx.Response) -> RegistryHTTPError:
    payload = _parse_error_payload(resp)
    detail = ""
    if payload is not None:
        raw_detail = payload.get("detail")
        detail = raw_detail if isinstance(raw_detail, str) else str(raw_detail or "")
    status = resp.status_code
    message = f"HTTP {status}: {detail or resp.reason_phrase or 'request failed'}"
    if status == 404:
        return RegistryNotFoundError(message, status_code=status, payload=payload)
    if status == 409:
        return RegistryConflictError(message, status_code=status, payload=payload)
    if status in (400, 422):
        return RegistryValidationError(message, status_code=status, payload=payload)
    return RegistryHTTPError(message, status_code=status, payload=payload)


class RegistryClient:
    """httpx long-lived client for Agent OS registry (gateway write path)."""

    def __init__(self, config: RegistryConfig) -> None:
        self._config = config
        self._base_url = _normalize_base_url(config.endpoint)
        self._http: httpx.AsyncClient | None = None
        # Local fallback / agent_id → service_id bookkeeping.
        self._registered_agents: dict[str, AgentInfo] = {}
        self._agent_service_ids: dict[str, str] = {}

    @property
    def enabled(self) -> bool:
        return bool(self._base_url)

    @property
    def endpoint(self) -> str:
        return self._base_url.rstrip("/")

    @property
    def node(self) -> str:
        return str(self._config.node or "").strip()

    async def get_launch_spec(
        self,
        framework: str,
        *,
        version: str | None = None,
    ) -> LaunchSpec:
        """``GET /api/images/{framework}/launch-spec`` (optional ``?version=``)."""
        fw = str(framework or "").strip()
        if not fw:
            raise RegistryValidationError(
                "framework is required", status_code=400, payload=None
            )
        if not self.enabled:
            return LaunchSpec(
                framework=fw,
                framework_version=str(version or "default").strip() or "default",
                raw={"source": "local_stub"},
            )
        params: dict[str, Any] = {}
        if version is not None and str(version).strip():
            params["version"] = str(version).strip()
        data = await self._request_json(
            "GET",
            f"api/images/{_encode(fw)}/launch-spec",
            params=params or None,
        )
        return LaunchSpec.from_dict(data)

    async def list_images(
        self,
        *,
        framework: str | None = None,
        uploaded_by: str | None = None,
    ) -> list[ImageEntry]:
        """``GET /api/images`` — flat list, one entry = one framework version."""
        if not self.enabled:
            names = (
                [str(framework).strip()]
                if framework and str(framework).strip()
                else sorted(_LOCAL_STUB_FRAMEWORKS)
            )
            return [
                ImageEntry(
                    framework=name,
                    framework_version="default",
                    is_default=True,
                    imageurl=f"local/stub/{name}:latest",
                    raw={"source": "local_stub"},
                )
                for name in names
                if name
            ]
        params: dict[str, Any] = {}
        if framework is not None and str(framework).strip():
            params["framework"] = str(framework).strip()
        if uploaded_by is not None and str(uploaded_by).strip():
            params["uploaded_by"] = str(uploaded_by).strip()
        data = await self._request_json(
            "GET",
            "api/images",
            params=params or None,
            expect_list=True,
        )
        items = data if isinstance(data, list) else []
        return [
            ImageEntry.from_dict(item)
            for item in items
            if isinstance(item, dict)
        ]

    async def register_instance(
        self,
        *,
        service_id: str,
        kind: str,
        framework: str,
        framework_version: str,
        node: str,
        address: str,
        user: str,
    ) -> InstanceRecord:
        """``POST /api/instances`` — idempotent upsert by ``service_id``."""
        body = {
            "service_id": str(service_id or "").strip(),
            "kind": str(kind or "").strip(),
            "framework": str(framework or "").strip(),
            "framework_version": str(framework_version or "").strip(),
            "node": str(node or "").strip(),
            "address": str(address or "").strip(),
            "user": str(user or "").strip(),
        }
        if not body["service_id"] or not body["framework"] or not body["user"]:
            raise RegistryValidationError(
                "service_id, framework and user are required",
                status_code=400,
                payload=body,
            )
        if not self.enabled:
            return InstanceRecord.from_dict({**body, "status": "运行", "dataset": "default"})
        data = await self._request_json("POST", "api/instances", json=body)
        return InstanceRecord.from_dict(data)

    async def update_instance(
        self,
        service_id: str,
        *,
        node: str | None = None,
        address: str | None = None,
    ) -> InstanceRecord:
        """``PATCH /api/instances/{service_id}`` — update placement fields."""
        sid = str(service_id or "").strip()
        if not sid:
            raise RegistryValidationError(
                "service_id is required", status_code=400, payload=None
            )
        body: dict[str, Any] = {}
        if node is not None:
            body["node"] = str(node).strip()
        if address is not None:
            body["address"] = str(address).strip()
        if not body:
            raise RegistryValidationError(
                "at least one of node/address is required",
                status_code=400,
                payload=None,
            )
        if not self.enabled:
            return InstanceRecord.from_dict(
                {"service_id": sid, **body, "status": "运行"}
            )
        data = await self._request_json(
            "PATCH",
            f"api/instances/{_encode(sid)}",
            json=body,
        )
        return InstanceRecord.from_dict(data)

    async def unregister_instance(self, service_id: str) -> dict[str, Any]:
        """``DELETE /api/instances/{service_id}`` (idempotent)."""
        sid = str(service_id or "").strip()
        if not sid:
            raise RegistryValidationError(
                "service_id is required", status_code=400, payload=None
            )
        if not self.enabled:
            return {"service_id": sid, "dataset": "default", "deleted": True}
        data = await self._request_json("DELETE", f"api/instances/{_encode(sid)}")
        return data if isinstance(data, dict) else {"service_id": sid, "deleted": True}

    async def report_node_heartbeat(
        self,
        node: str | None = None,
        *,
        status: Any = None,
    ) -> HeartbeatResult:
        """``POST /api/nodes/{node}/heartbeat`` — covers all instances on node."""
        node_ip = str(node if node is not None else self.node or "").strip()
        if not node_ip:
            raise RegistryValidationError(
                "node is required for heartbeat",
                status_code=400,
                payload=None,
            )
        body: dict[str, Any] | None = None
        if status is not None:
            body = {"status": status}
        if not self.enabled:
            return HeartbeatResult(
                node=node_ip,
                state="healthy",
                ttl_seconds=90,
                raw={"source": "local_stub"},
            )
        data = await self._request_json(
            "POST",
            f"api/nodes/{_encode(node_ip)}/heartbeat",
            json=body,
        )
        return HeartbeatResult.from_dict(data)

    # ── Compatibility helpers used by AgentOSRouterClient ─────────────────

    async def get_image_info(self, image_name: str) -> ImageInfo:
        """Resolve framework launch-spec into ``ImageInfo`` for sandbox create."""
        framework = str(image_name or "").strip()
        if not self.enabled:
            imageurl = f"local/stub/{framework}:latest"
            runtime_spec = {
                "runtime": "python3.11",
                "sandbox_type": "docker",
                "rootfs": {
                    "imageurl": imageurl,
                    "user": "agentos",
                    "ports": ["tcp:22"],
                },
                "cpu": 1000,
                "memory": 2048,
            }
            return ImageInfo(
                image_name=framework,
                image_uri=imageurl,
                metadata={
                    "source": "local_stub",
                    "agent_type": framework,
                    "runtime_spec": runtime_spec,
                    "env_vars": {},
                },
            )
        try:
            spec = await self.get_launch_spec(framework)
        except RegistryError:
            logger.exception(
                "[RegistryClient] get_launch_spec failed: framework=%s", framework
            )
            raise
        runtime_spec = dict(spec.runtime_spec) if isinstance(spec.runtime_spec, dict) else {}
        rootfs = runtime_spec.get("rootfs") if isinstance(runtime_spec.get("rootfs"), dict) else {}
        image_uri = (
            str(rootfs.get("imageurl") or rootfs.get("image_url") or "").strip() or None
        )
        return ImageInfo(
            image_name=framework,
            image_uri=image_uri,
            metadata={
                "agent_type": framework,
                "framework": spec.framework or framework,
                "framework_version": spec.framework_version,
                "launch_spec": dict(spec.raw),
                "runtime_spec": runtime_spec,
                "env_vars": dict(spec.env_vars),
                "source": "registry",
            },
        )

    async def list_user_images(self, user_id: str) -> list[ImageInfo]:
        """List switchable frameworks; one ``ImageInfo`` per framework.

        ``GET /api/images`` is flat (one row per version). For UI listing we
        keep a single entry per framework, preferring ``is_default=true``.
        """
        uid = str(user_id or "").strip()
        entries = await self.list_images()
        by_framework: dict[str, ImageEntry] = {}
        for entry in entries:
            framework = str(entry.framework or "").strip()
            if not framework:
                continue
            existing = by_framework.get(framework)
            if existing is None or (entry.is_default and not existing.is_default):
                by_framework[framework] = entry

        images: list[ImageInfo] = []
        for framework, entry in by_framework.items():
            imageurl = str(entry.imageurl or "").strip() or None
            images.append(
                ImageInfo(
                    image_name=framework,
                    image_uri=imageurl,
                    metadata={
                        "agent_type": framework,
                        "user_id": uid,
                        "framework": framework,
                        "framework_version": entry.framework_version,
                        "is_default": entry.is_default,
                        "imageurl": entry.imageurl,
                        "uploaded_by": entry.uploaded_by,
                        "source": "registry" if self.enabled else "local_stub",
                    },
                )
            )
        return images

    async def register_agent(self, agent_info: AgentInfo) -> None:
        """Map ``AgentInfo`` → ``POST /api/instances`` (or local bookkeeping)."""
        info = agent_info.copy()
        user = str(info.user_id or "").strip()
        framework = str(info.agent_type or "").strip()
        service_id = instance_service_id(user, framework)
        image_meta = info.metadata.get("image_info")
        if not isinstance(image_meta, dict):
            image_meta = {}
        sandbox_meta = info.metadata.get("sandbox")
        if not isinstance(sandbox_meta, dict):
            sandbox_meta = {}

        framework_version = str(
            image_meta.get("framework_version")
            or info.metadata.get("framework_version")
            or "default"
        ).strip()
        node = str(
            info.metadata.get("node")
            or sandbox_meta.get("node")
            or self.node
            or ""
        ).strip()
        address = str(
            info.metadata.get("address")
            or sandbox_meta.get("address")
            or info.sandbox_id
            or ""
        ).strip()
        kind = str(info.metadata.get("kind") or resolve_instance_kind(framework)).strip()

        if self.enabled:
            record = await self.register_instance(
                service_id=service_id,
                kind=kind,
                framework=framework,
                framework_version=framework_version,
                node=node,
                address=address,
                user=user,
            )
            info.metadata["service_id"] = record.service_id
            info.metadata["registry_status"] = record.status
        else:
            info.metadata["service_id"] = service_id

        self._registered_agents[info.agent_id] = info
        self._agent_service_ids[info.agent_id] = service_id

    async def unregister_agent(
        self,
        agent_id: str,
        *,
        user_id: str | None = None,
        agent_type: str | None = None,
    ) -> None:
        """Unregister via ``DELETE /api/instances/{service_id}``.

        Resolves ``service_id`` from the local ``agent_id`` map, then
        ``(user_id, agent_type)``, so callers can unregister even when the
        async register task has not finished writing the map yet.
        """
        key = str(agent_id or "").strip()
        cached = self._registered_agents.pop(key, None) if key else None
        mapped = self._agent_service_ids.pop(key, None) if key else None
        if user_id is not None:
            uid = str(user_id).strip()
        elif cached is not None:
            uid = str(cached.user_id or "").strip()
        else:
            uid = ""
        if agent_type is not None:
            framework = str(agent_type).strip()
        elif cached is not None:
            framework = str(cached.agent_type or "").strip()
        else:
            framework = ""
        service_id = (
            mapped
            or (instance_service_id(uid, framework) if uid and framework else "")
            or key
        )
        if not service_id:
            return
        if self.enabled:
            await self.unregister_instance(service_id)

    async def report_heartbeat(self, agent_id: str) -> None:
        """Compatibility shim: node-level heartbeat (``agent_id`` ignored)."""
        del agent_id
        if not self.node and not self.enabled:
            return
        if not self.node:
            logger.warning(
                "[RegistryClient] report_heartbeat skipped: registry.node is empty"
            )
            return
        await self.report_node_heartbeat(self.node)

    async def close(self) -> None:
        self._registered_agents.clear()
        self._agent_service_ids.clear()
        client = self._http
        self._http = None
        if client is not None:
            await client.aclose()

    async def __aenter__(self) -> RegistryClient:
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        await self.close()

    # ── HTTP transport ────────────────────────────────────────────────────

    async def _get_http(self) -> httpx.AsyncClient:
        if self._http is None:
            timeout = httpx.Timeout(self._config.request_timeout_s)
            # Keep-alive for heartbeat cadence; limits leave connection open.
            self._http = httpx.AsyncClient(
                base_url=self._base_url,
                timeout=timeout,
                headers={"Accept": "application/json"},
                limits=httpx.Limits(
                    max_keepalive_connections=4,
                    max_connections=8,
                    keepalive_expiry=max(30.0, float(self._config.request_timeout_s) * 3),
                ),
            )
        return self._http

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        expect_list: bool = False,
    ) -> Any:
        client = await self._get_http()
        try:
            resp = await client.request(method, path, json=json, params=params)
        except httpx.HTTPError as exc:
            raise RegistryConnectionError(f"{type(exc).__name__}: {exc}") from exc
        if resp.status_code >= 400:
            raise _wrap_http_error(resp)
        if resp.status_code == 204 or not resp.content:
            return [] if expect_list else {}
        try:
            data = resp.json()
        except ValueError as exc:
            raise RegistryHTTPError(
                f"invalid JSON response for {method} {path}",
                status_code=resp.status_code,
                payload=None,
            ) from exc
        if expect_list:
            if isinstance(data, list):
                return data
            raise RegistryHTTPError(
                f"expected JSON list for {method} {path}",
                status_code=resp.status_code,
                payload=data if isinstance(data, dict) else None,
            )
        if isinstance(data, dict):
            return data
        return {"detail": data}
