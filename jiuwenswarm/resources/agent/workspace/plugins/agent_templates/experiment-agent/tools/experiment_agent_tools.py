"""Thin JiuwenSwarm Tool adapters over Module 3's deterministic runtime."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from openjiuwen.core.foundation.tool import Tool, ToolCard
from openjiuwen.core.sys_operation.cwd import get_cwd


def _experiment_main() -> Path:
    current = Path(__file__).resolve()
    for parent in current.parents:
        candidate = parent / "skills" / "experiment" / "scripts" / "main.py"
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        "cannot locate workspace/skills/experiment/scripts/main.py"
    )


def _append_path(command: list[str], flag: str, value: Any) -> None:
    if value not in (None, ""):
        command.extend([flag, str(value)])


def _trusted_workspace_root() -> Path:
    """Return the project root while rejecting untrusted environment layouts."""
    runtime_workspace = Path(get_cwd()).resolve()
    configured = os.environ.get("JIUWENSWARM_DATA_DIR", "").strip()
    if not configured:
        return runtime_workspace
    data_root = Path(configured).resolve()
    if data_root.name != ".jiuwenswarm-project":
        return runtime_workspace
    project_root = data_root.parent.resolve()
    if data_root != project_root / ".jiuwenswarm-project":
        return runtime_workspace
    return project_root


def _workspace_path(
    value: Any,
    *,
    must_exist: bool,
    base: Path | None = None,
) -> Path:
    workspace = _trusted_workspace_root()
    candidate = Path(str(value))
    if not candidate.is_absolute():
        candidate = (base or workspace) / candidate
    resolved = candidate.resolve(strict=must_exist)
    if resolved != workspace and not resolved.is_relative_to(workspace):
        raise ValueError(f"path is outside the trusted workspace: {value}")
    return resolved


def _validate_initialize_paths(inputs: dict[str, Any]) -> tuple[Path, Path | None]:
    input_path = _workspace_path(inputs["input_path"], must_exist=True)
    try:
        payload = json.loads(input_path.read_text(encoding="utf-8"))
        raw_run_dir = payload["execution_config"]["run_dir"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ValueError(f"cannot read execution_config.run_dir: {exc}") from exc
    _workspace_path(raw_run_dir, must_exist=False)
    manifest_value = inputs.get("manifest_path")
    manifest_path = (
        _workspace_path(manifest_value, must_exist=True)
        if manifest_value not in (None, "")
        else None
    )
    return input_path, manifest_path


def _run_cli(
    command: list[str],
    input_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    completed = subprocess.run(
        command,
        cwd=str(_trusted_workspace_root()),
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
    )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            return {
                "error": "Module 3 runtime command failed",
                "return_code": completed.returncode,
                "detail": detail[-4000:],
            }
        return {
            "error": "Module 3 runtime returned invalid JSON",
            "detail": str(exc),
            "stdout": completed.stdout[-4000:],
        }
    if not isinstance(payload, dict):
        return {"error": "Module 3 runtime response must be a JSON object"}
    if completed.returncode != 0 and payload.get("status") != "REPLAN":
        return {
            "error": "Module 3 runtime command failed",
            "return_code": completed.returncode,
            "detail": payload,
        }
    return payload


class _ExperimentTool(Tool):
    command_name = ""
    agent_name = "experiment-agent"
    tool_name = "experiment_control"

    async def _invoke_command(
        self,
        command: list[str],
        *,
        input_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        result = await asyncio.to_thread(_run_cli, command, input_payload)
        await asyncio.to_thread(
            _record_agent_event,
            result,
            agent=self.agent_name,
            tool=self.tool_name,
            action=command[2] if len(command) > 2 else "unknown",
        )
        return result

    @staticmethod
    def _base_command(command_name: str) -> list[str]:
        return [sys.executable, str(_experiment_main()), command_name]

    async def stream(self, inputs: dict[str, Any], **kwargs):
        yield await self.invoke(inputs, **kwargs)


class ExperimentControlTool(_ExperimentTool):
    """Root Agent gate and status tool."""

    agent_name = "experiment-agent"
    tool_name = "experiment_control"

    def __init__(self) -> None:
        super().__init__(
            ToolCard(
                id="experiment_control",
                name="experiment_control",
                parallel_safe=False,
                description=(
                    "初始化/查询模块三状态机，并由根Agent读取隔离审查上下文或提交"
                    "严格的两阶段LLM审查。LLM决定只授权后端继续门禁，不能直接写批准字段。"
                ),
                input_params={
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": [
                                "adapt_planning",
                                "initialize",
                                "status",
                                "review_context",
                                "submit_code_review",
                                "submit_execution_review"
                            ],
                        },
                        "planning_dir": {"type": "string"},
                        "workspace_root": {"type": "string"},
                        "domain_path": {"type": "string"},
                        "resource_constraints_path": {"type": "string"},
                        "output_path": {"type": "string"},
                        "input_path": {"type": "string"},
                        "manifest_path": {"type": "string"},
                        "run_dir": {"type": "string"},
                        "run_id": {"type": "string"},
                        "review": {"type": "object"},
                        "allow_downloads": {"type": "boolean", "default": True},
                        "max_download_gb": {
                            "type": "number",
                            "exclusiveMinimum": 0,
                            "default": 20,
                        },
                    },
                    "required": ["action"],
                },
            )
        )

    async def invoke(self, inputs: dict[str, Any], **kwargs) -> dict[str, Any]:
        action = inputs.get("action")
        if action == "adapt_planning":
            required = ("planning_dir", "workspace_root")
            if any(not inputs.get(name) for name in required):
                return {"error": "adapt_planning requires planning_dir and workspace_root"}
            try:
                workspace_root = _workspace_path(inputs["workspace_root"], must_exist=True)
                planning_dir = _workspace_path(
                    inputs["planning_dir"],
                    must_exist=True,
                    base=workspace_root,
                )
                manifest_path = (
                    _workspace_path(
                        inputs["manifest_path"],
                        must_exist=True,
                        base=workspace_root,
                    )
                    if inputs.get("manifest_path")
                    else None
                )
                domain_path = (
                    _workspace_path(
                        inputs["domain_path"],
                        must_exist=True,
                        base=workspace_root,
                    )
                    if inputs.get("domain_path")
                    else None
                )
                resource_path = (
                    _workspace_path(
                        inputs["resource_constraints_path"],
                        must_exist=True,
                        base=workspace_root,
                    )
                    if inputs.get("resource_constraints_path")
                    else None
                )
                output_path = (
                    _workspace_path(
                        inputs["output_path"],
                        must_exist=False,
                        base=workspace_root,
                    )
                    if inputs.get("output_path")
                    else None
                )
            except ValueError as exc:
                return {"error": str(exc)}
            command = self._base_command("adapt-planning")
            _append_path(command, "--planning-dir", planning_dir)
            _append_path(command, "--workspace-root", workspace_root)
            _append_path(command, "--manifest", manifest_path)
            _append_path(command, "--domain", domain_path)
            _append_path(command, "--resource-constraints", resource_path)
            _append_path(command, "--output", output_path)
            return await self._invoke_command(command)
        if action == "initialize":
            if not inputs.get("input_path"):
                return {"error": "initialize requires input_path"}
            try:
                input_path, manifest_path = _validate_initialize_paths(inputs)
            except ValueError as exc:
                return {"error": str(exc)}
            command = self._base_command("agent-init")
            _append_path(command, "--input", input_path)
            _append_path(command, "--manifest", manifest_path)
            if inputs.get("allow_downloads", True):
                command.append("--allow-downloads")
            else:
                command.append("--no-downloads")
            _append_path(
                command,
                "--max-download-gb",
                inputs.get("max_download_gb", 20),
            )
            return await self._invoke_command(command)
        if action == "status":
            if not inputs.get("run_dir") or not inputs.get("run_id"):
                return {"error": "status requires run_dir and run_id"}
            try:
                run_dir = _workspace_path(inputs["run_dir"], must_exist=True)
            except ValueError as exc:
                return {"error": str(exc)}
            command = self._base_command("agent-status")
            _append_path(command, "--run-dir", run_dir)
            _append_path(command, "--run-id", inputs["run_id"])
            return await self._invoke_command(command)
        if action == "review_context":
            command = _resume_command("agent-review-context", inputs)
            if "error" in command:
                return command
            return await self._invoke_command(command["command"])
        if action in {"submit_code_review", "submit_execution_review"}:
            if not isinstance(inputs.get("review"), dict):
                return {"error": f"{action} requires a structured review object"}
            command_name = (
                "agent-submit-code-review"
                if action == "submit_code_review"
                else "agent-submit-execution-review"
            )
            command = _resume_command(command_name, inputs)
            if "error" in command:
                return command
            return await self._invoke_command(
                command["command"], input_payload=inputs["review"]
            )
        return {"error": f"unsupported action: {action}"}


class ExperimentDataTool(_ExperimentTool):
    """DataAgent's only stage operation."""

    agent_name = "experiment-data-agent"
    tool_name = "experiment_prepare_data"

    def __init__(self) -> None:
        super().__init__(
            ToolCard(
                id="experiment_prepare_data",
                name="experiment_prepare_data",
                parallel_safe=False,
                description="按已验证规划准备数据集，并输出严格DataAgentResponse。",
                input_params=_resume_schema(include_download_options=True),
            )
        )

    async def invoke(self, inputs: dict[str, Any], **kwargs) -> dict[str, Any]:
        command = _resume_command("agent-data", inputs)
        if "error" in command:
            return command
        if inputs.get("allow_downloads", True):
            command["command"].append("--allow-downloads")
        else:
            command["command"].append("--no-downloads")
        _append_path(
            command["command"],
            "--max-download-gb",
            inputs.get("max_download_gb", 20),
        )
        return await self._invoke_command(command["command"])


