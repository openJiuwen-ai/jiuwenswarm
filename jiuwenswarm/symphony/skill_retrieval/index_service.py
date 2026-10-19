from __future__ import annotations

import json
import logging
import shutil
import tempfile
import threading
import time
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from .catalog import CATALOG_FILENAME, load_catalog_by_worker
from .config import SkillRetrievalSettings, load_settings
from .dispatch_imports import dispatch_import_path
from .inventory import SkillInventory, scan_skill_inventory
from .markdown import render_build_failure, render_build_success, render_disabled

TREE_INDEX_FILENAME = "tree_index.yaml"
MANIFEST_FILENAME = "manifest.json"
STATE_FILENAME = "state.json"
LOGGER = logging.getLogger(__name__)
BUILD_LOG_LIMIT = 40
TREE_BUILD_PROGRESS_START = 0.35
TREE_BUILD_PROGRESS_END = 0.85
TREE_BUILD_PROGRESS_MIN_INTERVAL_SECONDS = 1.0
TREE_BUILD_PROGRESS_MIN_DELTA = 0.01
TREE_BUILD_PROGRESS_LLM_HINT_LIMIT = 0.12


class SkillIndexBuildCancelled(RuntimeError):
    pass


class SkillIndexBuildTimeout(RuntimeError):
    pass


