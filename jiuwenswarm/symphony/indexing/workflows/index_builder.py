from __future__ import annotations

import logging
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Sequence

from indexing.models import (
    CATALOG_FILENAME,
    TREE_HTML_FILENAME,
    TREE_INDEX_FILENAME,
)
from indexing.io.items_jsonl import (
    download_http_object_to_path,
    is_http_uri,
    is_passthrough_item_uri,
    load_items_jsonl_text,
    parse_jsonl_scanned_items,
)
from indexing.workflows.tree_ops import (
    align_leaf_nodes_with_catalog,
    build_catalog_records_from_existing,
    enrich_branch_descriptions,
    merge_added_skills_into_tree,
    prune_deleted_skills_from_tree,
    tree_nodes_to_tree_dict,
)
from indexing.tree.visualizer import generate_html as generate_tree_html

from indexing.io import (
    load_catalog_records,
    load_manifest,
    load_tree_preset,
    normalize_item_paths,
    write_manifest,
    write_tree_preset,
)
from indexing.scanners import create_scanner, get_scanner_class, normalize_item_type
from indexing.tree import DynamicTreeConfig, TreeBuildConfig, TreeManagerConfig
from indexing.tree.builder import build_tree
from indexing.tree.schema import normalize_root_categories
from shared.profiling import StageTimer
from shared.storage import download_s3_object_to_path, is_s3_uri, materialize_s3_dir, upload_local_dir_to_s3

from .artifacts import (
    BuildConfig,
    ResolvedBuildConfig,
    build_catalog_records_from_nodes,
    can_build_tree_with_llm,
    resolve_build_config,
    write_catalog,
)

LOGGER = logging.getLogger("index_builder")


@dataclass(frozen=True)
class ResolvedItemPath:
    source_path: str
    source_type: str
    materialized_dir: Path


