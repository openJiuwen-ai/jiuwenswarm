from __future__ import annotations

from pathlib import Path
from typing import Any

from .catalog import CATALOG_FILENAME, load_catalog_by_worker
from .config import load_settings
from .dispatch_imports import dispatch_import_path
from .index_service import _index_dir
from .markdown import render_disabled, render_retrieve_failure, render_retrieve_success
from .skill_tree import build_skill_tree_payload


class SkillRetrieveService:
    def __init__(self, manager: Any) -> None:
        self._manager = manager

    def retrieve(self, query: str) -> dict[str, Any]:
        settings = load_settings()
        if not settings.enabled:
            return {"success": False, "result": render_disabled()}
        normalized_query = str(query or "").strip()
        if not normalized_query:
            return {"success": False, "result": render_retrieve_failure("`query` is required.")}

        from .index_service import SkillIndexService

        status = SkillIndexService(self._manager).status()
        if not status.get("index_exists"):
            return {
                "success": False,
                "result": render_retrieve_failure(
                    "Skill retrieval index does not exist. Next step: call `skill_index_build`, "
                    "then call `skill_retrieve` again with the same query."
                ),
            }
        if not status.get("fresh"):
            return {
                "success": False,
                "result": render_retrieve_failure(
                    "Skill retrieval index is stale because installed skills or build settings changed. "
                    "Next step: call `skill_index_build` to refresh it, then call `skill_retrieve` again "
                    "with the same query."
                ),
            }
        if not settings.llm.model or not settings.llm.api_key:
            return {
                "success": False,
                "result": render_retrieve_failure(
                    "Skill retrieval requires a model and API key. "
                    "Configure `models.defaults[0].model_client_config` or `symphony.skill_retrieval.llm`."
                ),
            }

        index_dir = _index_dir(settings)
        catalog_by_worker = load_catalog_by_worker(index_dir)
        try:
            result = self._run_dispatch_retrieve(settings=settings, index_dir=index_dir, query=normalized_query)
        except Exception as exc:
            return {"success": False, "result": render_retrieve_failure(str(exc))}

        payload = {
            "success": True,
            "result": render_retrieve_success(
                query=normalized_query,
                index_dir=str(index_dir),
                indexed_count=int(status.get("indexed_count") or 0),
                result=result,
                catalog_by_worker=catalog_by_worker,
                settings_summary=_settings_summary(settings),
            ),
        }
        skill_tree = build_skill_tree_payload(
            query=normalized_query,
            result=result,
            catalog_by_worker=catalog_by_worker,
        )
        if skill_tree is not None:
            payload["skill_tree"] = skill_tree
        return payload

    @staticmethod
    def _run_dispatch_retrieve(*, settings: Any, index_dir: Path, query: str) -> Any:
        return run_structured_skill_retrieve(
            settings=settings,
            index_dir=index_dir,
            query=query,
        )


def run_structured_skill_retrieve(*, settings: Any, index_dir: Path, query: str) -> Any:
    with dispatch_import_path():
        from retrieval.service.models import (
            GenerationConfig,
            OpenAIClientConfig,
            RenderConfig,
            RequestConfig,
            RetrieverConfig,
            TraversalConfig,
        )
        from retrieval.service.retriever import Retriever

        retrieve = settings.retrieve
        config = RetrieverConfig(
            top_k=retrieve.top_k,
            llm_client_config=OpenAIClientConfig(
                model=settings.llm.model,
                api_key=settings.llm.api_key,
                base_url=settings.llm.base_url,
                seed=settings.llm.seed,
            ),
            traversal_config=TraversalConfig(
                max_branch_choices=retrieve.max_branch_choices,
                max_parallel_branches=retrieve.max_parallel_branches,
                enable_parallel_branches=True,
            ),
            render_config=RenderConfig(
                compact_codes_enabled=retrieve.compact_codes_enabled,
                flatten_tree=retrieve.flatten_tree,
                max_exposure_depth=retrieve.max_exposure_depth,
            ),
            generation_config=GenerationConfig(
                max_tokens=retrieve.max_tokens,
                request_timeout_seconds=retrieve.request_timeout_seconds,
            ),
        )
        retriever = Retriever.from_index(index_dir, config=config)
        try:
            return retriever.search_details(
                query,
                search_config=RequestConfig(top_k=retrieve.top_k),
            )
        finally:
            retriever.close()


def _settings_summary(settings: Any) -> str:
    retrieve = settings.retrieve
    return (
        "compact=false, flat=false"
        if not retrieve.compact_codes_enabled and not retrieve.flatten_tree
        else f"compact={retrieve.compact_codes_enabled}, flat={retrieve.flatten_tree}"
    ) + (
        f", top_k={retrieve.top_k}, max_branch_choices={retrieve.max_branch_choices}, "
        f"max_parallel_branches={retrieve.max_parallel_branches}"
    )
