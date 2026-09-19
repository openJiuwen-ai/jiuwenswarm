# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Windows 文件系统 ACL 控制 (合成 SID + NTFS DACL).

读写不对称:
  - 读: 黑名单. Users/Everyone 默认 RX 不必再授 Allow; deny_read 在名单
    目录上打 Deny Read, 并用 SetNamedSecurityInfo 传播到已有子树.
  - 写: 白名单. allow_write 打 Allow Write 并传播到已有子树;
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

import ctypes
import logging
import os
import queue
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from jiuwenbox.logging_config import configure_logging
from jiuwenbox.supervisor import win_constants as const
from jiuwenbox.server.workspace import (
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


def _parent_traverse_plan() -> dict[str, object]:
    """祖先目录的 traverse ACE: 只追加/替换沙箱 SID, 不删 Users / 宿主条目."""
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
    """安装/重装时只补祖先 traverse. 数据读写来自 policy allow_write / extra.paths.
    """
    _require_windows()
    sid = (sandbox_user_sid or "").strip()
    try:
        profile = str(Path.home().resolve())
    except OSError:
        profile = (os.environ.get("USERPROFILE") or "").strip()
    if not profile or not os.path.isdir(profile):
        logger.warning("reconcile_install_acl 找不到 USERPROFILE")
        return
    if sid:
        grant_parent_traverse(profile, sid)
    group_sid = None
    try:
        from jiuwenbox.supervisor import win_setup
        group_sid = win_setup.get_sandbox_group_sid()
    except Exception:  # noqa: BLE001
        logger.debug("reconcile_install_acl 取组 SID 失败", exc_info=True)
    if group_sid:
        grant_parent_traverse(profile, group_sid)
    logger.info("reconcile_install_acl 完成 profile=%s (traverse only)", profile)


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


_advapi32_acl: ctypes.WinDLL | None = None
_kernel32_acl: ctypes.WinDLL | None = None


def _get_acl_advapi32() -> ctypes.WinDLL:
    global _advapi32_acl
    if _advapi32_acl is None:
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(wintypes.DWORD),
        ]
        advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = (
            wintypes.BOOL
        )
        advapi32.GetSecurityDescriptorDacl.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(wintypes.BOOL),
            ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.BOOL),
        ]
        advapi32.GetSecurityDescriptorDacl.restype = wintypes.BOOL
        # SetNamedSecurityInfoW: 长 syscall, ctypes 会释放 GIL, 避免扫
        # AppData\\Local 时冻住 box-server asyncio.
        advapi32.SetNamedSecurityInfoW.argtypes = [
            wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD,
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
        ]
        advapi32.SetNamedSecurityInfoW.restype = wintypes.DWORD
        _advapi32_acl = advapi32
    return _advapi32_acl


def _get_acl_kernel32() -> ctypes.WinDLL:
    global _kernel32_acl
    if _kernel32_acl is None:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        _kernel32_acl = kernel32
    return _kernel32_acl