class SkillIndexService:
    def __init__(self, manager: Any) -> None:
        self._manager = manager

    def status(self) -> dict[str, Any]:
        settings = load_settings()
        inventory = scan_skill_inventory(self._manager)
        index_dir = _index_dir(settings)
        state = _read_state(settings)
        expected = _expected_fingerprint(inventory, settings)
        complete = _is_complete_index(index_dir)
        manifest_matches = complete and _manifest_matches_inventory(index_dir, inventory)
        fresh = complete and manifest_matches and state.get("fingerprint") == expected
        build_state = _build_state_from_state(state)
        if build_state.get("status") == "success" and not fresh:
            build_state = _stale_success_build_state()
        return {
            "enabled": settings.enabled,
            "artifact_root": str(settings.artifact_root),
            "index_dir": str(index_dir),
            "index_exists": complete,
            "fresh": fresh,
            "installed_count": inventory.count,
            "installed_enabled_count": inventory.count,
            "indexed_count": int(state.get("indexed_count") or 0) if complete and manifest_matches else 0,
            "built_at": str(state.get("built_at") or "") if complete else "",
            "inventory_fingerprint": inventory.fingerprint,
            "fingerprint": str(state.get("fingerprint") or "") if complete else "",
            "build_branching_factor": settings.build.branching_factor,
            "build_max_depth": settings.build.max_depth,
            "build_request_timeout_seconds": settings.build.request_timeout_seconds,
            "build_total_timeout_seconds": settings.build.total_timeout_seconds,
            "build_status": build_state.get("status", "idle"),
            "build_stage": build_state.get("stage", ""),
            "build_message": build_state.get("message", ""),
            "build_error": build_state.get("error", ""),
            "build_progress": build_state.get("progress", 0.0),
            "build_started_at": build_state.get("started_at", ""),
            "build_finished_at": build_state.get("finished_at", ""),
            "build_elapsed_seconds": build_state.get("elapsed_seconds", 0.0),
            "build_cancel_requested": bool(build_state.get("cancel_requested", False)),
            "build_logs": build_state.get("logs", []),
            "retrieval_top_k": settings.retrieve.top_k,
            "retrieval_compact_codes_enabled": settings.retrieve.compact_codes_enabled,
            "retrieval_flatten_tree": settings.retrieve.flatten_tree,
            "retrieval_max_exposure_depth": settings.retrieve.max_exposure_depth,
        }

    def build_index(
        self,
        *,
        force: bool = False,
        cancel_check: Any | None = None,
        source: str = "manual",
    ) -> dict[str, Any]:
        started = time.monotonic()
        settings = load_settings()
        if not settings.enabled:
            return {"success": False, "result": render_disabled()}

        _write_build_state(
            settings,
            status="running",
            stage="scan",
            message="Scanning installed skills.",
            progress=0.05,
            started_at=_now_iso(),
            clear_error=True,
            clear_logs=True,
        )
        self._check_cancel_or_timeout(settings, started, cancel_check, stage="scan")
        inventory = scan_skill_inventory(self._manager)
        if inventory.count == 0:
            _cleanup_index(settings)
            _write_build_state(
                settings,
                status="failed",
                stage="scan",
                message="No installed skills were found.",
                error="No installed skills were found under the agent skills directory.",
                progress=1.0,
                finished_at=_now_iso(),
                elapsed_seconds=time.monotonic() - started,
            )
            return {
                "success": False,
                "result": render_build_failure(
                    "No installed skills were found under the agent skills directory."
                ),
            }

        settings.artifact_root.mkdir(parents=True, exist_ok=True)
        _tmp_dir(settings).mkdir(parents=True, exist_ok=True)
        expected = _expected_fingerprint(inventory, settings)
        state = _read_state(settings)

        recovered = False
        if not force:
            recovered = self._recover_index(
                settings=settings,
                inventory=inventory,
                expected_fingerprint=expected,
            )
        state = _read_state(settings)
        index_dir = _index_dir(settings)
        index_complete = _is_complete_index(index_dir)
        manifest_matches = index_complete and _manifest_matches_inventory(index_dir, inventory)
        fingerprint_matches = state.get("fingerprint") == expected
        fresh_index_available = index_complete and manifest_matches and fingerprint_matches
        if not force and fresh_index_available:
            _write_build_state(
                settings,
                status="success",
                stage="reuse",
                message="Existing index is fresh; reused without rebuilding.",
                progress=1.0,
                finished_at=_now_iso(),
                elapsed_seconds=time.monotonic() - started,
                inventory=inventory,
                fingerprint=expected,
            )
            return {
                "success": True,
                "result": render_build_success(
                    reused=True,
                    inventory=inventory,
                    index_dir=str(index_dir),
                    elapsed_seconds=time.monotonic() - started,
                ),
                "recovered": recovered,
            }

        if not settings.llm.model or not settings.llm.api_key:
            if _is_stale_index(settings, inventory, expected):
                _cleanup_index(settings)
            error = (
                "Offline skill tree build requires a model and API key. "
                "Configure `models.defaults[0].model_client_config` or `symphony.skill_retrieval.llm`."
            )
            _write_build_state(
                settings,
                status="failed",
                stage="llm_config",
                message="Build LLM configuration is missing.",
                error=error,
                progress=1.0,
                finished_at=_now_iso(),
                elapsed_seconds=time.monotonic() - started,
                inventory=inventory,
                fingerprint=expected,
            )
            return {
                "success": False,
                "result": render_build_failure(error),
            }

        build_root = (
            _tmp_dir(settings) / f"build-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{time.time_ns()}"
        )
        build_index_dir = build_root / "index"
        try:
            _write_build_state(
                settings,
                status="running",
                stage="llm_check",
                message="Checking skill index build model connectivity.",
                progress=0.2,
                inventory=inventory,
                fingerprint=expected,
            )
            self._check_cancel_or_timeout(settings, started, cancel_check, stage="llm_check")
            self._check_build_llm_access(settings)
            build_index_dir.mkdir(parents=True, exist_ok=True)
            self._check_cancel_or_timeout(settings, started, cancel_check, stage="build")
            _write_build_state(
                settings,
                status="running",
                stage="build",
                message="Building installed skill tree index.",
                progress=0.35,
                inventory=inventory,
                fingerprint=expected,
            )
            if force:
                LOGGER.info(
                    "[skill-index] stage=build status=running "
                    "detail=Starting full rebuild because force rebuild was requested."
                )
                self._run_dispatch_build(settings=settings, inventory=inventory, output_dir=build_index_dir)
            elif index_complete and not fingerprint_matches:
                LOGGER.info("[skill-index] stage=build status=running detail=Starting incremental rebuild.")
                try:
                    self.incremental_build(settings=settings, inventory=inventory, output_dir=build_index_dir)
                except Exception as exc:
                    LOGGER.info(
                        "[skill-index] stage=build status=running "
                        "detail=Incremental rebuild failed; falling back to full rebuild. error=%s",
                        exc,
                    )
                    shutil.rmtree(build_index_dir, ignore_errors=True)
                    build_index_dir.mkdir(parents=True, exist_ok=True)
                    self._run_dispatch_build(settings=settings, inventory=inventory, output_dir=build_index_dir)
            else:
                LOGGER.info("[skill-index] stage=build status=running detail=Starting full rebuild.")
                self._run_dispatch_build(settings=settings, inventory=inventory, output_dir=build_index_dir)
            self._check_cancel_or_timeout(settings, started, cancel_check, stage="publish")
            if not _is_complete_index(build_index_dir):
                raise RuntimeError("skill index build finished without complete index artifacts")
            _write_build_state(
                settings,
                status="running",
                stage="publish",
                message="Publishing skill retrieval index.",
                progress=0.9,
            )
            _publish_index(settings=settings, candidate_dir=build_index_dir)
            _write_state(
                settings,
                inventory=inventory,
                fingerprint=expected,
                elapsed_seconds=time.monotonic() - started,
            )
        except SkillIndexBuildCancelled as exc:
            _write_build_state(
                settings,
                status="cancelled",
                stage="cancelled",
                message=str(exc),
                progress=1.0,
                finished_at=_now_iso(),
                elapsed_seconds=time.monotonic() - started,
                inventory=inventory,
                fingerprint=expected,
            )
            return {"success": False, "result": render_build_failure(str(exc))}
        except SkillIndexBuildTimeout as exc:
            if _is_stale_index(settings, inventory, expected):
                _cleanup_index(settings)
            _write_build_state(
                settings,
                status="failed",
                stage="timeout",
                message="Skill index build timed out.",
                error=str(exc),
                progress=1.0,
                finished_at=_now_iso(),
                elapsed_seconds=time.monotonic() - started,
                inventory=inventory,
                fingerprint=expected,
            )
            return {"success": False, "result": render_build_failure(str(exc))}
        except Exception as exc:
            if _is_stale_index(settings, inventory, expected):
                _cleanup_index(settings)
            error = _normalize_build_error(exc)
            failed_state = _build_state_from_state(_read_state(settings))
            failed_stage = failed_state.get("stage") or "failed"
            if failed_stage not in {"llm_check", "build", "publish"}:
                failed_stage = "failed"
            _write_build_state(
                settings,
                status="failed",
                stage=failed_stage,
                message="Skill index build failed.",
                error=error,
                progress=1.0,
                finished_at=_now_iso(),
                elapsed_seconds=time.monotonic() - started,
                inventory=inventory,
                fingerprint=expected,
            )
            return {"success": False, "result": render_build_failure(error)}
        finally:
            if _is_complete_index(_index_dir(settings)) and build_root.exists():
                shutil.rmtree(build_root, ignore_errors=True)

        return {
            "success": True,
            "result": render_build_success(
                reused=False,
                inventory=inventory,
                index_dir=str(_index_dir(settings)),
                elapsed_seconds=time.monotonic() - started,
            ),
        }

    @staticmethod
    def request_cancel() -> dict[str, Any]:
        settings = load_settings()
        _write_build_state(
            settings,
            status="cancelled",
            stage="cancelled",
            message="Skill index build cancellation requested by user.",
            progress=1.0,
            finished_at=_now_iso(),
            cancel_requested=False,
        )
        return {"success": True, "result": "# Skill Index Build\n\nCancellation requested."}

    @staticmethod
    def mark_background_started(*, source: str = "manual") -> None:
        settings = load_settings()
        if not settings.enabled:
            return
        _write_build_state(
            settings,
            status="running",
            stage="queued",
            message="Skill index build queued.",
            progress=0.01,
            started_at=_now_iso(),
            clear_error=True,
            clear_logs=True,
        )

    def incremental_build(
        self,
        *,
        settings: SkillRetrievalSettings,
        inventory: SkillInventory,
        output_dir: Path,
    ):
        existing_index = _index_dir(settings)
        manifest_path = existing_index / MANIFEST_FILENAME
        existing_paths = _load_manifest_item_paths(manifest_path)
        if existing_paths is None:
            raise RuntimeError("existing index manifest unavailable; falling back to full rebuild")

        current_paths = {str(Path(p).expanduser().resolve()) for p in inventory.item_paths}
        added_paths = sorted(current_paths - existing_paths)
        removed_paths = sorted(existing_paths - current_paths)

        if not added_paths and not removed_paths:
            raise RuntimeError("no changes detected despite fingerprint mismatch; falling back to full rebuild")

        add_index_path: Path | None = None
        try:
            with dispatch_import_path():
                from indexing.tree.builder import TreeBuilder
                from indexing.workflows.index_builder import IndexBuilder

                _ensure_tree_builder_compat(TreeBuilder)
                config = self._make_build_config(settings)
                with _capture_tree_build_progress(TreeBuilder, settings=settings, inventory=inventory):
                    if added_paths:
                        if removed_paths:
                            add_index_path = output_dir.parent / "added-skills"
                        else:
                            add_index_path = output_dir
                        with _suppress_dispatch_console():
                            IndexBuilder.add(
                                item_paths=added_paths,
                                base_index_dir=existing_index,
                                output_dir=add_index_path,
                                item_type="skill",
                                config=config,
                            )
                    if removed_paths:
                        source = add_index_path if added_paths else existing_index
                        with _suppress_dispatch_console():
                            IndexBuilder.delete(
                                item_paths=removed_paths,
                                base_index_dir=source,
                                output_dir=output_dir,
                                item_type="skill",
                                config=config,
                            )
        finally:
            if add_index_path and add_index_path != output_dir:
                shutil.rmtree(add_index_path, ignore_errors=True)

    def tree(self, *, language: str = "cn") -> dict[str, Any]:
        settings = load_settings()
        if not settings.enabled:
            return {"success": False, "result": render_disabled(language)}

        index_dir = _index_dir(settings)
        inventory = scan_skill_inventory(self._manager)
        state = _read_state(settings)
        expected = _expected_fingerprint(inventory, settings)
        if (
            not _is_complete_index(index_dir)
            or not _manifest_matches_inventory(index_dir, inventory)
            or state.get("fingerprint") != expected
        ):
            return {
                "success": False,
                "result": _missing_tree_markdown(language),
                "nodes": [],
                "branch_count": 0,
                "leaf_count": 0,
                "index_dir": str(index_dir),
            }

        tree_path = index_dir / TREE_INDEX_FILENAME
        try:
            payload = yaml.safe_load(tree_path.read_text(encoding="utf-8")) or {}
        except Exception as exc:
            return {"success": False, "result": f"# Skill Index Tree\n\nFailed to read `{tree_path}`: {exc}"}

        nodes = payload.get("nodes") if isinstance(payload, dict) else None
        if not isinstance(nodes, list):
            return {
                "success": False,
                "result": f"# Skill Index Tree\n\n`{tree_path}` does not contain a valid nodes list.",
            }

        branch_count = sum(1 for node in nodes if isinstance(node, dict) and node.get("type") == "branch")
        leaf_count = sum(1 for node in nodes if isinstance(node, dict) and node.get("type") == "leaf")
        catalog_by_worker = load_catalog_by_worker(index_dir)
        tree_nodes = _tree_node_payload(nodes, catalog_by_worker=catalog_by_worker)
        return {
            "success": True,
            "result": (
                "# Skill Index Tree\n\n"
                f"- Index directory: `{index_dir}`\n"
                f"- Branch nodes: {branch_count}\n"
                f"- Skill leaves: {leaf_count}\n\n"
                f"{_render_tree_outline(nodes)}"
            ),
            "nodes": tree_nodes,
            "branch_count": branch_count,
            "leaf_count": leaf_count,
            "index_dir": str(index_dir),
        }

    @staticmethod
    def _make_build_config(settings: SkillRetrievalSettings) -> Any:
        with dispatch_import_path():
            from indexing.workflows.artifacts import (
                BuildConfig,
                BuildExecutionConfig,
                BuildLLMConfig,
                BuildOutputConfig,
                TaxonomyBuildConfig,
            )

            build = settings.build
            llm_config = BuildLLMConfig(
                model=settings.llm.model,
                api_key=settings.llm.api_key,
                base_url=settings.llm.base_url,
                seed=settings.llm.seed,
            )
            for attr in ("stream", "streaming", "enable_streaming"):
                if hasattr(llm_config, attr):
                    setattr(llm_config, attr, False)
            return BuildConfig(
                llm_config=llm_config,
                taxonomy_config=TaxonomyBuildConfig(
                    branching_factor=build.branching_factor,
                    max_depth=build.max_depth,
                    root_categories=build.root_categories,
                    postprocess_enabled=build.postprocess_enabled,
                    postprocess_max_passes=build.postprocess_max_passes,
                    postprocess_min_skills=build.postprocess_min_skills,
                    equivalence_enabled=build.equivalence_enabled,
                ),
                execution_config=BuildExecutionConfig(
                    max_workers=build.max_workers,
                    max_retries=build.max_retries,
                    request_timeout_seconds=build.request_timeout_seconds,
                    classification_batch_limit=build.classification_batch_limit,
                    discovery_seed=build.discovery_seed,
                ),
                output_config=BuildOutputConfig(generate_html=False),
            )

    @staticmethod
    def _check_cancel_or_timeout(
        settings: SkillRetrievalSettings,
        started: float,
        cancel_check: Any | None,
        *,
        stage: str,
    ) -> None:
        state = _read_state(settings)
        cancel_requested = bool(_build_state_from_state(state).get("cancel_requested", False))
        if cancel_requested or (callable(cancel_check) and bool(cancel_check())):
            raise SkillIndexBuildCancelled(f"Skill index build cancelled at stage `{stage}`.")
        total_timeout = float(settings.build.total_timeout_seconds or 0.0)
        if total_timeout > 0 and time.monotonic() - started > total_timeout:
            raise SkillIndexBuildTimeout(
                f"Skill index build exceeded total timeout {total_timeout:.1f}s at stage `{stage}`."
            )

    @staticmethod
    def _run_dispatch_build(
        *,
        settings: SkillRetrievalSettings,
        inventory: SkillInventory,
        output_dir: Path,
    ) -> None:
        with dispatch_import_path():
            from indexing.tree.builder import TreeBuilder
            from indexing.workflows.index_builder import IndexBuilder

            _ensure_tree_builder_compat(TreeBuilder)

            config = SkillIndexService._make_build_config(settings)
            with _suppress_dispatch_console():
                with _capture_tree_build_progress(TreeBuilder, settings=settings, inventory=inventory):
                    IndexBuilder.build(
                        item_paths=inventory.item_paths,
                        output_dir=output_dir,
                        item_type="skill",
                        config=config,
                    )

    @staticmethod
    def _check_build_llm_access(settings: SkillRetrievalSettings) -> None:
        try:
            SkillIndexService._check_build_llm_access_with_tree_runtime(settings)
        except Exception as exc:
            raise RuntimeError(f"Skill index build model is not reachable or rejected the request: {exc}") from exc

    @staticmethod
    def _check_build_llm_access_with_tree_runtime(settings: SkillRetrievalSettings) -> None:
        with dispatch_import_path():
            from indexing.tree import DynamicTreeConfig, TreeBuildConfig, TreeManagerConfig
            from indexing.tree.builder import TreeBuilder
            from indexing.tree.llm_runtime import TreeLLMRuntime
            from indexing.tree.schema import normalize_root_categories
            from indexing.workflows.artifacts import resolve_build_config

            config = SkillIndexService._make_build_config(settings)
            resolved = resolve_build_config(config=config)
            taxonomy = resolved.taxonomy_config
            execution = resolved.execution_config
            root_categories = normalize_root_categories(taxonomy.root_categories)
            with tempfile.TemporaryDirectory(prefix="skill-index-llm-check-") as tmpdir:
                tmp_path = Path(tmpdir)
                builder = TreeBuilder(
                    skills_dir=tmp_path,
                    output_path=tmp_path / "tree_index.yaml",
                    config=DynamicTreeConfig(
                        branching_factor=taxonomy.branching_factor,
                        max_depth=taxonomy.max_depth,
                        root_categories=root_categories,
                    ),
                    manager_config=TreeManagerConfig(
                        branching_factor=taxonomy.branching_factor,
                        max_depth=taxonomy.max_depth,
                        root_categories=root_categories,
                        build=TreeBuildConfig(
                            max_workers=1,
                            num_retries=execution.max_retries,
                            timeout=execution.request_timeout_seconds,
                            classify_batch_cap=execution.classification_batch_limit,
                            postprocess_enabled=taxonomy.postprocess_enabled,
                            postprocess_max_passes=taxonomy.postprocess_max_passes,
                            postprocess_min_skills=taxonomy.postprocess_min_skills,
                            equiv_grouping_enabled=taxonomy.equivalence_enabled,
                            discovery_seed=execution.discovery_seed,
                        ),
                    ),
                    client=resolved.llm_config.client,
                    model=resolved.llm_config.model,
                    api_key=resolved.llm_config.api_key,
                    base_url=resolved.llm_config.base_url,
                    llm_seed=resolved.llm_config.seed,
                    max_workers=1,
                    item_type="skill",
                )
                with _suppress_dispatch_console():
                    result = TreeLLMRuntime(builder).call_llm_json(
                        'Return exactly this JSON object and no extra text: {"ok": true}',
                        max_retries=1,
                    )
            if result.get("ok") is not True:
                raise RuntimeError(f"unexpected connectivity check response: {result!r}")

    @staticmethod
    def _recover_index(
        *,
        settings: SkillRetrievalSettings,
        inventory: SkillInventory,
        expected_fingerprint: str,
    ) -> bool:
        if _is_complete_index(_index_dir(settings)):
            return False
        for candidate in _recovery_candidates(settings):
            try:
                if not _is_complete_index(candidate):
                    continue
                if not _manifest_matches_inventory(candidate, inventory):
                    continue
                _publish_index(settings=settings, candidate_dir=candidate)
                _write_state(settings, inventory=inventory, fingerprint=expected_fingerprint)
                return True
            except Exception as exc:
                _record_recovery_failure(candidate, exc)
        return False


