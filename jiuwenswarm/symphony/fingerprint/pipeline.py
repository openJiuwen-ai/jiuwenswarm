"""Fingerprint extraction orchestration."""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Dict, Iterable, Optional, Union

from jiuwenswarm.symphony.llm import get_llm_token_usage_summary
from jiuwenswarm.symphony.fingerprint.batching import (
    chunked,
    ensure_result_count,
    gather_limited,
)
from jiuwenswarm.symphony.fingerprint.incremental import (
    IncrementalFingerprintStore,
    load_io_name_vocabulary,
)
from jiuwenswarm.symphony.fingerprint.manifest import SkillManifestParser
from jiuwenswarm.symphony.fingerprint.models import (
    ExtractedSkillSchema,
    FingerprintExtractionResult,
    NormalizationResult,
    RawSkillManifest,
    SkillFolder,
    SkillSchemaExtractor,
)
from jiuwenswarm.symphony.fingerprint.normalize import SkillFingerprintNormalizer
from jiuwenswarm.symphony.fingerprint.scan import SkillFolderScanner

ProgressCallback = Callable[[str, int, int, str], None]
EventCallback = Callable[..., None]
IndexedSkillFolder = tuple[int, SkillFolder]


class FingerprintExtractor:
    """Scan, parse, extract, and normalize Skill fingerprints."""

    def __init__(
        self,
        schema_extractor: SkillSchemaExtractor,
        scanner: Optional[SkillFolderScanner] = None,
        parser: Optional[SkillManifestParser] = None,
        normalizer: SkillFingerprintNormalizer | None = None,
        progress: Optional[ProgressCallback] = None,
        event_log: Optional[EventCallback] = None,
        max_workers: int = 1,
        normalization_workers: int | None = None,
        normalization_batch_size: int | None = None,
    ) -> None:
        self.schema_extractor = schema_extractor
        self.scanner = scanner or SkillFolderScanner()
        self.parser = parser or SkillManifestParser()
        if normalizer is None:
            raise ValueError("FingerprintExtractor requires normalizer.")
        self.normalizer = normalizer
        self.progress = progress
        self.event_log = event_log
        self.max_workers = max(1, max_workers)
        # Reserved for a future deterministic merge strategy. Normalization LLM
        # batches remain serial because every batch may extend the shared vocabulary.
        self.normalization_workers = max(
            1,
            int(normalization_workers if normalization_workers is not None else max_workers),
        )
        self.normalization_batch_size = normalization_batch_size

    async def extract_from_root(
        self,
        skills_root: Union[Path, str],
        output_dir: Union[Path, str],
        *,
        max_depth: int | None = None,
        force: bool = False,
        cache_dir: Union[Path, str] | None = None,
        fingerprint_signature: str = "",
    ) -> FingerprintExtractionResult:
        output_path = Path(output_dir).resolve()
        cache_path = Path(cache_dir).resolve() if cache_dir is not None else output_path
        folders, current_hashes = self.scanner.snapshot(
            skills_root,
            max_depth=max_depth,
        )
        self._emit_event("scan.done", skill_count=len(folders))
        incremental_store = IncrementalFingerprintStore(
            cache_path,
            signature=fingerprint_signature,
        )
        self.normalizer.io_name_vocabulary = load_io_name_vocabulary(
            cache_path,
            self.normalizer,
        )
        reuse_plan = incremental_store.plan(
            folders,
            current_hashes,
            force=force,
            on_reuse=lambda folder_index, folder, fingerprint: self._emit_event(
                "fingerprint.reuse",
                folder_index + 1,
                total=len(folders),
                path=folder.relative_path,
                skill_id=fingerprint.id,
            ),
        )
        self._emit_event(
            "diff.done",
            changed_count=len(reuse_plan.changed_folders),
            removed_count=len(reuse_plan.removed_paths),
        )

        diagnostics = []
        decisions = []
        extracted_results = await self._extract_selected(
            reuse_plan.changed_folders,
            total=len(folders),
            emit_done=False,
        )
        fingerprints = list(reuse_plan.fingerprints)
        fingerprints_by_path = dict(reuse_plan.fingerprints_by_path)
        for folder_index, folder in reuse_plan.changed_folders:
            normalized = extracted_results[folder_index]
            fingerprints.append(normalized.fingerprint)
            fingerprints_by_path[folder.relative_path] = normalized.fingerprint
            incremental_store.save_result(
                folder,
                current_hashes[folder.relative_path],
                normalized,
            )
            diagnostics.extend(normalized.diagnostics)
            decisions.extend(normalized.decisions)
            self._emit_event(
                "fingerprint.done",
                folder_index + 1,
                total=len(folders),
                path=folder.relative_path,
                skill_id=normalized.fingerprint.id,
                diagnostics=len(normalized.diagnostics),
            )

        fingerprints = sorted(fingerprints, key=lambda item: item.id)
        return FingerprintExtractionResult(
            fingerprints=fingerprints,
            diagnostics=diagnostics,
            normalization_decisions=decisions,
            io_name_vocab=self.normalizer.io_name_vocabulary.to_dict(),
            llm_token_usage=get_llm_token_usage_summary(),
            folders=folders,
            current_hashes=current_hashes,
            removed_paths=reuse_plan.removed_paths,
            fingerprints_by_path=fingerprints_by_path,
            reused_count=reuse_plan.reused_count,
            extracted_count=len(reuse_plan.changed_folders),
        )

    async def _extract_selected(
        self,
        indexed_folders: Iterable[IndexedSkillFolder],
        *,
        total: int | None = None,
        emit_done: bool = True,
    ) -> dict[int, NormalizationResult]:
        """Run parse, schema extraction, and normalization for selected folders.

        The input keeps each folder's original scan index so progress events and
        returned results can still line up with the full scan order.
        """
        selected_folders = list(indexed_folders)
        if not selected_folders:
            return {}

        progress_total = total
        if progress_total is None:
            progress_total = max(folder_index for folder_index, _folder in selected_folders) + 1

        manifests = self._parse_manifests(selected_folders, progress_total)
        extracted = await self._extract_schemas(selected_folders, progress_total, manifests)
        results = await self._normalize_schemas(
            selected_folders,
            progress_total,
            manifests,
            extracted,
        )
        if emit_done:
            for folder_index, folder in selected_folders:
                self._emit_progress("done", folder_index + 1, progress_total, folder.relative_path)
        return results

    def _parse_manifests(
        self,
        indexed_folders: list[IndexedSkillFolder],
        total: int,
    ) -> dict[int, RawSkillManifest]:
        manifests: dict[int, RawSkillManifest] = {}
        for folder_index, folder in indexed_folders:
            self._emit_progress("parse", folder_index + 1, total, folder.relative_path)
            manifests[folder_index] = self.parser.parse(folder)
        return manifests

    def _emit_progress(self, stage: str, current: int, total: int, item: str) -> None:
        if self.progress is not None:
            self.progress(stage, current, total, item)

    def _emit_event(self, stage: str, current: int | None = None, **details: object) -> None:
        if self.event_log is None:
            return
        if current is not None:
            details["current"] = current
        self.event_log(stage, **details)

    def _should_extract_in_batches(self) -> bool:
        return bool(
            getattr(self.schema_extractor, "use_batch", False)
            and hasattr(self.schema_extractor, "extract_many")
        )

    async def _extract_schemas(
        self,
        indexed_folders: list[IndexedSkillFolder],
        total: int,
        manifests_by_index: dict[int, RawSkillManifest],
    ) -> dict[int, ExtractedSkillSchema]:
        batch_results = await gather_limited(
            self._schema_extraction_batches(indexed_folders),
            max_workers=self.max_workers,
            run_batch=lambda batch: self._extract_schema_batch(
                batch,
                total,
                manifests_by_index,
            ),
        )

        results: dict[int, ExtractedSkillSchema] = {}
        for batch_result in batch_results:
            results.update(batch_result)
        return results

    def _schema_extraction_batches(
        self,
        indexed_folders: list[IndexedSkillFolder],
    ) -> list[list[IndexedSkillFolder]]:
        batch_size = 1
        if self._should_extract_in_batches():
            batch_size = max(
                1,
                int(getattr(self.schema_extractor, "batch_size", 32)),
            )
        return chunked(indexed_folders, batch_size)

    async def _normalize_schemas(
        self,
        indexed_folders: list[IndexedSkillFolder],
        total: int,
        manifests_by_index: Dict[int, RawSkillManifest],
        extracted_by_index: Dict[int, ExtractedSkillSchema],
    ) -> dict[int, NormalizationResult]:
        results_by_index: dict[int, NormalizationResult] = {}
        for batch in chunked(indexed_folders, self._normalization_batch_size()):
            for folder_index, folder in batch:
                self._emit_progress("normalize", folder_index + 1, total, folder.relative_path)
            results = await self.normalizer.normalize(
                [
                    (manifests_by_index[folder_index], extracted_by_index[folder_index])
                    for folder_index, _folder in batch
                ]
            )
            ensure_result_count(
                len(results),
                len(batch),
                "Batch normalizer",
            )
            results_by_index.update(
                {
                    folder_index: result
                    for (folder_index, _folder), result in zip(batch, results)
                }
            )
        return results_by_index

    async def _extract_schema_batch(
        self,
        batch: list[IndexedSkillFolder],
        total: int,
        manifests_by_index: Dict[int, RawSkillManifest],
    ) -> dict[int, ExtractedSkillSchema]:
        for folder_index, folder in batch:
            self._emit_progress("extract", folder_index + 1, total, folder.relative_path)
        batch_indexes = [folder_index for folder_index, _folder in batch]
        batch_manifests = [manifests_by_index[index] for index in batch_indexes]
        extract_many = getattr(self.schema_extractor, "extract_many", None)
        if self._should_extract_in_batches() and callable(extract_many):
            batch_extracted = list(await extract_many(batch_manifests))
        else:
            batch_extracted = [
                await self.schema_extractor.extract(manifest)
                for manifest in batch_manifests
            ]
        ensure_result_count(
            len(batch_extracted),
            len(batch_manifests),
            "Batch schema extractor",
        )
        return dict(zip(batch_indexes, batch_extracted))

    def _normalization_batch_size(self) -> int:
        if self.normalization_batch_size is not None:
            return max(1, int(self.normalization_batch_size))
        return max(
            1,
            int(
                getattr(self.normalizer.io_name_resolver, "batch_size", 1)
            ),
        )