class ExperimentImplementationTool(_ExperimentTool):
    """ImplementationAgent's only stage operation."""

    agent_name = "experiment-implementation-agent"
    tool_name = "experiment_resolve_implementation"

    def __init__(self) -> None:
        super().__init__(
            ToolCard(
                id="experiment_resolve_implementation",
                name="experiment_resolve_implementation",
                parallel_safe=False,
                description=(
                    "检查、复用或受限写入实现，静态登记为冻结任务并请求根Agent独立审查；"
                    "本工具不能批准或执行自己生成的代码。"
                ),
                input_params={
                    **_resume_schema(),
                    "properties": {
                        **_resume_schema()["properties"],
                        "action": {
                            "type": "string",
                            "enum": ["inspect", "write_generated", "request_approval"],
                            "default": "inspect"
                        },
                        "proposal_path": {"type": "string"},
                        "proposal": _proposal_schema()
                    }
                },
            )
        )

    async def invoke(self, inputs: dict[str, Any], **kwargs) -> dict[str, Any]:
        if inputs.get("action", "inspect") == "inspect":
            command = _resume_command("agent-inspect-implementation", inputs)
            if "error" in command:
                return command
            return await self._invoke_command(command["command"])
        if inputs.get("action") == "write_generated":
            direct = inputs.get("proposal")
            proposal_path = inputs.get("proposal_path")
            if not isinstance(direct, dict) and not proposal_path:
                return {
                    "error": "write_generated requires proposal object or proposal_path"
                }
            if isinstance(direct, dict) and proposal_path:
                return {"error": "proposal and proposal_path are mutually exclusive"}
            command = _resume_command("agent-write-generated", inputs)
            if "error" in command:
                return command
            if isinstance(direct, dict):
                return await self._invoke_command(
                    command["command"], input_payload=direct
                )
            try:
                proposal = _workspace_path(proposal_path, must_exist=True)
            except ValueError as exc:
                return {"error": str(exc)}
            _append_path(command["command"], "--proposal", proposal)
            return await self._invoke_command(command["command"])
        command = _resume_command("agent-implementation", inputs)
        if "error" in command:
            return command
        return await self._invoke_command(command["command"])


