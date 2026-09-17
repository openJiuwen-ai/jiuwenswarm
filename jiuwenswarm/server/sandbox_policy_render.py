# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Windows 沙箱运行时 policy 副本 (user_config) 读写.

officeAce 经 WS 接口 (sandbox.files.set / sandbox.network.set) 配置的文件白/黑名单、
网络域名白/黑名单, 直接写进 workspace 下的稀疏副本 (只存用户可配字段, 不 dump 基底),
不存 config.yaml. box-server 启动时读**基底** (打包 windows-policy.yaml, 随 wheel,
default) + **副本** (user_config) 合并 (``policy_engine.merge_policy``, list 去重并集)
→ 不生成合并文件. 机制对齐 jiuwenclaw config.yaml 的 template+override.

副本结构 (``<config_dir>/windows-policy.runtime.yaml``, 稀疏, 只存用户改的字段):
    windows:
      filesystem:
        allow_read:  [<用户白名单>]   # merge 时去重并集到基底 allow_read (workspace/skills 必需集不丢)
        allow_write: [<用户白名单>]
        deny_read:   [<用户黑名单>]   # deny_read/deny_write 用户段
        deny_write:  [<用户黑名单>]
      network:
        disable_all: false            # 总开关; true 传给 box-server, win_proxy 短路拒绝
        egress:
          allowed_domains: [<用户网络白名单>]   # merge 去重并集到基底 (pypi/npmmirror)
          blocked_domains: [<用户网络黑名单>]   # 黑名单优先

拷贝只一次: 副本不存在时建稀疏空骨架; 已存在不重新建 (exe 重启不重拷).
热更新: 基底随 wheel 升级新增字段 → box-server load_policy 重读基底 → 新字段生效
(副本只存用户配置, 不固化基底, 不挡升级).

disable_all (总开关只压不删): true 时副本设 ``windows.network.disable_all=true``,
box-server 传给 EgressFilter 短路拒绝所有出站; 用户 allow/blocked_domains 原样
保留在副本 (不清空), 关掉总开关 (副本改 false) 即恢复. 不清空基底 allowed_domains.

