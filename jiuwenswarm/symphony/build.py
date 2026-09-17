"""Symphony graph build APIs."""

from __future__ import annotations

import json
import hashlib
import logging
import os
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from openjiuwen.symphony import (
    CapabilityFingerprint,
    FINGERPRINT_ARTIFACT_FILENAME,
    FingerprintArtifact,
    FingerprintService,
    ScanResult,
    SkillFolderScanner,
    SymphonyRuntime,
)

from jiuwenswarm.symphony.config import (
    SymphonyConfig,
    default_symphony_config,
)
from jiuwenswarm.symphony.adapter import (
    FingerprintLLMAdapter,
    ScanResultCapabilityProvider,
    fingerprint_settings_from_swarm,
    graph_build_orchestration_config_from_swarm,
    graph_config_from_swarm,
    llm_config_signature,
    model_from_config,
    model_response_observer_from_config,
)
from jiuwenswarm.symphony.llm import (
    LLMConfig,
    get_llm_token_usage_summary,
    reset_llm_token_usage,
)
from jiuwenswarm.symphony.graph_state import (
    GRAPH_STATE_FILENAME,
    GraphStateBuilder,
    load_graph_state,
    write_graph_state,
)
from jiuwenswarm.symphony.graph_storage import (
    build_run_dir,
    latest_incomplete_build,
    graph_exists,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GraphStatus:
    success: bool
    graph_dir: str
    exists: bool
    stale: bool
    skill_count: int
    changed_count: int
    added_count: int
    removed_count: int
    resume_available: bool = False
    checkpoint_dir: str = ""
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "graph_dir": self.graph_dir,
            "exists": self.exists,
            "stale": self.stale,
            "skill_count": self.skill_count,
            "changed_count": self.changed_count,
            "added_count": self.added_count,
            "removed_count": self.removed_count,
            "resume_available": self.resume_available,
            "checkpoint_dir": self.checkpoint_dir,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class GraphBuildResult:
    success: bool
    graph_dir: str
    skill_count: int
    reused_count: int
    extracted_count: int
    removed_count: int
    edge_count: int
    diagnostics_count: int
    relation_reused_count: int = 0
    relation_resolved_count: int = 0
    version: str = ""
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "graph_dir": self.graph_dir,
            "skill_count": self.skill_count,
            "reused_count": self.reused_count,
            "extracted_count": self.extracted_count,
            "removed_count": self.removed_count,
            "edge_count": self.edge_count,
            "diagnostics_count": self.diagnostics_count,
            "relation_reused_count": self.relation_reused_count,
            "relation_resolved_count": self.relation_resolved_count,
            "version": self.version,
            "detail": self.detail,
        }


class GraphBuildRuntimeFactory:
    """Create core fingerprint components behind a testable app boundary."""

    @staticmethod
    def scan(
        skills_root: str | Path,
        *,
        max_depth: int | None,
    ) -> ScanResult:
        return SkillFolderScanner(
            skills_root,
            max_depth=max_depth,
        ).scan()

    @staticmethod
    def fingerprint_service(
        scan_result: ScanResult,
        artifact_root: Path,
        *,
        llm_config: LLMConfig | None,
        runtime_config: SymphonyConfig,
    ) -> FingerprintService:
        return FingerprintService(
            ScanResultCapabilityProvider(scan_result),
            artifact_root,
            llm=(FingerprintLLMAdapter(llm_config) if llm_config is not None else None),
            settings=fingerprint_settings_from_swarm(
                runtime_config,
                llm_config,
            ),
        )