def _cleanup_index(settings: SkillRetrievalSettings) -> None:
    """Remove existing index artifacts while preserving build state for UI reporting."""
    index_dir = _index_dir(settings)
    if index_dir.exists():
        shutil.rmtree(index_dir, ignore_errors=True)


def _index_dir(settings: SkillRetrievalSettings) -> Path:
    return settings.artifact_root / "index"


def _load_manifest_item_paths(manifest_path: Path) -> set[str] | None:
    """Load resolved item_paths from an existing index manifest."""
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    raw = payload.get("item_paths") if isinstance(payload, dict) else None
    if not isinstance(raw, list):
        return None
    return {str(Path(p).expanduser().resolve()) for p in raw}


def _ensure_tree_builder_compat(tree_builder_cls: type) -> None:
    default_attrs = {
        "_skill_profiles_enabled": False,
        "_cache_observability": False,
    }
    for name, value in default_attrs.items():
        if not hasattr(tree_builder_cls, name):
            setattr(tree_builder_cls, name, value)
    if not hasattr(tree_builder_cls, "_write_yaml"):
        setattr(tree_builder_cls, "_write_yaml", _write_tree_yaml)


def _write_tree_yaml(tree_builder: Any, tree_dict: dict[str, Any]) -> None:
    writer = getattr(tree_builder, "_preset_writer")
    writer.write_yaml(tree_dict)


