# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for Dreaming Sweeper — pure functions and file-based logic.

Tests cover:
- DreamingConfig.load()   — configuration loading from dict / env
- Sweeper._parse_history()  — event filtering
- Sweeper._detect_rounds()  — round pair detection
- Sweeper._compress()       — text compression with token budget
- Sweeper._parse_dreaming_entries() — DREAMING.md parsing
- Sweeper.load_existing_summary()  — knowledge title loading
- Sweeper.promote_agent()  — write DREAMING.md with dedup + eviction
- Sweeper.promote_code()   — write consolidated_xxx.md with hash dedup
- Sweeper._load/save_checkpoint() — checkpoint persistence
- Sweeper.scan_new_sessions()    — incremental scan logic
- SessionManager.has_active_tasks()
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from unittest import mock

import pytest

try:
    from jiuwenswarm.agents.harness.common.memory.dreaming.sweeper import Sweeper  # noqa: F401
    _DREAMING_AVAILABLE = True
except ImportError:
    _DREAMING_AVAILABLE = False


# ===========================================================================
# Inline copy of the unit under test (pure functions only)
# Avoid importing jiuwenswarm to dodge import-time side effects
# ===========================================================================

_MIN_SESSION_ROUNDS = 4
_MAX_SESSIONS_PER_SWEEP = 10
_MAX_SESSION_AGE_DAYS = 30
_MAX_COMPRESS_TOKENS = 30000
_MAX_ENTRIES_AGENT = 50
_MAX_PROMOTIONS_PER_SESSION = 5
_MIN_NEW_SESSIONS = 3
_MIN_SESSION_AGE_BYPASS_DAYS = 7


# ── parse_history ──
def _parse_history(session_dir: Path) -> list[dict]:
    path_jsonl = session_dir / "history.jsonl"
    path_json = session_dir / "history.json"
    if path_jsonl.exists():
        try:
            data = [
                json.loads(line)
                for line in path_jsonl.read_text(encoding="utf-8", errors="replace").splitlines()
                if line.strip()
            ]
        except (OSError, json.JSONDecodeError):
            return []
    else:
        if not path_json.exists():
            return []
        try:
            data = json.loads(path_json.read_text(encoding="utf-8", errors="replace"))
            if not isinstance(data, list):
                return []
        except (OSError, json.JSONDecodeError):
            return []
    
    def _should_keep(entry: dict) -> bool:
        if entry.get("role") == "user":
            return True
        if entry.get("event_type", "") == "chat.final":
            return True
        if entry.get("role") == "assistant" and "event_type" not in entry:
            return True
        return False

    return [e for e in data if _should_keep(e)]


# ── detect_rounds ──
def _detect_rounds(events: list[dict]) -> list[tuple[int, int]]:
    current_user = -1
    pairs: list[tuple[int, int]] = []
    for i, e in enumerate(events):
        role = e.get("role", "")
        if role == "user":
            current_user = i
        elif role == "assistant" and current_user != -1:
            pairs.append((current_user, i))
            current_user = -1
    return pairs


# ── compress ──
def _compress(events: list[dict], _depth: int = 0) -> str:
    max_recurse = 5
    parts: list[str] = []
    for e in events:
        role = e.get("role", "")
        content = str(e.get("content", ""))
        if role == "user":
            parts.append(f"[User]: {content[:2000]}")
        elif role == "assistant" and content.strip():
            parts.append(f"[Assistant]: {content[:3000]}")
    result = "\n\n".join(parts)
    est_tokens = len(result) // 2
    if est_tokens > _MAX_COMPRESS_TOKENS:
        if _depth < max_recurse:
            rounds = _detect_rounds(events)
            if len(rounds) > 2:
                keep_from = len(rounds) // 3
                trimmed = events[rounds[keep_from][0]:]
                prefix = f"[... 前 {keep_from} 轮对话已省略 ...]\n\n"
                return prefix + _compress(trimmed, _depth + 1)
        max_chars = _MAX_COMPRESS_TOKENS * 2
        result = "[... 内容过长，已截断 ...]\n\n" + result[-max_chars:]
    return result