class SymphonyGraphBuilder:
    """Build and refresh the offline Symphony graph."""

    def __init__(
        self,
        *,
        runtime_factory: GraphBuildRuntimeFactory | None = None,
        state_builder: GraphStateBuilder | None = None,
    ) -> None:
        self.runtime_factory = runtime_factory or GraphBuildRuntimeFactory()
        self.state_builder = state_builder or GraphStateBuilder()

    def status(
        self,
        skills_root: str | Path,
        graph_dir: str | Path,
        *,
        llm_config: LLMConfig | None = None,
        symphony_config: SymphonyConfig | None = None,
    ) -> GraphStatus:
        runtime_config = symphony_config or default_symphony_config()
        output_dir = Path(graph_dir).resolve()
        scan_result = self.runtime_factory.scan(
            skills_root,
            max_depth=runtime_config.fingerprint.scan.max_depth,
        )
        current_hashes = self.state_builder.capability_hashes(scan_result.capabilities)
        state = load_graph_state(output_dir)
        active_entries = state.active_entries()
        exists = graph_exists(output_dir)
        added = [path for path in current_hashes if path not in active_entries]
        changed = [
            path
            for path, digest in current_hashes.items()
            if path in active_entries and active_entries[path].content_hash != digest
        ]
        removed = [path for path in active_entries if path not in current_hashes]
        stale = (not exists) or bool(added or changed or removed)
        if exists and not stale:
            try:
                artifact = (
                    SymphonyRuntime(
                        graph_artifact_root=output_dir,
                        capability_provider=(),
                        model=None,
                    )
                    .orchestration.read()
                    .to_dict()
                )
                capabilities = [
                    CapabilityFingerprint.model_validate(item)
                    for item in artifact.get("capabilities") or []
                ]
                source_snapshot = artifact.get("source_snapshot")
                expected_snapshot = _graph_status_source_snapshot(
                    capabilities=capabilities,
                    current_hashes=current_hashes,
                    runtime_config=runtime_config,
                    llm_config=llm_config,
                    artifact_source_snapshot=(
                        source_snapshot if isinstance(source_snapshot, dict) else {}
                    ),
                )
                identity_runtime = SymphonyRuntime(
                    graph_artifact_root=output_dir,
                    capability_provider=capabilities,
                    model=None,
                    orchestration_config=graph_build_orchestration_config_from_swarm(
                        runtime_config
                    ),
                    source_snapshot=expected_snapshot,
                    graph_config=graph_config_from_swarm(runtime_config),
                )
                stale = not identity_runtime.orchestration.status().fresh
            except (FileNotFoundError, ValueError):
                stale = True
        resume_from = latest_incomplete_build(output_dir)
        detail = "graph is fresh"
        if not exists:
            detail = "Symphony graph is missing"
        elif stale:
            detail = "Symphony graph is stale"

        return GraphStatus(
            success=True,
            graph_dir=str(output_dir),
            exists=exists,
            stale=stale,
            skill_count=len(scan_result.capabilities),
            changed_count=len(changed),
            added_count=len(added),
            removed_count=len(removed),
            resume_available=resume_from is not None,
            checkpoint_dir=str(resume_from) if resume_from is not None else "",
            detail=detail,
        )

    async def build(
        self,
        skills_root: str | Path,
        graph_dir: str | Path,
        llm_config: LLMConfig | None = None,
        *,
        force: bool = False,
        build_log: Callable[..., None] | None = None,
        symphony_config: SymphonyConfig | None = None,
        resume: bool = True,
    ) -> GraphBuildResult:
        runtime_config = symphony_config or default_symphony_config()
        output_dir = Path(graph_dir).resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        reset_llm_token_usage()
        run_id = _new_run_id()
        checkpoint = _BuildCheckpoint(build_run_dir(output_dir, run_id))
        artifact_dir = build_run_dir(output_dir, run_id) / "artifacts"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        resume_from = latest_incomplete_build(output_dir) if resume else None
        checkpoint.record(
            "update.start",
            status="running",
            skills_root=str(skills_root),
            graph_dir=str(output_dir),
            artifact_dir=str(artifact_dir),
            force=force,
            resume_from=str(resume_from) if resume_from is not None else "",
        )
        if resume_from is not None:
            _record_build_log(
                build_log,
                "resume.detected",
                checkpoint_dir=str(resume_from),
            )

        _record_build_log(build_log, "scan.start", skills_root=str(skills_root))
        scan_result = self.runtime_factory.scan(
            skills_root,
            max_depth=runtime_config.fingerprint.scan.max_depth,
        )
        current_hashes = self.state_builder.capability_hashes(scan_result.capabilities)
        old_state = load_graph_state(output_dir)
        active_entries = old_state.active_entries()
        removed_paths = set(active_entries) - set(current_hashes)
        settings = fingerprint_settings_from_swarm(runtime_config, llm_config)
        fingerprint_signature = settings.signature()
        reused_count, extracted_count = _logical_fingerprint_counts(
            output_dir=output_dir,
            current_hashes=current_hashes,
            active_entries=active_entries,
            fingerprint_signature=fingerprint_signature,
            force=force,
        )
        _record_build_log(
            build_log,
            "scan.done",
            capability_count=len(scan_result.capabilities),
            diagnostics_count=len(scan_result.diagnostics),
        )
        _record_build_log(
            build_log,
            "diff.done",
            reused_count=reused_count,
            extracted_count=extracted_count,
            removed_count=len(removed_paths),
        )
        checkpoint.record("fingerprint.start", status="running")
        _record_build_log(
            build_log,
            "fingerprint.extract.start",
            capability_count=len(scan_result.capabilities),
        )
        fingerprint_service = self.runtime_factory.fingerprint_service(
            scan_result,
            output_dir,
            llm_config=llm_config,
            runtime_config=runtime_config,
        )

        def record_fingerprint_progress(event: dict[str, Any]) -> None:
            if event.get("event") != "fingerprint.extract.progress":
                return
            _record_build_log(
                build_log,
                "fingerprint.extract.start",
                current=event.get("current"),
                total=event.get("total"),
            )

        fingerprint_artifact = await fingerprint_service.build(
            force=force,
            progress_callback=record_fingerprint_progress,
        )
        fingerprint_failure_count = sum(
            len(item.failures) for item in fingerprint_artifact.fingerprints
        )
        checkpoint.record(
            "fingerprint.done",
            status="running",
            reused_count=reused_count,
            extracted_count=extracted_count,
            failure_count=fingerprint_failure_count,
        )
        _record_build_log(
            build_log,
            "fingerprint.done",
            reused_count=reused_count,
            extracted_count=extracted_count,
            failure_count=fingerprint_failure_count,
        )
        _record_build_log(
            build_log,
            "artifact.fingerprints.write.start",
            artifact=FINGERPRINT_ARTIFACT_FILENAME,
        )
        _write_json_artifact(
            fingerprint_artifact.model_dump(mode="json"),
            artifact_dir / FINGERPRINT_ARTIFACT_FILENAME,
        )
        _record_build_log(
            build_log,
            "artifact.fingerprints.write.done",
            artifact=FINGERPRINT_ARTIFACT_FILENAME,
            fingerprint_count=len(fingerprint_artifact.fingerprints),
        )

        _record_build_log(
            build_log,
            "graph.build.start",
            fingerprint_count=len(fingerprint_artifact.fingerprints),
            workers=runtime_config.build.workers,
        )
        capabilities = list(fingerprint_artifact.fingerprints)
        source_snapshot = _graph_source_snapshot(
            capabilities=capabilities,
            current_hashes=current_hashes,
            fingerprint_artifact=fingerprint_artifact,
            fingerprint_signature=fingerprint_signature,
            runtime_config=runtime_config,
            llm_config=llm_config,
        )
        fingerprints_by_id = {
            item.capability_id: item for item in fingerprint_artifact.fingerprints
        }
        new_state = self.state_builder.next_state(
            capabilities=scan_result.capabilities,
            current_hashes=current_hashes,
            fingerprints_by_id=fingerprints_by_id,
            old_state=old_state,
            removed_paths=removed_paths,
        )
        prepared_graph: dict[str, Any] = {}
        graph_resolution: dict[str, Any] = {}

        def prepare_artifact(version_dir: Path) -> None:
            """Complete the version before agent-core switches ``current.json``."""

            graph_payload = json.loads(
                (version_dir / "graph.json").read_text(encoding="utf-8")
            )
            prepared_graph.update(graph_payload)
            edge_count = len(graph_payload.get("edges") or [])
            relation_reused_count, relation_resolved_count = _relation_cache_counts(
                graph_payload, force=force
            )
            graph_diagnostics = list(graph_payload.get("diagnostics") or [])
            candidate_count = _nonnegative_int(
                graph_resolution.get("candidate_count"),
                fallback=0,
            )
            match_count = _nonnegative_int(
                graph_resolution.get("match_count"),
                fallback=edge_count,
            )
            accepted_match_count = _nonnegative_int(
                graph_resolution.get("accepted_match_count"),
                fallback=edge_count,
            )
            checkpoint.record(
                "graph.done",
                status="running",
                edge_count=edge_count,
                relation_reused_count=relation_reused_count,
                relation_resolved_count=relation_resolved_count,
            )
            _record_build_log(
                build_log,
                "graph.build.done",
                candidate_count=candidate_count,
                match_count=match_count,
                accepted_match_count=accepted_match_count,
                edge_count=edge_count,
                diagnostics_count=len(graph_diagnostics),
                relation_reused_count=relation_reused_count,
                relation_resolved_count=relation_resolved_count,
            )
            checkpoint.record("publish.start", status="running")
            _record_build_log(build_log, "artifact.graph.write.start")
            _copy_fingerprint_artifact(artifact_dir, version_dir)
            _write_json_artifact(
                get_llm_token_usage_summary(),
                version_dir / "llm_token_usage.json",
            )
            _record_build_log(build_log, "artifact.graph.write.done")
            _record_build_log(
                build_log,
                "state.write.start",
                path=str(version_dir / GRAPH_STATE_FILENAME),
            )
            write_graph_state(new_state, version_dir)
            _record_build_log(build_log, "state.write.done")

        runtime = SymphonyRuntime(
            graph_artifact_root=output_dir,
            capability_provider=capabilities,
            model=(model_from_config(llm_config) if llm_config is not None else None),
            model_response_observer=(
                model_response_observer_from_config(llm_config)
                if llm_config is not None
                else None
            ),
            orchestration_config=graph_build_orchestration_config_from_swarm(
                runtime_config
            ),
            source_snapshot=source_snapshot,
            graph_config=graph_config_from_swarm(runtime_config),
            prepare_artifact=prepare_artifact,
        )
        checkpoint.record("graph.start", status="running")
        graph_build = await runtime.orchestration.build(
            force=force,
            progress=_public_progress_adapter(
                build_log,
                graph_resolution=graph_resolution,
            ),
        )
        graph_payload = prepared_graph or runtime.orchestration.read().to_dict()
        edge_count = len(graph_payload.get("edges") or [])
        graph_diagnostics = list(graph_payload.get("diagnostics") or [])
        relation_reused_count, relation_resolved_count = _relation_cache_counts(
            graph_payload,
            force=force,
        )
        published_dir = graph_build.graph_path.parent
        checkpoint.record(
            "publish.done",
            status="success",
            version=graph_build.version,
            published_dir=str(published_dir),
        )
        _cleanup_published_build_artifacts(artifact_dir)

        return GraphBuildResult(
            success=True,
            graph_dir=str(output_dir),
            skill_count=len(scan_result.capabilities),
            reused_count=reused_count,
            extracted_count=extracted_count,
            removed_count=len(removed_paths),
            edge_count=edge_count,
            diagnostics_count=(
                len(scan_result.diagnostics)
                + fingerprint_failure_count
                + len(graph_diagnostics)
            ),
            relation_reused_count=relation_reused_count,
            relation_resolved_count=relation_resolved_count,
            version=graph_build.version,
        )


