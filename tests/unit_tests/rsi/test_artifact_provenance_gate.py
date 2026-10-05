import json
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from pathlib import Path

import pytest

from jiuwenswarm.agents.harness.common.rsi.artifact_provenance_gate import (
    ArtifactProvenanceGate,
    GateContext,
)


EVALUATED_AT = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)


def _write(path: Path, content: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")


def _valid_context(tmp_path: Path) -> GateContext:
    run_dir = tmp_path / "run"
    manager_run_id = "task-001-iteration-001"
    experiment = run_dir / "experiments" / manager_run_id
    _write(experiment / "paper" / "main.pdf", b"%PDF-1.7\n")
    _write(
        experiment / "paper" / "main.aux",
        "\\citation{alpha,beta}\n\\bibcite{alpha}{1}\n\\bibcite{beta}{2}\n",
    )
    _write(experiment / "design" / "report.md", "# Report\n")
    _write(
        experiment / "manager" / "state.json",
        json.dumps(
            {
                "reports": [
                    {
                        "report_id": "reporting:1:1",
                        "module": "reporting",
                        "mode": "run",
                        "attempt": 1,
                        "outcome": "succeeded",
                        "artifact_paths": [
                            f"experiments/{manager_run_id}/design/report.md"
                        ],
                    }
                ]
            }
        ),
    )
    _write(
        run_dir / "model_calls.jsonl",
        json.dumps(
            {
                "call_id": "call-001",
                "status": "succeeded",
                "model_call": {"tokens": {"input": 12, "output": 7}},
            }
        )
        + "\n",
    )
    return GateContext(
        task_id="task-001",
        manager_run_id=manager_run_id,
        iteration=1,
        run_dir=run_dir,
    )


def _evaluate(context: GateContext, *, evaluated_at: datetime = EVALUATED_AT):
    return ArtifactProvenanceGate().evaluate(context, evaluated_at=evaluated_at)


def _check(result, check_id: str):
    return next(check for check in result.checks if check.id == check_id)


def test_complete_run_passes_with_stable_schema_and_rule_order(tmp_path: Path):
    result = _evaluate(_valid_context(tmp_path))
    payload = result.as_dict()

    assert result.decision == "PASS"
    assert payload["schema_version"] == "1.0"
    assert payload["gate"] == {
        "name": "artifact-provenance-gate",
        "version": "0.1.0",
    }
    assert payload["run"] == {
        "task_id": "task-001",
        "manager_run_id": "task-001-iteration-001",
        "iteration": 1,
    }
    assert payload["evaluated_at"] == "2026-09-29T12:00:00Z"
    assert [check["id"] for check in payload["checks"]] == [
        "APG001",
        "APG002",
        "APG003",
        "APG004",
        "APG005",
    ]
    assert payload["summary"] == {
        "passed": 5,
        "failed": 0,
        "skipped": 0,
        "warnings": 0,
    }
    assert payload["coverage"]["citations"]["status"] == "checked"
    with pytest.raises(FrozenInstanceError):
        result.decision = "BLOCKED"


@pytest.mark.parametrize("condition", ["missing", "empty", "directory"])
def test_apg001_blocks_when_final_pdf_is_not_a_nonempty_file(
    tmp_path: Path, condition: str
):
    context = _valid_context(tmp_path)
    pdf = context.run_dir / "experiments" / context.manager_run_id / "paper" / "main.pdf"
    pdf.unlink()
    if condition == "empty":
        pdf.touch()
    elif condition == "directory":
        pdf.mkdir()

    result = _evaluate(context)

    assert result.decision == "BLOCKED"
    assert (_check(result, "APG001").severity, _check(result, "APG001").status) == (
        "blocker",
        "FAIL",
    )


@pytest.mark.parametrize(
    ("state_content", "expected_fragment"),
    [
        (None, "missing"),
        ("not-json", "JSON"),
        (json.dumps([]), "object"),
        (json.dumps({"reports": {}}), "reports"),
        (json.dumps({"reports": []}), "Reporting"),
        (
            json.dumps(
                {
                    "reports": [
                        {
                            "module": "reporting",
                            "mode": "run",
                            "outcome": "failed",
                            "artifact_paths": ["report.md"],
                        }
                    ]
                }
            ),
            "Reporting",
        ),
    ],
)
def test_apg002_blocks_invalid_or_unsuccessful_manager_state(
    tmp_path: Path, state_content: str | None, expected_fragment: str
):
    context = _valid_context(tmp_path)
    state = (
        context.run_dir
        / "experiments"
        / context.manager_run_id
        / "manager"
        / "state.json"
    )
    state.unlink()
    if state_content is not None:
        _write(state, state_content)

    result = _evaluate(context)
    check = _check(result, "APG002")

    assert result.decision == "BLOCKED"
    assert check.status == "FAIL"
    assert expected_fragment.lower() in check.message.lower()


@pytest.mark.parametrize(
    "artifact_paths",
    [
        [],
        [r"C:\outside\report.md"],
        ["../outside/report.md"],
        ["experiments/task-001-iteration-001/design/missing.md"],
    ],
)
def test_apg003_blocks_missing_or_unsafe_reporting_artifacts(
    tmp_path: Path, artifact_paths: list[str]
):
    context = _valid_context(tmp_path)
    state = (
        context.run_dir
        / "experiments"
        / context.manager_run_id
        / "manager"
        / "state.json"
    )
    payload = json.loads(state.read_text(encoding="utf-8"))
    payload["reports"][0]["artifact_paths"] = artifact_paths
    state.write_text(json.dumps(payload), encoding="utf-8")

    result = _evaluate(context)

    assert result.decision == "BLOCKED"
    assert _check(result, "APG003").status == "FAIL"


def test_apg003_accepts_reporting_artifact_directories(tmp_path: Path):
    context = _valid_context(tmp_path)
    experiment = context.run_dir / "experiments" / context.manager_run_id
    state_path = experiment / "manager" / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["reports"][0]["artifact_paths"] = [
        (experiment / "paper").relative_to(context.run_dir).as_posix()
    ]
    _write(state_path, json.dumps(state))

    result = _evaluate(context)

    assert result.decision == "PASS"
    assert _check(result, "APG003").status == "PASS"


def test_apg003_blocks_symlink_escape_when_symlinks_are_supported(tmp_path: Path):
    context = _valid_context(tmp_path)
    outside = tmp_path / "outside.md"
    outside.write_text("outside", encoding="utf-8")
    link = context.run_dir / "escaped.md"
    try:
        link.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    state = (
        context.run_dir
        / "experiments"
        / context.manager_run_id
        / "manager"
        / "state.json"
    )
    payload = json.loads(state.read_text(encoding="utf-8"))
    payload["reports"][0]["artifact_paths"] = ["escaped.md"]
    state.write_text(json.dumps(payload), encoding="utf-8")

    result = _evaluate(context)

    assert result.decision == "BLOCKED"
    assert _check(result, "APG003").status == "FAIL"


def test_apg003_warns_for_missing_or_unsafe_non_reporting_artifacts(tmp_path: Path):
    context = _valid_context(tmp_path)
    state = (
        context.run_dir
        / "experiments"
        / context.manager_run_id
        / "manager"
        / "state.json"
    )
    payload = json.loads(state.read_text(encoding="utf-8"))
    payload["reports"].append(
        {
            "report_id": "ideation:1:1",
            "module": "ideation",
            "mode": "run",
            "outcome": "succeeded",
            "artifact_paths": ["missing.md", r"C:\outside\notes.md"],
        }
    )
    state.write_text(json.dumps(payload), encoding="utf-8")

    result = _evaluate(context)
    warning = _check(result, "APG003_NON_REPORTING_ARTIFACT")
    evidence = {item["key"]: item["value"] for item in warning.as_dict()["evidence"]}

    assert result.decision == "PASS"
    assert _check(result, "APG003").status == "PASS"
    assert (warning.severity, warning.status) == ("warning", "PASS")
    assert evidence == {
        "declared_count": 2,
        "failure_types": ["absolute_artifact_path", "missing_artifact"],
        "unresolved_count": 2,
    }
    assert result.summary["warnings"] == 1


@pytest.mark.parametrize(
    "ledger_content",
    [
        None,
        "",
        "not-json\n",
        "[]\n",
        json.dumps(
            {"call_id": "", "model_call": {"tokens": {"input": 1, "output": 2}}}
        ),
        json.dumps({"call_id": "call-1", "model_call": {"tokens": {"input": 1}}}),
        json.dumps(
            {
                "call_id": "call-1",
                "model_call": {"tokens": {"input": True, "output": 2}},
            }
        ),
        json.dumps(
            {
                "call_id": "call-1",
                "model_call": {"tokens": {"input": -1, "output": 2}},
            }
        ),
        json.dumps(
            {
                "call_id": "call-valid",
                "status": "succeeded",
                "model_call": {"tokens": {"input": 1, "output": 2}},
            }
        )
        + "\n"
        + json.dumps(
            {
                "call_id": "call-failed",
                "status": "failed",
                "model_call": {"tokens": {"input": None, "output": None}},
            }
        ),
    ],
)
def test_apg004_blocks_any_incomplete_or_invalid_nonempty_ledger_line(
    tmp_path: Path, ledger_content: str | None
):
    context = _valid_context(tmp_path)
    ledger = context.run_dir / "model_calls.jsonl"
    ledger.unlink()
    if ledger_content is not None:
        _write(ledger, ledger_content + ("\n" if ledger_content else ""))

    result = _evaluate(context)

    assert result.decision == "BLOCKED"
    assert _check(result, "APG004").status == "FAIL"


def test_apg004_counts_duplicate_valid_call_ids_once_and_warns(tmp_path: Path):
    context = _valid_context(tmp_path)
    ledger = context.run_dir / "model_calls.jsonl"
    record = {
        "call_id": "call-001",
        "model_call": {"tokens": {"input": 12, "output": 7}},
    }
    ledger.write_text(
        json.dumps(record) + "\n" + json.dumps(record) + "\n", encoding="utf-8"
    )

    result = _evaluate(context)
    usage_check = _check(result, "APG004")
    warning = _check(result, "APG004_DUPLICATE_CALL_ID")

    assert result.decision == "PASS"
    assert usage_check.status == "PASS"
    assert {item["key"]: item["value"] for item in usage_check.as_dict()["evidence"]}[
        "unique_call_count"
    ] == 1
    assert (warning.severity, warning.status) == ("warning", "PASS")
    assert result.summary["warnings"] == 1


def test_apg005_blocks_unresolved_citations(tmp_path: Path):
    context = _valid_context(tmp_path)
    aux = context.run_dir / "experiments" / context.manager_run_id / "paper" / "main.aux"
    aux.write_text(
        "\\citation{alpha,missing}\n\\bibcite{alpha}{1}\n", encoding="utf-8"
    )

    result = _evaluate(context)

    assert result.decision == "BLOCKED"
    assert _check(result, "APG005").status == "FAIL"


def test_apg005_is_skipped_without_aux_and_does_not_block(tmp_path: Path):
    context = _valid_context(tmp_path)
    aux = context.run_dir / "experiments" / context.manager_run_id / "paper" / "main.aux"
    aux.unlink()

    result = _evaluate(context)
    payload = result.as_dict()

    assert result.decision == "PASS"
    assert _check(result, "APG005").status == "SKIPPED"
    assert payload["coverage"]["citations"] == {
        "status": "skipped",
        "reason": "compiled_aux_not_available",
        "source": [],
    }
    assert payload["summary"] == {
        "passed": 4,
        "failed": 0,
        "skipped": 1,
        "warnings": 0,
    }


def test_apg005_warns_but_passes_when_aux_has_no_citations(tmp_path: Path):
    context = _valid_context(tmp_path)
    aux = context.run_dir / "experiments" / context.manager_run_id / "paper" / "main.aux"
    aux.write_text("\\relax\n", encoding="utf-8")

    result = _evaluate(context)

    assert result.decision == "PASS"
    assert _check(result, "APG005").status == "PASS"
    assert _check(result, "APG005_NO_CITATIONS").severity == "warning"


def test_apg005_allows_bibtex_wildcard_citation(tmp_path: Path):
    context = _valid_context(tmp_path)
    aux = context.run_dir / "experiments" / context.manager_run_id / "paper" / "main.aux"
    aux.write_text(
        "\\citation{*}\n\\bibcite{source-a}{{1}{2026}}{{Author}}{{}}}\n",
        encoding="utf-8",
    )

    result = _evaluate(context)

    assert result.decision == "PASS"
    assert _check(result, "APG005").status == "PASS"


def test_serialization_uses_relative_paths_hashes_and_stable_order(tmp_path: Path):
    context = _valid_context(tmp_path)

    first = _evaluate(context, evaluated_at=EVALUATED_AT).as_dict()
    second = _evaluate(
        context,
        evaluated_at=datetime(2026, 9, 29, 12, 1, tzinfo=timezone.utc),
    ).as_dict()
    first_time = first.pop("evaluated_at")
    second_time = second.pop("evaluated_at")
    serialized = json.dumps(first, sort_keys=True)

    assert first == second
    assert first_time != second_time
    assert str(tmp_path) not in serialized
    assert first["inputs"] == sorted(
        first["inputs"], key=lambda item: (item["role"], item["path"])
    )
    assert all(not Path(item["path"]).is_absolute() for item in first["inputs"])
    assert all(
        len(item["sha256"]) == 64
        for item in first["inputs"]
        if item["present"]
    )
    assert first["coverage"]["numeric_claims"] == {
        "status": "skipped",
        "reason": "no_reliable_contract",
        "source": [],
    }
