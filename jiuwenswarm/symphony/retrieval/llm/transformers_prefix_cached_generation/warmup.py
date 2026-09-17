from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from models.retrieval import RetrieverNode
from ..base import PrefixCacheRuntimeOOM
from ...tree.render.disclosure import (
    DisclosureConfig,
    build_disclosure_prompt_parts,
    build_exposed_fragment,
)
from ...tree.types import ProgressiveRetrieverConfig

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PrefixCacheWarmupResult:
    attempted: int = 0
    prepared: int = 0
    skipped: int = 0


def warmup_progressive_prefix_cache(
    *,
    client: Any,
    root: RetrieverNode,
    config: ProgressiveRetrieverConfig,
) -> PrefixCacheWarmupResult:
    if not getattr(getattr(client, "capabilities", None), "progressive_prefix_kv_cache", False):
        logger.debug("prefix cache warmup skipped: client capability disabled")
        return PrefixCacheWarmupResult(skipped=1)
    prepare = getattr(client, "prepare_prefix_cache", None)
    if not callable(prepare):
        logger.debug("prefix cache warmup skipped: client has no prepare_prefix_cache")
        return PrefixCacheWarmupResult(skipped=1)
    disclosure_config = DisclosureConfig(
        max_exposure_depth_per_call=max(0, int(config.max_exposure_depth_per_call)),
        exposure_threshold=max(0, int(config.exposure_threshold)),
        compact_boundary_codes_enabled=bool(config.compact_boundary_codes_enabled),
        compact_boundary_codebook=tuple(str(code) for code in config.compact_boundary_codebook),
        flatten_full_tree_in_prompt=bool(config.flatten_full_tree_in_prompt),
    )
    default_top_k = max(1, int(config.top_k))
    attempted = 0
    prepared = 0
    skipped = 0
    seen: set[str] = set()
    logger.debug(
        "prefix cache warmup start root=%s default_top_k=%s",
        root.node_id,
        default_top_k,
    )

    for node, branch_path in _iter_nodes(root, (root.node_id,)):
        fragment = build_exposed_fragment(
            root=node,
            branch_path=branch_path,
            config=disclosure_config,
            subtree_item_count=_count_items,
        )
        if len(fragment.code_to_resolution) <= 1:
            skipped += 1
            logger.debug(
                "prefix cache warmup skipped node=%s branch_path=%s reason=insufficient_candidates candidates=%s",
                node.node_id,
                branch_path,
                len(fragment.code_to_resolution),
            )
            continue
        parts = build_disclosure_prompt_parts(fragment=fragment, query_messages=(), top_k=default_top_k)
        if parts.cache_id in seen:
            skipped += 1
            logger.debug(
                "prefix cache warmup skipped duplicate cache_id=%s node=%s top_k=%s",
                parts.cache_id,
                node.node_id,
                default_top_k,
            )
            continue
        seen.add(parts.cache_id)
        attempted += 1
        logger.debug(
            "prefix cache warmup preparing cache_id=%s node=%s branch_path=%s default_top_k=%s candidates=%s",
            parts.cache_id,
            node.node_id,
            branch_path,
            default_top_k,
            len(fragment.code_to_resolution),
        )
        try:
            prepare(
                cache_id=parts.cache_id,
                prefix_messages=parts.prefix_messages,
                prefix_token_hash=parts.prefix_token_hash,
                metadata={
                    "node_id": node.node_id,
                    "branch_path": tuple(branch_path),
                    "top_k": int(default_top_k),
                    "fragment_fingerprint": fragment.fragment_fingerprint,
                },
            )
        except PrefixCacheRuntimeOOM as exc:
            logger.warning(
                "prefix cache warmup stopped after OOM attempted=%s prepared=%s skipped=%s "
                "cache_id=%s node=%s top_k=%s error=%s",
                attempted,
                prepared,
                skipped,
                parts.cache_id,
                node.node_id,
                default_top_k,
                exc,
            )
            return PrefixCacheWarmupResult(attempted=attempted, prepared=prepared, skipped=skipped)
        prepared += 1
        logger.debug(
            "prefix cache warmup prepared cache_id=%s attempted=%s prepared=%s skipped=%s",
            parts.cache_id,
            attempted,
            prepared,
            skipped,
        )
    logger.debug(
        "prefix cache warmup complete attempted=%s prepared=%s skipped=%s",
        attempted,
        prepared,
        skipped,
    )
    return PrefixCacheWarmupResult(attempted=attempted, prepared=prepared, skipped=skipped)


def _iter_nodes(node: RetrieverNode, branch_path: tuple[str, ...]):
    yield node, branch_path
    for child in node.children:
        yield from _iter_nodes(child, branch_path + (child.node_id,))


def _count_items(node: RetrieverNode) -> int:
    total = len(node.items)
    for child in node.children:
        total += _count_items(child)
    return total


__all__ = ["PrefixCacheWarmupResult", "warmup_progressive_prefix_cache"]
