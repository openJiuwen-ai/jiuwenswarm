# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""FlashBashTool 双账本删除探测回归测试。

锁定评审 P1：文件被 flash edit 更新后，再通过 Bash 删除时，delete 记录
必须保存 flash 侧的最新内容，而不是 stock JSON 里的旧快照。
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from openjiuwen.core.common.exception.codes import StatusCode
from openjiuwen.core.sys_operation import OperationMode
from openjiuwen.harness.tools.base_tool import ToolOutput
from openjiuwen.harness.tools.shell.bash._tool import BashTool

import openjiuwen.core.sys_operation.cwd as agent_cwd
from jiuwenswarm.agents.harness.flash.tools import flash_bash_tool
from jiuwenswarm.agents.harness.flash.tools import flash_file_history
from jiuwenswarm.agents.harness.flash.tools.flash_bash_history import (
    record_detected_deletions,
)
from jiuwenswarm.agents.harness.flash.tools.flash_bash_tool import FlashBashTool


class _FakeSession:
    def __init__(self) -> None:
        self._session_id = "sess-flash-bash"
        self._agent_id = "flash-bash-agent"

    def get_session_id(self) -> str:
        return self._session_id

    def agent_id(self) -> str:
        return self._agent_id


class _FakeFs:
    def __init__(self, content: str) -> None:
        self._content = content

    async def read_file(self, file_path: str):
        return SimpleNamespace(
            code=StatusCode.SUCCESS.code,
            data=SimpleNamespace(content=self._content),
        )


class _FakeOperation:
    def __init__(self, content: str) -> None:
        self.mode = OperationMode.LOCAL
        self._fs = _FakeFs(content)

    def fs(self) -> _FakeFs:
        return self._fs


def _parsed_command(command: str = "echo hi") -> SimpleNamespace:
    return SimpleNamespace(
        command=command,
        run_in_background=False,
        workdir=None,
    )


@pytest.fixture
def temp_dir():
    dir_path = tempfile.mkdtemp()
    yield dir_path
    shutil.rmtree(dir_path, ignore_errors=True)


def _entry(
    action: str,
    timestamp: str,
    old_content: str,
    new_content: str | None,
    **extra,
) -> dict:
    return {
        "action": action,
        "timestamp": timestamp,
        "old_content": old_content,
        "new_content": new_content,
        **extra,
    }


