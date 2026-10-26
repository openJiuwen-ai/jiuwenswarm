# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import jiuwenswarm.server.app_agentserver as app_agentserver


def test_set_exit_reason_updates_module_state() -> None:
    app_agentserver._set_exit_reason("test_reason")
    assert app_agentserver._EXIT_REASON == "test_reason"


def test_atexit_log_exit_reason_emits_critical(monkeypatch) -> None:
    messages: list[str] = []

    def _capture(msg: str, *args: object) -> None:
        messages.append(msg % args if args else msg)

    monkeypatch.setattr(app_agentserver.logger, "critical", _capture)
    app_agentserver._set_exit_reason("clean_shutdown")
    app_agentserver._atexit_log_exit_reason()
    assert any("atexit reason=clean_shutdown" in message for message in messages)
