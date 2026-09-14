# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""OHOS compat stub: pymilvus full symbol surface.

Native dependency chain (orjson is Rust-native with no OHOS wheel) makes real
pymilvus uninstallable on HarmonyOS devices. The dev-stable checkpointer chain
only *imports* pymilvus symbols:

- ``openjiuwen/core/session/checkpointer/inmemory.py`` imports
  ``pymilvus.client.utils.is_successful`` (imported, never called on the
  in-memory path)
- ``openjiuwen/extensions/context_evolver/core/db_connector/milvus_connector.py``
  imports ``utility`` at module level (function bodies import the rest lazily)
- ``openjiuwen_deepsearch`` (enterprise_dev 0.2.0 wheel) imports
  ``pymilvus.client.search_result.SearchResult`` in agent_factory/workflow
- milvus store/retriever modules import MilvusClient & friends inside
  functions (non-startup paths)

This stub provides the full import surface (placeholder classes raising
``MilvusException`` on construction) so any import path stays alive; actual
Milvus operations are not part of the HarmonyOS feature set.

Verified on device (2026-09): dev-stable openjiuwen 0.1.16 + jiuwenswarm
dev-stable_test — full startup chain, real LLM conversation, cron agentTurn
and pip isolation all pass with this stub in place.

Installed by scripts/install-ohos-agentserver.sh (phase 2 companion step) via
copy into the venv site-packages. Do NOT list real pymilvus in
requirements-harmony.txt.
"""
from __future__ import annotations

from typing import Any


class MilvusException(Exception):
    """Placeholder exception mirroring pymilvus.MilvusException."""


class _MilvusPlaceholder:
    """Shared placeholder behaviour: usable as a type, unusable at runtime."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise MilvusException(
            "pymilvus is stubbed on HarmonyOS (native grpc/orjson unavailable)"
        )


class Collection(_MilvusPlaceholder):
    pass


class CollectionSchema(_MilvusPlaceholder):
    pass


class MilvusClient(_MilvusPlaceholder):
    pass


class AsyncMilvusClient(_MilvusPlaceholder):
    pass


class AnnSearchRequest(_MilvusPlaceholder):
    pass


class Function(_MilvusPlaceholder):
    pass


class RRFRanker(_MilvusPlaceholder):
    pass


class WeightedRanker(_MilvusPlaceholder):
    pass


class DataType:  # enum-like placeholder
    pass


class FunctionType:  # enum-like placeholder
    pass


class connections:  # module-like namespace
    @staticmethod
    def connect(*args: Any, **kwargs: Any) -> None:
        raise MilvusException("pymilvus stubbed on HarmonyOS")

    @staticmethod
    def disconnect(*args: Any, **kwargs: Any) -> None:
        return None

    @staticmethod
    def has_connection(*args: Any, **kwargs: Any) -> bool:
        return False


class utility:  # module-like namespace
    @staticmethod
    def has_collection(*args: Any, **kwargs: Any) -> bool:
        raise MilvusException("pymilvus stubbed on HarmonyOS")

    @staticmethod
    def list_collections(*args: Any, **kwargs: Any) -> list:
        raise MilvusException("pymilvus stubbed on HarmonyOS")


__version__ = "2.6.9+ohos-stub"
__all__ = [
    "AnnSearchRequest", "AsyncMilvusClient", "Collection", "CollectionSchema",
    "DataType", "Function", "FunctionType", "MilvusClient", "MilvusException",
    "RRFRanker", "WeightedRanker", "connections", "utility",
]