def graph_status(
    skills_root: str | Path,
    graph_dir: str | Path,
    *,
    llm_config: LLMConfig | None = None,
    symphony_config: SymphonyConfig | None = None,
    runtime_factory: GraphBuildRuntimeFactory | None = None,
) -> GraphStatus:
    """Report whether a graph exists and differs from the Skill folders."""

    return SymphonyGraphBuilder(runtime_factory=runtime_factory).status(
        skills_root,
        graph_dir,
        llm_config=llm_config,
        symphony_config=symphony_config,
    )


async def build_graph(
    skills_root: str | Path,
    graph_dir: str | Path,
    llm_config: LLMConfig | None = None,
    *,
    workers: int = 1,
    force: bool = False,
    build_log: Callable[..., None] | None = None,
    symphony_config: SymphonyConfig | None = None,
    runtime_factory: GraphBuildRuntimeFactory | None = None,
    resume: bool = True,
) -> GraphBuildResult:
    """Build or refresh the offline Symphony graph."""

    del workers
    return await SymphonyGraphBuilder(
        runtime_factory=runtime_factory,
    ).build(
        skills_root,
        graph_dir,
        llm_config,
        force=force,
        build_log=build_log,
        symphony_config=symphony_config,
        resume=resume,
    )


def _public_progress_adapter(
    build_log: Callable[..., None] | None,
    *,
    graph_resolution: dict[str, Any] | None = None,
) -> Callable[[dict[str, Any]], None] | None:
    if build_log is None and graph_resolution is None:
        return None

    def record(event: dict[str, Any]) -> None:
        stage = str(event.get("event") or "graph.progress")
        if stage in {
            "build_started",
            "build_published",
            "build_failed",
            "build_cancelled",
        }:
            return
        details = {key: value for key, value in event.items() if key != "event"}
        if stage == "graph.resolve.done" and graph_resolution is not None:
            graph_resolution.clear()
            graph_resolution.update(details)
        _record_build_log(build_log, stage, **details)

    return record


