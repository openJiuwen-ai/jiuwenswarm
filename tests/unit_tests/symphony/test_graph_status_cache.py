# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for Skill Graph status caching and build-log tail reads."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import stat as stat_module
import threading
import time
from types import SimpleNamespace

import pytest

from jiuwenswarm.symphony import service as service_module
from jiuwenswarm.symphony.build import GraphBuildRuntimeFactory
from jiuwenswarm.symphony.config import symphony_config_from_dict
from jiuwenswarm.symphony.service import (
    SwarmSymphonyService,
    _cache_excludes_directory,
    _cache_excludes_file,
    _cache_is_link_stat,
    _read_build_log,
)


def _config(tmp_path: Path, *, max_depth: int | None = None):
    skills_root = tmp_path / "skills"
    graph_dir = tmp_path / "graph"
    skills_root.mkdir()
    graph_dir.mkdir()
    return symphony_config_from_dict(
        {
            "paths": {"skills_root": str(skills_root), "graph_dir": str(graph_dir)},
            "fingerprint": {"scan": {"max_depth": max_depth}},
        }
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
                "diagnostics": {"added": []},
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


@pytest.mark.skipif(
    os.name == "nt",
    reason="Windows ctime is creation time, not a change time",
)
@pytest.mark.asyncio
async def test_preserved_mtime_change_invalidates_graph_status(
    monkeypatch,
    tmp_path,
):
    config = _config(tmp_path)
    skill_dir = config.paths.skills_root / "writer"
    skill_dir.mkdir()
    skill_file = skill_dir / "SKILL.md"
    original_text = "---\nname: writer\n---\nabc\n"
    changed_text = "---\nname: writer\n---\nxyz\n"
    assert len(original_text) == len(changed_text)
    skill_file.write_text(original_text, encoding="utf-8")
    calls = [0]
    _install_fake_status(monkeypatch, config, calls=calls)
    service = SwarmSymphonyService()

    await service.graph_status()
    original_stat = skill_file.stat()
    time.sleep(0.01)
    skill_file.write_text(changed_text, encoding="utf-8")
    os.utime(
        skill_file,
        ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
    )
    changed_stat = skill_file.stat()
    assert changed_stat.st_size == original_stat.st_size
    assert changed_stat.st_mtime_ns == original_stat.st_mtime_ns

    await service.graph_status()

    assert calls == [2]


@pytest.mark.asyncio
async def test_cache_expires_after_ttl(monkeypatch, tmp_path):
    config = _config(tmp_path)
    calls = [0]
    _install_fake_status(monkeypatch, config, calls=calls)
    service = SwarmSymphonyService()

    await service.graph_status()
    service._graph_status_cache_created_at = 0.0
    await service.graph_status()

    assert calls == [2]


@pytest.mark.asyncio
async def test_nested_asset_change_invalidates_with_shallow_max_depth(
    monkeypatch,
    tmp_path,
):
    config = _config(tmp_path, max_depth=1)
    skill_dir = config.paths.skills_root / "writer"
    scripts_dir = skill_dir / "scripts"
    scripts_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: writer\n---\nbody\n",
        encoding="utf-8",
    )
    helper = scripts_dir / "helper.py"
    helper.write_text("print('one')\n", encoding="utf-8")
    calls = [0]
    _install_fake_status(monkeypatch, config, calls=calls)
    service = SwarmSymphonyService()

    await service.graph_status()
    helper.write_text("print('two changed')\n", encoding="utf-8")
    await service.graph_status()

    assert calls == [2]


@pytest.mark.asyncio
async def test_excluded_asset_directories_do_not_invalidate(
    monkeypatch,
    tmp_path,
):
    config = _config(tmp_path)
    skill_dir = config.paths.skills_root / "writer"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: writer\n---\nbody\n",
        encoding="utf-8",
    )
    node_modules = skill_dir / "node_modules" / "pkg"
    node_modules.mkdir(parents=True)
    (node_modules / "index.js").write_text("old\n", encoding="utf-8")
    git_dir = skill_dir / ".git" / "objects"
    git_dir.mkdir(parents=True)
    (git_dir / "blob").write_text("old\n", encoding="utf-8")
    calls = [0]
    _install_fake_status(monkeypatch, config, calls=calls)
    service = SwarmSymphonyService()

    await service.graph_status()
    (node_modules / "index.js").write_text("new content\n", encoding="utf-8")
    (git_dir / "blob").write_text("new content\n", encoding="utf-8")
    await service.graph_status()

    assert calls == [1]


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
    first["detail"] = "caller-mutation"
    first["diagnostics"]["added"].append("caller-mutation")
    second = await service.graph_status()

    assert calls == [1]
    assert second["detail"] == "Symphony graph is missing"
    assert second["diagnostics"] == {"added": []}


