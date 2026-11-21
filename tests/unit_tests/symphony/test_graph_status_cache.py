"""Core-owned graph freshness and host build-log tail reads."""
import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from jiuwenswarm.runtime.symphony import service as service_module
from jiuwenswarm.runtime.symphony.service import SwarmSymphonyService, _read_build_log, _build_log_path
from jiuwenswarm.common.symphony_config import symphony_config_from_dict

@pytest.mark.asyncio
async def test_status_delegates_inventory_and_freshness_to_core_each_time(tmp_path, monkeypatch):
    config = symphony_config_from_dict({"capability": {"sources": [{"type": "skill", "root": str(tmp_path / "skills")}]},
        "storage": {"artifact_root": str(tmp_path / "runtime")}, "orchestration": {"enabled": True}})
    calls = []
    def status(value, **kwargs):
        calls.append((value, kwargs))
        return SimpleNamespace(to_dict=lambda: {"success": True, "exists": False, "stale": True, "added_count": len(calls)})
    monkeypatch.setattr(service_module, "load_symphony_config", lambda: config)
    monkeypatch.setattr(service_module, "graph_status", status)
    service = SwarmSymphonyService()
    assert (await service.graph_status())["added_count"] == 1
    assert (await service.graph_status())["added_count"] == 2
    assert calls == [(config, {"llm_config": None})] * 2
    assert not (tmp_path / "runtime").exists()

def test_read_build_log_reads_only_the_tail(monkeypatch, tmp_path):
    log_path = _build_log_path(tmp_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
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
    log_path = _build_log_path(tmp_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
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
    log_path = _build_log_path(tmp_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps({"stage": f"技能-{index}"}, ensure_ascii=False)
        for index in range(40)
    ]
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    monkeypatch.setattr(service_module, "_BUILD_LOG_READ_CHUNK_BYTES", 7)

    entries = _read_build_log(tmp_path, limit=2)

    assert [entry["stage"] for entry in entries] == ["技能-38", "技能-39"]


def test_read_build_log_keeps_last_line_without_trailing_newline(tmp_path):
    log_path = _build_log_path(tmp_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(
        '{"stage": "first"}\n{"stage": "second"}',
        encoding="utf-8",
    )

    entries = _read_build_log(tmp_path, limit=1)

    assert [entry["stage"] for entry in entries] == ["second"]
