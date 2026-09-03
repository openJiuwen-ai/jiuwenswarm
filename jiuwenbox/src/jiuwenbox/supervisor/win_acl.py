# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Windows 文件系统 ACL 控制 (合成 SID + NTFS DACL).

读写不对称:
  - 读: 黑名单. Users/Everyone 默认 RX 不必再授 Allow; deny_read 在名单
    目录上打 Deny Read, 并用 SetNamedSecurityInfo 传播到已有子树.
  - 写: 白名单. allow_write (及 workspace 缺省) 打 Allow Write 并传播到已有子树;
    deny_write 同样传播 (否则 allow_write 已把可写 ACE 灌进 .git 等已有子树).
    未列出的路径默认写不了.

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
from dataclasses import dataclass, field
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


@dataclass(frozen=True)
class AclGrantRecord:
    """单次 grant 的指纹记录, 供下次启动跳过传播."""

    path: str
    kind: str  # allow_write | deny_write | allow_read | deny_read
    mask: int
    flags: int
    propagated: bool
    skipped: bool = False


@dataclass
class ApplyAclResult:
    """apply_sandbox_acl 返回值."""

    paths: list[str] = field(default_factory=list)
    grants: list[AclGrantRecord] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)


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


def _path_key(raw: str) -> str:
    try:
        return os.path.normcase(str(_expand_fs_path(raw)))
    except OSError:
        return os.path.normcase(os.path.abspath(os.path.expandvars(os.path.expanduser(raw))))


def _path_covered_by_roots(path_key: str, roots: set[str]) -> bool:
    for root in roots:
        if not root:
            continue
        if path_key == root or path_key.startswith(root + os.sep):
            return True
    return False


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
    - 主目录: 只授非继承 traverse, 不打 Deny List / Deny Read
      (默认跟 NTFS; 黑名单用 policy deny_*). 不改 Users/宿主 ACE.
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
            "deny_list": False,
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



def _harden_profile_children(profile: str, sid_obj) -> None:
    """不再给主目录子项打 Deny. 保留函数名供测试 monkeypatch.
    """
    del profile, sid_obj


