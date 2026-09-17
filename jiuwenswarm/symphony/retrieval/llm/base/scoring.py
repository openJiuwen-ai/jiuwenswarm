from __future__ import annotations

import math
from dataclasses import dataclass
from time import perf_counter
from typing import Mapping, Sequence

from .errors import CandidateEncodingError, CandidateScoringError
from .types import CandidateScore, CandidateScoringResult


@dataclass(frozen=True)
class CandidateTokenization:
    candidate_codes: tuple[str, ...]
    token_to_code: Mapping[int, str]
    candidate_token_ids: tuple[int, ...]
    encode_ms: float


def prepare_candidate_token_ids(
    *,
    candidate_codes: Sequence[str],
    encoded_codes: Mapping[str, int | None],
    require_single_token_codes: bool,
) -> CandidateTokenization:
    started = perf_counter()
    resolved_codes = tuple(str(code) for code in candidate_codes)
    missing = [code for code in resolved_codes if encoded_codes.get(code) is None]
    if require_single_token_codes and missing:
        raise CandidateEncodingError(
            f"visible candidate ids are not single-token under the scoring tokenizer: {missing}"
        )

    token_to_code: dict[int, str] = {}
    token_collisions: dict[int, list[str]] = {}
    for code in resolved_codes:
        token_id = encoded_codes.get(code)
        if token_id is None:
            continue
        resolved_token_id = int(token_id)
        previous = token_to_code.get(resolved_token_id)
        if previous is not None and previous != code:
            token_collisions.setdefault(resolved_token_id, [previous]).append(code)
            continue
        token_to_code[resolved_token_id] = code
    if token_collisions:
        collision_details = ", ".join(
            f"{token_id}: {sorted(set(codes))}" for token_id, codes in sorted(token_collisions.items())
        )
        raise CandidateEncodingError(f"visible candidate ids collide under the scoring tokenizer: {collision_details}")

    return CandidateTokenization(
        candidate_codes=resolved_codes,
        token_to_code=token_to_code,
        candidate_token_ids=tuple(sorted(token_to_code.keys())),
        encode_ms=round((perf_counter() - started) * 1000.0, 3),
    )


def build_candidate_scoring_result(
    *,
    tokenization: CandidateTokenization,
    scored_pairs: Sequence[tuple[int, float]],
    code_to_canonical_id: Mapping[str, str],
    latency_breakdown: Mapping[str, float],
) -> CandidateScoringResult:
    if not tokenization.token_to_code:
        return CandidateScoringResult(
            scores=(),
            candidate_codes=tokenization.candidate_codes,
            candidate_token_ids=(),
            latency_breakdown=dict(latency_breakdown),
        )
    if not scored_pairs:
        return CandidateScoringResult(
            scores=(),
            candidate_codes=tokenization.candidate_codes,
            candidate_token_ids=tokenization.candidate_token_ids,
            latency_breakdown=dict(latency_breakdown),
        )

    returned_token_ids: list[int] = []
    seen_token_ids: set[int] = set()
    for token_id, _logit in scored_pairs:
        resolved_token_id = int(token_id)
        if resolved_token_id not in tokenization.token_to_code:
            raise CandidateScoringError(f"scoring backend returned an unknown candidate token id: {resolved_token_id}")
        if resolved_token_id in seen_token_ids:
            raise CandidateScoringError(f"scoring backend returned duplicate candidate token id: {resolved_token_id}")
        seen_token_ids.add(resolved_token_id)
        returned_token_ids.append(resolved_token_id)
    missing_token_ids = sorted(set(tokenization.token_to_code.keys()) - set(returned_token_ids))
    if missing_token_ids:
        raise CandidateScoringError(
            "scoring backend did not return scores for every visible candidate token: " f"{missing_token_ids}"
        )

    normalize_started = perf_counter()
    logits = [float(logit) for _token_id, logit in scored_pairs]
    max_logit = max(logits)
    exp_values = [math.exp(logit - max_logit) for logit in logits]
    denom = sum(exp_values) or 1.0
    scores: list[CandidateScore] = []
    for index, ((token_id, logit), exp_value) in enumerate(zip(scored_pairs, exp_values), start=1):
        code = tokenization.token_to_code[int(token_id)]
        scores.append(
            CandidateScore(
                code=code,
                canonical_id=str(code_to_canonical_id.get(code) or code),
                token_id=int(token_id),
                logit=float(logit),
                probability=float(exp_value / denom),
                rank=index,
            )
        )
    merged_latency = dict(latency_breakdown)
    merged_latency["normalize_ms"] = round((perf_counter() - normalize_started) * 1000.0, 3)
    return CandidateScoringResult(
        scores=tuple(scores),
        candidate_codes=tokenization.candidate_codes,
        candidate_token_ids=tokenization.candidate_token_ids,
        latency_breakdown=merged_latency,
    )


__all__ = [
    "CandidateTokenization",
    "build_candidate_scoring_result",
    "prepare_candidate_token_ids",
]
