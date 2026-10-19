from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..render.disclosure import DisclosureConfig, build_exposed_fragment

from ..contracts import CurrentSubtreeProvider
from ..types import CurrentSubtree, ProgressiveRetrieverConfig, SearchCursor, SelectableTarget


@dataclass(frozen=True)
class DefaultCurrentSubtreeProvider(CurrentSubtreeProvider):
    config: ProgressiveRetrieverConfig
    subtree_item_count: object
    cache: dict[tuple[int, tuple[str, ...]], CurrentSubtree] | None = None
    cache_lock: Any | None = None

    def get_current_subtree(self, *, cursor: SearchCursor) -> CurrentSubtree:
        cache_key = (id(cursor.node), tuple(cursor.branch_path))
        if self.cache is not None:
            if self.cache_lock is None:
                cached = self.cache.get(cache_key)
            else:
                with self.cache_lock:
                    cached = self.cache.get(cache_key)
            if cached is not None:
                return CurrentSubtree(
                    cursor=cursor,
                    fragment=cached.fragment,
                    selectable_targets=cached.selectable_targets,
                )
        fragment = build_exposed_fragment(
            root=cursor.node,
            branch_path=cursor.branch_path,
            config=DisclosureConfig(
                max_exposure_depth_per_call=max(0, int(self.config.max_exposure_depth_per_call)),
                exposure_threshold=max(0, int(self.config.exposure_threshold)),
                compact_boundary_codes_enabled=bool(self.config.compact_boundary_codes_enabled),
                compact_boundary_codebook=tuple(str(code) for code in self.config.compact_boundary_codebook),
                flatten_full_tree_in_prompt=bool(self.config.flatten_full_tree_in_prompt),
            ),
            subtree_item_count=self.subtree_item_count,
        )
        subtree = CurrentSubtree(
            cursor=cursor,
            fragment=fragment,
            selectable_targets=tuple(
                SelectableTarget(resolution=item) for item in fragment.code_to_resolution.values()
            ),
        )
        if self.cache is not None:
            if self.cache_lock is None:
                self.cache[cache_key] = subtree
            else:
                with self.cache_lock:
                    self.cache.setdefault(cache_key, subtree)
                    subtree = self.cache[cache_key]
        return subtree
