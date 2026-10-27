# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from jiuwenswarm.common.cleanup import (
    CLEANUP_PERIOD_ENV,
    DEFAULT_CLEANUP_PERIOD_DAYS,
    MAX_CLEANUP_PERIOD_DAYS,
    _get_cleanup_period_days,
    _parse_period_days,
    _rmtree,
    cleanup_old_sessions,
    cleanup_orphan_file_ops,
)

CLEANUP_MODULE = "jiuwenswarm.common.cleanup"
LOGGER_NAME = "jiuwenswarm.common.cleanup"


def _patch_sessions_dir(monkeypatch, sessions_dir: Path) -> None:
    monkeypatch.setattr(f"{CLEANUP_MODULE}.get_agent_sessions_dir", lambda: sessions_dir)


def _make_session_dirs(sessions_dir: Path, ages_seconds: list[float]) -> list[Path]:
    """按给定"存在时长（秒）"批量创建会话目录并设置其 mtime。"""
    now = time.time()
    created: list[Path] = []
    for idx, age in enumerate(ages_seconds):
        entry = sessions_dir / f"session-{idx:03d}"
        entry.mkdir(parents=True)
        stamp = now - age
        os.utime(entry, (stamp, stamp))
        created.append(entry)
    return created


class TestCleanup:

    @staticmethod
    def test_removes_old_sessions_by_mtime(tmp_path, monkeypatch):
        """会话目录 mtime 超过保留期时被删除（与 cc 一致）。"""
        sessions_dir = tmp_path / "sessions"
        stale = sessions_dir / "session-old"
        stale.mkdir(parents=True)

        # 直接修改目录 mtime 到很久以前
        old_time = time.time() - 999 * 86400
        os.utime(stale, (old_time, old_time))

        _patch_sessions_dir(monkeypatch, sessions_dir)
        monkeypatch.setattr(f"{CLEANUP_MODULE}._get_cleanup_period_days", lambda: 30)

        result = cleanup_old_sessions()
        assert result["removed"] >= 1
        assert not stale.exists()

    @staticmethod
    def test_removes_file_ops_without_session(tmp_path, monkeypatch):
        """file_ops 对应的 session 目录已不存在时被删除。"""
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir(parents=True)

        hist_dir = tmp_path / "agent-ws" / ".agent_history"
        hist_dir.mkdir(parents=True)
        fop = hist_dir / "file_ops_jiuwenswarm_dead_session.json"
        fop.write_text("{}", encoding="utf-8")

        monkeypatch.setattr(f"{CLEANUP_MODULE}.get_agent_workspace_dir", lambda: tmp_path / "agent-ws")
        monkeypatch.setattr(f"{CLEANUP_MODULE}.get_user_workspace_dir", lambda: tmp_path / "user-ws")
        _patch_sessions_dir(monkeypatch, sessions_dir)

        result = cleanup_orphan_file_ops()
        assert result["removed"] >= 1
        assert not fop.exists()