class IndexBuilder:
    @staticmethod
    def build(
        item_paths: list[str] | None = None,
        output_dir: str | Path | None = None,
        *,
        item_type: str = "skill",
        config: BuildConfig | None = None,
        item_jsonl_path: str | None = None,
    ) -> str | Path:
        timer = StageTimer("IndexBuilder.build", logger=LOGGER)
        try:
            with timer.phase("resolve_inputs"):
                if output_dir is None:
                    raise ValueError("IndexBuilder.build: output_dir is required")
                normalized_item_paths = _resolve_item_paths_or_error(
                    item_paths=item_paths,
                    item_jsonl_path=item_jsonl_path,
                    operation="build",
                )
                resolved_config = resolve_build_config(config=config)
                normalized_item_type = normalize_item_type(item_type)
                pre_scanned_skills, manifest_item_paths = _load_pre_scanned_items(
                    item_jsonl_path=item_jsonl_path,
                    default_paths=normalized_item_paths,
                )
                output_value = str(output_dir).strip()
            if is_s3_uri(output_value):
                with tempfile.TemporaryDirectory(prefix="retriever-index-s3-output-") as tmpdir:
                    local_output_dir = Path(tmpdir) / "index"
                    _IndexBuildWorkflow(
                        item_paths=manifest_item_paths,
                        output_dir=local_output_dir,
                        resolved_config=resolved_config,
                        item_type=normalized_item_type,
                        pre_scanned_skills=pre_scanned_skills,
                        manifest_item_paths=manifest_item_paths,
                    ).build()
                    with timer.phase("upload_s3_output"):
                        upload_local_dir_to_s3(local_output_dir, output_value)
                return output_value.rstrip("/")
            return _IndexBuildWorkflow(
                item_paths=manifest_item_paths,
                output_dir=Path(output_dir),
                resolved_config=resolved_config,
                item_type=normalized_item_type,
                pre_scanned_skills=pre_scanned_skills,
                manifest_item_paths=manifest_item_paths,
            ).build()
        finally:
            timer.finish()

    @staticmethod
    def add(
        item_paths: list[str] | None = None,
        base_index_dir: str | Path | None = None,
        output_dir: str | Path | None = None,
        *,
        item_type: str = "skill",
        config: BuildConfig | None = None,
        item_jsonl_path: str | None = None,
    ) -> str | Path:
        timer = StageTimer("IndexBuilder.add", logger=LOGGER)
        try:
            with timer.phase("resolve_inputs"):
                if base_index_dir is None:
                    raise ValueError("IndexBuilder.add: base_index_dir is required")
                if output_dir is None:
                    raise ValueError("IndexBuilder.add: output_dir is required")
                base_dir = _materialize_existing_index_dir(base_index_dir, cache_namespace="retriever-index-add-cache")
                manifest = load_manifest(base_dir)
                existing = normalize_item_paths(manifest.get("item_paths") or ())
                normalized_item_paths = _resolve_item_paths_or_error(
                    item_paths=item_paths,
                    item_jsonl_path=item_jsonl_path,
                    operation="add",
                )
                added_scanned_skills, added_paths = _load_pre_scanned_items(
                    item_jsonl_path=item_jsonl_path,
                    default_paths=normalized_item_paths,
                )
                combined = normalize_item_paths([*existing, *added_paths])
                resolved_config = resolve_build_config(config=config)
                normalized_item_type = normalize_item_type(item_type)
                output_value = str(output_dir).strip()
            if is_s3_uri(output_value):
                with tempfile.TemporaryDirectory(prefix="retriever-index-add-s3-output-") as tmpdir:
                    local_output_dir = Path(tmpdir) / "index"
                    _IncrementalIndexBuildWorkflow(
                        item_paths=combined,
                        output_dir=local_output_dir,
                        resolved_config=resolved_config,
                        base_index_dir=base_dir,
                        added_paths=added_paths,
                        removed_paths=[],
                        item_type=normalized_item_type,
                        added_scanned_skills=added_scanned_skills,
                        manifest_item_paths=combined,
                    ).build()
                    with timer.phase("upload_s3_output"):
                        upload_local_dir_to_s3(local_output_dir, output_value)
                return output_value.rstrip("/")
            return _IncrementalIndexBuildWorkflow(
                item_paths=combined,
                output_dir=Path(output_dir),
                resolved_config=resolved_config,
                base_index_dir=base_dir,
                added_paths=added_paths,
                removed_paths=[],
                item_type=normalized_item_type,
                added_scanned_skills=added_scanned_skills,
                manifest_item_paths=combined,
            ).build()
        finally:
            timer.finish()

    @staticmethod
    def delete(
        item_paths: list[str] | None = None,
        base_index_dir: str | Path | None = None,
        output_dir: str | Path | None = None,
        *,
        item_type: str = "skill",
        config: BuildConfig | None = None,
        item_jsonl_path: str | None = None,
    ) -> str | Path:
        timer = StageTimer("IndexBuilder.delete", logger=LOGGER)
        try:
            with timer.phase("resolve_inputs"):
                if base_index_dir is None:
                    raise ValueError("IndexBuilder.delete: base_index_dir is required")
                if output_dir is None:
                    raise ValueError("IndexBuilder.delete: output_dir is required")
                base_dir = _materialize_existing_index_dir(
                    base_index_dir, cache_namespace="retriever-index-delete-cache"
                )
                manifest = load_manifest(base_dir)
                existing = normalize_item_paths(manifest.get("item_paths") or ())
                normalized_item_paths = _resolve_item_paths_or_error(
                    item_paths=item_paths,
                    item_jsonl_path=item_jsonl_path,
                    operation="delete",
                )
                _, removed_paths = _load_pre_scanned_items(
                    item_jsonl_path=item_jsonl_path,
                    default_paths=normalized_item_paths,
                )
                removed = set(removed_paths)
                remaining = [path for path in existing if path not in removed]
                normalized_remaining = normalize_item_paths(remaining)
                resolved_config = resolve_build_config(config=config)
                normalized_item_type = normalize_item_type(item_type)
                output_value = str(output_dir).strip()
            if is_s3_uri(output_value):
                with tempfile.TemporaryDirectory(prefix="retriever-index-delete-s3-output-") as tmpdir:
                    local_output_dir = Path(tmpdir) / "index"
                    _IncrementalIndexBuildWorkflow(
                        item_paths=normalized_remaining,
                        output_dir=local_output_dir,
                        resolved_config=resolved_config,
                        base_index_dir=base_dir,
                        added_paths=[],
                        removed_paths=sorted(removed),
                        item_type=normalized_item_type,
                        manifest_item_paths=normalized_remaining,
                    ).build()
                    with timer.phase("upload_s3_output"):
                        upload_local_dir_to_s3(local_output_dir, output_value)
                return output_value.rstrip("/")
            return _IncrementalIndexBuildWorkflow(
                item_paths=normalized_remaining,
                output_dir=Path(output_dir),
                resolved_config=resolved_config,
                base_index_dir=base_dir,
                added_paths=[],
                removed_paths=sorted(removed),
                item_type=normalized_item_type,
                manifest_item_paths=normalized_remaining,
            ).build()
        finally:
            timer.finish()


