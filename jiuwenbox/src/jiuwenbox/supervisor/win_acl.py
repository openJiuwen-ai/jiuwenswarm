# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Windows 文件系统 ACL 控制 (合成 SID + NTFS DACL).

对齐 docs/window沙箱.md 6.7:
  - 读控制: deny-then-allow (依赖安装阶段预装读 ACL; 本模块施加 Deny/Allow Read).
  - 写控制: allow-only (合成 SID JHXSandboxWrite 对 allow_write 授 Allow Write
    + Execute + Delete; 对 deny_write 施加 Deny Write ACE 精细化封锁).

实现通过 ``pywin32`` 的 ``win32security`` 操作 DACL. 所有 win32 调用延迟到
函数体内执行, 模块顶层不 import 任何 pywin32, 因此 Linux 下可 import (运行
时函数会因 ``import win32security`` 失败而抛出明确错误, 由上层 ``sys.platform``
守卫避免误触).
"""

from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path
from typing import Literal

from jiuwenbox.logging_config import configure_logging
from jiuwenbox.supervisor import win_constants as const
from jiuwenbox.server.workspace import (
    JIUWENBOX_HOME,
    JIUWENCLAW_DATA_DIR_PATH,
    OFFICE_CLAW_DATA_ROOT,
)

configure_logging()
logger = logging.getLogger(__name__)


def _require_windows() -> None:
    """守卫: 非 Windows 平台直接抛错而非走到 win32 调用."""
    if sys.platform != "win32":
        raise RuntimeError(
            "win_acl 仅在 Windows 平台可用; 当前平台 "
            f"{sys.platform!r} 不支持 NTFS DACL 操作"
        )


def _ensure_pywin32():
    """惰性加载 pywin32.win32security, 失败时给出清晰错误."""
    try:
        import win32security  # type: ignore[import-not-found]
        import win32con  # type: ignore[import-not-found]
        import win32api  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - 仅 Windows 缺包时触发
        raise RuntimeError(
            "pywin32 (win32security/win32con/win32api) 未安装, 无法操作 NTFS ACL; "
            "请先 pip install pywin32"
        ) from exc
    return win32security, win32con, win32api


def _resolve_sid(sid_str: str):
    """把 SID 字符串解析为 pywin32 SID 对象."""
    win32security, _, _ = _ensure_pywin32()
    return win32security.ConvertStringSidToSid(sid_str)


def get_synthetic_write_sid() -> str:
    """返回合成写权限 SID 字符串.

    SID 格式: S-1-5-21-<sub0>-<sub1>-<RID>. 固定 sub-authority 区段 +
    固定 RID, 不关联任何真实账户. 详见 docs/window沙箱.md 2.2.
    """
    sub_auths = "-".join(str(s) for s in const.SYNTHETIC_WRITE_SID_SUBAUTHS)
    return (
        f"{const.SYNTHETIC_WRITE_SID_PREFIX}-"
        f"{sub_auths}-{const.SYNTHETIC_WRITE_SID_RID}"
    )


def _parse_getace_tuple(ace_tuple: tuple) -> "tuple[int, int, int, object]":
    """解析 pywin32 PyACL.GetAce 返回, 兼容 3/4/5 元组形态.

    返回 (ace_type, ace_flags, access_mask, sid). 无法区分 Allow/Deny 时默认 Allow.
    """
    if len(ace_tuple) == 5:
        ace_type, ace_flags, _ace_size, access_mask, sid = ace_tuple
    elif len(ace_tuple) == 4:
        ace_type, ace_flags, access_mask, sid = ace_tuple
    elif len(ace_tuple) == 3:
        first, access_mask, sid = ace_tuple
        if isinstance(first, tuple):
            # 当前 pywin32: header 子元组 (ace_type, ace_flags).
            ace_type, ace_flags = first
        else:
            # 旧版: (access_mask, ace_flags, sid), 无 ace_type → 视为 Allow.
            ace_type = const.ACCESS_ALLOWED_ACE_TYPE
            ace_flags = access_mask
            access_mask = first
    else:
        ace_type = const.ACCESS_ALLOWED_ACE_TYPE
        ace_flags = 0
        access_mask = 0
        sid = None
    return int(ace_type), int(ace_flags), int(access_mask), sid


def _sid_dedup_key(sid) -> str:
    """把 pywin32 SID 对象转成稳定字符串, 供 ACE 去重比较 (SDDL 形式)."""
    win32security, _, _ = _ensure_pywin32()
    try:
        return win32security.ConvertSidToStringSid(sid)
    except Exception:  # noqa: BLE001 - 回退 repr, 去重降级 best-effort
        return repr(sid)


def _rebuild_acl_with_order(
    existing_dacl,
    new_aces: "list[tuple[int, int, int, object]] | tuple[int, int, int, object] | None" = None,
) -> "object":
    """重建 ACL: Deny ACE 在前, Allow 在后 (NTFS 显式 Deny 优先).

    追加 new_aces 前按 (sid, type, mask, flags) 去重, 避免重复施加相同 ACE 致
    DACL 膨胀 (review #5: 旧版每次建沙箱对 ~/.office-claw 递归 grant Read 各
    追加一份, N 次循环后单文件 N 条重复 ACE).
    """
    win32security, _, _ = _ensure_pywin32()
    deny_aces: list[tuple[int, int, object]] = []  # (flags, mask, sid)
    allow_aces: list[tuple[int, int, object]] = []
    seen: set[tuple[str, int, int, int]] = set()  # (sid_key, ace_type, mask, flags)

    def _add(ace_type: int, flags: int, mask: int, sid,
             bucket: "list[tuple[int, int, object]]") -> None:
        key = (_sid_dedup_key(sid), ace_type, int(mask), int(flags))
        if key in seen:
            return  # 已存在相同 ACE, 跳过 (去重).
        seen.add(key)
        bucket.append((flags, mask, sid))

    if existing_dacl is not None:
        # GetAclSize() 返回字节数非 ACE 个数, 用 GetAceCount() 拿真实个数.
        # 同时对 existing_dacl 内部已有重复 ACE 也去重 (重建只保留一份).
        for i in range(existing_dacl.GetAceCount()):
            ace_type, ace_flags, access_mask, sid = _parse_getace_tuple(
                existing_dacl.GetAce(i),
            )
            if ace_type == const.ACCESS_DENIED_ACE_TYPE:
                _add(ace_type, ace_flags, access_mask, sid, deny_aces)
            else:
                _add(ace_type, ace_flags, access_mask, sid, allow_aces)
    # new_aces 兼容单 ACE 元组 (旧调用) 和 ACE 列表 (合并写入).
    if new_aces is not None:
        if isinstance(new_aces, tuple):
            new_aces_iter = [new_aces]
        else:
            new_aces_iter = new_aces
        for nt, nf, nm, ns in new_aces_iter:
            if nt == const.ACCESS_DENIED_ACE_TYPE:
                _add(nt, nf, nm, ns, deny_aces)
            else:
                _add(nt, nf, nm, ns, allow_aces)
    acl = win32security.ACL()
    for flags, mask, sid in deny_aces:
        # AddAccess{Denied,Allowed}AceEx 新版 pywin32 要 (revision, flags, mask, sid);
        # 旧版只收 (flags, mask, sid) 3 参. ACL_REVISION=2 (普通文件 ACE).
        acl.AddAccessDeniedAceEx(2, flags, mask, sid)
    for flags, mask, sid in allow_aces:
        acl.AddAccessAllowedAceEx(2, flags, mask, sid)
    return acl


def purge_sid_aces(path: str, sid_objs: "list[object]") -> int:
    """清除 ``path`` 上属于 ``sid_objs`` 任一 SID 的所有 ACE (不管 Allow/Deny).
        配置切换 (路径从 deny_read 移到 allow_read) 时, apply 前先清掉旧 ACE,
        避免同 SID 上 Deny+Allow 共存导致 Deny 压过 Allow (NTFS 显式 Deny 优先).
    """
    win32security, _, _ = _ensure_pywin32()
    if not sid_objs:
        return 0
    sd = win32security.GetNamedSecurityInfo(
        path,
        win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION,
    )
    existing_dacl = sd.GetSecurityDescriptorDacl()
    if existing_dacl is None:
        return 0
    # 预算待清 SID 的稳定字符串键 (SDDL 形式), 用字符串相等比对.
    purge_keys = {_sid_dedup_key(t) for t in sid_objs}
    deny_aces: list[tuple[int, int, object]] = []
    allow_aces: list[tuple[int, int, object]] = []
    removed = 0
    for i in range(existing_dacl.GetAceCount()):
        ace_type, ace_flags, ace_mask, ace_sid = _parse_getace_tuple(
            existing_dacl.GetAce(i),
        )
        if _sid_dedup_key(ace_sid) in purge_keys:
            removed += 1
            continue
        if ace_type == const.ACCESS_DENIED_ACE_TYPE:
            deny_aces.append((ace_flags, ace_mask, ace_sid))
        else:
            allow_aces.append((ace_flags, ace_mask, ace_sid))
    if removed == 0:
        return 0
    acl = win32security.ACL()
    for flags, mask, sid in deny_aces:
        acl.AddAccessDeniedAceEx(2, flags, mask, sid)
    for flags, mask, sid in allow_aces:
        acl.AddAccessAllowedAceEx(2, flags, mask, sid)
    _t0 = time.perf_counter()
    win32security.SetNamedSecurityInfo(
        path,
        win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION,
        None, None, acl, None,
    )
    _t1 = time.perf_counter()
    logger.debug(
        "purge ACE: path=%s removed=%d remaining=%d t_set=%.3fs",
        path, removed, len(deny_aces) + len(allow_aces), _t1 - _t0,
    )
    return removed


def grant_ace(
    path: str,
    sid: str | object,
    *,
    rights: int,
    mode: Literal["ALLOW", "DENY"],
    recursive: bool = True,
) -> None:
    """对 ``path`` 施加一个 ACE.

    Args:
        path: 文件/目录绝对路径.
        sid: SID 字符串或 pywin32 SID 对象.
        rights: 访问掩码 (FILE_GENERIC_WRITE 等组合).
        mode: "ALLOW" -> ACCESS_ALLOWED_ACE_TYPE; "DENY" -> ACCESS_DENIED_ACE_TYPE.
        recursive: 目录时是否让子对象继承.
    """
    _require_windows()
    win32security, win32con, _ = _ensure_pywin32()

    if isinstance(sid, str):
        sid_obj = _resolve_sid(sid)
    else:
        sid_obj = sid

    ace_type = (
        const.ACCESS_ALLOWED_ACE_TYPE if mode == "ALLOW"
        else const.ACCESS_DENIED_ACE_TYPE
    )
    inherit_flags = (
        const.RECURSIVE_ACE_FLAGS if recursive else 0
    )

    if recursive and _path_is_heavy_acl_target(path):
        _set_ace_no_propagate(
            path, sid_obj, rights=int(rights), mode=mode, inheritable=True,
        )
        return

    sd = win32security.GetNamedSecurityInfo(
        path,
        win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION,
    )
    existing_dacl = sd.GetSecurityDescriptorDacl()

    acl = _rebuild_acl_with_order(
        existing_dacl,
        (ace_type, inherit_flags, rights, sid_obj),
    )

    flags = win32security.DACL_SECURITY_INFORMATION

    win32security.SetNamedSecurityInfo(
        path,
        win32security.SE_FILE_OBJECT,
        flags,
        None,
        None,
        acl,
        None,
    )
    logger.debug(
        "施加 ACE: path=%s mode=%s rights=0x%X recursive=%s",
        path, mode, rights, recursive,
    )


def grant_aces(
    path: str,
    aces: list[tuple[str | object, int, Literal["ALLOW", "DENY"]]],
    *,
    recursive: bool = True,
) -> None:
    """对 ``path`` 一次 Get/Set 施加多条 ACE (避免多次重建 DACL).

    ``aces`` 元素为 ``(sid, rights, mode)``, 与 ``grant_ace`` 参数同义.
    """
    _require_windows()
    win32security, _, _ = _ensure_pywin32()
    inherit_flags = const.RECURSIVE_ACE_FLAGS if recursive else 0
    new_aces: list[tuple[int, int, int, object]] = []
    for sid, rights, mode in aces:
        sid_obj = _resolve_sid(sid) if isinstance(sid, str) else sid
        ace_type = (
            const.ACCESS_ALLOWED_ACE_TYPE if mode == "ALLOW"
            else const.ACCESS_DENIED_ACE_TYPE
        )
        new_aces.append((ace_type, inherit_flags, int(rights), sid_obj))
    if recursive and _path_is_heavy_acl_target(path):
        for sid, rights, mode in aces:
            _set_ace_no_propagate(
                path, sid, rights=int(rights), mode=mode, inheritable=True,
            )
        return
    sd = win32security.GetNamedSecurityInfo(
        path,
        win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION,
    )
    acl = _rebuild_acl_with_order(sd.GetSecurityDescriptorDacl(), new_aces)
    win32security.SetNamedSecurityInfo(
        path,
        win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION,
        None,
        None,
        acl,
        None,
    )
    logger.debug("施加 %d 条 ACE: path=%s recursive=%s", len(new_aces), path, recursive)


def grant_parent_traverse(path: str, sid: str | object) -> None:
    """给 ``path`` 到用户目录的每一级父目录授非递归 traverse.

    必须用 SetFileSecurity (不向子对象传播). SetNamedSecurityInfo 即使
    inherit_flags=0 也会把可继承 ACE 自动传播到全部子文件, 在 AppData /
    CPython 安装目录上会卡数分钟.

    停在 USERPROFILE (含), 不改 ``C:\\Users`` / ``C:\\``.
    """
    _require_windows()
    win32security, _, _ = _ensure_pywin32()
    try:
        current = Path(path).resolve()
    except OSError:
        return
    if current.is_file():
        current = current.parent
    stop_keys: set[str] = set()
    for env_key in ("USERPROFILE", "HOME"):
        raw = (os.environ.get(env_key) or "").strip()
        if not raw:
            continue
        try:
            stop_keys.add(os.path.normcase(str(Path(raw).resolve())))
        except OSError:
            continue
    sid_obj = _resolve_sid(sid) if isinstance(sid, str) else sid
    seen: set[str] = set()
    while True:
        key = os.path.normcase(str(current))
        if key in seen:
            break
        seen.add(key)
        try:
            sd = win32security.GetFileSecurity(
                str(current), win32security.DACL_SECURITY_INFORMATION,
            )
            existing_dacl = sd.GetSecurityDescriptorDacl()
            acl = _rebuild_acl_with_order(
                existing_dacl,
                (const.ACCESS_ALLOWED_ACE_TYPE, 0, const.FILE_GENERIC_EXECUTE, sid_obj),
            )
            new_sd = win32security.SECURITY_DESCRIPTOR()
            new_sd.SetSecurityDescriptorDacl(1, acl, 0)
            win32security.SetFileSecurity(
                str(current), win32security.DACL_SECURITY_INFORMATION, new_sd,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("parent traverse 失败 path=%s: %s", current, exc)
        if key in stop_keys:
            break
        parent = current.parent
        if parent == current:
            break
        current = parent


_LOG_DIR_NAMES = frozenset({"log", "logs", ".logs"})
_SKIP_BASENAMES = frozenset({"runtime_state"})
# SetNamedSecurityInfo 会向已有子文件传播可继承 ACE, 这些目录体量大会卡启动.
# 只给目录自身打可继承 ACE (SetFileSecurity): 新文件自动继承, 不扫已有树.
_HEAVY_DIR_NAMES = frozenset({
    "runtime",
    "isolation_venv",
    "node_modules",
    "python-win",
    "__pycache__",
    ".git",
    ".venv",
    "venv",
    "site-packages",
    "cache",
    "code cache",
    "gpucache",
    "dawngraphitecache",
    "dawnwebgpucache",
    "shadercache",
    "grshadercache",
    "local storage",
    "session storage",
    "indexeddb",
    "blob_storage",
    "service worker",
    "cacheddata",
    "crashpad",
    "desktop",
    "documents",
    "downloads",
    "pictures",
    "videos",
    "music",
    "onedrive",
})


def _path_is_heavy_acl_target(path: str) -> bool:
    """Desktop / python-win 等大体量目录: 只改自身 DACL."""
    if not path:
        return False
    try:
        if not os.path.isdir(path):
            return False
    except OSError:
        return False
    name = os.path.basename(path.rstrip("\\/")).lower()
    if name in _HEAVY_DIR_NAMES:
        return True
    try:
        home = os.path.normcase(str(Path.home().resolve()))
        key = os.path.normcase(str(Path(os.path.expandvars(str(path))).expanduser().resolve()))
        return key == home
    except OSError:
        return False


_DESKTOP_DATA_READ_RIGHTS = const.ALLOW_READ_EXECUTE_RIGHTS
_DESKTOP_DATA_DENY_RIGHTS = const.DENY_READ_RIGHTS | const.DENY_WRITE_RIGHTS


def _set_ace_no_propagate(
    path: str,
    sid: str | object,
    *,
    rights: int,
    mode: Literal["ALLOW", "DENY"],
    inheritable: bool,
) -> None:
    """只改 ``path`` 自身 DACL, 不向已有子对象传播 (SetFileSecurity)."""
    _require_windows()
    win32security, _, _ = _ensure_pywin32()
    sid_obj = _resolve_sid(sid) if isinstance(sid, str) else sid
    ace_type = (
        const.ACCESS_ALLOWED_ACE_TYPE if mode == "ALLOW"
        else const.ACCESS_DENIED_ACE_TYPE
    )
    inherit_flags = const.RECURSIVE_ACE_FLAGS if inheritable else 0
    sd = win32security.GetFileSecurity(path, win32security.DACL_SECURITY_INFORMATION)
    acl = _rebuild_acl_with_order(
        sd.GetSecurityDescriptorDacl(),
        (ace_type, inherit_flags, int(rights), sid_obj),
    )
    new_sd = win32security.SECURITY_DESCRIPTOR()
    new_sd.SetSecurityDescriptorDacl(1, acl, 0)
    win32security.SetFileSecurity(path, win32security.DACL_SECURITY_INFORMATION, new_sd)


def _current_process_user_sid():
    """当前进程 TokenUser SID (pywin32 SID 对象)."""
    win32security, win32con, win32api = _ensure_pywin32()
    token = win32security.OpenProcessToken(
        win32api.GetCurrentProcess(), win32con.TOKEN_QUERY,
    )
    try:
        user, _ = win32security.GetTokenInformation(
            token, win32security.TokenUser,
        )
        return user
    finally:
        try:
            win32api.CloseHandle(token)
        except Exception:  # noqa: BLE001
            pass


def grant_current_user_write_dac(path: str, *, inheritable: bool = True) -> None:
    """给当前用户预授 WRITE_DAC|READ_CONTROL, 运行时 grant_ace 不再 WinError 5.

    只改 ``path`` 自身 DACL, 不向已有子树传播. owner 虽有隐式 WRITE_DAC,
    显式 ACE 让受限/拆分 token 场景也能改 DACL.
    """
    _require_windows()
    if not os.path.exists(path):
        return
    _set_ace_no_propagate(
        path, _current_process_user_sid(),
        rights=const.WRITE_DAC | const.READ_CONTROL,
        mode="ALLOW",
        inheritable=inheritable,
    )


def apply_desktop_data_rw(
    root: str,
    sandbox_user_sid: str | None = None,
    preserve_write_roots: list[str] | None = None,
) -> list[str]:
    """给桌面 dataDir (claw-desktop) 默认可读不可写.

    只授 FILE_GENERIC_READ | FILE_TRAVERSE, 不授写. 工作区写仍由
    apply_sandbox_acl 的 allow_write 单独施加, 本函数不改 agent/workspace.

    preserve_write_roots (如 jiuwenswarm / workspace) 整棵子树会被跳过以免
    覆盖写 ACE, 因此额外给这些根的祖先 (claw-desktop、agent 等) 补读+遍历,
    并给可写根预授当前用户 WRITE_DAC.

    性能: SetNamedSecurityInfo 会扫已有子树, 因此
      - 干净子树 (不含日志/体量目录): 一次内核传播
      - runtime / venv / node_modules / Electron Cache 等: 只改目录自身
      - log / logs / .logs / runtime_state: 默认不授权, 不进子树;
        只在目录自身打可继承 Deny (SetFileSecurity), 不扫已有文件, 避免拖慢启动
    """
    _require_windows()
    try:
        root_path = Path(os.path.expandvars(str(root))).expanduser().resolve()
    except OSError:
        return []
    if not root_path.is_dir():
        return []

    sid = get_synthetic_write_sid()
    sids: list[str | object] = [sid]
    if sandbox_user_sid:
        sids.append(sandbox_user_sid)
    sid_objs = [_resolve_sid(one) if isinstance(one, str) else one for one in sids]
    sid_keys = {_sid_dedup_key(one) for one in sid_objs}
    rights = _DESKTOP_DATA_READ_RIGHTS
    applied: list[str] = []
    preserve_keys: list[str] = []
    for raw in preserve_write_roots or []:
        try:
            preserve_keys.append(os.path.normcase(str(Path(os.path.expandvars(str(raw))).expanduser().resolve())))
        except OSError:
            continue
    t0 = time.perf_counter()

    def _is_preserved(path: Path) -> bool:
        try:
            key = os.path.normcase(str(path.resolve()))
        except OSError:
            key = os.path.normcase(str(path))
        for preserved in preserve_keys:
            if key == preserved or key.startswith(preserved + os.sep):
                return True
        return False

    def _is_log_or_state(name: str) -> bool:
        lower = name.lower()
        return lower in _LOG_DIR_NAMES or lower in _SKIP_BASENAMES

    def _is_heavy(name: str) -> bool:
        return name.lower() in _HEAVY_DIR_NAMES

    def _acl_replace_our_allow(existing_dacl, inherit_flags: int):
        """去掉本 SID 的旧 Allow (含上次误授的写), 再写上只读 Allow."""
        new_allow = [
            (const.ACCESS_ALLOWED_ACE_TYPE, inherit_flags, int(rights), one)
            for one in sid_objs
        ]
        if existing_dacl is None:
            return _rebuild_acl_with_order(None, new_allow)
        kept: list[tuple[int, int, int, object]] = []
        for i in range(existing_dacl.GetAceCount()):
            ace_type, ace_flags, ace_mask, ace_sid = _parse_getace_tuple(
                existing_dacl.GetAce(i),
            )
            if (
                ace_type == const.ACCESS_ALLOWED_ACE_TYPE
                and _sid_dedup_key(ace_sid) in sid_keys
            ):
                continue
            kept.append((ace_type, ace_flags, ace_mask, ace_sid))
        return _rebuild_acl_with_order(None, kept + new_allow)

    def _grant_here(path: Path) -> None:
        win32security, _, _ = _ensure_pywin32()
        sd = win32security.GetFileSecurity(
            str(path), win32security.DACL_SECURITY_INFORMATION,
        )
        acl = _acl_replace_our_allow(
            sd.GetSecurityDescriptorDacl(), const.RECURSIVE_ACE_FLAGS,
        )
        new_sd = win32security.SECURITY_DESCRIPTOR()
        new_sd.SetSecurityDescriptorDacl(1, acl, 0)
        win32security.SetFileSecurity(
            str(path), win32security.DACL_SECURITY_INFORMATION, new_sd,
        )

    def _grant_tree(path: Path) -> None:
        win32security, _, _ = _ensure_pywin32()
        sd = win32security.GetNamedSecurityInfo(
            str(path),
            win32security.SE_FILE_OBJECT,
            win32security.DACL_SECURITY_INFORMATION,
        )
        acl = _acl_replace_our_allow(
            sd.GetSecurityDescriptorDacl(), const.RECURSIVE_ACE_FLAGS,
        )
        win32security.SetNamedSecurityInfo(
            str(path),
            win32security.SE_FILE_OBJECT,
            win32security.DACL_SECURITY_INFORMATION,
            None, None, acl, None,
        )

    def _deny_here(path: Path) -> None:
        """logs / runtime_state: 只改目录自身, 不扫已有子文件.

        默认不授权: 不进子树、不 grant Allow. 可继承 Deny 挡之后新建的文件;
        不走 SetNamedSecurityInfo, 避免扫日志/yaml 拖慢启动.
        """
        if not path.exists():
            return
        try:
            for one in sid_objs:
                _set_ace_no_propagate(
                    str(path), one,
                    rights=_DESKTOP_DATA_DENY_RIGHTS,
                    mode="DENY",
                    inheritable=True,
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug("desktop data deny 失败 path=%s: %s", path, exc)

    def _subtree_dirty(path: Path) -> bool:
        """子树是否含日志/体量目录. 碰到第一个即返回, 不进入这些目录."""
        try:
            for _dirpath, dirnames, _files in os.walk(path, topdown=True):
                for d in dirnames:
                    if _is_log_or_state(d) or _is_heavy(d):
                        return True
        except OSError:
            return True
        return False

    def _visit(path: Path) -> None:
        if not path.exists():
            return
        if _is_preserved(path):
            return
        if path.is_file():
            _grant_tree(path)
            return
        if _is_log_or_state(path.name):
            _deny_here(path)
            return
        if _is_heavy(path.name):
            _grant_here(path)
            applied.append(str(path))
            return
        if not _subtree_dirty(path):
            _grant_tree(path)
            applied.append(str(path))
            return
        _grant_here(path)
        applied.append(str(path))
        try:
            children = list(path.iterdir())
        except OSError:
            return
        for child in children:
            _visit(child)

    def _grant_bridge_ancestors() -> None:
        """preserve 子树被跳过时, 仍给 claw-desktop / agent 等祖先打读+遍历.

        祖先若本身也是 preserve 根 (如 jiuwenswarm 整树可写), 只追加 RX,
        不走 _grant_here, 以免把写 ACE 替换成只读.
        """
        root_key = os.path.normcase(str(root_path))
        preserve_exact = set(preserve_keys)
        seen: set[str] = set()
        for preserved in preserve_keys:
            try:
                current = Path(preserved).parent
            except OSError:
                continue
            while True:
                try:
                    key = os.path.normcase(str(current.resolve()))
                except OSError:
                    break
                if key in seen:
                    parent = current.parent
                    if parent == current:
                        break
                    current = parent
                    continue
                if len(key) < len(root_key) or not (
                    key == root_key or key.startswith(root_key + os.sep)
                ):
                    break
                seen.add(key)
                try:
                    # 只有可写根本身保留写 ACE; 中间目录 (agent) 只授读+遍历.
                    if key in preserve_exact:
                        for one in sid_objs:
                            _set_ace_no_propagate(
                                str(current), one,
                                rights=rights, mode="ALLOW", inheritable=True,
                            )
                    else:
                        _grant_here(current)
                    applied.append(str(current))
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "desktop bridge ancestor grant 失败 path=%s: %s",
                        current, exc,
                    )
                if key == root_key:
                    break
                parent = current.parent
                if parent == current:
                    break
                current = parent

    def _grant_preserved_write_dac() -> None:
        for preserved in preserve_keys:
            if not os.path.exists(preserved):
                continue
            try:
                grant_current_user_write_dac(preserved, inheritable=True)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "desktop preserve WRITE_DAC 预授失败 path=%s: %s",
                    preserved, exc,
                )

    try:
        try:
            _grant_here(root_path)
            applied.append(str(root_path))
        except Exception as exc:  # noqa: BLE001
            logger.warning("desktop dataDir 根目录读+遍历失败 path=%s: %s", root_path, exc)
        _grant_bridge_ancestors()
        _grant_preserved_write_dac()
        _visit(root_path)
        # preserve 子树会被跳过, 其中的 logs / runtime_state 仍默认不授权.
        # 只改目录自身, 不扫已有文件.
        for rel in (
            "logs",
            os.path.join("jiuwenswarm", "logs"),
            os.path.join("jiuwenswarm", "agent", ".logs"),
            os.path.join("jiuwenswarm", "config", "runtime_state"),
        ):
            _deny_here(root_path / rel)
    except Exception as exc:  # noqa: BLE001
        logger.warning("apply_desktop_data_rw 失败 root=%s: %s", root_path, exc)
        return applied

    logger.info(
        "apply_desktop_data_rw 完成: root=%s elapsed=%.2fs paths=%d",
        root_path, time.perf_counter() - t0, len(applied),
    )
    return applied


def apply_sandbox_acl(
    workspace: str,
    allow_write: list[str],
    deny_write: list[str],
    allow_read: list[str] | None = None,
    deny_read: list[str] | None = None,
    *,
    recursive: bool = True,
    sandbox_user_sid: str | None = None,
    preinstalled_read_paths: "set[str] | None" = None,
) -> list[str]:
    """对沙箱工作区施加文件 ACL (写控制 allow-only + 读控制 deny-then-allow).

    Returns: 施加过 ACE 的顶层路径列表, 供 revoke_sandbox_acl 按清单撤销.
    """
    _require_windows()
    sid = get_synthetic_write_sid()
    allow_read = list(allow_read) if allow_read else []
    deny_read = list(deny_read) if deny_read else []

    applied: list[str] = []
    _purged: set[str] = set()

    # 待清 SID 对象列表 (合成 + 真实). 每个路径首次遇到时先 purge 旧 ACE,
    # 避免配置切换 (路径从 deny_read 移到 allow_read) 时同 SID 上 Deny + Allow
    purge_sids = [_resolve_sid(sid)]
    if sandbox_user_sid:
        purge_sids.append(_resolve_sid(sandbox_user_sid))

    # 段级耗时埋点: 记录每个 ACL 段 (allow_write / deny_write / deny_read /
    # allow_read / traverse) 的总耗时与命中路径数
    seg_stats: dict[str, list[float]] = {}  # 每段 [耗时, 命中数], 由 _seg_end 填

    def _purge_before_grant(path: str) -> None:
        """先清该路径上合成+真实 SID 的所有旧 ACE (best-effort).

        仅在本次 apply_sandbox_acl 调用中首次遇到该路径时执行,
        后续循环对同一路径不再 purge, 避免清掉之前循环刚写入的 ACE
        (如 deny_write 写入的 Deny ACE 被 allow_read 的 purge 误删).
        """
        if path in _purged:
            return
        _purged.add(path)
        try:
            purge_sid_aces(path, purge_sids)
        except Exception as exc:  # noqa: BLE001
            logger.debug("purge 旧 ACE 失败 path=%s: %s", path, exc)

    def _seg_begin() -> float:
        return time.perf_counter()

    def _seg_end(seg: str, t0: float, n: int) -> None:
        seg_stats[seg] = [time.perf_counter() - t0, n]

    # --- 写控制 ---
    write_targets = list(allow_write) or [workspace]
    _t_seg = _seg_begin()
    _n_seg = 0
    for path in write_targets:
        expanded = os.path.expandvars(path)
        if not os.path.exists(expanded):
            try:
                os.makedirs(expanded, exist_ok=True)
                logger.info("allow_write 路径不存在, 已预创建: %s", expanded)
            except OSError as exc:
                logger.warning(
                    "allow_write 路径不存在且预创建失败, 跳过 ACL: %s (%s)",
                    expanded, exc,
                )
                continue
        try:
            _purge_before_grant(expanded)
            grant_ace(
                expanded, sid,
                rights=const.ALLOW_WRITE_RIGHTS,
                mode="ALLOW",
                recursive=recursive,
            )
            if sandbox_user_sid:
                grant_ace(
                    expanded, sandbox_user_sid,
                    rights=const.ALLOW_WRITE_RIGHTS,
                    mode="ALLOW",
                    recursive=recursive,
                )
        except Exception as exc:
            logger.warning(
                "allow_write grant_ace 失败, 跳过该路径 (隔离降级): path=%s mode=ALLOW "
                "原因=%s; 沙箱内该路径写权限不受控",
                expanded, exc,
            )
            continue
        try:
            grant_current_user_write_dac(expanded, inheritable=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "allow_write WRITE_DAC 预授失败 path=%s: %s", expanded, exc,
            )
        applied.append(expanded)
        _n_seg += 1
    _seg_end("allow_write", _t_seg, _n_seg)
    _t_seg = _seg_begin()
    _n_seg = 0
    for path in deny_write:
        expanded = os.path.expandvars(path)
        if not os.path.exists(expanded):
            try:
                os.makedirs(expanded, exist_ok=True)
                logger.info("deny_write 路径不存在, 已预创建: %s", expanded)
            except OSError as exc:
                logger.warning(
                    "deny_write 路径不存在且预创建失败, 跳过 ACL: %s (%s)",
                    expanded, exc,
                )
                continue
        try:
            _purge_before_grant(expanded)
            grant_ace(
                expanded, sid,
                rights=const.DENY_WRITE_RIGHTS,
                mode="DENY",
                recursive=recursive,
            )
            if sandbox_user_sid:
                grant_ace(
                    expanded, sandbox_user_sid,
                    rights=const.DENY_WRITE_RIGHTS,
                    mode="DENY",
                    recursive=recursive,
                )
        except Exception as exc:
            logger.error(
                "deny_write grant_ace 失败, 跳过该路径 (隔离降级): path=%s mode=DENY "
                "原因=%s; 沙箱内该路径写未被拦截", expanded, exc,
            )
            continue
        applied.append(expanded)
        _n_seg += 1
    _seg_end("deny_write", _t_seg, _n_seg)

    # --- 读控制: deny_read (先施加 Deny Read) ---
    _t_seg = _seg_begin()
    _n_seg = 0
    for path in deny_read:
        expanded = os.path.expandvars(path)
        if not os.path.exists(expanded):
            try:
                os.makedirs(expanded, exist_ok=True)
                logger.info("deny_read 路径不存在, 已预创建: %s", expanded)
            except OSError as exc:
                logger.warning(
                    "deny_read 路径不存在且预创建失败, 跳过 ACL: %s (%s)",
                    expanded, exc,
                )
                continue
        try:
            _purge_before_grant(expanded)
            grant_ace(
                expanded, sid,
                rights=const.DENY_READ_RIGHTS,
                mode="DENY",
                recursive=recursive,
            )
            # child 用 jbx-sandbox 真实 SID + token 未受限 (受限 token 暂时弃用, 0xC0000142),
            # 合成 SID 的 deny 对它不生效, 必须给真实 SID 也下 DENY 才能拦截.
            if sandbox_user_sid:
                grant_ace(
                    expanded, sandbox_user_sid,
                    rights=const.DENY_READ_RIGHTS,
                    mode="DENY",
                    recursive=recursive,
                )
        except Exception as exc:
            logger.error(
                "deny_read grant_ace 失败, 跳过该路径 (隔离降级): path=%s mode=DENY "
                "原因=%s; 沙箱内该路径读未被拦截", expanded, exc,
            )
            continue
        applied.append(expanded)
        _n_seg += 1
    _seg_end("deny_read", _t_seg, _n_seg)

    # --- 读控制: allow_read (再施加 Allow Read 覆盖 deny) ---
    _preinstalled = preinstalled_read_paths or set()

    def _is_preinstalled(p: str) -> bool:
        return p.rstrip("\\/").lower() in _preinstalled

    read_targets = allow_read
    if not read_targets:
        # workspace 默认: 独立用户沙箱至少能读自己工作区.
        read_targets = [workspace]
    _t_seg = _seg_begin()
    _n_seg = 0
    for path in read_targets:
        expanded = os.path.expandvars(path)
        if not os.path.exists(expanded):
            try:
                os.makedirs(expanded, exist_ok=True)
                logger.info("allow_read 路径不存在, 已预创建: %s", expanded)
            except OSError as exc:
                logger.warning(
                    "allow_read 路径不存在且预创建失败, 跳过 ACL: %s (%s)",
                    expanded, exc,
                )
                continue
        if _is_preinstalled(expanded):
            logger.debug(
                "allow_read 路径已由 install 预装读 ACL, 跳过运行时 grant: %s",
                expanded,
            )
            applied.append(expanded)
            _n_seg += 1
            continue
        try:
            _purge_before_grant(expanded)
            grant_ace(
                expanded, sid,
                rights=const.FILE_GENERIC_READ,
                mode="ALLOW",
                recursive=recursive,
            )
        except Exception as exc:
            logger.warning(
                "allow_read grant_ace 失败, 跳过该路径 (隔离降级): path=%s mode=ALLOW "
                "原因=%s; 沙箱内该路径读可能受限",
                expanded, exc,
            )
            applied.append(expanded)
            continue
        # runner (jbx-sandbox 真实 SID, token 未受限) 读不了合成 SID 授权路径
        # 须给真实 SID 也 grant Read (含 Execute), 否则 runner 读不了 venv python/DLL.
        if sandbox_user_sid:
            try:
                grant_ace(
                    expanded, sandbox_user_sid,
                    rights=const.FILE_GENERIC_READ,
                    mode="ALLOW",
                    recursive=recursive,
                )
            except Exception as exc:
                logger.warning(
                    "allow_read grant_ace (sandbox SID) 失败, 跳过: path=%s 原因=%s; "
                    "runner 可能读不了该路径",
                    expanded, exc,
                )
        applied.append(expanded)
        _n_seg += 1
    _seg_end("allow_read", _t_seg, _n_seg)

    # 数据根 traverse (非递归 Allow Read): child 访问 workspace/venv/产物时路径上每级父目录需 traverse,
    # 残缺会 lstat EPERM 导致 playwright install 失败. 非递归 (只目录本身) 避免跨沙箱读其他 workspace.
    # 非递归 ACE 不进 applied 清单 (revoke 会 rglob 递归扫误删其他沙箱 ACE). traverse read 残留无害.
    _traverse_roots: list[Path] = []
    for _root in (OFFICE_CLAW_DATA_ROOT, JIUWENCLAW_DATA_DIR_PATH, JIUWENBOX_HOME):
        if _root and _root not in _traverse_roots and os.path.isdir(str(_root)):
            _traverse_roots.append(_root)
    _t_seg = _seg_begin()
    _n_seg = 0
    for _root in _traverse_roots:
        try:
            _purge_before_grant(str(_root))
            grant_ace(
                str(_root), sid,
                rights=const.FILE_GENERIC_READ,
                mode="ALLOW",
                recursive=False,
            )
            if sandbox_user_sid:
                grant_ace(
                    str(_root), sandbox_user_sid,
                    rights=const.FILE_GENERIC_READ,
                    mode="ALLOW",
                    recursive=False,
                )
            _n_seg += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "traverse grant_ace 失败, 跳过: path=%s 原因=%s",
                _root, exc,
            )
    _seg_end("traverse", _t_seg, _n_seg)
    if _traverse_roots:
        logger.info(
            "施加数据根 traverse: roots=%s (非递归, 不进 revoke 清单)",
            [str(r) for r in _traverse_roots],
        )

    # ~/.office-claw 数据根的递归 Read ACL 不在此每次建沙箱补授:
    # review #5 — 旧版每次建沙箱递归 grant Read 一份, revoke 不撤销,
    # N 次循环后单文件 N 条重复 ACE 致 DACL 膨胀. 改由 win_setup.install
    # install 时对整树递归 grant 一次 (合成 SID + 真实 SID 各一份, 与
    # jbx-sandbox 用户生命周期绑定, reinstall 不变). 此处 traverse ACE
    # (非递归, 不膨胀) 仍每次施加, 让 child 能 traverse 数据根.

    # 段级耗时汇总: 定位 ACL 黑箱归属 (哪个段最慢, 即问题路径集).
    _seg_lines = " ".join(
        f"{seg}={v[1]}paths/{v[0]:.2f}s" for seg, v in seg_stats.items() if v[1] or v[0] > 0.01
    )
    logger.info(
        "施加沙箱 ACL 完成: workspace=%s allow_write=%d deny_write=%d "
        "allow_read=%d deny_read=%d %s",
        workspace, len(write_targets), len(deny_write),
        len(read_targets), len(deny_read), _seg_lines,
    )
    # 去重保序 (同一路径可能同时出现在 allow_write 与 allow_read).
    seen: set[str] = set()
    unique: list[str] = []
    for p in applied:
        if p not in seen:
            seen.add(p)
            unique.append(p)
    return unique


def revoke_sandbox_acl(
    paths: list[str] | str,
    sandbox_user_sid: str | None = None,
) -> None:
    """撤销沙箱施加的所有 ACE (合成 SID + 真实 jbx-sandbox SID).

    Args:
        paths: apply_sandbox_acl 返回的施加路径清单 (workspace + allow/deny
            各项, 均为 grant 时的根路径). traverse_roots (数据根) 不在此清单.
        sandbox_user_sid: 真实 jbx-sandbox SID 字符串.
    """
    _require_windows()
    win32security, win32con, _ = _ensure_pywin32()
    sid_str = get_synthetic_write_sid()
    target_sids = [_resolve_sid(sid_str)]
    if sandbox_user_sid:
        target_sids.append(_resolve_sid(sandbox_user_sid))

    if isinstance(paths, str):
        root_list = [paths]
    else:
        root_list = list(paths)

    # 只对根路径本身 purge. 不再 rglob 展开。
    # - 子对象上的继承 ACE 是只读快照, 单独 purge 删不掉 (Windows 立即从父重继承);
    # - 只要删掉根的显式源头 ACE, 子的继承 ACE 自动消失.
    cleaned = 0
    for root_path in root_list:
        p = os.path.expandvars(root_path)
        if not os.path.exists(p):
            continue
        try:
            removed = purge_sid_aces(p, target_sids)
            if removed > 0:
                cleaned += 1
                logger.debug("revoke: 清理 %s 上 %d 个 ACE", p, removed)
        except Exception:  # noqa: BLE001 - ACL 清理是 best-effort
            logger.debug("revoke 单个路径失败: %s", p, exc_info=True)
    logger.info("撤销沙箱 ACL 完成: 清理根路径数=%d", cleaned)