class TestCleanupPeriodDays:
    """P01：``CLEANUP_PERIOD_DAYS`` 非法值必须回落默认值，不能变成"全删"。"""

    @staticmethod
    @pytest.mark.parametrize("raw", ["0", "-1", "-30", "abc", "1.5", "30d", "", "   "])
    def test_invalid_value_falls_back_to_default(monkeypatch, raw):
        monkeypatch.setenv(CLEANUP_PERIOD_ENV, raw)
        assert _get_cleanup_period_days() == DEFAULT_CLEANUP_PERIOD_DAYS

    @staticmethod
    def test_missing_env_falls_back_to_default(monkeypatch):
        monkeypatch.delenv(CLEANUP_PERIOD_ENV, raising=False)
        assert _get_cleanup_period_days() == DEFAULT_CLEANUP_PERIOD_DAYS

    @staticmethod
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("7", 7),
            (" 15 ", 15),
            ("+3", 3),
            (str(MAX_CLEANUP_PERIOD_DAYS), MAX_CLEANUP_PERIOD_DAYS),
        ],
        ids=["plain", "spaces", "plus-sign", "at-max"],
    )
    def test_valid_value_is_kept(monkeypatch, raw, expected):
        monkeypatch.setenv(CLEANUP_PERIOD_ENV, raw)
        assert _get_cleanup_period_days() == expected

    @staticmethod
    @pytest.mark.parametrize(
        "raw",
        [
            str(MAX_CLEANUP_PERIOD_DAYS + 1),
            "1" + "0" * 399,
            "1" * 5000,
        ],
        ids=["max-plus-one", "huge-400-digits", "over-int-str-limit"],
    )
    def test_too_large_value_is_clamped(monkeypatch, caplog, raw):
        """合法但过大的值夹紧到上界，而不是回落默认值。

        包括超过 CPython int-str 位数上限（默认 4300）的串：``int()`` 解析失败，
        但语义仍是"极大"，不能退化成"非法 → 回落 30 天"（那会开始真删数据）。
        """
        monkeypatch.setenv(CLEANUP_PERIOD_ENV, raw)
        with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
            assert _get_cleanup_period_days() == MAX_CLEANUP_PERIOD_DAYS

        messages = [rec.getMessage() for rec in caplog.records]
        assert any("clamped" in msg for msg in messages), caplog.text
        assert not any("using default" in msg for msg in messages), caplog.text
        # 畸形长值不能在日志里整串展开
        assert all(len(msg) < 200 for msg in messages), caplog.text

    @staticmethod
    @pytest.mark.parametrize(
        "raw",
        ["0\nFAKE-LOG-LINE injected", "abc\r\nfake", "0\tfake"],
        ids=["newline", "crlf", "tab"],
    )
    def test_warning_escapes_control_chars(monkeypatch, caplog, raw):
        """回归：配置值里的换行/制表符必须被转义，否则日志会被拆行伪造假日志。"""
        monkeypatch.setenv(CLEANUP_PERIOD_ENV, raw)
        with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
            _get_cleanup_period_days()

        assert caplog.records, caplog.text
        for rec in caplog.records:
            msg = rec.getMessage()
            assert "\n" not in msg and "\r" not in msg and "\t" not in msg, msg

    @staticmethod
    @pytest.mark.parametrize("raw", ["0", "-1", "abc"])
    def test_invalid_value_logs_warning(monkeypatch, caplog, raw):
        monkeypatch.setenv(CLEANUP_PERIOD_ENV, raw)
        with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
            _get_cleanup_period_days()
        assert any(
            CLEANUP_PERIOD_ENV in rec.getMessage() and rec.levelno == logging.WARNING
            for rec in caplog.records
        ), caplog.text

    @staticmethod
    @pytest.mark.parametrize(
        "raw",
        ["0", "-1", "-30", "0.5", "abc"],
        ids=["zero", "negative", "negative-large", "non-integer", "garbage"],
    )
    def test_invalid_period_keeps_all_sessions(tmp_path, monkeypatch, raw):
        """issue 复现场景：非法保留期不再删除任何会话目录（含几分钟前的）。"""
        sessions_dir = tmp_path / "sessions"
        dirs = _make_session_dirs(sessions_dir, [86400, 60])
        _patch_sessions_dir(monkeypatch, sessions_dir)
        monkeypatch.setenv(CLEANUP_PERIOD_ENV, raw)

        result = cleanup_old_sessions()

        assert result == {"removed": 0, "errors": 0}
        assert sorted(p.name for p in sessions_dir.iterdir()) == sorted(p.name for p in dirs)

    @staticmethod
    def test_huge_period_does_not_abort_cleanup(tmp_path, monkeypatch):
        """回归：超大保留期不得让整轮清理抛 OverflowError 而整体失效。

        原先 days*86400 溢出 → cleanup_old_sessions 抛错 → 被 run_cleanup 吞掉，
        会话清理整轮不执行；夹紧到上界后应正常跑完且不删除任何目录。
        """
        sessions_dir = tmp_path / "sessions"
        dirs = _make_session_dirs(sessions_dir, [400 * 86400, 60])
        _patch_sessions_dir(monkeypatch, sessions_dir)
        monkeypatch.setenv(CLEANUP_PERIOD_ENV, "1" + "0" * 399)

        result = cleanup_old_sessions()  # 修复前这里直接抛 OverflowError

        assert result == {"removed": 0, "errors": 0}
        assert sorted(p.name for p in sessions_dir.iterdir()) == sorted(p.name for p in dirs)


