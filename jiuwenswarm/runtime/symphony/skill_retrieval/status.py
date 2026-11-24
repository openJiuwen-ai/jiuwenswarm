"""Read the configured Skill collection for the host's retrieval status query."""

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass

from openjiuwen.symphony import SymphonyConfig, SymphonyRuntime


@dataclass(frozen=True)
class LiveRetrievalStatus:
    """Collection counts and discovery settings before session overrides."""

    installed_count: int
    installed_enabled_count: int
    candidate_scale: str
    estimated_candidate_tokens: int
    candidate_budget_tokens: int
    layout: str
    index_state: str


async def read_live_retrieval_status(
    config: SymphonyConfig,
    *,
    disabled_capability_ids: Callable[[], Iterable[str]],
    source_by_id: Callable[[], Mapping[str, str]],
) -> LiveRetrievalStatus:
    """Read the collection's live discovery status.

    Args:
        config: Resolved host Symphony configuration.
        disabled_capability_ids: Provide disabled IDs in the configured collection.
        source_by_id: Provide installation provenance by capability ID.

    Returns:
        Flat-view counts and prompt budget together with the active index layout.
    """
    settings = config.retrieval
    runtime = SymphonyRuntime(None, config)
    try:
        flat_config = settings.model_copy(update={"use_existing_index": False})
        installed_count = len(runtime.retrieval.bind_search(config=flat_config).records())
        directory = runtime.retrieval.bind_search(
            config=flat_config,
            disabled_capability_ids=disabled_capability_ids,
            source_by_id=source_by_id,
        )
        installed_enabled_count = len(directory.records())
        snapshot = directory.prompt_snapshot(refresh=False)
        candidate_scale = "small" if snapshot.all_candidates_included else "large"
        if candidate_scale == "large" and settings.use_existing_index:
            directory = runtime.retrieval.bind_search(
                config=settings,
                disabled_capability_ids=disabled_capability_ids,
                source_by_id=source_by_id,
            )
        artifact = directory.artifact
        return LiveRetrievalStatus(
            installed_count=installed_count,
            installed_enabled_count=installed_enabled_count,
            candidate_scale=candidate_scale,
            estimated_candidate_tokens=snapshot.estimated_candidate_tokens,
            candidate_budget_tokens=snapshot.candidate_budget_tokens,
            layout=artifact.layout,
            index_state=artifact.index_state,
        )
    finally:
        await runtime.aclose()
