"""Thin JiuwenSwarm Tool adapters for the paper-gen-agent pipeline.

Each tool wraps one stage as a subprocess (or, where the stage is already
an agent_template, as a JSON-RPC invocation). Tools are intentionally
dumb: they accept a request dict, hand it to a deterministic backend, and
return whatever the backend produced. The root Agent does all routing and
state mutation on top of these primitives.

Convention
----------
- Every invoke tool accepts the same envelope::

    {
      "run_id": str,
      "stage_id": "conception" | "planning" | "experiment" | "writing",
      "run_dir": str (abs path),
      "input": dict (stage-specific),
      "previous_stage_outputs": {"conception": "...", "planning": "..."},
      "iteration_context": {"rounds": int, "max_rounds": int, "last_feedback": str|None}
    }

- Every invoke tool returns the same shape::

    {
      "status": "completed" | "replan" | "revise" | "aborted" | "failed",
      "output_dir": str,
      "summary": str,
      "artifacts": dict,
      "next_action": {
        "type": "proceed" | "replan_to:<stage>" | "abort",
        "target_stage": str,
        "reason": str
      }
    }

State tools (read/update) work on a single file: run_dir/pipeline_state.json.
They use write-temp + rename for atomicity so a crash mid-update never
leaves the file half-written.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from openjiuwen.core.foundation.tool import Tool, ToolCard


# ---------------------------------------------------------------------------
# Workspace path resolution
# ---------------------------------------------------------------------------

def _workspace_root() -> Path:
    """Locate the runtime workspace root.

    Mirrors the trust model used by experiment-agent's tools: prefer the
    JIUWENSWARM_DATA_DIR env var if it points to a real .jiuwenswarm-project
    directory; otherwise fall back to the agent's cwd.
    """
    runtime = Path.cwd().resolve()
    configured = os.environ.get("JIUWENSWARM_DATA_DIR", "").strip()
    if not configured:
        return runtime
    data_root = Path(configured).resolve()
    if data_root.name != ".jiuwenswarm-project":
        return runtime
    project_root = data_root.parent.resolve()
    if data_root != project_root / ".jiuwenswarm-project":
        return runtime
    return project_root


def _abs_under_root(value: Any) -> Path:
    """Resolve a user-supplied path safely under the workspace root."""
    root = _workspace_root()
    candidate = Path(str(value))
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve()
    if resolved != root and not resolved.is_relative_to(root):
        raise ValueError(f"path is outside the trusted workspace: {value}")
    return resolved


# ---------------------------------------------------------------------------
# Subprocess helper
# ---------------------------------------------------------------------------

def _run_subprocess(
    command: list[str],
    *,
    input_payload: dict[str, Any] | None = None,
    timeout: int = 600,
) -> dict[str, Any]:
    """Run a subprocess and parse its JSON stdout."""
    completed = subprocess.run(
        command,
        cwd=str(_workspace_root()),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        input=(
            json.dumps(input_payload, ensure_ascii=False)
            if input_payload is not None
            else None
        ),
        shell=False,
        check=False,
        timeout=timeout,
    )
    if completed.returncode != 0 and not completed.stdout.strip():
        return {
            "status": "failed",
            "error": "subprocess returned non-zero with empty stdout",
            "return_code": completed.returncode,
            "stderr": completed.stderr[-4000:],
        }
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        return {
            "status": "failed",
            "error": f"subprocess returned invalid JSON: {exc}",
            "return_code": completed.returncode,
            "stdout": completed.stdout[-4000:],
            "stderr": completed.stderr[-4000:],
        }


def _run_status_subprocess(
    command: list[str],
    status_file: Path,
    *,
    timeout: int,
) -> dict[str, Any]:
    """Run a file-protocol stage and return its authoritative status.json."""
    completed = subprocess.run(
        command,
        cwd=str(_workspace_root()),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
        check=False,
        timeout=timeout,
    )
    try:
        payload = json.loads(status_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        payload = {
            "status": "error",
            "errors": ["stage did not produce a valid status.json"],
            "artifacts": [],
        }
    payload["return_code"] = completed.returncode
    if completed.stderr:
        payload["stderr_tail"] = completed.stderr[-4000:]
    return payload


# ---------------------------------------------------------------------------
# Backend path discovery
# ---------------------------------------------------------------------------

def _find_skill_entry(skills_subdir: str) -> Path:
    """Locate workspace/skills/<subdir>/scripts/main.py."""
    current = Path(__file__).resolve()
    for parent in current.parents:
        candidate = parent / "skills" / skills_subdir / "scripts" / "main.py"
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        f"cannot locate workspace/skills/{skills_subdir}/scripts/main.py"
    )


def _load_paper_gen_runtime_module(filename: str, module_name: str):
    """Load one paper-gen runtime module from this exact workspace.

    Jiuwen can keep several workspace copies on ``sys.path``. A regular
    ``import scripts...`` may therefore bind to an older installed copy. Loading
    by absolute file path keeps the root Agent and deterministic CLI on the same
    implementation while preserving the runtime modules' flat-import fallback.
    """
    cached = sys.modules.get(module_name)
    if cached is not None:
        return cached
    scripts_dir = _find_skill_entry("paper-gen").parent
    module_path = scripts_dir / filename
    if not module_path.is_file():
        raise FileNotFoundError(f"paper-gen runtime module missing: {module_path}")
    scripts_text = str(scripts_dir)
    if scripts_text not in sys.path:
        sys.path.insert(0, scripts_text)
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load paper-gen runtime module: {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    return module


def _find_agent_main(agent_dir: str) -> Path:
    """Locate workspace/plugins/agent_templates/<agent_dir>/tools/<file>.py main.

    For experiment-agent, we route through its own experiment_control tool
    (via its CLI), not by importing its Python module. This keeps the
    paper-gen-agent decoupled from experiment-agent's internal layout.
    """
    current = Path(__file__).resolve()
    for parent in current.parents:
        candidate = parent / "plugins" / "agent_templates" / agent_dir
        if candidate.is_dir():
            return candidate
    raise FileNotFoundError(
        f"cannot locate workspace/plugins/agent_templates/{agent_dir}"
    )


# ---------------------------------------------------------------------------
# Envelope construction
# ---------------------------------------------------------------------------

def _build_stage_envelope(
    raw: dict[str, Any],
    stage_id: str,
) -> dict[str, Any]:
    """Validate the canonical envelope and stamp the stage_id."""
    required = ("run_id", "run_dir", "input")
    missing = [k for k in required if not raw.get(k)]
    if missing:
        raise ValueError(f"{stage_id} invoke requires: {missing}")
    return {
        "run_id": raw["run_id"],
        "stage_id": stage_id,
        "run_dir": str(_abs_under_root(raw["run_dir"])),
        "input": raw["input"],
        "previous_stage_outputs": raw.get("previous_stage_outputs", {}),
        "iteration_context": raw.get(
            "iteration_context",
            {"rounds": 0, "max_rounds": 1, "last_feedback": None},
        ),
    }


def _write_replan_feedback(run_dir: Path, stage: str, feedback: dict[str, Any]) -> Path:
    """Persist structured feedback for the target stage's next invocation."""
    path = run_dir / "feedback" / f"replan_to_{stage}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(feedback, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return path