class ExperimentExecutionTool(_ExperimentTool):
    """ExecutionAgent's only stage operation."""

    agent_name = "experiment-execution-agent"
    tool_name = "experiment_execute"

    def __init__(self) -> None:
        super().__init__(
            ToolCard(
                id="experiment_execute",
                name="experiment_execute",
                parallel_safe=False,
                description="严格执行冻结任务，保留日志、重试信息和原始指标。",
                input_params=_resume_schema(),
            )
        )

    async def invoke(self, inputs: dict[str, Any], **kwargs) -> dict[str, Any]:
        command = _resume_command("agent-execute", inputs)
        if "error" in command:
            return command
        return await self._invoke_command(command["command"])


class ExperimentAnalysisTool(_ExperimentTool):
    """AnalysisAgent's only stage operation."""

    agent_name = "experiment-analysis-agent"
    tool_name = "experiment_analyze"

    def __init__(self) -> None:
        super().__init__(
            ToolCard(
                id="experiment_analyze",
                name="experiment_analyze",
                parallel_safe=False,
                description=(
                    "从成功运行计算聚合、判据、假设与候选图表，输出模块四契约。"
                ),
                input_params=_resume_schema(),
            )
        )

    async def invoke(self, inputs: dict[str, Any], **kwargs) -> dict[str, Any]:
        command = _resume_command("agent-analyze", inputs)
        if "error" in command:
            return command
        return await self._invoke_command(command["command"])