def reconcile_install_acl(sandbox_user_sid: str | None = None) -> None:
    """安装/重装 exe 时把沙箱 SID 的 ACE 收敛到默认隔离态.

    - 去掉残留: 主目录上沙箱 (OI)(CI)(R)
    - 主目录 / AppData / Roaming: 沙箱只补 Allow Traverse (不打 Deny List)
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


def _write_file_dacl_propagate(path: str, acl, *, sd=None) -> None:
    """用 SetNamedSecurityInfo 写 DACL, 把可继承 ACE 推到已有子对象.

    保留对象自身的 SE_DACL_PROTECTED (禁止从再上一级继承), 避免 Documents
    解开保护后被主目录 Users (OI)(CI) 灌入. 已禁止继承的子目录系统不会改.
    """
    win32security, _, _ = _ensure_pywin32()
    if sd is None:
        sd = win32security.GetFileSecurity(
            path, win32security.DACL_SECURITY_INFORMATION,
        )
    try:
        ctrl, _rev = sd.GetSecurityDescriptorControl()
    except Exception:  # noqa: BLE001
        ctrl = 0
    info = win32security.DACL_SECURITY_INFORMATION
    protected_bit = getattr(
        win32security, "SE_DACL_PROTECTED", const.SE_DACL_PROTECTED,
    )
    if ctrl & protected_bit:
        info |= getattr(
            win32security, "PROTECTED_DACL_SECURITY_INFORMATION",
            const.PROTECTED_DACL_SECURITY_INFORMATION,
        )
    else:
        info |= getattr(
            win32security, "UNPROTECTED_DACL_SECURITY_INFORMATION",
            const.UNPROTECTED_DACL_SECURITY_INFORMATION,
        )
    t0 = time.perf_counter()
    win32security.SetNamedSecurityInfo(
        path,
        win32security.SE_FILE_OBJECT,
        info,
        None, None, acl, None,
    )
    logger.info(
        "SetNamedSecurityInfo 已传播 DACL path=%s elapsed=%.2fs",
        path, time.perf_counter() - t0,
    )


def _warn_if_heavy_propagate(path: str, kind: str) -> None:
    """对 USERPROFILE 整树传播时打警告 (Documents 等子目录不警告)."""
    try:
        home = os.path.normcase(str(Path.home().resolve()))
        key = os.path.normcase(str(Path(path).resolve()))
    except OSError:
        return
    if key == home:
        logger.warning(
            "%s 将对整个 USERPROFILE 传播 ACE, 可能很慢: %s", kind, path,
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


def purge_sid_aces(
    path: str,
    sid_objs: "list[object]",
    *,
    propagate: bool = False,
) -> int:
    """清除 ``path`` 上属于 ``sid_objs`` 任一 SID 的所有 ACE (不管 Allow/Deny).

    配置切换 (路径从 deny_read 移到 allow_read) 时, apply 前先清掉旧 ACE,
    避免同 SID 上 Deny+Allow 共存导致 Deny 压过 Allow (NTFS 显式 Deny 优先).

    ``propagate=True`` 且 path 为目录时走 SetNamedSecurityInfo, 把清掉沙箱
    ACE 后的 DACL 推到已有子树 (传播式 grant 之后子对象上的 (I) 副本不会
    随根上 SetFileSecurity 自动消失).
    """
    win32security, _, _ = _ensure_pywin32()
    if not sid_objs:
        return 0
    if _is_acl_forbidden_path(path):
        logger.warning("拒绝改盘符根/Users 的 DACL (purge): %s", path)
        return 0
    sd = win32security.GetFileSecurity(
        path, win32security.DACL_SECURITY_INFORMATION,
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
    if propagate and os.path.isdir(path):
        _write_file_dacl_propagate(path, acl, sd=sd)
    else:
        _write_file_dacl(path, acl, sd=sd)
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
        recursive: True 时 ACE 带 (OI)(CI), 只写本节点, 新对象继承, 不扫已有子树.
    """
    _require_windows()
    if _is_acl_forbidden_path(path):
        logger.warning("拒绝改盘符根/Users 的 DACL (grant_ace): %s", path)
        return

    if isinstance(sid, str):
        sid_obj = _resolve_sid(sid)
    else:
        sid_obj = sid

    # 只改 path 自身. recursive=True 时 ACE 带 (OI)(CI), 新对象继承;
    # 不用 SetNamedSecurityInfo, 避免扫已有子树.
    _set_ace_no_propagate(
        path, sid_obj, rights=int(rights), mode=mode, inheritable=recursive,
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
    replace_aces_no_propagate(path, aces, inheritable=recursive)


def grant_parent_traverse(
    path: str,
    sid: str | object,
    *,
    readable_roots: list[str] | None = None,
    preserve_roots: list[str] | None = None,
) -> None:
    """给 ``path`` 的祖先授 traverse, 一直到 USERPROFILE.

    **不改 ``path`` 自身 DACL** (从父目录起走). ``allow_write`` / claw-desktop
    已在目标节点打了可继承写 ACE; 若再从目标自己走, 中间目录规则会把写 ACE
    覆盖成 traverse-only, 沙箱在该目录创建文件会「拒绝访问」.

    ``preserve_roots`` 里的祖先同样跳过 (例如目标是白名单目录下的文件,
    父目录就是 allow_write 根).

    必须用 SetFileSecurity (不向子对象传播).
    停在 USERPROFILE (含), **禁止改盘符根 / ``X:\\Users`` 的 DACL**.
    """
    _require_windows()
    win32security, _, _ = _ensure_pywin32()
    try:
        resolved = Path(path).resolve()
    except OSError:
        return
    stop_keys = _user_profile_stop_keys()
    current = resolved
    # 非主目录的目录/文件: 从父级开始, 避免覆盖 allow_write 打在目标上的写 ACE.
    # USERPROFILE 自身仍要打 traverse (install reconcile 会直接传主目录).
    if current.is_file() or os.path.normcase(str(current)) not in stop_keys:
        current = current.parent
    desktop_keys = _desktop_data_dir_keys()
    readable_keys: set[str] = set()
    for raw in readable_roots or []:
        if not raw:
            continue
        try:
            readable_keys.add(_path_key(raw))
        except OSError:
            continue
    preserve_keys: set[str] = set()
    for raw in preserve_roots or []:
        if not raw:
            continue
        try:
            preserve_keys.add(_path_key(raw))
        except OSError:
            continue
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
        is_profile = key in stop_keys
        is_desktop = key in desktop_keys
        if key in preserve_keys and not is_profile:
            parent = current.parent
            if parent == current:
                break
            current = parent
            continue
        plan = _parent_traverse_plan(is_profile, is_desktop)
        profile_is_readable = is_profile and _path_covered_by_roots(key, readable_keys)
        if profile_is_readable:
            plan = {
                "rights": const.ALLOW_READ_EXECUTE_RIGHTS,
                "ace_flags": const.RECURSIVE_ACE_FLAGS,
                "protect": None,
                "skip_inherited": False,
                "drop_world": False,
                "deny_list": False,
            }
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
        except Exception as exc:  # noqa: BLE001
            logger.debug("parent traverse 失败 path=%s: %s", current, exc)
        if is_profile:
            break
        parent = current.parent
        if parent == current:
            break
        current = parent


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


def replace_aces_no_propagate(
    path: str,
    aces: list[tuple[str | object, int, Literal["ALLOW", "DENY"]]],
    *,
    drop_sids: list[str | object] | None = None,
    inheritable: bool = False,
    propagate: bool = False,
) -> None:
    """一次 Get DACL / 重建 / 写入.

    ``inheritable=True`` 给 ACE 打 (OI)(CI).
    ``propagate=True`` 且 path 为目录时用 SetNamedSecurityInfo 推到已有子树;
    否则 SetFileSecurity 只改本节点.
    """
    _require_windows()
    if _is_acl_forbidden_path(path):
        logger.warning("拒绝改盘符根/Users 的 DACL (no-propagate replace): %s", path)
        return
    if not aces and not drop_sids:
        return
    win32security, _, _ = _ensure_pywin32()
    inherit_flags = const.RECURSIVE_ACE_FLAGS if inheritable else 0
    new_aces: list[tuple[int, int, int, object]] = []
    for sid, rights, mode in aces:
        sid_obj = _resolve_sid(sid) if isinstance(sid, str) else sid
        ace_type = (
            const.ACCESS_ALLOWED_ACE_TYPE if mode == "ALLOW"
            else const.ACCESS_DENIED_ACE_TYPE
        )
        new_aces.append((ace_type, inherit_flags, int(rights), sid_obj))
    drop_keys: set[str] | None = None
    if drop_sids:
        drop_keys = {
            _sid_dedup_key(_resolve_sid(s) if isinstance(s, str) else s)
            for s in drop_sids
        }
    sd = win32security.GetFileSecurity(path, win32security.DACL_SECURITY_INFORMATION)
    acl = _rebuild_acl_with_order(
        sd.GetSecurityDescriptorDacl(),
        new_aces or None,
        drop_sid_keys=drop_keys,
    )
    if propagate and os.path.isdir(path):
        _write_file_dacl_propagate(path, acl, sd=sd)
        logger.info(
            "replace ACE propagate: path=%s aces=%d drop=%s inheritable=%s",
            path, len(new_aces), bool(drop_keys), inheritable,
        )
    else:
        _write_file_dacl(path, acl, sd=sd)
        logger.debug(
            "replace ACE no-propagate: path=%s aces=%d drop=%s inheritable=%s",
            path, len(new_aces), bool(drop_keys), inheritable,
        )


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
) -> list[str]:
    """给桌面 dataDir (claw-desktop) 授可继承读写 ACE, 不扫子树.

    只改名单上的目录自身, ACE 带 (OI)(CI). 新文件继承; 已有文件不逐个更新.
    preserve_write_roots 给可写根预授 WRITE_DAC, 并给到 dataDir 的祖先补 RW.
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

    def _acl_replace_our_allow(existing_dacl, inherit_flags: int):
        """去掉本 SID 的旧 Allow/Deny (含上次只读或 logs Deny), 再写上读写 Allow."""
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
            sd.GetSecurityDescriptorDacl(), const.RECURSIVE_ACE_FLAGS,
        )
        _write_file_dacl(str(path), acl, protect=True, sd=sd)

    def _grant_bridge_ancestors() -> None:
        """preserve 子树祖先 (claw-desktop / agent) 补打可继承读写 ACE. 只向上走."""
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
            logger.warning("desktop dataDir 根目录读写授权失败 path=%s: %s", root_path, exc)
        _grant_bridge_ancestors()
        _grant_preserved_write_dac()
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
            except Exception as exc:  # noqa: BLE001
                logger.debug("desktop preserve RW grant 失败 path=%s: %s", preserved, exc)
        for rel in (
            "logs",
            os.path.join("jiuwenswarm", "logs"),
            os.path.join("jiuwenswarm", "agent", ".logs"),
            os.path.join("jiuwenswarm", "config", "runtime_state"),
            os.path.join("jiuwenswarm", "agent", "skills"),
        ):
            target = root_path / rel
            if not target.exists():
                continue
            try:
                _grant_here(target)
                applied.append(str(target))
            except Exception as exc:  # noqa: BLE001
                logger.debug("desktop data RW grant 失败 path=%s: %s", target, exc)
    except Exception as exc:  # noqa: BLE001
        logger.warning("apply_desktop_data_rw 失败 root=%s: %s", root_path, exc)
        return applied

    logger.info(
        "apply_desktop_data_rw 完成: root=%s elapsed=%.2fs paths=%d",
        root_path, time.perf_counter() - t0, len(applied),
    )
    return applied



def _ace_applies_to_object(ace_flags: int) -> bool:
    """继承-only ACE (INHERIT_ONLY) 不作用于本对象本身."""
    return (int(ace_flags) & const.INHERIT_ONLY_ACE) == 0


def effective_grant(
    path: str,
    mask: int,
    allow_sids: "set[str] | frozenset[str]",
    deny_only_sids: "set[str] | frozenset[str] | None" = None,
) -> bool:
    """只读根节点 DACL, 判断沙箱有效 SID 集合是否已被授予 ``mask``.

    Deny 优先: 任一匹配 SID (含 deny-only) 的 Deny 位挡住对应权限;
    Allow 仅统计 ``allow_sids`` (deny-only 组匹配不了 Allow ACE).
    """
    _require_windows()
    if not path or not os.path.exists(path) or not mask:
        return False
    if not allow_sids and not deny_only_sids:
        return False
    deny_only = deny_only_sids or frozenset()
    match_sids = set(allow_sids) | set(deny_only)
    try:
        win32security, _, _ = _ensure_pywin32()
        sd = win32security.GetFileSecurity(
            path, win32security.DACL_SECURITY_INFORMATION,
        )
        dacl = sd.GetSecurityDescriptorDacl()
    except Exception:  # noqa: BLE001
        return False
    if dacl is None:
        return False

    denied = 0
    granted = 0
    for i in range(dacl.GetAceCount()):
        ace_type, ace_flags, ace_mask, ace_sid = _parse_getace_tuple(dacl.GetAce(i))
        if not _ace_applies_to_object(ace_flags):
            continue
        sid_key = _sid_dedup_key(ace_sid)
        if sid_key not in match_sids:
            continue
        if ace_type == const.ACCESS_DENIED_ACE_TYPE:
            denied |= int(ace_mask)
        elif sid_key in allow_sids:
            granted |= int(ace_mask)

    needed = int(mask)
    if (denied & needed) != 0:
        return False
    return (granted & needed) == needed


def _root_has_our_inheritable_ace(
    path: str,
    rights: int,
    mode: Literal["ALLOW", "DENY"],
    our_sid_keys: "set[str]",
) -> bool:
    """根上是否已有沙箱 SID 的等价 (OI)(CI) ACE (不证明已传播到子树)."""
    if not our_sid_keys or not os.path.exists(path):
        return False
    want_type = (
        const.ACCESS_ALLOWED_ACE_TYPE if mode == "ALLOW"
        else const.ACCESS_DENIED_ACE_TYPE
    )
    want_flags = const.RECURSIVE_ACE_FLAGS
    try:
        win32security, _, _ = _ensure_pywin32()
        sd = win32security.GetFileSecurity(
            path, win32security.DACL_SECURITY_INFORMATION,
        )
        dacl = sd.GetSecurityDescriptorDacl()
    except Exception:  # noqa: BLE001
        return False
    if dacl is None:
        return False
    for i in range(dacl.GetAceCount()):
        ace_type, ace_flags, ace_mask, ace_sid = _parse_getace_tuple(dacl.GetAce(i))
        if ace_type != want_type:
            continue
        if _sid_dedup_key(ace_sid) not in our_sid_keys:
            continue
        if (int(ace_flags) & want_flags) != want_flags:
            continue
        if int(ace_flags) & const.INHERITED_ACE:
            continue
        if (int(ace_mask) & int(rights)) == int(rights):
            return True
    return False


def _fingerprint_key(path: str, kind: str) -> str:
    return f"{path.replace(chr(92), '/').rstrip('/').lower()}|{kind}"


def _dir_is_effectively_empty(path: str) -> bool:
    """刚创建或空目录: 无已有子对象, 不必 SetNamedSecurityInfo 传播."""
    if not os.path.isdir(path):
        return False
    try:
        with os.scandir(path) as it:
            return next(it, None) is None
    except OSError:
        return False


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
    fingerprint_cache: "dict[str, dict] | None" = None,
) -> ApplyAclResult:
    """对沙箱工作区施加文件 ACL.

    写白名单 allow_write: 目录用 SetNamedSecurityInfo 把写 ACE 传到已有子树.
    deny_write / deny_read: 同样传播 (allow_write 已把可写 ACE 灌进子树后,
    deny_write 必须传播才能盖住 .git 等已有文件). allow_read 仍只改节点.

    ``fingerprint_cache``: 上次传播成功的指纹 (key=path|kind), 与
    effective_grant / 根 ACE 双命中时可跳过整树传播.

    Returns: ApplyAclResult (paths / grants / failed).
    """
    _require_windows()
    sid = get_synthetic_write_sid()
    allow_read = list(allow_read) if allow_read else []
    deny_read = list(deny_read) if deny_read else []
    fp_cache = fingerprint_cache or {}

    result = ApplyAclResult()
    applied: list[str] = []
    _purged: set[str] = set()

    purge_sids = [_resolve_sid(sid)]
    if sandbox_user_sid:
        purge_sids.append(_resolve_sid(sandbox_user_sid))
    our_sid_keys = {_sid_dedup_key(s) for s in purge_sids}

    allow_sids: set[str] = set(our_sid_keys)
    deny_only_sids: set[str] = set()
    try:
        from jiuwenbox.supervisor import win_setup as _win_setup
        groups = _win_setup.get_sandbox_token_groups()
        allow_sids |= set(groups.allow_sids)
        deny_only_sids |= set(groups.deny_only_sids)
    except Exception:  # noqa: BLE001
        logger.debug(
            "取沙箱 TOKEN_GROUPS 失败, 跳过判定退化为只看沙箱 SID",
            exc_info=True,
        )

    seg_stats: dict[str, list[float]] = {}

    def _purge_before_grant(path: str) -> None:
        if path in _purged:
            return
        _purged.add(path)
        try:
            purge_sid_aces(path, purge_sids)
        except Exception as exc:  # noqa: BLE001
            logger.debug("purge 旧 ACE 失败 path=%s: %s", path, exc)

    def _grant_our_aces(
        path: str,
        rights: int,
        mode: Literal["ALLOW", "DENY"],
        *,
        ace_recursive: bool | None = None,
        propagate: bool = False,
    ) -> None:
        rec = recursive if ace_recursive is None else ace_recursive
        aces: list[tuple[object, int, Literal["ALLOW", "DENY"]]] = [
            (sid, rights, mode),
        ]
        if sandbox_user_sid:
            aces.append((sandbox_user_sid, rights, mode))
        first = path not in _purged
        if first:
            _purged.add(path)
        replace_aces_no_propagate(
            path, aces,
            drop_sids=purge_sids if first else None,
            inheritable=rec,
            propagate=propagate,
        )

    def _seg_begin() -> float:
        return time.perf_counter()

    def _seg_end(seg: str, t0: float, n: int) -> None:
        seg_stats[seg] = [time.perf_counter() - t0, n]

    def _should_skip_propagate(
        path: str,
        kind: str,
        rights: int,
        mode: Literal["ALLOW", "DENY"],
    ) -> bool:
        if mode == "DENY":
            return False
        entry = fp_cache.get(_fingerprint_key(path, kind))
        if not entry or not entry.get("propagated"):
            return False
        if int(entry.get("mask", 0)) != int(rights):
            return False
        if not _root_has_our_inheritable_ace(path, rights, mode, our_sid_keys):
            return False
        if kind in ("allow_write", "allow_read"):
            return effective_grant(path, rights, allow_sids, deny_only_sids)
        return False

    def _note_grant(
        path: str,
        kind: str,
        rights: int,
        *,
        propagated: bool,
        skipped: bool = False,
    ) -> None:
        # flags 记实际写下的 ACE 继承标志 (由 recursive 决定), 与是否传播无关.
        flags = const.RECURSIVE_ACE_FLAGS if recursive else 0
        result.grants.append(AclGrantRecord(
            path=path, kind=kind, mask=int(rights), flags=flags,
            propagated=propagated, skipped=skipped,
        ))

    write_targets = list(allow_write) or [workspace]
    _t_seg = _seg_begin()
    _n_seg = 0
    for path in write_targets:
        expanded = os.path.expandvars(path)
        created_empty = False
        if not os.path.exists(expanded):
            try:
                os.makedirs(expanded, exist_ok=True)
                created_empty = True
                logger.info("allow_write 路径不存在, 已预创建: %s", expanded)
            except OSError as exc:
                logger.warning(
                    "allow_write 路径不存在且预创建失败, 跳过 ACL: %s (%s)",
                    expanded, exc,
                )
                result.failed.append(expanded)
                continue
        write_rights = const.ALLOW_WRITE_RIGHTS | const.FILE_GENERIC_READ
        if _should_skip_propagate(expanded, "allow_write", write_rights, "ALLOW"):
            logger.info("allow_write 指纹命中, 跳过传播: %s", expanded)
            # 标记已处理: 同一路径若又出现在 deny_write, 其 first-purge 会
            # 把这里保留下来的 Allow ACE 一起清掉.
            _purged.add(expanded)
            _note_grant(
                expanded, "allow_write", write_rights,
                propagated=True, skipped=True,
            )
            applied.append(expanded)
            _n_seg += 1
            continue
        do_propagate = not (created_empty or _dir_is_effectively_empty(expanded))
        try:
            if do_propagate:
                _warn_if_heavy_propagate(expanded, "allow_write")
            _grant_our_aces(
                expanded, write_rights, "ALLOW", propagate=do_propagate,
            )
        except Exception as exc:
            logger.warning(
                "allow_write grant_ace 失败, 跳过该路径 (隔离降级): path=%s mode=ALLOW "
                "原因=%s; 沙箱内该路径写权限不受控",
                expanded, exc,
            )
            result.failed.append(expanded)
            continue
        try:
            cur_key = _sid_dedup_key(_current_process_user_sid())
            if not effective_grant(
                expanded,
                const.WRITE_DAC | const.READ_CONTROL,
                {cur_key},
                None,
            ):
                grant_current_user_write_dac(expanded, inheritable=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "allow_write WRITE_DAC 预授失败 path=%s: %s", expanded, exc,
            )
        _note_grant(expanded, "allow_write", write_rights, propagated=do_propagate)
        applied.append(expanded)
        _n_seg += 1
    _seg_end("allow_write", _t_seg, _n_seg)

    _t_seg = _seg_begin()
    _n_seg = 0
    for path in deny_write:
        expanded = os.path.expandvars(path)
        created_empty = False
        if not os.path.exists(expanded):
            try:
                os.makedirs(expanded, exist_ok=True)
                created_empty = True
                logger.info("deny_write 路径不存在, 已预创建: %s", expanded)
            except OSError as exc:
                logger.warning(
                    "deny_write 路径不存在且预创建失败, 跳过 ACL: %s (%s)",
                    expanded, exc,
                )
                result.failed.append(expanded)
                continue
        do_propagate = not (created_empty or _dir_is_effectively_empty(expanded))
        try:
            if do_propagate:
                _warn_if_heavy_propagate(expanded, "deny_write")
            _grant_our_aces(
                expanded, const.DENY_WRITE_RIGHTS, "DENY",
                propagate=do_propagate,
            )
        except Exception as exc:
            logger.error(
                "deny_write grant_ace 失败, 跳过该路径 (隔离降级): path=%s mode=DENY "
                "原因=%s; 沙箱内该路径写未被拦截", expanded, exc,
            )
            result.failed.append(expanded)
            continue
        _note_grant(
            expanded, "deny_write", const.DENY_WRITE_RIGHTS,
            propagated=do_propagate,
        )
        applied.append(expanded)
        _n_seg += 1
    _seg_end("deny_write", _t_seg, _n_seg)

    _t_seg = _seg_begin()
    _n_seg = 0
    for path in deny_read:
        expanded = os.path.expandvars(path)
        created_empty = False
        if not os.path.exists(expanded):
            try:
                os.makedirs(expanded, exist_ok=True)
                created_empty = True
                logger.info("deny_read 路径不存在, 已预创建: %s", expanded)
            except OSError as exc:
                logger.warning(
                    "deny_read 路径不存在且预创建失败, 跳过 ACL: %s (%s)",
                    expanded, exc,
                )
                result.failed.append(expanded)
                continue
        do_propagate = not (created_empty or _dir_is_effectively_empty(expanded))
        try:
            if do_propagate:
                _warn_if_heavy_propagate(expanded, "deny_read")
            _grant_our_aces(
                expanded, const.DENY_READ_RIGHTS, "DENY",
                propagate=do_propagate,
            )
        except Exception as exc:
            logger.error(
                "deny_read grant_ace 失败, 跳过该路径 (隔离降级): path=%s mode=DENY "
                "原因=%s; 沙箱内该路径读未被拦截", expanded, exc,
            )
            result.failed.append(expanded)
            continue
        _note_grant(
            expanded, "deny_read", const.DENY_READ_RIGHTS,
            propagated=do_propagate,
        )
        applied.append(expanded)
        _n_seg += 1
    _seg_end("deny_read", _t_seg, _n_seg)

    _preinstalled = preinstalled_read_paths or set()

    def _is_preinstalled(p: str) -> bool:
        return p.rstrip("\\/").lower() in _preinstalled

    read_targets = allow_read
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
                result.failed.append(expanded)
                continue
        if _is_preinstalled(expanded):
            logger.debug(
                "allow_read 路径已由 install 预装读 ACL, 跳过运行时 grant: %s",
                expanded,
            )
            applied.append(expanded)
            _n_seg += 1
            continue
        if effective_grant(
            expanded, const.FILE_GENERIC_READ, allow_sids, deny_only_sids,
        ):
            logger.info("allow_read 已有有效读权限, 跳过 grant: %s", expanded)
            _purged.add(expanded)
            _note_grant(
                expanded, "allow_read", const.FILE_GENERIC_READ,
                propagated=False, skipped=True,
            )
            applied.append(expanded)
            _n_seg += 1
            continue
        try:
            _grant_our_aces(expanded, const.FILE_GENERIC_READ, "ALLOW")
        except Exception as exc:
            logger.warning(
                "allow_read grant_ace 失败, 跳过该路径 (隔离降级): path=%s mode=ALLOW "
                "原因=%s; 沙箱内该路径读可能受限",
                expanded, exc,
            )
            result.failed.append(expanded)
            applied.append(expanded)
            continue
        _note_grant(
            expanded, "allow_read", const.FILE_GENERIC_READ, propagated=False,
        )
        applied.append(expanded)
        _n_seg += 1
    _seg_end("allow_read", _t_seg, _n_seg)

    try:
        _office = str(OFFICE_CLAW_DATA_ROOT) if OFFICE_CLAW_DATA_ROOT else ""
        if _office and os.path.isdir(_office):
            _purge_before_grant(_office)
            logger.info("已去掉 ~/.office-claw 特殊 ACL: %s", _office)
    except Exception as exc:  # noqa: BLE001
        logger.warning("去掉 ~/.office-claw 特殊 ACL 失败 (非致命): %s", exc)

    _traverse_roots: list[Path] = []
    for _root in (JIUWENCLAW_DATA_DIR_PATH, JIUWENBOX_HOME):
        if _root and _root not in _traverse_roots and os.path.isdir(str(_root)):
            _traverse_roots.append(_root)
    _t_seg = _seg_begin()
    _n_seg = 0
    for _root in _traverse_roots:
        try:
            _grant_our_aces(
                str(_root), const.FILE_GENERIC_READ, "ALLOW", ace_recursive=False,
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

    _seg_lines = " ".join(
        f"{seg}={v[1]}paths/{v[0]:.2f}s"
        for seg, v in seg_stats.items() if v[1] or v[0] > 0.01
    )
    logger.info(
        "施加沙箱 ACL 完成: workspace=%s allow_write=%d deny_write=%d "
        "allow_read=%d deny_read=%d failed=%d %s",
        workspace, len(write_targets), len(deny_write),
        len(read_targets), len(deny_read), len(result.failed), _seg_lines,
    )
    seen: set[str] = set()
    unique: list[str] = []
    for p in applied:
        if p not in seen:
            seen.add(p)
            unique.append(p)
    result.paths = unique
    return result



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
    sid_str = get_synthetic_write_sid()
    target_sids = [_resolve_sid(sid_str)]
    if sandbox_user_sid:
        target_sids.append(_resolve_sid(sandbox_user_sid))

    if isinstance(paths, str):
        root_list = [paths]
    else:
        root_list = list(paths)

    # 对根路径 purge 并用 SetNamedSecurityInfo 传播到已有子树.
    # 传播式 grant 之后, 子对象上已有 (I) 副本; 只 SetFileSecurity 改根
    # 不会让这些副本消失, 必须同样传播撤销.
    cleaned = 0
    for root_path in root_list:
        p = os.path.expandvars(root_path)
        if not os.path.exists(p):
            continue
        try:
            removed = purge_sid_aces(p, target_sids, propagate=True)
            if removed > 0:
                cleaned += 1
                logger.debug("revoke: 清理 %s 上 %d 个 ACE (propagated)", p, removed)
        except Exception:  # noqa: BLE001 - ACL 清理是 best-effort
            logger.debug("revoke 单个路径失败: %s", p, exc_info=True)
    logger.info("撤销沙箱 ACL 完成: 清理根路径数=%d", cleaned)
