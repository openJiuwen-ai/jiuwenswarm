from __future__ import annotations

import logging
import random
import threading
import time
from collections import defaultdict
from typing import Any

import numpy as np

from .bank import ExperienceBank
from .cluster import ClusteredQuery, cluster_traces, _faiss_cluster, populate_cluster
from .distiller import TraceDistiller
from .embed import EmbeddingClient
from .models import ExperienceBankBuildConfig, DistilledPattern, ExperienceItem, TraceRecord

LOGGER = logging.getLogger(__name__)


class ExperienceBaseBuilder:
    """Build an ``ExperienceBank`` index from parsed traces via
    cluster → distill → persist pipeline.
    """

    def __init__(
        self,
        kb: ExperienceBank,
        embedding_client: EmbeddingClient,
        llm_client: Any,
        llm_model: str,
        *,
        skills_info: list[dict[str, str]] | None = None,
        build_config: ExperienceBankBuildConfig | None = None,
    ) -> None:
        self._kb = kb
        self._embedder = embedding_client
        if not llm_client:
            raise ValueError("ExperienceBaseBuilder requires llm_client for distillation.")
        self._llm = llm_client
        if not llm_model:
            raise ValueError("ExperienceBaseBuilder requires llm_model for distillation.")
        self._llm_model = llm_model
        self._skills_info = skills_info
        self._config = build_config or ExperienceBankBuildConfig()
        self._min_cluster_size = self._config.min_cluster_size
        self._max_workers = self._config.max_workers
        self._max_success_examples = self._config.max_success_examples
        self._pending: list[TraceRecord] = []
        self._flush_threshold = self._config.pending_flush_threshold
        self._min_hits = self._config.min_hits_for_pattern
        self._pattern_merge_threshold = self._config.pattern_merge_threshold
        self._query_examples_count = self._config.query_examples_count
        self._skill_cluster_num = self._config.skill_cluster_num
        self._lock = threading.Lock()

    def build(self, traces: list[TraceRecord]) -> int:
        """Build the experience KB from a list of parsed ``TraceRecord``.

        Pipeline stages:
            1. **Cluster** — group by skill set, then semantic cluster via FAISS
            2. **Distill** — LLM distills each cluster into a generalized pattern
            3. **Write** — write distilled patterns into ``ExperienceBank``

        Returns the number of experience items created.

        Raises:
            ValueError: if the target KB already contains entries (full
            rebuild only — use a fresh directory to avoid accidental data loss).
        """
        if self._kb.count > 0:
            LOGGER.error(
                "ExperienceBaseBuilder: refusing to build — "
                "target KB directory is not empty (existing %d entries). "
                "This is a full-build operation; use a fresh directory to avoid overwriting data.",
                self._kb.count,
            )
            raise ValueError(
                f"KB directory is not empty: {self._kb.count} entries exist. "
                f"ExperienceBaseBuilder performs a full build and will overwrite existing data. "
                f"Use a fresh directory or clear the KB first."
            )

        t0 = time.monotonic()

        if not traces:
            LOGGER.warning("TraceIndexBuilder: no traces provided, skipping")
            return 0

        valid_traces = self._filter_valid_traces(traces)
        if not valid_traces:
            LOGGER.warning("TraceIndexBuilder: no valid traces after filtering, skipping")
            return 0

        # --- Cluster ---
        t1 = time.monotonic()
        clusters = cluster_traces(
            valid_traces, self._embedder,
            n_clusters=self._skill_cluster_num,
            min_cluster_size=self._min_cluster_size,
        )
        cluster_elapsed = time.monotonic() - t1
        LOGGER.info(
            "TraceIndexBuilder: clustering done: %d clusters in %.2fs",
            len(clusters), cluster_elapsed,
        )

        if not clusters:
            LOGGER.warning("TraceIndexBuilder: no clusters formed, skipping")
            return 0

        # --- Distill ---
        t2 = time.monotonic()
        distiller = TraceDistiller(
            self._llm,
            self._llm_model,
            skills_info=self._skills_info,
            max_workers=self._max_workers,
            max_success_examples=self._max_success_examples,
        )
        distilled = distiller.run(clusters)
        distill_elapsed = time.monotonic() - t2
        LOGGER.info(
            "TraceIndexBuilder: distillation done: %d patterns in %.2fs",
            len(distilled), distill_elapsed,
        )

        # --- Write to KB ---
        t3 = time.monotonic()
        cluster_by_id = {c.cluster_id: c for c in clusters}

        valid_patterns = [p for p in distilled if p.pattern_description]
        merged_patterns = self._merge_similar_patterns(valid_patterns, cluster_by_id)
        batch_items = []
        sample_n = max(0, self._query_examples_count)
        rng = random.Random(42)
        for pattern in merged_patterns:
            top_skills = pattern.effective_skills if pattern.effective_skills else []
            cluster = cluster_by_id.get(pattern.cluster_id)
            examples = (
                [trace.query for trace in cluster.success_traces]
                if cluster
                else [pattern.pattern_description]
            )
            if sample_n == 0:
                sampled = []
            elif len(examples) > sample_n:
                sampled = rng.sample(examples, sample_n)
            else:
                sampled = list(examples)
            item = self._kb.create_item(
                query_pattern=pattern.pattern_description,
                query_examples=sampled,
                skill_ids=top_skills,
                success_count=len(cluster.success_traces) if cluster else 0,
            )
            batch_items.append(item)

        self._kb.add_batch(batch_items)
        created = len(batch_items)
        build_index_elapsed = time.monotonic() - t3
        total_elapsed = time.monotonic() - t0
        LOGGER.info(
            "TraceIndexBuilder: build index done: %d entries in %.2fs",
            created, build_index_elapsed,
        )
        LOGGER.info(
            "TraceIndexBuilder: pipeline total: %.2fs, created %d entries",
            total_elapsed, created,
        )
        return created

    @staticmethod
    def _filter_valid_traces(traces: list[TraceRecord]) -> list[TraceRecord]:
        """Drop traces that are not success or carry no skills.

        Both conditions make a trace useless for positive-pattern distillation:
        failures have no skill signal to reinforce, and skill-less traces
        cannot be partitioned by skill set in clustering.
        """
        valid_traces: list[TraceRecord] = []
        dropped = 0
        for t in traces:
            if not t.success or not t.skills:
                dropped += 1
                continue
            valid_traces.append(t)
        if dropped > 0:
            LOGGER.warning(
                "ExperienceBaseBuilder: dropped %d invalid trace(s) "
                "(success=False or no skills) out of %d provided",
                dropped, len(traces),
            )
        return valid_traces

    def add(self, trace: TraceRecord) -> None:
        """Record a successful query-skill mapping.

        This adds to the pending buffer. Call flush() to cluster and persist.
        """
        if not trace.success or not trace.skills:
            reason = "success=False" if not trace.success else "no skills associated"
            LOGGER.error(
                "ExperienceBaseBuilder: rejecting trace %s: %s",
                getattr(trace, "trace_id", "<unknown>"), reason,
            )
            return
        with self._lock:
            self._pending.append(trace)
            pending_count = len(self._pending)

        # Auto-flush if buffer is large enough (non-blocking)
        if pending_count >= self._flush_threshold:
            with self._lock:
                snapshot = list(self._pending)
                self._pending.clear()
            # Flush outside the lock to avoid blocking add() during LLM calls
            self._flush_snapshot(snapshot)

    def flush(self) -> int:
        """Cluster pending records and merge into the KB.

        Returns the number of new experience items created.
        Blocks until complete — use for graceful shutdown only.
        """
        with self._lock:
            if not self._pending:
                return 0
            pending = list(self._pending)
            self._pending.clear()

        return self._flush_snapshot(pending, force=True)

    def _flush_snapshot(self, pending: list[TraceRecord], force: bool = False) -> int:
        """Flush a snapshot of pending records. Safe to call from any thread."""
        if not pending:
            return 0

        # Step 1: group by skill_ids to reduce noise first
        by_skill = defaultdict(list)
        for r in pending:
            by_skill[tuple(sorted(r.skills))].append(r)

        created = 0
        for skill_key, records in by_skill.items():
            created += self._cluster_and_merge(records, list(skill_key), force=force)

        LOGGER.info(
            "ExperienceBaseBuilder: flushed %d pending records, created %d experience items",
            len(pending), created,
        )
        return created

    def _merge_similar_patterns(
        self,
        patterns: list[DistilledPattern],
        cluster_by_id: dict[int, ClusteredQuery],
    ) -> list[DistilledPattern]:
        if len(patterns) <= 1 or self._pattern_merge_threshold >= 1.0:
            return list(patterns)

        threshold = float(self._pattern_merge_threshold)

        def skill_bucket_key(p: DistilledPattern) -> frozenset:
            return frozenset(p.effective_skills)

        buckets: dict[frozenset, list[int]] = defaultdict(list)
        for i, p in enumerate(patterns):
            buckets[skill_bucket_key(p)].append(i)

        def success_count(i: int) -> int:
            c = cluster_by_id.get(patterns[i].cluster_id)
            return len(c.success_traces) if c else 0

        merged: list[DistilledPattern] = []
        synthetic_counter = 1
        for bucket_indices in buckets.values():
            ordered = sorted(
                bucket_indices,
                key=success_count,
                reverse=True,
            )
            if len(ordered) == 1:
                merged.append(patterns[ordered[0]])
                continue

            descriptions = [patterns[i].pattern_description for i in ordered]
            embeddings = self._embedder.embed_batch(descriptions)
            arr = np.asarray(embeddings, dtype=np.float32)
            norms = np.linalg.norm(arr, axis=1, keepdims=True)
            norms[norms == 0] = 1.0
            unit = arr / norms
            local_sim = unit @ unit.T  # cosine similarity within bucket
            n = len(ordered)
            triu_mask = np.triu(np.ones((n, n), dtype=bool), k=1)

            assigned = np.zeros(n, dtype=bool)
            while True:
                avail_mask = (~assigned)[:, None] & (~assigned)[None, :] & triu_mask
                sim_view = np.where(avail_mask, local_sim, -np.inf)
                if not np.isfinite(sim_view).any() or float(sim_view.max()) < threshold:
                    for pos in np.nonzero(~assigned)[0]:
                        merged.append(patterns[ordered[int(pos)]])
                    break

                flat = int(np.argmax(sim_view))
                i, j = flat // n, flat % n

                group: list[int] = [i, j]
                assigned[i] = True
                assigned[j] = True
                while True:
                    candidates = np.nonzero(~assigned)[0]
                    if len(candidates) == 0:
                        break
                    sub = local_sim[np.ix_(group, candidates)]
                    # SINGLE-LINKAGE: a candidate joins if it is ≥threshold to
                    # ANY member of the group (not ALL members). This lets
                    # transitively-similar patterns merge even when two members
                    # of the group are only loosely similar to each other.
                    best_per_candidate = sub.max(axis=0)        # max sim vs group, per candidate
                    score = np.where(
                        np.any(sub >= threshold, axis=0), best_per_candidate, -np.inf,
                    )
                    if not np.isfinite(score).any():
                        break
                    pick = int(candidates[int(np.argmax(score))])
                    group.append(pick)
                    assigned[pick] = True

                # Re-distill a fresh pattern covering the merged group: feed the
                # group's existing patterns + pooled representative queries to
                # the LLM so the survivor pattern subsumes all sub-abilities
                # rather than just copying the success_count-max one.
                rep = patterns[ordered[group[0]]]
                pooled: list[TraceRecord] = []
                group_patterns: list[str] = []
                for pos in group:
                    pat = patterns[ordered[pos]]
                    group_patterns.append(pat.pattern_description)
                    cluster = cluster_by_id.get(pat.cluster_id)
                    if cluster:
                        pooled.extend(cluster.success_traces)
                merged_pattern = self._redistill_merged(
                    rep, group_patterns, pooled
                )
                synthetic_id = -synthetic_counter
                synthetic_counter += 1
                cluster_by_id[synthetic_id] = ClusteredQuery(
                    cluster_id=synthetic_id,
                    centroid_query=merged_pattern,
                    member_traces=list(pooled),
                    success_traces=list(pooled),
                    failure_traces=[],
                )
                merged.append(DistilledPattern(
                    cluster_id=synthetic_id,
                    effective_skills=rep.effective_skills,
                    pattern_description=merged_pattern,
                ))

        LOGGER.info(
            "TraceIndexBuilder: merged %d patterns (threshold=%.2f, single-linkage greedy, bucketed-by-skill)",
            len(merged), threshold,
        )
        return merged

    def _redistill_merged(
        self,
        rep: DistilledPattern,
        group_patterns: list[str],
        pooled: list[TraceRecord],
    ) -> str:
        """Produce a survivor pattern for a merged group via the LLM.

        Feeds the group's existing clean patterns to TraceDistiller's merge
        routine, which returns one pattern subsuming all sub-abilities. The
        LLM also sees a sample of pooled success queries for real phrasing.
        Falls back to the rep pattern on any failure.
        """
        # Dedup patterns, keep order, drop empties.
        seen: set[str] = set()
        uniq: list[str] = []
        for p in group_patterns:
            if p and p not in seen:
                seen.add(p)
                uniq.append(p)
        skill = (rep.effective_skills or [""])[0] if rep.effective_skills else ""
        distiller = TraceDistiller(
            self._llm,
            self._llm_model,
            skills_info=self._skills_info,
            max_workers=self._max_workers,
            max_success_examples=self._max_success_examples,
        )
        return distiller.distill_merged_pattern(skill, uniq, rep.pattern_description)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _cluster_and_merge(
        self,
        records: list[TraceRecord],
        skill_ids: list[str],
        force: bool = False,
    ) -> int:
        """Embed records, cluster by semantic similarity, name each cluster,
        dedup against existing KB, and write to KB.

        Mirrors the full-build path: adaptive K-Means clustering (via
        ``cluster_traces``) → LLM distillation → single-linkage merge of the
        NEW patterns → batch-dedup against existing items → one ``add_batch``
        write (avoids the O(N) write-amplification of per-item ``add``).

        Returns number of items created.
        """
        if len(records) < self._min_hits and not force:
            # Too few records — put back into pending for later
            self._pending.extend(records)
            return 0

        # Stage 1: cluster via the shared adaptive-K routine (same as full build)
        clusters = cluster_traces(
            records, self._embedder,
            n_clusters=self._skill_cluster_num,
            min_cluster_size=self._min_cluster_size,
        )
        if not clusters:
            if not force:
                self._pending.extend(records)
            return 0

        # Stage 2: distill each new cluster into a pattern
        distiller = TraceDistiller(
            self._llm,
            self._llm_model,
            skills_info=self._skills_info,
            max_workers=self._max_workers,
            max_success_examples=self._max_success_examples,
        )
        distilled = distiller.run(clusters)
        valid_patterns = [p for p in distilled if p.pattern_description]
        if not valid_patterns:
            return 0

        # Stage 3: single-linkage merge among the NEW patterns (same logic as
        # full build) so transitive duplicates within this batch collapse first.
        cluster_by_id = {c.cluster_id: c for c in clusters}
        merged_patterns = self._merge_similar_patterns(valid_patterns, cluster_by_id)

        # Stage 4: dedup each new pattern against existing KB items with the
        # same skill set. Skip if an existing item is already ≥threshold similar
        # (single-linkage already collapsed within-batch duplicates; this only
        # guards cross-batch). Collect survivors for one batched write.
        sample_n = max(0, self._query_examples_count)
        new_items: list[ExperienceItem] = []
        for pat in merged_patterns:
            examples = [t.query for t in cluster_by_id.get(pat.cluster_id, None).success_traces] \
                if cluster_by_id.get(pat.cluster_id) else []
            if self._kb.exist(pat.effective_skills):
                # Same-skill items already exist — skip if any is similar enough
                hits = self._kb.search_by_embedding(pat.pattern_description, threshold=self._pattern_merge_threshold)
                if any(set(it.skill_ids) == set(pat.effective_skills) for _s, it in hits):
                    continue
            new_items.append(self._kb.create_item(
                query_pattern=pat.pattern_description,
                query_examples=examples[:sample_n] if sample_n else [],
                skill_ids=list(pat.effective_skills),
            ))

        # Stage 5: ONE batched write (single index rebuild + single persist)
        if new_items:
            self._kb.add_batch(new_items)
        return len(new_items)

__all__ = ["ExperienceBaseBuilder"]