@contextmanager
def _capture_tree_build_progress(
    tree_builder_cls: type,
    *,
    settings: SkillRetrievalSettings,
    inventory: SkillInventory,
):
    original_tree_progress = getattr(tree_builder_cls, "_tree_progress", None)
    if not callable(original_tree_progress):
        yield
        return

    reporter = _SkillTreeBuildProgressReporter(settings=settings, total=inventory.count)

    def wrapped_tree_progress(builder: Any, *args: Any, **kwargs: Any) -> Any:
        progress = original_tree_progress(builder, *args, **kwargs)
        return _TreeProgressProxy(progress, reporter)

    setattr(tree_builder_cls, "_tree_progress", wrapped_tree_progress)
    try:
        yield
    finally:
        setattr(tree_builder_cls, "_tree_progress", original_tree_progress)


class _TreeProgressProxy:
    def __init__(self, progress: Any, reporter: "_SkillTreeBuildProgressReporter") -> None:
        self._progress = progress
        self._reporter = reporter

    def __enter__(self) -> "_TreeProgressProxy":
        self._progress.__enter__()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> Any:
        return self._progress.__exit__(exc_type, exc, tb)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._progress, name)

    def update(self, task_id: Any, *args: Any, **kwargs: Any) -> Any:
        result = self._progress.update(task_id, *args, **kwargs)
        self._reporter.record_update(kwargs)
        return result