设计依据: docs/windows_sandbox_officeace_integration_design.md §1.3;
box-server/server/policy_reader.py:load_policy (基底+副本合并);
box-server/supervisor/win_proxy.py:EgressFilter (disable_all 短路).
"""

from __future__ import annotations

import copy
import hashlib
import logging
import os
import re
import threading
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

_RUNTIME_COPY_NAME = "windows-policy.runtime.yaml"

# P1-13: 同进程串行化副本读写 (防 lost-update). WS handler 虽串行, 但
# sandbox.files.set + sandbox.network.set 可能并发触发 (officeAce 前端同时改文件
# 和网络). 跨进程锁 (多 agent-server 实例) 收益低场景少, 暂不加 msvcrt/fcntl.
_copy_lock = threading.Lock()


def _config_dir() -> Path:
    """副本所在目录: 与 config.yaml 同目录 (<workspace>/config/).

    照搬 config.yaml 机制: 跟随 ``JIUWENCLAW_DATA_DIR`` / workspace 解析
    (jiuwenclaw.utils.get_config_dir), 不引入新依赖. agent-server 启动时 workspace
    已初始化, 该目录必然存在 (init_user_workspace 创建).
    """
    from jiuwenswarm.common.utils import get_config_dir  # lazy import
    return get_config_dir()


def _runtime_copy_path() -> Path:
    """运行时副本落点: <config_dir>/windows-policy.runtime.yaml (与 config.yaml 同目录)."""
    return _config_dir() / _RUNTIME_COPY_NAME


def _empty_skeleton() -> dict[str, Any]:
    """副本稀疏空骨架 (首次创建用): 只含 windows 空结构, 不 dump 基底."""
    return {
        "windows": {
            "filesystem": {
                "allow_read": [],
                "allow_write": [],
                "deny_read": [],
                "deny_write": [],
            },
            "network": {
                "disable_all": False,
                "egress": {
                    "allowed_domains": [],
                    "blocked_domains": [],
                },
            },
        }
    }


def ensure_copy_exists() -> Path:
    """副本不存在时建稀疏空骨架 (不 dump 基底). 返回副本路径.

    拷贝只一次: 已存在不重建 (exe 重启 / box-server 重启不重拷). 基底内容由
    box-server load_policy 每次重读刷新, 不固化进副本 (热更新安全).
    """
    copy_p = _runtime_copy_path()
    copy_p.parent.mkdir(parents=True, exist_ok=True)
    if not copy_p.is_file():
        try:
            copy_p.write_text(
                yaml.safe_dump(_empty_skeleton(), allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )
            logger.info("已创建运行时 policy 副本骨架: %s", copy_p)
        except OSError as exc:
            logger.warning("写运行时 policy 副本 %s 失败: %s", copy_p, exc)
    return copy_p


def _load_copy() -> dict[str, Any]:
    """读副本 (不存在则建空骨架并返回)."""
    with _copy_lock:  # P1-13: 串行化, 防与 _save_copy 并发 lost-update
        p = ensure_copy_exists()
        try:
            data = yaml.safe_load(p.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            logger.warning("读副本 %s 失败: %s", p, exc)
            return copy.deepcopy(_empty_skeleton())
    if not isinstance(data, dict):
        return copy.deepcopy(_empty_skeleton())
    # 修正错位的顶层 network: 键 (应属于 windows.network).
    # 旧版可能把 network: 写在顶层, 导致合并到 Linux NetworkPolicy 而非 Windows.
    top_network = data.pop("network", None)
    if isinstance(top_network, dict) and top_network:
        win_net = data.setdefault("windows", {}).setdefault("network", {})
        for k, v in top_network.items():
            if k not in win_net or not win_net[k]:
                win_net[k] = v
    # 补齐结构 (兼容旧副本缺字段)
    skel = _empty_skeleton()
    win = data.setdefault("windows", {})
    win.setdefault("filesystem", {})
    win.setdefault("network", {})
    win["network"].setdefault("disable_all", False)
    win["network"].setdefault("egress", {})
    for k in ("allow_read", "allow_write", "deny_read", "deny_write"):
        win["filesystem"].setdefault(k, [])
    for k in ("allowed_domains", "blocked_domains"):
        win["network"]["egress"].setdefault(k, [])
    return data


def _save_copy(data: dict[str, Any]) -> None:
    p = _runtime_copy_path()
    with _copy_lock:  # P1-13: 串行化, 防并发写覆盖
        try:
            # 原子写: 先写 tmp 再 rename. 旧版直接 write_text 覆盖, 写中途崩溃
            # 留半截 YAML → box-server load_policy 解析失败回落基底 (default:allow,
            # 隔离形同虚设). tmp+rename 保证副本要么完整旧值要么完整新值.
            tmp = p.with_suffix(p.suffix + ".tmp")
            tmp.write_text(
                yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )
            os.replace(tmp, p)  # 原子 rename (同文件系统内)
        except OSError as exc:
            logger.warning("写副本 %s 失败: %s", p, exc)
            # tmp 残留清理 (best-effort).
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass


def _norm_str_list(values: list[Any]) -> list[str]:
    return [str(v) for v in values if str(v).strip()]


def _dedup_preserve_order(values: list[str]) -> list[str]:
    """去重保序 (首次出现顺序). 用于 get_sandbox_files_config 合并 read/write 字段."""
    seen: set[str] = set()
    out: list[str] = []
    for v in values:
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out


# P0-7: 输入校验. 黑白名单经 WS 接口下发, 原实现只 str().strip() 无校验,
# 含路径越界/控制字符/非法域名的串被原样写进副本 → 进 WFP/文件 ACL, 制造
# 安全错觉. 这里分路径 (Path.resolve 防越界 + 绝对路径) 与域名 (正则校验) 两类.


def _validate_file_path(value: str) -> str:
    """校验单个文件路径白/黑名单条目, 返回规范化后的绝对路径.

    要求绝对路径 (Windows 形如 C:\\... 或 POSIX /...), 拒绝相对路径与含
    控制字符/空字节的串. 不要求路径当前存在 (黑名单路径可能尚未创建).
    """
    from urllib.parse import unquote
    s = str(value).strip()
    if not s:
        raise ValueError("empty path")
    # 解码 URL 编码后仍校验控制字符 (防 %00 等绕过).
    decoded = unquote(s)
    if "\x00" in s or "\x00" in decoded or any(ord(c) < 32 and c not in "\t" for c in s):
        raise ValueError(f"path contains control characters: {s!r}")
    # 绝对路径校验: Windows 盘符 C:\\ 或 POSIX / 开头. 拒绝相对路径 (避免越界).
    from pathlib import PureWindowsPath, PurePosixPath
    is_abs = PureWindowsPath(s).is_absolute() or PurePosixPath(s).is_absolute()
    if not is_abs:
        raise ValueError(f"path must be absolute: {s!r}")
    return s


def _norm_file_paths(values: list[Any]) -> list[str]:
    """规范化并校验文件路径列表 (白/黑名单). 不合格条目记 warning 跳过, 不整体失败."""
    result: list[str] = []
    for v in values:
        try:
            p = _validate_file_path(v)
            if p not in result:
                result.append(p)
        except ValueError as exc:
            logger.warning("[sandbox.files] 跳过非法路径条目: %s", exc)
    return result


# 域名格式: 通配 *.example.com / example.com / sub.example.com:port 不允许
# (WFP 比对按域名不含端口, 端口在 win_proxy EgressFilter 另外控制).
_DOMAIN_RE = re.compile(
    r"^(?:\*\.)?"
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
    r"[A-Za-z]{2,}$"
)


def _validate_domain(value: str) -> str:
    """校验单个域名白/黑名单条目, 返回原样字符串 (WFP/win_proxy 按域名比对)."""
    s = str(value).strip().lower()
    if not s:
        raise ValueError("empty domain")
    if "\x00" in s or any(ord(c) < 32 for c in s):
        raise ValueError(f"domain contains control characters: {value!r}")
    # 拒绝含端口/路径/查询的串 (WFP 域名条件不含这些, 误配会被静默不匹配).
    if any(c in s for c in ":/?#"):
        raise ValueError(f"domain must not contain port/path/query: {value!r}")
    if not _DOMAIN_RE.match(s):
        raise ValueError(f"invalid domain format: {value!r}")
    return s


def _norm_domains(values: list[Any]) -> list[str]:
    """规范化并校验域名列表. 不合格条目记 warning 跳过."""
    result: list[str] = []
    for v in values:
        try:
            d = _validate_domain(v)
            if d not in result:
                result.append(d)
        except ValueError as exc:
            logger.warning("[sandbox.network] 跳过非法域名条目: %s", exc)
    return result


# ----------------------------------------------------------------------------
# get / set: 读写副本的 windows 段 (用户配置原始值, 不含基底)
# ----------------------------------------------------------------------------

def get_sandbox_files_config() -> dict[str, Any]:
    """返回用户文件白/黑名单 (副本 windows.filesystem, 不含基底必需集).

    与 set_sandbox_files_config 对称: set 把 allow 写入 allow_read+allow_write,
    deny 写入 deny_read+deny_write; get 也读全部 4 字段取并集返回, 避免将来出现
    read/write 分离配置时 get 只读 allow_read/deny_read 而丢失 allow_write/deny_write.
    当前 read=write 冗余写, 并集即原值.
    """
    data = _load_copy()
    fs = data.get("windows", {}).get("filesystem", {})
    allow = _norm_str_list(fs.get("allow_read") or []) + _norm_str_list(fs.get("allow_write") or [])
    deny = _norm_str_list(fs.get("deny_read") or []) + _norm_str_list(fs.get("deny_write") or [])
    # 去重保序 (read/write 冗余写时 4 字段内容相同, 并集会有重复).
    return {
        "allow": _dedup_preserve_order(allow),
        "deny": _dedup_preserve_order(deny),
    }


def set_sandbox_files_config(allow: list[Any], deny: list[Any]) -> dict[str, Any]:
    """整体替换用户文件白/黑名单.

    白名单 allow → 副本 allow_read + allow_write (merge 时去重并集到基底必需集, 不丢).
    黑名单 deny → 副本 deny_read + deny_write (NTFS 显式 Deny 优先).
    空 list 表示清空用户段 (副本该字段置空 → merge 不追加 → 回落基底).
    """
    if not isinstance(allow, list) or not isinstance(deny, list):
        raise ValueError("allow and deny must be lists")
    # P0-7: 路径校验 (绝对路径 + 无控制字符), 非法条目 warning 跳过.
    allow_norm = _norm_file_paths(allow)
    deny_norm = _norm_file_paths(deny)
    data = _load_copy()
    fs = data["windows"]["filesystem"]
    fs["allow_read"] = list(allow_norm)
    fs["allow_write"] = list(allow_norm)
    fs["deny_read"] = list(deny_norm)
    fs["deny_write"] = list(deny_norm)
    _save_copy(data)
    return {"allow": allow_norm, "deny": deny_norm}


def get_sandbox_network_config() -> dict[str, Any]:
    """返回用户网络配置 (副本 windows.network)."""
    data = _load_copy()
    net = data.get("windows", {}).get("network", {})
    eg = net.get("egress", {})
    return {
        "disable_all": bool(net.get("disable_all", False)),
        "allow_domains": _norm_str_list(eg.get("allowed_domains") or []),
        "deny_domains": _norm_str_list(eg.get("blocked_domains") or []),
    }


def set_sandbox_network_config(
    disable_all: bool,
    allow_domains: list[Any],
    deny_domains: list[Any],
) -> dict[str, Any]:
    """整体替换用户网络配置.

    disable_all → 副本 windows.network.disable_all (box-server 传给 EgressFilter 短路
    拒绝所有出站; allow/blocked_domains 原样保留在副本, 不清空, 关掉即恢复).
    allow_domains → 副本 egress.allowed_domains (merge 去重并集到基底 pypi/npmmirror).
    deny_domains → 副本 egress.blocked_domains (黑名单优先).
    """
    if not isinstance(disable_all, bool):
        raise ValueError("disable_all must be boolean")
    if not isinstance(allow_domains, list) or not isinstance(deny_domains, list):
        raise ValueError("allow_domains and deny_domains must be lists")
    # P0-7: 域名校验 (格式 + 无端口/路径/控制字符), 非法条目 warning 跳过.
    allow_norm = _norm_domains(allow_domains)
    deny_norm = _norm_domains(deny_domains)
    data = _load_copy()
    net = data["windows"]["network"]
    net["disable_all"] = disable_all
    net["egress"]["allowed_domains"] = list(allow_norm)
    net["egress"]["blocked_domains"] = list(deny_norm)
    _save_copy(data)
    return {
        "disable_all": disable_all,
        "allow_domains": allow_norm,
        "deny_domains": deny_norm,
    }


def fingerprint_runtime_policy() -> str | None:
    """副本内容指纹 (sha256), 供 JiuwenBoxRunner 判断是否需重 spawn.

    副本不存在返回 None. 用户改 files/network 配置后, runner 检测内容变 → 重 spawn
    box-server (重读基底+副本合并, 新配置生效).
    """
    p = _runtime_copy_path()
    if not p.is_file():
        return None
    try:
        return hashlib.sha256(p.read_bytes()).hexdigest()
    except OSError as exc:
        logger.debug("计算副本指纹失败: %s", exc)
        return None


__all__ = [
    "fingerprint_runtime_policy",
    "get_sandbox_files_config",
    "set_sandbox_files_config",
    "get_sandbox_network_config",
    "set_sandbox_network_config",
]