def _set_named_dacl_nogil(path: str, info: int, acl) -> None:
    """SetNamedSecurityInfoW via ctypes so the tree-walk syscall drops the GIL.

    pywin32 ``SetNamedSecurityInfo`` holds the GIL for the whole kernel call.
    Walking ``AppData\\Local`` then freezes box-server HTTP for ~100s, which
    shows up as SKILL.md ``read_file`` timeouts and stacked sandbox creates.
    """
    win32security, _, _ = _ensure_pywin32()
    tmp = win32security.SECURITY_DESCRIPTOR()
    tmp.Initialize()
    tmp.SetSecurityDescriptorDacl(1, acl, 0)
    sddl = win32security.ConvertSecurityDescriptorToStringSecurityDescriptor(
        tmp,
        win32security.SDDL_REVISION_1,
        win32security.DACL_SECURITY_INFORMATION,
    )
    advapi32 = _get_acl_advapi32()
    psd = ctypes.c_void_p()
    if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        sddl, 1, ctypes.byref(psd), None,
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        present = wintypes.BOOL()
        defaulted = wintypes.BOOL()
        pacl = ctypes.c_void_p()
        if not advapi32.GetSecurityDescriptorDacl(
            psd, ctypes.byref(present), ctypes.byref(pacl), ctypes.byref(defaulted),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        err = advapi32.SetNamedSecurityInfoW(
            path,
            const.SE_FILE_OBJECT,
            int(info),
            None,
            None,
            pacl if present.value else None,
            None,
        )
        if err:
            raise ctypes.WinError(err)
    finally:
        if psd.value:
            _get_acl_kernel32().LocalFree(psd)


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
    try:
        _set_named_dacl_nogil(path, int(info), acl)
    except Exception:  # noqa: BLE001
        # ctypes 路径失败时回退 pywin32 (会持有 GIL).
        logger.debug(
            "SetNamedSecurityInfo ctypes path failed, falling back to pywin32",
            exc_info=True,
        )
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


# 整树 SetNamedSecurityInfo 放到后台: 根节点 ACE 先同步写上, 子树传播不挡
# 沙箱 create/exec. 超大目录拆成一层子树并行传播, 避免单次扫 AppData 数分钟.
ACL_SPLIT_ENTRIES = 5_000
ACL_SPLIT_DIRECT_DIRS = 32
ACL_SPLIT_MAX_DEPTH = 2
ACL_PROPAGATE_WORKERS = 4
ACL_ACCESS_WAIT_SECONDS = 30.0

_propagate_jobs: queue.Queue[str] = queue.Queue()
_propagate_lock = threading.Lock()
_propagate_cv = threading.Condition(_propagate_lock)
_propagate_queued: set[str] = set()
_propagate_done: set[str] = set()
_propagate_depth: dict[str, int] = {}
_propagate_pool: ThreadPoolExecutor | None = None
_propagate_dispatcher: threading.Thread | None = None
_split_plan_cache: dict[str, tuple[float, str]] = {}
_split_plan_lock = threading.Lock()


def _propagate_path_key(path: str) -> str:
    try:
        return _path_key(path)
    except Exception:  # noqa: BLE001
        return os.path.normcase(os.path.abspath(path))


def _ensure_propagate_worker() -> None:
    global _propagate_pool, _propagate_dispatcher
    with _propagate_lock:
        if _propagate_pool is None:
            _propagate_pool = ThreadPoolExecutor(
                max_workers=ACL_PROPAGATE_WORKERS,
                thread_name_prefix="jiuwenbox-acl-propagate",
            )
        dispatcher = _propagate_dispatcher
        if dispatcher is not None and dispatcher.is_alive():
            return
        thread = threading.Thread(
            target=_propagate_dispatcher_main,
            name="jiuwenbox-acl-dispatch",
            daemon=True,
        )
        _propagate_dispatcher = thread
        thread.start()


def schedule_tree_propagate(
    path: str, *, force: bool = False, depth: int = 0,
) -> None:
    """Enqueue SetNamedSecurityInfo walk. Dedup in-flight / already-done paths.

    ``force=True``: 根 DACL 刚被改写, 即使本进程已走过该树也要再入队.
    失败的任务不进 ``_propagate_done``, 下次 apply 可以重试.
    ``depth``: 拆分层数, 超过 ``ACL_SPLIT_MAX_DEPTH`` 后不再拆, 整树一次传播.
    """
    if not path or not os.path.isdir(path):
        return
    key = _propagate_path_key(path)
    with _propagate_cv:
        if key in _propagate_queued:
            return
        if not force and key in _propagate_done:
            return
        _propagate_queued.add(key)
        _propagate_depth[key] = max(0, int(depth))
        if force:
            _propagate_done.discard(key)
    _ensure_propagate_worker()
    _propagate_jobs.put(path)
    logger.info(
        "queued background DACL propagate path=%s force=%s depth=%s",
        path, force, depth,
    )


def _propagate_dispatcher_main() -> None:
    while True:
        path = _propagate_jobs.get()
        pool = _propagate_pool
        if pool is None:
            _propagate_job(path)
            continue
        pool.submit(_propagate_job, path)


def _propagate_job(path: str) -> None:
    key = _propagate_path_key(path)
    with _propagate_lock:
        depth = _propagate_depth.get(key, 0)
    ok = False
    try:
        _run_tree_propagate(path, depth=depth)
        ok = True
    except Exception:  # noqa: BLE001
        logger.warning(
            "background DACL propagate failed path=%s", path, exc_info=True,
        )
    finally:
        with _propagate_cv:
            _propagate_queued.discard(key)
            _propagate_depth.pop(key, None)
            if ok:
                _propagate_done.add(key)
            _propagate_cv.notify_all()
        try:
            _propagate_jobs.task_done()
        except ValueError:
            pass


def _probe_dir_weight(path: str) -> tuple[int, int, int, bool]:
    """有界统计: (直接文件数, 直接子目录数, 已见条目, 是否撞到拆分阈值)."""
    direct_files = 0
    direct_dirs = 0
    try:
        with os.scandir(path) as it:
            for entry in it:
                try:
                    if entry.is_symlink() or (
                        hasattr(entry, "is_junction") and entry.is_junction()
                    ):
                        continue
                    is_dir = entry.is_dir(follow_symlinks=False)
                except OSError:
                    continue
                if is_dir:
                    direct_dirs += 1
                else:
                    direct_files += 1
                if direct_dirs >= ACL_SPLIT_DIRECT_DIRS:
                    return direct_files, direct_dirs, direct_files + direct_dirs, True
    except OSError:
        return 0, 0, 0, False
    n = direct_files + direct_dirs
    if n > ACL_SPLIT_ENTRIES:
        return direct_files, direct_dirs, n, True
    try:
        for root, dirs, files in os.walk(path, followlinks=False):
            if os.path.normcase(root) == os.path.normcase(path):
                continue
            n += len(dirs) + len(files)
            if n > ACL_SPLIT_ENTRIES:
                return direct_files, direct_dirs, n, True
    except OSError:
        pass
    return direct_files, direct_dirs, n, False


def _dir_split_plan(path: str, *, depth: int = 0) -> str:
    """keep | propagate | split. 结果短 TTL 缓存."""
    if not os.path.isdir(path):
        return "keep"
    if depth >= ACL_SPLIT_MAX_DEPTH:
        return "propagate"
    try:
        cache_key = f"{_path_key(path)}:{depth}"
    except Exception:  # noqa: BLE001
        cache_key = f"{path}:{depth}"
    now = time.monotonic()
    with _split_plan_lock:
        hit = _split_plan_cache.get(cache_key)
        if hit is not None and now - hit[0] < 300.0:
            return hit[1]
    direct_files, direct_dirs, entries, hit_limit = _probe_dir_weight(path)
    if direct_dirs >= ACL_SPLIT_DIRECT_DIRS:
        plan = "split"
    elif hit_limit or entries > ACL_SPLIT_ENTRIES:
        plan = "split" if direct_dirs >= 2 else "propagate"
    else:
        plan = "propagate"
    with _split_plan_lock:
        _split_plan_cache[cache_key] = (now, plan)
    return plan


def _collect_explicit_inheritable_aces(dacl) -> list[tuple[int, int, int, object]]:
    """父节点上显式 (OI)/(CI) ACE, 不含已继承副本."""
    out: list[tuple[int, int, int, object]] = []
    if dacl is None:
        return out
    inherit = const.OBJECT_INHERIT_ACE | const.CONTAINER_INHERIT_ACE
    for i in range(dacl.GetAceCount()):
        ace_type, ace_flags, ace_mask, ace_sid = _parse_getace_tuple(dacl.GetAce(i))
        if int(ace_flags) & const.INHERITED_ACE:
            continue
        if (int(ace_flags) & inherit) == 0:
            continue
        out.append((int(ace_type), int(ace_flags), int(ace_mask), ace_sid))
    return out


def _push_inheritable_aces_to_child(
    child: str,
    aces: list[tuple[int, int, int, object]],
    *,
    directory: bool,
) -> None:
    if not aces:
        return
    packed: list[tuple[object, int, Literal["ALLOW", "DENY"]]] = []
    for ace_type, _flags, mask, sid in aces:
        mode: Literal["ALLOW", "DENY"] = (
            "ALLOW" if ace_type == const.ACCESS_ALLOWED_ACE_TYPE else "DENY"
        )
        packed.append((sid, int(mask), mode))
    replace_aces_no_propagate(
        child, packed, inheritable=directory, propagate=False,
    )


def _split_and_enqueue_children(path: str, *, depth: int) -> None:
    """把本层文件打上 ACE, 子目录 merge 后并行入队."""
    win32security, _, _ = _ensure_pywin32()
    sd = win32security.GetFileSecurity(
        path, win32security.DACL_SECURITY_INFORMATION,
    )
    parent_acl = sd.GetSecurityDescriptorDacl()
    aces = _collect_explicit_inheritable_aces(parent_acl)
    if not aces:
        _write_file_dacl_propagate(path, parent_acl, sd=sd)
        return
    try:
        with os.scandir(path) as it:
            entries = list(it)
    except OSError as exc:
        logger.info("split scandir failed path=%s: %s", path, exc)
        _write_file_dacl_propagate(path, parent_acl, sd=sd)
        return
    child_dirs = 0
    for entry in entries:
        try:
            if entry.is_symlink() or (
                hasattr(entry, "is_junction") and entry.is_junction()
            ):
                continue
            if entry.is_file(follow_symlinks=False):
                _push_inheritable_aces_to_child(
                    entry.path, aces, directory=False,
                )
            elif entry.is_dir(follow_symlinks=False):
                _push_inheritable_aces_to_child(
                    entry.path, aces, directory=True,
                )
                schedule_tree_propagate(
                    entry.path, force=True, depth=depth + 1,
                )
                child_dirs += 1
        except OSError:
            continue
    if child_dirs == 0:
        _write_file_dacl_propagate(path, parent_acl, sd=sd)
        return
    logger.info(
        "split DACL propagate path=%s children=%d depth=%s",
        path, child_dirs, depth,
    )


def _run_tree_propagate(path: str, *, depth: int = 0) -> None:
    if not os.path.isdir(path):
        return
    plan = _dir_split_plan(path, depth=depth)
    if plan == "split":
        _split_and_enqueue_children(path, depth=depth)
        return
    win32security, _, _ = _ensure_pywin32()
    sd = win32security.GetFileSecurity(
        path, win32security.DACL_SECURITY_INFORMATION,
    )
    acl = sd.GetSecurityDescriptorDacl()
    if acl is None:
        return
    _write_file_dacl_propagate(path, acl, sd=sd)


def _is_propagate_inflight_for(path: str) -> bool:
    """自身、祖先、或已拆出的子孙 job 仍在队列里."""
    if not path:
        return False
    key = _propagate_path_key(path)
    with _propagate_lock:
        queued = set(_propagate_queued)
    if key in queued:
        return True
    for queued_key in queued:
        if _is_under_or_equal(key, queued_key) or _is_under_or_equal(queued_key, key):
            return True
    return False


def _write_cap_sid_for_path(path: str) -> str | None:
    """Nearest ancestor write-root cap SID. Never allocate a nested SID.

    Restricted tokens only carry extra/workspace root cap SIDs. An ACE for a
    newly minted leaf SID would not satisfy WRITE_RESTRICTED.
    """
    from jiuwenbox.supervisor import win_setup as _ws

    cur = path
    for _ in range(64):
        try:
            cap = _ws.get_write_cap_sid_if_exists(cur)
        except Exception:  # noqa: BLE001
            cap = None
        if cap:
            return cap
        parent = os.path.dirname(cur)
        if not parent or parent == cur:
            break
        cur = parent
    return None


def _write_trustees_for_access_path(path: str) -> list[str]:
    from jiuwenbox.supervisor import win_setup as _ws

    sids: list[str] = []
    cap = _write_cap_sid_for_path(path)
    if cap:
        sids.append(cap)
    try:
        group = _ws.get_sandbox_group_sid()
        if group:
            sids.append(group)
    except Exception:  # noqa: BLE001
        pass
    return sids


def _sid_has_object_write_ace(path: str, sid: str) -> bool:
    """``sid`` itself has object-applicable write ACE (not OR'd with other SIDs)."""
    if not path or not sid:
        return False
    try:
        key = _sid_dedup_key(_resolve_sid(sid))
    except Exception:  # noqa: BLE001
        return False
    return effective_grant(
        path, const.ALLOW_WRITE_RIGHTS | const.FILE_GENERIC_READ, {key},
    )


def _target_has_write_ace(path: str) -> bool:
    """WRITE_RESTRICTED needs cap SID (restricted half) and group (normal half)."""
    if not path or not os.path.exists(path):
        return False
    cap = _write_cap_sid_for_path(path)
    if not cap or not _sid_has_object_write_ace(path, cap):
        return False
    from jiuwenbox.supervisor import win_setup as _ws

    try:
        group = _ws.get_sandbox_group_sid()
    except Exception:  # noqa: BLE001
        group = None
    if group and not _sid_has_object_write_ace(path, group):
        return False
    return True


def _propagate_tree_now(path: str) -> None:
    """当前线程对目录做一次 SetNamedSecurityInfo, 不再只入队."""
    if not path or not os.path.isdir(path):
        return
    win32security, _, _ = _ensure_pywin32()
    sd = win32security.GetFileSecurity(
        path, win32security.DACL_SECURITY_INFORMATION,
    )
    acl = sd.GetSecurityDescriptorDacl()
    if acl is None:
        return
    parent = os.path.dirname(path)
    if parent and parent != path and not _collect_explicit_inheritable_aces(acl):
        try:
            parent_sd = win32security.GetFileSecurity(
                parent, win32security.DACL_SECURITY_INFORMATION,
            )
            parent_aces = _collect_explicit_inheritable_aces(
                parent_sd.GetSecurityDescriptorDacl(),
            )
            if parent_aces:
                _push_inheritable_aces_to_child(
                    path, parent_aces, directory=True,
                )
                sd = win32security.GetFileSecurity(
                    path, win32security.DACL_SECURITY_INFORMATION,
                )
                acl = sd.GetSecurityDescriptorDacl()
        except Exception:  # noqa: BLE001
            logger.debug("sync propagate merge from parent failed path=%s", path)
    if acl is None:
        return
    _write_file_dacl_propagate(path, acl, sd=sd)


def _access_pin_targets(path: str) -> list[str]:
    """Objects that need a local write ACE for this access (no tree walk).

    Existing file: the file plus its parent dir. Existing dir: the dir.
    Missing path: nearest existing ancestor dir so create/inherit can proceed.
    """
    if not path:
        return []
    target = os.path.expandvars(os.path.expanduser(str(path)))
    try:
        if os.path.exists(target):
            target = str(Path(target).resolve())
    except OSError:
        pass
    seen: set[str] = set()
    out: list[str] = []

    def _add(raw: str) -> None:
        if not raw or not os.path.exists(raw):
            return
        try:
            key = os.path.normcase(os.path.abspath(raw))
        except OSError:
            key = os.path.normcase(raw)
        if key in seen:
            return
        seen.add(key)
        out.append(raw)

    if os.path.exists(target):
        _add(target)
        if not os.path.isdir(target):
            _add(os.path.dirname(target))
        return out
    cur = os.path.dirname(target) or target
    for _ in range(64):
        if os.path.isdir(cur):
            _add(cur)
            break
        parent = os.path.dirname(cur)
        if not parent or parent == cur:
            break
        cur = parent
    return out


def pin_access_write_ace(path: str) -> bool:
    """Non-recursive write ACE on this object only. Tree propagate continues.

    Directories get inheritable (OI)(CI) so newly created children inherit.
    Existing children are not visited.
    """
    if not path or not os.path.exists(path) or _is_acl_forbidden_path(path):
        return False
    try:
        if _target_has_write_ace(path):
            return False
    except Exception:  # noqa: BLE001
        pass
    sids = _write_trustees_for_access_path(path)
    cap = _write_cap_sid_for_path(path)
    if not cap:
        logger.debug("pin access ACE skipped, no write-cap SID yet path=%s", path)
        return False
    rights = const.ALLOW_WRITE_RIGHTS | const.FILE_GENERIC_READ
    inheritable = os.path.isdir(path)
    aces: list[tuple[str | object, int, Literal["ALLOW", "DENY"]]] = [
        (sid, rights, "ALLOW") for sid in sids
    ]
    try:
        replace_aces_no_propagate(
            path, aces, inheritable=inheritable, propagate=False,
        )
    except Exception:  # noqa: BLE001
        logger.warning("pin access write ACE failed path=%s", path, exc_info=True)
        return False
    logger.info(
        "pinned non-recursive write ACE path=%s inheritable=%s "
        "(background tree propagate continues)",
        path, inheritable,
    )
    return True


def ensure_access_acl_ready(
    path: str, *, timeout: float = ACL_ACCESS_WAIT_SECONDS,
) -> None:
    """Pin a non-recursive write ACE on the accessed object/dir, then return.

    extra.paths 的整树传播继续在后台跑. 当前指令不等待、也不对本目录做
    同步 SetNamedSecurityInfo. ``timeout`` 保留给调用方兼容, 不再等待.
    """
    _ = timeout
    if not path:
        return
    for one in _access_pin_targets(path):
        try:
            if _target_has_write_ace(one):
                continue
        except Exception:  # noqa: BLE001
            pass
        pin_access_write_ace(one)


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
    FILE_LIST_DIRECTORY Deny, 否则穿过主目录进子目录仍会 5).
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
        _write_file_dacl(path, acl, sd=sd)
        schedule_tree_propagate(path, force=True)
    else:
        _write_file_dacl(path, acl, sd=sd)
    _t1 = time.perf_counter()
    logger.debug(
        "purge ACE: path=%s removed=%d remaining=%d t_set=%.3fs",
        path, removed, len(deny_aces) + len(allow_aces), _t1 - _t0,
    )
    return removed


