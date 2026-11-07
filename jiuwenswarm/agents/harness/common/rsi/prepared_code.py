"""Host-prepared research source through the existing implementation contract."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

from openjiuwen.rsi.artifact_rsi.paper_opt.auto_research.common.workspace import (
    generated_code_dir,
    project_root,
    smoke_test_dir,
)
from openjiuwen.rsi.artifact_rsi.paper_opt.auto_research.modules.code_implementation.schemas import (
    CodeImplementationInput,
    CodeImplementationManifest,
    CodeImplementationOutput,
    ImplementedVariant,
)

_PUBLIC_FILES = ("run.py", "dataset.json", "manifest.json", "PROTOCOL.md")


class PreparedCodeAgent:
    """Copy frozen public inputs and execute an offline functional smoke test.

    Source preparation is a human-assisted step. The existing Manager adapter
    records its real result; ExperimentExecutionAgent runs the live study.
    """

    def __init__(self, source_dir: str | Path, *, task_id: str) -> None:
        self.source_dir = Path(source_dir).expanduser().resolve()
        self.task_id = task_id

    async def arun(self, inputs: CodeImplementationInput) -> CodeImplementationOutput:
        return await asyncio.to_thread(self._prepare, inputs)

    def _prepare(self, inputs: CodeImplementationInput) -> CodeImplementationOutput:
        run_id = inputs.plan.run_id
        if not run_id or run_id in {".", ".."} or any(c in run_id for c in "/\\:"):
            raise ValueError("run_id must be a single directory name")
        root = project_root().resolve()
        workspace = generated_code_dir(run_id).resolve()
        smoke_root = smoke_test_dir(run_id).resolve()
        if not workspace.is_relative_to(root / "experiments") or not smoke_root.is_relative_to(root / "experiments"):
            raise ValueError("run_id workspace escapes the research root")
        sources = [self.source_dir / name for name in _PUBLIC_FILES]
        for source in sources:
            if not source.resolve().is_relative_to(self.source_dir) or not source.is_file():
                raise ValueError(f"public source is missing or outside source directory: {source.name}")
        workspace.mkdir(parents=True, exist_ok=True)
        smoke_root.mkdir(parents=True, exist_ok=True)
        hashes = {}
        for source in sources:
            content = source.read_bytes()
            workspace.joinpath(source.name).write_bytes(content)
            hashes[source.name] = hashlib.sha256(content).hexdigest()
        workspace.joinpath("prepared_source_manifest.json").write_text(
            json.dumps({"preparation": "human-assisted frozen source", "sha256": hashes}, indent=2) + "\n",
            encoding="utf-8",
        )
        invocation = [
            sys.executable, "run.py", "--method", "paired_study",
            "--usage-ledger", str(root / "model_calls.jsonl"), "--task-id", self.task_id,
            "--node-ref", run_id,
        ]
        metrics_path = smoke_root / "paired_study.metrics.json"
        metrics_path.unlink(missing_ok=True)
        command = [*invocation, "--smoke-test", "--output", str(metrics_path)]
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
        result = subprocess.run(
            command, cwd=workspace, env=env, capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=60,
        )
        log = f"cwd={workspace}\nexit_code={result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        smoke_root.joinpath("paired_study.log").write_text(log, encoding="utf-8")
        failure = ""
        if result.returncode:
            failure = f"exit_code={result.returncode}; {result.stderr.strip()}"
        else:
            try:
                metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
                if not (
                    isinstance(metrics, dict) and metrics.get("method") == "paired_study"
                    and metrics.get("status") == "completed"
                    and metrics.get("smoke_test") is True
                    and metrics.get("model_call_count") == 0
                ):
                    failure = "offline smoke metrics do not match the frozen runner contract"
            except (OSError, ValueError) as exc:
                failure = f"offline smoke metrics unavailable: {type(exc).__name__}"
        passed = not failure
        return CodeImplementationOutput(
            implementation=CodeImplementationManifest(
                run_id=run_id, workspace_dir=str(workspace), files=list(_PUBLIC_FILES),
                variants=[ImplementedVariant(name="paired_study", invocation=invocation)],
                assumptions=["Research code, dataset and protocol were prepared before the Manager run."],
                smoke_test_passed=passed, status="ready" if passed else "failed",
                readiness="smoke_ready" if passed else "failed",
                smoke_failures={} if passed else {"paired_study": failure},
                notes="Human-assisted source preparation; offline smoke uses zero model calls. "
                "Live experimental execution is performed by ExperimentExecutionAgent.",
            )
        )