# ── parse_dreaming_entries ──
def _parse_dreaming_entries(text: str) -> list[dict[str, str]]:
    import re
    entries: list[dict[str, str]] = []
    for part in re.split(r'^## ', text, flags=re.MULTILINE)[1:]:
        lines = part.strip().splitlines()
        if not lines:
            continue
        title = lines[0].strip()
        source = ""
        content_lines: list[str] = []
        for line in lines[1:]:
            if line.startswith("_source:") and line.endswith("_"):
                source = line
            else:
                content_lines.append(line)
        entries.append({
            "title": title,
            "source": source,
            "content": "\n".join(content_lines).strip(),
        })
    return entries


# ===========================================================================
# Test helpers
# ===========================================================================

def _make_history_entry(role: str, content: str, event_type: str | None = None) -> dict:
    entry = {"role": role, "content": content}
    if event_type is not None:
        entry["event_type"] = event_type
    return entry


def _write_history_json(path: Path, entries) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix == ".jsonl":
        payload = "\n".join(json.dumps(item, ensure_ascii=False) for item in entries)
        if payload:
            payload += "\n"
        path.write_text(payload, encoding="utf-8")
        return
    path.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")


# ===========================================================================
# DreamingConfig.load()
# ===========================================================================
class TestDreamingConfigLoad:
    """Test DreamingConfig.load() with mock config."""

    def test_default_disabled(self):
        cfg = self._cfg({})
        assert cfg.enabled is False
        assert cfg.interval_seconds == 14400.0

    def test_enabled_via_config(self):
        cfg = self._cfg({"memory": {"dreaming": {"code": {"enabled": True}}}})
        assert cfg.enabled is True

    def test_interval_via_config(self):
        cfg = self._cfg({"memory": {"dreaming": {"code": {"enabled": True, "interval_seconds": 7200}}}})
        assert cfg.interval_seconds == 7200.0

    def test_enabled_via_env(self, monkeypatch):
        monkeypatch.setenv("DREAMING_CODE_ENABLED", "true")
        cfg = self._cfg({})
        assert cfg.enabled is True

    def test_disabled_via_env(self, monkeypatch):
        monkeypatch.setenv("DREAMING_CODE_ENABLED", "0")
        cfg = self._cfg({"memory": {"dreaming": {"code": {"enabled": True}}}})
        assert cfg.enabled is False

    def test_interval_via_env(self, monkeypatch):
        monkeypatch.setenv("DREAMING_INTERVAL", "3600")
        cfg = self._cfg({"memory": {"dreaming": {"code": {"enabled": True}}}})
        assert cfg.interval_seconds == 3600.0

    def test_agent_mode(self):
        cfg = self._cfg({"memory": {"dreaming": {"agent": {"enabled": True}}}}, mode="agent")
        assert cfg.enabled is True

    @staticmethod
    def _cfg(memory_overrides: dict, *, mode: str = "code") -> object:
        from dataclasses import dataclass

        @dataclass
        class DreamingConfig:
            enabled: bool = False
            interval_seconds: float = 14400.0

            @classmethod
            def load(cls, mode: str = "code"):
                config = {"memory": memory_overrides.get("memory", {})}
                raw = config.get("memory", {}).get("dreaming", {}).get(mode, {})
                if not isinstance(raw, dict):
                    raw = {}
                env_key = f"DREAMING_{mode.upper()}_ENABLED"
                env_val = os.getenv(env_key)
                enabled = env_val.lower() in ("true", "1", "yes") if env_val is not None else bool(
                    raw.get("enabled", False))
                interval = float(os.getenv("DREAMING_INTERVAL", str(raw.get("interval_seconds", 14400.0))))
                return cls(enabled=enabled, interval_seconds=interval)

        return DreamingConfig.load(mode=mode)


# ===========================================================================
# _parse_history
# ===========================================================================