class _SkillTreeBuildProgressReporter:
    def __init__(self, *, settings: SkillRetrievalSettings, total: int) -> None:
        self._settings = settings
        self._total = max(0, int(total or 0))
        self._leaf = 0
        self._llm = 0
        self._pending = 0
        self._last_write = 0.0
        self._last_progress = TREE_BUILD_PROGRESS_START
        self._lock = threading.Lock()

    def record_update(self, fields: dict[str, Any]) -> None:
        if not fields:
            return
        with self._lock:
            if "leaf" in fields:
                self._leaf = max(self._leaf, _coerce_nonnegative_int(fields.get("leaf")))
            elif "completed" in fields:
                self._leaf = max(self._leaf, _coerce_nonnegative_int(fields.get("completed")))
            if "llm" in fields:
                self._llm = max(self._llm, _coerce_nonnegative_int(fields.get("llm")))
            if "pending" in fields:
                self._pending = _coerce_nonnegative_int(fields.get("pending"))
            self._maybe_write()

    def _maybe_write(self) -> None:
        state = _read_state(self._settings)
        build_state = _build_state_from_state(state)
        if build_state.get("status") != "running" or build_state.get("stage") != "build":
            return

        now = time.monotonic()
        current_progress = _coerce_progress(build_state.get("progress", 0.0))
        progress = max(current_progress, self._last_progress, self._compute_progress())
        progress = min(TREE_BUILD_PROGRESS_END, max(TREE_BUILD_PROGRESS_START, progress))
        progress_changed = progress - self._last_progress >= TREE_BUILD_PROGRESS_MIN_DELTA
        interval_elapsed = now - self._last_write >= TREE_BUILD_PROGRESS_MIN_INTERVAL_SECONDS
        if not progress_changed or not interval_elapsed:
            return

        _write_build_progress(self._settings, progress=progress)
        self._last_write = now
        self._last_progress = progress

    def _compute_progress(self) -> float:
        leaf_ratio = min(1.0, self._leaf / self._total) if self._total > 0 else 0.0
        llm_hint = min(TREE_BUILD_PROGRESS_LLM_HINT_LIMIT, self._llm * 0.01)
        effective_ratio = max(leaf_ratio, llm_hint)
        return TREE_BUILD_PROGRESS_START + (TREE_BUILD_PROGRESS_END - TREE_BUILD_PROGRESS_START) * effective_ratio