def _load_pre_scanned_items(
    *,
    item_jsonl_path: str | None,
    default_paths: Sequence[str],
) -> tuple[Dict[str, dict] | None, list[str]]:
    jsonl_text = load_items_jsonl_text(item_jsonl_path=item_jsonl_path)
    if not str(jsonl_text or "").strip():
        return None, list(default_paths)
    scanned_items, manifest_paths = parse_jsonl_scanned_items(jsonl_text)
    return scanned_items, normalize_item_paths(manifest_paths)


def _resolve_item_paths_or_error(
    *,
    item_paths: Sequence[str] | None,
    item_jsonl_path: str | None,
    operation: str,
) -> list[str]:
    normalized_item_paths = normalize_item_paths(item_paths or ())
    if normalized_item_paths:
        return normalized_item_paths
    if str(item_jsonl_path or "").strip():
        return []
    raise ValueError(f"IndexBuilder.{operation}: item_paths is empty and item_jsonl_path is not provided")


def _materialize_existing_index_dir(base_index_dir: str | Path, *, cache_namespace: str) -> Path:
    raw = str(base_index_dir).strip()
    if is_s3_uri(raw):
        return materialize_s3_dir(raw, cache_namespace=cache_namespace)
    return Path(base_index_dir).resolve()


def _resolve_materialized_item_paths(
    item_paths: Sequence[str], *, work_dir: Path, item_type: str
) -> list[ResolvedItemPath]:
    resolved: list[ResolvedItemPath] = []
    extracted_root = work_dir / "materialized"
    extracted_root.mkdir(parents=True, exist_ok=True)
    scanner_cls = get_scanner_class(item_type)

    for index, raw_path in enumerate(item_paths):
        raw_text = str(raw_path).strip()
        if not raw_text:
            continue
        if is_s3_uri(raw_text) or is_http_uri(raw_text):
            archive_path = _download_remote_zip(raw_text, extracted_root / f"item-{index}.zip")
            item_dir = _extract_item_zip(archive_path, extracted_root / f"item-{index}", scanner_cls=scanner_cls)
            source_type = "s3_zip" if is_s3_uri(raw_text) else "http_zip"
            resolved.append(ResolvedItemPath(source_path=raw_text, source_type=source_type, materialized_dir=item_dir))
            continue

        local_path = Path(raw_text).expanduser().resolve()
        if not local_path.exists():
            raise FileNotFoundError(f"Item path not found: {local_path}")
        if local_path.is_dir():
            candidate = scanner_cls.detect_item_root(local_path)
            if candidate is None:
                LOGGER.warning(
                    f"Skipping invalid {scanner_cls.item_type} directory "
                    f"(no {scanner_cls.item_type} root found): {local_path}"
                )
                continue
            resolved.append(
                ResolvedItemPath(
                    source_path=str(local_path),
                    source_type="local_dir",
                    materialized_dir=candidate,
                )
            )
            continue
        if local_path.is_file() and local_path.suffix.lower() == ".zip":
            item_dir = _extract_item_zip(local_path, extracted_root / f"item-{index}", scanner_cls=scanner_cls)
            resolved.append(
                ResolvedItemPath(
                    source_path=str(local_path),
                    source_type="local_zip",
                    materialized_dir=item_dir,
                )
            )
            continue
        raise ValueError(
            f"Unsupported item path: {raw_text}. Only local dir/zip, s3://...zip, and http(s)://...zip are supported"
        )

    names: set[str] = set()
    for item in resolved:
        if item.materialized_dir.name in names:
            raise ValueError(f"Duplicate skill directory name detected: {item.materialized_dir.name}")
        names.add(item.materialized_dir.name)
    return resolved


def _download_remote_zip(uri: str, destination_path: Path) -> Path:
    if not str(uri).lower().endswith(".zip"):
        raise ValueError(f"Remote item path must point to a zip file: {uri}")
    if is_http_uri(uri):
        return download_http_object_to_path(str(uri), destination_path)
    return download_s3_object_to_path(str(uri), destination_path)


