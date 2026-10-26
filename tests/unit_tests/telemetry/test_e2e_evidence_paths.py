from __future__ import annotations

import importlib

from ruamel.yaml import YAML

from scripts import e2e_telemetry_trace


def test_evidence_defaults_are_absolute_and_under_repository(monkeypatch) -> None:
    monkeypatch.delenv("TELEMETRY_EVIDENCE_DIR", raising=False)
    monkeypatch.delenv("TELEMETRY_E2E_EVIDENCE", raising=False)
    module = importlib.reload(e2e_telemetry_trace)

    evidence_root = module.REPO_ROOT / ".telemetry-evidence"
    assert module.DEFAULT_COLLECTOR_EVIDENCE_DIR == evidence_root
    assert module.DEFAULT_EVIDENCE == evidence_root / "evidence.json"
    assert module.DEFAULT_TRACE_JSON == evidence_root / "traces.jsonl"
    assert module.DEFAULT_METRIC_JSON == evidence_root / "metrics.jsonl"
    assert all(
        path.is_absolute()
        for path in (
            module.DEFAULT_COLLECTOR_EVIDENCE_DIR,
            module.DEFAULT_EVIDENCE,
            module.DEFAULT_TRACE_JSON,
            module.DEFAULT_METRIC_JSON,
        )
    )


def test_compose_uses_windows_safe_long_bind_syntax() -> None:
    compose_path = (
        e2e_telemetry_trace.REPO_ROOT
        / "deploy"
        / "observability"
        / "docker-compose.yml"
    )
    compose = YAML(typ="safe").load(compose_path.read_text(encoding="utf-8"))
    evidence_mount = compose["services"]["otel-collector"]["volumes"][1]

    assert evidence_mount == {
        "type": "bind",
        "source": "${TELEMETRY_EVIDENCE_DIR:-../../.telemetry-evidence}",
        "target": "/var/lib/otel/evidence",
    }


def test_e2e_parser_defers_faas_entrypoint() -> None:
    parser = e2e_telemetry_trace.build_parser()
    entrypoint_action = next(
        action for action in parser._actions if action.dest == "entrypoint"
    )

    assert tuple(entrypoint_action.choices) == ("gateway", "team-runner")
