# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""When-gate evidence JSONL (no LLM, no network)."""
from __future__ import annotations

import json
from pathlib import Path

from jiuwenswarm.agents.harness.common.rails.admission_evidence import (
    emit_when_gate,
    evidence_log_path_from_env,
    maybe_log_when_gate,
)


def test_emit_when_gate_reconstructible(tmp_path: Path, monkeypatch):
    p = tmp_path / "evidence.jsonl"
    emit_when_gate(p, allowed=True, reason="improvable", recent_success_rate=0.67)
    emit_when_gate(p, allowed=False, reason="converged", recent_success_rate=1.0)
    rows = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x]
    assert len(rows) == 2
    assert rows[0]["kind"] == "when_gate"
    assert rows[0]["decision"] == "allow_evolve"
    assert rows[0]["verified"] is True
    assert rows[1]["decision"] == "suppress"
    assert rows[1]["verifier_version"] == "skillforge-vag-1.0"
    monkeypatch.setenv("JIUWEN_EVIDENCE_LOG", str(p))
    assert evidence_log_path_from_env() == str(p)
    maybe_log_when_gate(allowed=False, reason="insufficient", recent_success_rate=-1.0)
    rows2 = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x]
    assert len(rows2) == 3


def test_maybe_log_disabled_is_noop(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("JIUWEN_EVIDENCE_LOG", raising=False)
    maybe_log_when_gate(allowed=True, reason="x", recent_success_rate=0.5)
    assert list(tmp_path.iterdir()) == []
