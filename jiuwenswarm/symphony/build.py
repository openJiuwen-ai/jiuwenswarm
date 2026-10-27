"""Symphony graph build APIs."""

from __future__ import annotations

import json
import hashlib
import logging
import os
import shutil
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from openjiuwen.symphony import SymphonyRuntime

from jiuwenswarm.symphony.config import (
    SymphonyConfig,
    default_symphony_config,
)
from jiuwenswarm.symphony.fingerprint import (
    FingerprintExtractor,
    LLMConfig,
    LLMIONameResolver,
    LLMSchemaExtractor,
    NormalizationConfig,
    SkillFolderScanner,
    SkillManifestParser,
    SkillFingerprintNormalizer,
    SkillSchemaExtractor,
    write_extraction_result,
)
from jiuwenswarm.symphony.fingerprint.extract.extractor import (
    SCHEMA_EXTRACTION_PROTOCOL_VERSION,
)
from jiuwenswarm.symphony.adapter import (
    capabilities_from_skills,
    graph_build_orchestration_config_from_swarm,
    graph_config_from_swarm,
    llm_config_signature,
    model_from_config,
    model_response_observer_from_config,
)
from jiuwenswarm.symphony.llm import (
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
    """Create default runtime adapters for a Symphony graph build."""

    @staticmethod
    def schema_extractor(
        llm_config: LLMConfig | None,
        extraction_config: Any,
    ) -> SkillSchemaExtractor:
        if llm_config is None:
            raise ValueError(
                "llm_config is required when schema_extractor is not provided"
            )
        return LLMSchemaExtractor(
            llm_config,
            body_limit=extraction_config.body_limit,
            batch_size=extraction_config.batch_size,
        )

    @staticmethod
    def io_name_resolver(
        llm_config: LLMConfig | None,
        normalization_config: Any,
    ) -> Any:
        if llm_config is None:
            raise ValueError(
                "llm_config is required when io_name_resolver is not provided"
            )
        return LLMIONameResolver(
            llm_config,
            batch_size=normalization_config.batch_size,
        )


class SymphonyGraphBuilder:
    """Build and refresh the offline Symphony graph."""

    def __init__(
        self,
        *,
        scanner: SkillFolderScanner | None = None,
        parser: SkillManifestParser | None = None,
        runtime_factory: GraphBuildRuntimeFactory | None = None,
        state_builder: GraphStateBuilder | None = None,
    ) -> None:
        self.scanner = scanner or SkillFolderScanner()
        self.parser = parser or SkillManifestParser()
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
        folders = self.scanner.scan(
            skills_root,
            max_depth=runtime_config.fingerprint.scan.max_depth,
        )
        current_hashes = self.state_builder.folder_hashes(folders)
        state = load_graph_state(output_dir)
        active_entries = state.active_entries()
        exists = graph_exists(output_dir)
        added = [path for path in current_hashes if path not in active_entries]
        changed = [
            path
            for path, digest in current_hashes.items()
            if path in active_entries and active_entries[path].skill_md_sha256 != digest
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
                capabilities = capabilities_from_skills(
                    artifact.get("capabilities") or []
                )
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
            skill_count=len(folders),
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
        schema_extractor: SkillSchemaExtractor | None = None,
        io_name_resolver: Any | None = None,
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

        normalization_runtime_config = runtime_config.fingerprint.normalization
        normalization_config = NormalizationConfig(
            max_vocab_size=normalization_runtime_config.max_vocab_size,
            possible_duplicate_name_similarity_threshold=(
                normalization_runtime_config.duplicate_name_similarity_threshold
            ),
        )
        normalizer = SkillFingerprintNormalizer(
            config=normalization_config,
            io_name_resolver=io_name_resolver
            or self.runtime_factory.io_name_resolver(
                llm_config,
                runtime_config.fingerprint.normalization,
            ),
        )
        schema_extractor = schema_extractor or self.runtime_factory.schema_extractor(
            llm_config,
            runtime_config.fingerprint.extraction,
        )

        fingerprint_extractor = FingerprintExtractor(
            schema_extractor=schema_extractor,
            scanner=self.scanner,
            parser=self.parser,
            normalizer=normalizer,
            progress=_fingerprint_progress_adapter(build_log),
            event_log=build_log,
            max_workers=runtime_config.fingerprint.extraction.workers,
            normalization_workers=runtime_config.fingerprint.normalization.workers,
            normalization_batch_size=runtime_config.fingerprint.normalization.batch_size,
        )
        fingerprint_signature = _fingerprint_signature(runtime_config, llm_config)
        _record_build_log(build_log, "scan.start", skills_root=str(skills_root))
        checkpoint.record("fingerprint.start", status="running")
        extraction_result = await fingerprint_extractor.extract_from_root(
            skills_root,
            output_dir=artifact_dir,
            max_depth=runtime_config.fingerprint.scan.max_depth,
            force=force,
            cache_dir=output_dir,
            fingerprint_signature=fingerprint_signature,
        )
        checkpoint.record(
            "fingerprint.done",
            status="running",
            reused_count=extraction_result.reused_count,
            extracted_count=extraction_result.extracted_count,
        )
        _record_build_log(
            build_log,
            "artifact.fingerprints.write.start",
            fingerprint_count=len(extraction_result.fingerprints),
            diagnostics_count=len(extraction_result.diagnostics),
        )
        write_extraction_result(extraction_result, artifact_dir)
        _record_build_log(build_log, "artifact.fingerprints.write.done")

        _record_build_log(
            build_log,
            "graph.build.start",
            fingerprint_count=len(extraction_result.fingerprints),
            workers=runtime_config.build.workers,
        )
        capabilities = capabilities_from_skills(extraction_result.fingerprints)
        source_snapshot = _graph_source_snapshot(
            capabilities=capabilities,
            current_hashes=extraction_result.current_hashes,
            runtime_config=runtime_config,
            llm_config=llm_config,
        )
        new_state = self.state_builder.next_state(
            folders=extraction_result.folders,
            current_hashes=extraction_result.current_hashes,
            fingerprints_by_path=extraction_result.fingerprints_by_path,
            old_state=load_graph_state(output_dir),
            removed_paths=extraction_result.removed_paths,
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
            write_extraction_result(extraction_result, version_dir)
            _write_io_vocab(extraction_result.io_name_vocab, version_dir)
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
            model=model_from_config(llm_config),
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
            skill_count=len(extraction_result.folders),
            reused_count=extraction_result.reused_count,
            extracted_count=extraction_result.extracted_count,
            removed_count=len(extraction_result.removed_paths),
            edge_count=edge_count,
            diagnostics_count=(
                len(extraction_result.diagnostics) + len(graph_diagnostics)
            ),
            relation_reused_count=relation_reused_count,
            relation_resolved_count=relation_resolved_count,
            version=graph_build.version,
        )


def graph_status(
    skills_root: str | Path,
    graph_dir: str | Path,
    *,
    scanner: SkillFolderScanner | None = None,
    llm_config: LLMConfig | None = None,
    symphony_config: SymphonyConfig | None = None,
) -> GraphStatus:
    """Report whether a graph exists and differs from the Skill folders."""

    return SymphonyGraphBuilder(scanner=scanner).status(
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
    schema_extractor: SkillSchemaExtractor | None = None,
    io_name_resolver: Any | None = None,
    scanner: SkillFolderScanner | None = None,
    parser: SkillManifestParser | None = None,
    build_log: Callable[..., None] | None = None,
    symphony_config: SymphonyConfig | None = None,
    runtime_factory: GraphBuildRuntimeFactory | None = None,
    resume: bool = True,
) -> GraphBuildResult:
    """Build or refresh the offline Symphony graph."""

    del workers
    return await SymphonyGraphBuilder(
        scanner=scanner,
        parser=parser,
        runtime_factory=runtime_factory,
    ).build(
        skills_root,
        graph_dir,
        llm_config,
        force=force,
        schema_extractor=schema_extractor,
        io_name_resolver=io_name_resolver,
        build_log=build_log,
        symphony_config=symphony_config,
        resume=resume,
    )


def _fingerprint_progress_adapter(
    build_log: Callable[..., None] | None,
) -> Callable[[str, int, int, str], None] | None:
    if build_log is None:
        return None

    stage_map = {
        "parse": "fingerprint.parse.start",
        "extract": "fingerprint.extract.start",
        "normalize": "fingerprint.normalize.start",
    }

    def record(stage: str, current: int, total: int, item: str) -> None:
        build_stage = stage_map.get(stage)
        if build_stage is None:
            return
        build_log(build_stage, current=current, total=total, path=item)

    return record


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


def _write_io_vocab(payload: dict[str, Any], graph_dir: Path) -> None:
    _write_json_artifact(payload, graph_dir / "io_name_vocab.json")


def _write_json_artifact(payload: dict[str, Any], path: Path) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


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


def _fingerprint_signature(
    runtime_config: SymphonyConfig,
    llm_config: LLMConfig | None,
) -> str:
    payload = {
        "schema_version": "Symphony-fingerprint-signature-v1",
        "extraction_protocol": SCHEMA_EXTRACTION_PROTOCOL_VERSION,
        "fingerprint": asdict(runtime_config.fingerprint),
        "llm": _llm_signature(llm_config),
    }
    return _stable_hash(payload)


def _llm_signature(llm_config: LLMConfig | None) -> dict[str, Any]:
    if llm_config is None:
        return {}
    return {
        "backend": getattr(llm_config, "backend", ""),
        "model": getattr(llm_config, "model", ""),
        "temperature": getattr(llm_config, "temperature", ""),
    }


def _graph_source_snapshot(
    *,
    capabilities: list[Any],
    current_hashes: dict[str, str],
    runtime_config: SymphonyConfig,
    llm_config: LLMConfig | None,
) -> dict[str, Any]:
    return {
        "schema_version": "JiuwenSwarm-symphony-graph-source-v1",
        "capabilities_sha256": _capabilities_signature(capabilities),
        "current_hashes": dict(sorted(current_hashes.items())),
        "fingerprint_sha256": _fingerprint_signature(runtime_config, llm_config),
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
        fingerprint_sha256 = _fingerprint_signature(runtime_config, llm_config)
        llm_sha256 = llm_config_signature(llm_config)
    return {
        "schema_version": "JiuwenSwarm-symphony-graph-source-v1",
        "capabilities_sha256": _capabilities_signature(capabilities),
        "current_hashes": dict(sorted(current_hashes.items())),
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
    return _stable_hash(
        {
            "extraction_protocol": SCHEMA_EXTRACTION_PROTOCOL_VERSION,
            "fingerprint": asdict(runtime_config.fingerprint),
        }
    )


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
