# -*- coding: utf-8 -*-
"""main_flow.py — paper-gen 端到端 swarmflow workflow: conception → planning → experiment → writing。

4 stage sequential await + bounded experiment-to-planning REPLAN + run_summary 落盘。

调用方式：
    uv run python -m jiuwenswarm.resources.agent.workspace.skills.paper_gen.scripts.main \\
        --input-dir mock/run1 --output-dir out/run1 [--dry-run]

或嵌套（最多 1 层）：
    from swarmflow import workflow
    await workflow("scripts/main_flow.py", args={"input_dir": ..., "output_dir": ...})
"""
from __future__ import annotations

import json
import hashlib
import logging
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any

# 模块级 logger（main.py 已 setup_logging；这里只取实例）
log = logging.getLogger("paper_gen.main_flow")

# swarmflow facade 不可顶层 import（仅 run_workflow context 期间注册到 sys.modules）。
# 单独跑 main.py 时用 fallback print；将来被嵌套 run_workflow 加载时仍可用真 phase() / log()。
try:
    from swarmflow import log as _swarmflow_log_facade, phase as _swarmflow_phase_facade
    _HAS_SWARMFLOW = True
except ImportError:  # pragma: no cover
    _HAS_SWARMFLOW = False
    _swarmflow_log_facade = None
    _swarmflow_phase_facade = None


def _swarmflow_log(msg: Any) -> None:
    """Report progress when an engine provider exists; stay callable in tests/CLI."""
    if _swarmflow_log_facade is not None:
        try:
            _swarmflow_log_facade(msg)
            return
        except RuntimeError as exc:
            if "No SwarmFlow provider is installed" not in str(exc):
                raise
    log.info(f"[swarmflow.log] {msg}")


def _swarmflow_phase(title: str) -> None:
    """Mark a workflow phase without requiring a provider for direct invocation."""
    if _swarmflow_phase_facade is not None:
        try:
            _swarmflow_phase_facade(title)
            return
        except RuntimeError as exc:
            if "No SwarmFlow provider is installed" not in str(exc):
                raise
    log.info(f"[swarmflow.phase] {title}")


META = {
    "name": "paper-gen",
    "description": "CCF BDCI 论文生成端到端编排: conception → planning → experiment → writing（产出 paper.pdf）",
    "whenToUse": "用户给研究方向后，端到端产出可投递的 ICLR 2024 格式 paper.pdf",
    "phases": [
        {"title": "conception",  "detail": "文献检索 + 构思生成"},
        {"title": "planning",    "detail": "方法设计 + 实验规划 + 门禁校验 + 写产物"},
        {"title": "experiment",  "detail": "数据准备 + 实现 + 执行 + 分析"},
        {"title": "writing",     "detail": "章节合同、专项审查、修订与 ICLR PDF 门禁 → paper.pdf"},
    ],
}

# REPLAN 路由表。experiment 的结构化反馈回传 planning；其他 stage 不猜测重试。
_REPLAN_ROUTE = {
    "conception":  "abort",          # 上游失败不能重跑
    "planning":    "replan_planning",
    "experiment":  "replan_experiment",
}


