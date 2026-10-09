"""Deterministic integrity checks for completed PAPER RSI runs."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Mapping


_SCHEMA_VERSION = "1.0"
_GATE_NAME = "artifact-provenance-gate"
_GATE_VERSION = "0.1.0"
_CITATION_RE = re.compile(r"\\citation\{([^}]*)\}")
_BIBCITE_RE = re.compile(r"\\bibcite\{([^}]*)\}")


@dataclass(frozen=True, slots=True)
class GateContext:
    task_id: str
    manager_run_id: str
    iteration: int
    run_dir: Path


@dataclass(frozen=True, slots=True)
class AuditInput:
    role: str
    path: str
    present: bool
    sha256: str | None = None

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "role": self.role,
            "path": self.path,
            "present": self.present,
        }
        if self.sha256 is not None:
            payload["sha256"] = self.sha256
        return payload


@dataclass(frozen=True, slots=True)
class AuditCheck:
    id: str
    severity: str
    status: str
    message: str
    evidence: tuple[tuple[str, Any], ...] = ()
    remediation: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "severity": self.severity,
            "status": self.status,
            "message": self.message,
            "evidence": [
                {"key": key, "value": value}
                for key, value in sorted(self.evidence, key=lambda item: item[0])
            ],
            "remediation": self.remediation,
        }


@dataclass(frozen=True, slots=True)
class AuditResult:
    context: GateContext
    evaluated_at: datetime
    decision: str
    checks: tuple[AuditCheck, ...]
    inputs: tuple[AuditInput, ...]
    coverage: Mapping[str, Mapping[str, Any]]
    summary: Mapping[str, int]

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "gate": {"name": _GATE_NAME, "version": _GATE_VERSION},
            "run": {
                "task_id": self.context.task_id,
                "manager_run_id": self.context.manager_run_id,
                "iteration": self.context.iteration,
            },
            "evaluated_at": _format_utc(self.evaluated_at),
            "decision": self.decision,
            "summary": dict(self.summary),
            "coverage": {
                name: {
                    "status": item["status"],
                    "reason": item["reason"],
                    "source": list(item["source"]),
                }
                for name, item in self.coverage.items()
            },
            "inputs": [
                item.as_dict()
                for item in sorted(self.inputs, key=lambda item: (item.role, item.path))
            ],
            "checks": [
                check.as_dict() for check in sorted(self.checks, key=lambda check: check.id)
            ],
        }


class ArtifactProvenanceGate:
    """Evaluate APG001-APG005 without modifying the PAPER run."""

    def evaluate(
        self,
        context: GateContext,
        *,
        evaluated_at: datetime | None = None,
    ) -> AuditResult:
        timestamp = evaluated_at or datetime.now(timezone.utc)
        if timestamp.tzinfo is None:
            raise ValueError("evaluated_at must be timezone-aware")
        timestamp = timestamp.astimezone(timezone.utc)

        run_dir = context.run_dir.resolve()
        experiment = run_dir / "experiments" / context.manager_run_id
        pdf_path = experiment / "paper" / "main.pdf"
        aux_path = experiment / "paper" / "main.aux"
        state_path = experiment / "manager" / "state.json"
        ledger_path = run_dir / "model_calls.jsonl"

        checks: list[AuditCheck] = []
        inputs: list[AuditInput] = []

        checks.append(self._check_pdf(run_dir, pdf_path))
        inputs.append(_input_for(run_dir, "final_paper", pdf_path))

        manager_reports, manager_check = self._check_manager_state(run_dir, state_path)
        checks.append(manager_check)
        inputs.append(_input_for(run_dir, "manager_state", state_path))

        reporting_reports = [
            report
            for report in manager_reports
            if report.get("module") == "reporting"
            and report.get("mode") == "run"
            and report.get("outcome") == "succeeded"
        ]
        artifact_check, artifact_inputs = self._check_reporting_artifacts(
            run_dir, reporting_reports
        )
        checks.append(artifact_check)
        inputs.extend(artifact_inputs)
        non_reporting_warning = self._check_non_reporting_artifacts(
            run_dir, manager_reports
        )
        if non_reporting_warning is not None:
            checks.append(non_reporting_warning)

        usage_check, usage_warning = self._check_usage_ledger(run_dir, ledger_path)
        checks.append(usage_check)
        if usage_warning is not None:
            checks.append(usage_warning)
        inputs.append(_input_for(run_dir, "runtime_usage", ledger_path))

        citation_check, citation_warning = self._check_citations(run_dir, aux_path)
        checks.append(citation_check)
        if citation_warning is not None:
            checks.append(citation_warning)
        inputs.append(_input_for(run_dir, "compiled_aux", aux_path))

        ordered_checks = tuple(sorted(checks, key=lambda check: check.id))
        failed = sum(
            check.status == "FAIL" and check.severity == "blocker"
            for check in ordered_checks
        )
        summary = {
            "passed": sum(
                check.status == "PASS" and check.severity == "blocker"
                for check in ordered_checks
            ),
            "failed": failed,
            "skipped": sum(check.status == "SKIPPED" for check in ordered_checks),
            "warnings": sum(check.severity == "warning" for check in ordered_checks),
        }
        coverage = _coverage(run_dir, pdf_path, ledger_path, aux_path)
        return AuditResult(
            context=context,
            evaluated_at=timestamp,
            decision="BLOCKED" if failed else "PASS",
            checks=ordered_checks,
            inputs=tuple(sorted(inputs, key=lambda item: (item.role, item.path))),
            coverage=coverage,
            summary=summary,
        )

    @staticmethod
    def _check_pdf(run_dir: Path, pdf_path: Path) -> AuditCheck:
        relative = _relative_path(run_dir, pdf_path)
        if not pdf_path.is_file():
            return _failure(
                "APG001",
                "Final paper PDF is missing or is not a regular file.",
                "Create the final paper at the required PAPER RSI path.",
                (("path", relative),),
            )
        size = pdf_path.stat().st_size
        if size == 0:
            return _failure(
                "APG001",
                "Final paper PDF is empty.",
                "Compile a nonempty final paper PDF.",
                (("path", relative), ("size_bytes", 0)),
            )
        return _pass(
            "APG001",
            "Final paper PDF is a nonempty regular file.",
            (("path", relative), ("size_bytes", size)),
        )

    @staticmethod
    def _check_manager_state(
        run_dir: Path, state_path: Path
    ) -> tuple[list[dict[str, Any]], AuditCheck]:
        relative = _relative_path(run_dir, state_path)
        if not state_path.is_file():
            return [], _failure(
                "APG002",
                "Manager state is missing.",
                "Preserve the Manager state.json for the completed run.",
                (("path", relative),),
            )
        try:
            payload = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return [], _failure(
                "APG002",
                "Manager state is not valid JSON.",
                "Write a complete JSON Manager state before finalization.",
                (("path", relative),),
            )
        if not isinstance(payload, dict):
            return [], _failure(
                "APG002",
                "Manager state must be a JSON object.",
                "Write the Manager state using the expected object schema.",
                (("path", relative),),
            )
        reports = payload.get("reports")
        if not isinstance(reports, list):
            return [], _failure(
                "APG002",
                "Manager state reports must be a list.",
                "Preserve the Manager reports list.",
                (("path", relative),),
            )
        manager_reports = [report for report in reports if isinstance(report, dict)]
        reporting_reports = [
            report
            for report in manager_reports
            if report.get("module") == "reporting"
            and report.get("mode") == "run"
            and report.get("outcome") == "succeeded"
        ]
        if not reporting_reports:
            return manager_reports, _failure(
                "APG002",
                "No successful terminal Reporting report was found.",
                "Complete the Reporting module successfully before finalization.",
                (("path", relative), ("report_count", len(reports))),
            )
        return manager_reports, _pass(
            "APG002",
            "Manager state contains a successful terminal Reporting report.",
            (("path", relative), ("successful_reporting_count", len(reporting_reports))),
        )

    @staticmethod
    def _check_reporting_artifacts(
        run_dir: Path, reporting_reports: list[dict[str, Any]]
    ) -> tuple[AuditCheck, list[AuditInput]]:
        if not reporting_reports:
            return (
                _failure(
                    "APG003",
                    "No successful Reporting report declares artifacts.",
                    "Complete Reporting and preserve its artifact paths.",
                ),
                [],
            )

        safe_paths: list[Path] = []
        failures: list[str] = []
        declared_count = 0
        for report in reporting_reports:
            raw_paths = report.get("artifact_paths")
            if not isinstance(raw_paths, list) or not raw_paths:
                failures.append("empty_artifact_paths")
                continue
            for raw_path in raw_paths:
                declared_count += 1
                if not isinstance(raw_path, str) or not raw_path.strip():
                    failures.append("invalid_artifact_path")
                    continue
                if _is_absolute(raw_path):
                    failures.append("absolute_artifact_path")
                    continue
                candidate = (run_dir / raw_path).resolve()
                if not candidate.is_relative_to(run_dir):
                    failures.append("artifact_path_escape")
                    continue
                if not candidate.exists():
                    failures.append("missing_artifact")
                    continue
                safe_paths.append(candidate)

        inputs = [_input_for(run_dir, "reporting_artifact", path) for path in safe_paths]
        if failures:
            return (
                _failure(
                    "APG003",
                    "Reporting artifact paths are missing, unsafe, or unresolved.",
                    "Use existing relative artifact paths contained within the run directory.",
                    (
                        ("declared_count", declared_count),
                        ("failure_types", sorted(set(failures))),
                        ("resolved_count", len(safe_paths)),
                    ),
                ),
                inputs,
            )
        return (
            _pass(
                "APG003",
                "All Reporting artifact paths resolve inside the run directory.",
                (
                    ("artifact_count", len(safe_paths)),
                    ("paths", sorted(_relative_path(run_dir, path) for path in safe_paths)),
                ),
            ),
            inputs,
        )

    @staticmethod
    def _check_non_reporting_artifacts(
        run_dir: Path, manager_reports: list[dict[str, Any]]
    ) -> AuditCheck | None:
        declared_count = 0
        failures: list[str] = []
        for report in manager_reports:
            if report.get("module") == "reporting":
                continue
            raw_paths = report.get("artifact_paths")
            if not isinstance(raw_paths, list):
                continue
            for raw_path in raw_paths:
                declared_count += 1
                if not isinstance(raw_path, str) or not raw_path.strip():
                    failures.append("invalid_artifact_path")
                    continue
                if _is_absolute(raw_path):
                    failures.append("absolute_artifact_path")
                    continue
                candidate = (run_dir / raw_path).resolve()
                if not candidate.is_relative_to(run_dir):
                    failures.append("artifact_path_escape")
                elif not candidate.exists():
                    failures.append("missing_artifact")

        if not failures:
            return None
        return AuditCheck(
            id="APG003_NON_REPORTING_ARTIFACT",
            severity="warning",
            status="PASS",
            message="Non-Reporting modules declared missing or unsafe artifacts.",
            evidence=(
                ("declared_count", declared_count),
                ("failure_types", sorted(set(failures))),
                ("unresolved_count", len(failures)),
            ),
            remediation="Preserve declared module artifacts under the run directory.",
        )

    @staticmethod
    def _check_usage_ledger(
        run_dir: Path, ledger_path: Path
    ) -> tuple[AuditCheck, AuditCheck | None]:
        relative = _relative_path(run_dir, ledger_path)
        if not ledger_path.is_file():
            return (
                _failure(
                    "APG004",
                    "Model-call usage ledger is missing.",
                    "Preserve run/model_calls.jsonl before finalization.",
                    (("path", relative),),
                ),
                None,
            )
        try:
            lines = ledger_path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            return (
                _failure(
                    "APG004",
                    "Model-call usage ledger cannot be read.",
                    "Write a readable UTF-8 model-call ledger.",
                    (("path", relative),),
                ),
                None,
            )

        unique: dict[str, tuple[int, int]] = {}
        duplicate_count = 0
        invalid_lines: list[int] = []
        for line_number, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                invalid_lines.append(line_number)
                continue
            if not isinstance(record, dict):
                invalid_lines.append(line_number)
                continue
            call_id = record.get("call_id")
            model_call = record.get("model_call")
            tokens = model_call.get("tokens") if isinstance(model_call, dict) else None
            input_tokens = tokens.get("input") if isinstance(tokens, dict) else None
            output_tokens = tokens.get("output") if isinstance(tokens, dict) else None
            if (
                not isinstance(call_id, str)
                or not call_id.strip()
                or not _valid_token_count(input_tokens)
                or not _valid_token_count(output_tokens)
            ):
                invalid_lines.append(line_number)
                continue
            if call_id in unique:
                duplicate_count += 1
                continue
            unique[call_id] = (input_tokens, output_tokens)

        if invalid_lines or not unique:
            return (
                _failure(
                    "APG004",
                    "Model-call ledger contains invalid rows or no traceable calls.",
                    "Record every call with a nonempty call_id and nonnegative integer input/output tokens.",
                    (
                        ("invalid_line_count", len(invalid_lines)),
                        ("invalid_lines", invalid_lines),
                        ("path", relative),
                        ("unique_call_count", len(unique)),
                    ),
                ),
                None,
            )

        check = _pass(
            "APG004",
            "Model-call ledger is complete and token counts are aggregable.",
            (
                ("duplicate_count", duplicate_count),
                ("input_tokens", sum(tokens[0] for tokens in unique.values())),
                ("output_tokens", sum(tokens[1] for tokens in unique.values())),
                ("path", relative),
                ("unique_call_count", len(unique)),
            ),
        )
        warning = None
        if duplicate_count:
            warning = AuditCheck(
                id="APG004_DUPLICATE_CALL_ID",
                severity="warning",
                status="PASS",
                message="Duplicate call IDs were counted once.",
                evidence=(("duplicate_count", duplicate_count),),
                remediation="Emit a unique call_id for every model call.",
            )
        return check, warning

    @staticmethod
    def _check_citations(
        run_dir: Path, aux_path: Path
    ) -> tuple[AuditCheck, AuditCheck | None]:
        relative = _relative_path(run_dir, aux_path)
        if not aux_path.is_file():
            return (
                AuditCheck(
                    id="APG005",
                    severity="blocker",
                    status="SKIPPED",
                    message="Compiled LaTeX AUX evidence is unavailable.",
                    evidence=(),
                    remediation=None,
                ),
                None,
            )
        try:
            content = aux_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return (
                _failure(
                    "APG005",
                    "Compiled LaTeX AUX cannot be read.",
                    "Preserve a readable UTF-8 AUX file or remove unreliable AUX evidence.",
                    (("path", relative),),
                ),
                None,
            )
        cited = {
            key.strip()
            for group in _CITATION_RE.findall(content)
            for key in group.split(",")
            if key.strip() and key.strip() != "*"
        }
        resolved = {key.strip() for key in _BIBCITE_RE.findall(content) if key.strip()}
        missing = sorted(cited - resolved)
        if missing:
            return (
                _failure(
                    "APG005",
                    "Compiled citations contain unresolved keys.",
                    "Resolve every cited key and recompile the paper.",
                    (("path", relative), ("unresolved_keys", missing)),
                ),
                None,
            )
        check = _pass(
            "APG005",
            "All compiled citation keys are resolved.",
            (
                ("cited_key_count", len(cited)),
                ("path", relative),
                ("resolved_key_count", len(resolved)),
            ),
        )
        warning = None
        if not cited:
            warning = AuditCheck(
                id="APG005_NO_CITATIONS",
                severity="warning",
                status="PASS",
                message="Compiled AUX contains no citation records.",
                evidence=(("path", relative),),
                remediation="Confirm that a citation-free paper is intentional.",
            )
        return check, warning


def _pass(
    check_id: str,
    message: str,
    evidence: tuple[tuple[str, Any], ...] = (),
) -> AuditCheck:
    return AuditCheck(
        id=check_id,
        severity="blocker",
        status="PASS",
        message=message,
        evidence=evidence,
        remediation=None,
    )


def _failure(
    check_id: str,
    message: str,
    remediation: str,
    evidence: tuple[tuple[str, Any], ...] = (),
) -> AuditCheck:
    return AuditCheck(
        id=check_id,
        severity="blocker",
        status="FAIL",
        message=message,
        evidence=evidence,
        remediation=remediation,
    )


def _input_for(run_dir: Path, role: str, path: Path) -> AuditInput:
    present = path.exists()
    digest = _sha256(path) if path.is_file() else None
    return AuditInput(
        role=role,
        path=_relative_path(run_dir, path),
        present=present,
        sha256=digest,
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _relative_path(run_dir: Path, path: Path) -> str:
    return path.resolve().relative_to(run_dir.resolve()).as_posix()


def _is_absolute(raw_path: str) -> bool:
    return PureWindowsPath(raw_path).is_absolute() or PurePosixPath(raw_path).is_absolute()


def _valid_token_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _coverage(
    run_dir: Path, pdf_path: Path, ledger_path: Path, aux_path: Path
) -> dict[str, dict[str, Any]]:
    citations_available = aux_path.is_file()
    return {
        "final_paper": {
            "status": "checked",
            "reason": None,
            "source": [_relative_path(run_dir, pdf_path)],
        },
        "runtime_usage": {
            "status": "checked",
            "reason": None,
            "source": [_relative_path(run_dir, ledger_path)],
        },
        "citations": {
            "status": "checked" if citations_available else "skipped",
            "reason": None if citations_available else "compiled_aux_not_available",
            "source": [_relative_path(run_dir, aux_path)] if citations_available else [],
        },
        "numeric_claims": {
            "status": "skipped",
            "reason": "no_reliable_contract",
            "source": [],
        },
        "experiment_protocol": {
            "status": "skipped",
            "reason": "no_reliable_contract",
            "source": [],
        },
        "negative_results": {
            "status": "skipped",
            "reason": "no_reliable_contract",
            "source": [],
        },
    }


def _format_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
