from __future__ import annotations

import logging
from abc import ABC
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from ..catalog.records import CatalogRecord

from .artifacts import (
    BuildConfig,
    BuildMethod,
    ResolvedBuildConfig,
)

LOGGER = logging.getLogger("index_builder")


@dataclass(frozen=True)
class BuildArtifactsRequest:
    records: Sequence[CatalogRecord]
    output_dir: Path
    resolved_config: ResolvedBuildConfig
    public_config: BuildConfig


class BaseIndexBuildMethod(ABC):
    name: str = ""
    build_embedding: bool = False
    build_bm25: bool = False

    def build_full(self, request: BuildArtifactsRequest) -> None:
        # BM25 和 Embedding 索引构建已移除
        pass

    def build_incremental(self, request: BuildArtifactsRequest) -> None:
        # BM25 和 Embedding 索引构建已移除
        pass


class BM25IndexBuildMethod(BaseIndexBuildMethod):
    name = "bm25"
    build_embedding = False
    build_bm25 = False


class EmbeddingBM25IndexBuildMethod(BaseIndexBuildMethod):
    name = "embedding+bm25"
    build_embedding = False
    build_bm25 = False


class ProgressiveIndexBuildMethod(EmbeddingBM25IndexBuildMethod):
    name = "progressive"


def resolve_index_build_method(config: ResolvedBuildConfig) -> BaseIndexBuildMethod:
    method_flags = BuildMethod(config.method)
    if method_flags & BuildMethod.TREE:
        return ProgressiveIndexBuildMethod()
    if method_flags & BuildMethod.EMBEDDING:
        return EmbeddingBM25IndexBuildMethod()
    return BM25IndexBuildMethod()


__all__ = [
    "BM25IndexBuildMethod",
    "BaseIndexBuildMethod",
    "BuildArtifactsRequest",
    "EmbeddingBM25IndexBuildMethod",
    "ProgressiveIndexBuildMethod",
    "resolve_index_build_method",
]