def test_delete_detection_uses_latest_flash_content(temp_dir):
    """stock 旧快照 + flash 新快照混合时，delete 必须记录 flash 最新内容。"""
    file_path = str(Path(temp_dir) / "mixed.txt")
    stock_path = Path(temp_dir) / ".agent_history" / "file_ops_agent_session.json"
    flash_path = (
        Path(temp_dir) / ".agent_history" / "file_ops_flash_agent_session.jsonl"
    )
    stock_path.parent.mkdir(parents=True, exist_ok=True)
    stock_path.write_text(
        json.dumps(
            {
                file_path: [
                    _entry(
                        "edit",
                        "2026-01-01T00:00:00+00:00",
                        "original",
                        "original",
                    )
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    flash_file_history.append_flash_history_entry(
        str(flash_path),
        file_path,
        _entry(
            "edit",
            "2026-01-01T00:01:00+00:00",
            "original",
            "updated",
        ),
    )

    record_detected_deletions(str(stock_path), str(flash_path))

    entries = flash_file_history.load_flash_history(flash_path)[file_path]
    assert len(entries) == 2
    assert entries[-1]["action"] == "delete"
    assert entries[-1]["old_content"] == "updated"
    assert entries[-1]["new_content"] is None


def test_delete_detection_covers_flash_only_history(temp_dir):
    """文件只有 flash 历史、没有 stock JSON 时，删除也不能漏记。"""
    file_path = str(Path(temp_dir) / "flash-only.txt")
    flash_path = (
        Path(temp_dir) / ".agent_history" / "file_ops_flash_agent_session.jsonl"
    )
    flash_file_history.append_flash_history_entry(
        str(flash_path),
        file_path,
        _entry(
            "edit",
            "2026-01-01T00:00:00+00:00",
            "",
            "flash only",
        ),
    )

    record_detected_deletions(None, str(flash_path))

    entries = flash_file_history.load_flash_history(flash_path)[file_path]
    assert entries[-1]["action"] == "delete"
    assert entries[-1]["old_content"] == "flash only"


def test_delete_detection_keeps_rewound_edit_as_current_content(temp_dir):
    """conversation rewind 不恢复工作区，删除前内容必须取 rewound edit。"""
    file_path = str(Path(temp_dir) / "rewound-edit.txt")
    flash_path = (
        Path(temp_dir) / ".agent_history" / "file_ops_flash_agent_session.jsonl"
    )
    flash_file_history.append_flash_history_entry(
        str(flash_path),
        file_path,
        _entry(
            "edit",
            "2026-01-01T00:00:00+00:00",
            "original",
            "updated",
            rewound_out=True,
        ),
    )

    record_detected_deletions(None, str(flash_path))

    entries = flash_file_history.load_flash_history(flash_path)[file_path]
    assert len(entries) == 2
    assert entries[-1]["old_content"] == "updated"


def test_delete_detection_does_not_duplicate_rewound_delete(temp_dir):
    """rewound delete 已表示当前文件缺失，不能再追加旧内容的 delete。"""
    file_path = str(Path(temp_dir) / "rewound-delete.txt")
    flash_path = (
        Path(temp_dir) / ".agent_history" / "file_ops_flash_agent_session.jsonl"
    )
    flash_file_history.append_flash_history_entry(
        str(flash_path),
        file_path,
        _entry(
            "edit",
            "2026-01-01T00:00:00+00:00",
            "original",
            "updated",
        ),
    )
    flash_file_history.append_flash_history_entry(
        str(flash_path),
        file_path,
        _entry(
            "delete",
            "2026-01-01T00:01:00+00:00",
            "updated",
            None,
            rewound_out=True,
        ),
    )

    record_detected_deletions(None, str(flash_path))

    entries = flash_file_history.load_flash_history(flash_path)[file_path]
    assert len(entries) == 2
    assert entries[-1]["action"] == "delete"
    assert entries[-1]["old_content"] == "updated"


def test_delete_detection_skips_discarded_edit(temp_dir):
    """discard 已恢复工作区，删除前内容必须取更早的有效版本。"""
    file_path = str(Path(temp_dir) / "discarded-edit.txt")
    flash_path = (
        Path(temp_dir) / ".agent_history" / "file_ops_flash_agent_session.jsonl"
    )
    flash_file_history.append_flash_history_entry(
        str(flash_path),
        file_path,
        _entry(
            "edit",
            "2026-01-01T00:00:00+00:00",
            "original",
            "updated",
        ),
    )
    flash_file_history.append_flash_history_entry(
        str(flash_path),
        file_path,
        _entry(
            "edit",
            "2026-01-01T00:01:00+00:00",
            "updated",
            "discarded",
            discarded_out=True,
        ),
    )

    record_detected_deletions(None, str(flash_path))

    entries = flash_file_history.load_flash_history(flash_path)[file_path]
    assert len(entries) == 3
    assert entries[-1]["action"] == "delete"
    assert entries[-1]["old_content"] == "updated"


def test_delete_detection_treats_discard_as_stronger_than_rewind(temp_dir):
    """同一条 rewound+discarded 时，按 discard 处理。"""
    file_path = str(Path(temp_dir) / "discarded-rewound.txt")
    flash_path = (
        Path(temp_dir) / ".agent_history" / "file_ops_flash_agent_session.jsonl"
    )
    flash_file_history.append_flash_history_entry(
        str(flash_path),
        file_path,
        _entry(
            "edit",
            "2026-01-01T00:00:00+00:00",
            "original",
            "updated",
        ),
    )
    flash_file_history.append_flash_history_entry(
        str(flash_path),
        file_path,
        _entry(
            "edit",
            "2026-01-01T00:01:00+00:00",
            "updated",
            "discarded",
            rewound_out=True,
            discarded_out=True,
        ),
    )

    record_detected_deletions(None, str(flash_path))

    entries = flash_file_history.load_flash_history(flash_path)[file_path]
    assert entries[-1]["action"] == "delete"
    assert entries[-1]["old_content"] == "updated"


def test_delete_detection_after_all_history_discarded_uses_restored_content(temp_dir):
    """文件首次修改后被 discard 时，删除前内容是恢复后的 old_content。"""
    file_path = str(Path(temp_dir) / "all-discarded.txt")
    flash_path = (
        Path(temp_dir) / ".agent_history" / "file_ops_flash_agent_session.jsonl"
    )
    flash_file_history.append_flash_history_entry(
        str(flash_path),
        file_path,
        _entry(
            "edit",
            "2026-01-01T00:00:00+00:00",
            "original",
            "updated",
            discarded_out=True,
        ),
    )

    record_detected_deletions(None, str(flash_path))

    entries = flash_file_history.load_flash_history(flash_path)[file_path]
    assert len(entries) == 2
    assert entries[-1]["action"] == "delete"
    assert entries[-1]["old_content"] == "original"


def test_delete_detection_after_discarded_delete_uses_restored_content(temp_dir):
    """discard 一个 delete 会恢复 old_content，后续删除不能记录为已删除。"""
    file_path = str(Path(temp_dir) / "discarded-delete.txt")
    flash_path = (
        Path(temp_dir) / ".agent_history" / "file_ops_flash_agent_session.jsonl"
    )
    flash_file_history.append_flash_history_entry(
        str(flash_path),
        file_path,
        _entry(
            "delete",
            "2026-01-01T00:00:00+00:00",
            "original",
            None,
            discarded_out=True,
        ),
    )

    record_detected_deletions(None, str(flash_path))

    entries = flash_file_history.load_flash_history(flash_path)[file_path]
    assert len(entries) == 2
    assert entries[-1]["action"] == "delete"
    assert entries[-1]["old_content"] == "original"


def test_delete_detection_after_discarded_create_does_not_record(temp_dir):
    """discard agent 新建文件后文件本应不存在，无需追加 delete。"""
    file_path = str(Path(temp_dir) / "discarded-create.txt")
    flash_path = (
        Path(temp_dir) / ".agent_history" / "file_ops_flash_agent_session.jsonl"
    )
    flash_file_history.append_flash_history_entry(
        str(flash_path),
        file_path,
        _entry(
            "edit",
            "2026-01-01T00:00:00+00:00",
            None,
            "created",
            discarded_out=True,
        ),
    )

    record_detected_deletions(None, str(flash_path))

    entries = flash_file_history.load_flash_history(flash_path)[file_path]
    assert len(entries) == 1


def test_history_recording_is_disabled_in_sandbox_mode():
    """沙箱文件不在本地文件系统可见，删除探测必须整体关闭，避免虚假 delete。"""
    tool = FlashBashTool.__new__(FlashBashTool)
    tool._operation = SimpleNamespace(mode=OperationMode.SANDBOX)

    assert tool._history_recording_enabled(_parsed_command()) is False


def test_history_recording_remains_enabled_in_local_mode():
    tool = FlashBashTool.__new__(FlashBashTool)
    tool._operation = SimpleNamespace(mode=OperationMode.LOCAL)

    assert tool._history_recording_enabled(_parsed_command()) is True


def test_delete_detection_keeps_existing_file_untouched(temp_dir):
    file_path = str(Path(temp_dir) / "exists.txt")
    Path(file_path).write_text("still here\n", encoding="utf-8")
    flash_path = (
        Path(temp_dir) / ".agent_history" / "file_ops_flash_agent_session.jsonl"
    )
    flash_file_history.append_flash_history_entry(
        str(flash_path),
        file_path,
        _entry(
            "edit",
            "2026-01-01T00:00:00+00:00",
            "",
            "still here\n",
        ),
    )

    record_detected_deletions(None, str(flash_path))

    entries = flash_file_history.load_flash_history(flash_path)[file_path]
    assert len(entries) == 1
    assert entries[0]["action"] == "edit"


async def test_flash_bash_post_detection_uses_flash_ledger(temp_dir, monkeypatch):
    """完整 FlashBashTool 链路：stock hook 关闭，post-detect 读双账本。"""
    file_path = str(Path(temp_dir) / "post-detect.txt")
    Path(file_path).write_text("updated\n", encoding="utf-8")
    flash_path = (
        Path(temp_dir)
        / ".agent_history"
        / "file_ops_flash_flash-bash-agent_sess-flash-bash.jsonl"
    )
    flash_file_history.append_flash_history_entry(
        str(flash_path),
        file_path,
        _entry(
            "edit",
            "2026-01-01T00:00:00+00:00",
            "original",
            "updated",
        ),
    )

    session = _FakeSession()
    monkeypatch.setattr(flash_bash_tool, "get_current_session", lambda: session)
    monkeypatch.setattr(flash_file_history, "get_workspace", lambda: str(temp_dir))
    monkeypatch.setattr(agent_cwd, "get_workspace", lambda: str(temp_dir))

    async def fake_stock_invoke(self, inputs, **kwargs):
        # stock 单账本 hook 必须被 FlashBashTool 关闭，避免把旧内容写进 JSON。
        assert self._build_history_path(session) is None
        Path(file_path).unlink()
        return ToolOutput(success=True, data={"content": "deleted"})

    monkeypatch.setattr(BashTool, "invoke", fake_stock_invoke)
    tool = FlashBashTool(MagicMock(), language="en", agent_id="flash-bash-agent")

    result = await tool.invoke({"command": f"Remove-Item -LiteralPath {file_path}"})

    assert result.success is True
    entries = flash_file_history.load_flash_history(flash_path)[file_path]
    assert len(entries) == 2
    assert entries[-1]["action"] == "delete"
    assert entries[-1]["old_content"] == "updated"


async def test_flash_bash_explicit_rm_records_current_content(temp_dir, monkeypatch):
    """显式 rm 在执行前读取当前全文，未入账文件也能恢复。"""
    file_path = str(Path(temp_dir) / "explicit-rm.txt")
    Path(file_path).write_text("current before rm\n", encoding="utf-8")
    flash_path = (
        Path(temp_dir)
        / ".agent_history"
        / "file_ops_flash_flash-bash-agent_sess-flash-bash.jsonl"
    )

    session = _FakeSession()
    monkeypatch.setattr(flash_bash_tool, "get_current_session", lambda: session)
    monkeypatch.setattr(flash_file_history, "get_workspace", lambda: str(temp_dir))
    monkeypatch.setattr(agent_cwd, "get_workspace", lambda: str(temp_dir))

    async def fake_stock_invoke(self, inputs, **kwargs):
        Path(file_path).unlink()
        return ToolOutput(success=True, data={"content": "deleted"})

    monkeypatch.setattr(BashTool, "invoke", fake_stock_invoke)
    operation = _FakeOperation("current before rm\n")
    tool = FlashBashTool(operation, language="en", agent_id="flash-bash-agent")

    command_path = file_path.replace("\\", "/")
    result = await tool.invoke({"command": f"rm {command_path}"})

    assert result.success is True
    entries = flash_file_history.load_flash_history(flash_path)[file_path]
    assert len(entries) == 1
    assert entries[0]["action"] == "delete"
    assert entries[0]["old_content"] == "current before rm\n"
