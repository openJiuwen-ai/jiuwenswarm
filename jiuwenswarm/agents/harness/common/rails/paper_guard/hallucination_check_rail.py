# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Hallucination-check rail for automated research paper generation.

Quantitative claims (percentages, metric values) in a member's model output
are validated against a ground-truth experiment results file. Numbers that
cannot be traced to the ground truth cause an injected warning note, so the
writing/proofreading stages know to remove or correct them.

Config (``paper_guard`` section in ``config.yaml``)::

    paper_guard:
      enabled: true
      results_path: null        # optional override; default resolves below
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.rails.base import DeepAgentRail

logger = logging.getLogger(__name__)

_NUM_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\d+(?:\.\d+)?\s*%"),
    re.compile(
        r"(?:F1|Accuracy|Acc|Precision|Recall|BLEU|mAP|loss)"
        # [a-z_]* covers JSON-key variants such as accuracy_recent.
        r"[a-z_]*[\s=:\"']+\d+(?:\.\d+)?",
        re.IGNORECASE,
    ),
)


def _default_results_path() -> Path:
    override = os.environ.get("PAPER_GUARD_RESULTS_PATH")
    if override:
        return Path(override)
    return (
        Path.home()
        / ".jiuwenswarm"
        / "agent"
        / "workspace"
        / "output"
        / "results.json"
    )


def _extract_numbers(text: str) -> set[str]:
    found: set[str] = set()
    if not text:
        return found
    for pat in _NUM_PATTERNS:
        found.update(m.group(0).replace(" ", "") for m in pat.finditer(text))
    return found


def _num_value(token: str) -> float | None:
    # Take the trailing number so metric names with embedded digits
    # (F1, mAP) are not mistaken for the claim value.
    m = re.search(r"(\d+(?:\.\d+)?)\s*%?$", token)
    return float(m.group(1)) if m else None


def _unsupported_claims(claimed: set[str], truth: set[str]) -> set[str]:
    """Claims lacking ground-truth support.

    Percent-vs-fraction is normalized (92.3% matches 0.923); otherwise an
    exact numeric comparison with float tolerance decides.
    """
    truth_vals: list[float] = []
    for tok in truth:
        v = _num_value(tok)
        if v is None:
            continue
        truth_vals.append(v)
        if tok.endswith("%"):
            truth_vals.append(v / 100.0)
    bad: set[str] = set()
    for claim in claimed:
        v = _num_value(claim)
        if v is None:
            continue
        candidates = [v / 100.0] if claim.endswith("%") else [v]
        if not any(
            abs(c - t) <= 1e-9 * max(1.0, abs(t))
            for c in candidates
            for t in truth_vals
        ):
            bad.add(claim)
    return bad


class HallucinationCheckRail(DeepAgentRail):
    """Warn when quantitative claims lack ground-truth support."""

    priority: int = 80

    def __init__(
        self,
        results_path: str | None = None,
        enabled: bool = True,
    ) -> None:
        super().__init__()
        self._results_path = Path(results_path) if results_path else _default_results_path()
        self._enabled = enabled

    def _ground_truth(self) -> set[str]:
        try:
            raw = self._results_path.read_text(encoding="utf-8")
            return _extract_numbers(json.dumps(json.loads(raw), ensure_ascii=False))
        except (OSError, json.JSONDecodeError, ValueError):
            return set()

    async def after_model_call(self, ctx: AgentCallbackContext) -> None:
        if not self._enabled:
            return
        try:
            output = _output_text(ctx)
            claimed = _extract_numbers(output)
            if not claimed:
                return
            truth = self._ground_truth()
            if not truth:
                _inject(ctx, "PAPER_GUARD[hallucination]: no ground-truth results file, "
                             "quantitative claims left unverified.")
                return
            unsupported = _unsupported_claims(claimed, truth)
            if unsupported:
                _inject(
                    ctx,
                    "PAPER_GUARD[hallucination]: quantitative claims not found in "
                    "ground-truth results; remove or correct: "
                    + "; ".join(sorted(unsupported)[:10]),
                )
                logger.warning(
                    "[PaperGuard] %d unsupported quantitative claims", len(unsupported),
                )
        except Exception as exc:
            logger.warning("[PaperGuard] hallucination check skipped: %s", exc)

    async def before_tool_call(self, ctx: AgentCallbackContext) -> None:
        if not self._enabled:
            return
        try:
            inputs = getattr(ctx, "inputs", None)
            tool_name = str(getattr(inputs, "tool_name", "") or "")
            tool_args = getattr(inputs, "tool_args", None) or {}
            if not isinstance(tool_args, dict) or "write" not in tool_name.lower():
                return
            content = str(tool_args.get("content") or tool_args.get("text") or "")
            claimed = _extract_numbers(content)
            if not claimed:
                return
            truth = self._ground_truth()
            if not truth:
                return
            unsupported = _unsupported_claims(claimed, truth)
            if unsupported:
                _inject(
                    ctx,
                    "PAPER_GUARD[hallucination]: file write contains untraceable "
                    "numbers: " + "; ".join(sorted(unsupported)[:10]),
                )
        except Exception as exc:
            logger.warning("[PaperGuard] pre-write check skipped: %s", exc)


def _output_text(ctx: AgentCallbackContext) -> str:
    for attr in ("output", "response", "result", "text"):
        val = getattr(ctx, attr, None)
        if isinstance(val, str) and val:
            return val
        for sub in ("message", "text", "content"):
            sub_val = getattr(val, sub, None)
            if isinstance(sub_val, str) and sub_val:
                return sub_val
    return ""


def _inject(ctx: AgentCallbackContext, note: str) -> None:
    for attr in ("notes", "warnings", "system_notes"):
        lst = getattr(ctx, attr, None)
        if isinstance(lst, list):
            lst.append(note)
            logger.info("%s", note)
            return
    if hasattr(ctx, "__dict__"):
        for attr in vars(ctx):
            if isinstance(getattr(ctx, attr, None), list):
                getattr(ctx, attr).append(note)
                break
    logger.info("%s", note)