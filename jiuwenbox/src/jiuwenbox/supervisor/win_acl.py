# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Windows 文件系统 ACL 控制 (合成 SID + NTFS DACL).

对齐 docs/window沙箱.md 6.7:
  - 读控制: deny-then-allow (依赖安装阶段预装读 ACL; 本模块施加 Deny/Allow Read).
  - 写控制: allow-only (合成 SID JHXSandboxWrite 对 allow_write 授 Allow Write
    + Execute + Delete; 对 deny_write 施加 Deny Write ACE 精细化封锁).

DACL 只增删沙箱 SID (jbx-sandbox + 合成写 SID) 的 ACE. 不删 Users / Everyone /
宿主用户 / SYSTEM 的已有条目, 不改盘符根和 ``X:\\Users``. 宿主主用户权限不受影响.

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
    if isinstance(sid, str) and sid.startswith("S-"):
        return sid
    win32security, _, _ = _ensure_pywin32()
    try:
        return win32security.ConvertSidToStringSid(sid)
    except Exception:  # noqa: BLE001 - 回退 repr, 去重降级 best-effort
        return repr(sid)


def _is_volume_root(path: str | Path) -> bool:
    """``D:\\`` / ``D:`` / ``C:\\`` 这类盘符根."""
    raw = str(path).strip()
    if len(raw) >= 2 and raw[1] == ":":
        rest = raw[2:].replace("/", "\\").strip("\\")
        if rest == "":
            return True
    try:
        p = Path(raw)
        try:
            p = p.resolve()
        except OSError:
            pass
        if p.parent == p:
            return True
        s = str(p).replace("/", "\\")
        if len(s) >= 2 and s[1] == ":" and s[2:].strip("\\") == "":
            return True
    except Exception:  # noqa: BLE001
        pass
    return False


def _is_users_directory(path: str | Path) -> bool:
    """``C:\\Users`` / ``D:\\Users`` (profile 的父目录), 不是某个用户主目录."""
    s = str(path).strip().replace("/", "\\").rstrip("\\")
    if len(s) >= 2 and s[1] == ":":
        rest = s[2:].strip("\\")
        if rest.lower() == "users":
            return True
    try:
        p = Path(str(path))
        try:
            p = p.resolve()
        except OSError:
            pass
        return p.name.lower() == "users" and _is_volume_root(p.parent)
    except Exception:  # noqa: BLE001
        return False


def _is_host_dacl_frozen(path: str | Path) -> bool:
    """盘符根和 Users 目录禁止改 DACL (主用户列目录依赖这里的 Users ACE)."""
    return _is_volume_root(path) or _is_users_directory(path)


def _is_acl_forbidden_path(path: str) -> bool:
    try:
        return _is_host_dacl_frozen(path)
    except Exception:  # noqa: BLE001
        return False


def _user_profile_stop_keys() -> set[str]:
    """当前进程 USERPROFILE / HOME 的规范化路径 (grant_parent_traverse 停点)."""
    keys: set[str] = set()
    for env_key in ("USERPROFILE", "HOME"):
        raw = (os.environ.get(env_key) or "").strip()
        if not raw:
            continue
        try:
            keys.add(os.path.normcase(str(Path(raw).resolve())))
        except OSError:
            continue
    try:
        keys.add(os.path.normcase(str(Path.home().resolve())))
    except OSError:
        pass
    return keys


def _expand_fs_path(raw: str) -> Path:
    """展开 %VAR% / ~ 后再 resolve.

    不能用 ``Path.expandvars``: 该 API 从 Python 3.13 才有, 当前运行时
    (含打包的 jiuwenswarm.exe) 是 3.12, 调用会 AttributeError, 沙箱创建
    失败后 Electron 会把运行环境停掉再拉起.
    """
    return Path(os.path.expandvars(os.path.expanduser(str(raw)))).resolve()