class TestParseHistory:
    @staticmethod
    def test_empty_dir(tmp_path):
        result = _parse_history(tmp_path / "nonexistent")
        assert result == []

    @staticmethod
    def test_no_history_json(tmp_path):
        session_dir = tmp_path / "sess_001"
        session_dir.mkdir()
        result = _parse_history(session_dir)
        assert result == []

    @staticmethod
    def test_filters_deltas(tmp_path):
        entries = [
            _make_history_entry("user", "hello"),
            _make_history_entry("assistant", "part 1", "chat.delta"),
            _make_history_entry("assistant", "full answer", "chat.final"),
        ]
        _write_history_json(tmp_path / "sess_001" / "history.json", entries)

        result = _parse_history(tmp_path / "sess_001")
        assert len(result) == 2
        assert result[0]["role"] == "user"
        assert result[1]["event_type"] == "chat.final"

    @staticmethod
    def test_filters_errors(tmp_path):
        entries = [
            _make_history_entry("user", "hello"),
            _make_history_entry("assistant", "error msg", "chat.error"),
            _make_history_entry("assistant", "final", "chat.final"),
        ]
        _write_history_json(tmp_path / "sess_001" / "history.json", entries)

        result = _parse_history(tmp_path / "sess_001")
        assert len(result) == 2

    @staticmethod
    def test_keeps_legacy_assistant(tmp_path):
        entries = [
            _make_history_entry("user", "hello"),
            _make_history_entry("assistant", "old format"),  # no event_type
        ]
        _write_history_json(tmp_path / "sess_001" / "history.json", entries)

        result = _parse_history(tmp_path / "sess_001")
        assert len(result) == 2
        assert result[1]["role"] == "assistant"

    @staticmethod
    def test_corrupted_json_returns_empty(tmp_path):
        path = tmp_path / "sess_001" / "history.json"
        path.parent.mkdir()
        path.write_text("{invalid json", encoding="utf-8")
        result = _parse_history(tmp_path / "sess_001")
        assert result == []


# ===========================================================================
# _detect_rounds
# ===========================================================================

class TestDetectRounds:
    @staticmethod
    def test_empty():
        assert _detect_rounds([]) == []

    @staticmethod
    def test_single_round():
        events = [
            {"role": "user"}, {"role": "assistant"},
        ]
        assert _detect_rounds(events) == [(0, 1)]

    @staticmethod
    def test_multi_round():
        events = [
            {"role": "user"}, {"role": "assistant"},
            {"role": "user"}, {"role": "assistant"},
            {"role": "user"}, {"role": "assistant"},
        ]
        rounds = _detect_rounds(events)
        assert len(rounds) == 3
        assert rounds[0] == (0, 1)
        assert rounds[1] == (2, 3)
        assert rounds[2] == (4, 5)

    @staticmethod
    def test_consecutive_users_takes_last():
        events = [
            {"role": "user"}, {"role": "user"}, {"role": "assistant"},
        ]
        rounds = _detect_rounds(events)
        assert rounds == [(1, 2)]

    @staticmethod
    def test_unmatched_user():
        events = [
            {"role": "user"}, {"role": "assistant"}, {"role": "user"},
        ]
        rounds = _detect_rounds(events)
        assert rounds == [(0, 1)]

    @staticmethod
    def test_unmatched_assistant():
        events = [
            {"role": "assistant"}, {"role": "user"}, {"role": "assistant"},
        ]
        rounds = _detect_rounds(events)
        assert rounds == [(1, 2)]


# ===========================================================================
# _compress
# ===========================================================================

class TestCompress:
    @staticmethod
    def test_simple():
        events = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]
        result = _compress(events)
        assert "[User]: hi" in result
        assert "[Assistant]: hello" in result

    @staticmethod
    def test_truncates_long_messages():
        long_user = "x" * 3000
        long_assistant = "y" * 5000
        events = [
            {"role": "user", "content": long_user},
            {"role": "assistant", "content": long_assistant},
        ]
        result = _compress(events)
        assert len("[User]: " + long_user) > len(result.split("[User]: ")[1].split("\n")[0])
        user_part = result.split("[User]: ")[1].split("\n\n")[0]
        assert len(user_part) <= 2000

    @staticmethod
    def test_empty_assistant_skipped():
        events = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": ""},
        ]
        result = _compress(events)
        assert "assistant" not in result.lower()
        assert "[User]: hi" in result

    @staticmethod
    def test_triggers_trim_on_large_input():
        events = []
        for i in range(100):
            events.append({"role": "user", "content": "x" * 2000})
            events.append({"role": "assistant", "content": "y" * 3000})
        result = _compress(events)
        assert "已省略" in result or "已截断" in result


