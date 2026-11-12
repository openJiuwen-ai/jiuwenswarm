# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Unit tests for Runtime Worker endpoint isolation."""

from __future__ import annotations

from pathlib import Path

from jiuwenswarm.server.supervisor import worker_process


def test_default_socket_path_is_scoped_to_named_instance(
    tmp_path: Path,
    monkeypatch,
) -> None:
    instance = tmp_path / "instances" / "rea-beta4-mac"
    monkeypatch.setenv("JIUWENSWARM_DATA_DIR", str(instance))
    monkeypatch.delenv("JIUWENSWARM_RUNTIME_SOCKET", raising=False)

    socket_path = Path(worker_process.default_socket_path())

    assert socket_path == instance / "agent" / "run" / "runtime-worker.sock"


def test_runtime_socket_override_has_priority(
    tmp_path: Path,
    monkeypatch,
) -> None:
    override = tmp_path / "custom-runtime.sock"
    monkeypatch.setenv("JIUWENSWARM_DATA_DIR", str(tmp_path / "instance"))
    monkeypatch.setenv("JIUWENSWARM_RUNTIME_SOCKET", str(override))

    assert worker_process.default_socket_path() == str(override)
