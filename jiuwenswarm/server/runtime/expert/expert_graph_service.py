# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Runtime service for ExpertGraph discovery and team materialization RPCs.

The graph domain module is intentionally stateless.  This service owns the
process-local snapshot lifecycle so the WebSocket layer only needs to forward
``method`` and ``params`` and send the returned :class:`ExpertGraphOpResult`.
Only server-mined candidates can be materialized; callers cannot inject an
arbitrary member list or package path.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from jiuwenswarm.common.utils import get_agent_experts_dir
from jiuwenswarm.server.runtime.expert import expert_store as _expert_store
from jiuwenswarm.server.runtime.expert.expert_graph import (
    ExpertGraphSnapshot,
    ExpertInventorySnapshot,
    build_expert_graph,
    collect_expert_inventory,
    mine_expert_teams,
    normalize_expert_manifest,
)
from jiuwenswarm.server.runtime.expert.expert_store import ExpertPackageSource
from jiuwenswarm.server.runtime.expert.team_materializer import (
    TeamMaterializationError,
    materialize_team_candidate,
)

logger = logging.getLogger(__name__)

_INVENTORY_REFRESH = "experts.inventory.refresh"
_GRAPH_BUILD = "experts.graph.build"
_GRAPH_GET = "experts.graph.get"
_TEAMS_MINE = "experts.teams.mine"
_TEAMS_MATERIALIZE = "experts.teams.materialize"


@dataclass(frozen=True)
class ExpertGraphOpResult:
    """One ExpertGraph RPC result ready for ``AgentResponse``."""

    ok: bool
    payload: dict[str, Any] = field(default_factory=dict)


