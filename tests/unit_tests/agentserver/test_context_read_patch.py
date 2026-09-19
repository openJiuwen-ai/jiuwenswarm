# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""人设文件读取跳过跨进程读写锁。"""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest

from openjiuwen.core.sys_operation.base import OperationMode
from openjiuwen.core.sys_operation.config import LocalWorkConfig
from openjiuwen.core.sys_operation.local.fs_operation import FsOperation
from openjiuwen.core.sys_operation.sys_operation import SysOperation, SysOperationCard
from openjiuwen.harness.prompts.sections import context as context_mod

from jiuwenswarm.server.runtime.context_read_patch import (
    apply_context_read_patch,
    remove_context_read_patch,
)


class _Workspace:
    """只提供人设文件路径。"""

    def __init__(self, agent_md) -> None:
        self._agent_md = agent_md

    def get_node_path(self, node_name: str):
        if node_name == "AGENT.md":
            return self._agent_md
        return None


def _local_sys_op(name: str) -> SysOperation:
    card = SysOperationCard(id=name, mode=OperationMode.LOCAL, work_config=LocalWorkConfig())
    return SysOperation(card)


@pytest.fixture
def context_read_patch():
    apply_context_read_patch()
    yield
    remove_context_read_patch()
    getattr(context_mod, "_CONTEXT_FILE_CACHE").clear()


def _install_lock_spy(monkeypatch) -> list[bool]:
    """记下有没有跳过锁，避免测试真的去建 sqlite 锁。"""
    skipped: list[bool] = []

    @classmethod
    @asynccontextmanager
    async def _spy(cls, file_path, timeout, *, skip_lock=False):
        del cls, file_path, timeout
        skipped.append(bool(skip_lock))
        yield

    monkeypatch.setattr(FsOperation, "_maybe_read_lock", _spy)
    return skipped


@pytest.mark.asyncio
async def test_persona_read_skips_cross_process_lock(
    context_read_patch, tmp_path, monkeypatch
):
    """读 AGENT.md 时跳过跨进程锁。"""
    agent_md = tmp_path / "AGENT.md"
    agent_md.write_text("这是已经填好的助手说明，不是空白模板。\n", encoding="utf-8")
    skipped = _install_lock_spy(monkeypatch)
    text = await context_mod._read_context_file(
        _local_sys_op("ctx-read-test"),
        _Workspace(agent_md),
        "AGENT.md",
    )
    assert text is not None
    assert skipped == [True]


@pytest.mark.asyncio
async def test_other_reads_still_take_the_lock(context_read_patch, tmp_path, monkeypatch):
    """普通读文件仍然加锁。"""
    note = tmp_path / "note.txt"
    note.write_text("plain", encoding="utf-8")
    skipped = _install_lock_spy(monkeypatch)
    result = await _local_sys_op("ctx-read-other").fs().read_file(str(note))
    assert result.code == 0
    assert result.data.content == "plain"
    assert skipped == [False]
