# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""flash 文件元数据侧车缓存（snapshot-bound sidecar）。

agent-core 的读注册表（``_FILE_READ_REGISTRY``）只保存 LF 归一化后的文本
内容，原始字节里的 sha256 / EOL / 编码在读取时被丢弃。本模块在 flash 读
文件时顺手把这三样元数据补记下来，供 FlashEditFileTool 在"缓存命中"路
径上零读盘匹配，并把 CAS 乐观锁哈希（``expected_content_sha256``）原样
传给 fs 层——竞态防护与 stock 每次全量读的方案完全等价。

绑定规则（评审确立的不变量）：**内容、摘要、EOL、编码必须来自同一份快照**，
以 (mtime_ns, size_bytes) 作为快照版本号。任何一项失配即视为快照过期，
调用方必须回退全量读，不得拼用两代快照的字段。stock write_file 等非
flash 工具改写文件后 mtime 变化，旧侧车自然失效，一次全量读后自愈。
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from typing import Dict, Optional


@dataclass
class FlashFileMeta:
    """单文件快照元数据：与 registry 内容快照同源同拍。"""

    sha256: str
    eol: str
    encoding: str
    mixed_eol: bool
    snapshot_mtime_ns: int
    snapshot_size_bytes: int


_FILE_META_CACHE: Dict[str, FlashFileMeta] = {}


def _meta_key(file_path: str) -> str:
    return os.path.normcase(os.path.abspath(file_path))


def detect_encoding(raw: bytes) -> str:
    """UTF-16 LE BOM 探测，与 agent-core EditFileTool._detect_encoding 同口径。"""
    return "utf-16-le" if raw[:2] == b"\xff\xfe" else "utf-8"


def detect_eol(raw: bytes) -> str:
    """与 agent-core EditFileTool._detect_eol 同口径。"""
    return "\r\n" if b"\r\n" in raw else "\n"


def detect_mixed_eol(raw: bytes) -> bool:
    """是否同时存在 CRLF 与独立 LF。

    统一 CRLF / 统一 LF 都能从 LF 归一化文本无损重建；只有二者混合时，
    “LF 归一化 + 单一 EOL” 不足以还原原始字节，必须回退全量读。
    """
    if b"\r\n" not in raw:
        return False
    idx = raw.find(b"\n")
    while idx != -1:
        if idx == 0 or raw[idx - 1] != 0x0D:
            return True
        idx = raw.find(b"\n", idx + 1)
    return False


def _text_has_mixed_eol(content: str) -> bool:
    if "\r\n" not in content:
        return False
    idx = content.find("\n")
    while idx != -1:
        if idx == 0 or content[idx - 1] != "\r":
            return True
        idx = content.find("\n", idx + 1)
    return False


def record_file_meta(file_path: str, raw: bytes, mtime_ns: int, size_bytes: int) -> FlashFileMeta:
    """读取快照后记录元数据（raw 必须是刚读到的原始字节，读前 stat）。"""
    meta = FlashFileMeta(
        sha256=hashlib.sha256(raw).hexdigest(),
        eol=detect_eol(raw),
        encoding=detect_encoding(raw),
        mixed_eol=detect_mixed_eol(raw),
        snapshot_mtime_ns=mtime_ns,
        snapshot_size_bytes=size_bytes,
    )
    _FILE_META_CACHE[_meta_key(file_path)] = meta
    return meta


def record_written_meta(
    file_path: str,
    content: str,
    *,
    mtime_ns: int,
    size_bytes: int,
    encoding: str = "utf-8",
) -> FlashFileMeta:
    """写回后按实际提交内容记录元数据。

    fs 层 write_file 的默认编码是 utf-8（stock EditFileTool 不传 encoding，
    二者同口径）；EOL 从写回内容本身探测。调用方拿不到写后 stat（如沙箱
    本地 os.stat 失败）时应改调 pop_file_meta 主动作废，宁慢不错配。
    """
    meta = FlashFileMeta(
        sha256=hashlib.sha256(content.encode(encoding, errors="replace")).hexdigest(),
        eol="\r\n" if "\r\n" in content else "\n",
        encoding=encoding,
        mixed_eol=_text_has_mixed_eol(content),
        snapshot_mtime_ns=mtime_ns,
        snapshot_size_bytes=size_bytes,
    )
    _FILE_META_CACHE[_meta_key(file_path)] = meta
    return meta


def get_file_meta(file_path: str) -> Optional[FlashFileMeta]:
    return _FILE_META_CACHE.get(_meta_key(file_path))


def pop_file_meta(file_path: str) -> None:
    _FILE_META_CACHE.pop(_meta_key(file_path), None)


def clear_file_meta_cache() -> None:
    """测试隔离用：清空进程内缓存。"""
    _FILE_META_CACHE.clear()


__all__ = [
    "FlashFileMeta",
    "clear_file_meta_cache",
    "detect_encoding",
    "detect_eol",
    "detect_mixed_eol",
    "get_file_meta",
    "pop_file_meta",
    "record_file_meta",
    "record_written_meta",
]
