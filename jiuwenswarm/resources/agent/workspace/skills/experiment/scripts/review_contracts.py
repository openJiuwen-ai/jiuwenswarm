"""Strict contracts and deterministic gates for independent LLM review."""

from __future__ import annotations

import ast
import hashlib
import json
import re
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from contracts import (
    STANDARD_EXECUTOR_TASK_TYPES,
    ExperimentModuleInput,
    resolve_task_type,
)
from implementation_approval import (
    implementation_code_digest,
)
from io_utils import portable_arguments, relative_to_run, safe_join
from plan_execution import planned_method_names
from runtime_models import ImplementationManifest, RuntimeModel, RuntimeRunResult


_PINNED_DEPENDENCY = re.compile(
    r"^[A-Za-z0-9_.-]+(?:\[[A-Za-z0-9_,.-]+\])?==[A-Za-z0-9][A-Za-z0-9_.+!-]*$"
)
_ENV_REFERENCE = re.compile(
    r"(?:os\.getenv\(|os\.environ(?:\.get\(|\[))\s*['\"]([A-Za-z_][A-Za-z0-9_]*)"
)
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)['\"]?(?:api[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret|"
    r"token|secret|password|private[_-]?key|authorization)['\"]?\s*[:=]\s*"
    r"['\"][^'\"]+['\"]"
)
_SHELL_EXECUTABLES = {
    "bash",
    "bash.exe",
    "cmd",
    "cmd.exe",
    "command.com",
    "dash",
    "fish",
    "powershell",
    "powershell.exe",
    "pwsh",
    "pwsh.exe",
    "sh",
    "sh.exe",
    "zsh",
}


class StaticFinding(RuntimeModel):
    category: Literal[
        "PATH",
        "SHELL",
        "SECRET",
        "DEPENDENCY",
        "ENVIRONMENT",
        "SCHEMA",
        "METHOD",
        "METRIC",
        "INTEGRITY",
        "RESOURCE",
    ]
    severity: Literal["ERROR", "WARNING"]
    method: str | None = None
    file: str | None = None
    message: str = Field(min_length=1)


class DeterministicStaticReport(RuntimeModel):
    schema_version: str = "1.0.0"
    code_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    passed: bool
    findings: list[StaticFinding] = Field(default_factory=list)

    @model_validator(mode="after")
    def result_matches_findings(self) -> "DeterministicStaticReport":
        errors = any(item.severity == "ERROR" for item in self.findings)
        if self.passed == errors:
            raise ValueError("static report passed flag does not match findings")
        return self


class ReviewCodeFile(RuntimeModel):
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)
    content: str


class CodeReviewContext(RuntimeModel):
    schema_version: str = "1.0.0"
    run_id: str = Field(min_length=1)
    review_stage: Literal["CODE_REVIEW_REQUIRED"] = "CODE_REVIEW_REQUIRED"
    reviewer: Literal["experiment-agent"] = "experiment-agent"
    method_design: dict[str, Any]
    experiment_objectives: list[str] = Field(min_length=1)
    planned_methods: list[str] = Field(min_length=1)
    code_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    code_files: list[ReviewCodeFile] = Field(min_length=1)
    sources: list[dict[str, Any]] = Field(min_length=1)
    fixed_dependencies: dict[str, list[str]]
    commands: dict[str, list[str]]
    data_access: dict[str, list[str]]
    resources: dict[str, dict[str, Any]]
    static_report: DeterministicStaticReport
    risks: list[str] = Field(default_factory=list)


class ExecutionReviewContext(RuntimeModel):
    schema_version: str = "1.0.0"
    run_id: str = Field(min_length=1)
    review_stage: Literal["EXECUTION_REVIEW_REQUIRED"] = (
        "EXECUTION_REVIEW_REQUIRED"
    )
    reviewer: Literal["experiment-agent"] = "experiment-agent"
    code_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    code_material: CodeReviewContext
    code_review_path: str = Field(min_length=1)
    code_review_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    smoke_result_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    smoke_results: list[RuntimeRunResult] = Field(min_length=1)
    environment_deployment: dict[str, Any]
    environment_deployment_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    metric_contracts: dict[str, dict[str, Any]] = Field(min_length=1)
    commands: dict[str, list[str]]
    data_access: dict[str, list[str]]
    smoke_data_access: dict[str, list[str]]
    static_report: DeterministicStaticReport
    risks: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def code_material_matches_execution_review(self) -> "ExecutionReviewContext":
        if self.code_material.run_id != self.run_id:
            raise ValueError("second-stage code material run_id does not match")
        if self.code_material.code_digest != self.code_digest:
            raise ValueError("second-stage code material digest does not match")
        if self.code_material.reviewer != self.reviewer:
            raise ValueError("second-stage code material reviewer does not match")
        return self


