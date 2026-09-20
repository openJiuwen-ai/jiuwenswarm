"""Thin application adapter around Symphony's public taxonomy-build SDK.

Discovery itself remains owned by :mod:`openjiuwen.symphony.discovery`.  This
module only projects JiuwenSwarm's live Skill records, model configuration and
artifact path into the public build/status/cancel surface.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import fields
from pathlib import Path
from typing import Any

from openjiuwen.symphony.agent import (
    AgenticSkillRetrievalToolkit,
    LLMConfig as CoreLLMConfig,
    SkillIndexBuildConfig,
    SkillRecord as IndexedSkillRecord,
)
from openjiuwen.symphony.discovery import SkillRecord

from jiuwenswarm.common.config import get_config
from jiuwenswarm.symphony.llm import LLMConfig as SwarmLLMConfig


RecordsProvider = Callable[[], Sequence[SkillRecord]]


class SkillTaxonomyRuntime:
    """Bind JiuwenSwarm configuration to Symphony's existing index runtime."""

    def __init__(
        self,
        *,
        records_provider: RecordsProvider,
        index_root: str | Path,
        config_base: dict[str, Any] | None = None,
    ) -> None:
        self._records_provider = records_provider
        self._index_root = Path(index_root).expanduser().resolve()
        self._config_base = config_base

    @property
    def index_root(self) -> Path:
        return self._index_root

    def start_build(self, *, force: bool = False) -> dict[str, Any]:
        """Start a permitted background build for automatic or explicit use."""

        records = self._indexed_records()
        if not records:
            # Core's empty-inventory path is not a refreshable taxonomy. Reject it
            # before starting a worker so a previously usable index is untouched.
            return {
                "success": False,
                "result": "No enabled Skills are available for taxonomy construction.",
                "data": {"state": "failed", "build_id": ""},
                "error": {
                    "code": "empty_inventory",
                    "message": "No enabled Skills are available.",
                },
            }
        return self._toolkit(with_llm=True, records=records).build_index_async(
            force=force
        )

    def status(self, *, build_id: str | None = None) -> dict[str, Any]:
        return self._toolkit().load_index_status(
            build_id=build_id,
            include_logs=False,
        )

    def cancel(self, *, build_id: str | None = None) -> dict[str, Any]:
        return self._toolkit(records=()).cancel_build(build_id=build_id)

    def _toolkit(
        self,
        *,
        with_llm: bool = False,
        records: Sequence[IndexedSkillRecord] | None = None,
    ) -> AgenticSkillRetrievalToolkit:
        return AgenticSkillRetrievalToolkit(
            index_root=self._index_root,
            skills=records if records is not None else self._indexed_records(),
            build_config=self._build_config(),
            llm_config=self._llm_config() if with_llm else None,
        )

    def _indexed_records(self) -> tuple[IndexedSkillRecord, ...]:
        return tuple(
            IndexedSkillRecord(
                name=record.name,
                description=record.description,
                worker_id=record.worker_id,
                skill_md_path=record.skill_file,
                enabled=True,
                metadata={
                    "source": record.source,
                    "version": record.version,
                    "author": record.author,
                },
                content_hash=record.content_hash,
            )
            for record in self._records_provider()
        )

    def _retrieval_config(self) -> dict[str, Any]:
        config = (
            self._config_base if isinstance(self._config_base, dict) else get_config()
        )
        symphony = config.get("symphony") if isinstance(config, dict) else {}
        retrieval = (
            symphony.get("skill_retrieval") if isinstance(symphony, dict) else {}
        )
        return retrieval if isinstance(retrieval, dict) else {}

    def _build_config(self) -> SkillIndexBuildConfig:
        raw = self._retrieval_config().get("build")
        raw = raw if isinstance(raw, dict) else {}
        allowed = {item.name for item in fields(SkillIndexBuildConfig)}
        values = {key: value for key, value in raw.items() if key in allowed}
        # A failed refresh must never destroy a previously usable taxonomy.
        values["preserve_previous_index_on_failure"] = True
        return SkillIndexBuildConfig(**values)

    def _llm_config(self) -> CoreLLMConfig:
        raw = self._retrieval_config().get("llm")
        raw = raw if isinstance(raw, dict) else {}
        model = str(raw.get("model") or raw.get("model_name") or "").strip()
        api_key = str(raw.get("api_key") or "").strip()
        base_url = str(raw.get("base_url") or raw.get("api_base") or "").strip()

        if not (model and api_key and base_url):
            try:
                default = SwarmLLMConfig.from_default_model()
            except (RuntimeError, ValueError):
                # Core's non-strict builder has a deterministic taxonomy
                # fallback. Absence of a configured chat model must reach that
                # path instead of failing in this application adapter.
                default = SwarmLLMConfig()
            client = default.model_client_config or {}
            model = model or default.model
            api_key = api_key or str(client.get("api_key") or "").strip()
            base_url = base_url or default.base_url

        seed_value = raw.get("seed")
        seed = int(seed_value) if seed_value is not None else None
        return CoreLLMConfig(
            model=model,
            api_key=api_key,
            base_url=base_url,
            seed=seed,
        )