@pytest.mark.asyncio
async def test_concurrent_cache_miss_scans_once(monkeypatch, tmp_path):
    config = _config(tmp_path)
    calls = [0]

    def slow_graph_status(*args, **kwargs):
        del args, kwargs
        calls[0] += 1
        time.sleep(0.05)
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
        slow_graph_status,
    )
    service = SwarmSymphonyService()

    await asyncio.gather(
        service.graph_status(),
        service.graph_status(),
    )

    assert calls == [1]


@pytest.mark.asyncio
async def test_invalidation_during_scan_does_not_cache_stale_result(
    monkeypatch,
    tmp_path,
):
    config = _config(tmp_path)
    calls = [0]
    started = threading.Event()
    release = threading.Event()

    def blocking_graph_status(*args, **kwargs):
        del args, kwargs
        calls[0] += 1
        if calls[0] == 1:
            started.set()
            release.wait(timeout=2)
            detail = "stale"
        else:
            detail = "fresh"
        return SimpleNamespace(
            to_dict=lambda: {
                "success": True,
                "exists": False,
                "stale": True,
                "detail": detail,
            }
        )

    monkeypatch.setattr(
        "jiuwenswarm.symphony.service.load_symphony_config",
        lambda: config,
    )
    monkeypatch.setattr(
        "jiuwenswarm.symphony.service.graph_status",
        blocking_graph_status,
    )
    service = SwarmSymphonyService()

    task = asyncio.create_task(service.graph_status())
    assert await asyncio.to_thread(started.wait, 2)
    service._invalidate_graph_status_cache()
    release.set()
    first = await task

    assert first["detail"] == "stale"
    second = await service.graph_status()
    assert calls == [2]
    assert second["detail"] == "fresh"


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


def test_read_build_log_reads_bounded_bytes(monkeypatch, tmp_path):
    log_path = tmp_path / "build_log.jsonl"
    lines = [json.dumps({"stage": f"stage-{index}"}) for index in range(20_000)]
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    file_size = log_path.stat().st_size
    bytes_read = [0]
    real_open = Path.open

    class CountingHandle:
        def __init__(self, handle):
            self._handle = handle

        def __enter__(self):
            self._handle.__enter__()
            return self

        def __exit__(self, *args):
            return self._handle.__exit__(*args)

        def seek(self, *args):
            return self._handle.seek(*args)

        def tell(self):
            return self._handle.tell()

        def read(self, size=-1):
            data = self._handle.read(size)
            bytes_read[0] += len(data)
            return data

    def counting_open(self, *args, **kwargs):
        return CountingHandle(real_open(self, *args, **kwargs))

    monkeypatch.setattr(Path, "open", counting_open)

    entries = _read_build_log(tmp_path, limit=3)

    assert [entry["stage"] for entry in entries] == [
        "stage-19997",
        "stage-19998",
        "stage-19999",
    ]
    assert 0 < bytes_read[0] < file_size // 4


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


def test_cache_exclusions_follow_expected_scanner_rules():
    """False exclusions would hide Skill changes from the cache key."""
    directories = {
        ".git": True,
        ".venv": True,
        "__pycache__": True,
        ".cache": True,
        "node_modules": True,
        "api_keys": True,
        "secret_store": True,
        "my_credentials": True,
        "token_cache": True,
        "passwords": True,
        "private_keys": True,
        "hockey": False,
        "monkey": False,
        "src": False,
        "scripts": False,
    }
    files = {
        "SKILL.md": False,
        "index.js": False,
        "notes.md": False,
        "id_rsa": True,
        "id_rsa.pub": True,
        "server.pem": True,
        "token.json": True,
        "secrets.yaml": True,
        "password.txt": True,
        "api_key.py": True,
        "hockey.py": False,
        "tokenizer.py": False,
        "public.py": False,
        ".env": True,
        ".env.local": True,
        "x.pyc": True,
    }

    for name, expected in directories.items():
        assert _cache_excludes_directory(name) is expected, name
    for name, expected in files.items():
        assert _cache_excludes_file(name) is expected, name


def test_cache_link_detection_handles_windows_reparse_points(monkeypatch):
    reparse_flag = 0x400
    monkeypatch.setattr(
        stat_module,
        "FILE_ATTRIBUTE_REPARSE_POINT",
        reparse_flag,
        raising=False,
    )
    regular = SimpleNamespace(
        st_mode=stat_module.S_IFREG,
        st_file_attributes=0,
    )
    reparse = SimpleNamespace(
        st_mode=stat_module.S_IFREG,
        st_file_attributes=reparse_flag,
    )
    symlink = SimpleNamespace(
        st_mode=stat_module.S_IFLNK,
        st_file_attributes=0,
    )

    assert _cache_is_link_stat(regular) is False
    assert _cache_is_link_stat(reparse) is True
    assert _cache_is_link_stat(symlink) is True
