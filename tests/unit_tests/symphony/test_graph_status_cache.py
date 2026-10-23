# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for Skill Graph status caching and build-log tail reads."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from jiuwenswarm.symphony import service as service_module
from jiuwenswarm.symphony.build import GraphBuildRuntimeFactory
from jiuwenswarm.symphony.config import symphony_config_from_dict
from jiuwenswarm.symphony.service import (
    SwarmSymphonyService,
    _read_build_log,
)


def _config(tmp_path: Path):
    skills_root = tmp_path / "skills"
    graph_dir = tmp_path / "graph"
    skills_root.mkdir()
    graph_dir.mkdir()
    return symphony_config_from_dict(
        {"paths": {"skills_root": str(skills_root), "graph_dir": str(graph_dir)}}
    )


def _install_fake_status(monkeypatch, config, *, calls: list[int]):
    def fake_graph_status(*args, **kwargs):
        del args, kwargs
        calls[0] += 1
        return SimpleNamespace(
            to_dict=lambda: {
                "success": True,
                "exists": False,
                "stale": True,
                "detail": "Symphony graph is missing",
            }
        )

    monkeypatch.setattr(
        "jiuwenswarm.symphony.service.load_symphony_config",
        lambda: config,
    )
    monkeypatch.setattr(
        "jiuwenswarm.symphony.service.graph_status",
        fake_graph_status,
    )


@pytest.mark.asyncio
async def test_graph_status_reuses_payload_when_inputs_are_unchanged(
    monkeypatch,
    tmp_path,
):
    config = _config(tmp_path)
    calls = [0]
    _install_fake_status(monkeypatch, config, calls=calls)
    service = SwarmSymphonyService()

    first = await service.graph_status()
    second = await service.graph_status()

    assert calls == [1]
    assert first == second


@pytest.mark.asyncio
async def test_real_status_scan_runs_once_for_unchanged_inputs(
    monkeypatch,
    tmp_path,
):
    config = _config(tmp_path)
    scan_calls = [0]
    real_scan = GraphBuildRuntimeFactory.scan

    def counting_scan(*args, **kwargs):
        scan_calls[0] += 1
        return real_scan(*args, **kwargs)

    monkeypatch.setattr(
        GraphBuildRuntimeFactory,
        "scan",
        staticmethod(counting_scan),
    )
    monkeypatch.setattr(
        "jiuwenswarm.symphony.service.load_symphony_config",
        lambda: config,
    )
    service = SwarmSymphonyService()

    first = await service.graph_status()
    second = await service.graph_status()

    assert scan_calls == [1]
    assert first["exists"] is False
    assert second == first


@pytest.mark.asyncio
async def test_skill_file_change_invalidates_graph_status(
    monkeypatch,
    tmp_path,
):
    config = _config(tmp_path)
    skill_dir = config.paths.skills_root / "writer"
    skill_dir.mkdir()
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text("---\nname: writer\n---\nold\n", encoding="utf-8")
    calls = [0]
    _install_fake_status(monkeypatch, config, calls=calls)
    service = SwarmSymphonyService()

    await service.graph_status()
    skill_file.write_text(
        "---\nname: writer\n---\nnew content\n",
        encoding="utf-8",
    )
    await service.graph_status()

    assert calls == [2]


@pytest.mark.asyncio
async def test_build_log_change_refreshes_payload_without_rescanning(
    monkeypatch,
    tmp_path,
):
    config = _config(tmp_path)
    log_path = config.paths.graph_dir / "build_log.jsonl"
    log_path.write_text('{"stage": "update.start"}\n', encoding="utf-8")
    calls = [0]
    _install_fake_status(monkeypatch, config, calls=calls)
    service = SwarmSymphonyService()

    await service.graph_status()
    log_path.write_text(
        '{"stage": "update.start"}\n{"stage": "update.done"}\n',
        encoding="utf-8",
    )
    status = await service.graph_status()

    assert calls == [1]
    assert [entry["stage"] for entry in status["build_log"]] == [
        "update.start",
        "update.done",
    ]


@pytest.mark.asyncio
async def test_explicit_invalidation_forces_a_new_status(monkeypatch, tmp_path):
    config = _config(tmp_path)
    calls = [0]
    _install_fake_status(monkeypatch, config, calls=calls)
    service = SwarmSymphonyService()

    await service.graph_status()
    service._invalidate_graph_status_cache()
    await service.graph_status()

    assert calls == [2]


@pytest.mark.asyncio
async def test_cached_payload_is_isolated_from_callers(monkeypatch, tmp_path):
    config = _config(tmp_path)
    calls = [0]
    _install_fake_status(monkeypatch, config, calls=calls)
    service = SwarmSymphonyService()

    first = await service.graph_status()
    first["build_log"].append({"stage": "caller-mutation"})
    second = await service.graph_status()

    assert calls == [1]
    assert second["build_log"] == []


def test_read_build_log_reads_only_the_tail(monkeypatch, tmp_path):
    log_path = tmp_path / "build_log.jsonl"
    lines = [json.dumps({"stage": f"stage-{index}"}) for index in range(200)]
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def fail_read_text(self, *args, **kwargs):
        del self, args, kwargs
        raise AssertionError("build log must not be read in full")

    monkeypatch.setattr(Path, "read_text", fail_read_text)

    entries = _read_build_log(tmp_path, limit=3)

    assert [entry["stage"] for entry in entries] == [
        "stage-197",
        "stage-198",
        "stage-199",
    ]


def test_read_build_log_handles_utf8_split_at_chunk_boundary(
    monkeypatch,
    tmp_path,
):
    log_path = tmp_path / "build_log.jsonl"
    lines = [
        json.dumps({"stage": f"技能-{index}"}, ensure_ascii=False)
        for index in range(40)
    ]
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    monkeypatch.setattr(service_module, "_BUILD_LOG_READ_CHUNK_BYTES", 7)

    entries = _read_build_log(tmp_path, limit=2)

    assert [entry["stage"] for entry in entries] == ["技能-38", "技能-39"]


def test_read_build_log_keeps_last_line_without_trailing_newline(tmp_path):
    log_path = tmp_path / "build_log.jsonl"
    log_path.write_text(
        '{"stage": "first"}\n{"stage": "second"}',
        encoding="utf-8",
    )

    entries = _read_build_log(tmp_path, limit=1)

    assert [entry["stage"] for entry in entries] == ["second"]
