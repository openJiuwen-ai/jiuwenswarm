# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""FlashEditFileTool 单测：缓存命中零读盘 / 回退路径 / hunks 原子性 / JSONL history。

覆盖验收项（见 flash-editfile-io-analysis-and-optimization.md §7）：
- flash read → flash edit 同文件链路（缓存命中时工具层零 fs 读）；
- 无侧车（stock read）/ 外部修改 / 沙箱约束下的回退与拒绝语义；
- hunks 全命中才写回、重叠拒绝；
- history 纯追加（schema 与 stock 对齐，可被 diff_service 合并读取）。
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

import pytest
import pytest_asyncio

from openjiuwen.core.runner import Runner
from openjiuwen.core.sys_operation import (
    LocalWorkConfig,
    OperationMode,
    SysOperationCard,
)
from openjiuwen.harness.tools.filesystem import (
    ReadFileTool,
    _FILE_READ_REGISTRY,
)

from jiuwenswarm.agents.harness.flash.tools import flash_file_history
from jiuwenswarm.agents.harness.flash.tools.flash_edit_tool import FlashEditFileTool
from jiuwenswarm.agents.harness.flash.tools.flash_file_meta import clear_file_meta_cache
from jiuwenswarm.agents.harness.flash.tools.flash_read_tool import FlashReadFileTool


class _CountingFsProxy:
    """对真实 fs 操作计数（read_file / write_file），其余透传。"""

    def __init__(self, real_fs):
        self._real = real_fs
        self.read_calls = 0
        self.write_calls = 0

    async def read_file(self, *args, **kwargs):
        self.read_calls += 1
        return await self._real.read_file(*args, **kwargs)

    async def write_file(self, *args, **kwargs):
        self.write_calls += 1
        return await self._real.write_file(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._real, name)


class _CountingOpProxy:
    """包一层 SysOperation：fs() 返回计数代理，其余属性透传。"""

    def __init__(self, real_op):
        self._real = real_op
        self.mode = real_op.mode
        self.fs_proxy = _CountingFsProxy(real_op.fs())

    def fs(self):
        return self.fs_proxy

    def __getattr__(self, name):
        return getattr(self._real, name)


class _FakeSession:
    def __init__(self, session_id="sess-flash-edit", agent_id="flash-edit-agent"):
        self._session_id = session_id
        self._agent_id = agent_id

    def get_session_id(self):
        return self._session_id

    def agent_id(self):
        return self._agent_id


@pytest.fixture(autouse=True)
def _isolate_registries():
    _FILE_READ_REGISTRY.clear()
    clear_file_meta_cache()
    yield
    _FILE_READ_REGISTRY.clear()
    clear_file_meta_cache()


@pytest_asyncio.fixture(name="sys_op")
async def sys_op_fixture():
    await Runner.start()
    card_id = "test_flash_edit_tool_op"
    card = SysOperationCard(
        id=card_id,
        mode=OperationMode.LOCAL,
        work_config=LocalWorkConfig(shell_allowlist=["echo"]),
    )
    Runner.resource_mgr.add_sys_operation(card)
    op = Runner.resource_mgr.get_sys_operation(card_id)
    yield op
    Runner.resource_mgr.remove_sys_operation(sys_operation_id=card_id)
    await Runner.stop()


@pytest.fixture
def temp_dir():
    dir_path = tempfile.mkdtemp()
    yield dir_path
    shutil.rmtree(dir_path, ignore_errors=True)


def _make_tools(sys_op):
    proxy = _CountingOpProxy(sys_op)
    read_tool = FlashReadFileTool(proxy)
    edit_tool = FlashEditFileTool(proxy)
    return read_tool, edit_tool, proxy