def _record_recovery_failure(candidate: Path, exc: Exception) -> None:
    LOGGER.debug("Skipping unusable skill retrieval index recovery candidate %s: %s", candidate, exc)


class _NullWriter:
    @staticmethod
    def write(text: str) -> int:
        return len(text)

    @staticmethod
    def flush() -> None:
        return None


@contextmanager
def _suppress_dispatch_console():
    sink = _NullWriter()
    with redirect_stdout(sink), redirect_stderr(sink):
        yield


def _tmp_dir(settings: SkillRetrievalSettings) -> Path:
    return settings.artifact_root / "tmp"


def _state_file(settings: SkillRetrievalSettings) -> Path:
    return settings.artifact_root / STATE_FILENAME


def _expected_fingerprint(inventory: SkillInventory, settings: SkillRetrievalSettings) -> str:
    import hashlib

    payload = {
        "inventory": inventory.fingerprint,
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def expected_index_fingerprint(inventory: SkillInventory, settings: SkillRetrievalSettings) -> str:
    return _expected_fingerprint(inventory, settings)


def _stale_success_build_state() -> dict[str, Any]:
    return {
        "status": "idle",
        "stage": "",
        "message": "No usable skill index is available. Rebuild the index to use Agentic skill retrieval.",
        "error": "",
        "progress": 0.0,
        "started_at": "",
        "finished_at": "",
        "elapsed_seconds": 0.0,
        "cancel_requested": False,
        "logs": [],
    }


def _read_state(settings: SkillRetrievalSettings) -> dict[str, Any]:
    path = _state_file(settings)
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_state(
    settings: SkillRetrievalSettings,
    *,
    inventory: SkillInventory,
    fingerprint: str,
    elapsed_seconds: float | None = None,
) -> None:
    state = _read_state(settings)
    build_state = _build_state_from_state(state)
    logs = build_state.get("logs", [])
    elapsed = (
        float(elapsed_seconds)
        if elapsed_seconds is not None
        else build_state.get("elapsed_seconds", 0.0)
    )
    payload = {
        "schema_version": 1,
        "built_at": _now_iso(),
        "fingerprint": fingerprint,
        "indexed_count": inventory.count,
        "index_dir": str(_index_dir(settings)),
        "inventory": inventory.to_state_payload(),
        "build": {
            "status": "success",
            "stage": "success",
            "message": "Skill retrieval index build completed.",
            "error": "",
            "progress": 1.0,
            "started_at": build_state.get("started_at", ""),
            "finished_at": _now_iso(),
            "elapsed_seconds": elapsed,
            "cancel_requested": False,
            "logs": _append_log(logs, stage="success", status="success", message="Build completed."),
        },
    }
    settings.artifact_root.mkdir(parents=True, exist_ok=True)
    _state_file(settings).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_build_state(
    settings: SkillRetrievalSettings,
    *,
    status: str,
    stage: str,
    message: str,
    error: str | None = None,
    progress: float | None = None,
    started_at: str | None = None,
    finished_at: str | None = None,
    elapsed_seconds: float | None = None,
    cancel_requested: bool | None = None,
    inventory: SkillInventory | None = None,
    fingerprint: str | None = None,
    clear_error: bool = False,
    clear_logs: bool = False,
) -> None:
    settings.artifact_root.mkdir(parents=True, exist_ok=True)
    state = _read_state(settings)
    build_state = _build_state_from_state(state)
    next_build = dict(build_state)
    next_build.update(
        {
            "status": status,
            "stage": stage,
            "message": message,
            "progress": _coerce_progress(progress if progress is not None else next_build.get("progress", 0.0)),
        }
    )
    if error is not None:
        next_build["error"] = str(error)
    elif clear_error:
        next_build["error"] = ""
    if started_at is not None:
        next_build["started_at"] = started_at
        next_build["finished_at"] = ""
        next_build["elapsed_seconds"] = 0.0
        next_build["cancel_requested"] = False
    if finished_at is not None:
        next_build["finished_at"] = finished_at
    if elapsed_seconds is not None:
        next_build["elapsed_seconds"] = float(elapsed_seconds)
    if cancel_requested is not None:
        next_build["cancel_requested"] = bool(cancel_requested)
    logs = [] if clear_logs else next_build.get("logs", [])
    next_build["logs"] = _append_log(
        logs,
        stage=stage,
        status=status,
        message=message if error in (None, "") else f"{message} {error}",
    )
    state["schema_version"] = int(state.get("schema_version") or 1)
    if fingerprint and status == "success":
        state["fingerprint"] = fingerprint
    if inventory is not None:
        state["inventory"] = inventory.to_state_payload()
        state["indexed_count"] = inventory.count if status == "success" else int(state.get("indexed_count") or 0)
    state["build"] = next_build
    _state_file(settings).write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    LOGGER.info("[skill-index] stage=%s status=%s detail=%s", stage, status, message)


def repair_interrupted_build_state() -> bool:
    settings = load_settings()
    build_state = _build_state_from_state(_read_state(settings))
    if build_state.get("status") != "running":
        return False

    message = "Previous skill index build was interrupted before it completed. Rebuild the index to continue."
    _write_build_state(
        settings,
        status="failed",
        stage="interrupted",
        message=message,
        error=message,
        progress=1.0,
        finished_at=_now_iso(),
        cancel_requested=False,
    )
    return True


def _write_build_progress(settings: SkillRetrievalSettings, *, progress: float) -> None:
    state = _read_state(settings)
    build_state = _build_state_from_state(state)
    if build_state.get("status") != "running" or build_state.get("stage") != "build":
        return
    next_progress = _coerce_progress(progress)
    if next_progress <= _coerce_progress(build_state.get("progress", 0.0)):
        return
    build_state["progress"] = next_progress
    state["build"] = build_state
    settings.artifact_root.mkdir(parents=True, exist_ok=True)
    _state_file(settings).write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _build_state_from_state(state: dict[str, Any]) -> dict[str, Any]:
    raw = state.get("build") if isinstance(state, dict) else None
    if not isinstance(raw, dict):
        return {
            "status": "idle",
            "stage": "",
            "message": "",
            "error": "",
            "progress": 0.0,
            "started_at": "",
            "finished_at": "",
            "elapsed_seconds": 0.0,
            "cancel_requested": False,
            "logs": [],
        }
    out = dict(raw)
    out["status"] = str(out.get("status") or "idle")
    out["stage"] = str(out.get("stage") or "")
    out["message"] = str(out.get("message") or "")
    out["error"] = str(out.get("error") or "")
    out["progress"] = _coerce_progress(out.get("progress", 0.0))
    out["started_at"] = str(out.get("started_at") or "")
    out["finished_at"] = str(out.get("finished_at") or "")
    try:
        out["elapsed_seconds"] = float(out.get("elapsed_seconds") or 0.0)
    except (TypeError, ValueError):
        out["elapsed_seconds"] = 0.0
    out["cancel_requested"] = bool(out.get("cancel_requested", False))
    logs = out.get("logs")
    out["logs"] = logs[-BUILD_LOG_LIMIT:] if isinstance(logs, list) else []
    return out


def _append_log(logs: Any, *, stage: str, status: str, message: str) -> list[dict[str, Any]]:
    current = list(logs) if isinstance(logs, list) else []
    current.append(
        {
            "time": _now_iso(),
            "stage": stage,
            "status": status,
            "message": str(message or ""),
        }
    )
    return current[-BUILD_LOG_LIMIT:]


def _coerce_progress(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def _coerce_nonnegative_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_stale_index(settings: SkillRetrievalSettings, inventory: SkillInventory, expected: str) -> bool:
    index_dir = _index_dir(settings)
    if not _is_complete_index(index_dir):
        return False
    state = _read_state(settings)
    return not _manifest_matches_inventory(index_dir, inventory) or state.get("fingerprint") != expected


def _normalize_build_error(exc: Exception) -> str:
    text = str(exc)
    if "set to false for non-streaming calls" in text:
        return (
            f"{text}\n\n"
            "The skill index builder uses non-streaming LLM calls. If this remote model rejects the request, "
            "check the provider's non-streaming parameter compatibility or use another build model."
        )
    return text


def _missing_tree_markdown(language: str) -> str:
    normalized = str(language or "").lower()
    if normalized.startswith("zh") or normalized.startswith("cn"):
        return (
            "当前没有可用的已安装技能检索索引，或索引已与当前已安装/启用技能不一致。\n\n"
            "可以点击页面上的“构建索引”重新构建；也可以忽略该能力，继续使用 jiuwenswarm 原有流程。"
        )
    return (
        "No usable installed-skill retrieval index is available, or the index no longer matches the current "
        "installed/enabled skills.\n\n"
        "Use the web page build button to rebuild the index, or ignore retrieval and continue with the original "
        "jiuwenswarm flow."
    )


def _is_complete_index(path: Path) -> bool:
    return (
        (path / TREE_INDEX_FILENAME).is_file()
        and (path / CATALOG_FILENAME).is_file()
        and (path / MANIFEST_FILENAME).is_file()
    )


def _publish_index(*, settings: SkillRetrievalSettings, candidate_dir: Path) -> None:
    final_dir = _index_dir(settings)
    backup_dir = settings.artifact_root / f"index.backup-{time.time_ns()}"
    if final_dir.exists():
        final_dir.rename(backup_dir)
    try:
        candidate_dir.rename(final_dir)
    except Exception:
        if backup_dir.exists() and not final_dir.exists():
            backup_dir.rename(final_dir)
        raise
    if backup_dir.exists():
        shutil.rmtree(backup_dir, ignore_errors=True)


def _recovery_candidates(settings: SkillRetrievalSettings) -> list[Path]:
    candidates: list[Path] = []
    root = settings.artifact_root
    if root.exists():
        candidates.extend(sorted(root.glob("index.backup-*"), key=lambda path: path.stat().st_mtime, reverse=True))
    tmp = _tmp_dir(settings)
    if tmp.exists():
        for build_root in sorted(tmp.glob("build-*"), key=lambda path: path.stat().st_mtime, reverse=True):
            candidates.append(build_root / "index")
            candidates.append(build_root)
    return candidates


def _manifest_matches_inventory(index_dir: Path, inventory: SkillInventory) -> bool:
    try:
        payload = json.loads((index_dir / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    except Exception:
        return False
    raw_paths = payload.get("item_paths") if isinstance(payload, dict) else None
    if not isinstance(raw_paths, list):
        return False
    expected = {str(Path(path).expanduser().resolve()) for path in inventory.item_paths}
    actual = {str(Path(str(path)).expanduser().resolve()) for path in raw_paths}
    return expected == actual


def _render_tree_outline(nodes: list[Any], *, max_nodes: int = 400) -> str:
    by_cid: dict[str, dict[str, Any]] = {}
    children: dict[str, list[str]] = {}
    for raw in nodes:
        if not isinstance(raw, dict):
            continue
        cid = str(raw.get("cid") or "").strip()
        if not cid:
            continue
        by_cid[cid] = raw
        parent = cid.rsplit(".", 1)[0] if "." in cid else ""
        children.setdefault(parent, []).append(cid)

    for child_list in children.values():
        child_list.sort(key=lambda item: (item.count("."), item.lower()))

    lines: list[str] = []
    emitted = 0

    def label_for(cid: str) -> str:
        node = by_cid.get(cid, {})
        name = cid.rsplit(".", 1)[-1]
        description = _compact_text(str(node.get("description") or ""), limit=120)
        if str(node.get("type") or "") == "leaf":
            worker_id = str(node.get("worker_id") or name).strip()
            suffix = f" - {description}" if description else ""
            return f"`{worker_id}`{suffix}"
        suffix = f" - {description}" if description else ""
        return f"{name}{suffix}"

    def walk(parent: str, depth: int) -> None:
        nonlocal emitted
        for cid in children.get(parent, []):
            if emitted >= max_nodes:
                return
            lines.append(f"{'  ' * depth}- {label_for(cid)}")
            emitted += 1
            walk(cid, depth + 1)

    walk("", 0)
    if emitted < len(by_cid):
        lines.append(f"\n... {len(by_cid) - emitted} more nodes omitted")
    return "\n".join(lines) if lines else "(empty tree)"


def _tree_node_payload(nodes: list[Any], *, catalog_by_worker: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    known_cids: set[str] = set()
    for raw in nodes:
        if not isinstance(raw, dict):
            continue
        cid = str(raw.get("cid") or "").strip()
        if cid:
            known_cids.add(cid)

    for raw in nodes:
        if not isinstance(raw, dict):
            continue
        cid = str(raw.get("cid") or "").strip()
        if not cid:
            continue
        parent_cid = cid.rsplit(".", 1)[0] if "." in cid else ""
        if parent_cid not in known_cids:
            parent_cid = ""
        node_type = str(raw.get("type") or "").strip() or "branch"
        worker_id = str(raw.get("worker_id") or "").strip()
        catalog = catalog_by_worker.get(worker_id, {})
        skill_name = str(catalog.get("name") or "").strip() if node_type == "leaf" else ""
        fallback_label = worker_id if node_type == "leaf" and worker_id else cid.rsplit(".", 1)[-1]
        out.append(
            {
                "cid": cid,
                "parent_cid": parent_cid,
                "type": node_type,
                "label": _node_label(raw, fallback_label=fallback_label),
                "description": str(raw.get("description") or "").strip(),
                "select_when": str(raw.get("select_when") or "").strip(),
                "dont_select_when": str(raw.get("dont_select_when") or "").strip(),
                "source_description": str(raw.get("source_description") or "").strip(),
                "worker_id": worker_id,
                "skill_name": skill_name,
                "category": str(raw.get("category") or "").strip(),
                "keywords": _string_list(raw.get("keywords")),
                "examples": _string_list(raw.get("examples")),
            }
        )
    out.sort(key=lambda item: (str(item.get("cid") or "").count("."), str(item.get("cid") or "").lower()))
    return out


def _node_label(node: dict[str, Any], *, fallback_label: str) -> str:
    for key in ("name", "display_name", "worker_id"):
        value = str(node.get(key) or "").strip()
        if value:
            return value
    return fallback_label


def _string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value or "").strip()
    return [text] if text else []


def _compact_text(text: str, *, limit: int) -> str:
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[:max(0, limit - 3)].rstrip() + "..."