class TestParsePeriodDays:
    """``int()`` 解析失败时的分叉：数字过长 → 夹紧；不是数字 → None（回落默认）。"""

    @staticmethod
    def test_huge_digit_string_is_treated_as_too_large():
        """超长数字串必须落在"过大"一侧，不能变成 None（None 会回落默认值）。

        这里只断言"结果 > 上界"，不断言具体哨兵值：CPython 的 int-str 位数上限
        可由 ``PYTHONINTMAXSTRDIGITS`` 调高或关闭，届时 ``int()`` 直接解析成功、
        返回那个巨型整数；上限生效时才返回 ``MAX + 1`` 哨兵。两种情况的共同契约
        是"结果超过上界"，把测试钉在哨兵上会在上限被关闭的机器上误报失败。
        """
        days = _parse_period_days("1" * 5000)

        assert days is not None
        assert days > MAX_CLEANUP_PERIOD_DAYS

    @staticmethod
    def test_huge_digit_string_hits_sentinel_when_int_limit_bites():
        """位数上限真正生效时走哨兵分支——固定上限，不依赖宿主环境配置。"""
        original_limit = sys.get_int_max_str_digits()
        sys.set_int_max_str_digits(640)
        try:
            assert _parse_period_days("1" * 700) == MAX_CLEANUP_PERIOD_DAYS + 1
        finally:
            sys.set_int_max_str_digits(original_limit)

    @staticmethod
    @pytest.mark.parametrize("raw", ["abc", "1.5", "30d", "²"])
    def test_non_numeric_returns_none(raw):
        assert _parse_period_days(raw) is None

    @staticmethod
    def test_plain_integer_is_returned_as_is():
        assert _parse_period_days("36500") == MAX_CLEANUP_PERIOD_DAYS


class TestRemovalVerification:
    """回归：删除失败/无法确认时必须计 errors + warning，不能伪造成 removed。"""

    @staticmethod
    def test_rmtree_reports_true_when_path_gone(tmp_path):
        target = tmp_path / "session-old"
        target.mkdir()

        assert _rmtree(target) is True
        assert not target.exists()

    @staticmethod
    def test_rmtree_reports_false_when_still_present(tmp_path, monkeypatch):
        """rmtree 被吞掉（未真正删除）时不能算成功。"""
        target = tmp_path / "session-old"
        target.mkdir()
        monkeypatch.setattr("shutil.rmtree", lambda *args, **kwargs: None)

        assert _rmtree(target) is False
        assert target.exists()

    @staticmethod
    def test_rmtree_reports_false_when_verification_fails(tmp_path, monkeypatch, caplog):
        """lstat 抛 OSError（父目录不可访问等）时按"未确认删除"处理，不能算成功。"""

        def _denied(_path):
            raise PermissionError(13, "Permission denied")

        target = tmp_path / "session-old"
        target.mkdir()
        monkeypatch.setattr(f"{CLEANUP_MODULE}.os", SimpleNamespace(lstat=_denied))

        with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
            assert _rmtree(target) is False

        assert any(
            "cleanup_old_sessions:" in rec.getMessage() and rec.levelno == logging.WARNING
            for rec in caplog.records
        ), caplog.text

    @staticmethod
    def test_cleanup_counts_removal_failure_as_error(tmp_path, monkeypatch, caplog):
        """rmtree 没删掉目录时，cleanup_old_sessions 计 errors 并打 warning。"""
        sessions_dir = tmp_path / "sessions"
        _make_session_dirs(sessions_dir, [40 * 86400])
        _patch_sessions_dir(monkeypatch, sessions_dir)
        monkeypatch.setenv(CLEANUP_PERIOD_ENV, "30")
        monkeypatch.setattr("shutil.rmtree", lambda *args, **kwargs: None)

        with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
            result = cleanup_old_sessions()

        assert result == {"removed": 0, "errors": 1}
        assert len(list(sessions_dir.iterdir())) == 1
        assert any(
            "failed to remove" in rec.getMessage() and rec.levelno == logging.WARNING
            for rec in caplog.records
        ), caplog.text


class TestAllExpiredSessions:
    """全部会话都过期时照常全量回收。"""

    @staticmethod
    def test_all_expired_sessions_are_removed(tmp_path, monkeypatch):
        sessions_dir = tmp_path / "sessions"
        total = 12
        _make_session_dirs(sessions_dir, [40 * 86400] * total)
        _patch_sessions_dir(monkeypatch, sessions_dir)
        monkeypatch.setenv(CLEANUP_PERIOD_ENV, "30")

        result = cleanup_old_sessions()

        assert result == {"removed": total, "errors": 0}
        assert list(sessions_dir.iterdir()) == []

    @staticmethod
    def test_fresh_sessions_are_kept(tmp_path, monkeypatch):
        """同一轮里新旧混排：过期的删、未过期的留。"""
        sessions_dir = tmp_path / "sessions"
        _make_session_dirs(sessions_dir, [40 * 86400, 1 * 86400, 60])
        _patch_sessions_dir(monkeypatch, sessions_dir)
        monkeypatch.setenv(CLEANUP_PERIOD_ENV, "30")

        result = cleanup_old_sessions()

        assert result == {"removed": 1, "errors": 0}
        assert sorted(p.name for p in sessions_dir.iterdir()) == ["session-001", "session-002"]