def _resume_schema(*, include_download_options: bool = False) -> dict[str, Any]:
    properties: dict[str, Any] = {
        "run_dir": {"type": "string"},
        "run_id": {"type": "string"},
    }
    if include_download_options:
        properties.update(
            {
                "allow_downloads": {"type": "boolean", "default": True},
                "max_download_gb": {
                    "type": "number",
                    "exclusiveMinimum": 0,
                    "default": 20,
                },
            }
        )
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": ["run_dir", "run_id"],
    }


def _resume_command(command_name: str, inputs: dict[str, Any]) -> dict[str, Any]:
    if not inputs.get("run_dir") or not inputs.get("run_id"):
        return {"error": f"{command_name} requires run_dir and run_id"}
    try:
        run_dir = _workspace_path(inputs["run_dir"], must_exist=True)
    except ValueError as exc:
        return {"error": str(exc)}
    command = _ExperimentTool._base_command(command_name)
    _append_path(command, "--run-dir", run_dir)
    _append_path(command, "--run-id", inputs["run_id"])
    return {"command": command}


def _proposal_schema() -> dict[str, Any]:
    file_properties = {
        name: {"type": "string"}
        for name in (
            "main.py",
            "README.md",
            "requirements.txt",
            "test_smoke.py",
            "config.example.json",
        )
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "method": {"type": "string"},
            "files": {
                "type": "object",
                "additionalProperties": False,
                "properties": file_properties,
            },
            **file_properties,
            "source_url": {"type": ["string", "null"]},
            "revision": {"type": ["string", "null"]},
            "license": {"type": "string"},
            "dependency_plan": {
                "type": "array",
                "items": {"type": "string"},
            },
            "required_env": {
                "type": "array",
                "items": {"type": "string"},
            },
            "uses_gpu": {"type": "boolean"},
            "notes": {"type": "string"},
        },
        "required": [
            "method",
            "source_url",
            "revision",
            "license",
            "dependency_plan",
            "required_env",
            "uses_gpu",
            "notes",
        ],
    }


def _record_agent_event(
    result: dict[str, Any],
    *,
    agent: str,
    tool: str,
    action: str,
) -> None:
    raw_run_dir = result.get("run_dir")
    if not isinstance(raw_run_dir, str) or not raw_run_dir:
        return
    try:
        run_dir = _workspace_path(raw_run_dir, must_exist=True)
    except ValueError:
        return
    output_dir = run_dir / "outputs"
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "agent-events.json"
    if path.exists() and path.is_symlink():
        raise ValueError("agent event log cannot be a symbolic link")
    try:
        events = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else []
    except (OSError, json.JSONDecodeError):
        events = []
    if not isinstance(events, list):
        raise ValueError("agent event log must contain a list")
    canonical = json.dumps(result, ensure_ascii=False, sort_keys=True).encode("utf-8")
    events.append(
        {
            "sequence": len(events) + 1,
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "agent": agent,
            "tool": tool,
            "action": action,
            "run_id": result.get("run_id"),
            "stage": result.get("stage"),
            "result_sha256": hashlib.sha256(canonical).hexdigest(),
        }
    )
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(events, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)