async def test_gate_requires_prior_read(sys_op, temp_dir):
    _, edit_tool, _ = _make_tools(sys_op)
    file_path = os.path.join(temp_dir, "gate.txt")
    Path(file_path).write_text("hello\n", encoding="utf-8")

    result = await edit_tool.invoke({"file_path": file_path, "old_string": "hello", "new_string": "world"})

    assert result.success is False
    assert "must be read before editing" in result.error


async def test_read_then_edit_single(sys_op, temp_dir):
    read_tool, edit_tool, _ = _make_tools(sys_op)
    file_path = os.path.join(temp_dir, "single.txt")
    Path(file_path).write_text("alpha\nbeta\ngamma\n", encoding="utf-8")

    read_res = await read_tool.invoke({"file_path": file_path})
    assert read_res.success is True

    edit_res = await edit_tool.invoke({
        "file_path": file_path,
        "old_string": "beta",
        "new_string": "BETA",
    })
    assert edit_res.success is True, edit_res.error
    assert edit_res.data["replacements"] == 1
    assert Path(file_path).read_text(encoding="utf-8") == "alpha\nBETA\ngamma\n"


async def test_cache_hit_zero_fs_reads(sys_op, temp_dir):
    """核心验收：flash read 之后连续两次 edit，工具层零 fs 读（缓存命中）。"""
    read_tool, edit_tool, proxy = _make_tools(sys_op)
    file_path = os.path.join(temp_dir, "cache.txt")
    Path(file_path).write_text("one\ntwo\nthree\nfour\n", encoding="utf-8")

    assert (await read_tool.invoke({"file_path": file_path})).success is True

    reads_after_read = proxy.fs_proxy.read_calls
    assert reads_after_read >= 1  # 注册快照的 bytes 读

    first = await edit_tool.invoke({"file_path": file_path, "old_string": "two", "new_string": "TWO"})
    assert first.success is True, first.error
    assert proxy.fs_proxy.read_calls == reads_after_read, "第一次 edit 不应触发工具层读"
    assert proxy.fs_proxy.write_calls == 1

    # 写后回填（registry + 侧车）应让第二次 edit 继续命中缓存。
    second = await edit_tool.invoke({"file_path": file_path, "old_string": "four", "new_string": "FOUR"})
    assert second.success is True, second.error
    assert proxy.fs_proxy.read_calls == reads_after_read, "第二次 edit 仍不应触发工具层读"
    assert proxy.fs_proxy.write_calls == 2

    assert Path(file_path).read_text(encoding="utf-8") == "one\nTWO\nthree\nFOUR\n"


async def test_fresh_read_fallback_without_sidecar(sys_op, temp_dir):
    """stock read_file 只填 registry 不填侧车 → edit 应回退一次全量读并成功。"""
    proxy = _CountingOpProxy(sys_op)
    stock_read = ReadFileTool(proxy)
    edit_tool = FlashEditFileTool(proxy)
    file_path = os.path.join(temp_dir, "fallback.txt")
    Path(file_path).write_text("aaa\nbbb\n", encoding="utf-8")

    assert (await stock_read.invoke({"file_path": file_path})).success is True
    reads_after_read = proxy.fs_proxy.read_calls

    result = await edit_tool.invoke({"file_path": file_path, "old_string": "bbb", "new_string": "BBB"})

    assert result.success is True, result.error
    assert proxy.fs_proxy.read_calls == reads_after_read + 1, "无侧车时应恰好一次全量读"
    assert Path(file_path).read_text(encoding="utf-8") == "aaa\nBBB\n"


async def test_external_modification_rejected(sys_op, temp_dir):
    read_tool, edit_tool, _ = _make_tools(sys_op)
    file_path = os.path.join(temp_dir, "external.txt")
    Path(file_path).write_text("original\n", encoding="utf-8")

    assert (await read_tool.invoke({"file_path": file_path})).success is True

    # 外部改写：内容与长度都变（mtime+size 双失配）。
    Path(file_path).write_text("tampered-with-longer-content\n", encoding="utf-8")

    result = await edit_tool.invoke({"file_path": file_path, "old_string": "original", "new_string": "x"})

    assert result.success is False
    assert "modified externally" in result.error
    # registry 被作废：不重读就再编辑必须再次撞准入门槛。
    retry = await edit_tool.invoke({"file_path": file_path, "old_string": "tampered", "new_string": "y"})
    assert retry.success is False
    assert "must be read before editing" in retry.error