def resolve_desktop_data_dir() -> str:
    """桌面数据目录 (claw-desktop), 供 ACL / traverse 共用.

    顺序: ``JIUWENBOX_DESKTOP_DATA_DIR`` → ``JIUWENSWARM_DATA_DIR`` 的父目录
    若名为 claw-desktop → 已存在的 ``%APPDATA%\\claw-desktop``.
    """
    raw = (os.environ.get("JIUWENBOX_DESKTOP_DATA_DIR") or "").strip()
    if raw:
        try:
            return str(_expand_fs_path(raw))
        except OSError:
            return raw
    swarm = (os.environ.get("JIUWENSWARM_DATA_DIR") or "").strip()
    if swarm:
        try:
            parent = _expand_fs_path(swarm).parent
            if parent.name.lower() == "claw-desktop":
                return str(parent)
        except OSError:
            pass
    appdata = (os.environ.get("APPDATA") or "").strip()
    if appdata:
        try:
            candidate = _expand_fs_path(appdata) / "claw-desktop"
            if candidate.is_dir():
                return str(candidate)
        except OSError:
            pass
    return ""


def _desktop_data_dir_keys() -> set[str]:
    """规范化的桌面数据目录路径, 供 traverse 识别叶子 (须授 RW 而非只 traverse)."""
    raw = resolve_desktop_data_dir()
    if not raw:
        return set()
    try:
        return {os.path.normcase(str(Path(raw).resolve()))}
    except OSError:
        return {os.path.normcase(raw)}


def _parent_traverse_plan(is_profile: bool, is_desktop: bool) -> dict[str, object]:
    """主目录 / 数据目录 / 中间目录 的 traverse ACE 规则.

    只追加/替换沙箱 SID 的 ACE, 不删 Users / 宿主条目.
    - 数据目录 (claw-desktop): 可继承读写 (含 FILE_LIST_DIRECTORY), 禁止继承,
      否则 AppData 受保护 DACL 会把沙箱 ACE 冲掉, list_files 报 WinError 5.
    - 主目录: 只授非继承 traverse; 沙箱 SID 显式 Deny List (不含 READ_ATTRIBUTES,
      避免挡住穿过 AppData). 不改 Users/宿主 ACE, 不强制 protect.
    - 中间目录 (AppData/Roaming): 只授非继承 traverse, 不改继承, 旁边其它目录不授权.
    """
    if is_desktop:
        return {
            "rights": const.ALLOW_WRITE_RIGHTS | const.FILE_GENERIC_READ,
            "ace_flags": const.RECURSIVE_ACE_FLAGS,
            "protect": True,
            "skip_inherited": False,
            "drop_world": False,
            "deny_list": False,
        }
    if is_profile:
        return {
            "rights": const.FILE_GENERIC_EXECUTE,
            "ace_flags": 0,
            "protect": None,
            "skip_inherited": False,
            "drop_world": False,
            "deny_list": True,
        }
    return {
        "rights": const.FILE_GENERIC_EXECUTE,
        "ace_flags": 0,
        "protect": None,
        "skip_inherited": False,
        "drop_world": False,
        "deny_list": False,
    }


def _keeper_sid_keys(owner_sid=None) -> set[str]:
    """宿主侧必须保留列目录的 SID: SYSTEM / Administrators / 当前用户."""
    keys = {const.SID_LOCAL_SYSTEM, const.SID_BUILTIN_ADMINISTRATORS}
    if owner_sid is not None:
        keys.add(_sid_dedup_key(owner_sid))
    return keys


def _host_keeper_aces(owner_sid) -> list[tuple[int, int, int, object]]:
    """SYSTEM + Administrators + 当前用户的可继承 Full Control."""
    aces: list[tuple[int, int, int, object]] = []
    for sddl in (const.SID_LOCAL_SYSTEM, const.SID_BUILTIN_ADMINISTRATORS):
        try:
            aces.append(
                (
                    const.ACCESS_ALLOWED_ACE_TYPE,
                    const.RECURSIVE_ACE_FLAGS,
                    const.FILE_ALL_ACCESS,
                    _resolve_sid(sddl),
                ),
            )
        except Exception:  # noqa: BLE001
            logger.debug("解析 keeper SID 失败 sid=%s", sddl)
    if owner_sid is not None:
        aces.append(
            (
                const.ACCESS_ALLOWED_ACE_TYPE,
                const.RECURSIVE_ACE_FLAGS,
                const.FILE_ALL_ACCESS,
                owner_sid,
            ),
        )
    return aces