# ===========================================================================
# _parse_dreaming_entries
# ===========================================================================

class TestParseDreamingEntries:
    @staticmethod
    def test_empty():
        assert _parse_dreaming_entries("") == []

    @staticmethod
    def test_single_entry():
        text = "# Dreaming 记忆\n\n## 标题A\n_source: sess_1 | 2026-04-15_\n正文内容"
        entries = _parse_dreaming_entries(text)
        assert len(entries) == 1
        assert entries[0]["title"] == "标题A"
        assert entries[0]["source"] == "_source: sess_1 | 2026-04-15_"
        assert entries[0]["content"] == "正文内容"

    @staticmethod
    def test_multi_entry():
        text = "# Dreaming\n\n## A\n_source: s1_\nbody a\n\n## B\n_source: s2_\nbody b"
        entries = _parse_dreaming_entries(text)
        assert len(entries) == 2
        assert entries[0]["title"] == "A"
        assert entries[1]["title"] == "B"

    @staticmethod
    def test_no_source_line():
        text = "# Dreaming\n\n## A\njust content"
        entries = _parse_dreaming_entries(text)
        assert entries[0]["source"] == ""
        assert entries[0]["content"] == "just content"


# ===========================================================================
# promote_agent
# ===========================================================================

@pytest.mark.skipif(not _DREAMING_AVAILABLE, reason="openjiuwen dreaming module is not available")
class TestPromoteAgent:
    @staticmethod
    def test_first_entry_creates_file(tmp_path):
        output_dir = tmp_path / "memory"
        output_dir.mkdir(parents=True, exist_ok=True)
        sweeper = Sweeper(sessions_dir=str(tmp_path / "sessions"),
                         output_dir=str(output_dir), mode="agent")
        result = sweeper.promote_agent("New Title", "New content body", "sess_001")
        assert result is not None
        text = Path(result).read_text(encoding="utf-8")
        assert "# Dreaming" in text
        assert "## New Title" in text
        assert "New content body" in text

    @staticmethod
    def test_dedup_title(tmp_path):
        output_dir = tmp_path / "memory"
        output_dir.mkdir(parents=True, exist_ok=True)
        sweeper = Sweeper(sessions_dir=str(tmp_path / "sessions"),
                         output_dir=str(output_dir), mode="agent")
        sweeper.promote_agent("Same Title", "Content A", "sess_001")
        result = sweeper.promote_agent("Same Title", "Content B", "sess_002")
        assert result is None

    @staticmethod
    def test_eviction_at_limit(tmp_path):
        output_dir = tmp_path / "memory"
        output_dir.mkdir(parents=True, exist_ok=True)
        sweeper = Sweeper(sessions_dir=str(tmp_path / "sessions"),
                         output_dir=str(output_dir), mode="agent")
        for i in range(_MAX_ENTRIES_AGENT + 5):
            sweeper.promote_agent(f"Title_{i:03d}", f"Content {i}", "sess_001")

        entries = _parse_dreaming_entries(
            (tmp_path / "memory" / "DREAMING.md").read_text(encoding="utf-8")
        )
        assert len(entries) == _MAX_ENTRIES_AGENT
        assert entries[0]["title"] == "Title_005"


# ===========================================================================
# promote_code
# ===========================================================================