def _record_build_log(
    build_log: Callable[..., None] | None, stage: str, **details: Any
) -> None:
    if build_log is not None:
        build_log(stage, **details)


def _write_json_artifact(payload: dict[str, Any], path: Path) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _copy_fingerprint_artifact(source_dir: Path, target_dir: Path) -> None:
    """Copy the core-owned canonical artifact into an unpublished graph version."""

    source = source_dir / FINGERPRINT_ARTIFACT_FILENAME
    if not source.is_file():
        raise FileNotFoundError(f"Missing core fingerprint artifact: {source}")
    shutil.copy2(source, target_dir / FINGERPRINT_ARTIFACT_FILENAME)


def _cleanup_published_build_artifacts(artifact_dir: Path) -> None:
    """Best-effort removal of the duplicate pre-publish artifact snapshot."""

    try:
        shutil.rmtree(artifact_dir)
    except FileNotFoundError:
        return
    except OSError:
        logger.warning(
            "Failed to clean published Symphony build artifacts: %s",
            artifact_dir,
            exc_info=True,
        )


class _BuildCheckpoint:
    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir
        self.path = run_dir / "checkpoint.json"

    def record(self, stage: str, *, status: str, **details: Any) -> None:
        now = datetime.now(timezone.utc).isoformat()
        payload = {
            "schema_version": "Symphony-build-checkpoint-v1",
            "run_id": self.run_dir.name,
            "stage": stage,
            "status": status,
            "updated_at": now,
            **details,
        }
        if self.path.is_file():
            try:
                previous = json.loads(self.path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                previous = {}
            payload.setdefault("started_at", previous.get("started_at") or now)
        else:
            payload["started_at"] = now
        self.run_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        os.replace(tmp, self.path)


def _new_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    return f"{stamp}-{uuid.uuid4().hex[:12]}"


def _graph_source_snapshot(
    *,
    capabilities: list[Any],
    current_hashes: dict[str, str],
    fingerprint_artifact: FingerprintArtifact,
    fingerprint_signature: str,
    runtime_config: SymphonyConfig,
    llm_config: LLMConfig | None,
) -> dict[str, Any]:
    return {
        "schema_version": "JiuwenSwarm-symphony-graph-source-v2",
        "capabilities_sha256": _capabilities_signature(capabilities),
        "current_hashes": dict(sorted(current_hashes.items())),
        "fingerprint_schema_version": fingerprint_artifact.schema_version,
        "fingerprint_source_snapshot": fingerprint_artifact.source_snapshot.model_dump(
            mode="json",
            exclude={"captured_at"},
        ),
        "fingerprint_sha256": fingerprint_signature,
        "fingerprint_config_sha256": _fingerprint_config_signature(runtime_config),
        "graph_config": graph_config_from_swarm(runtime_config),
        "llm_sha256": (
            llm_config_signature(llm_config) if llm_config is not None else ""
        ),
    }


def _graph_status_source_snapshot(
    *,
    capabilities: list[Any],
    current_hashes: dict[str, str],
    runtime_config: SymphonyConfig,
    llm_config: LLMConfig | None,
    artifact_source_snapshot: dict[str, Any],
) -> dict[str, Any]:
    fingerprint_sha256 = artifact_source_snapshot.get("fingerprint_sha256", "")
    llm_sha256 = artifact_source_snapshot.get("llm_sha256", "")
    if llm_config is not None:
        fingerprint_sha256 = fingerprint_settings_from_swarm(
            runtime_config,
            llm_config,
        ).signature()
        llm_sha256 = llm_config_signature(llm_config)
    return {
        "schema_version": "JiuwenSwarm-symphony-graph-source-v2",
        "capabilities_sha256": _capabilities_signature(capabilities),
        "current_hashes": dict(sorted(current_hashes.items())),
        "fingerprint_schema_version": artifact_source_snapshot.get(
            "fingerprint_schema_version",
            "",
        ),
        "fingerprint_source_snapshot": artifact_source_snapshot.get(
            "fingerprint_source_snapshot",
            {},
        ),
        "fingerprint_sha256": fingerprint_sha256,
        "fingerprint_config_sha256": _fingerprint_config_signature(runtime_config),
        "graph_config": graph_config_from_swarm(runtime_config),
        "llm_sha256": llm_sha256,
    }


def _capabilities_signature(capabilities: list[Any]) -> str:
    payloads = [
        item.to_dict() if hasattr(item, "to_dict") else dict(item)
        for item in capabilities
    ]
    payloads.sort(
        key=lambda item: str(item.get("capability_id") or item.get("id") or "")
    )
    return _stable_hash(payloads)


def _fingerprint_config_signature(runtime_config: SymphonyConfig) -> str:
    return fingerprint_settings_from_swarm(runtime_config, None).signature()


def _logical_fingerprint_counts(
    *,
    output_dir: Path,
    current_hashes: dict[str, str],
    active_entries: dict[str, Any],
    fingerprint_signature: str,
    force: bool,
) -> tuple[int, int]:
    """Report source-level reuse without inspecting core's private cache."""

    reuse_allowed = (
        not force
        and _published_fingerprint_signature(output_dir) == fingerprint_signature
    )
    reused_count = 0
    if reuse_allowed:
        for relative_path, content_hash in current_hashes.items():
            if relative_path not in active_entries:
                continue
            if active_entries[relative_path].content_hash == content_hash:
                reused_count += 1
    return reused_count, len(current_hashes) - reused_count


def _published_fingerprint_signature(output_dir: Path) -> str:
    if not graph_exists(output_dir):
        return ""
    try:
        artifact = (
            SymphonyRuntime(
                graph_artifact_root=output_dir,
                capability_provider=(),
                model=None,
            )
            .orchestration.read()
            .to_dict()
        )
    except (FileNotFoundError, ValueError):
        return ""
    source_snapshot = artifact.get("source_snapshot")
    if not isinstance(source_snapshot, dict):
        return ""
    return str(source_snapshot.get("fingerprint_sha256") or "")


def _relation_cache_counts(
    graph_payload: dict[str, Any],
    *,
    force: bool,
) -> tuple[int, int]:
    edge_count = len(graph_payload.get("edges") or [])
    config = graph_payload.get("config")
    llm = config.get("llm") if isinstance(config, dict) else None
    relation_cache = llm.get("relation_cache") if isinstance(llm, dict) else None
    if not isinstance(relation_cache, dict):
        return 0, edge_count
    reused_count = _nonnegative_int(
        relation_cache.get("reused_count"),
        fallback=0,
    )
    resolved_count = _nonnegative_int(
        relation_cache.get("resolved_count"),
        fallback=edge_count,
    )
    return (0 if force else reused_count), resolved_count


def _nonnegative_int(value: Any, *, fallback: int) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return fallback


def _stable_hash(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode(
            "utf-8"
        )
    ).hexdigest()