def _extract_item_zip(zip_path: Path, target_dir: Path, *, scanner_cls) -> Path:
    if zip_path.suffix.lower() != ".zip":
        raise ValueError(f"Zip path expected, got: {zip_path}")
    if target_dir.exists():
        shutil.rmtree(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as archive:
        _safe_extract_zip(archive, target_dir)

    direct_candidate = scanner_cls.detect_item_root(target_dir)
    if direct_candidate is not None:
        return direct_candidate

    child_dirs = [path for path in sorted(target_dir.iterdir()) if path.is_dir() and not path.name.startswith(".")]
    if len(child_dirs) == 1:
        nested_candidate = scanner_cls.detect_item_root(child_dirs[0])
        if nested_candidate is not None:
            return nested_candidate

    unique_parents = sorted(
        {
            path.resolve()
            for path in target_dir.rglob("*")
            if path.is_dir() and "__MACOSX" not in path.parts and scanner_cls.detect_item_root(path) is not None
        }
    )
    if len(unique_parents) == 1:
        return unique_parents[0]
    if len(unique_parents) > 1:
        pretty = ", ".join(str(path.relative_to(target_dir)) for path in unique_parents[:5])
        raise ValueError("Zip archive contains multiple item roots; unable to choose one: " f"{pretty}")
    raise ValueError(f"Zip archive does not contain a valid {scanner_cls.item_type} root: {zip_path}")


def _safe_extract_zip(archive: zipfile.ZipFile, target_dir: Path) -> None:
    target_root = target_dir.resolve()
    for member in archive.infolist():
        member_name = str(member.filename or "").replace("\\", "/")
        try:
            (target_root / member_name).resolve().relative_to(target_root)
        except ValueError as exc:
            raise ValueError(f"Unsafe zip member path: {member.filename}") from exc
    archive.extractall(target_root)


def _validate_item_dir(path: Path, *, scanner_cls) -> Path:
    candidate = scanner_cls.detect_item_root(path)
    if candidate is None:
        raise ValueError(f"Item directory does not contain a valid {scanner_cls.item_type} root: {path}")
    return candidate


def _normalize_manifest_item_path(value: str | Path) -> str:
    raw = str(value).strip()
    if is_passthrough_item_uri(raw):
        return raw
    return str(Path(raw).expanduser().resolve())


class _IndexBuildWorkflow:
    def __init__(
        self,
        *,
        item_paths: Sequence[str],
        output_dir: Path,
        resolved_config: ResolvedBuildConfig,
        item_type: str,
        pre_scanned_skills: Dict[str, dict] | None = None,
        manifest_item_paths: Sequence[str] | None = None,
    ) -> None:
        self._item_paths = [str(path).strip() for path in item_paths if str(path).strip()]
        self._output_dir = output_dir.resolve()
        self._config = resolved_config
        self._item_type = normalize_item_type(item_type)
        self._pre_scanned_skills = (
            {str(key): dict(value) for key, value in (pre_scanned_skills or {}).items()}
            if pre_scanned_skills is not None
            else None
        )
        self._manifest_item_paths = [
            str(path).strip() for path in (manifest_item_paths or self._item_paths) if str(path).strip()
        ]

    def build(self) -> Path:
        timer = StageTimer("_IndexBuildWorkflow.build", logger=LOGGER)
        try:
            with timer.phase("prepare_workspace"):
                self._output_dir.mkdir(parents=True, exist_ok=True)
            if self._pre_scanned_skills is not None:
                return self._build_from_pre_scanned(timer=timer)
            with tempfile.TemporaryDirectory(prefix="retriever-index-build-") as tmpdir:
                aggregate_dir = Path(tmpdir) / "skills"
                aggregate_dir.mkdir(parents=True, exist_ok=True)
                pre_scanned_skills = self._pre_scanned_skills
                if pre_scanned_skills is None:
                    with timer.phase("materialize_items"):
                        resolved_item_paths = _resolve_materialized_item_paths(
                            self._item_paths, work_dir=Path(tmpdir), item_type=self._item_type
                        )
                        self._materialize_skill_dirs(aggregate_dir, resolved_item_paths)
                    tree_skill_entries = None
                else:
                    resolved_item_paths = []
                    tree_skill_entries = list(pre_scanned_skills.values())

                tree_output_path = self._output_dir / TREE_INDEX_FILENAME
                self._require_llm_tree_build()
                with timer.phase("build_tree_llm"):
                    self._build_llm_tree(
                        skills_dir=aggregate_dir,
                        tree_output_path=tree_output_path,
                        display_skills_dir=(
                            self._infer_display_skills_dir(resolved_item_paths) if pre_scanned_skills is None else None
                        ),
                        skill_entries=tree_skill_entries,
                    )

                with timer.phase("build_catalog_and_tree_outputs"):
                    catalog_records = self._build_catalog_records(
                        aggregate_dir,
                        tree_output_path,
                        resolved_item_paths=resolved_item_paths,
                        pre_scanned_skills=pre_scanned_skills,
                    )
                    nodes = enrich_branch_descriptions(
                        load_tree_preset(tree_output_path).get("nodes") or [], catalog_records=catalog_records
                    )
                    write_tree_preset({"nodes": nodes}, tree_output_path)
                    if self._config.output_config.generate_html:
                        generate_tree_html(
                            tree_nodes_to_tree_dict(nodes, catalog_records), self._output_dir / TREE_HTML_FILENAME
                        )
                    else:
                        self._unlink_if_exists(self._output_dir / TREE_HTML_FILENAME)
                    write_catalog(catalog_records, self._output_dir / CATALOG_FILENAME)
                with timer.phase("write_manifest"):
                    write_manifest(
                        self._output_dir,
                        self._manifest_item_paths,
                        catalog_records,
                        mode="full",
                        item_type=self._item_type,
                    )
            return self._output_dir
        finally:
            timer.finish()

    def _require_llm_tree_build(self) -> None:
        """Validate that the fixed LLM tree build path can run.

        Raises:
            ValueError: If model and client credentials are not configured.
        """

        if can_build_tree_with_llm(self._config):
            return
        raise ValueError(
            "Offline tree build requires llm_config.model and either llm_config.client or llm_config.api_key"
        )

    def _build_llm_tree(
        self,
        *,
        skills_dir: Path,
        tree_output_path: Path,
        display_skills_dir: Path | None = None,
        skill_entries: Sequence[dict] | None = None,
    ) -> None:
        """Run the canonical LLM tree build.

        Args:
            skills_dir: Materialized item directory used by the scanner.
            tree_output_path: Destination tree preset path.
            display_skills_dir: Optional original directory shown in scanned paths.
            skill_entries: Optional pre-scanned entries, bypassing directory scan.
        """

        llm_config = self._config.llm_config
        taxonomy_config = self._config.taxonomy_config
        execution_config = self._config.execution_config
        output_config = self._config.output_config
        root_categories = normalize_root_categories(taxonomy_config.root_categories)
        LOGGER.info(
            "tree llm runtime | workers=%s | timeout_seconds=%s | classify_batch_cap=%s",
            execution_config.max_workers,
            execution_config.request_timeout_seconds,
            execution_config.classification_batch_limit,
        )
        build_tree(
            skills_dir=skills_dir,
            output_path=tree_output_path,
            config=DynamicTreeConfig(
                branching_factor=taxonomy_config.branching_factor,
                max_depth=taxonomy_config.max_depth,
                root_categories=root_categories,
            ),
            manager_config=TreeManagerConfig(
                branching_factor=taxonomy_config.branching_factor,
                max_depth=taxonomy_config.max_depth,
                root_categories=root_categories,
                build=TreeBuildConfig(
                    max_workers=execution_config.max_workers,
                    num_retries=execution_config.max_retries,
                    timeout=execution_config.request_timeout_seconds,
                    classify_batch_cap=execution_config.classification_batch_limit,
                    postprocess_enabled=taxonomy_config.postprocess_enabled,
                    postprocess_max_passes=taxonomy_config.postprocess_max_passes,
                    postprocess_min_skills=taxonomy_config.postprocess_min_skills,
                    equiv_grouping_enabled=taxonomy_config.equivalence_enabled,
                    discovery_seed=execution_config.discovery_seed,
                ),
            ),
            client=llm_config.client,
            model=llm_config.model,
            api_key=llm_config.api_key,
            base_url=llm_config.base_url,
            llm_seed=llm_config.seed,
            max_workers=execution_config.max_workers,
            verbose=False,
            show_tree=False,
            generate_html=output_config.generate_html,
            display_skills_dir=display_skills_dir,
            item_type=self._item_type,
            skill_entries=list(skill_entries) if skill_entries is not None else None,
        )

    def _build_from_pre_scanned(self, *, timer: StageTimer) -> Path:
        pre_scanned_skills = self._pre_scanned_skills or {}
        tree_output_path = self._output_dir / TREE_INDEX_FILENAME
        self._require_llm_tree_build()
        with timer.phase("build_tree_llm"):
            self._build_llm_tree(
                skills_dir=self._output_dir,
                tree_output_path=tree_output_path,
                skill_entries=list(pre_scanned_skills.values()),
            )

        with timer.phase("build_catalog_and_tree_outputs"):
            catalog_records = self._build_catalog_records(
                self._output_dir,
                tree_output_path,
                resolved_item_paths=[],
                pre_scanned_skills=pre_scanned_skills,
            )
            nodes = enrich_branch_descriptions(
                load_tree_preset(tree_output_path).get("nodes") or [], catalog_records=catalog_records
            )
            write_tree_preset({"nodes": nodes}, tree_output_path)
            if self._config.output_config.generate_html:
                generate_tree_html(
                    tree_nodes_to_tree_dict(nodes, catalog_records), self._output_dir / TREE_HTML_FILENAME
                )
            else:
                self._unlink_if_exists(self._output_dir / TREE_HTML_FILENAME)
            write_catalog(catalog_records, self._output_dir / CATALOG_FILENAME)
        with timer.phase("write_manifest"):
            write_manifest(
                self._output_dir, self._manifest_item_paths, catalog_records, mode="full", item_type=self._item_type
            )
        return self._output_dir

    @staticmethod
    def _unlink_if_exists(path: Path) -> None:
        if path.exists():
            path.unlink()

    @staticmethod
    def _materialize_skill_dirs(aggregate_dir: Path, item_paths: Sequence[ResolvedItemPath]) -> None:
        for item in item_paths:
            skill_dir = item.materialized_dir
            destination = aggregate_dir / skill_dir.name
            if destination.exists():
                raise ValueError(f"Duplicate skill directory name detected: {skill_dir.name}")
            try:
                destination.symlink_to(skill_dir, target_is_directory=True)
            except Exception:
                shutil.copytree(skill_dir, destination)

    @staticmethod
    def _infer_display_skills_dir(item_paths: Sequence[ResolvedItemPath]) -> Path | None:
        local_dirs = [item.materialized_dir.parent.resolve() for item in item_paths if item.source_type == "local_dir"]
        if not local_dirs:
            return None
        parents = set(local_dirs)
        return next(iter(parents)) if len(parents) == 1 else None

    def _build_catalog_records(
        self,
        aggregate_dir: Path,
        tree_output_path: Path,
        *,
        resolved_item_paths: Sequence[ResolvedItemPath],
        pre_scanned_skills: Dict[str, dict] | None = None,
    ):
        if pre_scanned_skills is not None:
            scanned = {str(key): dict(value) for key, value in pre_scanned_skills.items()}
            return build_catalog_records_from_nodes(
                nodes=load_tree_preset(tree_output_path).get("nodes") or [], scanned_skills=scanned
            )
        source_by_skill = {item.materialized_dir.name: item.source_path for item in resolved_item_paths}
        scanned: Dict[str, dict] = {}
        scanner = create_scanner(self._item_type, aggregate_dir, display_items_dir=aggregate_dir)
        for item in scanner.to_dict_list():
            scanned[str(item["id"])] = item
        for worker_id, item in scanned.items():
            source_path = source_by_skill.get(worker_id)
            if source_path:
                item["path"] = source_path
        return build_catalog_records_from_nodes(
            nodes=load_tree_preset(tree_output_path).get("nodes") or [], scanned_skills=scanned
        )


class _IncrementalIndexBuildWorkflow(_IndexBuildWorkflow):
    def __init__(
        self,
        *,
        item_paths: Sequence[str],
        output_dir: Path,
        resolved_config: ResolvedBuildConfig,
        base_index_dir: Path,
        added_paths: Sequence[str],
        removed_paths: Sequence[str],
        item_type: str,
        added_scanned_skills: Dict[str, dict] | None = None,
        manifest_item_paths: Sequence[str] | None = None,
    ) -> None:
        super().__init__(
            item_paths=item_paths,
            output_dir=output_dir,
            resolved_config=resolved_config,
            item_type=item_type,
            manifest_item_paths=manifest_item_paths,
        )
        self._base_index_dir = base_index_dir.resolve()
        self._added_paths = [str(path).strip() for path in added_paths if str(path).strip()]
        self._removed_paths = [str(path).strip() for path in removed_paths if str(path).strip()]
        self._added_scanned_skills = (
            {str(key): dict(value) for key, value in (added_scanned_skills or {}).items()}
            if added_scanned_skills is not None
            else None
        )

    def build(self) -> Path:
        self._output_dir.mkdir(parents=True, exist_ok=True)
        if not (self._base_index_dir / TREE_INDEX_FILENAME).exists():
            return super().build()

        existing_catalog = load_catalog_records(self._base_index_dir / CATALOG_FILENAME)
        existing_nodes = list(load_tree_preset(self._base_index_dir / TREE_INDEX_FILENAME).get("nodes") or [])

        remaining_paths = set(self._item_paths)
        removed_path_set = set(self._removed_paths)
        kept_catalog = []
        for record in existing_catalog:
            normalized_path = _normalize_manifest_item_path(record.skill_path)
            if normalized_path in remaining_paths and normalized_path not in removed_path_set:
                kept_catalog.append(record)

        if self._added_paths:
            scanned_added = (
                self._added_scanned_skills if self._added_scanned_skills is not None else self._scan_added_skills()
            )
            existing_nodes = merge_added_skills_into_tree(nodes=existing_nodes, added_skills=scanned_added)
            added_catalog = build_catalog_records_from_nodes(
                nodes=existing_nodes,
                scanned_skills=scanned_added,
                restrict_worker_ids=set(scanned_added),
            )
            merged = {record.worker_id: record for record in kept_catalog}
            for record in added_catalog:
                merged[record.worker_id] = record
            catalog_records = sorted(merged.values(), key=lambda item: item.cid)
        else:
            removed_worker_ids = {
                record.worker_id
                for record in existing_catalog
                if _normalize_manifest_item_path(record.skill_path) in removed_path_set
            }
            existing_nodes = prune_deleted_skills_from_tree(existing_nodes, removed_worker_ids=removed_worker_ids)
            catalog_records = sorted(kept_catalog, key=lambda item: item.cid)

        worker_to_record = {record.worker_id: record for record in catalog_records}
        nodes = align_leaf_nodes_with_catalog(existing_nodes, worker_to_record)
        catalog_records = build_catalog_records_from_existing(nodes=nodes, records_by_worker=worker_to_record)
        nodes = enrich_branch_descriptions(nodes, catalog_records=catalog_records)

        write_tree_preset({"nodes": nodes}, self._output_dir / TREE_INDEX_FILENAME)
        if self._config.output_config.generate_html:
            generate_tree_html(tree_nodes_to_tree_dict(nodes, catalog_records), self._output_dir / TREE_HTML_FILENAME)
        else:
            self._unlink_if_exists(self._output_dir / TREE_HTML_FILENAME)
        write_catalog(catalog_records, self._output_dir / CATALOG_FILENAME)
        write_manifest(
            self._output_dir, self._manifest_item_paths, catalog_records, mode="incremental", item_type=self._item_type
        )
        return self._output_dir

    def _scan_added_skills(self) -> dict[str, dict]:
        scanned: dict[str, dict] = {}
        with tempfile.TemporaryDirectory(prefix="retriever-index-added-items-") as tmpdir:
            resolved_item_paths = _resolve_materialized_item_paths(
                self._added_paths, work_dir=Path(tmpdir), item_type=self._item_type
            )
            for item in resolved_item_paths:
                skill_dir = item.materialized_dir
                scan_root = skill_dir.parent
                scanner = create_scanner(self._item_type, scan_root, display_items_dir=scan_root)
                for scanned_item in scanner.to_dict_list():
                    if str(scanned_item.get("id") or "") == skill_dir.name:
                        scanned[skill_dir.name] = scanned_item
                        if not is_s3_uri(item.source_path):
                            scanned[skill_dir.name]["path"] = _normalize_manifest_item_path(item.source_path)
                        else:
                            scanned[skill_dir.name]["path"] = item.source_path
                        break
        return scanned


__all__ = ["IndexBuilder", "_IndexBuildWorkflow", "_IncrementalIndexBuildWorkflow"]