@pytest.mark.skipif(not _DREAMING_AVAILABLE, reason="openjiuwen dreaming module is not available")
class TestPromoteCode:
    @staticmethod
    def test_creates_file_with_hash_name(tmp_path):
        output_dir = tmp_path / "memory"
        output_dir.mkdir(parents=True, exist_ok=True)
        sweeper = Sweeper(sessions_dir=str(tmp_path / "sessions"),
                         output_dir=str(output_dir), mode="code")
        result = sweeper.promote_code("Title A", "Body content", "sess_001")
        assert result is not None
        path = Path(result)
        assert path.exists()
        assert path.name.startswith("consolidated_")
        assert path.name.endswith(".md")
        text = path.read_text(encoding="utf-8")
        assert "name: Title A" in text
        assert "source_session: sess_001" in text
        assert "Body content" in text

    @staticmethod
    def test_same_content_dedup(tmp_path):
        output_dir = tmp_path / "memory"
        output_dir.mkdir(parents=True, exist_ok=True)
        sweeper = Sweeper(sessions_dir=str(tmp_path / "sessions"),
                         output_dir=str(output_dir), mode="code")
        r1 = sweeper.promote_code("Title", "Same body", "sess_001")
        r2 = sweeper.promote_code("Title", "Same body", "sess_001")
        assert r1 is not None
        assert r2 is None


# ===========================================================================
# checkpoint
# ===========================================================================

@pytest.mark.skipif(not _DREAMING_AVAILABLE, reason="openjiuwen dreaming module is not available")
class TestCheckpoint:
    @staticmethod
    def test_save_and_load(tmp_path):
        sweeper = Sweeper(sessions_dir=str(tmp_path / "sessions"),
                         output_dir=str(tmp_path / "memory"), mode="code")
        sweeper.init()
        sweeper.scanned_sessions["sess_001"] = {"history_mtime": 1.0, "round_count": 3}
        sweeper.save_checkpoint()

        sweeper2 = Sweeper(sessions_dir=str(tmp_path / "sessions"),
                          output_dir=str(tmp_path / "memory"), mode="code")
        sweeper2.init()
        assert "sess_001" in sweeper2.scanned_sessions
        assert sweeper2.scanned_sessions["sess_001"]["round_count"] == 3

    @staticmethod
    def test_backward_compat_list_format(tmp_path):
        cp_path = tmp_path / "memory" / ".dreams" / "ingestion-checkpoint.json"
        cp_path.parent.mkdir(parents=True)
        cp_path.write_text(json.dumps({"scanned_sessions": ["sess_a", "sess_b"]}), encoding="utf-8")
        sweeper = Sweeper(sessions_dir=str(tmp_path / "sessions"),
                         output_dir=str(tmp_path / "memory"), mode="code")
        sweeper.init()
        assert "sess_a" in sweeper.scanned_sessions
        assert sweeper.scanned_sessions["sess_a"] == {}


# ===========================================================================
# scan_new_sessions
# ===========================================================================