async def run(args: dict) -> dict:
    """4 stage sequential orchestration + 增量 resume + REPLAN 反馈聚合。

    Args:
        args: 含 input_dir + output_dir；可选 force（True=忽略缓存全跑）。

    Returns:
        run_summary dict（同时落盘到 output_dir/run_summary.json）。
    """
    args = args if isinstance(args, dict) else {}
    input_dir = Path(args["input_dir"])
    output_dir = Path(args["output_dir"])
    output_dir.mkdir(parents=True, exist_ok=True)
    resumable_stage_dirs = _load_resumable_stage_dirs(output_dir)
    force = bool(args.get("force", False))
    max_replan_rounds = max(0, int(
        args.get("max_replan_rounds", args.get("max_experiment_replans", 2))
    ))
    # Dataset bytes are reusable only through the experiment module's
    # fingerprint-verified cache. Failed/superseded attempt directories are
    # removed after their compact status/cost record has been persisted.
    dataset_cache = Path(
        args.get("dataset_cache_dir")
        or (output_dir.parent / ".paper-gen-cache" / "datasets")
    ).resolve()
    dataset_cache.mkdir(parents=True, exist_ok=True)
    os.environ["JIUWENSWARM_DATASET_CACHE"] = str(dataset_cache)

    started_at = time.time()
    log.info(f"run() 启动: input_dir={input_dir}, output_dir={output_dir}, force={force}")
    summary: dict[str, Any] = {
        "input_dir": str(input_dir),
        "output_dir": str(output_dir),
        "force": force,
        "stages": {},
        "started_at": _iso8601(started_at),
        "invocation_id": f"paper-gen-{int(started_at * 1000)}",
        "status": "running",
        "telemetry_path": str(output_dir / "stage_metrics.json"),
        "token_usage_path": str(output_dir / "token_usage.json"),
        "token_usage_report_path": str(output_dir / "token_usage_report.md"),
        "resource_usage_report_path": str(output_dir / "resource_usage_report.md"),
        "resource_attempts_path": str(output_dir / "resource_attempts.json"),
        "dataset_cache_dir": str(dataset_cache),
        "cleanup_policy": "delete_failed_or_superseded_attempt_artifacts; retain_only_fingerprint_verified_dataset_cache",
        "cleanup_actions": [],
        "active_stage_dirs": {
            "conception": str(output_dir / "stage1_conception"),
            "planning": str(resumable_stage_dirs.get("planning", output_dir / "stage2_planning")),
            "experiment": str(resumable_stage_dirs.get("experiment", output_dir / "stage3_experiment")),
            "writing": str(output_dir / "stage4_writing"),
        },
    }

    # ── Stage 1: conception ─────────────────────────────────────
    _swarmflow_phase("conception")
    stage1_out = output_dir / "stage1_conception"
    conception_cache_inputs = [input_dir]
    cached = None if force else await _maybe_skip_stage(
        "conception", stage1_out, conception_cache_inputs
    )
    if cached is not None:
        log.info(f"Stage 1/4 conception: 使用缓存（status=complete, wall={cached['summary'].get('wall_time_seconds')}s）")
        stage1 = cached
    else:
        _cleanup_failed_directory(stage1_out, output_dir, summary, "stale conception cache before rerun")
        log.info(f"Stage 1/4 conception: 启动新跑（input_dir={input_dir}, output_dir={stage1_out}）")
        log.info("Stage 1/4: conception (literature + research framing)")
        stage1 = await _run_conception_stage(input_dir, stage1_out)
        log.info(f"Stage 1/4 conception: 子进程完成 status={stage1['summary']['status']}, wall={stage1['summary'].get('wall_time_seconds')}s")
    stage1["summary"]["cached"] = cached is not None
    if cached is None and stage1["summary"].get("status") == "complete":
        _write_stage_cache_key("conception", stage1_out, conception_cache_inputs)
    summary["stages"]["conception"] = stage1["summary"]
    _record_stage_attempt(summary, "conception", stage1["summary"])
    _write_stage_metrics(summary)
    if stage1["summary"]["status"] != "complete":
        log.info(f"Stage 1 conception 失败 → 终止: {stage1['summary'].get('errors', [])[:3]}")
        _cleanup_failed_directory(stage1_out, output_dir, summary, "failed conception")
        return _finalize(summary, "aborted_conception", started_at)

    # ── Stage 2: planning ────────────────────────────────────────
    _swarmflow_phase("planning")
    stage2_out = resumable_stage_dirs.get("planning", output_dir / "stage2_planning")
    planning_cache_inputs = [stage1_out / "conception_output.json"]
    cached = None if force else await _maybe_skip_stage(
        "planning", stage2_out, planning_cache_inputs
    )
    if cached is not None:
        log.info(f"Stage 2/4 planning: 使用缓存（status=complete, wall={cached['summary'].get('wall_time_seconds')}s）")
        stage2 = cached
    else:
        _cleanup_failed_directory(stage2_out, output_dir, summary, "stale planning cache before rerun")
        log.info(f"Stage 2/4 planning: 启动新跑（input_dir=stage1→_input, output_dir={stage2_out}）")
        log.info("Stage 2/4: planning (method + experiment + feasibility)")
        # ── 关键适配：conception 产物是单文件大 JSON；planning load_inputs.py:37-45
        # 期望 7 个独立文件（research_question.json / hypotheses.json / ...）。
        # 不拆的话 load_inputs 7 个全 miss → payloads 全 None → 空转反思循环。
        planning_input_dir = _adapt_conception_to_planning(stage1_out, stage2_out)
        log.info(f"  → 适配 stage1→stage2 input: {planning_input_dir} (8 个文件)")
        log.info("Stage 2: 调 planning subprocess (无超时, expect 5-8 min)...")
        stage2 = await _run_planning_stage(planning_input_dir, stage2_out)
        log.info(f"Stage 2/4 planning: 子进程完成 status={stage2['summary']['status']}, wall={stage2['summary'].get('wall_time_seconds')}s")
    stage2["summary"]["cached"] = cached is not None
    if cached is None and stage2["summary"].get("status") == "complete":
        _write_stage_cache_key("planning", stage2_out, planning_cache_inputs)
    summary["stages"]["planning"] = stage2["summary"]
    _record_stage_attempt(summary, "planning", stage2["summary"])
    _write_stage_metrics(summary)
    if stage2["summary"]["status"] != "complete":
        log.info(f"Stage 2 planning 失败 → 终止: {stage2['summary'].get('errors', [])[:3]}")
        _cleanup_failed_directory(stage2_out, output_dir, summary, "failed planning")
        if stage2["summary"].get("status") == "unsupported_execution_capability":
            return _finalize(summary, "blocked_execution_capability", started_at)
        return _finalize(summary, "aborted_planning", started_at)

    # ── Stage 3: experiment ──────────────────────────────────────
    _swarmflow_phase("experiment")
    stage3_out = resumable_stage_dirs.get("experiment", output_dir / "stage3_experiment")
    experiment_cache_inputs = _planning_contract_paths(stage2_out)
    cached = None if force else await _maybe_skip_stage(
        "experiment", stage3_out, experiment_cache_inputs
    )
    if cached is not None:
        log.info(f"Stage 3/4 experiment: 使用缓存（status=complete, wall={cached['summary'].get('wall_time_seconds')}s）")
        stage3 = cached
    else:
        _cleanup_failed_directory(stage3_out, output_dir, summary, "stale experiment cache before rerun")
        log.info("Stage 3/4 experiment: 启动新跑（projection + experiment-agent）")
        log.info("Stage 3/4: experiment (adapt + scaffold + run pipeline)")
        stage3 = await _run_experiment_stage(stage2_out, stage3_out)
        log.info(f"Stage 3/4 experiment: 子进程完成 status={stage3['summary']['status']}, wall={stage3['summary'].get('wall_time_seconds')}s")
    stage3["summary"]["cached"] = cached is not None
    if cached is None and stage3["summary"].get("status") == "complete":
        _write_stage_cache_key("experiment", stage3_out, experiment_cache_inputs)
    summary["stages"]["experiment"] = stage3["summary"]
    _record_stage_attempt(summary, "experiment", stage3["summary"])
    _write_stage_metrics(summary)
    # Stage 3 失败/partial → 不直接终止：允许 Stage 4 writing 试跑（若 experiment 有部分产物能合成 m3）；
    # 但若 experiment 完全没产出（status=error 且无 m3 数据）则终止。
    exp_status = stage3["summary"]["status"]
    if exp_status not in ("complete", "partial"):
        log.info(f"Stage 3 experiment 失败 → 终止: {stage3['summary'].get('errors', [])[:3]}")
        _cleanup_failed_directory(stage3_out, output_dir, summary, "failed experiment")
        return _finalize(summary, "aborted_experiment", started_at)
    # REPLAN 代表没有实证结果。不能让 writing 把空结果包装成论文。每一轮改写都使用
    # 新目录，上一轮 planning/experiment 与其 feedback bundle 都保留作审计证据。
    replan_history: list[dict[str, Any]] = []
    # Keep the previous public key as a read-only compatibility alias. Some
    # launchers and status renderers still consume it.
    summary["experiment_replan_history"] = replan_history
    seen_blocker_signatures: set[str] = set()
    while _experiment_requires_replan(stage3["summary"]):
        round_number = len(replan_history) + 1
        bundle_dir = output_dir / f"replan_round_{round_number}"
        try:
            feedback_file = _build_replan_bundle(
                stage3_out=stage3_out,
                previous_planning_dir=stage2_out,
                bundle_dir=bundle_dir,
                base_dir=Path.cwd(),
            )
            feedback = json.loads(feedback_file.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            entry = {
                "round": round_number,
                "planning_dir": str(stage2_out),
                "experiment_dir": str(stage3_out),
                "stop_reason": "missing_or_invalid_planning_feedback",
                "error": str(exc),
            }
            replan_history.append(entry)
            summary["replan_history"] = replan_history
            _cleanup_failed_directory(stage3_out, output_dir, summary, "invalid experiment REPLAN output")
            _cleanup_failed_directory(bundle_dir, output_dir, summary, "invalid REPLAN feedback bundle")
            return _finalize(summary, "waiting_for_experiment_replan", started_at)
        signature = _feedback_signature(feedback)
        next_stage2_out = output_dir / f"stage2_planning_replan_{round_number}"
        next_stage3_out = output_dir / f"stage3_experiment_replan_{round_number}"
        entry = {
            "round": round_number,
            "feedback_file": str(feedback_file),
            "feedback_bundle": str(bundle_dir),
            "previous_planning_dir": str(stage2_out),
            "previous_experiment_dir": str(stage3_out),
            "planning_dir": str(next_stage2_out),
            "experiment_dir": str(next_stage3_out),
            "blocker_signature": signature,
            "planning_feedback": feedback,
            "experiment_status": stage3["summary"].get("status"),
        }
        replan_history.append(entry)
        summary["replan_history"] = replan_history
        if signature in seen_blocker_signatures:
            entry["stop_reason"] = "no_progress_repeated_blockers"
            _cleanup_failed_directory(stage3_out, output_dir, summary, "repeated experiment REPLAN blockers")
            _cleanup_failed_directory(bundle_dir, output_dir, summary, "consumed REPLAN feedback bundle")
            return _finalize(summary, "replan_exhausted", started_at)
        seen_blocker_signatures.add(signature)
        if len(replan_history) > max_replan_rounds:
            entry["stop_reason"] = "max_replan_rounds_exceeded"
            _cleanup_failed_directory(stage3_out, output_dir, summary, "experiment REPLAN limit reached")
            _cleanup_failed_directory(bundle_dir, output_dir, summary, "consumed REPLAN feedback bundle")
            return _finalize(summary, "replan_exhausted", started_at)
        # A cached initial planning stage may lack its preserved conception
        # input.  Recreate it under the *new* planning attempt, never mutate
        # the historical directory.
        planning_input_dir = stage2_out / "_input"
        if not planning_input_dir.is_dir():
            planning_input_dir = _adapt_conception_to_planning(stage1_out, next_stage2_out)
        _swarmflow_phase("planning_replan")
        next_stage2 = await _run_planning_stage(
            planning_input_dir, next_stage2_out, feedback_file=feedback_file
        )
        next_stage2["summary"]["cached"] = False
        summary["stages"]["planning"] = next_stage2["summary"]
        _record_stage_attempt(
            summary, f"planning_replan_{round_number}", next_stage2["summary"]
        )
        _write_stage_metrics(summary)
        if next_stage2["summary"].get("status") != "complete":
            entry["stop_reason"] = "planning_replan_failed"
            _cleanup_failed_directory(next_stage2_out, output_dir, summary, "failed planning REPLAN")
            _cleanup_failed_directory(stage3_out, output_dir, summary, "superseded experiment REPLAN attempt")
            _cleanup_failed_directory(bundle_dir, output_dir, summary, "consumed REPLAN feedback bundle")
            return _finalize(summary, "aborted_planning_replan", started_at)
        # The planning CLI consumes but does not copy the immutable conception
        # input.  Persist the exact input snapshot used by this rewrite so it
        # becomes the predecessor for a subsequent REPLAN.
        next_input_snapshot = next_stage2_out / "_input"
        if not next_input_snapshot.exists():
            shutil.copytree(planning_input_dir, next_input_snapshot)
        _swarmflow_phase("experiment_replan")
        previous_stage2_out = stage2_out
        previous_stage3_out = stage3_out
        next_stage3 = await _run_experiment_stage(next_stage2_out, next_stage3_out)
        next_stage3["summary"]["cached"] = False
        stage2_out, stage2 = next_stage2_out, next_stage2
        stage3_out, stage3 = next_stage3_out, next_stage3
        summary["active_stage_dirs"]["planning"] = str(stage2_out)
        summary["active_stage_dirs"]["experiment"] = str(stage3_out)
        summary["stages"]["experiment"] = stage3["summary"]
        _record_stage_attempt(
            summary, f"experiment_replan_{round_number}", stage3["summary"]
        )
        _write_stage_metrics(summary)
        if next_stage2["summary"].get("status") == "complete":
            _write_stage_cache_key("planning", stage2_out, planning_cache_inputs)
        if next_stage3["summary"].get("status") == "complete":
            _write_stage_cache_key(
                "experiment", stage3_out, _planning_contract_paths(stage2_out)
            )
        _cleanup_failed_directory(previous_stage2_out, output_dir, summary, "superseded planning attempt")
        _cleanup_failed_directory(previous_stage3_out, output_dir, summary, "superseded experiment REPLAN attempt")
        _cleanup_failed_directory(bundle_dir, output_dir, summary, "consumed REPLAN feedback bundle")
        if stage3["summary"].get("status") not in ("complete", "partial"):
            entry["stop_reason"] = "experiment_replan_failed"
            _cleanup_failed_directory(stage3_out, output_dir, summary, "failed experiment REPLAN")
            return _finalize(summary, "aborted_experiment_replan", started_at)

    # ── Stage 4: writing ─────────────────────────────────────────
    _swarmflow_phase("writing")
    stage4_out = output_dir / "stage4_writing"
    writing_cache_inputs = _writing_source_paths(stage1_out, stage2_out, stage3_out)
    cached = None if force else await _maybe_skip_stage(
        "writing", stage4_out, writing_cache_inputs
    )
    if cached is not None:
        log.info(f"Stage 4/4 writing: 使用缓存（status=complete, wall={cached['summary'].get('wall_time_seconds')}s）")
        stage4 = cached
    else:
        _cleanup_failed_directory(stage4_out, output_dir, summary, "stale writing cache before rerun")
        log.info("Stage 4/4 writing: 启动新版 evidence-first writing")
        try:
            from scripts._writing_adapter import prepare_writing_inputs
            writing_inputs = prepare_writing_inputs(
                stage1_out, stage2_out, stage3_out, stage4_out, base_dir=Path.cwd()
            )
            log.info("  → 保留原始运行记录与哈希，投影 stage1+2+3 → writing inputs")
            stage4 = await _run_writing_stage(
                m1_path=Path(writing_inputs["m1"]),
                m2_path=Path(writing_inputs["m2"]),
                m3_path=Path(writing_inputs["m3"]),
                source_manifest=Path(writing_inputs["source_manifest"]),
                output_dir=stage4_out,
                dry_run=bool(args.get("dry_run", False)),
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            log.exception("Stage 4 输入适配或写作启动失败")
            stage4 = {"summary": {
                "status": "error",
                "errors": [f"writing preparation failed: {exc}"],
                "warnings": [],
                "artifacts": [],
            }}
        log.info(f"Stage 4/4 writing: 子进程完成 status={stage4['summary']['status']}, wall={stage4['summary'].get('wall_time_seconds')}s")
    stage4["summary"]["cached"] = cached is not None
    summary["stages"]["writing"] = stage4["summary"]
    release = _assess_writing_release(stage4_out, stage4["summary"])
    release_path = stage4_out / "publication_eligibility.json"
    _write_json_atomic(release_path, release)
    stage4["summary"]["publication_eligibility"] = release
    stage4["summary"].setdefault("artifacts", []).append(str(release_path))
    if release["eligible"]:
        end_status = "complete"
        if cached is None:
            _write_stage_cache_key("writing", stage4_out, writing_cache_inputs)
    elif stage4["summary"].get("status") in {
        "error", "aborted", "preflight_failed", "load_inputs_failed"
    }:
        end_status = "aborted_writing"
    else:
        end_status = "blocked_writing_release"
    stage4["summary"]["release_eligible"] = bool(release["eligible"])
    attempt_summary = dict(stage4["summary"])
    if not release["eligible"]:
        # A writing subprocess can report `complete` while a deterministic
        # publication gate still rejects the PDF. Such cost belongs to the
        # failed bucket, never to the successful end-to-end path.
        attempt_summary["status"] = end_status
        attempt_summary["errors"] = [
            *list(attempt_summary.get("errors") or []),
            *[f"publication gate failed: {item}" for item in release.get("errors") or []],
        ]
    _record_stage_attempt(summary, "writing", attempt_summary)
    _write_stage_metrics(summary)
    if end_status == "aborted_writing":
        _cleanup_failed_directory(stage4_out, output_dir, summary, "failed writing")
    return _finalize(summary, end_status, started_at)


def _assess_writing_release(stage4_out: Path, stage_summary: dict[str, Any]) -> dict[str, Any]:
    """Decide whether stage four produced a formally releasable paper.

    ``partial`` remains useful inside writing as a diagnostic state, but it is
    never a successful paper-gen delivery.  The release decision is rebuilt
    from disk so a stale or overly broad transport status cannot publish a PDF.
    """
    release_mode = str(stage_summary.get("release_mode") or "strict_pass").strip()
    is_draft_release = release_mode == "draft_with_warnings"
    checks: dict[str, bool] = {
        "writing_status_complete": stage_summary.get("status") == "complete",
        "writing_verdict_pass": stage_summary.get("verdict") == "pass",
        "status_errors_empty": not bool(stage_summary.get("errors")),
    }
    errors: list[str] = []

    required_files = {
        "paper_pdf_exists": stage4_out / "paper.pdf",
        "paper_tex_exists": stage4_out / "paper.tex",
        "pdf_validation_exists": stage4_out / "pdf_validation.json",
        "evidence_graph_audit_exists": stage4_out / "reviews" / "evidence_graph_audit.json",
        "final_review_exists": stage4_out / "reviews" / "final_paper_reviewer.json",
    }
    for check, path in required_files.items():
        checks[check] = path.is_file() and path.stat().st_size > 0

    pdf_validation = _read_json_object(required_files["pdf_validation_exists"])
    checks["pdf_validation_passed"] = pdf_validation.get("passed") is True
    graph_audit = _read_json_object(required_files["evidence_graph_audit_exists"])
    checks["evidence_graph_audit_passed"] = graph_audit.get("passed") is True
    final_review = _read_json_object(required_files["final_review_exists"])
    normalized_final = final_review.get("normalized") if isinstance(final_review.get("normalized"), dict) else {}
    checks["final_review_passed"] = normalized_final.get("verdict") == "pass"

    specialist_roles = (
        "claim_verifier", "literature_novelty_reviewer",
        "argument_reviewer", "visual_reviewer_final",
    )
    for role in specialist_roles:
        revised = stage4_out / "reviews" / f"{role}.after_revision.json"
        base = stage4_out / "reviews" / f"{role}.json"
        record = _read_json_object(revised if revised.is_file() else base)
        normalized = record.get("normalized") if isinstance(record.get("normalized"), dict) else {}
        checks[f"{role}_passed"] = normalized.get("verdict") == "pass"

    # A finished draft can carry *specific, non-blocking* editorial follow-up
    # after the writing skill has made its bounded, targeted repairs.  Do not
    # treat that as equivalent to bad evidence or a broken PDF.  The latter
    # stay in ``errors`` regardless of release mode.  This distinction avoids
    # re-running the same expensive reviewer loop merely because a reviewer
    # still prefers a different wording or presentation choice.
    editorial_checks = {
        "writing_verdict_pass",
        "final_review_passed",
        *{f"{role}_passed" for role in specialist_roles},
    }
    warnings: list[str] = []
    for check, passed in checks.items():
        if passed:
            continue
        if is_draft_release and check in editorial_checks:
            warnings.append(f"nonblocking editorial follow-up: {check}")
        else:
            errors.append(check)
    return {
        "schema_version": 1,
        "eligible": not errors,
        "checks": checks,
        "errors": errors,
        "warnings": warnings,
        "release_mode": release_mode,
        "next_action": (
            "deliver_draft_with_warnings" if not errors and is_draft_release
            else "deliver_paper" if not errors else "revise_or_recover_writing"
        ),
    }


def _read_json_object(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _experiment_requires_replan(stage3_summary: dict[str, Any]) -> bool:
    """Use the module-three decision; retain legacy warning support for old runs."""
    if isinstance(stage3_summary.get("replan_requested"), bool):
        return stage3_summary["replan_requested"]
    warnings = stage3_summary.get("warnings") or []
    return any(isinstance(item, str) and item.startswith("REPLAN ") for item in warnings)


def _build_replan_bundle(
    *,
    stage3_out: Path,
    previous_planning_dir: Path,
    bundle_dir: Path,
    base_dir: Path | None = None,
) -> Path:
    """Materialize one immutable module-three → planning REPLAN handoff.

    The run root is resolved from the stage-three request, never selected by
    recursive search or modification time.  This matters once content-addressed
    ``runs/<run_id>`` directories coexist.  The bundle keeps the planning
    snapshot and any verified source-resolution hints alongside the structured
    feedback, which is exactly the layout the planning REPLAN path consumes.
    """
    from _experiment_output import (  # noqa: E402
        output_path,
        planning_feedback,
        resolve_stage3_run_dir,
    )

    run_dir = resolve_stage3_run_dir(stage3_out, base_dir or Path.cwd())
    source = output_path(run_dir)
    if not source.is_file():
        raise ValueError(f"未找到模块三终态产物: {source}")
    payload = json.loads(source.read_text(encoding="utf-8"))
    feedback = planning_feedback(payload)
    if feedback is None:
        raise ValueError("模块三标记 REPLAN，但缺少结构化 planning_feedback")

    bundle_dir.mkdir(parents=True, exist_ok=True)
    feedback_file = bundle_dir / "planning_feedback.json"
    _write_json_atomic(feedback_file, feedback)
    for name in (
        "method_design.json", "method_review.json", "experiment_plan.json",
        "data_plan.json", "execution_config.json", "contract_ledger.json",
    ):
        source_file = previous_planning_dir / name
        if source_file.is_file():
            shutil.copy2(source_file, bundle_dir / name)

    # A resolver-produced direct URL is only a planning hint, never a silent
    # dataset substitution.  The next planning contract still has to adopt it
    # explicitly and pass the new stage-three gates.
    source_resolution = run_dir / "data" / "source-resolution"
    if source_resolution.is_dir():
        target = bundle_dir / "source_resolution"
        target.mkdir(parents=True, exist_ok=True)
        for index, source_file in enumerate(sorted(source_resolution.glob("*.json"))):
            shutil.copy2(source_file, target / f"{index:02d}-{source_file.name}")
    return feedback_file


def _persist_experiment_feedback(stage3_out: Path, stage2_out: Path) -> dict[str, Any] | None:
    """Legacy-compatible helper used by focused tests and older callers.

    The normal orchestration uses a versioned ``replan_round_*`` bundle.  This
    wrapper intentionally writes the former same-directory layout so callers
    relying on the old function still receive structured feedback and hints.
    """
    try:
        feedback_file = _build_replan_bundle(
            stage3_out=stage3_out,
            previous_planning_dir=stage2_out,
            bundle_dir=stage2_out,
            base_dir=Path.cwd(),
        )
        loaded = json.loads(feedback_file.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _feedback_signature(feedback: dict[str, Any] | None) -> str:
    if not isinstance(feedback, dict):
        return ""
    blockers = feedback.get("blockers") if isinstance(feedback.get("blockers"), list) else []
    normalized = sorted(str(item).strip() for item in blockers if str(item).strip())
    return json.dumps({"reason": str(feedback.get("reason") or "").strip(), "blockers": normalized}, ensure_ascii=False, sort_keys=True)


_STAGE_CACHE_KEY_NAME = ".stage-cache-key.json"


def _load_resumable_stage_dirs(output_dir: Path) -> dict[str, Path]:
    """Recover the last successful REPLAN directories from run_summary.

    Successful rewritten planning/experiment stages use versioned directory
    names. Without this pointer a later invocation falls back to the deleted
    initial directories and repeats expensive model/experiment work. Paths are
    accepted only when they still exist inside this output root.
    """
    summary_path = output_dir / "run_summary.json"
    try:
        payload = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    declared = payload.get("active_stage_dirs") if isinstance(payload, dict) else None
    if not isinstance(declared, dict):
        return {}
    root = output_dir.resolve()
    result: dict[str, Path] = {}
    for stage in ("planning", "experiment"):
        value = declared.get(stage)
        if not isinstance(value, str) or not value.strip():
            continue
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = output_dir / candidate
        resolved = candidate.resolve()
        if root in resolved.parents and resolved.is_dir():
            result[stage] = resolved
    return result


async def _maybe_skip_stage(
    stage_name: str,
    stage_dir: Path,
    input_paths: list[Path],
) -> dict | None:
    """检查 stage_dir/status.json 是否存在 + status==complete，是则返缓存的 summary；否则 None。

    命中缓存说明该 stage 之前完整跑通过；本次 paper-gen run 跳过它，节省时间。
    force=True 时由 caller 决定不走这条路径。
    """
    status_file = stage_dir / "status.json"
    if not status_file.is_file():
        return None
    try:
        from _status import read_status_file
    except ImportError:
        # main_flow.py 跑时 _status.py 在同目录；这里兜底 sys.path
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from _status import read_status_file
    payload = read_status_file(status_file)
    if payload is None:
        return None
    if payload.get("status") != "complete":
        return None
    cache_key_path = stage_dir / _STAGE_CACHE_KEY_NAME
    try:
        cache_key = json.loads(cache_key_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        log.info("%s 缺少输入指纹；为避免复用旧任务产物，本轮重跑", stage_name)
        return None
    expected_fingerprint = _fingerprint_paths(input_paths)
    if (
        not isinstance(cache_key, dict)
        or cache_key.get("stage") != stage_name
        or cache_key.get("input_sha256") != expected_fingerprint
    ):
        log.info("%s 输入内容已变化；缓存失效并重跑", stage_name)
        return None
    if stage_name == "planning":
        projection_report = stage_dir.parent / "stage3_experiment" / "projection_report.json"
        if projection_report.is_file():
            try:
                projection = json.loads(projection_report.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                projection = {}
            if projection.get("status") == "invalid":
                log.warning("planning 的下游投影报告为 invalid；不复用该 complete 缓存")
                return None
    missing = _missing_required_artifacts(stage_name, stage_dir)
    if missing:
        log.warning("%s 的 status=complete 但缺必要产物 %s；缓存失效并重跑", stage_name, missing)
        return None
    return {"summary": payload}


def _write_stage_cache_key(
    stage_name: str,
    stage_dir: Path,
    input_paths: list[Path],
) -> None:
    _write_json_atomic(
        stage_dir / _STAGE_CACHE_KEY_NAME,
        {
            "schema_version": 1,
            "stage": stage_name,
            "input_sha256": _fingerprint_paths(input_paths),
            "input_count": len(input_paths),
        },
    )


def _fingerprint_paths(paths: list[Path]) -> str:
    """Hash stage inputs deterministically without depending on absolute paths."""
    digest = hashlib.sha256()
    for index, raw_path in enumerate(paths):
        path = Path(raw_path)
        digest.update(f"input:{index}:{path.name}\0".encode("utf-8"))
        if path.is_file():
            digest.update(b"file\0")
            digest.update(path.read_bytes())
            continue
        if path.is_dir():
            digest.update(b"dir\0")
            for item in sorted(
                (candidate for candidate in path.rglob("*") if candidate.is_file()),
                key=lambda candidate: candidate.relative_to(path).as_posix(),
            ):
                relative = item.relative_to(path)
                if (
                    "__pycache__" in relative.parts
                    or item.suffix.casefold() in {".pyc", ".log"}
                    or item.name in {_STAGE_CACHE_KEY_NAME, "progress.json"}
                ):
                    continue
                digest.update(relative.as_posix().encode("utf-8"))
                digest.update(b"\0")
                digest.update(item.read_bytes())
            continue
        digest.update(b"missing\0")
    return digest.hexdigest()


def _planning_contract_paths(stage2_dir: Path) -> list[Path]:
    names = (
        "method_design.json", "experiment_plan.json", "data_plan.json",
        "execution_config.json", "budget_report.json", "contract_ledger.json",
    )
    paths = [stage2_dir / name for name in names]
    for name in ("domain.json", "resource_constraints.json"):
        paths.append(stage2_dir / "_input" / name)
    return paths


def _writing_source_paths(
    stage1_dir: Path,
    stage2_dir: Path,
    stage3_dir: Path,
) -> list[Path]:
    paths = [stage1_dir / "conception_output.json", *_planning_contract_paths(stage2_dir)]
    paths.append(stage3_dir / "request.json")
    try:
        from _experiment_output import resolve_stage3_run_dir, output_path
        run_dir = resolve_stage3_run_dir(stage3_dir, Path.cwd())
        paths.extend((
            output_path(run_dir),
            run_dir / "outputs" / "runtime-results.json",
            run_dir / "outputs" / "artifact-manifest.json",
        ))
    except (OSError, ValueError):
        # Missing evidence remains part of the fingerprint and will also fail
        # the stage's required-artifact/readiness gates.
        paths.append(stage3_dir / "run" / "outputs" / "experiment-module-output.json")
    return paths


def _missing_required_artifacts(stage_name: str, stage_dir: Path) -> list[str]:
    required = {
        "conception": ["conception_output.json"],
        "planning": ["method_design.json", "experiment_plan.json", "data_plan.json", "execution_config.json"],
        # A formal current writing delivery contains the rendered PDF, its TeX
        # source, and the deterministic PDF validation report.  ``paper.json``
        # belonged to the retired Part1/Part2 path and must not force a costly
        # successful evidence-first run to be rerun.
        "writing": ["paper.pdf", "paper.tex", "pdf_validation.json"],
    }
    if stage_name == "experiment":
        try:
            from _experiment_output import output_path, resolve_stage3_run_dir
            expected = output_path(resolve_stage3_run_dir(stage_dir, Path.cwd()))
        except (OSError, ValueError):
            return ["authoritative experiment run_dir"]
        return [] if expected.is_file() else [str(expected)]
    return [name for name in required.get(stage_name, []) if not (stage_dir / name).is_file()]


def _normalized_token_total(info: dict[str, Any]) -> dict[str, int]:
    keys = ("request_count", "prompt_tokens", "completion_tokens", "total_tokens")
    total = ((info.get("llm_token_usage") or {}).get("total") or {})
    return {key: int(total.get(key) or 0) for key in keys}


def _token_usage_is_reported(info: dict[str, Any]) -> bool:
    """Distinguish observed zero usage from missing provider telemetry.

    Prior status files only exposed a zero-valued ``total`` object when the
    tracker was disconnected, which made an incomplete cost ledger look
    complete.  New skill CLIs mark that condition explicitly.  Older files
    retain their previous interpretation for backward compatibility.
    """
    usage = info.get("llm_token_usage") if isinstance(info, dict) else None
    if not isinstance(usage, dict):
        return False
    state = usage.get("measurement_status")
    if state in {"unavailable", "partial"}:
        return False
    if state in {"reported", "not_applicable"}:
        return True
    return isinstance(usage.get("total"), dict)


def _record_stage_attempt(
    summary: dict[str, Any], attempt_name: str, info: dict[str, Any]
) -> None:
    """Append one actually executed stage attempt to the durable resource ledger.

    Cached stages have no cost in the current invocation and are deliberately not
    appended.  The attempt id makes repeated metric refreshes idempotent while
    preserving failed attempts across later resumes and successful retries.
    """
    output_dir = Path(summary["output_dir"])
    path = output_dir / "resource_attempts.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except (OSError, json.JSONDecodeError):
        payload = {}
    attempts = payload.get("attempts") if isinstance(payload, dict) else None
    if not isinstance(attempts, list):
        attempts = []
    cached = bool(info.get("cached"))
    base_stage = attempt_name.split("_replan_", 1)[0]
    if cached and any(
        isinstance(item, dict) and item.get("stage") == base_stage
        for item in attempts
    ):
        return
    if cached:
        usage_key = _normalized_token_total(info)
        attempt_id = (
            f"historical-cache:{base_stage}:"
            f"{float(info.get('wall_time_seconds') or 0.0):.2f}:"
            f"{usage_key['total_tokens']}"
        )
    else:
        attempt_id = f"{summary.get('invocation_id', 'paper-gen')}:{attempt_name}"
    # Any new execution of a stage supersedes the previous accepted attempt.
    # An experiment REPLAN additionally invalidates the planning attempt that
    # produced the rejected contract. This lets the ledger separate useful
    # final-path cost from retry/failure cost after the fact.
    if not cached:
        for item in attempts:
            if (
                isinstance(item, dict)
                and item.get("stage") == base_stage
                and item.get("status") == "complete"
                and not item.get("invalidated")
            ):
                item["invalidated"] = True
                item["outcome_reason"] = "superseded_by_later_attempt"
    if base_stage == "experiment" and info.get("replan_requested") is True:
        planning_candidates = [
            item for item in attempts
            if isinstance(item, dict)
            and item.get("stage") == "planning"
            and item.get("status") == "complete"
            and not item.get("invalidated")
        ]
        if planning_candidates:
            invalidated = max(
                planning_candidates,
                key=lambda item: str(item.get("recorded_at") or ""),
            )
            invalidated["invalidated"] = True
            invalidated["outcome_reason"] = "invalidated_by_experiment_replan"
    record = {
        "attempt_id": attempt_id,
        "invocation_id": summary.get("invocation_id"),
        "recorded_at": _iso8601(time.time()),
        "stage": base_stage,
        "attempt_name": attempt_name,
        "status": info.get("status"),
        "historical_cache_import": cached,
        "incurred_in_current_invocation": not cached,
        "invalidated": False,
        "outcome_reason": (
            "accepted_stage_result" if info.get("status") == "complete"
            else "replan" if info.get("replan_requested") is True
            else str(info.get("status") or "failed")
        ),
        "token_usage_reported": _token_usage_is_reported(info),
        "wall_time_seconds": round(float(info.get("wall_time_seconds") or 0.0), 2),
        "llm_tokens": _normalized_token_total(info),
        "errors": list(info.get("errors") or []),
        "artifacts": list(info.get("artifacts") or []),
    }
    by_id = {
        item.get("attempt_id"): item
        for item in attempts
        if isinstance(item, dict) and item.get("attempt_id")
    }
    by_id[attempt_id] = record
    attempts = list(by_id.values())
    token_keys = ("request_count", "prompt_tokens", "completion_tokens", "total_tokens")
    token_total = {key: 0 for key in token_keys}
    successful_token_total = {key: 0 for key in token_keys}
    unsuccessful_token_total = {key: 0 for key in token_keys}
    wall_total = 0.0
    missing_usage_attempts = 0
    by_stage: dict[str, dict[str, Any]] = {}
    for item in attempts:
        usage = item.get("llm_tokens") or {}
        reported = item.get("token_usage_reported")
        if reported is None:
            reported = any(int(usage.get(key) or 0) > 0 for key in token_keys)
            item["token_usage_reported"] = reported
        if not reported:
            missing_usage_attempts += 1
        wall = float(item.get("wall_time_seconds") or 0.0)
        wall_total += wall
        stage = str(item.get("stage") or "unknown")
        stage_bucket = by_stage.setdefault(stage, {
            "attempt_count": 0,
            "wall_time_seconds": 0.0,
            "llm_tokens": {key: 0 for key in token_keys},
            "successful_path": {key: 0 for key in token_keys},
            "replan_or_failed": {key: 0 for key in token_keys},
        })
        stage_bucket["attempt_count"] += 1
        stage_bucket["wall_time_seconds"] += wall
        accepted = item.get("status") == "complete" and not item.get("invalidated")
        item["cost_bucket"] = "successful_path" if accepted else "replan_or_failed"
        for key in token_keys:
            value = int(usage.get(key) or 0)
            token_total[key] += value
            stage_bucket["llm_tokens"][key] += value
            target_total = successful_token_total if accepted else unsuccessful_token_total
            target_stage = stage_bucket["successful_path"] if accepted else stage_bucket["replan_or_failed"]
            target_total[key] += value
            target_stage[key] += value
    for bucket in by_stage.values():
        bucket["wall_time_seconds"] = round(bucket["wall_time_seconds"], 2)
    _write_json_atomic(path, {
        "updated_at": _iso8601(time.time()),
        "attempts": attempts,
        "aggregate": {
            "attempt_count": len(attempts),
            "wall_time_seconds": round(wall_total, 2),
            "llm_tokens": token_total,
            "successful_path": {
                "full_pipeline_completed": all(
                    any(
                        isinstance(item, dict)
                        and item.get("stage") == stage
                        and item.get("cost_bucket") == "successful_path"
                        for item in attempts
                    )
                    for stage in ("conception", "planning", "experiment", "writing")
                ),
                "llm_tokens": successful_token_total,
            },
            "replan_or_failed": {"llm_tokens": unsuccessful_token_total},
            "coverage": {
                "reported_attempt_count": len(attempts) - missing_usage_attempts,
                "missing_usage_attempt_count": missing_usage_attempts,
                "complete": missing_usage_attempts == 0,
            },
            "by_stage": by_stage,
        },
        "note": "Append-only cost ledger for executed attempts; cached reuse adds no new attempt.",
    })


def _read_attempt_aggregate(output_dir: Path) -> dict[str, Any]:
    path = output_dir / "resource_attempts.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    aggregate = payload.get("aggregate") if isinstance(payload, dict) else None
    return aggregate if isinstance(aggregate, dict) else {}


def _write_stage_metrics(summary: dict) -> None:
    """每阶段结束立即刷新账本，进程中断时也保留已完成部分。"""
    _reconcile_logged_token_usage(summary)
    stages: dict[str, Any] = {}
    totals = {"request_count": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    current_totals = {key: 0 for key in totals}
    cached_totals = {key: 0 for key in totals}
    stage_wall_total = 0.0
    current_stage_wall_total = 0.0
    for name, info in summary.get("stages", {}).items():
        usage = info.get("llm_token_usage") or {}
        token_total = usage.get("total") if isinstance(usage, dict) else {}
        token_total = token_total if isinstance(token_total, dict) else {}
        for key in totals:
            value = int(token_total.get(key) or 0)
            totals[key] += value
            (cached_totals if info.get("cached") else current_totals)[key] += value
        stage_wall = float(info.get("wall_time_seconds") or 0.0)
        stage_wall_total += stage_wall
        if not info.get("cached"):
            current_stage_wall_total += stage_wall
        stage_dir = Path(summary["output_dir"]) / {
            "conception": "stage1_conception", "planning": "stage2_planning",
            "experiment": "stage3_experiment", "writing": "stage4_writing",
        }[name]
        stages[name] = {
            "status": info.get("status"),
            "cached": bool(info.get("cached")),
            "wall_time_seconds": info.get("wall_time_seconds", 0),
            "llm_token_usage": usage,
            "status_path": str(stage_dir / "status.json"),
            "log_files": [str(p) for p in sorted(stage_dir.glob("*.log"))],
            "artifacts": info.get("artifacts") or [],
        }
    payload = {
        "updated_at": _iso8601(time.time()),
        "stages": stages,
        "llm_token_usage_total": totals,
        "llm_token_usage_current_run_total": current_totals,
        "llm_token_usage_cached_history_total": cached_totals,
        "stage_wall_time_seconds_total": round(stage_wall_total, 2),
        "current_run_stage_wall_time_seconds_total": round(current_stage_wall_total, 2),
        "orchestrator_wall_time_seconds": summary.get("wall_time_seconds"),
        "all_execution_attempts": _read_attempt_aggregate(Path(summary["output_dir"])),
    }
    _write_json_atomic(Path(summary["output_dir"]) / "stage_metrics.json", payload)
    aggregate = payload["all_execution_attempts"] or {}
    by_stage_attempts = aggregate.get("by_stage") or {}
    if aggregate:
        successful = ((aggregate.get("successful_path") or {}).get("llm_tokens") or {})
        unsuccessful = ((aggregate.get("replan_or_failed") or {}).get("llm_tokens") or {})
        overall = aggregate.get("llm_tokens") or {}
        full_pipeline_completed = bool(
            (aggregate.get("successful_path") or {}).get("full_pipeline_completed")
        )
    else:
        successful, unsuccessful, overall = totals, {key: 0 for key in totals}, totals
        full_pipeline_completed = all(
            (summary.get("stages") or {}).get(stage, {}).get("status") == "complete"
            for stage in ("conception", "planning", "experiment", "writing")
        )
    token_payload = {
        "schema_version": 1,
        "updated_at": payload["updated_at"],
        "modules": {
            stage: {
                "attempt_count": int((by_stage_attempts.get(stage) or {}).get("attempt_count") or 0),
                "successful_path": (by_stage_attempts.get(stage) or {}).get("successful_path") or {},
                "replan_or_failed": (by_stage_attempts.get(stage) or {}).get("replan_or_failed") or {},
                **((by_stage_attempts.get(stage) or {}).get("llm_tokens") or _normalized_token_total(
                    (summary.get("stages") or {}).get(stage, {})
                )),
            }
            for stage in ("conception", "planning", "experiment", "writing")
        },
        "successful_path": {
            "full_pipeline_completed": full_pipeline_completed,
            "provisional": not full_pipeline_completed,
            "total": {key: int(successful.get(key) or 0) for key in totals},
        },
        "replan_or_failed": {
            "total": {key: int(unsuccessful.get(key) or 0) for key in totals},
        },
        "overall_total": {key: int(overall.get(key) or 0) for key in totals},
        "total": {key: int(overall.get(key) or 0) for key in totals},
        "coverage": aggregate.get("coverage") or {
            "reported_attempt_count": 0,
            "missing_usage_attempt_count": 0,
            "complete": False,
        },
        "note": (
            "Successful-path cost contains the currently accepted attempt for each module; "
            "it is final only when full_pipeline_completed is true. REPLAN/failed includes "
            "failed, rejected and superseded attempts."
        ),
    }
    _write_json_atomic(Path(summary["output_dir"]) / "token_usage.json", token_payload)
    _write_resource_usage_report(Path(summary["output_dir"]), payload)
    log.info(
        "资源汇总: stage_wall=%.2fs, current_stage_wall=%.2fs, "
        "LLM requests=%d, prompt_tokens=%d, completion_tokens=%d, total_tokens=%d",
        stage_wall_total, current_stage_wall_total, totals["request_count"],
        totals["prompt_tokens"], totals["completion_tokens"], totals["total_tokens"],
    )


def _write_resource_usage_report(output_dir: Path, current: dict[str, Any]) -> None:
    """Write a review-friendly resource summary beside the machine-readable logs."""
    history = current.get("all_execution_attempts") or {}
    # ``_read_attempt_aggregate`` already returns the aggregate object rather
    # than the whole resource_attempts.json payload.
    aggregate = history
    by_stage = aggregate.get("by_stage") or {}
    lines = [
        "# Paper-gen resource usage",
        "",
        f"Updated: {current.get('updated_at', '')}",
        "",
        "## All executed attempts (including failed retries)",
        "",
        "| Module | Attempts | LLM requests | Prompt tokens | Completion tokens | Total tokens | Wall time (s) |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for stage in ("conception", "planning", "experiment", "writing"):
        bucket = by_stage.get(stage) or {}
        tokens = bucket.get("llm_tokens") or {}
        lines.append(
            f"| {stage} | {int(bucket.get('attempt_count') or 0)} | "
            f"{int(tokens.get('request_count') or 0)} | "
            f"{int(tokens.get('prompt_tokens') or 0)} | "
            f"{int(tokens.get('completion_tokens') or 0)} | "
            f"{int(tokens.get('total_tokens') or 0)} | "
            f"{float(bucket.get('wall_time_seconds') or 0.0):.2f} |"
        )
    total = aggregate.get("llm_tokens") or {}
    successful = ((aggregate.get("successful_path") or {}).get("llm_tokens") or {})
    unsuccessful = ((aggregate.get("replan_or_failed") or {}).get("llm_tokens") or {})
    full_pipeline_completed = bool(
        (aggregate.get("successful_path") or {}).get("full_pipeline_completed")
    )
    coverage = aggregate.get("coverage") or {}
    lines.extend([
        f"| **TOTAL** | **{int(aggregate.get('attempt_count') or 0)}** | "
        f"**{int(total.get('request_count') or 0)}** | "
        f"**{int(total.get('prompt_tokens') or 0)}** | "
        f"**{int(total.get('completion_tokens') or 0)}** | "
        f"**{int(total.get('total_tokens') or 0)}** | "
        f"**{float(aggregate.get('wall_time_seconds') or 0.0):.2f}** |",
        "",
        "## Successful path vs REPLAN/failed",
        "",
        f"- Full pipeline completed: {'yes' if full_pipeline_completed else 'no'}",
        f"- Successful path: prompt={int(successful.get('prompt_tokens') or 0)}, "
        f"completion={int(successful.get('completion_tokens') or 0)}, "
        f"total={int(successful.get('total_tokens') or 0)}"
        + ("" if full_pipeline_completed else " (provisional)"),
        f"- REPLAN/failed/superseded: prompt={int(unsuccessful.get('prompt_tokens') or 0)}, "
        f"completion={int(unsuccessful.get('completion_tokens') or 0)}, "
        f"total={int(unsuccessful.get('total_tokens') or 0)}",
        f"- Overall: prompt={int(total.get('prompt_tokens') or 0)}, "
        f"completion={int(total.get('completion_tokens') or 0)}, "
        f"total={int(total.get('total_tokens') or 0)}",
        f"- Provider usage coverage: {'complete' if coverage.get('complete') else 'incomplete'}; "
        f"missing attempts={int(coverage.get('missing_usage_attempt_count') or 0)}",
        "",
        "## Current invocation stage view",
        "",
        "| Module | Cache | Status | LLM requests | Total tokens | Wall time (s) |",
        "|---|---|---|---:|---:|---:|",
    ])
    for stage in ("conception", "planning", "experiment", "writing"):
        item = (current.get("stages") or {}).get(stage) or {}
        tokens = ((item.get("llm_token_usage") or {}).get("total") or {})
        lines.append(
            f"| {stage} | {'yes' if item.get('cached') else 'no'} | "
            f"{item.get('status', '')} | {int(tokens.get('request_count') or 0)} | "
            f"{int(tokens.get('total_tokens') or 0)} | "
            f"{float(item.get('wall_time_seconds') or 0.0):.2f} |"
        )
    lines.extend([
        "",
        "Wall times are accumulated per stage attempt and may overlap with provider/network waits. "
        "The all-attempt total intentionally includes failed retries; cached reuse does not add a new attempt.",
        "",
    ])
    rendered = "\n".join(lines)
    (output_dir / "resource_usage_report.md").write_text(rendered, encoding="utf-8")
    # Stable name shared with the Jiuwen root-Agent execution path.
    (output_dir / "token_usage_report.md").write_text(rendered, encoding="utf-8")


_FORWARDED_LLM_USAGE = re.compile(
    r"\[(conception|planning|experiment|writing) stderr\].*?"
    r"\[LLM\]\s+<<<.*?tokens=\{input=(\d+),\s*output=(\d+)\}"
)


def _reconcile_logged_token_usage(summary: dict) -> None:
    """Recover provider usage omitted by a child status file.

    Native-agent stages emit one compact ``[LLM] <<<`` record per provider
    response.  The detailed ``llm_call_end`` event is deliberately ignored to
    avoid double counting.  This fallback also repairs old cached stage status
    files when a later resume reads them.
    """
    output_dir = Path(summary["output_dir"])
    log_path = output_dir / "paper_gen.log"
    if not log_path.is_file():
        return
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    recovered: dict[str, dict[str, int]] = {}
    for match in _FORWARDED_LLM_USAGE.finditer(text):
        stage = match.group(1)
        prompt = int(match.group(2))
        completion = int(match.group(3))
        total = recovered.setdefault(stage, {
            "request_count": 0, "prompt_tokens": 0,
            "completion_tokens": 0, "total_tokens": 0,
        })
        total["request_count"] += 1
        total["prompt_tokens"] += prompt
        total["completion_tokens"] += completion
        total["total_tokens"] += prompt + completion
    default_stage_dirs = {
        "conception": "stage1_conception", "planning": "stage2_planning",
        "experiment": "stage3_experiment", "writing": "stage4_writing",
    }
    active_stage_dirs = summary.get("active_stage_dirs") or {}
    for stage, total in recovered.items():
        info = (summary.get("stages") or {}).get(stage)
        if not isinstance(info, dict):
            continue
        declared = ((info.get("llm_token_usage") or {}).get("total") or {})
        if int(declared.get("request_count") or 0):
            continue
        usage = {
            "total": total,
            "by_stage": {stage: total},
            "by_operation": {}, "records": [],
            "measurement_status": "reported",
            "source": "paper_gen.log forwarded provider usage",
        }
        info["llm_token_usage"] = usage
        declared_dir = active_stage_dirs.get(stage)
        stage_dir = Path(declared_dir) if isinstance(declared_dir, str) else output_dir / default_stage_dirs[stage]
        status_path = stage_dir / "status.json"
        try:
            status = json.loads(status_path.read_text(encoding="utf-8"))
            if isinstance(status, dict):
                status["llm_token_usage"] = usage
                _write_json_atomic(status_path, status)
        except (OSError, json.JSONDecodeError):
            pass


def _resource_usage_summary(summary: dict) -> dict:
    token_total = {"request_count": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    current_token_total = {key: 0 for key in token_total}
    stage_wall = 0.0
    current_stage_wall = 0.0
    per_stage: dict[str, Any] = {}
    for name, info in summary.get("stages", {}).items():
        total = ((info.get("llm_token_usage") or {}).get("total") or {})
        cached = bool(info.get("cached"))
        wall = float(info.get("wall_time_seconds") or 0.0)
        normalized = {key: int(total.get(key) or 0) for key in token_total}
        for key, value in normalized.items():
            token_total[key] += value
            if not cached:
                current_token_total[key] += value
        stage_wall += wall
        if not cached:
            current_stage_wall += wall
        per_stage[name] = {"cached": cached, "wall_time_seconds": wall, "llm_tokens": normalized}
    attempt_aggregate = _read_attempt_aggregate(Path(summary["output_dir"]))
    return {
        "per_stage": per_stage,
        "all_stage_wall_time_seconds": round(stage_wall, 2),
        "current_run_stage_wall_time_seconds": round(current_stage_wall, 2),
        "orchestrator_wall_time_seconds": summary.get("wall_time_seconds", 0.0),
        "llm_tokens_all_stages": token_total,
        "llm_tokens_current_run": current_token_total,
        "successful_path": attempt_aggregate.get("successful_path") or {},
        "replan_or_failed": attempt_aggregate.get("replan_or_failed") or {},
        "overall_attempt_tokens": attempt_aggregate.get("llm_tokens") or token_total,
        "all_execution_attempts": attempt_aggregate,
        "note": "Cached stage values are historical status values; current-run totals exclude cached stages.",
    }


def _adapt_conception_to_planning(stage1_out: Path, stage2_out: Path) -> Path:
    """把 conception 产物（单文件大 JSON）拆成 planning 期望的 8 个独立文件。

    背景：planning/scripts/load_inputs.py:37-45 期望 plan_dir/ 下含 8 个文件：
    research_question.json / hypotheses.json / gap_report.json /
        key_papers.json / references.json / resource_constraints.json /
        research_frontier.json / domain.json

    但 conception 只输出一个 conception_output.json（顶层含全部 8 字段），
    不拆的话 load_inputs 8 个全 miss → payloads 全 None → method_designer 空转。
    references 不能省略为仅 key_papers 的兼容降级：规划的创新性判断、基线和
    实验取舍需要看到模块一已核验的完整文献证据。

    Args:
        stage1_out: stage1_conception 输出目录（含 conception_output.json）
        stage2_out: stage2_planning 输出目录；适配产物写到 stage2_out/_input/

    Returns:
        适配产物目录路径（stage2_out/_input/），planning subprocess 拿这个当 input_dir。
    """
    input_dir = stage2_out / "_input"
    input_dir.mkdir(parents=True, exist_ok=True)

    conception_json = stage1_out / "conception_output.json"
    if not conception_json.is_file():
        log.warning("stage1 缺 conception_output.json，planning input 目录将为空")
        return input_dir

    try:
        data = json.loads(conception_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        log.warning(f"  ⚠️ conception_output.json 解析失败: {e}，planning input 目录将为空")
        return input_dir

    # conception PASS 时直接顶层；FAILED 时可能包在 partial 里
    payload = data.get("partial") if isinstance(data.get("partial"), dict) else data
    if not isinstance(payload, dict):
        log.warning("  ⚠️ conception 顶层不是 dict，planning input 目录将为空")
        return input_dir

    # 8 个拆分键（与 load_inputs.py:_INPUT_FILES 严格一致）
    split_keys = [
        ("research_question",    "research_question.json"),
        ("hypotheses",           "hypotheses.json"),
        ("gap_report",           "gap_report.json"),
        ("key_papers",           "key_papers.json"),
        ("references",           "references.json"),
        ("resource_constraints", "resource_constraints.json"),
        ("research_frontier",    "research_frontier.json"),
        ("domain",               "domain.json"),
    ]

    written = 0
    for key, filename in split_keys:
        value = payload.get(key)
        if value is None:
            continue
        out_file = input_dir / filename
        out_file.write_text(
            json.dumps(value, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        written += 1
    log.info(f"写入 {written}/8 个 planning input 文件到 {input_dir}")
    return input_dir


def _adapt_stages_to_writing(stage1_out: Path, stage2_out: Path, stage3_out: Path, stage4_out: Path) -> Path:
    """把 stage1/2/3 产物聚合成 writing 期望的 m1/m2/m3 三个 JSON（写到 stage4_out/_input/）。

    writing/scripts/stage1_load_inputs.py 期望 m1/m2/m3 三个 JSON 路径，schema：
        m1 = {research_question, hypotheses, key_papers, gap_report, research_frontier, domain, ...}
            （7 字段顶层 dict——直接复用 conception_output.json）
        m2 = {method_design: {...}, experiment_plan: {...}}
            （按 user 选择仅 2 个键，不含 data_plan/budget_report/execution_config）
        m3 = {experiment_results: {key_findings, tables, figures, statistics,
                                   experiment_setup, result_analysis, ...}}
            （experiment_setup / result_analysis 从 stage3.experiment_results 合成——
             experiment 产物的 ExperimentResults schema 仅有 experiment_runs/analysis_records，
             不含 writing 期望的 experiment_setup/result_analysis 字段）

    Args:
        stage1_out: stage1_conception 目录（含 conception_output.json）
        stage2_out: stage2_planning 目录（含 method_design.json + experiment_plan.json）
        stage3_out: stage3_experiment 目录（含 outputs/experiment-module-output.json）
        stage4_out: stage4_writing 目录；适配产物写到 stage4_out/_input/

    Returns:
        适配产物目录路径（stage4_out/_input/），含 m1.json / m2.json / m3.json。
    """
    # Compatibility wrapper only.  The old implementation below predates
    # content-addressed stage-three run directories and loses execution
    # evidence, so every caller must enter the authoritative adapter first.
    from scripts._writing_adapter import prepare_writing_inputs
    prepared = prepare_writing_inputs(
        stage1_out,
        stage2_out,
        stage3_out,
        stage4_out,
        base_dir=Path.cwd(),
    )
    return Path(prepared["m1"]).parent

    # Retained temporarily for source-history readability; unreachable by
    # design.  Remove in the next compatibility-breaking cleanup.
    input_dir = stage4_out / "_input"
    input_dir.mkdir(parents=True, exist_ok=True)

    # ── m1: 直接拷贝 conception_output.json（顶层已含 7 字段）──
    src_m1 = stage1_out / "conception_output.json"
    adapter_notes: list[str] = []
    if src_m1.is_file():
        m1_text = src_m1.read_text(encoding="utf-8")
        try:
            m1_for_writing = json.loads(m1_text)
            m1_payload = (
                m1_for_writing.get("partial")
                if isinstance(m1_for_writing, dict)
                and isinstance(m1_for_writing.get("partial"), dict)
                else m1_for_writing
            )
            valid_writing_domains = {
                "agent", "cv", "graph", "kg", "multimodal", "nlp",
                "other", "recsys", "rl", "speech",
            }
            domain = m1_payload.get("domain") if isinstance(m1_payload, dict) else None
            if isinstance(domain, dict) and domain.get("domain_name") not in valid_writing_domains:
                original = domain.get("domain_name")
                domain["domain_name"] = "other"
                adapter_notes.append(
                    f"m1.domain.domain_name: {original!r}->'other'（writing 枚举兼容；原始 stage1 不改）"
                )
            m1_text = json.dumps(m1_for_writing, ensure_ascii=False, indent=2)
        except (TypeError, ValueError):
            pass
        (input_dir / "m1.json").write_text(m1_text, encoding="utf-8")
        m1_keys = []
        try:
            m1_data = json.loads(src_m1.read_text(encoding="utf-8"))
            payload = m1_data.get("partial") if isinstance(m1_data.get("partial"), dict) else m1_data
            if isinstance(payload, dict):
                m1_keys = sorted(payload.keys())
        except Exception:
            pass
        log.info(f"  m1 ← {src_m1} ({(input_dir / 'm1.json').stat().st_size}B, keys={m1_keys})")
    else:
        log.warning(f"  ⚠️ m1 缺失: {src_m1} 不存在，writing 会 load_inputs_failed")
        # 写个空 dict 占位（让 writing 子进程能解析 + 给清晰错误）
        (input_dir / "m1.json").write_text("{}", encoding="utf-8")

    # ── m2: 合并 method_design.json + experiment_plan.json 为顶层 dict ──
    m2: dict[str, Any] = {}
    for key, fname in [
        ("method_design",   "method_design.json"),
        ("experiment_plan", "experiment_plan.json"),
    ]:
        p = stage2_out / fname
        if p.is_file():
            try:
                m2[key] = json.loads(p.read_text(encoding="utf-8"))
                log.info(f"  m2.{key} ← {p} ({p.stat().st_size}B)")
            except (OSError, json.JSONDecodeError) as e:
                log.warning(f"  ⚠️ m2.{key} 解析失败: {e}")
        else:
            log.warning(f"  ⚠️ m2.{key} 缺失: {p} 不存在")
    (input_dir / "m2.json").write_text(
        json.dumps(m2, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    log.info(f"  m2 → {(input_dir / 'm2.json').stat().st_size}B (keys={sorted(m2.keys())})")

    # ── m3: 从 stage3.experiment_results 合成 ──
    src_m3 = stage3_out / "outputs" / "experiment-module-output.json"
    m3_payload: dict[str, Any] = {"experiment_results": {}}
    if src_m3.is_file():
        try:
            data = json.loads(src_m3.read_text(encoding="utf-8"))
            # experiment-module-output.json 顶层包 experiment_results 子树
            er = data.get("experiment_results") if isinstance(data, dict) else None
            if not isinstance(er, dict):
                # 老式 / 退化：experiment_results 直接平铺在顶层
                er = data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError) as e:
            log.warning(f"  ⚠️ m3 src 解析失败: {e}，m3 留空")
            er = {}
        if isinstance(er, dict):
            m3_payload["experiment_results"] = {
                # 透传已有字段
                "key_findings": list(er.get("key_findings") or []),
                "tables":       dict(er.get("tables") or {}),
                "figures":      dict(er.get("figures") or {}),
                "statistics":   dict(er.get("statistics") or {}),
                # 合成 experiment_setup：从 experiment_runs[].command 拼出来
                "experiment_setup": _synthesize_experiment_setup(er),
                # 合成 result_analysis：从 analysis_records[].summary 拼出来
                "result_analysis":  _synthesize_result_analysis(er),
            }
            log.info(
                f"  m3 ← {src_m3} ({(input_dir / 'm3.json').stat().st_size if False else src_m3.stat().st_size}B)"
            )
            log.info(
                f"    key_findings={len(m3_payload['experiment_results']['key_findings'])}, "
                f"tables={len(m3_payload['experiment_results']['tables'])}, "
                f"figures={len(m3_payload['experiment_results']['figures'])}, "
                f"statistics={len(m3_payload['experiment_results']['statistics'])}, "
                f"experiment_setup={len(m3_payload['experiment_results']['experiment_setup'])} chars, "
                f"result_analysis={len(m3_payload['experiment_results']['result_analysis'])} chars"
            )
    else:
        log.warning(f"  ⚠️ m3 src 缺失: {src_m3} 不存在，m3 留空（writing 会 load_inputs_failed）")
    (input_dir / "m3.json").write_text(
        json.dumps(m3_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    log.info(f"  m3 → {(input_dir / 'm3.json').stat().st_size}B")

    # writing 契约要求 m2.metrics 与 m3.statistics 的键逐字一致，且每条
    # success_criteria 都含显式比较符/阈值。模块二保存的是展示层聚合名，模块三
    # 输出的是逐运行指标名；仅在 writing 输入副本中对齐，原始 m2/m3 均保留。
    er_for_writing = m3_payload.get("experiment_results", {})
    statistic_keys = list((er_for_writing.get("statistics") or {}).keys())
    plan_for_writing = m2.get("experiment_plan") if isinstance(m2, dict) else None
    if isinstance(plan_for_writing, dict) and statistic_keys:
        original_metrics = list(plan_for_writing.get("metrics") or [])
        plan_for_writing["metrics"] = statistic_keys
        adapter_notes.append(
            f"m2.experiment_plan.metrics: {original_metrics!r}->{statistic_keys!r}（与真实 m3 统计键对齐）"
        )
        metric_aliases = {
            "macro_f1_mean": "macro_f1",
            "macro_f1_std": "macro_f1",
            "macro_f1_range": "macro_f1",
            "accuracy_mean": "accuracy",
            "accuracy_std": "accuracy",
            "train_time_mean": "train_time_seconds",
            "inference_time_mean": "inference_time_seconds",
            "total_time_mean": "total_time_seconds",
        }
        baselines = plan_for_writing.get("baselines") or []
        for baseline in baselines:
            if isinstance(baseline, dict):
                name = str(baseline.get("metric_name") or "")
                baseline["metric_name"] = metric_aliases.get(name, name)
        criteria = plan_for_writing.get("success_criteria") or []
        kept_criteria = []
        for criterion in criteria:
            text = criterion.get("criterion") if isinstance(criterion, dict) else criterion
            if isinstance(text, str) and any(op in text for op in (">=", "<=", "≥", "≤", ">", "<", "=")):
                rewritten = text
                for source, target in sorted(metric_aliases.items(), key=lambda item: -len(item[0])):
                    rewritten = rewritten.replace(source, target)
                kept_criteria.append(
                    {**criterion, "criterion": rewritten}
                    if isinstance(criterion, dict)
                    else rewritten
                )
        if kept_criteria:
            removed_count = len(criteria) - len(kept_criteria)
            plan_for_writing["success_criteria"] = kept_criteria
            adapter_notes.append(
                f"m2.experiment_plan.success_criteria: 保留 {len(kept_criteria)} 条显式量化判据，"
                f"排除 {removed_count} 条流程/完整性要求（原始 stage2 不改）"
            )
        (input_dir / "m2.json").write_text(
            json.dumps(m2, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    (input_dir / "writing-adapter-report.json").write_text(
        json.dumps({"notes": adapter_notes}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return input_dir


def _synthesize_experiment_setup(er: dict) -> str:
    """从 experiment_runs[].command + method + status 拼成多行自然语言描述。

    writing _check_m3 校验 m3.experiment_results.experiment_setup 非空字符串（§4.15）。
    """
    runs = er.get("experiment_runs") or []
    if not isinstance(runs, list) or not runs:
        # 兜底：直接从 key_findings 推断
        return "实验由 paper-gen experiment stage 自动执行；具体执行命令详见 stage3_experiment/outputs/ 下原始产物。"
    lines: list[str] = []
    for i, r in enumerate(runs[:8], 1):  # 限 8 条避免太长
        if not isinstance(r, dict):
            continue
        expt_id = r.get("experiment_id") or r.get("run_record_id") or f"run-{i}"
        method = r.get("method") or "?"
        status = r.get("status") or "?"
        command = (r.get("command") or "").strip()
        dataset = r.get("dataset") or ""
        line = f"[{expt_id}] method={method} status={status} dataset={dataset}"
        if command:
            line += f" cmd={command[:200]}"
        lines.append(line)
    if not lines:
        return "experiment_runs 字段非 list/dict，无法合成 setup。"
    return "实验设置：\n" + "\n".join(lines)


def _synthesize_result_analysis(er: dict) -> str:
    """从 analysis_records[].summary + key_findings 拼成多行自然语言结论。

    writing _check_m3 校验 m3.experiment_results.result_analysis 非空字符串（§4.16）。
    """
    analyses = er.get("analysis_records") or []
    parts: list[str] = []
    if isinstance(analyses, list) and analyses:
        for i, a in enumerate(analyses[:8], 1):
            if not isinstance(a, dict):
                continue
            method = a.get("method_name") or "?"
            summary = (a.get("summary") or "").strip()
            if summary:
                parts.append(f"分析 {i} ({method}): {summary[:400]}")
    # 兜底：用 key_findings 凑数
    if not parts:
        kf = er.get("key_findings") or []
        if isinstance(kf, list) and kf:
            for i, s in enumerate(kf[:6], 1):
                parts.append(f"关键发现 {i}: {str(s)[:400]}")
    if not parts:
        return "experiment_results 字段无 analysis_records/key_findings，无法合成 analysis。"
    return "结果分析：\n" + "\n".join(parts)


# ── stage wrappers（thin layer over _stage_runner）──


async def _run_conception_stage(input_dir: Path, output_dir: Path) -> dict:
    from scripts._stage_runner import run_conception_stage
    return await run_conception_stage(input_dir, output_dir)


async def _run_planning_stage(input_dir: Path, output_dir: Path, feedback_file: Path | None = None) -> dict:
    from scripts._stage_runner import run_planning_stage
    return await run_planning_stage(input_dir, output_dir, feedback_file)


async def _run_experiment_stage(planning_dir: Path, output_dir: Path) -> dict:
    from scripts._stage_runner import run_experiment_stage
    return await run_experiment_stage(planning_dir, output_dir)


async def _run_writing_stage(
    *, m1_path: Path, m2_path: Path, m3_path: Path, source_manifest: Path, output_dir: Path, dry_run: bool = False,
) -> dict:
    """Stage 4: writing 子进程 wrapper（3 JSON 输入 + status 协议转换）。"""
    from scripts._stage_runner import run_writing_stage
    return await run_writing_stage(
        m1_path=m1_path, m2_path=m2_path, m3_path=m3_path,
        source_manifest=source_manifest, output_dir=output_dir, dry_run=dry_run,
    )


async def run_writing_from_stages(args: dict) -> dict:
    """Run only the new fourth stage from completed stage directories.

    This reproducible entry point never re-executes or mutates stages 1--3.
    """
    output_dir = Path(args["output_dir"])
    output_dir.mkdir(parents=True, exist_ok=True)
    started_at = time.time()
    stage4_out = output_dir / "stage4_writing"
    summary: dict[str, Any] = {
        "input_mode": "completed_stage_outputs",
        "output_dir": str(output_dir),
        "stages": {},
        "cleanup_policy": "delete_failed_or_superseded_attempt_artifacts; retain_only_fingerprint_verified_dataset_cache",
        "cleanup_actions": [],
    }
    prepared: dict[str, str] = {}
    try:
        from scripts._writing_adapter import prepare_writing_inputs
        prepared = prepare_writing_inputs(
            args["stage1_dir"], args["stage2_dir"], args["stage3_dir"], stage4_out,
            base_dir=Path.cwd(),
        )
        stage4 = await _run_writing_stage(
            m1_path=Path(prepared["m1"]),
            m2_path=Path(prepared["m2"]),
            m3_path=Path(prepared["m3"]),
            source_manifest=Path(prepared["source_manifest"]),
            output_dir=stage4_out,
            dry_run=bool(args.get("dry_run", False)),
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        stage4 = {"summary": {
            "status": "error",
            "errors": [f"writing preparation failed: {exc}"],
            "warnings": [],
            "artifacts": [],
        }}
    stage4["summary"]["cached"] = False
    release = _assess_writing_release(stage4_out, stage4["summary"])
    release_path = stage4_out / "publication_eligibility.json"
    _write_json_atomic(release_path, release)
    stage4["summary"]["publication_eligibility"] = release
    stage4["summary"].setdefault("artifacts", []).append(str(release_path))
    stage_status = stage4["summary"].get("status")
    if release["eligible"]:
        end_status = "complete"
    elif stage_status in {"error", "aborted", "preflight_failed", "load_inputs_failed"}:
        end_status = "aborted_writing"
    else:
        end_status = "blocked_writing_release"
    summary["stages"] = {"writing": stage4["summary"]}
    if prepared.get("report"):
        summary["writing_adapter_report"] = prepared["report"]
    if end_status == "aborted_writing":
        _cleanup_failed_directory(stage4_out, output_dir, summary, "failed standalone writing")
    return _finalize(summary, end_status, started_at)


# ── run_summary 写盘 ──


def _cleanup_failed_directory(
    path: Path,
    output_root: Path,
    summary: dict[str, Any],
    reason: str,
) -> None:
    """Delete one failed/superseded run directory without keeping a backup.

    Only descendants of this invocation's output root may be removed. Reusable
    datasets live in the separate fingerprint-verified cache and are therefore
    unaffected. A compact cleanup ledger remains in ``run_summary.json``.
    """
    root = output_root.resolve()
    target = path.resolve()
    if target == root or root not in target.parents:
        raise ValueError(f"refusing to clean path outside run output: {target}")
    if not target.exists():
        return
    bytes_before = _path_size(target)
    if target.is_dir():
        shutil.rmtree(target)
    else:
        target.unlink()
    summary.setdefault("cleanup_actions", []).append({
        "path": str(target),
        "reason": reason,
        "bytes_removed": bytes_before,
        "backup_created": False,
    })
    log.info("清理失败/作废产物: %s (%d bytes, reason=%s)", target, bytes_before, reason)


def _path_size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    total = 0
    for item in path.rglob("*"):
        try:
            if item.is_file() and not item.is_symlink():
                total += item.stat().st_size
        except OSError:
            continue
    return total


def _finalize(summary: dict, end_status: str, started_at: float) -> dict:
    summary["status"] = end_status
    summary["finished_at"] = _iso8601(time.time())
    summary["wall_time_seconds"] = round(time.time() - started_at, 2)
    summary["resource_usage"] = _resource_usage_summary(summary)
    out_path = Path(summary["output_dir"]) / "run_summary.json"
    _write_json_atomic(out_path, summary)
    _write_stage_metrics(summary)
    log.info(f"paper-gen 终态: {end_status}, wall={summary['wall_time_seconds']}s, "
                   f"summary={out_path}")
    usage = summary["resource_usage"]["llm_tokens_all_stages"]
    log.info(
        "paper-gen LLM 总消耗: requests=%d, prompt=%d, completion=%d, total=%d; "
        "current-run stage wall=%.2fs",
        usage["request_count"], usage["prompt_tokens"], usage["completion_tokens"],
        usage["total_tokens"], summary["resource_usage"]["current_run_stage_wall_time_seconds"],
    )
    return summary


def _write_json_atomic(path: Path, data: dict) -> None:
    """原子写盘（tmp → os.replace）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp.replace(path)


def _iso8601(t: float) -> str:
    import datetime as _dt
    return _dt.datetime.fromtimestamp(t, _dt.timezone.utc).isoformat()
