#!/usr/bin/env python3
"""Check that a task folder is complete, before handing it over.

    python scripts/check_folder.py <task-folder>

Checks the shape, the key set against the template, and the numbers' floors.
It does not score anything: it never imports the seed and never runs the
evaluator. Exit status is 0 when the folder is complete, 1 otherwise.
"""
import json, pathlib, sys
if len(sys.argv) != 2:
    sys.exit(__doc__)
root = pathlib.Path(sys.argv[1])
card = json.loads((root / "run" / "scorecard.json").read_text())
TOP = {"statement", "script", "hash", "entrypoint", "evaluator_file", "evaluator_command",
       "packages", "reply_format", "iterations", "workers", "max_tokens_per_call", "options",
       "scorecard"}
OPTIONS = {"c_puct", "prior_exponent", "repair_attempts", "completion_timeout", "mode",
           "staleness", "async_ratio"}
SPLIT = {"gateShards", "rolloutShards", "testShards", "seed"}
MANIFEST = {"task_id", "artifact_path", "run_dir", "max_iterations", "entrypoint", "reply_format",
            "language", "files_in_seed"}
problems = []
if not (root / "seed").is_dir(): problems.append("no seed/ directory")
manifest_path = root / "task.json"
if not manifest_path.is_file(): problems.append("no task.json")
else:
    manifest = json.loads(manifest_path.read_text())
    problems += [f"missing task.json key {k!r}" for k in sorted(MANIFEST - manifest.keys())]
    problems += [f"unknown task.json key {k!r}" for k in sorted(manifest.keys() - MANIFEST)]
    for key, card_key in (("max_iterations", "iterations"), ("entrypoint", "entrypoint"), ("reply_format", "reply_format")):
        if key in manifest and manifest[key] != card.get(card_key):
            problems.append(f"task.json {key} {manifest[key]!r} != scorecard {card_key} {card.get(card_key)!r}")
    IGNORED = ("__pycache__/", ".git/", "node_modules/", ".venv/")
    listing = sorted(rel for rel in (p.relative_to(root / "seed").as_posix()
                                     for p in (root / "seed").rglob("*") if p.is_file())
                     if not rel.endswith(".pyc") and not any(part in rel + "/" for part in IGNORED))
    if manifest.get("files_in_seed") != listing: problems.append(f"task.json files_in_seed != seed/ listing {listing}")
    if manifest.get("artifact_path") != f"seed/{card.get('entrypoint')}": problems.append("task.json artifact_path should be seed/<entrypoint>")
    if manifest.get("run_dir") != "run": problems.append("task.json run_dir should be 'run'")
if not (root / "seed" / card.get("entrypoint", "")).is_file():
    problems.append(f"seed/ does not contain entrypoint {card.get('entrypoint')!r}")
problems += [f"missing top-level key {k!r}" for k in sorted(TOP - card.keys())]
problems += [f"unknown top-level key {k!r}" for k in sorted(card.keys() - TOP)]
problems += [f"missing options key {k!r}" for k in sorted(OPTIONS - card.get("options", {}).keys())]
if not str(card.get("script", "")).strip(): problems.append("script is empty")
crit = (card.get("scorecard") or {}).get("criteria") or []
if not crit: problems.append("scorecard.criteria is empty")
else:
    m = crit[0].get("measure") or {}
    if m.get("kind") != "custom_script": problems.append("measure.kind must be custom_script")
    if not isinstance(m.get("timeoutSeconds"), (int, float)): problems.append("measure.timeoutSeconds missing")
    split = m.get("split") or {}
    problems += [f"missing split key {k!r}" for k in sorted(SPLIT - split.keys())]
    if int(split.get("gateShards") or 0) < 4: problems.append("gateShards must be >= 4")
    if int(split.get("rolloutShards") or 0) > int(split.get("gateShards") or 0): problems.append("rolloutShards > gateShards")
w, it = int(card.get("workers") or 0), int(card.get("iterations") or 0)
if it < 4 * w: problems.append(f"iterations {it} < 4 x workers {w}")
mode = (card.get("options") or {}).get("mode")
if w > 1 and mode == "serial": problems.append("mode serial with workers > 1: the run warns and then serialises, wasting the workers")
if w == 1 and mode != "serial": problems.append("mode should be serial with one worker")
if int(card.get("max_tokens_per_call") or 0) < 32000: problems.append("max_tokens_per_call below 32000: a thinking model returns nothing")
if not isinstance(card.get("evaluator_command"), list): problems.append("evaluator_command must be a list")
if not str(card.get("evaluator_file", "")).endswith(".py") and not card.get("evaluator_command"):
    problems.append("non-Python evaluator_file needs an evaluator_command")
mut = root / "run" / "prompts" / "mutation.md"
if mut.is_file() and "${reply_format}" not in mut.read_text(): problems.append("prompts/mutation.md lacks ${reply_format}")
source = root / card.get("evaluator_file", "evaluate.py")
if source.is_file() and source.read_text() != card.get("script"):
    problems.append(f"{source.name} differs from the card's script: re-assemble the card")
print("\n".join(problems) or f"ok: {root} is complete")
sys.exit(1 if problems else 0)