_TOKEN_KEYS = ("request_count", "prompt_tokens", "completion_tokens", "total_tokens")
_PIPELINE_STAGES = ("conception", "planning", "experiment", "writing")


def _empty_token_totals() -> dict[str, int]:
    return {key: 0 for key in _TOKEN_KEYS}


def _token_totals(payload: Any) -> tuple[dict[str, int], bool, str]:
    """Normalize the token block used by every stage status protocol."""
    usage = payload.get("llm_token_usage") if isinstance(payload, dict) else None
    total = usage.get("total") if isinstance(usage, dict) else None
    reported = isinstance(total, dict)
    normalized = _empty_token_totals()
    if reported:
        for key in _TOKEN_KEYS:
            try:
                normalized[key] = max(int(total.get(key) or 0), 0)
            except (TypeError, ValueError):
                normalized[key] = 0
        # Providers occasionally omit total_tokens while returning both sides.
        if not normalized["total_tokens"]:
            normalized["total_tokens"] = (
                normalized["prompt_tokens"] + normalized["completion_tokens"]
            )
    source = str(usage.get("source") or "stage status") if isinstance(usage, dict) else "unreported"
    return normalized, reported, source


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _record_stage_token_usage(
    run_dir: Path,
    stage: str,
    stage_dir: Path,
    result_status: str,
    stage_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Append one stage attempt and rebuild per-module/full-pipeline totals."""
    summary = stage_summary if isinstance(stage_summary, dict) else {}
    status_payload = _read_json_object(stage_dir / "status.json")
    usage_source_payload = summary
    _, summary_reported, _ = _token_totals(summary)
    if not summary_reported:
        usage_source_payload = status_payload
    totals, reported, source = _token_totals(usage_source_payload)

    path = run_dir / "token_usage.json"
    existing = _read_json_object(path)
    attempts = existing.get("attempts") if isinstance(existing.get("attempts"), list) else []
    sequence = max(
        [int(item.get("sequence") or 0) for item in attempts if isinstance(item, dict)] or [0]
    ) + 1
    # A new attempt for one stage supersedes its previous accepted attempt.
    # If experiment asks for REPLAN, the planning attempt that produced that
    # invalid experiment contract is also reclassified as REPLAN cost.
    for item in attempts:
        if isinstance(item, dict) and item.get("stage") == stage and item.get("status") == "completed":
            item["invalidated"] = True
            item["outcome_reason"] = "superseded_by_later_attempt"
    if stage == "experiment" and result_status == "replan":
        planning_candidates = [
            item for item in attempts
            if isinstance(item, dict)
            and item.get("stage") == "planning"
            and item.get("status") == "completed"
            and not item.get("invalidated")
        ]
        if planning_candidates:
            invalidated = max(planning_candidates, key=lambda item: int(item.get("sequence") or 0))
            invalidated["invalidated"] = True
            invalidated["outcome_reason"] = "invalidated_by_experiment_replan"
    attempt = {
        "sequence": sequence,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "stage": stage,
        "status": result_status,
        "stage_dir": str(stage_dir),
        "reported_by_provider": reported,
        "source": source,
        "invalidated": False,
        "outcome_reason": "accepted_stage_result" if result_status == "completed" else result_status,
        **totals,
    }
    attempts.append(attempt)

    modules: dict[str, dict[str, Any]] = {}
    pipeline_total = _empty_token_totals()
    successful_total = _empty_token_totals()
    unsuccessful_total = _empty_token_totals()
    missing_usage_attempts = 0
    for module in _PIPELINE_STAGES:
        module_attempts = [
            item for item in attempts
            if isinstance(item, dict) and item.get("stage") == module
        ]
        module_total = _empty_token_totals()
        module_successful = _empty_token_totals()
        module_unsuccessful = _empty_token_totals()
        for item in module_attempts:
            if not item.get("reported_by_provider"):
                missing_usage_attempts += 1
            for key in _TOKEN_KEYS:
                value = max(int(item.get(key) or 0), 0)
                module_total[key] += value
                pipeline_total[key] += value
                if item.get("status") == "completed" and not item.get("invalidated"):
                    module_successful[key] += value
                    successful_total[key] += value
                else:
                    module_unsuccessful[key] += value
                    unsuccessful_total[key] += value
            item["cost_bucket"] = (
                "successful_path"
                if item.get("status") == "completed" and not item.get("invalidated")
                else "replan_or_failed"
            )
        modules[module] = {
            "attempt_count": len(module_attempts),
            "successful_path": module_successful,
            "replan_or_failed": module_unsuccessful,
            **module_total,
        }

    full_pipeline_completed = all(
        any(
            isinstance(item, dict)
            and item.get("stage") == stage
            and item.get("cost_bucket") == "successful_path"
            for item in attempts
        )
        for stage in _PIPELINE_STAGES
    )

    payload = {
        "schema_version": 1,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "modules": modules,
        "successful_path": {
            "full_pipeline_completed": full_pipeline_completed,
            "provisional": not full_pipeline_completed,
            "total": successful_total,
            "note": (
                "These are the currently accepted stage attempts. They become the true "
                "successful full-pipeline cost only when full_pipeline_completed is true."
            ),
        },
        "replan_or_failed": {
            "total": unsuccessful_total,
            "note": "Includes REPLAN attempts, direct failures, and completed attempts superseded later.",
        },
        "overall_total": pipeline_total,
        "total": pipeline_total,
        "coverage": {
            "attempt_count": len(attempts),
            "reported_attempt_count": len(attempts) - missing_usage_attempts,
            "missing_usage_attempt_count": missing_usage_attempts,
            "complete": missing_usage_attempts == 0,
            "note": (
                "Total includes every recorded stage attempt, including failed runs and REPLAN retries. "
                "A provider that omits usage is recorded as missing, never guessed."
            ),
        },
        "attempts": attempts,
    }
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    _write_token_usage_markdown(run_dir, payload)
    return {**totals, "reported_by_provider": reported, "source": source, "path": str(path)}


def _write_token_usage_markdown(run_dir: Path, payload: dict[str, Any]) -> None:
    lines = [
        "# Token usage",
        "",
        "| Module | Attempts | Successful-path tokens | REPLAN/failed tokens | Overall tokens |",
        "|---|---:|---:|---:|---:|",
    ]
    modules = payload.get("modules") or {}
    for stage in _PIPELINE_STAGES:
        item = modules.get(stage) or {}
        lines.append(
            f"| {stage} | {int(item.get('attempt_count') or 0)} | "
            f"{int((item.get('successful_path') or {}).get('total_tokens') or 0)} | "
            f"{int((item.get('replan_or_failed') or {}).get('total_tokens') or 0)} | "
            f"{int(item.get('total_tokens') or 0)} |"
        )
    total = payload.get("overall_total") or payload.get("total") or {}
    successful = ((payload.get("successful_path") or {}).get("total") or {})
    unsuccessful = ((payload.get("replan_or_failed") or {}).get("total") or {})
    coverage = payload.get("coverage") or {}
    lines.extend([
        f"| **TOTAL** | **{int(coverage.get('attempt_count') or 0)}** | "
        f"**{int(successful.get('total_tokens') or 0)}** | "
        f"**{int(unsuccessful.get('total_tokens') or 0)}** | "
        f"**{int(total.get('total_tokens') or 0)}** |",
        "",
        f"Full pipeline completed: "
        f"{'yes' if (payload.get('successful_path') or {}).get('full_pipeline_completed') else 'no'}.",
        "Successful-path tokens are provisional until all four modules complete.",
        "",
        "## Detailed totals",
        "",
        f"- Successful path: requests={int(successful.get('request_count') or 0)}, "
        f"prompt={int(successful.get('prompt_tokens') or 0)}, "
        f"completion={int(successful.get('completion_tokens') or 0)}, "
        f"total={int(successful.get('total_tokens') or 0)}",
        f"- REPLAN/failed: requests={int(unsuccessful.get('request_count') or 0)}, "
        f"prompt={int(unsuccessful.get('prompt_tokens') or 0)}, "
        f"completion={int(unsuccessful.get('completion_tokens') or 0)}, "
        f"total={int(unsuccessful.get('total_tokens') or 0)}",
        f"- Overall: requests={int(total.get('request_count') or 0)}, "
        f"prompt={int(total.get('prompt_tokens') or 0)}, "
        f"completion={int(total.get('completion_tokens') or 0)}, "
        f"total={int(total.get('total_tokens') or 0)}",
        "",
        f"Coverage complete: {'yes' if coverage.get('complete') else 'no'}; "
        f"missing usage attempts: {int(coverage.get('missing_usage_attempt_count') or 0)}.",
        "",
        "The total includes failed attempts and REPLAN retries. Missing provider usage is not estimated.",
        "",
    ])
    path = run_dir / "token_usage_report.md"
    tmp = path.with_suffix(".md.tmp")
    tmp.write_text("\n".join(lines), encoding="utf-8")
    os.replace(tmp, path)


def _attach_token_usage(
    result: dict[str, Any],
    *,
    run_dir: Path,
    stage: str,
    stage_dir: Path,
    stage_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    usage = _record_stage_token_usage(
        run_dir, stage, stage_dir, str(result.get("status") or "unknown"), stage_summary
    )
    result["token_usage"] = usage
    artifacts = result.get("artifacts")
    if not isinstance(artifacts, dict):
        artifacts = {"files": artifacts or []}
        result["artifacts"] = artifacts
    artifacts["token_usage"] = usage["path"]
    artifacts["token_usage_report"] = str(run_dir / "token_usage_report.md")
    return result


# ---------------------------------------------------------------------------
# Base tool
# ---------------------------------------------------------------------------

class _InvokeBase(Tool):
    """Shared envelope handling for the 4 invoke tools."""

    stage_id: str = ""
    agent_name = "paper-gen-agent"

    async def _run(self, inputs: dict[str, Any], command: list[str]) -> dict[str, Any]:
        try:
            envelope = _build_stage_envelope(inputs, self.stage_id)
        except ValueError as exc:
            return {
                "status": "failed",
                "error": str(exc),
                "output_dir": None,
                "summary": "invalid envelope",
                "artifacts": {},
                "next_action": {
                    "type": "abort",
                    "target_stage": self.stage_id,
                    "reason": str(exc),
                },
            }
        return await asyncio.to_thread(
            _run_subprocess, command, input_payload=envelope,
        )


# ---------------------------------------------------------------------------
# Stage 1: conception
# ---------------------------------------------------------------------------

class InvokeConceptionSubagentTool(_InvokeBase):
    """Invoke the conception sub-agent.

    The adapter invokes the conception Skill's real file-based CLI.  That
    Skill creates a native Jiuwen research_agent internally; this adapter only
    materializes inputs and translates its artifacts into the pipeline
    envelope.
    """

    stage_id = "conception"
    tool_name = "invoke_conception_subagent"

    def __init__(self) -> None:
        super().__init__(
            ToolCard(
                id=self.tool_name,
                name=self.tool_name,
                parallel_safe=False,
                description=(
                    "Invoke the conception sub-agent (stage 1). Translates a "
                    "user research direction into a structured research "
                    "definition (research_question / hypotheses / gap_report / "
                    "key_papers / references / research_frontier / domain / "
                    "resource_constraints)."
                ),
                input_params={
                    "type": "object",
                    "required": ["run_id", "run_dir", "input"],
                    "additionalProperties": False,
                    "properties": {
                        "run_id": {"type": "string"},
                        "run_dir": {"type": "string"},
                        "input": {"type": "object"},
                        "previous_stage_outputs": {"type": "object"},
                        "iteration_context": {"type": "object"},
                    },
                },
            )
        )

    async def invoke(self, inputs: dict[str, Any], **kwargs) -> dict[str, Any]:
        try:
            entry = _find_skill_entry("conception")
        except FileNotFoundError as exc:
            return self._envelope_error(exc)
        try:
            envelope = _build_stage_envelope(inputs, self.stage_id)
        except ValueError as exc:
            return {
                "status": "failed", "output_dir": None,
                "summary": "invalid conception envelope", "artifacts": {},
                "next_action": {
                    "type": "abort", "target_stage": "conception", "reason": str(exc),
                },
            }

        run_dir = Path(envelope["run_dir"])
        stage_dir = run_dir / "stage1_conception"
        input_dir = stage_dir / "_input"
        input_dir.mkdir(parents=True, exist_ok=True)
        request_file = input_dir / "00_user_request.json"
        temp_request = request_file.with_suffix(".json.tmp")
        temp_request.write_text(
            json.dumps(envelope["input"], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temp_request.replace(request_file)

        status_file = stage_dir / "status.json"
        progress_file = stage_dir / "progress.json"
        command = [
            sys.executable, str(entry),
            "--input-dir", str(input_dir),
            "--output-dir", str(stage_dir),
            "--status-file", str(status_file),
            "--progress-file", str(progress_file),
        ]
        try:
            completed = await asyncio.to_thread(
                subprocess.run,
                command,
                cwd=str(_workspace_root()),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                shell=False,
                check=False,
                timeout=1200,
            )
        except subprocess.TimeoutExpired:
            failed = {
                "status": "failed", "output_dir": str(stage_dir),
                "summary": "conception exceeded 1200 seconds", "artifacts": {},
                "next_action": {
                    "type": "abort", "target_stage": "conception",
                    "reason": "research_agent timeout",
                },
            }
            return _attach_token_usage(
                failed, run_dir=run_dir, stage=self.stage_id, stage_dir=stage_dir,
            )

        output_file = stage_dir / "conception_output.json"
        try:
            result = json.loads(output_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            result = {}
        if completed.returncode != 0 or result.get("status") != "PASS":
            reason = "; ".join(str(item) for item in result.get("errors") or [])
            if not reason:
                reason = completed.stderr[-2000:] or "conception failed without a valid output"
            failed = {
                "status": "failed", "output_dir": str(stage_dir),
                "summary": "conception failed", "artifacts": {},
                "next_action": {
                    "type": "abort", "target_stage": "conception", "reason": reason,
                },
            }
            return _attach_token_usage(
                failed, run_dir=run_dir, stage=self.stage_id, stage_dir=stage_dir,
                stage_summary=_read_json_object(status_file),
            )

        # The planning CLI consumes separate files.  Materialize them beside
        # conception_output.json so the Agent path and deterministic main_flow
        # expose the same boundary contract.
        split_files = {
            "research_question": "research_question.json",
            "hypotheses": "hypotheses.json",
            "gap_report": "gap_report.json",
            "key_papers": "key_papers.json",
            "references": "references.json",
            "resource_constraints": "resource_constraints.json",
            "research_frontier": "research_frontier.json",
            "domain": "domain.json",
        }
        artifacts = {"conception_output": str(output_file)}
        for key, filename in split_files.items():
            if key not in result:
                continue
            target = stage_dir / filename
            target.write_text(
                json.dumps(result[key], ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            artifacts[key] = str(target)
        completed_result = {
            "status": "completed", "output_dir": str(stage_dir),
            "summary": (
                f"conception complete: {len(result.get('references') or [])} "
                "verified references"
            ),
            "artifacts": artifacts,
            "next_action": {
                "type": "proceed", "target_stage": "planning", "reason": "",
            },
        }
        return _attach_token_usage(
            completed_result, run_dir=run_dir, stage=self.stage_id, stage_dir=stage_dir,
            stage_summary=_read_json_object(status_file),
        )

    async def stream(self, inputs, **kwargs):
        yield await self.invoke(inputs, **kwargs)

    def _envelope_error(self, exc: FileNotFoundError) -> dict[str, Any]:
        return {
            "status": "aborted",
            "error": str(exc),
            "output_dir": None,
            "summary": "conception backend not found",
            "artifacts": {},
            "next_action": {
                "type": "abort",
                "target_stage": "conception",
                "reason": "conception skill not installed",
            },
        }


# ---------------------------------------------------------------------------
# Stage 2: planning (delegates to plan-supervisor's call_planning_skill)
# ---------------------------------------------------------------------------

class InvokePlanningSupervisorTool(_InvokeBase):
    """Invoke plan-supervisor (stage 2) as a subprocess.

    Reuses plan-supervisor's tool semantics: input_dir / output_dir /
    feedback_file / passthrough. We translate our envelope into plan-
    supervisor's expected CLI form.
    """

    stage_id = "planning"
    tool_name = "invoke_planning_supervisor"

    def __init__(self) -> None:
        super().__init__(
            ToolCard(
                id=self.tool_name,
                name=self.tool_name,
                parallel_safe=False,
                description=(
                    "Invoke plan-supervisor (stage 2) as a subprocess. "
                    "Produces 5 .json + 5 .md artifacts + status.json in "
                    "output_dir. Accepts REPLAN feedback via envelope."
                ),
                input_params={
                    "type": "object",
                    "required": ["run_id", "run_dir", "input"],
                    "additionalProperties": False,
                    "properties": {
                        "run_id": {"type": "string"},
                        "run_dir": {"type": "string"},
                        "input": {"type": "object"},
                        "previous_stage_outputs": {"type": "object"},
                        "iteration_context": {"type": "object"},
                        "passthrough": {"type": "object"},
                    },
                },
            )
        )

    async def invoke(self, inputs: dict[str, Any], **kwargs) -> dict[str, Any]:
        try:
            entry = _find_skill_entry("planning")
        except FileNotFoundError as exc:
            return self._envelope_error(exc)

        try:
            envelope = _build_stage_envelope(inputs, self.stage_id)
        except ValueError as exc:
            return self._invalid_envelope(exc)

        run_dir = Path(envelope["run_dir"])
        stage_dir = run_dir / "stage2_planning"
        stage_dir.mkdir(parents=True, exist_ok=True)

        # plan-supervisor expects: --input-dir X --output-dir Y [--feedback-file Z] [--status-file Y/status.json]
        command = [
            sys.executable, str(entry),
            "--input-dir", str(run_dir / "stage1_conception"),
            "--output-dir", str(stage_dir),
            "--status-file", str(stage_dir / "status.json"),
            "--progress-file", str(stage_dir / "progress.json"),
            "--no-human-review",
        ]
        feedback_path = run_dir / "feedback" / "replan_to_planning.json"
        if envelope["iteration_context"].get("last_feedback"):
            feedback_path = _write_replan_feedback(
                run_dir,
                "planning",
                envelope["iteration_context"]["last_feedback"],
            )
        if feedback_path.is_file():
            command.extend(["--feedback-file", str(feedback_path)])

        try:
            result = await asyncio.to_thread(
                _run_status_subprocess,
                command,
                stage_dir / "status.json",
                timeout=1800,
            )
        except subprocess.TimeoutExpired:
            result = {
                "status": "error",
                "errors": ["planning exceeded 1800 seconds"],
                "artifacts": [],
            }
        canonical = self._wrap_result(result, stage_dir)
        return _attach_token_usage(
            canonical, run_dir=run_dir, stage=self.stage_id, stage_dir=stage_dir,
            stage_summary=result,
        )

    async def stream(self, inputs, **kwargs):
        yield await self.invoke(inputs, **kwargs)

    def _wrap_result(self, raw: dict[str, Any], stage_dir: Path) -> dict[str, Any]:
        # plan-supervisor returns {status, artifacts, errors, ...}.
        # Translate to the canonical envelope.
        if raw.get("status") == "complete":
            return {
                "status": "completed",
                "output_dir": str(stage_dir),
                "summary": raw.get("summary") or "planning complete",
                "artifacts": raw.get("artifacts", {}),
                "next_action": {"type": "proceed", "target_stage": "experiment", "reason": ""},
            }
        if raw.get("status", "").startswith("replan") or raw.get("status") == "REPLAN":
            return {
                "status": "replan",
                "output_dir": str(stage_dir),
                "summary": "planning requested replan",
                "artifacts": raw.get("artifacts", {}),
                "next_action": {
                    "type": "replan_to:planning",
                    "target_stage": "planning",
                    "reason": raw.get("summary", "replan requested"),
                },
            }
        return {
            "status": "failed",
            "output_dir": str(stage_dir),
            "summary": "planning failed",
            "artifacts": {},
            "next_action": {
                "type": "abort",
                "target_stage": "planning",
                "reason": raw.get("error") or raw.get("stderr", "unknown"),
            },
        }

    def _envelope_error(self, exc: FileNotFoundError) -> dict[str, Any]:
        return {
            "status": "aborted",
            "error": str(exc),
            "output_dir": None,
            "summary": "planning backend not found",
            "artifacts": {},
            "next_action": {
                "type": "abort",
                "target_stage": "planning",
                "reason": "planning skill not installed",
            },
        }

    def _invalid_envelope(self, exc: ValueError) -> dict[str, Any]:
        return {
            "status": "failed",
            "error": str(exc),
            "output_dir": None,
            "summary": "invalid envelope",
            "artifacts": {},
            "next_action": {
                "type": "abort",
                "target_stage": "planning",
                "reason": str(exc),
            },
        }


# ---------------------------------------------------------------------------
# Stage 3: experiment
# ---------------------------------------------------------------------------

class InvokeExperimentSubagentTool(_InvokeBase):
    """Run the real stage-three adapter and experiment Agent chain."""

    stage_id = "experiment"
    tool_name = "invoke_experiment_subagent"

    def __init__(self) -> None:
        super().__init__(
            ToolCard(
                id=self.tool_name,
                name=self.tool_name,
                parallel_safe=False,
                description=(
                    "Invoke the experiment sub-agent (stage 3). Translates "
                    "planning artifacts into reproducible experiment runs "
                    "and returns experiment-module-output.json + artifact "
                    "manifest."
                ),
                input_params={
                    "type": "object",
                    "required": ["run_id", "run_dir", "input"],
                    "additionalProperties": False,
                    "properties": {
                        "run_id": {"type": "string"},
                        "run_dir": {"type": "string"},
                        "input": {"type": "object"},
                        "previous_stage_outputs": {"type": "object"},
                        "iteration_context": {"type": "object"},
                    },
                },
            )
        )

    async def invoke(self, inputs: dict[str, Any], **kwargs) -> dict[str, Any]:
        try:
            envelope = _build_stage_envelope(inputs, self.stage_id)
        except ValueError as exc:
            return self._invalid_envelope(exc)

        run_dir = Path(envelope["run_dir"])
        stage_dir = run_dir / "stage3_experiment"
        stage_dir.mkdir(parents=True, exist_ok=True)

        try:
            stage_runner = _load_paper_gen_runtime_module(
                "_stage_runner.py", "_paper_gen_stage_runner_runtime"
            )
            experiment_output = _load_paper_gen_runtime_module(
                "_experiment_output.py", "_paper_gen_experiment_output_runtime"
            )
        except (FileNotFoundError, ImportError) as exc:
            failed = self._envelope_error(exc)
            failed["output_dir"] = str(stage_dir)
            return _attach_token_usage(
                failed, run_dir=run_dir, stage=self.stage_id, stage_dir=stage_dir,
            )

        try:
            result = await stage_runner.run_experiment_stage(
                run_dir / "stage2_planning",
                stage_dir,
                base_dir=_workspace_root(),
            )
        except Exception as exc:  # SDK/provider faults must not look successful.
            failed = self._failed(stage_dir, f"experiment runtime error: {exc}")
            return _attach_token_usage(
                failed, run_dir=run_dir, stage=self.stage_id, stage_dir=stage_dir,
            )

        summary = result.get("summary") if isinstance(result, dict) else None
        summary = summary if isinstance(summary, dict) else {}
        status = str(summary.get("status") or "error").lower()
        artifacts = {"files": list(summary.get("artifacts") or [])}

        def finish(response: dict[str, Any]) -> dict[str, Any]:
            return _attach_token_usage(
                response,
                run_dir=run_dir,
                stage=self.stage_id,
                stage_dir=stage_dir,
                stage_summary=summary,
            )

        try:
            module_run_dir = experiment_output.resolve_stage3_run_dir(
                stage_dir, _workspace_root()
            )
            payload, read_error = experiment_output.read_experiment_output(module_run_dir)
        except (OSError, ValueError) as exc:
            module_run_dir, payload, read_error = None, None, str(exc)

        if module_run_dir is not None:
            artifacts["run_dir"] = str(module_run_dir)
        if isinstance(payload, dict):
            artifacts["experiment_output"] = payload

        if status == "complete" and isinstance(payload, dict):
            return finish({
                "status": "completed",
                "output_dir": str(stage_dir),
                "summary": "experiment completed with verified module-three output",
                "artifacts": artifacts,
                "next_action": {
                    "type": "proceed", "target_stage": "writing", "reason": ""
                },
            })

        feedback = (
            experiment_output.planning_feedback(payload)
            if isinstance(payload, dict)
            else None
        )
        if summary.get("replan_requested") is True or feedback is not None:
            reason = (feedback or {}).get("reason") or "experiment requested replanning"
            feedback = feedback or summary.get("planning_feedback") or {
                "reason": str(reason),
                "affected_experiment_ids": [],
                "blockers": ["[schema] module three requested replanning without details"],
                "suggested_changes": ["regenerate the incomplete stage-two contracts"],
            }
            feedback_path = _write_replan_feedback(run_dir, "planning", feedback)
            artifacts["planning_feedback"] = feedback
            artifacts["planning_feedback_path"] = str(feedback_path)
            return finish({
                "status": "replan",
                "output_dir": str(stage_dir),
                "summary": str(reason),
                "artifacts": artifacts,
                "next_action": {
                    "type": "replan_to:planning",
                    "target_stage": "planning",
                    "reason": str(reason),
                },
            })

        errors = [str(item) for item in summary.get("errors") or []]
        if read_error:
            errors.append(read_error)
        reason = "; ".join(errors[:8]) or f"experiment ended with status={status}"
        # Contract/projection errors are actionable planning feedback; provider,
        # import and execution crashes are not disguised as scientific REPLANs.
        schema_error = any(
            token in reason.lower()
            for token in ("投影", "contract", "schema", "契约", "valid list", "validation")
        )
        if schema_error:
            feedback = {
                "reason": "stage-two artifacts cannot be projected into module three",
                "affected_experiment_ids": [],
                "blockers": [f"[schema] {item}" for item in errors[:8]],
                "suggested_changes": errors[:8],
            }
            feedback_path = _write_replan_feedback(run_dir, "planning", feedback)
            artifacts["planning_feedback"] = feedback
            artifacts["planning_feedback_path"] = str(feedback_path)
            return finish({
                "status": "replan",
                "output_dir": str(stage_dir),
                "summary": feedback["reason"],
                "artifacts": artifacts,
                "next_action": {
                    "type": "replan_to:planning",
                    "target_stage": "planning",
                    "reason": feedback["reason"],
                },
            })
        return finish(self._failed(stage_dir, reason, artifacts))

    async def stream(self, inputs, **kwargs):
        yield await self.invoke(inputs, **kwargs)

    def _failed(
        self,
        stage_dir: Path,
        reason: str,
        artifacts: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "status": "failed",
            "output_dir": str(stage_dir),
            "summary": "experiment failed",
            "artifacts": artifacts or {},
            "next_action": {
                "type": "abort",
                "target_stage": "experiment",
                "reason": reason,
            },
        }

    def _envelope_error(self, exc: FileNotFoundError) -> dict[str, Any]:
        return {
            "status": "aborted",
            "error": str(exc),
            "output_dir": None,
            "summary": "experiment backend not found",
            "artifacts": {},
            "next_action": {
                "type": "abort",
                "target_stage": "experiment",
                "reason": str(exc),
            },
        }

    def _invalid_envelope(self, exc: ValueError) -> dict[str, Any]:
        return {
            "status": "failed",
            "error": str(exc),
            "output_dir": None,
            "summary": "invalid envelope",
            "artifacts": {},
            "next_action": {
                "type": "abort",
                "target_stage": "experiment",
                "reason": str(exc),
            },
        }


# ---------------------------------------------------------------------------
# Stage 4: writing
# ---------------------------------------------------------------------------

class InvokeWritingSubagentTool(_InvokeBase):
    """Run the real writing skill and enforce its publication gates."""

    stage_id = "writing"
    tool_name = "invoke_writing_subagent"

    def __init__(self) -> None:
        super().__init__(
            ToolCard(
                id=self.tool_name,
                name=self.tool_name,
                parallel_safe=False,
                description=(
                    "Invoke the writing sub-agent (stage 4). Combines "
                    "conception + planning + experiment artifacts into a "
                    "research paper PDF, then verifies evidence, reviews, "
                    "LaTeX and PDF release gates."
                ),
                input_params={
                    "type": "object",
                    "required": ["run_id", "run_dir", "input"],
                    "additionalProperties": False,
                    "properties": {
                        "run_id": {"type": "string"},
                        "run_dir": {"type": "string"},
                        "input": {"type": "object"},
                        "previous_stage_outputs": {"type": "object"},
                        "iteration_context": {"type": "object"},
                    },
                },
            )
        )

    async def invoke(self, inputs: dict[str, Any], **kwargs) -> dict[str, Any]:
        try:
            envelope = _build_stage_envelope(inputs, self.stage_id)
        except ValueError as exc:
            return self._invalid_envelope(exc)

        run_dir = Path(envelope["run_dir"])
        stage_dir = run_dir / "stage4_writing"
        stage_dir.mkdir(parents=True, exist_ok=True)

        try:
            adapter = _load_paper_gen_runtime_module(
                "_writing_adapter.py", "_paper_gen_writing_adapter_runtime"
            )
            stage_runner = _load_paper_gen_runtime_module(
                "_stage_runner.py", "_paper_gen_stage_runner_runtime"
            )
            main_flow = _load_paper_gen_runtime_module(
                "main_flow.py", "_paper_gen_main_flow_runtime"
            )
            adapted = adapter.prepare_writing_inputs(
                run_dir / "stage1_conception",
                run_dir / "stage2_planning",
                run_dir / "stage3_experiment",
                stage_dir,
                base_dir=_workspace_root(),
            )
            result = await stage_runner.run_writing_stage(
                m1_path=Path(adapted["m1"]),
                m2_path=Path(adapted["m2"]),
                m3_path=Path(adapted["m3"]),
                source_manifest=Path(adapted["source_manifest"]),
                output_dir=stage_dir,
            )
        except (OSError, ValueError, KeyError, ImportError, FileNotFoundError) as exc:
            failed = self._failed(stage_dir, f"writing input/runtime error: {exc}")
            return _attach_token_usage(
                failed, run_dir=run_dir, stage=self.stage_id, stage_dir=stage_dir,
            )
        except Exception as exc:  # provider/SDK faults remain explicit failures.
            failed = self._failed(stage_dir, f"writing runtime error: {exc}")
            return _attach_token_usage(
                failed, run_dir=run_dir, stage=self.stage_id, stage_dir=stage_dir,
            )

        summary = result.get("summary") if isinstance(result, dict) else None
        summary = summary if isinstance(summary, dict) else {}

        def finish(response: dict[str, Any]) -> dict[str, Any]:
            return _attach_token_usage(
                response,
                run_dir=run_dir,
                stage=self.stage_id,
                stage_dir=stage_dir,
                stage_summary=summary,
            )

        release = main_flow._assess_writing_release(stage_dir, summary)
        release_path = stage_dir / "publication_eligibility.json"
        release_path.write_text(
            json.dumps(release, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        artifacts = {
            "files": list(summary.get("artifacts") or []),
            "publication_eligibility": str(release_path),
        }
        pdf_path = stage_dir / "paper.pdf"
        if pdf_path.is_file():
            artifacts["pdf_path"] = str(pdf_path)

        if release.get("eligible") is True:
            draft = str(release.get("release_mode") or "") == "draft_with_warnings"
            return finish({
                "status": "completed",
                "output_dir": str(stage_dir),
                "summary": (
                    "writing completed as a verified draft with disclosed editorial follow-up"
                    if draft else "writing completed and passed publication gates"
                ),
                "artifacts": artifacts,
                "warnings": list(release.get("warnings") or []),
                "next_action": {
                    "type": "proceed", "target_stage": "done", "reason": ""
                },
            })

        errors = [str(item) for item in summary.get("errors") or []]
        failed_checks = [str(item) for item in release.get("errors") or []]
        reason = "; ".join([*errors[:5], *failed_checks[:10]]) or (
            f"writing ended with status={summary.get('status', 'unknown')}"
        )
        # The writing skill already performs its bounded internal
        # writer/reviewer/reviser loop. Re-running the same outer stage with
        # identical evidence would spend another full model budget without a
        # new signal, so a partial draft is a truthful blocked terminal state.
        if str(summary.get("status") or "").lower() == "partial":
            reason = f"writing draft failed publication gates after internal revisions: {reason}"
        # These statuses mean the writing module has truthfully found a
        # missing upstream input.  Sending the root agent back to writing
        # would repeat the same prompt with unchanged evidence, so route the
        # repair to the producing stage instead of creating a writing loop.
        writing_status = str(summary.get("status") or "").lower()
        if writing_status in {"needs_experiment_data", "invalid_execution_evidence"}:
            return finish({
                "status": "replan",
                "output_dir": str(stage_dir),
                "summary": "writing requires corrected experiment evidence",
                "artifacts": artifacts,
                "next_action": {
                    "type": "replan",
                    "target_stage": "experiment",
                    "reason": reason,
                },
            })
        return finish(self._failed(stage_dir, reason, artifacts))

    async def stream(self, inputs, **kwargs):
        yield await self.invoke(inputs, **kwargs)

    def _failed(
        self,
        stage_dir: Path,
        reason: str,
        artifacts: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "status": "failed",
            "output_dir": str(stage_dir),
            "summary": "writing failed",
            "artifacts": artifacts or {},
            "next_action": {
                "type": "abort",
                "target_stage": "writing",
                "reason": reason,
            },
        }

    def _invalid_envelope(self, exc: ValueError) -> dict[str, Any]:
        return {
            "status": "failed",
            "error": str(exc),
            "output_dir": None,
            "summary": "invalid envelope",
            "artifacts": {},
            "next_action": {
                "type": "abort",
                "target_stage": "writing",
                "reason": str(exc),
            },
        }


# ---------------------------------------------------------------------------
# Token usage query
# ---------------------------------------------------------------------------

class ReadTokenUsageTool(Tool):
    """Read per-module and full-pipeline LLM token usage."""

    tool_name = "read_token_usage"
    agent_name = "paper-gen-agent"

    def __init__(self) -> None:
        super().__init__(
            ToolCard(
                id=self.tool_name,
                name=self.tool_name,
                parallel_safe=True,
                description=(
                    "Read token_usage.json and return conception/planning/experiment/"
                    "writing usage plus the full total. Includes failed attempts and "
                    "REPLAN retries; missing provider usage is explicitly reported."
                ),
                input_params={
                    "type": "object",
                    "required": ["run_dir"],
                    "additionalProperties": False,
                    "properties": {"run_dir": {"type": "string"}},
                },
            )
        )

    async def invoke(self, inputs: dict[str, Any], **kwargs) -> dict[str, Any]:
        try:
            run_dir = _abs_under_root(inputs["run_dir"])
        except (ValueError, KeyError) as exc:
            return {"error": f"invalid run_dir: {exc}"}
        token_path = run_dir / "token_usage.json"
        payload = _read_json_object(token_path)
        if payload:
            return {
                "status": "available",
                "source": str(token_path),
                "usage": payload,
            }

        # Deterministic paper-gen already persists equivalent information in
        # stage_metrics.json. Support it so CLI and TUI runs share one query tool.
        metrics_path = run_dir / "stage_metrics.json"
        metrics = _read_json_object(metrics_path)
        if metrics:
            attempts = metrics.get("all_execution_attempts") or {}
            total = attempts.get("llm_tokens") or metrics.get("llm_token_usage_total") or {}
            successful = ((attempts.get("successful_path") or {}).get("llm_tokens") or {})
            unsuccessful = ((attempts.get("replan_or_failed") or {}).get("llm_tokens") or {})
            full_pipeline_completed = bool(
                (attempts.get("successful_path") or {}).get("full_pipeline_completed")
            )
            modules: dict[str, dict[str, Any]] = {}
            attempt_modules = attempts.get("by_stage") or {}
            for stage in _PIPELINE_STAGES:
                if stage in attempt_modules:
                    item = attempt_modules.get(stage) or {}
                    modules[stage] = {
                        "attempt_count": int(item.get("attempt_count") or 0),
                        "successful_path": item.get("successful_path") or {},
                        "replan_or_failed": item.get("replan_or_failed") or {},
                        **{
                            key: int((item.get("llm_tokens") or {}).get(key) or 0)
                            for key in _TOKEN_KEYS
                        },
                    }
                else:
                    item = (metrics.get("stages") or {}).get(stage) or {}
                    normalized, reported, _ = _token_totals(item)
                    modules[stage] = {
                        "attempt_count": 1 if item else 0,
                        "reported_by_provider": reported,
                        **normalized,
                    }
            return {
                "status": "available",
                "source": str(metrics_path),
                "usage": {
                    "schema_version": 1,
                    "modules": modules,
                    "successful_path": {
                        "full_pipeline_completed": full_pipeline_completed,
                        "provisional": not full_pipeline_completed,
                        "total": {key: int(successful.get(key) or 0) for key in _TOKEN_KEYS},
                    },
                    "replan_or_failed": {
                        "total": {key: int(unsuccessful.get(key) or 0) for key in _TOKEN_KEYS},
                    },
                    "overall_total": {key: int(total.get(key) or 0) for key in _TOKEN_KEYS},
                    "total": {key: int(total.get(key) or 0) for key in _TOKEN_KEYS},
                    "coverage": {
                        "complete": None,
                        "note": "Converted from deterministic paper-gen stage_metrics.json.",
                    },
                },
            }
        return {
            "status": "unavailable",
            "source": None,
            "usage": {
                "modules": {stage: {"attempt_count": 0, **_empty_token_totals()} for stage in _PIPELINE_STAGES},
                "successful_path": {
                    "full_pipeline_completed": False,
                    "provisional": True,
                    "total": _empty_token_totals(),
                },
                "replan_or_failed": {"total": _empty_token_totals()},
                "overall_total": _empty_token_totals(),
                "total": _empty_token_totals(),
                "coverage": {
                    "complete": False,
                    "note": "No stage has produced token telemetry yet.",
                },
            },
        }

    async def stream(self, inputs, **kwargs):
        yield await self.invoke(inputs, **kwargs)


# ---------------------------------------------------------------------------
# State tools
# ---------------------------------------------------------------------------

def _state_path(run_dir: Path) -> Path:
    return run_dir / "pipeline_state.json"


class ReadPipelineStateTool(Tool):
    """Read run_dir/pipeline_state.json. Returns the full state object."""

    tool_name = "read_pipeline_state"
    agent_name = "paper-gen-agent"

    def __init__(self) -> None:
        super().__init__(
            ToolCard(
                id=self.tool_name,
                name=self.tool_name,
                parallel_safe=True,
                description="Read run_dir/pipeline_state.json. Returns the full state object or initializes one if absent.",
                input_params={
                    "type": "object",
                    "required": ["run_dir"],
                    "additionalProperties": False,
                    "properties": {"run_dir": {"type": "string"}},
                },
            )
        )

    async def invoke(self, inputs: dict[str, Any], **kwargs) -> dict[str, Any]:
        try:
            run_dir = _abs_under_root(inputs["run_dir"])
        except (ValueError, KeyError) as exc:
            return {"error": f"invalid run_dir: {exc}"}
        path = _state_path(run_dir)
        if not path.exists():
            return {
                "state": self._initial_state(run_dir),
                "created": True,
            }
        try:
            return {
                "state": json.loads(path.read_text(encoding="utf-8")),
                "created": False,
            }
        except json.JSONDecodeError as exc:
            return {"error": f"pipeline_state.json is corrupt: {exc}"}

    async def stream(self, inputs, **kwargs):
        yield await self.invoke(inputs, **kwargs)

    @staticmethod
    def _initial_state(run_dir: Path) -> dict[str, Any]:
        run_id = run_dir.name
        return {
            "version": "0.2.0",
            "run_id": run_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "current_stage": "conception",
            "stages": {
                "conception": {"status": "pending", "rounds": 0},
                "planning":   {"status": "pending", "rounds": 0},
                "experiment": {"status": "pending", "rounds": 0},
                "writing":    {"status": "pending", "rounds": 0},
            },
            "iteration": {
                "planning_replan_rounds": 0,
                "writing_revise_rounds": 0,
                "max_planning_replan": 2,
                "max_writing_revise": 3,
            },
        }


class UpdatePipelineStateTool(Tool):
    """Atomically update pipeline_state.json. Accepts a partial patch.

    Merge semantics: deep-merge dicts at one level of depth. Lists are
    replaced, not appended (use a separate read+rewrite if you need
    list surgery).
    """

    tool_name = "update_pipeline_state"
    agent_name = "paper-gen-agent"

    def __init__(self) -> None:
        super().__init__(
            ToolCard(
                id=self.tool_name,
                name=self.tool_name,
                parallel_safe=False,
                description=(
                    "Atomically update run_dir/pipeline_state.json. Accepts a "
                    "partial patch; deep-merges dicts at one level, replaces "
                    "lists. Uses write-temp + rename for crash safety."
                ),
                input_params={
                    "type": "object",
                    "required": ["run_dir", "patch"],
                    "additionalProperties": False,
                    "properties": {
                        "run_dir": {"type": "string"},
                        "patch": {"type": "object"},
                    },
                },
            )
        )

    async def invoke(self, inputs: dict[str, Any], **kwargs) -> dict[str, Any]:
        try:
            run_dir = _abs_under_root(inputs["run_dir"])
        except (ValueError, KeyError) as exc:
            return {"error": f"invalid run_dir: {exc}"}
        patch = inputs.get("patch", {})
        if not isinstance(patch, dict):
            return {"error": "patch must be a JSON object"}

        path = _state_path(run_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            try:
                current = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                return {"error": f"pipeline_state.json is corrupt: {exc}"}
        else:
            current = ReadPipelineStateTool._initial_state(run_dir)

        merged = self._deep_merge_one_level(current, patch)
        merged["updated_at"] = datetime.now(timezone.utc).isoformat()

        # Atomic write: temp file in same dir, then rename.
        fd, tmp_path = tempfile.mkstemp(
            prefix=".pipeline_state.", suffix=".tmp", dir=str(path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(merged, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

        return {"state": merged}

    async def stream(self, inputs, **kwargs):
        yield await self.invoke(inputs, **kwargs)

    @staticmethod
    def _deep_merge_one_level(base: dict, patch: dict) -> dict:
        out = dict(base)
        for k, v in patch.items():
            if (
                k in out
                and isinstance(out[k], dict)
                and isinstance(v, dict)
            ):
                out[k] = {**out[k], **v}
            else:
                out[k] = v
        return out
