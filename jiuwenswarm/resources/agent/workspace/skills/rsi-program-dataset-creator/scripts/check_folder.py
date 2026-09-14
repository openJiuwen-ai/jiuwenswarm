#!/usr/bin/env python3
"""Check that a task folder is complete, before handing it over.

    python scripts/check_folder.py <task-folder>

Checks the shape, the key set against the template, and the numbers' floors.
It does not score anything: it never imports the seed and never runs the
evaluator. Exit status is 0 when the folder is complete, 1 otherwise.
"""

from __future__ import annotations

import json
import logging
import pathlib
import sys


logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)

TOP = {
    "statement",
    "script",
    "hash",
    "entrypoint",
    "evaluator_file",
    "evaluator_command",
    "packages",
    "reply_format",
    "iterations",
    "workers",
    "max_tokens_per_call",
    "options",
    "scorecard",
}
OPTIONS = {
    "c_puct",
    "prior_exponent",
    "repair_attempts",
    "completion_timeout",
    "mode",
    "staleness",
    "async_ratio",
}
SPLIT = {"gateShards", "rolloutShards", "testShards", "seed"}
MANIFEST = {
    "task_id",
    "artifact_path",
    "run_dir",
    "max_iterations",
    "entrypoint",
    "reply_format",
    "language",
    "files_in_seed",
}


def _contains_ignored_part(relative_path: str, ignored: tuple[str, ...]) -> bool:
    path_with_separator = relative_path + "/"
    for part in ignored:
        if part in path_with_separator:
            return True
    return False


def main() -> int:
    if len(sys.argv) != 2:
        logger.error("%s", __doc__)
        return 1

    root = pathlib.Path(sys.argv[1])
    card = json.loads((root / "run" / "scorecard.json").read_text())
    problems: list[str] = []

    seed_root = root / "seed"
    if not seed_root.is_dir():
        problems.append("no seed/ directory")
    manifest_path = root / "task.json"
    if not manifest_path.is_file():
        problems.append("no task.json")
    else:
        manifest = json.loads(manifest_path.read_text())
        for key in sorted(MANIFEST - manifest.keys()):
            problems.append(f"missing task.json key {key!r}")
        for key in sorted(manifest.keys() - MANIFEST):
            problems.append(f"unknown task.json key {key!r}")
        for key, card_key in (
            ("max_iterations", "iterations"),
            ("entrypoint", "entrypoint"),
            ("reply_format", "reply_format"),
        ):
            if key in manifest and manifest[key] != card.get(card_key):
                problems.append(
                    f"task.json {key} {manifest[key]!r} != scorecard "
                    f"{card_key} {card.get(card_key)!r}"
                )

        ignored = ("__pycache__/", ".git/", "node_modules/", ".venv/")
        listing: list[str] = []
        for path in seed_root.rglob("*"):
            if not path.is_file():
                continue
            relative = path.relative_to(seed_root).as_posix()
            if relative.endswith(".pyc"):
                continue
            if _contains_ignored_part(relative, ignored):
                continue
            listing.append(relative)
        listing.sort()
        if manifest.get("files_in_seed") != listing:
            problems.append(
                f"task.json files_in_seed != seed/ listing {listing}"
            )
        if manifest.get("artifact_path") != f"seed/{card.get('entrypoint')}":
            problems.append(
                "task.json artifact_path should be seed/<entrypoint>"
            )
        if manifest.get("run_dir") != "run":
            problems.append("task.json run_dir should be 'run'")

    entrypoint = card.get("entrypoint", "")
    if not (seed_root / entrypoint).is_file():
        problems.append(f"seed/ does not contain entrypoint {entrypoint!r}")
    for key in sorted(TOP - card.keys()):
        problems.append(f"missing top-level key {key!r}")
    for key in sorted(card.keys() - TOP):
        problems.append(f"unknown top-level key {key!r}")
    card_options = card.get("options", {})
    for key in sorted(OPTIONS - card_options.keys()):
        problems.append(f"missing options key {key!r}")

    if not str(card.get("script", "")).strip():
        problems.append("script is empty")
    criteria = (card.get("scorecard") or {}).get("criteria") or []
    if not criteria:
        problems.append("scorecard.criteria is empty")
    else:
        measure = criteria[0].get("measure") or {}
        if measure.get("kind") != "custom_script":
            problems.append("measure.kind must be custom_script")
        if not isinstance(measure.get("timeoutSeconds"), (int, float)):
            problems.append("measure.timeoutSeconds missing")
        split = measure.get("split") or {}
        for key in sorted(SPLIT - split.keys()):
            problems.append(f"missing split key {key!r}")
        if int(split.get("gateShards") or 0) < 4:
            problems.append("gateShards must be >= 4")
        if int(split.get("rolloutShards") or 0) > int(
            split.get("gateShards") or 0
        ):
            problems.append("rolloutShards > gateShards")

    workers = int(card.get("workers") or 0)
    iterations = int(card.get("iterations") or 0)
    if iterations < 4 * workers:
        problems.append(
            f"iterations {iterations} < 4 x workers {workers}"
        )
    mode = (card.get("options") or {}).get("mode")
    if workers > 1 and mode == "serial":
        problems.append(
            "mode serial with workers > 1: the run warns and then serialises, "
            "wasting the workers"
        )
    if workers == 1 and mode != "serial":
        problems.append("mode should be serial with one worker")
    if int(card.get("max_tokens_per_call") or 0) < 32000:
        problems.append(
            "max_tokens_per_call below 32000: a thinking model returns nothing"
        )
    if not isinstance(card.get("evaluator_command"), list):
        problems.append("evaluator_command must be a list")
    if (
        not str(card.get("evaluator_file", "")).endswith(".py")
        and not card.get("evaluator_command")
    ):
        problems.append("non-Python evaluator_file needs an evaluator_command")
    mutation_prompt = root / "run" / "prompts" / "mutation.md"
    if mutation_prompt.is_file() and "${reply_format}" not in mutation_prompt.read_text():
        problems.append("prompts/mutation.md lacks ${reply_format}")
    source = root / card.get("evaluator_file", "evaluate.py")
    if source.is_file() and source.read_text() != card.get("script"):
        problems.append(
            f"{source.name} differs from the card's script: re-assemble the card"
        )

    # Runtime behaviour this script cannot test, only smell: the probe refuses a
    # folder whose `error` reports an exception without saying where it happened.
    warnings: list[str] = []
    script = str(card.get("script", ""))
    if (
        str(card.get("evaluator_file", "")).endswith(".py")
        and "except" in script
        and "traceback" not in script
    ):
        warnings.append(
            "script catches exceptions but never imports traceback: if the damaged copy "
            "the probe runs raises, `error` will say what and not where, and the folder is "
            "refused (PROBE_REFUSED). Write a trimmed traceback.format_exc()."
        )

    message = "\n".join(problems) or f"ok: {root} is complete"
    logger.info("%s", message)
    for warning in warnings:
        logger.warning("warning: %s", warning)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
