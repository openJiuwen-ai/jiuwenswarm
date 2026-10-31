"""FTS5 rank conversion used by memory retrieval."""

from jiuwenswarm.agents.harness.common.memory.internal import bm25_rank_to_score


def test_bm25_score_preserves_fts5_match_order() -> None:
    assert bm25_rank_to_score(-2.0) > bm25_rank_to_score(-0.2)


def test_weak_bm25_match_stays_below_a_minimum_score() -> None:
    assert bm25_rank_to_score(-0.0001) < 0.01
    assert bm25_rank_to_score(0.0) == 0.0