class CodeReviewDecision(RuntimeModel):
    decision: Literal["APPROVE_SMOKE", "REJECT", "REPLAN"]
    reviewer: Literal["experiment-agent"]
    review_model: str = Field(min_length=1, max_length=200)
    code_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    security_findings: list[str] = Field(default_factory=list)
    scientific_findings: list[str] = Field(default_factory=list)
    dependency_findings: list[str] = Field(default_factory=list)
    required_changes: list[str] = Field(default_factory=list)
    reason: str = Field(min_length=1)

    @field_validator("review_model")
    @classmethod
    def model_name_is_concrete(cls, value: str) -> str:
        value = value.strip()
        if value.casefold() in {
            "unknown",
            "model",
            "llm",
            "implementation-agent",
            "experiment-implementation-agent",
        }:
            raise ValueError("review_model must be the actual root reviewer model name")
        return value

    @model_validator(mode="after")
    def rejection_has_changes(self) -> "CodeReviewDecision":
        if self.decision == "REJECT" and not self.required_changes:
            raise ValueError("REJECT requires at least one required change")
        if self.decision == "APPROVE_SMOKE" and self.required_changes:
            raise ValueError("APPROVE_SMOKE cannot contain required changes")
        return self


class ExecutionReviewDecision(RuntimeModel):
    decision: Literal["APPROVE_EXECUTION", "REJECT", "REPLAN"]
    reviewer: Literal["experiment-agent"]
    review_model: str = Field(min_length=1, max_length=200)
    code_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    smoke_result_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    metric_contract_verified: bool
    command_verified: bool
    data_scope_verified: bool
    risks: list[str] = Field(default_factory=list)
    reason: str = Field(min_length=1)

    _model_name_is_concrete = field_validator("review_model")(
        CodeReviewDecision.model_name_is_concrete.__func__
    )

    @model_validator(mode="after")
    def approval_requires_all_attestations(self) -> "ExecutionReviewDecision":
        if self.decision == "APPROVE_EXECUTION" and not all(
            (
                self.metric_contract_verified,
                self.command_verified,
                self.data_scope_verified,
            )
        ):
            raise ValueError(
                "APPROVE_EXECUTION requires metric, command and data-scope verification"
            )
        return self