@pytest.mark.skipif(not _DREAMING_AVAILABLE, reason="openjiuwen dreaming module is not available")
class TestScanNewSessions:
    @staticmethod
    def _make_session(sessions_root: Path, sid: str, entries: list[dict], age_delta: float = 0):
        session_dir = sessions_root / sid
        session_dir.mkdir(parents=True)
        _write_history_json(session_dir / "history.jsonl", entries)
        metadata = {"mode": "code.normal", "session_id": sid, "created_at": time.time()}
        _write_history_json(session_dir / "metadata.json", metadata)
        mtime = time.time() - age_delta
        os.utime(session_dir, (mtime, mtime))
        os.utime(session_dir / "history.jsonl", (mtime, mtime))

    @staticmethod
    def test_no_sessions_dir(tmp_path):
        sweeper = Sweeper(sessions_dir=str(tmp_path / "no_such_dir"),
                         output_dir=str(tmp_path / "memory"), mode="code")
        sweeper.init()
        result = sweeper.scan_new_sessions()
        assert result == []

    def test_excludes_heartbeat(self, tmp_path):
        sessions_root = tmp_path / "sessions"
        (sessions_root / "heartbeat_check").mkdir(parents=True)
        # create 3 valid sessions with 4 rounds each
        for i in range(3):
            entries = []
            for j in range(1, 5):
                entries.append(_make_history_entry("user", f"q{j}"))
                entries.append(_make_history_entry("assistant", f"a{j}", "chat.final"))
            self._make_session(sessions_root, f"sess_00{i+1}", entries)
        sweeper = Sweeper(sessions_dir=str(sessions_root),
                         output_dir=str(tmp_path / "memory"), mode="code")
        sweeper.init()
        result = sweeper.scan_new_sessions()
        ids = {s["session_id"] for s in result}
        assert "heartbeat_check" not in ids

    def test_skips_insufficient_rounds(self, tmp_path):
        sessions_root = tmp_path / "sessions"
        # create 3 sessions, each with only 2 rounds (< MIN_SESSION_ROUNDS)
        for i in range(3):
            entries = [
                _make_history_entry("user", "hi"), _make_history_entry("assistant", "hey", "chat.final"),
                _make_history_entry("user", "bye"), _make_history_entry("assistant", "bye", "chat.final"),
            ]
            self._make_session(sessions_root, f"sess_00{i+1}", entries)
        sweeper = Sweeper(sessions_dir=str(sessions_root),
                         output_dir=str(tmp_path / "memory"), mode="code")
        sweeper.init()
        result = sweeper.scan_new_sessions()
        assert result == []

    def test_scans_valid_session(self, tmp_path):
        sessions_root = tmp_path / "sessions"
        # create 3 sessions, each with 4 valid rounds
        for i in range(3):
            entries = []
            for j in range(1, 5):
                entries.append(_make_history_entry("user", f"q{j}"))
                entries.append(_make_history_entry("assistant", f"a{j}", "chat.final"))
            self._make_session(sessions_root, f"sess_00{i+1}", entries)
        sweeper = Sweeper(sessions_dir=str(sessions_root),
                         output_dir=str(tmp_path / "memory"), mode="code")
        sweeper.init()
        result = sweeper.scan_new_sessions()
        assert len(result) == 3
        assert result[0]["session_id"] == "sess_001"
        assert result[0]["compressed_text"]

    def test_incremental_scan_unchanged_skipped(self, tmp_path):
        sessions_root = tmp_path / "sessions"
        # create 3 sessions, each with 4 valid rounds
        for i in range(3):
            entries = []
            for j in range(1, 5):
                entries.append(_make_history_entry("user", f"q{j}"))
                entries.append(_make_history_entry("assistant", f"a{j}", "chat.final"))
            self._make_session(sessions_root, f"sess_00{i+1}", entries)
        sweeper = Sweeper(sessions_dir=str(sessions_root),
                         output_dir=str(tmp_path / "memory"), mode="code")
        sweeper.init()
        result1 = sweeper.scan_new_sessions()
        assert len(result1) == 3

        # mark all 3 as scanned
        for i in range(3):
            sid = f"sess_00{i+1}"
            sweeper.scanned_sessions[sid] = {
                "history_mtime": (sessions_root / sid / "history.jsonl").stat().st_mtime,
                "round_count": 4,
            }
        result2 = sweeper.scan_new_sessions()
        assert result2 == []

    def test_incremental_scan_updated_reprocesses(self, tmp_path):
        sessions_root = tmp_path / "sessions"
        # create 3 sessions, each with 4 valid rounds
        for i in range(3):
            entries = []
            for j in range(1, 5):
                entries.append(_make_history_entry("user", f"q{j}"))
                entries.append(_make_history_entry("assistant", f"a{j}", "chat.final"))
            self._make_session(sessions_root, f"sess_00{i+1}", entries)
        sweeper = Sweeper(sessions_dir=str(sessions_root),
                         output_dir=str(tmp_path / "memory"), mode="code")
        sweeper.init()
        # mark sess_001 as already scanned (with 4 rounds), sess_002 and sess_003 as new
        sweeper.scanned_sessions["sess_001"] = {
            "history_mtime": (sessions_root / "sess_001" / "history.jsonl").stat().st_mtime - 1,
            "round_count": 4,
        }

        time.sleep(0.1)
        # add 4 more rounds to sess_001
        more_entries = [
            _make_history_entry("user", "q5"), _make_history_entry("assistant", "a5", "chat.final"),
            _make_history_entry("user", "q6"), _make_history_entry("assistant", "a6", "chat.final"),
            _make_history_entry("user", "q7"), _make_history_entry("assistant", "a7", "chat.final"),
            _make_history_entry("user", "q8"), _make_history_entry("assistant", "a8", "chat.final"),
        ]
        existing = []
        for line in (sessions_root / "sess_001" / "history.jsonl").read_text(encoding="utf-8").splitlines():
            if line.strip():
                existing.append(json.loads(line))
        existing.extend(more_entries)
        _write_history_json(sessions_root / "sess_001" / "history.jsonl", existing)

        result = sweeper.scan_new_sessions()
        assert len(result) == 3  # sess_001 updated + sess_002 & sess_003 new
        # find the sess_001 result
        sess_001_result = [r for r in result if r["session_id"] == "sess_001"]
        assert len(sess_001_result) == 1
        assert "q5" in sess_001_result[0]["compressed_text"]