def replace_aces_no_propagate(
    path: str,
    aces: list[tuple[str | object, int, Literal["ALLOW", "DENY"]]],
    *,
    drop_sids: list[str | object] | None = None,
    inheritable: bool = False,
    propagate: bool = False,
) -> None:
    """一次 Get DACL / 重建 / 写入.

    ``inheritable=True`` 给 ACE 带 (OI)(CI).
    ``propagate=True`` 且 path 为目录时: 本节点 SetFileSecurity 同步写入,
    再把传播丢到后台 (大目录会拆子树并行 SetNamedSecurityInfo).
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
        _write_file_dacl(path, acl, sd=sd)
        schedule_tree_propagate(path, force=True)
        logger.info(
            "replace ACE root-sync, tree propagate queued: path=%s aces=%d drop=%s inheritable=%s",
            path, len(new_aces), bool(drop_keys), inheritable,
        )
    else:
        _write_file_dacl(path, acl, sd=sd)
        logger.debug(
            "replace ACE no-propagate: path=%s aces=%d drop=%s inheritable=%s",
            path, len(new_aces), bool(drop_keys), inheritable,
        )


def _set_ace_no_propagate(
    path: str,
    sid: str | object,
    *,
    rights: int,
    mode: Literal["ALLOW", "DENY"],
    inheritable: bool = False,
) -> None:
    """grant_ace 单条入口: 只改本节点, 不向已有子树传播."""
    replace_aces_no_propagate(
        path, [(sid, int(rights), mode)], inheritable=inheritable,
    )


def _current_process_user_sid():
    """当前进程 TokenUser SID 对象."""
    win32security, win32con, win32api = _ensure_pywin32()
    token = win32security.OpenProcessToken(
        win32api.GetCurrentProcess(), win32con.TOKEN_QUERY,
    )
    try:
        info = win32security.GetTokenInformation(
            token, win32security.TokenUser,
        )
        return info[0] if isinstance(info, (tuple, list)) else info
    finally:
        try:
            win32api.CloseHandle(token)
        except Exception:  # noqa: BLE001
            pass


def grant_current_user_write_dac(path: str, *, inheritable: bool = True) -> None:
    """给当前用户预授 WRITE_DAC|READ_CONTROL, 运行时 grant_ace 不再 WinError 5.

    只改 ``path`` 自身 DACL, 不向已有子树传播. owner 虽有隐式 WRITE_DAC,
    显式 ACE 让受限拆分 token 场景也能改 DACL.
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

    **不改 ``path`` 自身 DACL** (从父目录起走). ``allow_write`` 已在目标
    节点打了可继承写 ACE; 若再从目标自己走, 中间目录规则会把写 ACE
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
        if key in preserve_keys and not is_profile:
            parent = current.parent
            if parent == current:
                break
            current = parent
            continue
        plan = _parent_traverse_plan()
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
) -> ApplyAclResult:
    """对沙箱工作区施加文件 ACL.

    写白名单 allow_write: 目录用 SetNamedSecurityInfo 把写 ACE 传到已有子树.
    deny_write / deny_read: 同样传播 (allow_write 已把可写 ACE 灌进子树后,
    deny_write 必须传播才能盖住 .git 等已有文件). allow_read 仍只改节点.

    幂等靠对根 DACL 做 check-then-set (不再有指纹缓存).

    Returns: ApplyAclResult (paths / grants / failed).
    """
    _require_windows()
    from jiuwenbox.supervisor import win_setup as _win_setup
    allow_read = list(allow_read) if allow_read else []
    deny_read = list(deny_read) if deny_read else []
    group_sid = _win_setup.get_sandbox_group_sid()
    identity_sids: list[str] = []
    if sandbox_user_sid:
        identity_sids.append(sandbox_user_sid)
    if group_sid:
        identity_sids.append(group_sid)

    result = ApplyAclResult()
    applied: list[str] = []
    _purged: set[str] = set()

    purge_sids = [_resolve_sid(s) for s in identity_sids]
    try:
        purge_sids.append(_resolve_sid(get_synthetic_write_sid()))
    except Exception:
        pass
    our_sid_keys = {_sid_dedup_key(s) for s in purge_sids}

    allow_sids: set[str] = set(our_sid_keys)
    deny_only_sids: set[str] = set()
    try:
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

    def _grant_sids(
        path: str,
        rights: int,
        mode: Literal["ALLOW", "DENY"],
        sids: list[str],
        *,
        ace_recursive: bool | None = None,
        propagate: bool = False,
    ) -> None:
        rec = recursive if ace_recursive is None else ace_recursive
        aces: list[tuple[object, int, Literal["ALLOW", "DENY"]]] = [
            (one, rights, mode) for one in sids if one
        ]
        if not aces:
            raise RuntimeError(f"no trustees for {mode} ACE on {path}")
        first = path not in _purged
        if first:
            _purged.add(path)
        replace_aces_no_propagate(
            path, aces,
            drop_sids=purge_sids if first else None,
            inheritable=rec,
            propagate=propagate,
        )

    def _write_trustees(path: str) -> list[str]:
        cap = _win_setup.load_or_create_write_cap_sid(path)
        sids = [cap]
        if group_sid:
            sids.append(group_sid)
        return sids

    def _grant_write_aces(
        path: str,
        rights: int,
        *,
        propagate: bool = False,
    ) -> None:
        _grant_sids(path, rights, "ALLOW", _write_trustees(path), propagate=propagate)

    def _grant_identity_aces(
        path: str,
        rights: int,
        mode: Literal["ALLOW", "DENY"],
        *,
        ace_recursive: bool | None = None,
        propagate: bool = False,
    ) -> None:
        _grant_sids(
            path, rights, mode, identity_sids,
            ace_recursive=ace_recursive, propagate=propagate,
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
        if kind == "allow_write":
            trustees = _write_trustees(path)
            if not trustees:
                return False
            # Cap SID 和组都要有 (OI)(CI) 写 ACE. 只命中 cap 时受限 token 的
            # 普通组检查仍会 ACCESS_DENIED (组上往往只有 traverse).
            for sid in trustees:
                keys = {_sid_dedup_key(_resolve_sid(sid))}
                if not _root_has_our_inheritable_ace(path, rights, mode, keys):
                    return False
            return True
        if kind == "allow_read":
            keys = {_sid_dedup_key(_resolve_sid(s)) for s in identity_sids}
            return _root_has_our_inheritable_ace(path, rights, mode, keys)
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

    write_targets = filter_write_roots(
        list(allow_write),
        deny_write=deny_write,
        drop_heavy=False,
        max_roots=None,
    )
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
            logger.info("allow_write 根 ACE 已在, 复用上次授权, 跳过传播: %s", expanded)
            # 标记已处理: 同一路径若又出现在 deny_write, 其 first-purge 会
            # 把这里保留下来的 Allow ACE 一起清掉.
            _purged.add(expanded)
            _note_grant(
                expanded, "allow_write", write_rights,
                propagated=False, skipped=True,
            )
            applied.append(expanded)
            _n_seg += 1
            continue
        do_propagate = not (created_empty or _dir_is_effectively_empty(expanded))
        try:
            if do_propagate:
                _warn_if_heavy_propagate(expanded, "allow_write")
            _grant_write_aces(
                expanded, write_rights, propagate=do_propagate,
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
            _grant_identity_aces(
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
            _grant_identity_aces(
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
            _grant_identity_aces(expanded, const.FILE_GENERIC_READ, "ALLOW")
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
    """撤销沙箱施加的所有 ACE (合成 SID + jbx-sandbox 用户/组 SID + per-root cap SID).

    Args:
        paths: apply_sandbox_acl 返回的施加路径清单 (workspace + allow/deny
            各项, 均为 grant 时的根路径). traverse_roots (数据根) 不在此清单.
        sandbox_user_sid: 真实 jbx-sandbox SID 字符串.
    """
    _require_windows()
    from jiuwenbox.supervisor import win_setup as _ws

    sid_str = get_synthetic_write_sid()
    target_sids = [_resolve_sid(sid_str)]
    if sandbox_user_sid:
        target_sids.append(_resolve_sid(sandbox_user_sid))
    # 写 ACE 的 trustee 是组 + per-root cap SID, 不是用户 SID; 不带上这两类
    # 就会把写授权留在盘上.
    try:
        group_sid = _ws.get_sandbox_group_sid()
        if group_sid:
            target_sids.append(_resolve_sid(group_sid))
    except Exception:  # noqa: BLE001
        logger.debug("revoke: 解析沙箱组 SID 失败", exc_info=True)

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
        per_root = list(target_sids)
        try:
            cap = _ws.get_write_cap_sid_if_exists(p)
            if cap:
                per_root.append(_resolve_sid(cap))
        except Exception:  # noqa: BLE001
            logger.debug("revoke: 解析 cap SID 失败 path=%s", p, exc_info=True)
        try:
            removed = purge_sid_aces(p, per_root, propagate=True)
            if removed > 0:
                cleaned += 1
                logger.debug("revoke: 清理 %s 上 %d 个 ACE (propagated)", p, removed)
        except Exception:  # noqa: BLE001 - ACL 清理是 best-effort
            logger.debug("revoke 单个路径失败: %s", p, exc_info=True)
    logger.info("撤销沙箱 ACL 完成: 清理根路径数=%d", cleaned)


MAX_WRITE_ROOTS_PER_CALL = 64
MAX_TOTAL_WRITE_CAPS = 512
HEAVY_PROPAGATE_ENTRIES = 20_000
# 巨型目录不再准入丢弃, 只作为拆分/警告阈值. 拆分常量见 ACL_SPLIT_*.
# 同一进程内重复判定同一根是否「巨型」的缓存 TTL. filter_write_roots 每次写调用
# 都跑, 没有缓存就是每次 exec 一轮全树 os.walk.
_HEAVY_CACHE_TTL_S = 300.0
_heavy_cache: dict[str, tuple[float, bool]] = {}
_heavy_cache_lock = threading.Lock()

_reconcile_lock = threading.Lock()
# key → (event, result_holder). result_holder[0] 是 owner 跑完后的失败根清单,
# 让搭便车的 waiter 也能拿到同样的失败结果而不是静默成功.
_reconcile_inflight: dict[frozenset[str], tuple[threading.Event, list]] = {}


class WriteRootDenied(ValueError):
    """A candidate write root failed the Codex-aligned filter chain."""


class WriteAceFailed(RuntimeError):
    """写 ACE 施加失败. 调用方必须 fail closed (HTTP 500), 不能当没事发生."""


def _sensitive_write_roots() -> list[str]:
    """Hard-deny write roots: JIUWENBOX_HOME and acl_state dir."""
    roots: list[str] = []
    try:
        from jiuwenbox.server.workspace import JIUWENBOX_HOME

        if JIUWENBOX_HOME:
            roots.append(str(JIUWENBOX_HOME))
    except Exception:  # noqa: BLE001
        pass
    try:
        from jiuwenbox.supervisor import win_setup as _ws

        roots.append(str(_ws._acl_home()))  # noqa: SLF001
    except Exception:  # noqa: BLE001
        pass
    return [p for p in roots if p]


def _sensitive_write_exempt_roots() -> list[str]:
    """Subtrees carved back out of the sensitive hard-deny.

    Windows 上 per-sandbox workspace 是 ``JIUWENBOX_HOME/workspace/<id>``, 即
    敏感根 JIUWENBOX_HOME 的子树. 不豁免的话沙箱自己的 workspace 会被过滤链
    丢掉, 进而被 reconcile 当作 stale 撤销写 ACE.
    """
    try:
        from jiuwenbox.server.workspace import SANDBOX_WORKSPACE

        return [str(SANDBOX_WORKSPACE)] if SANDBOX_WORKSPACE else []
    except Exception:  # noqa: BLE001
        return []


def _is_under_or_equal(path_key: str, root_key: str) -> bool:
    if not path_key or not root_key:
        return False
    if path_key == root_key:
        return True
    sep = os.sep
    return path_key.startswith(root_key.rstrip("\\/") + sep)


def _is_heavy_directory(path: str) -> bool:
    """全树计数是否超过 HEAVY_PROPAGATE_ENTRIES. 结果按 TTL 缓存."""
    if not os.path.isdir(path):
        return False
    try:
        cache_key = _path_key(path)
    except Exception:  # noqa: BLE001
        cache_key = path
    now = time.monotonic()
    with _heavy_cache_lock:
        hit = _heavy_cache.get(cache_key)
        if hit is not None and now - hit[0] < _HEAVY_CACHE_TTL_S:
            return hit[1]
    n = 0
    heavy = False
    try:
        for _root, dirs, files in os.walk(path):
            n += len(dirs) + len(files)
            if n > HEAVY_PROPAGATE_ENTRIES:
                heavy = True
                break
    except OSError:
        heavy = False
    with _heavy_cache_lock:
        _heavy_cache[cache_key] = (now, heavy)
    return heavy


def filter_write_roots(
    paths: list[str],
    *,
    deny_write: list[str] | None = None,
    drop_heavy: bool = True,
    max_roots: int | None = MAX_WRITE_ROOTS_PER_CALL,
) -> list[str]:
    """Codex-aligned write-root filter. Drops unsafe roots; does not raise.

    Callers that need fail-closed should treat an empty result plus original
    non-empty input as denial, or use ``assert_write_roots``.

    ``drop_heavy`` 仍接受但不再丢弃巨型目录 (改为后台拆分并行授权).
    ``max_roots`` 只在准入 (单次调用) 时生效. reconcile 传
    ``drop_heavy=False, max_roots=None``: max_roots 在 reconcile 里
    触发会把已授权的根截断成 stale 而撤销 ACE.
    """
    deny_keys = {_path_key(p) for p in (deny_write or []) if p}
    sensitive_keys = {_path_key(p) for p in _sensitive_write_roots() if p}
    exempt_keys = {_path_key(p) for p in _sensitive_write_exempt_roots() if p}
    out: list[str] = []
    seen: set[str] = set()
    _ = drop_heavy  # API compat; heavy dirs are split-granted, never dropped.

    for raw in paths:
        if not raw:
            continue
        try:
            key = _path_key(str(raw))
        except Exception:  # noqa: BLE001
            continue
        if key in seen:
            continue
        if _is_volume_root(raw) or _is_users_directory(raw):
            logger.info("filter_write_roots drop volume/Users root: %s", raw)
            continue
        # 只拒「候选在敏感根之内」. 敏感根的祖先不拒 — 计划的语义是整根进
        # allow_write, 敏感子树由同一份 policy 的 deny_write 雕空.
        exempt = any(_is_under_or_equal(key, e) for e in exempt_keys)
        if not exempt and any(_is_under_or_equal(key, s) for s in sensitive_keys):
            logger.info("filter_write_roots drop sensitive: %s", raw)
            continue
        if any(_is_under_or_equal(key, d) for d in deny_keys):
            logger.info("filter_write_roots drop deny_write subtree: %s", raw)
            continue
        # drop_heavy 是历史准入开关, 巨型目录改为拆分并行授权, 不再丢弃.
        seen.add(key)
        out.append(str(Path(os.path.expandvars(os.path.expanduser(raw)))))
        if max_roots is not None and len(out) >= max_roots:
            logger.warning("filter_write_roots hit per-call cap=%d", max_roots)
            break
    return out


def assert_write_roots(
    paths: list[str],
    *,
    deny_write: list[str] | None = None,
) -> list[str]:
    """Filter write roots and raise ``WriteRootDenied`` if nothing remains."""
    filtered = filter_write_roots(paths, deny_write=deny_write)
    if paths and not filtered:
        raise WriteRootDenied("all write roots were rejected by the filter chain")
    from jiuwenbox.supervisor import win_setup as _ws

    existing = _ws.write_cap_count()
    new_needed = 0
    for p in filtered:
        if not _ws.get_write_cap_sid_if_exists(p):
            new_needed += 1
    if existing + new_needed > MAX_TOTAL_WRITE_CAPS:
        raise WriteRootDenied(
            f"write cap SID budget exceeded ({existing}+{new_needed}>{MAX_TOTAL_WRITE_CAPS})"
        )
    return filtered


def cap_sids_for_write_roots(paths: list[str]) -> list[str]:
    """Allocate or reuse per-root write cap SIDs (box-server only)."""
    from jiuwenbox.supervisor import win_setup as _ws

    sids: list[str] = []
    seen: set[str] = set()
    for path in paths:
        sid = _ws.load_or_create_write_cap_sid(path)
        if sid not in seen:
            seen.add(sid)
            sids.append(sid)
    return sids


def revoke_write_root_aces(
    paths: list[str],
    sandbox_user_sid: str | None = None,
) -> None:
    """Revoke cap SID + group write ACEs (and leftover synth SID) on leftover roots."""
    _require_windows()
    from jiuwenbox.supervisor import win_setup as _ws

    group_sid = _ws.get_sandbox_group_sid()
    shared: list = []
    try:
        shared.append(_resolve_sid(get_synthetic_write_sid()))
    except Exception:  # noqa: BLE001
        pass
    if sandbox_user_sid:
        try:
            shared.append(_resolve_sid(sandbox_user_sid))
        except Exception:  # noqa: BLE001
            pass
    if group_sid:
        try:
            shared.append(_resolve_sid(group_sid))
        except Exception:  # noqa: BLE001
            pass
    for root_path in paths:
        p = os.path.expandvars(root_path)
        if not os.path.exists(p):
            continue
        target = list(shared)
        cap = _ws.get_write_cap_sid_if_exists(p)
        if cap:
            try:
                target.append(_resolve_sid(cap))
            except Exception:  # noqa: BLE001
                pass
        if not target:
            continue
        try:
            purge_sid_aces(p, target, propagate=True)
        except Exception:  # noqa: BLE001
            logger.debug("revoke_write_root_aces failed path=%s", p, exc_info=True)


def _raise_if_required_write_ace_failed(
    failed: list[str],
    required_roots: list[str] | None,
) -> None:
    """Fail closed for this call's roots; leftover union failures are skipped."""
    if not failed:
        return
    if required_roots is None:
        raise WriteAceFailed(f"failed to apply write ACEs: {failed}")
    req = {_path_key(p) for p in required_roots}
    blocking = [p for p in failed if _path_key(p) in req]
    if blocking:
        raise WriteAceFailed(f"failed to apply write ACEs: {blocking}")


def reconcile_live_write_roots(
    current_roots: list[str],
    *,
    sandbox_user_sid: str | None = None,
    deny_write: list[str] | None = None,
    required_roots: list[str] | None = None,
) -> list[str]:
    """Refresh write ACEs to the live-root union and revoke leftovers.

    Single-flight keyed by the canonical root set so concurrent execs with
    the same union do not re-walk the tree.

    Raises ``WriteAceFailed`` when a **required** root's write ACE could not
    be applied (``required_roots is None`` keeps the old fail-closed-on-any
    behavior). Leftover roots from other extra.paths / sandboxes that fail
    ACE apply are returned so the caller can forget them, instead of 500-ing
    an unrelated workspace write.

    撤销陈旧根失败仍是 best-effort (只记日志).
    """
    _require_windows()
    from jiuwenbox.supervisor import win_setup as _ws

    _ws.migrate_legacy_synthetic_aces(sandbox_user_sid)
    filtered = filter_write_roots(
        current_roots, deny_write=deny_write, drop_heavy=False, max_roots=None,
    )
    key = frozenset(_path_key(p) for p in filtered)
    owner = False
    with _reconcile_lock:
        existing = _reconcile_inflight.get(key)
        if existing is not None:
            event, holder = existing
        else:
            event, holder = threading.Event(), []
            _reconcile_inflight[key] = (event, holder)
            owner = True
    if not owner:
        if not event.wait(timeout=120.0):
            raise WriteAceFailed("timed out waiting for a concurrent write-ACE reconcile")
        failed = list(holder[0]) if holder else []
        _raise_if_required_write_ace_failed(failed, required_roots)
        return failed
    try:
        failed = _reconcile_write_roots_body(filtered, sandbox_user_sid=sandbox_user_sid)
        if failed:
            holder.append(failed)
    finally:
        with _reconcile_lock:
            _reconcile_inflight.pop(key, None)
        event.set()
    failed = list(holder[0]) if holder else []
    _raise_if_required_write_ace_failed(failed, required_roots)
    return failed


def _reconcile_write_roots_body(
    current_roots: list[str],
    *,
    sandbox_user_sid: str | None,
) -> list[str]:
    """Returns the roots whose write ACE could not be applied (empty = ok)."""
    from jiuwenbox.supervisor import win_setup as _ws

    current_keys = {_path_key(p) for p in current_roots}
    # 只看 live 写根. read_acl (allow_read/deny_*) 是另一套语义,
    # 混用会让 reconcile 把 deny_read/deny_write 的 ACE 一并撤掉.
    historical = _ws.load_live_write_acl()
    historical_keys = {_path_key(p) for p in historical}
    stale = [p for p in historical if _path_key(p) not in current_keys]
    # 已在清单里的根说明写 ACE 已施加过 (check-then-set 幂等), 只对新根 apply.
    to_apply = [p for p in current_roots if _path_key(p) not in historical_keys]
    # 沿用的老根 (已施加过, 本轮不重复 apply) 必须留在清单里.
    kept = [p for p in current_roots if _path_key(p) in historical_keys]
    failed: list[str] = []
    if to_apply:
        result = apply_sandbox_acl(
            "",
            to_apply,
            [],
            allow_read=[],
            deny_read=[],
            sandbox_user_sid=sandbox_user_sid,
        )
        if result.failed:
            logger.warning("reconcile apply failed paths=%s", result.failed)
            failed = list(result.failed)
        failed_keys = {_path_key(p) for p in result.failed}
        kept.extend(p for p in to_apply if _path_key(p) not in failed_keys)
    if stale:
        revoke_write_root_aces(stale, sandbox_user_sid=sandbox_user_sid)
        logger.info("reconcile revoked %d leftover write roots", len(stale))
    if stale or to_apply:
        try:
            _ws.save_live_write_acl(kept)
        except Exception:  # noqa: BLE001
            logger.debug("save_live_write_acl during reconcile failed", exc_info=True)
    return failed

