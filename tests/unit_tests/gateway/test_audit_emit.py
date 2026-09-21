# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import pytest

from openjiuwen_runtime.foundation.audit import (
    AuditManager,
    MemoryEmitter,
    reset_audit_manager,
)

from jiuwenswarm.common.audit_emit import (
    AuditTimer,
    emit_audit_evt,
    emit_audit_ua,
)


@pytest.fixture(autouse=True)
def _audit_memory():
    mem = MemoryEmitter()
    reset_audit_manager(AuditManager(emitter=mem))
    yield mem
    reset_audit_manager()


def test_emit_audit_ua_swallows_log_audit_errors(monkeypatch) -> None:
    def _boom(*_args, **_kwargs):
        raise RuntimeError("audit down")

    monkeypatch.setattr(
        "openjiuwen_runtime.foundation.audit.log_audit",
        _boom,
    )
    emit_audit_ua(SUBMDL="gateway", PROC="http_agent_send", UA="u1")


def test_emit_audit_records_ua_and_evt(
    _audit_memory: MemoryEmitter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    emit_audit_ua(SUBMDL="gateway", PROC="http_agent_send", UA="ok", COST=12)
    emit_audit_evt(
        SUBMDL="gateway",
        PROC="web_rpc_authorize",
        MSG="denied",
        EVT="FORBIDDEN",
    )
    assert len(_audit_memory.records) == 2
    ua_attrs = _audit_memory.records[0]["attributes"]
    evt_attrs = _audit_memory.records[1]["attributes"]
    assert ua_attrs.get("PROC") == "http_agent_send"
    assert evt_attrs.get("PROC") == "web_rpc_authorize"
    assert evt_attrs.get("RSPCD") == "E999"


def test_emit_audit_caller_is_business_site(_audit_memory: MemoryEmitter, monkeypatch) -> None:
    monkeypatch.setenv("JIUWENSWARM_EDITION", "enterprise")
    emit_audit_ua(SUBMDL="file", PROC="file_download", filename="hello.md")
    caller = _audit_memory.records[0]["attributes"]["caller"]
    assert caller.startswith("test_audit_emit.test_emit_audit_caller_is_business_site:")
    assert "audit_claw_log" not in caller


def test_audit_timer_cost_ms() -> None:
    with AuditTimer() as t:
        pass
    assert t.cost_ms >= 0
