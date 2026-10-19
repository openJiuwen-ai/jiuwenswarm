"""
Semantic clustering of execution traces.

Groups traces first by skill set, then clusters queries within each group
via FAISS K-Means (cosine distance).
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass

import numpy as np

from .embed import EmbeddingClient
from .models import TraceRecord

LOGGER = logging.getLogger(__name__)


@dataclass
class ClusteredQuery:
    """One semantic cluster of traces."""

    cluster_id: int
    centroid_query: str
    member_traces: list[TraceRecord]
    success_traces: list[TraceRecord]
    failure_traces: list[TraceRecord]


def cluster_traces(
        traces: list[TraceRecord],
        embedder: EmbeddingClient,
        n_clusters: int | None = None,
        min_cluster_size: int = 1,
        max_k: int = 8,
) -> list[ClusteredQuery]:
    """Cluster traces: first by skill set, then by semantic similarity.

    1. Partition traces by their skill set (frozenset of skill_ids).
    2. Within each group, embed ALL distinct queries (no sampling — preserves
       sub-ability coverage) and cluster via FAISS K-Means.
    3. K is adaptive per skill: min(max_k, max(2, round(sqrt(n/2)))), so small
       skills get few clusters, large skills are capped at max_k.
    4. Split each semantic cluster into success buckets.
    """
    if not traces:
        return []

    # Stage 1: group by skill set
    skill_groups: dict[frozenset, list[TraceRecord]] = {}
    for t in traces:
        key = frozenset(t.skills) if t.skills else frozenset()
        skill_groups.setdefault(key, []).append(t)

    result: list[ClusteredQuery] = []
    cluster_id_counter = 0

    # Stage 2: semantic clustering within each skill group
    for skill_key in sorted(skill_groups.keys(), key=lambda s: sorted(s)):
        group_traces = skill_groups.get(skill_key, [])
        if not group_traces:
            continue
        successes = [t for t in group_traces if t.success]
        # Cluster on the FULL set of distinct queries per skill — no sampling,
        # so sub-abilities of large skills are preserved. LLM distillation
        # caps its own input examples separately via max_success_examples.
        queries = [t.query for t in successes]
        embeddings = embedder.embed_batch(queries)

        # Adaptive k: sqrt(n/2), capped at max_k, at least 2.
        n = len(queries)
        if n_clusters is not None:
            k = n_clusters
        else:
            k = min(max_k, max(2, round((n / 2) ** 0.5)))

        labels = _faiss_cluster(
            embeddings,
            max_iterations=50,
            n_clusters=k,
            min_cluster_size=min_cluster_size,
        )

        local_clusters: dict[int, list[int]] = {}
        for i, label in enumerate(labels):
            if label >= 0:
                local_clusters.setdefault(label, []).append(i)

        for _local_id in sorted(local_clusters.keys()):
            indices = local_clusters.get(_local_id, [])
            members = [successes[i] for i in indices]

            # Filter clusters smaller than the configured min_cluster_size
            if len(members) < min_cluster_size:
                continue

            cluster_query = populate_cluster(cluster_id_counter, embeddings, indices, members)
            result.append(cluster_query)
            cluster_id_counter += 1

    return result


def populate_cluster(
        cluster_id_counter: int,
        embeddings: list[list[float]],
        indices: list[int],
        members: list[TraceRecord],
) -> ClusteredQuery:
    success_traces = [t for t in members if t.success]
    failure_traces = []

    # Find centroid: member whose embedding is closest to the group mean
    member_embeddings = np.array(
        [embeddings[i] for i in indices],
        dtype=np.float32,
    )
    centroid = member_embeddings.mean(axis=0)
    sims = member_embeddings @ centroid
    centroid_idx = int(sims.argmax())
    cluster_query = ClusteredQuery(
        cluster_id=cluster_id_counter,
        centroid_query=members[centroid_idx].query,
        member_traces=members,
        success_traces=success_traces,
        failure_traces=failure_traces,
    )
    return cluster_query


def _faiss_cluster(
        embeddings: list[list[float]],
        *,
        n_clusters: int | None = None,
        max_iterations: int = 50,
        min_cluster_size: int = 1,
) -> list[int]:
    """Cluster embeddings using FAISS K-Means with cosine distance.

    Returns list of cluster labels (-1 = noise / too-small cluster).
    Falls back to all-in-one cluster if FAISS is unavailable.
    """
    n = len(embeddings)
    if n == 0:
        return []
    if n < min_cluster_size:
        return [-1] * n

    try:
        import faiss
    except ImportError:
        LOGGER.warning("FAISS not available, falling back to single-cluster")
        return [0] * n

    # Normalize vectors for inner-product = cosine similarity
    arr = np.array(embeddings, dtype=np.float32)

    # Auto-determine k: aim for clusters of 3-8 items
    if n_clusters is None:
        k = max(2, n // 5)
    else:
        k = max(1, min(n_clusters, n))
    k = min(k, n)
    if k < 2:
        # Too few points for meaningful clustering — treat as single cluster
        return [0] * n

    # FAISS K-Means with cosine distance (via inner product on normalized vectors)
    dim = arr.shape[1]
    kmeans = faiss.Kmeans(
        dim,
        k,
        niter=max_iterations,
        verbose=False,
        gpu=False,
        spherical=True,  # enforces cosine similarity
        min_points_per_centroid=min_cluster_size,
        seed=42,
    )
    kmeans.train(arr)
    _, labels = kmeans.index.search(arr, 1)
    labels = labels.flatten().tolist()

    # Mark clusters smaller than min_cluster_size as noise (-1)
    from collections import Counter
    counts = Counter(labels)
    final = [-1 if counts[x] < min_cluster_size else x for x in labels]
    return final


__all__ = ["ClusteredQuery", "cluster_traces"]