async def test_hunks_all_or_nothing(sys_op, temp_dir):
    read_tool, edit_tool, proxy = _make_tools(sys_op)
    file_path = os.path.join(temp_dir, "hunks.txt")
    original = "aaa\nbbb\nccc\n"
    Path(file_path).write_text(original, encoding="utf-8")

    assert (await read_tool.invoke({"file_path": file_path})).success is True

    bad = await edit_tool.invoke({
        "file_path": file_path,
        "hunks": [
            {"old_string": "aaa", "new_string": "XXX"},
            {"old_string": "nope", "new_string": "YYY"},
        ],
    })
    assert bad.success is False
    assert "hunks[1]" in bad.error
    assert Path(file_path).read_text(encoding="utf-8") == original, "任一 hunk 失败文件必须保持不变"
    assert proxy.fs_proxy.write_calls == 0

    good = await edit_tool.invoke({
        "file_path": file_path,
        "hunks": [
            {"old_string": "aaa", "new_string": "XXX"},
            {"old_string": "ccc", "new_string": "ZZZ"},
            {"old_string": "bbb", "new_string": "YYY"},
        ],
    })
    assert good.success is True, good.error
    assert good.data["replacements"] == 3
    assert proxy.fs_proxy.write_calls == 1, "全部命中共一次写回"
    assert Path(file_path).read_text(encoding="utf-8") == "XXX\nYYY\nZZZ\n"


async def test_hunks_overlap_rejected(sys_op, temp_dir):
    read_tool, edit_tool, _ = _make_tools(sys_op)
    file_path = os.path.join(temp_dir, "overlap.txt")
    Path(file_path).write_text("abcdef\n", encoding="utf-8")

    assert (await read_tool.invoke({"file_path": file_path})).success is True

    result = await edit_tool.invoke({
        "file_path": file_path,
        "hunks": [
            {"old_string": "abc", "new_string": "X"},
            {"old_string": "bcd", "new_string": "Y"},
        ],
    })

    assert result.success is False
    assert "Overlapping" in result.error
    assert Path(file_path).read_text(encoding="utf-8") == "abcdef\n"


async def test_replace_all_within_hunk(sys_op, temp_dir):
    read_tool, edit_tool, _ = _make_tools(sys_op)
    file_path = os.path.join(temp_dir, "replace_all.txt")
    Path(file_path).write_text("x\nx\nx\ny\n", encoding="utf-8")

    assert (await read_tool.invoke({"file_path": file_path})).success is True

    result = await edit_tool.invoke({
        "file_path": file_path,
        "hunks": [{"old_string": "x", "new_string": "z", "replace_all": True}],
    })

    assert result.success is True, result.error
    assert result.data["replacements"] == 3
    assert Path(file_path).read_text(encoding="utf-8") == "z\nz\nz\ny\n"


async def test_crlf_eol_preserved(sys_op, temp_dir):
    read_tool, edit_tool, _ = _make_tools(sys_op)
    file_path = os.path.join(temp_dir, "crlf.txt")
    Path(file_path).write_bytes("first\r\nsecond\r\n".encode("utf-8"))

    assert (await read_tool.invoke({"file_path": file_path})).success is True

    result = await edit_tool.invoke({
        "file_path": file_path,
        "old_string": "second",
        "new_string": "SECOND",
    })

    assert result.success is True, result.error
    assert Path(file_path).read_bytes() == b"first\r\nSECOND\r\n", "CRLF 必须按原样保留"