_PROFILE_TRAVERSE_CHILD_NAMES = frozenset({"appdata", "application data"})


def _harden_profile_children(profile: str, sid_obj) -> None:
    """主目录直接子目录对沙箱 SID 显式 Deny Read, 不递归, 不动 Users/宿主 ACE.

    jbx-sandbox 在 Users 组, Desktop 上 ``Users:(I)(RX)`` 会让它列桌面;
    用沙箱 SID 的 Deny 压过组 Allow. 不改 AppData (穿过 claw-desktop 仍靠 traverse).
    """
    drop_keys = {_sid_dedup_key(sid_obj)}
    new_aces = [
        (
            const.ACCESS_DENIED_ACE_TYPE,
            const.RECURSIVE_ACE_FLAGS,
            const.FILE_GENERIC_READ,
            sid_obj,
        ),
    ]
    win32security, _, _ = _ensure_pywin32()
    try:
        entries = os.scandir(profile)
    except OSError:
        return
    with entries:
        for ent in entries:
            try:
                if not ent.is_dir(follow_symlinks=False):
                    continue
            except OSError:
                continue
            if ent.name.lower() in _PROFILE_TRAVERSE_CHILD_NAMES:
                continue
            try:
                sd = win32security.GetFileSecurity(
                    ent.path, win32security.DACL_SECURITY_INFORMATION,
                )
                acl = _rebuild_acl_with_order(
                    sd.GetSecurityDescriptorDacl(),
                    new_aces,
                    skip_inherited=False,
                    drop_sid_keys=drop_keys,
                )
                _write_file_dacl(ent.path, acl, protect=None, sd=sd)
            except Exception as exc:  # noqa: BLE001
                logger.debug("harden profile child 失败 path=%s: %s", ent.path, exc)


def reconcile_install_acl(sandbox_user_sid: str | None = None) -> None:
    """安装/重装 exe 时把沙箱 SID 的 ACE 收敛到默认隔离态.

    - 去掉残留: 主目录上沙箱 (OI)(CI)(R)
    - 主目录 / AppData / Roaming: 沙箱只能穿过 (显式 Deny List + Allow Traverse)
    - claw-desktop: 沙箱可读可写
    - 不删 Users / 宿主 / SYSTEM / Administrators 已有 ACE
    """
    _require_windows()
    sid = (sandbox_user_sid or "").strip()
    desktop = resolve_desktop_data_dir()
    if desktop and os.path.isdir(desktop):
        apply_desktop_data_rw(desktop, sandbox_user_sid=sid or None)
        if sid:
            grant_parent_traverse(desktop, sid)
        grant_parent_traverse(desktop, get_synthetic_write_sid())
        logger.info("reconcile_install_acl 完成 desktop=%s", desktop)
        return
    try:
        profile = str(Path.home().resolve())
    except OSError:
        profile = (os.environ.get("USERPROFILE") or "").strip()
    if not profile or not os.path.isdir(profile):
        logger.warning("reconcile_install_acl 找不到 USERPROFILE")
        return
    if sid:
        grant_parent_traverse(profile, sid)
    grant_parent_traverse(profile, get_synthetic_write_sid())
    logger.info("reconcile_install_acl 完成 profile=%s (无 claw-desktop)", profile)