class _ServiceError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ExpertGraphService:
    """Own inventory/graph/candidate snapshots for the five ExpertGraph RPCs."""

    def __init__(
        self,
        *,
        source_factory: Callable[[], ExpertPackageSource] | None = None,
        destination_root: Path | None = None,
    ) -> None:
        # Resolve the production source lazily.  Besides avoiding network work
        # at AgentServer startup, this keeps expert_store.reset_expert_source()
        # and tests that replace get_expert_source() effective.
        self._source_factory = source_factory
        self._destination_root = destination_root
        self._inventory: ExpertInventorySnapshot | None = None
        self._graph: ExpertGraphSnapshot | None = None
        self._candidates: dict[str, dict[str, Any]] = {}
        self._candidate_graph_id = ""
        self._mine_min_members = 2
        self._mine_max_members = 4
        self._mine_limit = 20
        self._lock = asyncio.Lock()

    async def execute(self, method: str, params: Any = None) -> ExpertGraphOpResult:
        """Dispatch one protocol method and map expected failures to stable codes."""

        method_value = str(getattr(method, "value", method) or "").strip()
        try:
            clean_params = self._require_params(params)
            if method_value == _INVENTORY_REFRESH:
                return await self.refresh_inventory(clean_params)
            if method_value == _GRAPH_BUILD:
                return await self.build_graph(clean_params)
            if method_value == _GRAPH_GET:
                return await self.get_graph(clean_params)
            if method_value == _TEAMS_MINE:
                return await self.mine_teams(clean_params)
            if method_value == _TEAMS_MATERIALIZE:
                return await self.materialize_team(clean_params)
            raise _ServiceError(
                "UNKNOWN_METHOD", f"unsupported expert graph method: {method_value}"
            )
        except _ServiceError as exc:
            return self._failure(exc.code, str(exc))
        except _expert_store.ExpertNotFound as exc:
            return self._failure("NOT_FOUND", str(exc))
        except _expert_store.ExpertRepoUnavailable as exc:
            return self._failure("REPO_UNAVAILABLE", str(exc))
        except _expert_store.InvalidExpertPackage as exc:
            return self._failure("INVALID_PACKAGE", str(exc))
        except TeamMaterializationError as exc:
            message = str(exc)
            if "nested expert teams" in message:
                code = "NESTED_TEAM_UNSUPPORTED"
            elif "already exists" in message:
                code = "ALREADY_EXISTS"
            else:
                code = "MATERIALIZE_FAILED"
            return self._failure(code, message)
        except Exception as exc:  # noqa: BLE001 - RPC boundary must always answer
            logger.exception("[ExpertGraphService] %s failed: %s", method_value, exc)
            return self._failure("INTERNAL_ERROR", str(exc))

    async def refresh_inventory(
        self, params: Mapping[str, Any] | None = None
    ) -> ExpertGraphOpResult:
        params = params or {}
        resolve_packages = self._bool_param(
            params, "resolve_packages", "resolvePackages", default=True
        )
        async with self._lock:
            inventory = await self._collect_inventory(resolve_packages=resolve_packages)
            self._replace_inventory(inventory)
            return self._success(inventory=inventory.to_dict())

    async def build_graph(
        self, params: Mapping[str, Any] | None = None
    ) -> ExpertGraphOpResult:
        params = params or {}
        force = self._bool_param(params, "force", default=False)
        resolve_packages = self._bool_param(
            params, "resolve_packages", "resolvePackages", default=True
        )
        async with self._lock:
            if self._graph is not None and not force:
                return self._success(graph=self._graph.to_dict())
            if self._inventory is None or force:
                inventory = await self._collect_inventory(
                    resolve_packages=resolve_packages
                )
                self._replace_inventory(inventory)
            assert self._inventory is not None
            self._graph = build_expert_graph(self._inventory)
            self._clear_candidates()
            return self._success(graph=self._graph.to_dict())

    async def get_graph(
        self, params: Mapping[str, Any] | None = None
    ) -> ExpertGraphOpResult:
        # ``graph: null`` is an intentional cache-miss response.  The frontend
        # uses it to fall through to experts.graph.build on a fresh process.
        del params
        async with self._lock:
            graph = self._graph.to_dict() if self._graph is not None else None
            return self._success(graph=graph)

    async def mine_teams(
        self, params: Mapping[str, Any] | None = None
    ) -> ExpertGraphOpResult:
        params = params or {}
        graph_id = self._text_param(params, "graph_id", "graphId")
        min_members = self._int_param(
            params, "min_members", "minMembers", default=2, minimum=2, maximum=4
        )
        max_members = self._int_param(
            params, "max_members", "maxMembers", default=4, minimum=2, maximum=4
        )
        limit = self._int_param(params, "limit", default=20, minimum=0, maximum=100)
        if max_members < min_members:
            raise _ServiceError(
                "BAD_REQUEST",
                "max_members must be greater than or equal to min_members",
            )

        async with self._lock:
            if self._graph is None:
                raise _ServiceError(
                    "GRAPH_NOT_BUILT", "expert graph has not been built"
                )
            if graph_id and graph_id != self._graph.graph_id:
                raise _ServiceError(
                    "GRAPH_STALE",
                    f"requested graph is stale: {graph_id}",
                )
            mined = mine_expert_teams(
                self._graph,
                min_members=min_members,
                max_members=max_members,
                limit=limit,
            )
            self._mine_min_members = min_members
            self._mine_max_members = max_members
            self._mine_limit = limit
            self._cache_mined_candidates(mined, graph=self._graph)
            return self._success(**mined)

    async def materialize_team(
        self, params: Mapping[str, Any] | None = None
    ) -> ExpertGraphOpResult:
        params = params or {}
        candidate_id = self._text_param(params, "candidate_id", "candidateId")
        graph_id = self._text_param(params, "graph_id", "graphId")
        if not candidate_id:
            raise _ServiceError("BAD_REQUEST", "missing candidate_id")

        async with self._lock:
            graph = self._graph
            if graph is None:
                raise _ServiceError(
                    "GRAPH_NOT_BUILT", "expert graph has not been built"
                )
            if graph_id and graph_id != graph.graph_id:
                raise _ServiceError(
                    "GRAPH_STALE", f"requested graph is stale: {graph_id}"
                )
            if self._candidate_graph_id != graph.graph_id:
                raise _ServiceError(
                    "CANDIDATES_NOT_MINED",
                    "team candidates have not been mined for the current graph",
                )
            candidate = self._candidates.get(candidate_id)
            if candidate is None:
                raise _ServiceError(
                    "CANDIDATE_NOT_FOUND",
                    f"team candidate does not exist: {candidate_id}",
                )

            node_by_id = {node.id: node for node in graph.nodes}
            member_ids = candidate.get("memberIds") or []
            if not isinstance(member_ids, list) or not member_ids:
                raise _ServiceError(
                    "INVALID_CANDIDATE", "candidate has no valid members"
                )
            for member_id in member_ids:
                node = node_by_id.get(str(member_id))
                if node is None:
                    raise _ServiceError(
                        "INVALID_CANDIDATE",
                        f"candidate member is absent from the current graph: {member_id}",
                    )
                if node.type == "team":
                    raise _ServiceError(
                        "NESTED_TEAM_UNSUPPORTED",
                        f"nested expert teams are not supported: {member_id}",
                    )

            source = self._get_source()
            expert_packages: dict[str, Path] = {}
            for member_id in member_ids:
                clean_id = str(member_id)
                node = node_by_id[clean_id]
                package_dir = await source.fetch(clean_id)
                if self._is_team_package(package_dir):
                    # The source may have changed after mining; trust the current
                    # package manifest rather than the stale graph descriptor.
                    raise _ServiceError(
                        "NESTED_TEAM_UNSUPPORTED",
                        f"nested expert teams are not supported: {clean_id}",
                    )
                try:
                    actual = normalize_expert_manifest(package_dir)
                except (
                    _expert_store.InvalidExpertPackage,
                    OSError,
                    ValueError,
                ) as exc:
                    raise _ServiceError(
                        "GRAPH_STALE",
                        f"candidate member changed after graph build: {clean_id}: {exc}",
                    ) from exc
                changed_fields = [
                    field_name
                    for field_name, expected, current in (
                        ("id", node.id, actual.id),
                        ("type", node.type, actual.type),
                        ("status", node.status, actual.status),
                        ("reusable", node.reusable, actual.reusable),
                        ("content_hash", node.content_hash, actual.content_hash),
                    )
                    if expected != current
                ]
                if changed_fields:
                    raise _ServiceError(
                        "GRAPH_STALE",
                        "candidate member changed after graph build: "
                        f"{clean_id} ({', '.join(changed_fields)})",
                    )
                expert_packages[clean_id] = package_dir

            destination_root = self._destination_root or get_agent_experts_dir()
            materialized_candidate = dict(candidate)
            materialized_candidate["graphId"] = graph.graph_id
            destination = await asyncio.to_thread(
                materialize_team_candidate,
                materialized_candidate,
                expert_packages=expert_packages,
                destination_root=destination_root,
            )

            # The package is committed atomically by the renderer.  Invalidate
            # the old graph immediately, then rescan the real source.  If the
            # local source switch is disabled the package remains safely on disk
            # and the response explicitly reports that it is not yet discoverable.
            self._inventory = None
            self._graph = None
            self._clear_candidates()
            warnings: list[str] = []
            refreshed: ExpertInventorySnapshot | None = None
            refreshed_graph: ExpertGraphSnapshot | None = None
            refreshed_candidates: list[dict[str, Any]] = []
            source_refreshed = False
            try:
                refreshed = await self._collect_inventory(resolve_packages=True)
                source_refreshed = any(
                    expert.id == candidate_id for expert in refreshed.experts
                )
                self._inventory = refreshed
                refreshed_graph = build_expert_graph(refreshed)
                self._graph = refreshed_graph
                remined = mine_expert_teams(
                    refreshed_graph,
                    min_members=self._mine_min_members,
                    max_members=self._mine_max_members,
                    limit=self._mine_limit,
                )
                self._cache_mined_candidates(remined, graph=refreshed_graph)
                refreshed_candidates = list(remined.get("candidates") or [])
                if not source_refreshed:
                    warnings.append(
                        "专家团已生成，但当前 expert source 尚未发现本地包；"
                        "请确认 JIUWEN_EXPERT_LOCAL_DIRS=1 后刷新"
                    )
            except Exception as exc:  # noqa: BLE001 - mutation already committed
                logger.warning(
                    "[ExpertGraphService] materialized %s but source refresh failed: %s",
                    candidate_id,
                    exc,
                )
                warnings.append(f"专家团已生成，但 expert source 刷新失败: {exc}")

            materialized = {
                "id": candidate_id,
                "name": str(candidate.get("name") or candidate_id),
                "type": "team",
                "source": "local",
                "available": source_refreshed,
            }
            return self._success(
                expert=materialized,
                inventory=refreshed.to_dict() if refreshed is not None else None,
                graph=(
                    refreshed_graph.to_dict()
                    if refreshed_graph is not None
                    else None
                ),
                candidates=refreshed_candidates,
                source_refreshed=source_refreshed,
                warnings=warnings,
                # Expose only the package directory name, never an absolute host path.
                package_name=destination.name,
            )

    async def _collect_inventory(
        self, *, resolve_packages: bool
    ) -> ExpertInventorySnapshot:
        return await collect_expert_inventory(
            self._get_source(), resolve_packages=resolve_packages
        )

    def _get_source(self) -> ExpertPackageSource:
        if self._source_factory is not None:
            return self._source_factory()
        return _expert_store.get_expert_source()

    def _replace_inventory(self, inventory: ExpertInventorySnapshot) -> None:
        self._inventory = inventory
        self._graph = None
        self._clear_candidates()

    def _clear_candidates(self) -> None:
        self._candidates = {}
        self._candidate_graph_id = ""

    def _cache_mined_candidates(
        self,
        mined: Mapping[str, Any],
        *,
        graph: ExpertGraphSnapshot,
    ) -> None:
        candidates = mined.get("candidates") or []
        self._candidates = {
            str(candidate.get("id")): dict(candidate)
            for candidate in candidates
            if isinstance(candidate, Mapping) and candidate.get("id")
        }
        self._candidate_graph_id = graph.graph_id

    @staticmethod
    def _is_team_package(package_dir: Path) -> bool:
        try:
            manifest = json.loads(
                (Path(package_dir) / "manifest.json").read_text(encoding="utf-8")
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return False
        return isinstance(manifest, Mapping) and (
            manifest.get("package_type") == "agent_group"
            or manifest.get("packageType") == "agent_group"
        )

    @staticmethod
    def _require_params(params: Any) -> Mapping[str, Any]:
        if params is None:
            return {}
        if not isinstance(params, Mapping):
            raise _ServiceError("BAD_REQUEST", "params must be an object")
        return params

    @staticmethod
    def _text_param(params: Mapping[str, Any], *names: str) -> str:
        for name in names:
            value = params.get(name)
            if value is not None:
                return str(value).strip()
        return ""

    @staticmethod
    def _bool_param(params: Mapping[str, Any], *names: str, default: bool) -> bool:
        for name in names:
            if name not in params:
                continue
            value = params[name]
            if isinstance(value, bool):
                return value
            raise _ServiceError("BAD_REQUEST", f"{name} must be a boolean")
        return default

    @staticmethod
    def _int_param(
        params: Mapping[str, Any],
        *names: str,
        default: int,
        minimum: int,
        maximum: int,
    ) -> int:
        for name in names:
            if name not in params:
                continue
            value = params[name]
            if isinstance(value, bool) or not isinstance(value, int):
                raise _ServiceError("BAD_REQUEST", f"{name} must be an integer")
            if not minimum <= value <= maximum:
                raise _ServiceError(
                    "BAD_REQUEST",
                    f"{name} must be between {minimum} and {maximum}",
                )
            return value
        return default

    @staticmethod
    def _success(**payload: Any) -> ExpertGraphOpResult:
        return ExpertGraphOpResult(ok=True, payload={"success": True, **payload})

    @staticmethod
    def _failure(code: str, message: str) -> ExpertGraphOpResult:
        return ExpertGraphOpResult(
            ok=False,
            payload={"success": False, "error": message, "code": code},
        )


__all__ = ["ExpertGraphOpResult", "ExpertGraphService"]
