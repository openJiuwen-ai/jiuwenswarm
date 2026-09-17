# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""System test for Dreaming Memory — end-to-end sweep pipeline.

Covers the full lifecycle:
  1. Create sessions with history.json
  2. Create a Sweeper instance targeting those sessions
  3. Mock LLM to return controlled knowledge items
  4. Execute run_sweep()
  5. Verify knowledge files are written and checkpoint updated
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from unittest import mock

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.system]

try:
    from jiuwenswarm.agents.harness.common.memory.dreaming import DreamingOrchestrator  # noqa: F401
except ImportError:
    pytest.skip("openjiuwen dreaming module is not available", allow_module_level=True)

# ===========================================================================
# Helpers
# ===========================================================================


def _make_history_entry(role: str, content: str, *, event_type: str | None = None) -> dict:
    entry = {"role": role, "content": content}
    if event_type is not None:
        entry["event_type"] = event_type
    return entry


def _create_valid_session(sessions_root: Path, sid: str, num_rounds: int = 5,
                         mode: str = "agent.plan") -> Path:
    """Create a session directory with enough rounds for dreaming."""
    session_dir = sessions_root / sid
    session_dir.mkdir(parents=True)
    entries = []
    for i in range(num_rounds):
        entries.append(_make_history_entry("user", f"请问第{i+1}个问题"))
        entries.append(_make_history_entry("assistant", f"这是第{i+1}个回答", event_type="chat.final"))
    history_payload = "\n".join(json.dumps(item, ensure_ascii=False) for item in entries)
    if history_payload:
        history_payload += "\n"
    (session_dir / "history.jsonl").write_text(history_payload, encoding="utf-8")
    metadata = {"mode": mode, "session_id": sid, "created_at": time.time()}
    (session_dir / "metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
    return session_dir


def _mock_extract(items: list[dict]):
    """Return an AsyncMock that simulates _extract_via_llm returning given items."""
    async def fake_extract(self, compressed_text: str, existing_summary: str) -> list[dict]:
        return items
    return fake_extract


# ===========================================================================
# End-to-End: Agent mode Dreaming
# ===========================================================================

class TestDreamingE2EAgent:
    """Full agent-mode dreaming: scan → extract → promote to DREAMING.md."""

    @pytest.mark.asyncio
    async def test_sweep_writes_dreaming_md(self, tmp_path):
        from jiuwenswarm.agents.harness.common.memory.dreaming.sweeper import Sweeper

        sessions_root = tmp_path / "sessions"
        output_dir = tmp_path / "memory"
        sessions_root.mkdir()

        _create_valid_session(sessions_root, "sess_001", num_rounds=5)
        _create_valid_session(sessions_root, "sess_002", num_rounds=5)
        _create_valid_session(sessions_root, "sess_003", num_rounds=5)

        sweeper = Sweeper(str(sessions_root), str(output_dir), mode="agent")
        sweeper.init()

        fake_extract = _mock_extract([
            {"title": "用户偏好 Python", "content": "用户倾向于使用 Python 开发"},
            {"title": "项目使用 FastAPI", "content": "当前项目框架是 FastAPI"},
        ])

        with mock.patch.object(Sweeper, "_extract_via_llm", fake_extract):
            await sweeper.run_sweep()

        dreaming_path = output_dir / "DREAMING.md"
        assert dreaming_path.exists()
        content = dreaming_path.read_text(encoding="utf-8")
        assert "用户偏好 Python" in content
        assert "项目使用 FastAPI" in content
        assert "_source:" in content

        cp_path = output_dir / ".dreams" / "ingestion-checkpoint.json"
        assert cp_path.exists()
        cp = json.loads(cp_path.read_text(encoding="utf-8"))
        assert "sess_001" in cp["scanned_sessions"]
        assert "sess_002" in cp["scanned_sessions"]
        assert "sess_003" in cp["scanned_sessions"]

    @pytest.mark.asyncio
    async def test_dedup_skips_existing_title(self, tmp_path):
        from jiuwenswarm.agents.harness.common.memory.dreaming.sweeper import Sweeper

        sessions_root = tmp_path / "sessions"
        output_dir = tmp_path / "memory"
        sessions_root.mkdir()

        # need 3 sessions to pass MIN_NEW_SESSIONS gate
        for sid in ["sess_a", "sess_b", "sess_c"]:
            _create_valid_session(sessions_root, sid, num_rounds=5)

        sweeper1 = Sweeper(str(sessions_root), str(output_dir), mode="agent")
        sweeper1.init()

        fake_extract = _mock_extract([
            {"title": "用户偏好 Python", "content": "第一轮提取"},
        ])

        with mock.patch.object(Sweeper, "_extract_via_llm", fake_extract):
            await sweeper1.run_sweep()

        _create_valid_session(sessions_root, "sess_002", num_rounds=5)
        _create_valid_session(sessions_root, "sess_003", num_rounds=5)
        sweeper2 = Sweeper(str(sessions_root), str(output_dir), mode="agent")
        sweeper2.init()
        sweeper2.scanned_sessions = sweeper1.scanned_sessions

        fake_extract2 = _mock_extract([
            {"title": "用户偏好 Python", "content": "第二轮提取（应被去重）"},
        ])

        with mock.patch.object(Sweeper, "_extract_via_llm", fake_extract2):
            await sweeper2.run_sweep()

        content = (output_dir / "DREAMING.md").read_text(encoding="utf-8")
        assert content.count("用户偏好 Python") == 1

    @pytest.mark.asyncio
    async def test_incremental_scan_reprocesses_updated_session(self, tmp_path):
        from jiuwenswarm.agents.harness.common.memory.dreaming.sweeper import Sweeper

        sessions_root = tmp_path / "sessions"
        output_dir = tmp_path / "memory"
        sessions_root.mkdir()

        # need 3 sessions to pass MIN_NEW_SESSIONS gate
        for sid in ["sess_001", "sess_a", "sess_b"]:
            _create_valid_session(sessions_root, sid, num_rounds=5)
        session_dir = sessions_root / "sess_001"
        orig_mtime = (session_dir / "history.jsonl").stat().st_mtime

        sweeper1 = Sweeper(str(sessions_root), str(output_dir), mode="agent")
        sweeper1.init()

        fake_extract = _mock_extract([
            {"title": "第一轮知识", "content": "第一次扫描提取"},
        ])

        with mock.patch.object(Sweeper, "_extract_via_llm", fake_extract):
            await sweeper1.run_sweep()

        # mark sess_a and sess_b as scanned, then update them too
        sweeper1.scanned_sessions["sess_a"] = {
            "history_mtime": (sessions_root / "sess_a" / "history.jsonl").stat().st_mtime,
            "round_count": 5,
        }
        sweeper1.scanned_sessions["sess_b"] = {
            "history_mtime": (sessions_root / "sess_b" / "history.jsonl").stat().st_mtime,
            "round_count": 5,
        }

        time.sleep(0.2)
        # add 4 more rounds to sess_001, sess_a and sess_b
        for sid in ["sess_001", "sess_a", "sess_b"]:
            sdir = sessions_root / sid
            existing = [
                json.loads(line)
                for line in (sdir / "history.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()
            ]
            existing.extend([
                _make_history_entry("user", "新问题6"),
                _make_history_entry("assistant", "新回答6", event_type="chat.final"),
                _make_history_entry("user", "新问题7"),
                _make_history_entry("assistant", "新回答7", event_type="chat.final"),
                _make_history_entry("user", "新问题8"),
                _make_history_entry("assistant", "新回答8", event_type="chat.final"),
                _make_history_entry("user", "新问题9"),
                _make_history_entry("assistant", "新回答9", event_type="chat.final"),
            ])
            (sdir / "history.jsonl").write_text(
                "\n".join(json.dumps(item, ensure_ascii=False) for item in existing) + "\n", encoding="utf-8"
            )

        sweeper2 = Sweeper(str(sessions_root), str(output_dir), mode="agent")
        sweeper2.init()
        sweeper2.scanned_sessions = sweeper1.scanned_sessions

        fake_extract2 = _mock_extract([
            {"title": "第二轮新增知识", "content": "增量扫描提取"},
        ])

        with mock.patch.object(Sweeper, "_extract_via_llm", fake_extract2):
            await sweeper2.run_sweep()

        content = (output_dir / "DREAMING.md").read_text(encoding="utf-8")
        assert "第一轮知识" in content
        assert "第二轮新增知识" in content

    @pytest.mark.asyncio
    async def test_skips_unchanged_session(self, tmp_path):
        from jiuwenswarm.agents.harness.common.memory.dreaming.sweeper import Sweeper

        sessions_root = tmp_path / "sessions"
        output_dir = tmp_path / "memory"
        sessions_root.mkdir()

        # need 3 sessions to pass MIN_NEW_SESSIONS gate
        for sid in ["sess_001", "sess_a", "sess_b"]:
            _create_valid_session(sessions_root, sid, num_rounds=5)

        sweeper1 = Sweeper(str(sessions_root), str(output_dir), mode="agent")
        sweeper1.init()

        fake_extract = _mock_extract([
            {"title": "初次知识", "content": "第一次扫描"},
        ])

        with mock.patch.object(Sweeper, "_extract_via_llm", fake_extract):
            await sweeper1.run_sweep()

        sweeper2 = Sweeper(str(sessions_root), str(output_dir), mode="agent")
        sweeper2.init()
        sweeper2.scanned_sessions = sweeper1.scanned_sessions

        fake_extract2 = _mock_extract([
            {"title": "不应该出现", "content": "第二次扫描不应该发生"},
        ])

        with mock.patch.object(Sweeper, "_extract_via_llm", fake_extract2):
            await sweeper2.run_sweep()

        content = (output_dir / "DREAMING.md").read_text(encoding="utf-8")
        assert "不应该出现" not in content


# ===========================================================================
# End-to-End: Code mode Dreaming
# ===========================================================================

class TestDreamingE2ECode:
    """Full code-mode dreaming: scan → extract → promote to consolidated_xxx.md."""

    @pytest.mark.asyncio
    async def test_sweep_writes_consolidated_files(self, tmp_path):
        from jiuwenswarm.agents.harness.common.memory.dreaming.sweeper import Sweeper

        sessions_root = tmp_path / "sessions"
        output_dir = tmp_path / "coding_memory"
        sessions_root.mkdir()

        _create_valid_session(sessions_root, "sess_010", num_rounds=5, mode="code.normal")
        _create_valid_session(sessions_root, "sess_011", num_rounds=5, mode="code.normal")
        _create_valid_session(sessions_root, "sess_012", num_rounds=5, mode="code.normal")

        sweeper = Sweeper(str(sessions_root), str(output_dir), mode="code")
        sweeper.init()

        fake_extract = _mock_extract([
            {"title": "Python 性能优化", "content": "使用列表推导式替代 for 循环"},
            {"title": "Git 工作流最佳实践", "content": "推荐使用 feature branch + rebase"},
        ])

        with mock.patch.object(Sweeper, "_extract_via_llm", fake_extract):
            await sweeper.run_sweep()

        files = sorted(output_dir.glob("consolidated_*.md"))
        assert len(files) == 2
        for f in files:
            text = f.read_text(encoding="utf-8")
            assert "---" in text
            assert "name:" in text
            assert "source_session:" in text
            assert "created_at:" in text

        combined = "\n".join(f.read_text(encoding="utf-8") for f in files)
        assert "Python 性能优化" in combined
        assert "Git 工作流最佳实践" in combined

    @pytest.mark.asyncio
    async def test_content_hash_dedup(self, tmp_path):
        from jiuwenswarm.agents.harness.common.memory.dreaming.sweeper import Sweeper

        sessions_root = tmp_path / "sessions"
        output_dir = tmp_path / "coding_memory"
        sessions_root.mkdir()

        # need 3 sessions to pass MIN_NEW_SESSIONS gate
        for sid in ["sess_020", "sess_021", "sess_022"]:
            _create_valid_session(sessions_root, sid, num_rounds=5, mode="code.normal")

        sweeper = Sweeper(str(sessions_root), str(output_dir), mode="code")
        sweeper.init()

        shared_body = "完全相同的知识内容用于测试去重"
        fake_extract = _mock_extract([
            {"title": "知识点A", "content": shared_body},
            {"title": "知识点B", "content": shared_body},
        ])

        with mock.patch.object(Sweeper, "_extract_via_llm", fake_extract):
            await sweeper.run_sweep()

        files = sorted(output_dir.glob("consolidated_*.md"))
        assert len(files) == 1


# ===========================================================================
# End-to-End: Dreaming start/stop lifecycle
# ===========================================================================

class TestDreamingE2ELifecycle:
    """Orch-level lifecycle: start → sweep → stop."""

    @pytest.mark.asyncio
    async def test_start_stop(self, tmp_path, monkeypatch):
        from jiuwenswarm.agents.harness.common.memory.dreaming import (
            start_dreaming,
            stop_dreaming,
            get_dreaming_orchestrator,
        )

        monkeypatch.setenv("DREAMING_AGENT_ENABLED", "true")

        sessions_root = tmp_path / "sessions"
        output_dir = tmp_path / "memory"
        sessions_root.mkdir()
        output_dir.mkdir()

        busy = False

        orch = await start_dreaming(
            sessions_dir=str(sessions_root),
            output_dir=str(output_dir),
            mode="agent",
            busy_checker=lambda: busy,
        )
        assert orch is not None
        assert get_dreaming_orchestrator("agent") is orch

        await stop_dreaming("agent")
        assert get_dreaming_orchestrator("agent") is None

    @pytest.mark.asyncio
    async def test_start_disabled_config(self, tmp_path, monkeypatch):
        from jiuwenswarm.agents.harness.common.memory.dreaming import (
            start_dreaming,
            get_dreaming_orchestrator,
        )
        monkeypatch.setenv("DREAMING_AGENT_ENABLED", "false")

        sessions_root = tmp_path / "sessions"
        output_dir = tmp_path / "memory"
        sessions_root.mkdir()
        output_dir.mkdir()

        orch = await start_dreaming(
            sessions_dir=str(sessions_root),
            output_dir=str(output_dir),
            mode="agent",
        )
        assert orch is None
        assert get_dreaming_orchestrator("agent") is None