def _write_file_dacl(
    path: str,
    acl,
    *,
    protect: bool | None = None,
    sd=None,
) -> None:
    """SetFileSecurity 写 DACL, 不向已有子对象传播, 并保留/恢复禁止继承.

    必须复用 GetFileSecurity 拿到的 SD. 新建空 SECURITY_DESCRIPTOR 会丢掉
    SE_DACL_PROTECTED, C:\\Users 上 Users 组的 (OI)(CI)(IO)(GR,GE) 会灌进
    用户主目录, jbx-sandbox 就能 Get-ChildItem 列出 Desktop/Documents.
    ``protect=True`` 强制恢复禁止继承 (用于 USERPROFILE, 修复已泄漏的主目录).
    ``protect=None`` 保持当前控制位.
    """
    win32security, _, _ = _ensure_pywin32()
    if sd is None:
        sd = win32security.GetFileSecurity(
            path, win32security.DACL_SECURITY_INFORMATION,
        )
    if protect is True:
        bit = getattr(win32security, "SE_DACL_PROTECTED", const.SE_DACL_PROTECTED)
        try:
            sd.SetSecurityDescriptorControl(bit, bit)
        except Exception:  # noqa: BLE001
            logger.debug(
                "SetSecurityDescriptorControl 失败 path=%s protect=%s", path, protect,
            )
    sd.SetSecurityDescriptorDacl(1, acl, 0)
    win32security.SetFileSecurity(
        path, win32security.DACL_SECURITY_INFORMATION, sd,
    )