async def test_create_new_file(sys_op, temp_dir):
    _, edit_tool, _ = _make_tools(sys_op)
    file_path = os.path.join(temp_dir, "created", "new.txt")

    result = await edit_tool.invoke({
        "file_path": file_path,
        "old_string": "",
        "new_string": "brand new\ncontent\n",
    })

    assert result.success is True, result.error
    assert result.data["created"] is True
    assert Path(file_path).read_text(encoding="utf-8") == "brand new\ncontent\n"

    # 创建即注册读状态：紧接着的编辑不应再要求 read_file。
    follow = await edit_tool.invoke({"file_path": file_path, "old_string": "brand", "new_string": "BRAND"})
    assert follow.success is True, follow.error


async def test_history_jsonl_appended_purely(sys_op, temp_dir, monkeypatch):
    """history 纯追加：多次编辑逐行追加，不重写旧行；schema 与 stock 对齐。"""
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.flash.tools.flash_edit_tool.get_current_session",
        lambda: _FakeSession(),
    )
    monkeypatch.setattr(flash_file_history, "get_workspace", lambda: str(temp_dir))

    read_tool, edit_tool, _ = _make_tools(sys_op)
    file_path = os.path.join(temp_dir, "hist.txt")
    # write_bytes 固定 LF：Windows 下 write_text 会把 \n 翻译成 \r\n，
    # 这里要断言的是与写入字节完全一致的 history 条目。
    Path(file_path).write_bytes(b"v1\n")
    assert (await read_tool.invoke({"file_path": file_path})).success is True

    assert (await edit_tool.invoke({"file_path": file_path, "old_string": "v1", "new_string": "v2"})).success
    assert (await edit_tool.invoke({"file_path": file_path, "old_string": "v2", "new_string": "v3"})).success

    history_path = Path(temp_dir) / ".agent_history" / "file_ops_flash_flash-edit-agent_sess-flash-edit.jsonl"
    assert history_path.is_file()

    data = flash_file_history.load_flash_history(history_path)
    entries = data[file_path]
    assert len(entries) == 2

    first, second = entries
    assert first["action"] == "edit"
    assert first["old_content"] == "v1\n"
    assert first["new_content"] == "v2\n"
    assert first["timestamp"]
    assert second["old_content"] == "v2\n"
    assert second["new_content"] == "v3\n"

    # 纯追加验证：第三条编辑后前两行字节级不变。
    with open(history_path, "r", encoding="utf-8") as f:
        lines_before = f.readlines()
    assert (await edit_tool.invoke({"file_path": file_path, "old_string": "v3", "new_string": "v4"})).success
    with open(history_path, "r", encoding="utf-8") as f:
        lines_after = f.readlines()
    assert lines_after[:2] == lines_before
    assert len(lines_after) == 3
    assert json.loads(lines_after[2])["new_content"] == "v4\n"


async def test_multi_file_read_registers_state_for_edit(sys_op, temp_dir):
    """flash 多文件并行读也走 bytes 快照覆写 → registry+侧车就绪 → edit 缓存命中。"""
    read_tool, edit_tool, proxy = _make_tools(sys_op)
    file_a = os.path.join(temp_dir, "a.txt")
    file_b = os.path.join(temp_dir, "b.txt")
    Path(file_a).write_text("aaa\n", encoding="utf-8")
    Path(file_b).write_text("bbb\n", encoding="utf-8")

    multi = await read_tool.invoke({"file_paths": [file_a, file_b]})
    assert multi.success is True
    assert multi.data["succeeded"] == 2

    reads_after_read = proxy.fs_proxy.read_calls
    for path, old, new in ((file_a, "aaa", "AAA"), (file_b, "bbb", "BBB")):
        result = await edit_tool.invoke({"file_path": path, "old_string": old, "new_string": new})
        assert result.success is True, result.error

    assert proxy.fs_proxy.read_calls == reads_after_read, "多文件读注册后编辑同样零读盘"