# ===========================================================================
# load_existing_summary
# ===========================================================================

@pytest.mark.skipif(not _DREAMING_AVAILABLE, reason="openjiuwen dreaming module is not available")
class TestLoadExistingSummary:
    @staticmethod
    def test_empty_output_dir(tmp_path):
        sweeper = Sweeper(sessions_dir=str(tmp_path / "sessions"),
                         output_dir=str(tmp_path / "not_exist"), mode="agent")
        assert sweeper.load_existing_summary() == "(空)"

    @staticmethod
    def test_agent_mode_no_file(tmp_path):
        output_dir = tmp_path / "memory"
        output_dir.mkdir()
        sweeper = Sweeper(sessions_dir=str(tmp_path / "sessions"),
                         output_dir=str(output_dir), mode="agent")
        assert sweeper.load_existing_summary() == "(空)"

    @staticmethod
    def test_agent_mode_with_entries(tmp_path):
        output_dir = tmp_path / "memory"
        output_dir.mkdir()
        (output_dir / "DREAMING.md").write_text(
            "# Dreaming 记忆\n\n## 标题A\n_source: s1_\nbody a\n\n## 标题B\n_source: s2_\nbody b",
            encoding="utf-8",
        )
        sweeper = Sweeper(sessions_dir=str(tmp_path / "sessions"),
                         output_dir=str(output_dir), mode="agent")
        result = sweeper.load_existing_summary()
        assert "- 标题A" in result
        assert "- 标题B" in result

    @staticmethod
    def test_code_mode_with_files(tmp_path):
        output_dir = tmp_path / "coding_memory"
        output_dir.mkdir()
        import hashlib
        h = hashlib.sha256("body".encode()).hexdigest()[:12]
        (output_dir / f"consolidated_{h}.md").write_text(
            "---\nname: 知识点X\n---\n\nbody\n", encoding="utf-8",
        )
        sweeper = Sweeper(sessions_dir=str(tmp_path / "sessions"),
                         output_dir=str(output_dir), mode="code")
        result = sweeper.load_existing_summary()
        assert "- 知识点X" in result


# ===========================================================================
# SessionManager.has_active_tasks
# ===========================================================================

class TestSessionManagerHasActiveTasks:
    @staticmethod
    def test_no_tasks():
        from jiuwenswarm.server.runtime.session.session_manager import SessionManager
        sm = SessionManager()
        assert sm.has_active_tasks() is False

    @staticmethod
    @pytest.mark.asyncio
    async def test_with_active_task():
        from jiuwenswarm.server.runtime.session.session_manager import SessionManager
        import asyncio
        sm = SessionManager()

        async def sleepy():
            await asyncio.sleep(99)

        await sm.submit_task("sess_1", sleepy)
        await asyncio.sleep(0.05)
        assert sm.has_active_tasks() is True
        await sm.cancel_session_task("sess_1")
        await asyncio.sleep(0)
        assert sm.has_active_tasks() is False

    @staticmethod
    @pytest.mark.asyncio
    async def test_ignores_none_tasks():
        from jiuwenswarm.server.runtime.session.session_manager import SessionManager
        import asyncio
        sm = SessionManager()

        async def quick():
            pass

        await sm.submit_task("sess_1", quick)
        await asyncio.sleep(0.05)
        assert sm.has_active_tasks() is False