def _rebuild_acl_with_order(
    existing_dacl,
    new_aces: "list[tuple[int, int, int, object]] | tuple[int, int, int, object] | None" = None,
    *,
    skip_inherited: bool = False,
    drop_sid_keys: "set[str] | None" = None,
    drop_deny_sid_keys: "set[str] | None" = None,
    keep_list_sid_keys: "set[str] | None" = None,
) -> "object":
    """重建 ACL: Deny ACE 在前, Allow 在后 (NTFS 显式 Deny 优先).

    追加 new_aces 前按 (sid, type, mask, flags) 去重, 避免重复施加相同 ACE 致
    DACL 膨胀 (review #5: 旧版每次建沙箱对 ~/.office-claw 递归 grant Read 各
    追加一份, N 次循环后单文件 N 条重复 ACE).

    ``skip_inherited``: SetFileSecurity 写入时应丢掉继承 ACE, 否则继承项会变成
    显式 ACE, 主目录解开保护后 Users 组读权限残留.
    ``drop_sid_keys``: 丢掉这些 SDDL SID 的已有 ACE (清理灌进主目录的 Users 组).
    ``drop_deny_sid_keys``: 丢掉这些 SID 上已有的 Deny ACE (清掉主目录上旧的
    FILE_LIST_DIRECTORY Deny, 否则穿过主目录进 AppData/claw-desktop 仍会 5).
    ``keep_list_sid_keys``: 若给出, 丢掉「带 FILE_LIST_DIRECTORY 且 SID 不在
    此集合」的 Allow ACE. 用来清沙箱残留 (OI)(CI)(R), 只留宿主/SYSTEM/Admins.
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
            if skip_inherited and (ace_flags & const.INHERITED_ACE):
                continue
            sid_key = _sid_dedup_key(sid)
            if drop_sid_keys and sid_key in drop_sid_keys:
                continue
            if (
                drop_deny_sid_keys
                and ace_type == const.ACCESS_DENIED_ACE_TYPE
                and sid_key in drop_deny_sid_keys
            ):
                continue
            if (
                keep_list_sid_keys is not None
                and ace_type != const.ACCESS_DENIED_ACE_TYPE
                and (int(access_mask) & const.FILE_LIST_DIRECTORY)
                and sid_key not in keep_list_sid_keys
            ):
                continue
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
    if _is_acl_forbidden_path(path):
        logger.warning("拒绝改盘符根/Users 的 DACL (purge): %s", path)
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
    if _is_acl_forbidden_path(path):
        logger.warning("拒绝改盘符根/Users 的 DACL (grant_ace): %s", path)
        return
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
    if _is_acl_forbidden_path(path):
        logger.warning("拒绝改盘符根/Users 的 DACL (grant_aces): %s", path)
        return
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


def _policy_deny_rights(path: Path, filesystem_policy: dict | None) -> int:
    """YAML 前缀规则的拒绝权限位; 子路径显式 allow 保留, 同路径 deny 优先.

    仅用于自动补授权, 不改变 apply_sandbox_acl 的原有施加方式.
    allow_write 原本也授 Read, 因此在读轴上保留这一行为.
    """
    if not filesystem_policy:
        return 0
    key = os.path.normcase(str(path.resolve()))
    denied = 0
    for axis, mask in (("read", const.DENY_READ_RIGHTS), ("write", const.DENY_WRITE_RIGHTS)):
        matches = []
        allow_paths = list(filesystem_policy.get(f"allow_{axis}") or [])
        if axis == "read":
            allow_paths += list(filesystem_policy.get("allow_write") or [])
        for is_deny, paths in (
            (False, allow_paths), (True, filesystem_policy.get(f"deny_{axis}") or []),
        ):
            for raw in paths:
                rule = os.path.normcase(str(_expand_fs_path(raw)))
                if key == rule or key.startswith(rule.rstrip(os.sep) + os.sep):
                    matches.append((len(rule), is_deny))
        if matches and max(matches)[1]:
            denied |= mask
    return denied


def grant_parent_traverse(
    path: str, sid: str | object, *, filesystem_policy: dict | None = None,
) -> None:
    """给 ``path`` 到用户目录的每一级父目录授非递归 traverse.

    必须用 SetFileSecurity (不向子对象传播). SetNamedSecurityInfo 即使
    inherit_flags=0 也会把可继承 ACE 自动传播到全部子文件, 在 AppData /
    CPython 安装目录上会卡数分钟.

    停在 USERPROFILE (含), **禁止改盘符根 / ``X:\\Users`` 的 DACL**.
    只增删当前沙箱 SID 的 ACE, 不删 Users / 宿主条目.
    规则见 ``_parent_traverse_plan``:
      - 主目录: 非继承 traverse + 沙箱 Deny List; 直接子目录对沙箱 Deny Read.
      - 数据目录 claw-desktop: 可继承读写 (含列目录), 禁止继承; 否则
        AppData 的保护 DACL 只把宿主 F 传给子目录, 沙箱 list_files 会 5.
      - 中间目录: 只追加沙箱 traverse ACE, 不删已有条目 (尤其 Users 列目录).
    """
    _require_windows()
    win32security, _, _ = _ensure_pywin32()
    try:
        current = Path(path).resolve()
    except OSError:
        return
    if current.is_file():
        current = current.parent
    stop_keys = _user_profile_stop_keys()
    desktop_keys = _desktop_data_dir_keys()
    sid_obj = _resolve_sid(sid) if isinstance(sid, str) else sid
    our_key = _sid_dedup_key(sid_obj)
    seen: set[str] = set()
    while True:
        if _is_host_dacl_frozen(current):
            logger.warning("拒绝改宿主盘符根 DACL (parent traverse): %s", current)
            break
        key = os.path.normcase(str(current))
        if key in seen:
            break
        seen.add(key)
        # 已有 YAML ACL 负责这些路径, 不用通行补授权重建它们的 DACL;
        # 否则会删掉 Deny, 或删掉 deny 目录下用户显式配置的 Allow 例外.
        deny_only = {
            name: (filesystem_policy or {}).get(name, [])
            for name in ("deny_read", "deny_write")
        }
        if _policy_deny_rights(current, deny_only):
            if current.parent == current or key in stop_keys:
                break
            current = current.parent
            continue
        is_profile = key in stop_keys
        is_desktop = key in desktop_keys
        plan = _parent_traverse_plan(is_profile, is_desktop)
        try:
            sd = win32security.GetFileSecurity(
                str(current), win32security.DACL_SECURITY_INFORMATION,
            )
            existing_dacl = sd.GetSecurityDescriptorDacl()
            new_aces: list[tuple[int, int, int, object]] = [
                (
                    const.ACCESS_ALLOWED_ACE_TYPE,
                    int(plan["ace_flags"]),
                    int(plan["rights"]),
                    sid_obj,
                ),
            ]
            if plan.get("deny_list"):
                new_aces.append(
                    (
                        const.ACCESS_DENIED_ACE_TYPE,
                        0,
                        const.FILE_LIST_DIRECTORY,
                        sid_obj,
                    ),
                )
            acl = _rebuild_acl_with_order(
                existing_dacl,
                new_aces,
                skip_inherited=bool(plan["skip_inherited"]),
                drop_sid_keys={our_key},
            )
            _write_file_dacl(
                str(current), acl,
                protect=True if plan["protect"] else None,
                sd=sd,
            )
            if is_profile:
                _harden_profile_children(str(current), sid_obj)
        except Exception as exc:  # noqa: BLE001
            logger.debug("parent traverse 失败 path=%s: %s", current, exc)
        if is_profile:
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
        key = os.path.normcase(str(_expand_fs_path(path)))
        return key == home
    except OSError:
        return False


# 桌面 dataDir 整树读写: Write+Execute+Delete + Read (WRITE ACE 不含 FILE_READ_DATA).
_DESKTOP_DATA_RW_RIGHTS = const.ALLOW_WRITE_RIGHTS | const.FILE_GENERIC_READ


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
    if _is_acl_forbidden_path(path):
        logger.warning("拒绝改盘符根/Users 的 DACL (no-propagate): %s", path)
        return
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
    _write_file_dacl(path, acl, sd=sd)


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
    if _is_acl_forbidden_path(path):
        logger.warning("拒绝改盘符根/Users 的 DACL (WRITE_DAC 预授): %s", path)
        return
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
    *,
    filesystem_policy: dict | None = None,
) -> list[str]:
    """给桌面 dataDir 补读写权限, 同时保留本次 YAML 的显式文件规则.

    授 Allow Write+Execute+Delete+Read. 含 agent workspace / skills 等
    allow_write 子树也会走访并改成 RW (不再跳过): 旧版曾对这些子树打只读
    ACE, 且 apply_sandbox_acl 的 Write ACE 不含 FILE_READ_DATA, 跳过会导致
    写入 skills 等目录时 WinError 5.

    preserve_write_roots 仍用于给可写根预授当前用户 WRITE_DAC, 以及给祖先
    补 RW; 不作为「跳过不授权」的名单.

    性能: SetNamedSecurityInfo 会扫已有子树, 因此
      - 干净子树: 一次内核传播
      - runtime / venv / node_modules / Electron Cache / logs 等: 只改目录自身
        (可继承 ACE, 新文件自动继承, 不扫已有文件)
    """
    _require_windows()
    try:
        root_path = _expand_fs_path(root)
    except OSError:
        return []
    if _is_acl_forbidden_path(str(root_path)):
        logger.warning("拒绝改盘符根/Users 的 DACL (desktop rw): %s", root_path)
        return []
    if not root_path.is_dir():
        return []

    sid = get_synthetic_write_sid()
    sids: list[str | object] = [sid]
    if sandbox_user_sid:
        sids.append(sandbox_user_sid)
    sid_objs = [_resolve_sid(one) if isinstance(one, str) else one for one in sids]
    sid_keys = {_sid_dedup_key(one) for one in sid_objs}
    rights = _DESKTOP_DATA_RW_RIGHTS
    applied: list[str] = []
    preserve_keys: list[str] = []
    for raw in preserve_write_roots or []:
        try:
            preserve_keys.append(os.path.normcase(str(_expand_fs_path(raw))))
        except OSError:
            continue
    t0 = time.perf_counter()

    def _is_log_or_state(name: str) -> bool:
        lower = name.lower()
        return lower in _LOG_DIR_NAMES or lower in _SKIP_BASENAMES

    def _is_heavy(name: str) -> bool:
        return name.lower() in _HEAVY_DIR_NAMES

    def _acl_replace_our_allow(existing_dacl, inherit_flags: int, path: Path):
        """清理旧桌面授权, 按当前 YAML 保留拒绝权限位."""
        denied = _policy_deny_rights(path, filesystem_policy)
        new_allow = [
            (const.ACCESS_ALLOWED_ACE_TYPE, inherit_flags, int(rights) & ~denied, one)
            for one in sid_objs
        ]
        if denied:
            new_allow += [
                (const.ACCESS_DENIED_ACE_TYPE, inherit_flags, denied, one)
                for one in sid_objs
            ]
        if existing_dacl is None:
            return _rebuild_acl_with_order(None, new_allow)
        kept: list[tuple[int, int, int, object]] = []
        for i in range(existing_dacl.GetAceCount()):
            ace_type, ace_flags, ace_mask, ace_sid = _parse_getace_tuple(
                existing_dacl.GetAce(i),
            )
            if _sid_dedup_key(ace_sid) in sid_keys:
                continue
            kept.append((ace_type, ace_flags, ace_mask, ace_sid))
        return _rebuild_acl_with_order(None, kept + new_allow)

    def _grant_here(path: Path) -> None:
        win32security, _, _ = _ensure_pywin32()
        sd = win32security.GetFileSecurity(
            str(path), win32security.DACL_SECURITY_INFORMATION,
        )
        acl = _acl_replace_our_allow(
            sd.GetSecurityDescriptorDacl(), const.RECURSIVE_ACE_FLAGS, path,
        )
        _write_file_dacl(str(path), acl, protect=True, sd=sd)

    def _grant_tree(path: Path) -> None:
        win32security, _, _ = _ensure_pywin32()
        sd = win32security.GetNamedSecurityInfo(
            str(path),
            win32security.SE_FILE_OBJECT,
            win32security.DACL_SECURITY_INFORMATION,
        )
        acl = _acl_replace_our_allow(
            sd.GetSecurityDescriptorDacl(), const.RECURSIVE_ACE_FLAGS, path,
        )
        win32security.SetNamedSecurityInfo(
            str(path),
            win32security.SE_FILE_OBJECT,
            win32security.DACL_SECURITY_INFORMATION
            | const.PROTECTED_DACL_SECURITY_INFORMATION,
            None, None, acl, None,
        )

    def _subtree_dirty(path: Path) -> bool:
        """子树是否含日志/体量目录. 碰到第一个即返回, 不进入这些目录."""
        key = os.path.normcase(str(path.resolve()))
        for paths in (filesystem_policy or {}).values():
            for raw in paths:
                rule = os.path.normcase(str(_expand_fs_path(raw)))
                if rule.startswith(key.rstrip(os.sep) + os.sep):
                    return True  # 不跨越 YAML 边界做整树自动补授权
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
        try:
            if path.is_file():
                _grant_tree(path)
                return
            if _is_log_or_state(path.name) or _is_heavy(path.name):
                _grant_here(path)
                applied.append(str(path))
                return
            if not _subtree_dirty(path):
                _grant_tree(path)
                applied.append(str(path))
                return
            _grant_here(path)
            applied.append(str(path))
        except Exception as exc:  # noqa: BLE001
            logger.warning("desktop data RW grant 失败 path=%s: %s", path, exc)
            return
        try:
            children = list(path.iterdir())
        except OSError:
            return
        for child in children:
            _visit(child)

    def _grant_bridge_ancestors() -> None:
        """preserve 子树祖先 (claw-desktop / agent) 补打读写 ACE.

        祖先若本身也是 preserve 根, 只追加 RW, 不走 _grant_here, 以免重建 DACL.
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
                    # preserve 根本身已有写 ACE; 中间目录同样授 RW.
                    if key in preserve_exact and not filesystem_policy:
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
            logger.warning("desktop dataDir 根目录读写授权失败 path=%s: %s", root_path, exc)
        _grant_bridge_ancestors()
        _grant_preserved_write_dac()
        _visit(root_path)
        # agent workspace / skills: 不全树 SetNamedSecurityInfo (避开 node_modules
        # 受保护文件导致整段失败). 改为按目录走访:
        #   - 每个 skill 子目录本身打可继承 RW ACE → 目录内新建/改文件可写
        #   - 干净子树 (无 node_modules) 才对该子树传播
        #   - node_modules 等大体量目录只改目录自身, 不扫内部文件
        for preserved in preserve_keys:
            try:
                p = Path(preserved)
                if not p.exists():
                    continue
                key = os.path.normcase(str(p.resolve()))
                root_key = os.path.normcase(str(root_path))
                if not (key == root_key or key.startswith(root_key + os.sep)):
                    continue
                _grant_here(p)
                applied.append(str(p))
                skills = p / "skills"
                if skills.is_dir():
                    _visit(skills)
            except Exception as exc:  # noqa: BLE001
                logger.debug("desktop preserve RW grant 失败 path=%s: %s", preserved, exc)
        # 旧版曾对这些路径打 Deny; 现在整树读写, 补授以清掉残留 Deny.
        for rel in (
            "logs",
            os.path.join("jiuwenswarm", "logs"),
            os.path.join("jiuwenswarm", "agent", ".logs"),
            os.path.join("jiuwenswarm", "config", "runtime_state"),
        ):
            target = root_path / rel
            if not target.exists():
                continue
            try:
                _grant_here(target)
            except Exception as exc:  # noqa: BLE001
                logger.debug("desktop data RW grant 失败 path=%s: %s", target, exc)
        # 只走访 YAML deny 涉及的桌面子树, 修复旧版本留下的显式 Allow /
        # 受保护 DACL. 单纯在父目录加 Deny 无法覆盖它们. 不跟随 junction.
        repaired: set[str] = set()
        deny_paths = list((filesystem_policy or {}).get("deny_read") or [])
        deny_paths += list((filesystem_policy or {}).get("deny_write") or [])
        for raw in deny_paths:
            denied_root = _expand_fs_path(raw)
            if root_path.is_relative_to(denied_root):
                denied_root = root_path
            if not denied_root.is_relative_to(root_path):
                continue
            pending = [denied_root]
            while pending:
                target = pending.pop()
                key = os.path.normcase(str(target))
                if key in repaired or not target.exists():
                    continue
                repaired.add(key)
                try:
                    if target.lstat().st_file_attributes & 0x400:  # FILE_ATTRIBUTE_REPARSE_POINT
                        continue
                    _grant_here(target)
                    if target.is_dir():
                        pending.extend(target.iterdir())
                except Exception as exc:  # noqa: BLE001 - 与已有 ACL 失败语义一致
                    logger.warning("desktop YAML ACL 修复失败 path=%s: %s", target, exc)
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
                rights=const.ALLOW_WRITE_RIGHTS | const.FILE_GENERIC_READ,
                mode="ALLOW",
                recursive=recursive,
            )
            if sandbox_user_sid:
                grant_ace(
                    expanded, sandbox_user_sid,
                    rights=const.ALLOW_WRITE_RIGHTS | const.FILE_GENERIC_READ,
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

    # 不再给 ~/.office-claw 整树/根特殊授权. 清掉旧版根 ACE, 子继承 ACE 随源头失效.
    # workspace 祖先 traverse 由 grant_parent_traverse 覆盖 (停在 USERPROFILE).
    try:
        _office = str(OFFICE_CLAW_DATA_ROOT) if OFFICE_CLAW_DATA_ROOT else ""
        if _office and os.path.isdir(_office):
            _purge_before_grant(_office)
            logger.info("已去掉 ~/.office-claw 特殊 ACL: %s", _office)
    except Exception as exc:  # noqa: BLE001
        logger.warning("去掉 ~/.office-claw 特殊 ACL 失败 (非致命): %s", exc)

    # 数据根 traverse (非递归 Allow Read): child 访问 workspace/venv/产物时路径上每级父目录需 traverse,
    # 残缺会 lstat EPERM 导致 playwright install 失败. 非递归 (只目录本身) 避免跨沙箱读其他 workspace.
    # 非递归 ACE 不进 applied 清单 (revoke 会 rglob 递归扫误删其他沙箱 ACE). traverse read 残留无害.
    _traverse_roots: list[Path] = []
    for _root in (JIUWENCLAW_DATA_DIR_PATH, JIUWENBOX_HOME):
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

    # ~/.office-claw 不再每次建沙箱 (也不在 install) 递归 grant Read.
    # 子目录 traverse 由 grant_parent_traverse 覆盖, 不在此对数据根整树授权.

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
