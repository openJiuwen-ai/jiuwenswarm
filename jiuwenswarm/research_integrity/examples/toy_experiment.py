# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Toy experiment: a real, deterministic evaluation used by integrity tests.

Evaluates a fixed rule-based predictor over a fixed 20-sample dataset and
writes ``results.json`` (accuracy = 15/20 = 0.75, computed — never hardcoded).
Used to prove the full provenance chain end to end:

    0.75 -> MetricRecord -> ArtifactRecord -> ExperimentRun -> ExperimentSpec
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def _build_dataset() -> list[tuple[int, int]]:
    """Fixed dataset: (feature, label) pairs, labels = feature parity + noise."""
    # 20 samples; the first 15 labels follow the parity rule (even->0,
    # odd->1), the last 5 deliberately violate it, so a parity predictor
    # scores exactly 15/20 = 0.75.
    labels = [0, 1, 0, 1, 0, 1, 0, 1, 0, 1, 0, 1, 0, 1, 0, 0, 1, 0, 1, 0]
    return [(index, label) for index, label in enumerate(labels)]


def evaluate() -> float:
    """Run the parity predictor over the fixed dataset; return accuracy."""
    dataset = _build_dataset()
    correct = sum(1 for feature, label in dataset if label == feature % 2)
    return correct / len(dataset)


def main() -> int:
    parser = argparse.ArgumentParser(description="Toy experiment for integrity tests")
    parser.add_argument("--seed", type=int, default=None, help="recorded seed")
    parser.add_argument(
        "--output",
        default="results.json",
        help="output artifact path (default: results.json)",
    )
    parser.add_argument(
        "--fail", action="store_true", help="exit non-zero (failure-path tests)"
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    accuracy = evaluate()
    payload = {
        "accuracy": accuracy,
        "n_samples": len(_build_dataset()),
        "seed": args.seed,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    logger.info("accuracy=%.4f -> %s", accuracy, output)

    if args.fail:
        logger.error("simulating failure")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
