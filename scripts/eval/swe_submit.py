# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Local copy of harness artifacts. Not a leaderboard submission.

Official entry is ``swebench submit package``, which re-grades each instance
from ``test_output.txt``. This helper only copies ``report.json`` so you can
inspect a run. ``run_evaluate.py`` writes it under ``unofficial-submission/``
and only when you pass ``--unofficial-package``.
"""

from __future__ import annotations

import gzip
import json
import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from eval_env import JIUWEN_ROOT
from swe_dataset import official_split_n

MAX_ARTIFACT_BYTES = 50 * 1024 * 1024


@dataclass
class PackageResult:
    split: str
    submission_id: str
    out_dir: Path
    resolved: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    no_generation: list[str] = field(default_factory=list)
    no_logs: list[str] = field(default_factory=list)


def submission_id(model_name: str, when: datetime | None = None) -> str:
    stamp = (when or datetime.now()).strftime("%Y%m%d")
    slug = (model_name or "unknown").replace("/", "__").replace(" ", "-")
    return f"{stamp}_{slug}"


def load_predictions(path: Path) -> dict[str, dict[str, Any]]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return {str(row["instance_id"]): row for row in rows if row.get("instance_id")}


def discover_eval_logs(run_id: str, model: str) -> Path | None:
    """Find ``logs/run_evaluation/<run_id>/<model>/`` from cwd or the repo root."""
    model_dir = (model or "unknown").replace("/", "__")
    for root in (Path.cwd(), JIUWEN_ROOT):
        for mid in ("run_evaluation", "evaluation"):
            candidate = root / "logs" / mid / run_id / model_dir
            if candidate.is_dir():
                return candidate
    return None


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=4, ensure_ascii=False) + "\n", encoding="utf-8")


def _copy_test_output(src: Path, dest_dir: Path) -> None:
    dest = dest_dir / "test_output.txt"
    if src.stat().st_size > MAX_ARTIFACT_BYTES:
        gz = dest_dir / "test_output.txt.gz"
        with src.open("rb") as fin, gzip.open(gz, "wb") as fout:
            shutil.copyfileobj(fin, fout)
        return
    shutil.copy2(src, dest)


def _report_resolved(report_path: Path, iid: str) -> bool | None:
    try:
        data = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    body = data.get(iid) if isinstance(data, dict) else None
    if not isinstance(body, dict):
        return None
    if "resolved" not in body:
        return None
    return bool(body["resolved"])


def _copy_trajs(raw_dir: Path, dest: Path) -> int:
    dest.mkdir(parents=True, exist_ok=True)
    n = 0
    for path in sorted(raw_dir.glob("*.traj.json")):
        shutil.copy2(path, dest / path.name)
        n += 1
        trace = raw_dir / path.name.replace(".traj.json", ".trace.json")
        if trace.is_file():
            shutil.copy2(trace, dest / trace.name)
    return n


def _metadata(model: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    extra = extra or {}
    return {
        "info": {
            "name": extra.get("name") or os.getenv("SWE_SUBMIT_NAME") or "jiuwenswarm-code-graph",
            "site": extra.get("site") or os.getenv("SWE_SUBMIT_SITE") or "TODO url describing the system",
            "report": extra.get("report") or "TODO arXiv / technical report / blog post",
            "authors": extra.get("authors") or os.getenv("SWE_SUBMIT_AUTHORS") or "TODO",
            "logo": "TODO url, and commit the image as logo.png in this folder",
        },
        "tags": {
            "checked": False,
            "model": [model],
            "org": extra.get("org") or os.getenv("SWE_SUBMIT_ORG") or "openJiuwen",
            "os_model": False,
            "os_system": extra.get("os_system", False),
            "system": {"attempts": 1},
            "agent": extra.get("agent") or "jiuwenswarm",
            "agent_org": extra.get("agent_org") or "openJiuwen",
            "model_display": extra.get("model_display") or model,
            "model_org": extra.get("model_org") or "TODO who publishes the model",
        },
    }


def package_swe_submission(
    *,
    predictions: Path,
    out_dir: Path,
    split: str = "verified",
    run_id: str = "swe",
    model: str | None = None,
    trajs: Path | None = None,
    logs_root: Path | None = None,
    sub_id: str | None = None,
    metadata_extra: dict[str, Any] | None = None,
) -> PackageResult:
    preds = load_predictions(predictions)
    if not preds:
        raise SystemExit(f"no predictions in {predictions}")
    model = model or next(iter(preds.values())).get("model_name_or_path") or "unknown"
    logs_root = logs_root or discover_eval_logs(run_id, model)
    result = PackageResult(
        split=split,
        submission_id=sub_id or submission_id(str(model)),
        out_dir=out_dir.expanduser().resolve(),
    )
    repo_dir = result.out_dir / "submission-repo"
    entry = result.out_dir / "entry"
    (repo_dir / "logs").mkdir(parents=True, exist_ok=True)
    (entry / "results").mkdir(parents=True, exist_ok=True)

    by_repo: dict[str, dict[str, int]] = {}
    for iid, pred in sorted(preds.items()):
        repo = str(pred.get("repo") or iid.split("__", 1)[0] if "__" in iid else "?")
        bucket = by_repo.setdefault(repo, {"resolved": 0, "total": 0})
        bucket["total"] += 1
        dest = repo_dir / "logs" / iid
        dest.mkdir(parents=True, exist_ok=True)
        patch_text = str(pred.get("model_patch") or "")
        inst_logs = (logs_root / iid) if logs_root else None
        harness_patch = inst_logs / "patch.diff" if inst_logs else None
        report_path = inst_logs / "report.json" if inst_logs else None
        test_out = inst_logs / "test_output.txt" if inst_logs else None
        if harness_patch and harness_patch.is_file():
            shutil.copy2(harness_patch, dest / "patch.diff")
        elif patch_text.strip():
            (dest / "patch.diff").write_text(patch_text, encoding="utf-8")
        else:
            result.no_generation.append(iid)
            continue
        if report_path and report_path.is_file():
            shutil.copy2(report_path, dest / "report.json")
        if test_out and test_out.is_file():
            _copy_test_output(test_out, dest)
        else:
            result.no_logs.append(iid)
        verdict = _report_resolved(dest / "report.json", iid) if (dest / "report.json").is_file() else None
        if verdict is True:
            result.resolved.append(iid)
            bucket["resolved"] += 1
        elif verdict is False:
            result.unresolved.append(iid)

    (repo_dir / "all_preds.jsonl").write_text(
        "".join(json.dumps(preds[iid], ensure_ascii=False) + "\n" for iid in sorted(preds)),
        encoding="utf-8",
    )
    traj_src = trajs or predictions.parent
    n_traj = _copy_trajs(traj_src, repo_dir / "trajs") if traj_src.is_dir() else 0

    _write_json(
        entry / "results" / "results.json",
        {
            "no_generation": sorted(result.no_generation),
            "no_logs": sorted(result.no_logs),
            "resolved": sorted(result.resolved),
            "unresolved": sorted(result.unresolved),
        },
    )
    _write_json(entry / "results" / "resolved_by_repo.json", dict(sorted(by_repo.items())))
    _write_json(entry / "results" / "resolved_by_time.json", {})
    try:
        import yaml
    except ImportError:
        yaml = None
    meta = _metadata(str(model), metadata_extra)
    header = (
        f"# Generated for {result.submission_id} ({result.split})\n"
        f"# resolved={len(result.resolved)} submitted={len(preds)} trajs={n_traj}\n"
        "# Replace remaining TODO before the experiments PR.\n"
    )
    if yaml is not None:
        (entry / "metadata.yaml").write_text(
            header + yaml.safe_dump(meta, sort_keys=False), encoding="utf-8"
        )
    else:
        (entry / "metadata.yaml").write_text(header + json.dumps(meta, indent=2), encoding="utf-8")
    n_sub = len(preds)
    official = official_split_n(result.split)
    denom = official or n_sub
    pct = 100.0 * len(result.resolved) / denom if denom else 0.0
    (entry / "README.md").write_text(
        f"# {result.submission_id}\n\n"
        f"Unofficial helper: resolved is copied from harness `report.json`, "
        f"not re-graded from `test_output.txt`. Not a ready experiments PR.\n\n"
        f"| | |\n| --- | --- |\n"
        f"| Split | `{result.split}` |\n"
        f"| Model | `{model}` |\n"
        f"| Resolved (official denom) | {len(result.resolved)} / {denom} ({pct:.2f}%) |\n"
        f"| Submitted rows | {n_sub} |\n"
        f"| No generation | {len(result.no_generation)} |\n"
        f"| Missing logs | {len(result.no_logs)} |\n",
        encoding="utf-8",
    )
    return result