def deterministic_static_review(
    request: ExperimentModuleInput,
    manifest: ImplementationManifest,
    run_dir: Path,
) -> DeterministicStaticReport:
    findings: list[StaticFinding] = []
    planned = planned_method_names(request)
    # 与 implementation_builder 走**同一份**判定（contracts.resolve_task_type）。
    # 两处各写一份会让「builder 认 retrieval、static review 判 SCHEMA」撕裂。
    task_type, unrecognized = resolve_task_type(request.data_plan.expected_size)
    label_column = request.data_plan.expected_size.get("label_column")
    if task_type is None:
        findings.append(_error(
            "SCHEMA",
            f"data_plan.expected_size.task_type "
            + (f"的值 {unrecognized!r} 无法识别" if unrecognized else "未提供"),
        ))
    elif task_type not in STANDARD_EXECUTOR_TASK_TYPES:
        # 任务类型认识、但没有内置执行器。这**不是** schema 错误——契约是合法的，
        # 缺的是执行通道。报 SCHEMA 会让人去改一个已经写对的字段。
        findings.append(StaticFinding(
            category="METHOD",
            severity="WARNING",
            message=(
                f"task_type={task_type!r} 没有模块三内置执行器；"
                f"该任务需经受控生成通道产出实现"
            ),
        ))
    # label_column 只在分类/回归下强制；检索/排序用 query/候选/相关性字段，
    # 无条件要 label_column 会让所有 retrieval 任务必然报一条无意义的 SCHEMA 错。
    if task_type in STANDARD_EXECUTOR_TASK_TYPES and (
        not isinstance(label_column, str) or not label_column.strip()
    ):
        findings.append(_error("SCHEMA", "data_plan未明确label_column"))
    for metric in request.experiment_plan.metrics:
        definition = manifest.metrics.get(metric)
        if definition is None or not definition.verified:
            findings.append(_error("METRIC", f"指标{metric!r}未通过确定性契约验证"))

    unplanned = set(manifest.implementations) - planned
    for method in sorted(unplanned):
        # Extra reusable entries are allowed, but never enter the review/execution set.
        findings.append(
            StaticFinding(
                category="METHOD",
                severity="WARNING",
                method=method,
                message="实现不在本次实验矩阵中，将被忽略且不得执行",
            )
        )
    for method in sorted(planned):
        definition = manifest.implementations.get(method)
        if definition is None:
            findings.append(_error("METHOD", f"计划方法{method!r}缺少实现", method))
            continue
        if definition.uses_gpu and (
            request.resource_constraints.gpu_hours <= 0
            or not request.resource_constraints.gpu_type
        ):
            findings.append(
                _error(
                    "RESOURCE",
                    "GPU实现缺少明确的gpu_type或正数gpu_hours资源条件",
                    method,
                )
            )
        if any(not _PINNED_DEPENDENCY.fullmatch(item) for item in definition.dependency_plan):
            findings.append(_error("DEPENDENCY", "新增依赖必须使用name==version精确固定", method))
        executable = Path(definition.command[0]).name.casefold()
        if executable in _SHELL_EXECUTABLES:
            findings.append(_error("SHELL", "实现命令禁止调用shell解释器", method))
        for command in (definition.command, definition.smoke_test_command):
            for index, argument in enumerate(command):
                if "\x00" in argument:
                    findings.append(_error("SHELL", "命令参数包含NUL字节", method))
                if _argument_escapes_run_dir(
                    argument,
                    run_dir,
                    executable_position=index == 0,
                ):
                    findings.append(
                        _error("PATH", f"命令参数访问未授权路径: {argument}", method)
                    )
        try:
            cwd = safe_join(run_dir, definition.cwd)
            if cwd.is_symlink():
                findings.append(_error("PATH", "实现cwd不能是符号链接", method))
        except ValueError as exc:
            findings.append(_error("PATH", str(exc), method))

        source_files = _method_source_files(manifest, method, run_dir)
        if not source_files:
            findings.append(_error("PATH", "未找到可审查的实现源文件", method))
            continue
        referenced_env: set[str] = set()
        for source in source_files:
            if source.is_symlink():
                findings.append(_error("PATH", "实现源文件不能是符号链接", method, source.name))
                continue
            try:
                content = source.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if _SECRET_ASSIGNMENT.search(content):
                findings.append(_error("SECRET", "源代码疑似包含内联密钥", method, source.name))
            referenced_env.update(_ENV_REFERENCE.findall(content))
            if source.suffix.casefold() == ".py" and _uses_unsafe_shell(content):
                findings.append(
                    _error(
                        "SHELL",
                        "实现源代码禁止使用shell=True",
                        method,
                        source.name,
                    )
                )
            if (
                definition.source_kind == "GENERATED"
                and source.name == "main.py"
                and _hardcodes_metric_value(
                    content,
                    metric_names=request.experiment_plan.metrics,
                )
            ):
                findings.append(
                    _error(
                        "INTEGRITY",
                        "生成代码疑似硬编码或伪造指标值",
                        method,
                        source.name,
                    )
                )
        undeclared = referenced_env - set(definition.required_env)
        if undeclared:
            findings.append(
                _error(
                    "ENVIRONMENT",
                    "代码引用未在required_env登记的环境变量: "
                    + ", ".join(sorted(undeclared)),
                    method,
                )
            )
    code_digest = implementation_code_digest(manifest, run_dir)
    return DeterministicStaticReport(
        code_digest=code_digest,
        passed=not any(item.severity == "ERROR" for item in findings),
        findings=findings,
    )


def build_code_review_context(
    request: ExperimentModuleInput,
    manifest: ImplementationManifest,
    run_dir: Path,
    dataset_paths: dict[str, str],
    risks: list[str],
) -> CodeReviewContext:
    report = deterministic_static_review(request, manifest, run_dir)
    planned = sorted(planned_method_names(request))
    code_files: list[ReviewCodeFile] = []
    review_paths = sorted(
        {
            path
            for method in planned
            for path in _method_source_files(manifest, method, run_dir)
        }
    )
    for path in review_paths:
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            content = "<binary or unreadable; inspect by SHA-256>"
        code_files.append(
            ReviewCodeFile(
                path=_review_path(path, run_dir),
                sha256=_sha256_file(path),
                size_bytes=path.stat().st_size,
                content=content,
            )
        )
    if not code_files:
        raise ValueError("code review context has no source files")
    return CodeReviewContext(
        run_id=request.run_id,
        method_design=request.method_design.model_dump(mode="json"),
        experiment_objectives=request.experiment_plan.objectives,
        planned_methods=planned,
        code_digest=report.code_digest,
        code_files=code_files,
        sources=[
            {
                "method": method,
                "source_kind": manifest.implementations[method].source_kind,
                "source_url": (
                    str(manifest.implementations[method].implementation_url)
                    if manifest.implementations[method].implementation_url else None
                ),
                "revision": manifest.implementations[method].revision,
                "license": manifest.implementations[method].license,
            }
            for method in planned
        ],
        fixed_dependencies={
            method: manifest.implementations[method].dependency_plan for method in planned
        },
        commands={
            method: portable_arguments(manifest.implementations[method].command, run_dir=run_dir)
            for method in planned
        },
        data_access={
            method: [relative_to_run(Path(path), run_dir) for path in dataset_paths.values()]
            for method in planned
        },
        resources={
            method: {
                "uses_gpu": manifest.implementations[method].uses_gpu,
                "gpu_hours_limit": request.resource_constraints.gpu_hours,
                "memory_gb_limit": request.resource_constraints.memory_gb,
            }
            for method in planned
        },
        static_report=report,
        risks=list(dict.fromkeys(risks)),
    )


