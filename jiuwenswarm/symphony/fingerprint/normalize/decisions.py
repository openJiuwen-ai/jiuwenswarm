"""Normalization decision recording helpers."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from jiuwenswarm.symphony.fingerprint.models import NormalizationDecision


class NormalizationDecisionRecorder:
    """Append structured normalization decisions to a trace list."""

    @staticmethod
    def record(
        decisions: List[NormalizationDecision],
        *,
        skill_id: str,
        path: Optional[str],
        direction: str,
        field: str,
        raw_value: str,
        token: str,
        normalized_value: str,
        method: str,
        vocab: str,
        vocab_version: str,
        confidence: float,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        decisions.append(
            NormalizationDecision(
                skill_id=skill_id,
                path=path,
                direction=direction,
                field=field,
                raw_value=raw_value,
                token=token,
                normalized_value=normalized_value,
                method=method,
                vocab=vocab,
                vocab_version=vocab_version,
                confidence=confidence,
                details=details or {},
            )
        )