def review_decision_digest(decision: RuntimeModel) -> str:
    encoded = json.dumps(
        decision.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def smoke_results_digest(results: list[RuntimeRunResult]) -> str:
    encoded = json.dumps(
        [item.model_dump(mode="json") for item in results],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _method_source_files(
    manifest: ImplementationManifest,
    method: str,
    run_dir: Path,
) -> list[Path]:
    definition = manifest.implementations[method]
    candidates: set[Path] = set()
    method_root = safe_join(run_dir, f"implementations/{_slug(method)}")
    if method_root.is_dir():
        for path in method_root.rglob("*"):
            if path.is_file():
                candidates.add(path)
    for argument in definition.command[1:]:
        if "{" in argument or "}" in argument:
            continue
        path = Path(argument)
        if not path.is_absolute():
            path = run_dir / path
        if path.is_file():
            candidates.add(path.resolve())
    return sorted(candidates)


def _argument_escapes_run_dir(
    argument: str,
    run_dir: Path,
    *,
    executable_position: bool,
) -> bool:
    if "{" in argument or "}" in argument or argument.startswith("-"):
        return False
    path = Path(argument)
    windows = PureWindowsPath(argument)
    posix = PurePosixPath(argument.replace("\\", "/"))
    if ".." in posix.parts:
        return True
    if executable_position:
        return False
    looks_like_path = path.is_absolute() or windows.drive or "/" in argument or "\\" in argument
    if not looks_like_path:
        return False
    candidate = path if path.is_absolute() else run_dir / path
    resolved = candidate.resolve()
    root = run_dir.resolve()
    return resolved != root and root not in resolved.parents


def _uses_unsafe_shell(content: str) -> bool:
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return True
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function_name = _call_name(node.func)
        if function_name in {
            "os.system",
            "os.popen",
            "subprocess.getoutput",
            "subprocess.getstatusoutput",
        }:
            return True
        if any(
            keyword.arg == "shell"
            and isinstance(keyword.value, ast.Constant)
            and keyword.value.value is True
            for keyword in node.keywords
        ):
            return True
        if (
            function_name
            in {
                "subprocess.call",
                "subprocess.check_call",
                "subprocess.check_output",
                "subprocess.Popen",
                "subprocess.run",
            }
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            return True
    return False


def _call_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _call_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return None


def _hardcodes_metric_value(
    content: str,
    *,
    metric_names: list[str],
) -> bool:
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return True
    normalized_metrics = {name.casefold() for name in metric_names}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values):
            key_name = (
                str(key.value).casefold()
                if isinstance(key, ast.Constant) and isinstance(key.value, str)
                else None
            )
            if (
                key_name in {"value", "metric_value", "score"}
                and isinstance(value, ast.Constant)
                and isinstance(value.value, (int, float))
                and not isinstance(value.value, bool)
            ):
                return True
            if key_name in normalized_metrics and _contains_numeric_metric_literal(value):
                return True
            if key_name == "metrics" and _contains_numeric_metric_literal(value):
                return True
    return False


def _contains_numeric_metric_literal(node: ast.AST) -> bool:
    if (
        isinstance(node, ast.Constant)
        and isinstance(node.value, (int, float))
        and not isinstance(node.value, bool)
    ):
        return True
    if not isinstance(node, ast.Dict):
        return False
    return any(
        _contains_numeric_metric_literal(value)
        for value in node.values
    )


def _error(
    category: str,
    message: str,
    method: str | None = None,
    file: str | None = None,
) -> StaticFinding:
    return StaticFinding(
        category=category,
        severity="ERROR",
        method=method,
        file=file,
        message=message,
    )


def _review_path(path: Path, run_dir: Path) -> str:
    try:
        return relative_to_run(path, run_dir)
    except ValueError:
        return "external-path-sha256:" + hashlib.sha256(
            str(path.resolve()).encode("utf-8")
        ).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _slug(value: str) -> str:
    from io_utils import slugify

    return slugify(value)
