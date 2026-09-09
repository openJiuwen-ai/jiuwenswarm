# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
r"""Windows 沙箱一次性环境准备.

对齐 docs/window沙箱.md 6.4:
  - 创建 jbx-sandbox 本地用户 + jbx-sandbox-users 组 (随机密码, 标记
    PASSWD_CANT_CHANGE|DONT_EXPIRE_PASSWD, 从登录界面隐藏).
  - LookupAccountName 取用户 SID, 写注册表供后续模块复用.
  - 安装 WFP filter set (win_wfp.install_wfp_filters).
  - 异步预装常用目录读 ACL (%USERPROFILE% / %SystemRoot% / Program Files /
    ProgramData) — 后台线程, 进度写注册表支持断点续传.

幂等: 通过注册表 ``HKLM\Software\JiuwenBox\WindowsSandbox\installed`` 标记
判断是否已完成, 重复执行无副作用.

安装入口:
  - 桌面安装器 (已提权) 调 ``--desktop-run-win-setup --install --force --recreate-user``.
  - 显式 CLI 非管理员仍会 ShellExecuteW(runas); 运行时不再走这条路.
"""

from __future__ import annotations

import ctypes
import json
import logging
import os
import secrets
import sys
import threading
import time
import argparse
from pathlib import Path
from ctypes import wintypes

from jiuwenbox.logging_config import configure_logging
from jiuwenbox.supervisor import win_constants as const

configure_logging()
logger = logging.getLogger(__name__)

# Windows netapi32 (NetUserAdd 等).
_netapi32: ctypes.WinDLL | None = None
_advapi32: ctypes.WinDLL | None = None
_kernel32: ctypes.WinDLL | None = None
_shell32: ctypes.WinDLL | None = None
# userenv.dll (DeleteProfileW — 删 profile 目录 + ProfileList 注册项).
_userenv: ctypes.WinDLL | None = None
# 并发创建沙箱时只允许一次 install / UAC, 避免连弹提权框并互删刚建的用户.
_INSTALL_LOCK = threading.RLock()


def _require_windows() -> None:
    if sys.platform != "win32":
        raise RuntimeError(
            f"win_setup 仅在 Windows 平台可用; 当前平台 {sys.platform!r}"
        )


# ShellExecuteW 的 nShowWindow 取值: 0=SW_HIDE (不弹 CMD), 1=SW_SHOWNORMAL.
# install/uninstall 提权子进程: 子进程 CMD 仍隐藏, 但 UAC 确认框必须可见 (桌面产品).
SW_HIDE = 0
SW_SHOWNORMAL = 1
SW_UAC_SHOW = SW_SHOWNORMAL


def _resolve_install_log_dir() -> str:
    """返回 install_force.log 应落盘的目录 (绝对路径)."""
    try:
        home = Path(os.environ.get("USERPROFILE") or "") or Path.home()
    except Exception:  # noqa: BLE001
        home = Path.home()
    office_root_env = os.environ.get("OFFICE_CLAW_DATA_DIR", "").strip()
    jiuwen_env = os.environ.get("JIUWENCLAW_DATA_DIR", "").strip()
    office_root = (
        Path(office_root_env).expanduser().resolve()
        if office_root_env
        else home / ".office-claw"
    )
    if jiuwen_env:
        data_dir = Path(jiuwen_env).expanduser().resolve()
    else:
        data_dir = office_root / ".jiuwenclaw"
    return str(data_dir / "jiuwenbox")


def _install_log_path() -> str:
    """install_force.log 的绝对路径, 落在用户数据目录而非包目录."""
    return os.path.join(_resolve_install_log_dir(), "install_force.log")


_ACL_POLICY_PATHS_USER_CACHE = "acl_policy_paths.json"


def _normalize_acl_path(path: str) -> str:
    """统一 ACL 路径比较格式 (expandvars + normpath + normcase)."""
    p = (path or "").strip()
    if not p:
        return ""
    return os.path.normcase(os.path.normpath(os.path.expandvars(p)))


def _user_acl_policy_cache_file() -> Path:
    return Path(_resolve_install_log_dir()) / _ACL_POLICY_PATHS_USER_CACHE


def _load_user_acl_policy_paths() -> set[str]:
    """读用户目录下的 acl 路径缓存 (普通用户可写, 无需 HKLM)."""
    cache = _user_acl_policy_cache_file()
    try:
        data = json.loads(cache.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return set()
    if not isinstance(data, list):
        return set()
    return {
        _normalize_acl_path(p)
        for p in data
        if isinstance(p, str) and p.strip()
    }


def _save_user_acl_policy_paths(paths: set[str]) -> None:
    if not paths:
        return
    cache = _user_acl_policy_cache_file()
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(
            json.dumps(sorted(paths), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError as exc:
        logger.warning("写入 acl_policy_paths 用户缓存失败 (非致命): %s", exc)


def _get_acl_policy_paths_recorded() -> set[str]:
    """已处理的 deny/allow 路径 = HKLM 注册表 ∪ 用户目录缓存."""
    recorded: set[str] = set()
    raw = reg_get_str(const.REG_VALUE_ACL_POLICY_PATHS)
    if raw:
        try:
            recorded |= {
                _normalize_acl_path(p)
                for p in json.loads(raw)
                if isinstance(p, str)
            }
        except (ValueError, TypeError):
            pass
    recorded |= _load_user_acl_policy_paths()
    return {p for p in recorded if p}


def is_install_completed() -> bool:
    """据 install_force.log 是否含完成标志判断 install 是否成功."""
    try:
        with open(_install_log_path(), "r", encoding="utf-8") as fh:
            return "Windows 沙箱安装完成" in fh.read()
    except OSError:
        return False


def _get_netapi32() -> ctypes.WinDLL:
    global _netapi32
    if _netapi32 is None:
        _netapi32 = ctypes.WinDLL("netapi32", use_last_error=True)
        _netapi32.NetUserAdd.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p,
            ctypes.POINTER(wintypes.DWORD),
        ]
        _netapi32.NetUserAdd.restype = wintypes.DWORD
        _netapi32.NetLocalGroupAddMembers.argtypes = [
            wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
            ctypes.c_void_p, wintypes.DWORD,
        ]
        _netapi32.NetLocalGroupAddMembers.restype = wintypes.DWORD
        # NetLocalGroupAdd(servername:LPCWSTR, level:DWORD, buf:LPBYTE, parm_err:LPDWORD)
        # 原代码 argtypes 错位 (把 level 标成 LPCWSTR, buf 标成 DWORD),
        # 调用时 int 0(level) 喂给 c_wchar_p → ctypes.ArgumentError.
        _netapi32.NetLocalGroupAdd.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        _netapi32.NetLocalGroupAdd.restype = wintypes.DWORD
        _netapi32.NetUserGetInfo.argtypes = [
            wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        _netapi32.NetUserGetInfo.restype = wintypes.DWORD
        # NetUserSetInfo(servername, username, level, buf, parm_err): 重设用户属性 (含密码).
        _netapi32.NetUserSetInfo.argtypes = [
            wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
            ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD),
        ]
        _netapi32.NetUserSetInfo.restype = wintypes.DWORD
        # NetUserDel(servername, username): 删用户 (uninstall).
        _netapi32.NetUserDel.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
        _netapi32.NetUserDel.restype = wintypes.DWORD
        # NetLocalGroupDel(servername, groupname): 删本地组 (uninstall).
        _netapi32.NetLocalGroupDel.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
        _netapi32.NetLocalGroupDel.restype = wintypes.DWORD
        _netapi32.NetApiBufferFree.argtypes = [ctypes.c_void_p]
        _netapi32.NetApiBufferFree.restype = wintypes.DWORD
    return _netapi32


def _get_advapi32() -> ctypes.WinDLL:
    global _advapi32
    if _advapi32 is None:
        _advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        _advapi32.LookupAccountNameW.argtypes = [
            wintypes.LPCWSTR, wintypes.LPCWSTR,
            ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD),
            ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD),
        ]
        _advapi32.LookupAccountNameW.restype = wintypes.BOOL
        _advapi32.ConvertSidToStringSidW.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR),
        ]
        _advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
        _advapi32.RegCreateKeyExW.argtypes = [
            wintypes.HKEY, wintypes.LPCWSTR, wintypes.DWORD,
            wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD,
            ctypes.c_void_p, ctypes.POINTER(wintypes.HKEY),
            ctypes.POINTER(wintypes.DWORD),
        ]
        _advapi32.RegCreateKeyExW.restype = wintypes.LONG
        _advapi32.RegSetValueExW.argtypes = [
            wintypes.HKEY, wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
            ctypes.c_void_p, wintypes.DWORD,
        ]
        _advapi32.RegSetValueExW.restype = wintypes.LONG
        _advapi32.RegQueryValueExW.argtypes = [
            wintypes.HKEY, wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
            ctypes.POINTER(wintypes.DWORD),
        ]
        _advapi32.RegQueryValueExW.restype = wintypes.LONG
        _advapi32.RegCloseKey.argtypes = [wintypes.HKEY]
        _advapi32.RegCloseKey.restype = wintypes.LONG
    return _advapi32


def get_kernel32() -> ctypes.WinDLL:
    global _kernel32
    if _kernel32 is None:
        _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        _kernel32.CloseHandle.restype = wintypes.BOOL
        # WaitForSingleObject: 主进程阻塞等待 install 子进程 SetEvent 通知完成.
        # 不依赖提权子进程的 process handle (ShellExecuteW 拿不到), 改靠命名 Event.
        _kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        _kernel32.WaitForSingleObject.restype = wintypes.DWORD
        # CreateEventW: 主进程创建命名 Event (非信号态), 名字传给 install 子进程.
        _kernel32.CreateEventW.argtypes = [
            ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR,
        ]
        _kernel32.CreateEventW.restype = wintypes.HANDLE
        # OpenEventW: install 子进程打开主进程创建的命名 Event.
        _kernel32.OpenEventW.argtypes = [
            wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR,
        ]
        _kernel32.OpenEventW.restype = wintypes.HANDLE
        # SetEvent: install 子进程跑完所有步骤后 set, 通知主进程可以继续.
        _kernel32.SetEvent.argtypes = [wintypes.HANDLE]
        _kernel32.SetEvent.restype = wintypes.BOOL
    return _kernel32


def _get_shell32() -> ctypes.WinDLL:
    global _shell32
    if _shell32 is None:
        _shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        _shell32.ShellExecuteW.argtypes = [
            wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR,
            wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.INT,
        ]
        _shell32.ShellExecuteW.restype = wintypes.HINSTANCE
    return _shell32


def _get_userenv() -> ctypes.WinDLL:
    """userenv.dll 的 DeleteProfileW: 删 profile 目录 + ProfileList 注册项.

    这是删 Windows 用户 profile 的正规方式 (比 shutil.rmtree C:\\Users\\<x>
    干净): 它会同时删除目录树 + HKLM\\...\\ProfileList\\<sid> 注册项. 仅
    rmtree 目录会留 ProfileList 项, 下次同名用户登录 Windows 发现原目录没了
    就建 .000/.001 备份 — 这正是 C:\\Users 下 jbx-sandbox.* 堆积的根因.
    需 admin.
    """
    global _userenv
    if _userenv is None:
        _userenv = ctypes.WinDLL("userenv", use_last_error=True)
        # DeleteProfileW(LPCWSTR lpSidString, LPCWSTR lpProfilePath,
        #                LPCWSTR lpComputerName): lpProfilePath 传 None 让
        # API 自己从 ProfileList 解析 (推荐, 避免 we 读到错路径).
        _userenv.DeleteProfileW.argtypes = [
            wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR,
        ]
        _userenv.DeleteProfileW.restype = wintypes.BOOL
    return _userenv


# ---------------------------------------------------------------------------
# 注册表读写.
# ---------------------------------------------------------------------------
HKEY_LOCAL_MACHINE = wintypes.HKEY(0x80000002)  # noqa: N816 - Win32 常量
# Win32 SAM 权限位: KEY_READ=0x20019, KEY_WRITE=0x20006, KEY_READ|KEY_WRITE=0x2001F.
# KEY_READ_ONLY 给 reg_get_str (普通用户可读已存在 key), KEY_READ_WRITE 给 _reg_open_create (写注册表, 需 admin).
KEY_READ_ONLY = 0x20019  # noqa: N816 - Win32 常量
KEY_READ_WRITE = 0x2001F  # noqa: N816 - Win32 常量
REG_SZ = 1  # noqa: N816 - Win32 常量
REG_DWORD = 4  # noqa: N816 - Win32 常量


def _reg_open_create() -> wintypes.HKEY:
    advapi32 = _get_advapi32()
    hkey = wintypes.HKEY()
    disp = wintypes.DWORD(0)
    ret = advapi32.RegCreateKeyExW(
        HKEY_LOCAL_MACHINE, const.REG_BASE_KEY, 0, None, 0,
        KEY_READ_WRITE, None, ctypes.byref(hkey), ctypes.byref(disp),
    )
    if ret != 0:
        raise ctypes.WinError(ret)
    return hkey


def _reg_set_str(name: str, value: str) -> None:
    advapi32 = _get_advapi32()
    hkey = _reg_open_create()
    try:
        buf = ctypes.create_unicode_buffer(value)
        ret = advapi32.RegSetValueExW(
            hkey, name, 0, REG_SZ, buf, (len(value) + 1) * 2,
        )
        if ret != 0:
            raise ctypes.WinError(ret)
    finally:
        advapi32.RegCloseKey(hkey)


def reg_get_str(name: str) -> str | None:
    # 读操作只用 KEY_READ 打开已存在 key (RegOpenKeyEx, 不创建).
    # 只读打开已存在 key: 用 KEY_READ_WRITE 会被普通用户拒 (WinError 5) 导致幂等检查抛错、提权分支走不到.
    # key 不存在/无权限均返回 None, 让上层走 install() 正常提权路径.
    advapi32 = _get_advapi32()
    hkey = wintypes.HKEY()
    ret = advapi32.RegOpenKeyExW(
        HKEY_LOCAL_MACHINE, const.REG_BASE_KEY, 0, KEY_READ_ONLY,
        ctypes.byref(hkey),
    )
    if ret != 0:
        # ERROR_FILE_NOT_FOUND (2) / ERROR_ACCESS_DENIED (5) 均视为"未读到".
        return None
    try:
        typ = wintypes.DWORD(0)
        # 两阶段读: 先查真实 size (返回 ERROR_MORE_DATA=234) 再分配 buffer. 固定 512 chars 会截断 DPAPI 密码 blob.
        size = wintypes.DWORD(0)
        ret = advapi32.RegQueryValueExW(
            hkey, name, None, ctypes.byref(typ), None, ctypes.byref(size),
        )
        if ret != 0 and ret != 234:
            return None
        # size 是字节数 (含 NUL). REG_SZ 是 UTF-16, char 数 = size/2.
        char_count = max(1, size.value // 2 + 1)
        buf = ctypes.create_unicode_buffer(char_count)
        ret = advapi32.RegQueryValueExW(
            hkey, name, None, ctypes.byref(typ), buf, ctypes.byref(size),
        )
        if ret != 0:
            return None
        return buf.value
    finally:
        advapi32.RegCloseKey(hkey)


def get_preinstalled_read_paths() -> "set[str]":
    """读 install 写入注册表的"已预装读 ACL 路径"清单.

    Returns: 已预装读 ACL 的路径集合 (展开后, 规范化小写比较). 读不到/解析失败
             返回空集 (空集 = 不跳过任何路径 = 行为同现状, 安全降级).
    """
    import json as _json
    raw = reg_get_str(const.REG_VALUE_PREINSTALLED_PATHS)
    if not raw:
        return set()
    try:
        paths = _json.loads(raw)
    except (ValueError, TypeError):  # noqa: BLE001
        return set()
    if not isinstance(paths, list):
        return set()
    return {os.path.expandvars(p).rstrip("\\/").lower() for p in paths if isinstance(p, str) and p}


def _reg_set_dword_under(full_subkey: str, name: str, value: int) -> None:
    """在 HKLM\\<full_subkey> 下写一个 REG_DWORD (用于隐藏登录界面用户等).

    ``full_subkey`` 是相对 HKLM 的完整路径 (不拼 REG_BASE_KEY).
    """
    advapi32 = _get_advapi32()
    hkey = wintypes.HKEY()
    disp = wintypes.DWORD(0)
    ret = advapi32.RegCreateKeyExW(
        HKEY_LOCAL_MACHINE, full_subkey, 0, None, 0, KEY_READ_WRITE,
        None, ctypes.byref(hkey), ctypes.byref(disp),
    )
    if ret != 0:
        raise ctypes.WinError(ret)
    try:
        dword = wintypes.DWORD(value)
        advapi32.RegSetValueExW(
            hkey, name, 0, REG_DWORD, ctypes.byref(dword),
            ctypes.sizeof(wintypes.DWORD),
        )
    finally:
        advapi32.RegCloseKey(hkey)


# ---------------------------------------------------------------------------
# 用户/组创建.
# ---------------------------------------------------------------------------
class UserInfo1(ctypes.Structure):
    _fields_ = [
        ("usri1_name", wintypes.LPWSTR),
        ("usri1_password", wintypes.LPWSTR),
        ("usri1_password_age", wintypes.DWORD),
        ("usri1_priv", wintypes.DWORD),
        ("usri1_home_dir", wintypes.LPWSTR),
        ("usri1_comment", wintypes.LPWSTR),
        ("usri1_flags", wintypes.DWORD),
        ("usri1_script_path", wintypes.LPWSTR),
    ]


USER_PRIV_USER = 1  # noqa: N816 - Win32 常量
# lmerr.h: 沙箱用户重建/校验用.
NERR_USER_NOT_FOUND = 2221  # noqa: N816 - Win32 常量
NERR_USER_EXISTS = 2224  # noqa: N816 - Win32 常量
# CryptProtectData 机器绑定; 旧版 flags=0 换 Windows 用户后无法解密.
CRYPTPROTECT_LOCAL_MACHINE = 0x4  # noqa: N816 - Win32 常量

# 沙箱用户缺失或无法登录时的修复提示 (运行时无管理员权限, 不能 NetUserAdd).
_SANDBOX_USER_REPAIR = (
    "请重新运行安装包, 或管理员执行 "
    "jiuwenswarm.exe --desktop-run-win-setup --install --force --recreate-user"
)


def _generate_password() -> str:
    """生成 jbx-sandbox 用户随机密码.

    用 ``secrets.token_urlsafe`` 生成强随机密码 (长度 ~SANDBOX_USER_PASSWORD_LENGTH
    字符的 URL 安全 base64). 持久化靠 DPAPI 注册表 (见 install 流程
    ``_save_sandbox_user_password``): install 时存一次, 后续 install/运行时经
    ``get_sandbox_user_password`` 读回, 重启后仍可登录. 已存在用户不重设密码
    (见 ``_create_sandbox_user``), 避免运行中沙箱凭据失效.
    """
    # token_urlsafe(n) 返回 ~1.3n 字符; 取 48 字节字节熵 (~64 字符), 满足密码
    # 复杂度策略 (含字母数字, 无需符号即可过 Windows 强密码策略).
    return secrets.token_urlsafe(48)


def _sandbox_user_exists() -> bool:
    """SAM 中是否仍有 jbx-sandbox. 与注册表 installed=1 / DPAPI 密码独立."""
    netapi32 = _get_netapi32()
    buf = ctypes.c_void_p()
    ret = netapi32.NetUserGetInfo(
        None, const.SANDBOX_USER_NAME, 0, ctypes.byref(buf),
    )
    try:
        return ret == 0
    finally:
        if buf:
            netapi32.NetApiBufferFree(buf)


def _logon_sandbox_user(password: str) -> bool:
    """用给定密码 LogonUserW 校验 jbx-sandbox 能否登录 (不加载 profile)."""
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    token = ctypes.c_void_p()
    logon_provider_default = 0  # noqa: N806 - Win32 常量
    # 登录类型优先 NETWORK (3) 非 INTERACTIVE (2): 本函数只校验密码不加载 profile.
    # INTERACTIVE 首次登录会物理建 C:\Users\jbx-sandbox 目录 + 挂载 NTUSER.DAT.
    logon_network = 3  # noqa: N806 - Win32 常量
    logon_interactive = 2  # noqa: N806 - Win32 常量
    ok = advapi32.LogonUserW(
        const.SANDBOX_USER_NAME, None, password,
        logon_network, logon_provider_default,
        ctypes.byref(token),
    )
    if not ok:
        network_err = ctypes.get_last_error()
        logger.debug(
            "jbx-sandbox network logon 失败 (WinError %d), 回退 interactive 校验密码",
            network_err,
        )
        ok = advapi32.LogonUserW(
            const.SANDBOX_USER_NAME, None, password,
            logon_interactive, logon_provider_default,
            ctypes.byref(token),
        )
    if ok:
        try:
            ctypes.WinDLL("kernel32").CloseHandle(token)
        except OSError:
            pass
        return True
    return False


def _save_sandbox_user_password(password: str) -> None:
    """DPAPI 加密密码写入 HKLM (机器绑定, 本机任意用户可解密)."""
    try:
        import win32crypt  # type: ignore[import-not-found]
        enc = win32crypt.CryptProtectData(
            password.encode("utf-8"), "jbx-sandbox-pw", None, None, None,
            CRYPTPROTECT_LOCAL_MACHINE,
        )
        _reg_set_str(const.REG_VALUE_SANDBOX_USER_PW, enc.hex())
    except ImportError:  # pragma: no cover
        logger.warning("pywin32 缺失, 沙箱用户密码未加密存储 (仅开发环境)")


def _create_sandbox_user(password: str, *, retries: int = 1) -> bool:
    """创建 jbx-sandbox 本地用户 (幂等: 已存在则跳过).

    retries>1 用于刚 NetUserDel 后的 pending-delete (Add 返回 2224).
    Returns: True=本次新建; False=已存在 (未改密码, 调用方用 DPAPI 旧密码).
    """
    netapi32 = _get_netapi32()
    info = UserInfo1()
    # LPWSTR 字段直接赋 str: ctypes 自动转 NUL 结尾宽字符串指针并绑定生命周期.
    # 不能用 create_unicode_buffer (返回 c_wchar_Array_N, py3.13 ctypes 拒绝数组→指针赋值).
    info.usri1_name = const.SANDBOX_USER_NAME
    info.usri1_password = password
    info.usri1_password_age = 0
    info.usri1_priv = USER_PRIV_USER
    info.usri1_home_dir = None
    info.usri1_comment = "JiuwenBox sandbox user"
    info.usri1_flags = const.SANDBOX_USER_FLAGS
    info.usri1_script_path = None

    attempts = max(1, retries)
    last_ret = 0
    last_err = 0
    for attempt in range(attempts):
        err = wintypes.DWORD(0)
        ret = netapi32.NetUserAdd(
            None, const.USER_INFO_1_LEVEL, ctypes.byref(info), ctypes.byref(err),
        )
        if ret == 0:
            logger.info("创建沙箱用户 %s 成功", const.SANDBOX_USER_NAME)
            return True
        if ret == NERR_USER_EXISTS:
            if attempt + 1 < attempts:
                logger.warning(
                    "NetUserAdd 用户已存在 (可能 pending-delete), 0.5s 后重试 (%d/%d)",
                    attempt + 1, attempts,
                )
                time.sleep(0.5)
                continue
            # 用户已存在不重设密码: 随机密码下本次生成的新密码与实际不一致, 不能用它存 DPAPI (否则重现 1326).
            # 调用方应读 DPAPI 旧密码存注册表, 读不到时由 _verify_or_reset 重设.
            logger.info(
                "沙箱用户 %s 已存在, 跳过 (不重设密码, 用 DPAPI 旧密码)",
                const.SANDBOX_USER_NAME,
            )
            return False
        last_ret, last_err = ret, err.value
        if attempt + 1 < attempts:
            logger.warning(
                "NetUserAdd 失败 ret=%d err=%d, 0.5s 后重试 (%d/%d)",
                ret, err.value, attempt + 1, attempts,
            )
            time.sleep(0.5)
            continue
        raise RuntimeError(
            f"NetUserAdd 失败: ret={last_ret} err={last_err}"
        )
    raise RuntimeError(
        f"NetUserAdd 失败: ret={last_ret} err={last_err}"
    )


def _set_user_password(user_name: str, password: str) -> None:
    """重设已有用户的密码 (NetUserSetInfo level=1).

    install 幂等场景: 用户已存在时把密码重设为本次生成的值, 保证用户密码
    与注册表 DPAPI 密码一致 (见 1326). 需 admin (install 本身 admin).
    """
    netapi32 = _get_netapi32()
    info = UserInfo1()
    info.usri1_name = user_name  # LPWSTR 直接赋 str (见 _create_sandbox_user 注释)
    info.usri1_password = password
    info.usri1_password_age = 0
    info.usri1_priv = USER_PRIV_USER
    info.usri1_home_dir = None
    info.usri1_comment = "JiuwenBox sandbox user"
    info.usri1_flags = const.SANDBOX_USER_FLAGS
    info.usri1_script_path = None
    err = wintypes.DWORD(0)
    ret = netapi32.NetUserSetInfo(
        None, user_name, const.USER_INFO_1_LEVEL, ctypes.byref(info), ctypes.byref(err),
    )
    if ret != 0:
        raise RuntimeError(
            f"NetUserSetInfo 失败: ret={ret} err={err.value}"
        )


def _add_user_to_group() -> None:
    """把 jbx-sandbox 加入 jbx-sandbox-users 组 (幂等, 失败 raise).

    S8: 旧版用 level 0 (LOCALGROUP_MEMBERS_INFO_0, 字段是 PSID) 却塞用户名字符串
    -> netapi 解析 PSID 失败返回 1337 (ERROR_INVALID_PASSWORD), 用户没进组。
    改用 level 3 (LOCALGROUP_MEMBERS_INFO_3, lgrpi3_domainandname 接受
    "DOMAIN\\user" 名字串), 与 LookupAccountName 拿到的域\\用户格式一致。
    """
    netapi32 = _get_netapi32()
    # 先尝试建组 (已存在则忽略, 错误码 2237 = NERR_GroupExists).

    class LocalGroupInfo0(ctypes.Structure):
        _fields_ = [("lgrpi0_name", wintypes.LPWSTR)]

    grp_info = LocalGroupInfo0()
    grp_info.lgrpi0_name = const.SANDBOX_USER_GROUP  # LPWSTR 直接赋 str (见 _create_sandbox_user 注释)
    ret = netapi32.NetLocalGroupAdd(
        None, 0, ctypes.byref(grp_info), None,
    )
    if ret == 0:
        logger.info("创建组 %s 成功", const.SANDBOX_USER_GROUP)
    elif ret in (2237, 1379):
        logger.info("组 %s 已存在, 跳过", const.SANDBOX_USER_GROUP)
    else:
        # 组创建失败是致命 (S12): 没组就没法加成员, 后续组级 ACL 全废。
        raise RuntimeError(f"NetLocalGroupAdd 失败 ret={ret}")

    # 加成员用 level 3 (LOCALGROUP_MEMBERS_INFO_3 { lgrpi3_domainandname: LPWSTR }).
    # level 0 字段是 PSID, 传名字串会返回 1337. level 3 接受 "DOMAIN\\user" 或裸名, 本地账户用裸名.
    class LocalGroupMembersInfo3(ctypes.Structure):  # noqa: E306 - Win32 结构体
        _fields_ = [("lgrpi3_domainandname", wintypes.LPWSTR)]

    member = LocalGroupMembersInfo3()
    member.lgrpi3_domainandname = const.SANDBOX_USER_NAME  # LPWSTR 直接赋 str
    ret = netapi32.NetLocalGroupAddMembers(
        None, const.SANDBOX_USER_GROUP, 3,
        ctypes.byref(member), 1,
    )
    # 0 = 成功; 1377 = ERROR_MEMBER_IN_ALIAS (已在组中, 幂等).
    # 1378 = ERROR_MEMBER_NOT_IN_ALIAS / NERR_GroupNotMember (某些 Windows 版本返回此码表示已在组中).
    if ret not in (0, 1377, 1378):
        # S12: 成员加入失败是致命, 组级 ACL 对 jbx-sandbox 不生效。
        raise RuntimeError(
            f"NetLocalGroupAddMembers 失败 ret={ret} (user={const.SANDBOX_USER_NAME} "
            f"group={const.SANDBOX_USER_GROUP})"
        )
    logger.info("用户 %s 已加入组 %s", const.SANDBOX_USER_NAME, const.SANDBOX_USER_GROUP)


def _lookup_user_sid(user_name: str) -> str:
    """LookupAccountName 取用户 SID 字符串."""
    advapi32 = _get_advapi32()
    # 先查长度.
    sid_buf = (ctypes.c_byte * 256)()
    sid_size = wintypes.DWORD(256)
    domain_buf = ctypes.create_unicode_buffer(256)
    domain_size = wintypes.DWORD(256)
    use = wintypes.DWORD(0)
    ok = advapi32.LookupAccountNameW(
        None, user_name, sid_buf, ctypes.byref(sid_size),
        domain_buf, ctypes.byref(domain_size), ctypes.byref(use),
    )
    if not ok:
        # 长度不够时重试.
        sid_buf = (ctypes.c_byte * sid_size.value)()
        domain_buf = ctypes.create_unicode_buffer(domain_size.value)
        ok = advapi32.LookupAccountNameW(
            None, user_name, sid_buf, ctypes.byref(sid_size),
            domain_buf, ctypes.byref(domain_size), ctypes.byref(use),
        )
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error())
    # 转 SID 字符串.
    sid_str = wintypes.LPWSTR()
    if not advapi32.ConvertSidToStringSidW(sid_buf, ctypes.byref(sid_str)):
        raise ctypes.WinError(ctypes.get_last_error())
    result = sid_str.value
    _free_sid_str(sid_str)
    return result


def _lookup_current_user_sid() -> str | None:
    """当前进程 TokenUser SID; 域账户不能靠 LookupAccountName(getpass.getuser())."""
    try:
        import win32api  # type: ignore[import-not-found]
        import win32con  # type: ignore[import-not-found]
        import win32security  # type: ignore[import-not-found]
        token = win32security.OpenProcessToken(
            win32api.GetCurrentProcess(), win32con.TOKEN_QUERY,
        )
        try:
            info = win32security.GetTokenInformation(
                token, win32security.TokenUser,
            )
            user = info[0] if isinstance(info, (tuple, list)) else info
            return win32security.ConvertSidToStringSid(user)
        finally:
            try:
                win32api.CloseHandle(token)
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        logger.debug("取当前用户 Token SID 失败, 回落 LookupAccountName", exc_info=True)
    try:
        import getpass as _getpass
        return _lookup_user_sid(_getpass.getuser())
    except Exception:  # noqa: BLE001
        return None


def _path_owned_by_current_user(path: str) -> bool:
    """当前用户是否已是 ``path`` (或其最近存在的父目录) 的 owner.

    owner 是当前用户时运行时就能改 DACL, 不必为每个沙箱 workspace 弹 UAC.
    """
    probe = path
    while probe and not os.path.exists(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    if not probe or not os.path.exists(probe):
        return False
    probe = os.path.normpath(probe)
    try:
        import win32security  # type: ignore[import-not-found]
        current = _lookup_current_user_sid()
        if not current:
            return False
        sd = win32security.GetFileSecurity(
            probe, win32security.OWNER_SECURITY_INFORMATION,
        )
        owner = sd.GetSecurityDescriptorOwner()
        owner_sid = win32security.ConvertSidToStringSid(owner)
        return os.path.normcase(owner_sid) == os.path.normcase(current)
    except Exception:  # noqa: BLE001
        return False


def _free_sid_str(sid_str: wintypes.LPWSTR) -> None:
    kernel32 = get_kernel32()
    try:
        kernel32.LocalFree(sid_str)  # type: ignore[arg-type]
    except Exception:  # noqa: BLE001
        pass


def _is_admin() -> bool:
    """检测当前进程是否以管理员身份运行."""
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:  # noqa: BLE001
        return False


def _delete_profile_by_sid(sid_str: str) -> bool:
    """DeleteProfileW 按 SID 删用户 profile (目录树 + ProfileList 注册项).

    需 admin. lpProfilePath 传 None 让 API 自己从 ProfileList 解析路径.
    返回 True=已删或本就无 profile; False=删除失败 (常见: profile 正被
    占用, 此情形下注册表项仍会被清理, 目录待下次重启后可手动删).

    本接口保留, 仅在 uninstall / reinstall 流程调用 (见 uninstall / install 回滚),
    不在沙箱运行期调用: 运行期 profile 由 win_exec.two_hop_spawn 的
    CreateProcessWithLogonW(LOGON_WITH_PROFILE) 首次登录创建, 首次后复用,
    运行期无需再删. 保留它是为了卸载干净 + reinstall 清理历史残留目录.
    """
    if not sid_str:
        return False
    userenv = _get_userenv()
    ok = userenv.DeleteProfileW(sid_str, None, None)
    if ok:
        return True
    err = ctypes.get_last_error()
    # ERROR_FILE_NOT_FOUND (2): 无 profile 可删, 视为幂等成功.
    if err == 2:
        return True
    logger.warning(
        "DeleteProfileW(sid=%s) 失败 last_error=%d (profile 可能正被占用; "
        "注册表项已清, 目录待重启后可手动删)", sid_str, err,
    )
    return False


def _purge_stale_profile_dirs() -> None:
    """清理 C:\\Users 下 jbx-sandbox* 历史残留 profile 目录.

    反复 reinstall 时 Windows 会建 jbx-sandbox.DESKTOP-XXX / .000 / .001 ...
    备份目录 (DeleteProfile 按 SID 只能删当前 SID 对应的那一个, 历史 RID
    对应的目录删不到). 这里按目录名前缀兜底 rmtree, 把所有
    jbx-sandbox / jbx-sandbox.* 一次性清掉. best-effort, 失败仅 warning.

    注意 profile 里有 WinX (Win+X 快捷菜单的 reparse point 目录, 系统锁定
    ACL), shutil.rmtree 默认遇 WinError 5 中止 → 前面删了的留半删残留. 用
    onerror 回调: 对每个失败文件 chmod 后重试, 仍失败则 warning 跳过并继续删
    其余 (而非整体中止). WinX 本身留作 reparse point 由 DeleteProfileW / 系统
    处理, 不阻断 profile 目录整体清理 (实测: 加 onerror 后 WinX 单文件失败但
    其余全删干净, 目录树最终 rmdir 成功).

    本接口保留, 仅在 uninstall / install 回滚流程调用, 不在沙箱运行期调用
    (运行期 profile 复用单一目录, 无残留堆积). 保留它专为 reinstall 清理历史
    残留 + uninstall 卸载干净, 见 _delete_profile_by_sid 注释.
    """
    import shutil

    def _rmtree_onerror(func, fpath, exc_info):
        # 对单文件/目录失败: 尝试改可写后重试一次. 仍失败则 warning 跳过,
        # 让 rmtree 继续删下一个 (默认行为是抛出中止整个 rmtree).
        try:
            os.chmod(fpath, 0o777)
            func(fpath)
        except OSError as exc:
            # 不阻断: 记 warning 后, rmtree 遇 onerror 返回 None 会继续后续文件.
            logger.debug("清理残留 profile: 跳过 %s (%s)", fpath, exc)

    users_root = os.environ.get("SystemDrive", "C:") + "\\Users"  # pylint: disable=use-system-path
    try:
        entries = os.listdir(users_root)
    except OSError as exc:
        logger.warning("列出 %s 失败, 跳过残留 profile 清理: %s", users_root, exc)
        return
    for name in entries:
        # 严格前缀匹配, 避免误删无关目录 (如 sandbox-other).
        if name == const.SANDBOX_USER_NAME or name.startswith(
            const.SANDBOX_USER_NAME + "."
        ):
            path = os.path.join(users_root, name)
            try:
                shutil.rmtree(path, onerror=_rmtree_onerror)
                # rmtree 不抛 (onerror 吞错) 即视为删完. 再确认目录还在不在
                # (onerror 全跳过时会残留), 仍在则 rmdir 兜底 (空目录可删).
                if os.path.isdir(path):
                    try:
                        os.rmdir(path)
                    except OSError:
                        logger.warning(
                            "残留 profile 目录 %s 删不尽 (含系统锁定子项如 WinX), "
                            "留待重启后删", path,
                        )
                        continue
                logger.info("删除残留 profile 目录 %s", path)
            except OSError as exc:
                # 正被占用 / 权限不足: 目录留待重启后删, 不阻断卸载.
                logger.warning(
                    "删除 %s 失败 (可能正被占用): %s", path, exc,
                )


def _load_policy_preinstall_paths(policy_path: str) -> list[str]:
    """从 windows-policy.yaml 读 read_acl_preinstall + tool_paths, 返回去重后的
    预装路径列表 (含 git_dir 的 usr/bin, bin 子目录 + bash_path 父目录).

    install 时调: 用户改 tool_paths 后 --force 重装, 把工具目录的读 ACL
    预装上 (运行时普通用户无权改这些目录 DACL).

    P1-11: 复用 PolicyReader.load_policy() 走 _resolve_tool_paths 探测填充
    (基底 tool_paths 四字段为空串时, load_policy 用 sys.executable/PATH 自动
    填充 python_dir/node_dir/git_dir/bash_path). 旧版直接 yaml.safe_load 读
    原始值, --force --policy-path 重装时 tool_paths 全空 → 预装集丢失工具目录
    → 重装后受限 token 读不了 tools/python. 现保证 install 与 runtime 读同一份
    填充后 tool_paths.
    """
    from pathlib import Path as _Path
    from jiuwenbox.server.policy_reader import PolicyReader
    # PolicyReader(policy_path=...) 加载指定文件; 若该路径等于基底则不合并副本
    # (policy_reader.py:182-185), 即只读该文件本身, 符合 install 提权场景.
    reader = PolicyReader(policy_path=_Path(policy_path))
    policy = reader.load_policy()
    win_fs = policy.windows.filesystem
    paths: list[str] = []
    for p in win_fs.read_acl_preinstall or []:
        if isinstance(p, str) and p:
            paths.append(p)
    tp = win_fs.tool_paths
    for key in ("git_dir", "node_dir", "python_dir"):
        v = getattr(tp, key, None)
        if isinstance(v, str) and v:
            paths.append(v)
            if key == "git_dir":
                paths.append(v.rstrip("\\/").replace("/", "\\") + "\\usr\\bin")  # pylint: disable=use-system-path
                paths.append(v.rstrip("\\/").replace("/", "\\") + "\\bin")  # pylint: disable=use-system-path
    bash_p = getattr(tp, "bash_path", None)
    if isinstance(bash_p, str) and bash_p:
        paths.append(os.path.dirname(bash_p))
    # 去重保序.
    seen: set[str] = set()
    out: list[str] = []
    for p in paths:
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return out


def _load_policy_acl_paths(policy_path: str) -> list[str]:
    """从 windows-policy.yaml 读所有需要施加 ACE 的用户配置路径."""
    import yaml as _yaml
    from jiuwenbox.server.policy_engine import read_policy_text
    data = _yaml.safe_load(read_policy_text(policy_path)) or {}
    win_fs = ((data.get("windows") or {}).get("filesystem") or {})
    keys = ("allow_read", "deny_read", "allow_write", "deny_write")
    paths: list[str] = []
    for k in keys:
        for p in win_fs.get(k) or []:
            if isinstance(p, str) and p:
                paths.append(p)
    # 去重保序.
    seen: set[str] = set()
    out: list[str] = []
    for p in paths:
        ep = os.path.expandvars(p)
        if ep and ep not in seen:
            seen.add(ep)
            out.append(ep)
    return out


def _quote_arg(arg: str) -> str:
    """参数含空格/制表符时用双引号包裹 (Windows 命令行规则)."""
    if " " in arg or "\t" in arg:
        return f'"{arg}"'
    return arg


_PYTHON_EXE_NAMES = frozenset({"python.exe", "pythonw.exe", "python3.exe"})


def _looks_like_python_exe(path: str) -> bool:
    """True if basename is a CPython interpreter (UAC 将显示为 Python)."""
    return os.path.basename(path).lower() in _PYTHON_EXE_NAMES


def _prefer_pythonw(path: str) -> str:
    """python.exe 会弹控制台; 同目录有 pythonw.exe 时改用它 (UAC 仍显示 Python)."""
    name = os.path.basename(path).lower()
    if name in {"python.exe", "python3.exe"}:
        sibling = os.path.join(os.path.dirname(path), "pythonw.exe")
        if os.path.isfile(sibling):
            return sibling
    return path


def _iter_bundled_python_candidates() -> list[str]:
    """随包 pythonw.exe/python.exe 候选路径 (CLAW_PYTHON_HOME / dataDir/runtime/python-win)."""
    candidates: list[str] = []
    for env_key in ("JIUWENCLAW_BASE_PYTHON",):
        raw = (os.environ.get(env_key) or "").strip()
        if raw:
            candidates.append(raw)
    for env_key in ("CLAW_PYTHON_HOME", "JIUWENBOX_BUNDLED_PYTHON"):
        raw = (os.environ.get(env_key) or "").strip()
        if raw:
            candidates.append(raw)
            candidates.append(os.path.join(raw, "pythonw.exe"))
            candidates.append(os.path.join(raw, "python.exe"))
    for data_key in ("JIUWENSWARM_DATA_DIR", "JIUWENSWARM_HOME"):
        data_dir = (os.environ.get(data_key) or "").strip()
        if not data_dir:
            continue
        runtime_dir = Path(data_dir).expanduser() / ".." / "runtime" / "python-win"
        candidates.append(str(runtime_dir / "pythonw.exe"))
        candidates.append(str(runtime_dir / "python.exe"))
    return candidates


def _resolve_uac_python() -> str | None:
    """找随包 python.exe 作为 UAC 展示名 (冻包 payload 是 JiuwenSwarm.exe)."""
    seen: set[str] = set()
    for cand in _iter_bundled_python_candidates():
        try:
            resolved = str(Path(cand).expanduser().resolve())
        except (OSError, RuntimeError):
            continue
        key = os.path.normcase(resolved)
        if key in seen or not os.path.isfile(resolved):
            continue
        seen.add(key)
        if _looks_like_python_exe(resolved):
            return _prefer_pythonw(resolved)
    return None


def _write_uac_python_launcher(payload_exe: str, payload_args: list[str]) -> str:
    """临时脚本: 提权后的 python.exe 再拉起冻包 exe 跑 win_setup."""
    import tempfile

    argv = [payload_exe, *payload_args]
    body = (
        "import os, subprocess, sys\n"
        f"_argv = {argv!r}\n"
        "if sys.platform == 'win32':\n"
        "    try:\n"
        "        import ctypes\n"
        "        ctypes.windll.kernel32.FreeConsole()\n"
        "    except Exception:\n"
        "        pass\n"
        "_kw = {}\n"
        "if sys.platform == 'win32':\n"
        "    _kw['creationflags'] = getattr(subprocess, 'CREATE_NO_WINDOW', 0x08000000)\n"
        "try:\n"
        "    raise SystemExit(int(subprocess.call(_argv, **_kw)))\n"
        "finally:\n"
        "    try:\n"
        "        os.remove(__file__)\n"
        "    except OSError:\n"
        "        pass\n"
    )
    fd, path = tempfile.mkstemp(prefix="jbx-uac-", suffix=".py")
    try:
        os.write(fd, body.encode("utf-8"))
    finally:
        os.close(fd)
    return path


def _uac_shell_execute_target(payload_exe: str, payload_args: list[str]) -> tuple[str, str]:
    """ShellExecuteW 的 (file, params); 能找到 python.exe 时用它做 UAC 展示名."""
    direct_params = " ".join(_quote_arg(p) for p in payload_args)
    uac_py = _resolve_uac_python()
    if not uac_py:
        logger.warning(
            "未找到随包 python.exe, UAC 将显示 %s; 请确认桌面 runtime/python-win 已解压",
            payload_exe,
        )
        return payload_exe, direct_params
    try:
        same = os.path.normcase(str(Path(uac_py).resolve())) == os.path.normcase(
            str(Path(payload_exe).resolve())
        )
    except (OSError, RuntimeError):
        same = False
    if same:
        return uac_py, direct_params
    launcher = _write_uac_python_launcher(payload_exe, payload_args)
    logger.info(
        "UAC 展示 python=%s, 实际 install payload=%s, launcher=%s",
        uac_py, payload_exe, launcher,
    )
    return uac_py, _quote_arg(launcher)


def _elevate_and_run_install(
    force: bool = False,
    preinstall_paths: list[str] | None = None,
    proxy_ports: tuple[int, int] = (
        const.DEFAULT_PROXY_PORT_RANGE_START,
        const.DEFAULT_PROXY_PORT_RANGE_END,
    ),
    policy_path: str | None = None,
    recreate_user: bool = False,
) -> int:
    """通过 UAC 拉起提权子进程执行 install, 并同步阻塞等待其完成.

    转发 force / recreate_user / preinstall_paths / proxy_ports / policy_path
    到提权子进程 (review MAJOR #9: 旧版只传 --install, force=True 会静默 no-op).

    同步机制: 沿用旧版能正常弹 UAC 的 ShellExecuteW (它返回 HINSTANCE, 拿不到
    子进程 handle, 无法直接 WaitForSingleObject 等进程). 改用一个命名 Event:
    主进程 CreateEventW 创建非信号态 Event, 名字经 --install-done-event 参数传
    给 install 子进程; ShellExecuteW 弹 UAC 拉起子进程后, 主进程
    WaitForSingleObject(event, INFINITE) 阻塞; install 子进程在 install() 跑完
    所有步骤 (用户/密码/installed 标记/PREINSTALLED_PATHS 全部就位) 后、return
    前 SetEvent 通知主进程. 这样主进程继续往下时 install 已真完成, 不会再用半
    成品密码 CreateProcessWithLogonW → 1326 (旧版异步返回的根因).

    用户取消 UAC 弹窗 (点"否"): ShellExecuteW 返回 <= 32, WinError 1223
    (ERROR_CANCELLED). 抛 RuntimeError 提示用户重试 (而非静默返回 0).
    """
    shell32 = _get_shell32()
    kernel32 = get_kernel32()
    # 冻结 exe 直接 runas 自身: pythonw launcher 会被桌面 taskkill /T 杀掉.
    if getattr(sys, "frozen", False):
        py = sys.executable
        parts = ["--desktop-run-win-setup", const.INSTALL_SUBCOMMAND]
    else:
        py = (os.environ.get("JIUWENBOX_RUNNER_PYTHON") or "").strip() or sys.executable
        parts = ["-m", "jiuwenbox.supervisor.win_setup", const.INSTALL_SUBCOMMAND]
    # 每次调用生成唯一 event 名, 避免多 box-server 实例/并发 install 串扰.
    event_name = f"Global\\JiuwenBox-Install-Done-{secrets.token_hex(8)}"
    # 主进程先创建非信号态 Event (子进程将 OpenEvent 同名并 SetEvent).
    # Manual reset=False (自动复位), initial=非信号. lpSecurityAttributes=None
    # (Global 命名空间默认 ACL 允许同会话/管理员子进程打开).
    h_event = kernel32.CreateEventW(None, False, False, event_name)
    if not h_event:
        err = ctypes.get_last_error()
        raise RuntimeError(
            f"创建 install 同步 Event 失败 (CreateEventW WinError {err})"
        )

    if force:
        parts.append("--force")
    if recreate_user:
        parts.append("--recreate-user")
    parts.append("--proxy-port-start")
    parts.append(str(proxy_ports[0]))
    parts.append("--proxy-port-end")
    parts.append(str(proxy_ports[1]))
    # 把完成 Event 名传给子进程, 子进程 install 跑完后 SetEvent 通知本进程.
    parts.append("--install-done-event")
    parts.append(event_name)
    # install_force.log 落盘到用户数据目录 (~/.office-claw/.jiuwenclaw/jiuwenbox),
    # 子进程需显式接收路径, 否则会 fallback 回包目录写日志.
    parts.append("--install-log-path")
    parts.append(_install_log_path())
    if preinstall_paths:
        # 用 base64(JSON) 编码列表传参: json.dumps 含内层双引号, Windows 命令行解析会把内层 " 当闭合, 含空格路径被拆成多 argv →
        # 子进程 argparse 报错退出, 到不了 install/SetEvent. base64 后是纯字母, 彻底避开命令行转义.
        import base64
        encoded = base64.b64encode(
            json.dumps(preinstall_paths).encode("utf-8")
        ).decode("ascii")
        parts.append("--preinstall-paths")
        parts.append(encoded)
    if policy_path:
        parts.append("--policy-path")
        parts.append(policy_path)
    if getattr(sys, "frozen", False) and py == sys.executable:
        uac_file, params = py, " ".join(_quote_arg(p) for p in parts)
    else:
        uac_file, params = _uac_shell_execute_target(py, parts)

    logger.info(
        "install 提权调用: uac=%s payload=%s event=%s cmd='%s %s'",
        uac_file, py, event_name, uac_file, params,
    )

    # ShellExecuteW(parent, verb, file, parameters, directory, show).
    # SW_UAC_SHOW: 弹出可见 UAC 确认框 (SW_HIDE 时用户看不到, 120s 超时导致沙箱/AgentServer 反复重启).
    result = shell32.ShellExecuteW(None, "runas", uac_file, params, None, SW_UAC_SHOW)
    logger.info(
        "ShellExecuteW(runas, SW_UAC_SHOW) 返回 %s (>32=已发起 UAC, 不代表用户已点确认)",
        result,
    )
    if result <= 32:  # <= 32 表示失败.
        kernel32.CloseHandle(wintypes.HANDLE(h_event))
        err = ctypes.get_last_error()
        if err == 1223:  # ERROR_CANCELLED: 用户点了"否".
            raise RuntimeError(
                "UAC 提权被用户取消; 请重新创建沙箱并同意 UAC, 或以管理员身份"
                " 手动运行 'python -m jiuwenbox.supervisor.win_setup --install'"
            )
        raise RuntimeError(
            f"UAC 提权失败 (ShellExecuteW 返回 {result}, WinError {err}); 请以"
            f"管理员身份手动运行 'python -m jiuwenbox.supervisor.win_setup --install'"
        )

    # 阻塞等待 install 跑完 SetEvent (Event 自动复位). P1-10: 120s 超时兜底, 旧版 INFINITE 在子进程
    # SetEvent 前崩溃会永久阻塞. 超时不 raise — install 可能仍在跑 (慢机器/用户离开 UAC), 降级 warning 让上层按 installed 标记判断.
    wait_obj_0 = 0  # noqa: N806 - Win32 常量
    wait_timeout = 0x00000102  # noqa: N806 - Win32 常量
    wait_failed = 0xFFFFFFFF  # noqa: N806 - Win32 常量
    install_wait_timeout = 120_000  # noqa: N806 - Win32 常量
    logger.info("已通过 UAC 提权运行 install 子进程 (force=%s), 阻塞等待完成 (超时 %ds)...", force, install_wait_timeout // 1000)
    try:
        wait_result = kernel32.WaitForSingleObject(
            wintypes.HANDLE(h_event), install_wait_timeout,
        )
        if wait_result == wait_failed:
            err = ctypes.get_last_error()
            raise RuntimeError(
                f"等待 install 子进程完成失败 (WaitForSingleObject WinError {err})"
            )
        if wait_result == wait_timeout:
            # 未在 120s 内 SetEvent. 不 raise — 可能仍在跑或已崩溃, 降级 warning, 上层按 installed 标记判断.
            logger.warning(
                "install 提权子进程 %ds 未完成 SetEvent (超时降级). install 可能仍在"
                "运行或已崩溃; 后续将按 installed 标记判断, 若失败请重试",
                install_wait_timeout // 1000,
            )
        elif wait_result != wait_obj_0:
            raise RuntimeError(
                f"等待 install 子进程返回意外值 {wait_result:#x}"
            )
        else:
            logger.info("install 提权子进程已 SetEvent, 视为完成")
        # SetEvent 只表示执行流到 install() 末尾, 不代表退出码. 致命步骤失败会 raise 走回滚, 靠超时兜底.
    finally:
        kernel32.CloseHandle(wintypes.HANDLE(h_event))
    return 0


# ---------------------------------------------------------------------------
# 读 ACL 预装 (异步后台线程).
# ---------------------------------------------------------------------------
def _preinstall_read_acl(
    paths: list[str], sid: str, skip_paths: "set[str] | None" = None,
) -> None:
    """对指定路径列表递归施加 Allow Read ACE (合成/沙箱用户 SID).

    Args:
        paths: 待预装读 ACL 的路径列表.
        sid: 沙箱用户 SID 字符串.
        skip_paths: 主线程已合并授权的路径集 (normcase 规范化), 这些路径
            的 GENERIC_READ ACE 已由 grant_aces 合并写入, 直接跳过省一遍递归.
            跳过时仍记入断点续传 done, 保证重装时幂等.
    """
    from jiuwenbox.supervisor import win_acl
    skip_norm = {os.path.normcase(p) for p in (skip_paths or [])}
    # 读已完成进度, 跳过已完成路径.
    done: set[str] = set()
    raw_progress = reg_get_str(const.REG_VALUE_READ_ACL_PROGRESS)
    if raw_progress:
        try:
            done = set(json.loads(raw_progress))
        except (ValueError, TypeError):
            done = set()
    try:
        for path in paths:
            expanded = os.path.expandvars(path)
            if expanded in done:
                logger.debug("预装读 ACL: 已完成, 跳过 %s", expanded)
                continue
            if not os.path.exists(expanded):
                logger.debug("预装读 ACL: 路径不存在 %s", expanded)
                continue
            if os.path.normcase(expanded) in skip_norm:
                # 主线程 grant_aces 已合并写入该路径的 GENERIC_READ ACE,
                # 直接记入 done (断点续传幂等), 不再重复递归施加.
                done.add(expanded)
                _reg_set_str(
                    const.REG_VALUE_READ_ACL_PROGRESS,
                    json.dumps(sorted(done)),
                )
                logger.debug("预装读 ACL: 已由主线程合并授权, 跳过 %s", expanded)
                continue
            # Allow Read+Execute ACE 给沙箱用户 SID (python.exe / DLL 需要 Execute).
            win_acl.grant_ace(
                expanded, sid,
                rights=const.ALLOW_READ_EXECUTE_RIGHTS,
                mode="ALLOW",
                recursive=True,
            )
            done.add(expanded)
            # 每完成一个路径写一次进度, 支持断点续传.
            _reg_set_str(
                const.REG_VALUE_READ_ACL_PROGRESS,
                json.dumps(sorted(done)),
            )
            logger.debug("预装读 ACL 完成: %s", expanded)
    except Exception:  # noqa: BLE001 - best-effort
        logger.warning("预装读 ACL 失败", exc_info=True)


def _preinstall_read_acl_async(
    paths: list[str], sid: str, skip_paths: "set[str] | None" = None,
) -> threading.Thread:
    """后台线程预装读 ACL.

    返回线程对象供调用方 join. install() 作为提权子进程必须等预装完成再
    退出, 否则 daemon 线程被强杀, 预装只做一半.
    """
    thread = threading.Thread(
        target=_preinstall_read_acl,
        args=(paths, sid, skip_paths),
        name="jbx-read-acl-preinstall",
        daemon=True,
    )
    thread.start()
    logger.info(
        "读 ACL 预装已在后台线程启动 (%d 路径, %d 已合并跳过)",
        len(paths), len(skip_paths or []),
    )
    return thread


# 预装读 ACL 的等待上限 (秒). 深度遍历大目录可能耗时, 但 install 作为
# 提权子进程不应无限挂起; 超时后写 installed 标记, 剩余路径由后续 ensure
# 补做 (grant_ace 幂等).
PREINSTALL_JOIN_TIMEOUT_SECONDS = 120.0  # noqa: N816 - Win32 常量


# ---------------------------------------------------------------------------
# 公开 API.
# ---------------------------------------------------------------------------
def install(
    force: bool = False,
    preinstall_paths: list[str] | None = None,
    proxy_ports: tuple[int, int] = (
        const.DEFAULT_PROXY_PORT_RANGE_START,
        const.DEFAULT_PROXY_PORT_RANGE_END,
    ),
    policy_path: str | None = None,
    recreate_user: bool = False,
) -> None:
    """执行一次性安装 (需管理员权限).

    Args:
        force: 忽略幂等标记强制重装.
        preinstall_paths: 读 ACL 预装路径 (来自根 policy 的
            ``windows.filesystem.read_acl_preinstall``). 为 None 时用
            默认 4 个系统目录.
        proxy_ports: (start, end) WFP Permit filter 放行的 loopback 端口范围
            (来自根 policy 的 ``windows.proxy.port_range_*``). 必须与
            win_proxy 实际监听端口一致, 否则代理路径被 Block 拦截
            (review MAJOR #7: 旧版硬编码默认端口, 忽略 policy).
        policy_path: windows-policy.yaml 路径; install 时读其 read_acl_preinstall
            + tool_paths 合并进预装路径. 用户改 tool_paths 后 --force 重装用.
        recreate_user: True 时无论机器上是否已有沙箱, 都先删 jbx-sandbox
            用户/组/profile 再重建. 桌面安装器每次重装 exe 传入; 运行时
            --force 补预装不传, 以免误删正在使用的账户.
    """
    _require_windows()
    # 若给了 policy_path, 读其 read_acl_preinstall + tool_paths 合并进预装路径.
    # 用户改 tool_paths 后 --force --policy-path <yaml> 重装即可预装新工具目录
    # (运行时普通用户无权改这些目录 DACL, 必须管理员预装).
    if policy_path:
        try:
            _pp = _load_policy_preinstall_paths(policy_path)
            if _pp:
                preinstall_paths = (preinstall_paths or []) + _pp
        except Exception as exc:  # noqa: BLE001
            logger.warning("install 读 policy 预装路径失败: %s", exc)
    if not _is_admin():
        _elevate_and_run_install(
            force=force,
            preinstall_paths=preinstall_paths,
            proxy_ports=proxy_ports,
            policy_path=policy_path,
            recreate_user=recreate_user,
        )
        return

    # exe 重装必须跳过幂等: 已 installed=1 也要删用户重建.
    if recreate_user:
        force = True

    # 幂等检查.
    if not force and reg_get_str(const.REG_VALUE_INSTALLED) == "1":
        logger.info("Windows 沙箱已安装, 跳过 (force=True 可重装)")
        return

    logger.info("开始 Windows 沙箱安装...")

    # install 主体 try/except, 致命步骤失败时调 _install_rollback 局部回滚并 raise,
    # 不写 installed=1 (旧版 best-effort 假成功).
    # 致命: 用户+组创建、网络隔离至少一条路成功 (WFP 或降级). 非致命: 隐藏用户、DPAPI、合成 SID 缓存、读 ACL 预装.
    # review #4: 失败回滚只清本次已成功步骤新增的资源 (用户/组/WFP filter), 不调
    # uninstall() 全量清理 (会删 jbx-sandbox 用户/profile, 影响并发运行的其他沙箱).
    steps_done: set[str] = set()
    try:
        # 1. 创建用户 + 组 (致命).
        if recreate_user:
            steps_done.add("recreate_user")
            logger.info(
                "exe 重装: 无论是否已有沙箱, 先删除 jbx-sandbox 用户/组/profile 再重建",
            )
            if _sandbox_user_exists():
                try:
                    logger.info("重装前卸载 WFP/防火墙…")
                    from jiuwenbox.supervisor import win_wfp
                    win_wfp.uninstall_wfp_filters()
                    win_wfp.uninstall_firewall_rule_fallback()
                    logger.info("重装前 WFP/防火墙已卸载")
                except Exception:  # noqa: BLE001
                    logger.warning("重装前卸载 WFP/防火墙失败", exc_info=True)
                logger.info("开始删除已有 jbx-sandbox 用户/profile")
                _remove_sandbox_user_and_profile()
                logger.info("已有 jbx-sandbox 删除流程结束")
            else:
                logger.info("jbx-sandbox 已不存在, 跳过删除/卸 WFP, 直接新建")
                _reg_set_str(const.REG_VALUE_INSTALLED, "")
                _reg_set_str(const.REG_VALUE_SANDBOX_USER_PW, "")
                _reg_set_str(const.REG_VALUE_SANDBOX_USER_SID, "")
        new_password = _generate_password()
        created = _create_sandbox_user(
            new_password, retries=5 if recreate_user else 1,
        )
        if recreate_user and not created:
            # 用户仍被占用时 NetUserDel 可能没删掉, 再删一次后重建.
            logger.warning("删除后沙箱用户仍存在, 再次尝试删除并重建")
            _remove_sandbox_user_and_profile()
            created = _create_sandbox_user(new_password, retries=5)
        if created:
            # 新建用户: 用本次随机密码存 DPAPI.
            password = new_password
            steps_done.add("user_created")  # 本次新建, 失败回滚删用户安全
        elif recreate_user:
            # 删不掉 (进程占用等): 重设密码并对齐本次 DPAPI, 避免新用户解不开旧密文.
            # 若账户其实已删 (pending-delete 后 GetInfo/SetInfo 变 2221), 改为再创建.
            password = new_password
            try:
                _set_user_password(const.SANDBOX_USER_NAME, password)
                logger.warning(
                    "无法删除已有 jbx-sandbox 用户, 已重设密码并对齐 DPAPI",
                )
                steps_done.add("user_existed")
            except Exception as exc:  # noqa: BLE001
                if f"ret={NERR_USER_NOT_FOUND}" not in str(exc):
                    raise
                logger.warning(
                    "删除后 NetUserSetInfo 用户不存在 (2221), 改为新建",
                )
                created = _create_sandbox_user(new_password, retries=5)
                if not created:
                    raise RuntimeError(
                        "无法重建 jbx-sandbox 用户: 删除后账户仍不存在或 "
                        "处于 pending-delete. " + _SANDBOX_USER_REPAIR
                    ) from exc
                password = new_password
                steps_done.add("user_created")
        else:
            # 用户已存在: 不能用本次新密码 (与实际不一致 → 1326). 读 DPAPI 旧密码保持一致;
            # 读不到 (旧版 "000000" 升级/DPAPI 损坏) 则用新随机密码 NetUserSetInfo 重设对齐.
            password = get_sandbox_user_password()
            if not password:
                password = new_password
                _set_user_password(const.SANDBOX_USER_NAME, password)
                logger.info("jbx-sandbox DPAPI 无旧密码, 已重设为新随机密码")
            # 用户已存在 (force 重装): 失败回滚不删用户 (避免误删既有账户影响并发沙箱).
            steps_done.add("user_existed")
        _add_user_to_group()
        # 从登录界面隐藏 jbx-sandbox 用户 (非致命).
        try:
            _reg_set_dword_under(
                r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon"
                r"\SpecialAccounts\UserList",
                const.SANDBOX_USER_NAME,
                0,
            )
        except Exception:  # noqa: BLE001
            logger.warning("隐藏登录界面用户失败, 不影响功能", exc_info=True)

        # 2. 查 SID 并存注册表.
        sid = _lookup_user_sid(const.SANDBOX_USER_NAME)
        _reg_set_str(const.REG_VALUE_SANDBOX_USER_SID, sid)
        _save_sandbox_user_password(password)

        # 合成 SID 缓存.
        from jiuwenbox.supervisor import win_acl
        synth_sid = win_acl.get_synthetic_write_sid()
        _reg_set_str(const.REG_VALUE_SYNTHETIC_WRITE_SID, synth_sid)

        # 3. 安装 WFP filter set (网络隔离). 失败则降级到防火墙规则 (致命:
        # WFP 主路径 + 降级路径都失败 = 网络隔离不可用, raise 触发回滚)。
        network_isolation_ok = False
        wfp_exc = None
        try:
            from jiuwenbox.supervisor import win_wfp
            win_wfp.install_wfp_filters(sid, proxy_ports[0], proxy_ports[1])
            network_isolation_ok = True
        except Exception as exc:  # noqa: BLE001
            wfp_exc = exc
            logger.warning(
                "WFP filter 安装失败, 降级到 PowerShell 防火墙规则", exc_info=True,
            )
            try:
                from jiuwenbox.supervisor import win_wfp
                fallback_ok = win_wfp.install_firewall_rule_fallback(
                    const.SANDBOX_USER_NAME, proxy_ports[0], proxy_ports[1],
                    sandbox_user_sid=sid,
                )
            except Exception as fallback_exc:  # noqa: BLE001
                logger.error(
                    "防火墙规则降级也失败, 网络隔离不可用", exc_info=True,
                )
                raise RuntimeError(
                    "网络隔离不可用: WFP 主路径与 PowerShell 降级均失败"
                ) from (exc, fallback_exc)
            if not fallback_ok:
                raise RuntimeError(
                    "网络隔离不可用: WFP 主路径失败, PowerShell 降级部分失败"
                ) from exc
            network_isolation_ok = True
        if not network_isolation_ok:
            # 不应到达此处 (上面任一路径失败已 raise), 防御性兜底。
            raise RuntimeError("网络隔离不可用 (未知原因)") from wfp_exc
        steps_done.add("wfp")  # 网络隔离已装 (WFP 或降级), 失败回滚需卸载

        # 4. 异步预装读 ACL (非致命: S11 修后默认不 preinstall 系统目录,
        # 只 grant 调用方显式传入的 preinstall_paths; 失败不影响 install 成功,
        # 沙箱工作区读权限由 _create_windows 的 apply_sandbox_acl 在创建时 grant)。
        if preinstall_paths:
            paths_to_preinstall = [
                os.path.expandvars(p) for p in preinstall_paths if p
            ]
        else:
            paths_to_preinstall = []
        # 对 ~/.office-claw 整树递归授 Read ACL: workspace/venv/.ms-playwright/业务产物都在其子树, jbx-sandbox 需读.
        # 用 Path.home()/.office-claw (与 relay-claw 同算法), 不依赖 env/常量解析 (避免 env 缺 JIUWENCLAW_DATA_DIR 算错路径 → EPERM).
        # 只 grant Read 不 grant Write (P0-3): 整树 Write 是跨沙箱互写/deny_write 失效/副本可篡改的根因, Write 由子树运行时单独精确授权.
        # 递归 Read 让各沙箱能互相读 workspace — 单用户本地部署可接受, 跨沙箱写靠精确 grant 隔离.
        # review #5: 递归 grant 移到 install 一次施加 (合成 SID + 真实 sandbox 用户 SID 各一份),
        # 不再每次建沙箱补授 (旧版每次追加一份 ACE 致 DACL 膨胀). 真实 SID 也需读
        # venv python/DLL (runner 第一跳用真实 SID, 合成 SID 的 ACE 对它不生效).
        try:
            from jiuwenbox.supervisor import win_acl as _wa, win_constants as _wc
            _office_claw_root = str(Path.home() / ".office-claw")
            os.makedirs(_office_claw_root, exist_ok=True)
            # 递归 grant Read+Execute: 合成 SID + 真实 sandbox 用户 SID 各一份.
            _wa.grant_ace(
                _office_claw_root, synth_sid,
                rights=_wc.ALLOW_READ_EXECUTE_RIGHTS, mode="ALLOW", recursive=True,
            )
            _wa.grant_ace(
                _office_claw_root, sid,
                rights=_wc.ALLOW_READ_EXECUTE_RIGHTS, mode="ALLOW", recursive=True,
            )
            logger.info("预装数据根递归 Read ACL: %s (Write 由运行时子树单独授权)", _office_claw_root)
        except Exception as exc:  # noqa: BLE001
            logger.warning("预装数据根 ACL 失败 (非致命): %s", exc)

        # bundled-python 目录 (tools/python, tools/node 等) owner 可能是 Administrators,
        # 运行时 box-server 进程 (普通用户) 没有 WRITE_DAC, apply_sandbox_acl
        # 实施 Allow Read ACE 时 WinError 5. install 阶段给当前用户加上 DACL 最小
        # 权限 (READ_CONTROL 读 SD + WRITE_DAC 写 DACL), 运行时能改这些目录的 DACL.
        _current_user_sid = _lookup_current_user_sid()
        # 主线程合并授权的读 ACL 路径集: 打包目录同时 grant WRITE_DAC (当前用户)
        # 和 GENERIC_READ (沙箱用户), 后台预装线程对这些路径直接跳过, 省一遍重复递归.
        _merged_read_paths: set[str] = set()
        if _current_user_sid:
            _bundled_dirs: list[str] = []
            _env_bundled = (os.environ.get("JIUWENBOX_BUNDLED_PYTHON") or "").strip()
            if _env_bundled:
                _bundled_dirs.append(_env_bundled)
            try:
                _exe_dir = str(Path(sys.executable).resolve().parent)
            except Exception:  # noqa: BLE001
                _exe_dir = ""
            if _exe_dir and _exe_dir not in _bundled_dirs:
                _bundled_dirs.append(_exe_dir)
            # 打包目录集与预装读 ACL 路径集的交集
            _preinstall_norm: set[str] = {
                os.path.normcase(os.path.expandvars(p)) for p in paths_to_preinstall if p
            }
            for _p in list(_bundled_dirs):
                if not os.path.isdir(_p):
                    logger.debug("打包工具目录不存在, 跳过预授权: %s", _p)
                    continue
                _p_norm = os.path.normcase(_p)
                _is_preinstall_target = _p_norm in _preinstall_norm
                try:
                    if _is_preinstall_target:
                        # 合并: WRITE_DAC 给当前用户 + GENERIC_READ 给沙箱用户, 一次 Get/Set.
                        _wa.grant_aces(
                            _p,
                            [
                                (_current_user_sid, _wc.WRITE_DAC | _wc.READ_CONTROL, "ALLOW"),
                                (sid, _wc.ALLOW_READ_EXECUTE_RIGHTS, "ALLOW"),
                            ],
                            recursive=True,
                        )
                        _merged_read_paths.add(_p_norm)
                        logger.info(
                            "grant WRITE_DAC|READ_CONTROL + GENERIC_READ (合并) 给当前用户+沙箱 (打包目录): %s",
                            _p,
                        )
                    else:
                        _wa.grant_ace(
                            _p, _current_user_sid,
                            rights=_wc.WRITE_DAC | _wc.READ_CONTROL,
                            mode="ALLOW", recursive=True,
                        )
                        logger.info("grant WRITE_DAC|READ_CONTROL 给当前用户 (打包目录): %s", _p)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("grant 失败 (打包目录, 非致命): %s=%s", _p, exc)

        # 用户 policy 的 deny/allow 路径 (allow_read/deny_read/allow_write/deny_write)
        _acl_paths: list[str] = []
        if _current_user_sid and policy_path:
            try:
                _acl_paths = _load_policy_acl_paths(policy_path)
            except Exception as exc:  # noqa: BLE001
                logger.warning("install 读 policy deny/allow 路径失败 (非致命): %s", exc)
            _granted_acl = 0
            for _p in _acl_paths:
                if not os.path.exists(_p):
                    logger.debug("policy deny/allow 路径不存在, 跳过预授权: %s", _p)
                    continue
                try:
                    _wa.grant_ace(
                        _p, _current_user_sid,
                        rights=_wc.WRITE_DAC | _wc.READ_CONTROL,
                        mode="ALLOW", recursive=True,
                    )
                    _granted_acl += 1
                    logger.info(
                        "grant WRITE_DAC|READ_CONTROL 给当前用户 (policy deny/allow): %s",
                        _p,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "grant WRITE_DAC 失败 (policy 路径, 非致命): %s=%s", _p, exc,
                    )
            if _acl_paths:
                logger.info(
                    "policy deny/allow 路径预授权完成: 总 %d 条, 成功 %d 条",
                    len(_acl_paths), _granted_acl,
                )
        preinstall_thread = _preinstall_read_acl_async(
            paths_to_preinstall, sid, skip_paths=_merged_read_paths,
        )
        preinstall_thread.join(timeout=PREINSTALL_JOIN_TIMEOUT_SECONDS)
        if preinstall_thread.is_alive():
            logger.warning(
                "读 ACL 预装未在 %.0fs 内完成, 剩余路径将由后续创建沙箱补做",
                PREINSTALL_JOIN_TIMEOUT_SECONDS,
            )

        # 5. 全部致命步骤通过, 写完成标记; 清进度标记 (断点续传已无用)。
        # 写 installed=1 前必须能用本次密码登录, 避免 installed=1 + 用户不存在/密码不对 → 运行时 1326.
        if not _logon_sandbox_user(password):
            raise RuntimeError(
                "jbx-sandbox 创建后 LogonUserW 失败, 不写 installed=1. "
                + _SANDBOX_USER_REPAIR
            )
        _reg_set_str(const.REG_VALUE_INSTALLED, "1")
        _reg_set_str(const.REG_VALUE_READ_ACL_PROGRESS, "")
        # 记录本次预装的路径集, 供 ensure_windows_setup 增量检测: 用户改了
        # tool_paths 后首次起 sandbox 时对比出新增路径, 提示需 --force 重装
        # (运行时普通用户无 WRITE_DAC 权限改外部目录 ACL, 只能管理员补预装).
        _reg_set_str(
            const.REG_VALUE_PREINSTALLED_PATHS,
            json.dumps(sorted({os.path.expandvars(p) for p in paths_to_preinstall})),
        )
        # 记录本次已预授 WRITE_DAC 的 deny/allow 路径集, 供 ensure_windows_setup
        # 增量检测: 用户改 policy 新增 deny_read/deny_write 路径时自动弹 UAC 补授权.
        if _current_user_sid and policy_path and _acl_paths:
            _record_acl_policy_paths(
                {_normalize_acl_path(p) for p in _acl_paths if p},
            )
        logger.info("Windows 沙箱安装完成")
    except Exception:
        # review #4: 失败回滚只清本次已成功步骤新增的资源 (用户/组/WFP filter),
        # 不调 uninstall() 全量清理 (会物理删 jbx-sandbox 用户 + profile,
        # 影响并发运行的其他沙箱 runner — 其 token 仍有效但下次
        # CreateProcessWithLogonW WinError 1326/1788, NTUSER.DAT 写失败).
        # profile 不在局部回滚清理 (由显式 uninstall() / install --force 调).
        logger.error("install 失败, 执行局部回滚", exc_info=True)
        try:
            _install_rollback(steps_done)
        except Exception:  # noqa: BLE001 - 回滚 best-effort, 不掩盖原异常
            logger.error("install 失败后局部回滚也失败", exc_info=True)
        raise


def collect_preinstall_paths(policy) -> list[str]:
    """收集 install 该预装读 ACL 的完整路径集.

    = policy.windows.filesystem.read_acl_preinstall (系统目录, 静态)
    + policy.windows.filesystem.tool_paths 展开的目录 (Git/Node/Python 安装路径,
    每台机不同; 含 git_dir 的 usr/bin + bin 子目录, bash_path 的父目录).

    lifespan (app.py) 和 _create_windows (process.py) 两处调
    ensure_windows_setup 时都用本函数算 preinstall_paths, 保证两处传的集合一致
    → install 记录进 REG_VALUE_PREINSTALLED_PATHS 的集合和后续创建沙箱时比对
    的集合相同 → 不会因 tool_paths "新增" 而每次创建沙箱都弹 UAC (实测问题).

    tool_paths 是 owner=Administrators 的外部目录, 运行时普通用户无 WRITE_DAC
    权限改不了它们的 ACL, 必须 install 提权预装. read_acl_preinstall 同理 (系统
    目录). 故本集合只含这两类"需提权预装"的路径, 不含 workspace/venv (那些
    owner=当前用户, 会话时 apply_sandbox_acl 普通权限即可授权).
    """
    fs = policy.windows.filesystem
    paths: list[str] = list(fs.read_acl_preinstall or [])
    tp = fs.tool_paths
    # git_dir / node_dir / python_dir
    for i, attr in enumerate(("git_dir", "node_dir", "python_dir")):
        d = (getattr(tp, attr, "") or "").strip()
        if not d:
            continue
        if d not in paths:
            paths.append(d)
        # git 安装根含 usr/bin/bash.exe + bin, 子目录也纳入 (PATH + 读 ACL).
        if attr == "git_dir":
            for sub in (os.path.join(d, "usr", "bin"), os.path.join(d, "bin")):
                if sub not in paths:
                    paths.append(sub)
    # bash_path 的父目录 (git_dir 未覆盖时用)
    bash_p = (getattr(tp, "bash_path", "") or "").strip()
    if bash_p:
        parent = os.path.dirname(bash_p)
        if parent and parent not in paths:
            paths.append(parent)
    return paths


def _record_acl_policy_paths(paths: set[str]) -> None:
    """把已处理 (预授或当前用户可自授权) 的 deny/allow 路径记入缓存, 避免重复 UAC."""
    if not paths:
        return
    normalized = {_normalize_acl_path(p) for p in paths if p}
    normalized = {p for p in normalized if p}
    if not normalized:
        return
    merged = _get_acl_policy_paths_recorded() | normalized
    if merged == _get_acl_policy_paths_recorded():
        return
    # 用户目录缓存: 普通进程可写, 避免 HKLM 写入失败导致每次重启都弹 UAC.
    _save_user_acl_policy_paths(merged)
    try:
        _reg_set_str(
            const.REG_VALUE_ACL_POLICY_PATHS,
            json.dumps(sorted(merged)),
        )
    except Exception as exc:  # noqa: BLE001 - 非管理员无 HKLM 写权限时回落用户缓存
        logger.debug("HKLM acl_policy_paths 写入跳过 (无管理员权限): %s", exc)


def ensure_acl_policy_paths_authorized(
    acl_paths: list[str],
    proxy_port_start: int = const.DEFAULT_PROXY_PORT_RANGE_START,
    proxy_port_end: int = const.DEFAULT_PROXY_PORT_RANGE_END,
    policy_path: str | None = None,
) -> None:
    """确保 deny/allow 路径已预授 WRITE_DAC.

    _create_windows 创建沙箱时调. 运行时不再弹 UAC: 未授权且当前不是管理员
    时只记日志; 已是管理员则本进程只补授 WRITE_DAC, 不整段 install()
    (避免每个新 workspace 都重装 WFP, 首次对话卡 30s+).
    """
    _require_windows()
    if reg_get_str(const.REG_VALUE_INSTALLED) != "1":
        return
    acl_recorded = _get_acl_policy_paths_recorded()
    new_acl_paths = {
        _normalize_acl_path(p) for p in acl_paths if p
    } - acl_recorded
    new_acl_paths = {p for p in new_acl_paths if p}
    if not new_acl_paths:
        return
    # 未展开的占位 / 当前用户已是 owner 的路径不弹 UAC:
    # per-sandbox workspace 每次都是新路径, 否则每个沙箱都要等 120s UAC.
    need_elevate: set[str] = set()
    skipped: list[str] = []
    for p in new_acl_paths:
        # 占位未展开 (如 {{ workspace }}) 的路径无法判定, 跳过.
        if "{{" in p:
            skipped.append(p)
            continue
        # %VAR% 环境变量风格且未展开的路径 (expandvars 后不存在) 跳过.
        if p.startswith("%") and "%" in p[1:] and not os.path.exists(p):
            skipped.append(p)
            continue
        if _path_owned_by_current_user(p):
            skipped.append(p)
            continue
        need_elevate.add(p)
    if skipped:
        logger.info(
            "新增 deny/allow 路径无需 UAC (占位或当前用户所有): %s",
            skipped,
        )
        _record_acl_policy_paths(set(skipped))
    if not need_elevate:
        return
    if not _is_admin():
        logger.warning(
            "新增 deny/allow 路径未预授 WRITE_DAC, 运行时不再弹 UAC: %s. "
            "请重新运行安装包以管理员补授权, 否则 grant_ace 可能 WinError 5.",
            sorted(need_elevate),
        )
        return
    logger.info(
        "检测到新增 deny/allow 路径未预授 WRITE_DAC: %s. "
        "当前进程已是管理员, 本进程只补授 WRITE_DAC (不整段重装).",
        sorted(need_elevate),
    )
    _grant_write_dac_to_current_user(need_elevate)


def _grant_write_dac_to_current_user(paths: set[str]) -> None:
    """给当前用户对 paths 预授 WRITE_DAC|READ_CONTROL, 不调 install()."""
    sid = _lookup_current_user_sid()
    if not sid:
        logger.warning("无法解析当前用户 SID, 跳过 WRITE_DAC 补授: %s", sorted(paths))
        return
    from jiuwenbox.supervisor import win_acl as _wa, win_constants as _wc
    granted: set[str] = set()
    for p in sorted(paths):
        if not os.path.exists(p):
            logger.debug("WRITE_DAC 路径不存在, 跳过: %s", p)
            continue
        try:
            _wa.grant_ace(
                p, sid,
                rights=_wc.WRITE_DAC | _wc.READ_CONTROL,
                mode="ALLOW", recursive=True,
            )
            granted.add(p)
            logger.info("grant WRITE_DAC|READ_CONTROL 给当前用户: %s", p)
        except Exception as exc:  # noqa: BLE001
            logger.warning("grant WRITE_DAC 失败 (非致命): %s=%s", p, exc)
    if granted:
        _record_acl_policy_paths(granted)


def ensure_windows_setup(
    force: bool = False,
    preinstall_paths: list[str] | None = None,
    proxy_port_start: int = const.DEFAULT_PROXY_PORT_RANGE_START,
    proxy_port_end: int = const.DEFAULT_PROXY_PORT_RANGE_END,
    policy_path: str | None = None,
) -> None:
    """运行时入口: 确保安装已完成 (幂等).

    由 ProcessRuntime.create / app.py lifespan 在 win32 分支调用.
    一次性准备由安装器 ``--desktop-run-win-setup --install --force --recreate-user`` 完成.
    尚未安装且非管理员时失败, 不弹 UAC.

    Args:
        preinstall_paths: 读 ACL 预装路径 (根 policy 的
            ``windows.filesystem.read_acl_preinstall``). 仅在首次安装时
            生效; 已安装则忽略.
        proxy_port_start/end: WFP Permit filter 放行的 loopback 端口范围
            (根 policy 的 ``windows.proxy.port_range_*``).
        policy_path: windows-policy.yaml 路径; 已安装时用于增量检测
            deny/allow 路径变更. 新增路径若当前不是管理员, 只记日志不弹 UAC.
    """
    _require_windows()
    try:
        if not force and reg_get_str(const.REG_VALUE_INSTALLED) == "1":
            # 幂等: 已安装. 但若 preinstall_paths 含已预装集合外的新路径 (用户改 tool_paths 后首次起 sandbox),
            # 运行时普通用户无权改外部目录 DACL → 新路径读 ACL 没预装 → 后续 _create_windows 改 ACL 会 WinError 5.
            # 这里检测出新增并提示用户 --force 重装.
            self_check_paths = {
                os.path.expandvars(p) for p in (preinstall_paths or []) if p
            }
            recorded_raw = reg_get_str(const.REG_VALUE_PREINSTALLED_PATHS)
            recorded: set[str] = set()
            if recorded_raw:
                try:
                    recorded = set(json.loads(recorded_raw))
                except (ValueError, TypeError):
                    recorded = set()
            new_paths = self_check_paths - recorded
            # 增量检测: 用户 policy 的 deny/allow 路径变更 (allow_read/deny_read/allow_write/deny_write).
            # install 阶段给这些路径预授 WRITE_DAC, 运行时 box-server 才能改 DACL 施加 Deny/Allow ACE.
            # 若 runtime policy 新增了路径但未预授 WRITE_DAC → grant_ace WinError 5 → Deny Read 不生效.
            new_acl_paths: set[str] = set()
            if policy_path:
                try:
                    acl_recorded = _get_acl_policy_paths_recorded()
                    new_acl_paths = {
                        _normalize_acl_path(p)
                        for p in _load_policy_acl_paths(policy_path)
                    } - acl_recorded
                    new_acl_paths = {p for p in new_acl_paths if p}
                except Exception as exc:  # noqa: BLE001
                    logger.debug("增量检测 deny/allow 路径变更失败 (非致命): %s", exc)
            # 当前用户已是 owner 的路径 (如 D:/y, D:/n) 启动时记入缓存即可.
            # 需管理员预授的路径推迟到创建沙箱时再弹 UAC (ensure_acl_policy_paths_authorized),
            # 避免「保存配置 → 重启 box-server」每次都打断用户.
            if new_acl_paths:
                _skip_owned: set[str] = set()
                _deferred_uac: set[str] = set()
                for _p in new_acl_paths:
                    if _path_owned_by_current_user(_p):
                        _skip_owned.add(_p)
                    else:
                        _deferred_uac.add(_p)
                if _skip_owned:
                    logger.info(
                        "deny/allow 路径无需 UAC (当前用户所有): %s",
                        sorted(_skip_owned),
                    )
                    _record_acl_policy_paths(_skip_owned)
                if _deferred_uac:
                    logger.info(
                        "需管理员预授的 deny/allow 路径 %s 推迟至创建沙箱时检查 "
                        "(运行时不弹 UAC; 无管理员权限则只记日志)",
                        sorted(_deferred_uac),
                    )
                new_acl_paths = set()
            if new_paths or new_acl_paths:
                if _is_admin():
                    logger.info(
                        "Windows 沙箱已安装, 检测到新增路径: 预装=%s, deny/allow=%s. "
                        "当前进程已是管理员, 本进程补预装 (不弹 UAC).",
                        sorted(new_paths), sorted(new_acl_paths),
                    )
                    install(
                        force=True,
                        preinstall_paths=sorted(new_paths),
                        proxy_ports=(proxy_port_start, proxy_port_end),
                        policy_path=policy_path,
                    )
                else:
                    logger.warning(
                        "Windows 沙箱已安装, 但检测到新增路径需管理员预装: "
                        "预装=%s, deny/allow=%s. 运行时不再弹 UAC; "
                        "请重新运行安装包, 或管理员执行 "
                        "jiuwenswarm.exe --desktop-run-win-setup --install --force.",
                        sorted(new_paths), sorted(new_acl_paths),
                    )
            # installed=1 不能保证账户还在 / 密码还能登录.
            _verify_or_reset_sandbox_user_password()
            return
        # installed != "1" 或 force=True. 非管理员走 install() 内的一次 UAC
        # (仅首次/损坏态; 已安装且用户存在不会到这里, 不会每次建沙箱弹框).
        install(
            force=force,
            preinstall_paths=preinstall_paths,
            proxy_ports=(proxy_port_start, proxy_port_end),
            policy_path=policy_path,
        )
    except Exception:  # noqa: BLE001
        logger.error("ensure_windows_setup 失败", exc_info=True)
        raise


def ensure_sandbox_user_can_logon() -> None:
    """创建沙箱前确认 jbx-sandbox 可登录; lifespan 失败时这里再拦一层."""
    _verify_or_reset_sandbox_user_password()


def _verify_or_reset_sandbox_user_password() -> None:
    """验证 jbx-sandbox 账户存在且密码与注册表一致. 账户缺失时提权重建一次."""
    if not _sandbox_user_exists():
        with _INSTALL_LOCK:
            if not _sandbox_user_exists():
                logger.warning(
                    "jbx-sandbox 用户不存在 (注册表可能仍为 installed=1), 尝试重建",
                )
                try:
                    install(force=True, recreate_user=True)
                except Exception as exc:  # noqa: BLE001
                    logger.error("重建 jbx-sandbox 失败. %s: %s", _SANDBOX_USER_REPAIR, exc)
                    raise RuntimeError(
                        "Windows sandbox user jbx-sandbox is missing "
                        "(installed=1 but the SAM account was deleted). "
                        + _SANDBOX_USER_REPAIR
                    ) from exc
                if not _sandbox_user_exists():
                    raise RuntimeError(
                        "Windows sandbox user jbx-sandbox is missing "
                        "(rebuild finished but the SAM account is still absent). "
                        + _SANDBOX_USER_REPAIR
                    )
    password = get_sandbox_user_password()
    if not password:
        return
    if _logon_sandbox_user(password):
        logger.debug("jbx-sandbox 密码一致性验证通过")
        return
    err = ctypes.get_last_error()
    # WinError 1326 = ERROR_LOGON_FAILURE (密码不一致或账户异常).
    logger.warning(
        "jbx-sandbox 密码验证失败 (LogonUserW WinError %d), 重设密码以对齐注册表",
        err,
    )
    try:
        _set_user_password(const.SANDBOX_USER_NAME, password)
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "jbx-sandbox 密码重设失败 (运行时非管理员可能无权). %s: %s",
            _SANDBOX_USER_REPAIR,
            exc,
        )
        raise RuntimeError(
            "jbx-sandbox logon failed and the password could not be reset. "
            + _SANDBOX_USER_REPAIR
        ) from exc
    if not _logon_sandbox_user(password):
        raise RuntimeError(
            "jbx-sandbox password was reset but LogonUserW still fails. "
            + _SANDBOX_USER_REPAIR
        )
    logger.info("jbx-sandbox 密码已重设, 与注册表一致")


def get_sandbox_user_sid() -> str | None:
    """从注册表读 jbx-sandbox 用户 SID."""
    _require_windows()
    return reg_get_str(const.REG_VALUE_SANDBOX_USER_SID)


def get_sandbox_user_password() -> str | None:
    """从注册表读 jbx-sandbox 用户密码 (DPAPI 解密). 解失败返回 None."""
    _require_windows()
    enc_hex = reg_get_str(const.REG_VALUE_SANDBOX_USER_PW)
    if not enc_hex:
        return None
    try:
        import win32crypt  # type: ignore[import-not-found]
        enc = bytes.fromhex(enc_hex)
        _, plain = win32crypt.CryptUnprotectData(enc, None, None, None, 0)
        return plain.decode("utf-8")
    except ImportError:  # pragma: no cover
        logger.warning("pywin32 缺失, 无法解密沙箱用户密码")
        return None
    except Exception:  # noqa: BLE001
        logger.warning("沙箱用户密码解密失败", exc_info=True)
        return None


def get_synthetic_write_sid() -> str | None:
    """从注册表读合成写 SID."""
    _require_windows()
    return reg_get_str(const.REG_VALUE_SYNTHETIC_WRITE_SID)


def _install_rollback(steps_done: "set[str]") -> None:
    """install 失败局部回滚: 只清本次已成功步骤新增的资源 (review #4).

    与 uninstall() 区别:
      - 不删 profile (DeleteProfileW / _purge_stale_profile_dirs): profile 删除
        是重操作, install 失败时其他并发沙箱的 runner 可能正用 NTUSER.DAT,
        删 profile 会让其写失败. profile 由显式 uninstall() / install --force 调.
      - 不清 REG_VALUE_SANDBOX_USER_PW / REG_VALUE_SANDBOX_USER_SID / SYNTHETIC_WRITE_SID:
        合成 SID 全局唯一保留 (reinstall 不变); DPAPI 密码 + SID 缓存留着,
        下次 install 已存在用户分支读旧密码对齐 (避免 1326).

    Args:
        steps_done: install 主体记录的已成功步骤集合, 含
            ``user_created`` / ``user_existed`` / ``recreate_user`` / ``wfp``.
    """
    _require_windows()
    if not _is_admin():
        # 局部回滚在 install 提权子进程内执行 (已是管理员); 非管理员路径不该到达.
        logger.warning("_install_rollback 非管理员, 跳过 (steps_done=%s)", steps_done)
        return
    # 回滚 WFP filter + 降级防火墙规则 (枚举法, 不依赖端口范围).
    if "wfp" in steps_done:
        try:
            from jiuwenbox.supervisor import win_wfp
            win_wfp.uninstall_wfp_filters()
            win_wfp.uninstall_firewall_rule_fallback()
        except Exception:  # noqa: BLE001
            logger.warning("局部回滚卸载 WFP/防火墙失败", exc_info=True)
    # 仅本次新建且非 recreate_user 才删用户. recreate 已删旧账户, 再删会留下
    # installed=1 但 SAM 无账户.
    if "user_created" in steps_done and "recreate_user" in steps_done:
        logger.info(
            "局部回滚: exe 重装已删旧账户, 保留本次新建的 %s, 避免机器无沙箱用户",
            const.SANDBOX_USER_NAME,
        )
    elif "user_created" in steps_done:
        netapi32 = _get_netapi32()
        ret = netapi32.NetUserDel(None, const.SANDBOX_USER_NAME)
        if ret not in (0, 2221):  # 2221 = NERR_UserNotFound (幂等)
            logger.warning("局部回滚 NetUserDel 返回 %d (继续)", ret)
        else:
            logger.info("局部回滚删除沙箱用户 %s", const.SANDBOX_USER_NAME)
        ret = netapi32.NetLocalGroupDel(None, const.SANDBOX_USER_GROUP)
        if ret not in (0, 2220):  # 2220 = NERR_GroupNotFound (幂等)
            logger.warning("局部回滚 NetLocalGroupDel 返回 %d (继续)", ret)
        else:
            logger.info("局部回滚删除沙箱组 %s", const.SANDBOX_USER_GROUP)
    elif "user_existed" in steps_done:
        logger.info("局部回滚: 用户已存在 (force 重装), 保留用户不删")
    # 清 installed 标记 (失败时本就没写 1, 兜底清空).
    _reg_set_str(const.REG_VALUE_INSTALLED, "")
    logger.info("install 局部回滚完成 (steps_done=%s)", steps_done)


def _data_root_paths() -> list[str]:
    """沙箱数据根路径 (apply 会 grant traverse, 不进差集清理)."""
    try:
        from jiuwenbox.server.workspace import (
            JIUWENBOX_HOME,
            JIUWENCLAW_DATA_DIR_PATH,
            OFFICE_CLAW_DATA_ROOT,
        )
        return [str(p) for p in (OFFICE_CLAW_DATA_ROOT, JIUWENCLAW_DATA_DIR_PATH, JIUWENBOX_HOME) if p]
    except Exception:  # noqa: BLE001
        return []


def _applied_acl_paths_file() -> Path:
    """施加路径历史清单的文件存储位置 (用户目录, 普通用户可写)."""
    try:
        from jiuwenbox.server.workspace import JIUWENBOX_HOME
        home = Path(JIUWENBOX_HOME)
    except Exception:  # noqa: BLE001
        home = Path(os.path.expanduser("~")) / ".jiuwenclaw" / "jiuwenbox"
    home.mkdir(parents=True, exist_ok=True)
    return home / "applied_acl_paths.json"


def _load_applied_acl_paths() -> list[str]:
    """读历史施加路径清单 (文件优先, 兼容旧注册表数据)."""
    f = _applied_acl_paths_file()
    if f.exists():
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return [p for p in data if isinstance(p, str) and p]
        except (ValueError, OSError):
            pass
    #
    raw = reg_get_str(const.REG_VALUE_APPLIED_ACL_PATHS)
    if raw:
        try:
            return [p for p in json.loads(raw) if isinstance(p, str) and p]
        except (ValueError, TypeError):
            pass
    return []


def _save_applied_acl_paths(paths: list[str]) -> None:
    """写历史施加路径清单到文件 (用户目录, 普通用户可写)."""
    f = _applied_acl_paths_file()
    try:
        f.write_text(json.dumps(paths, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        logger.warning("写施加路径历史文件失败 path=%s: %s", f, exc)


def record_applied_acl_paths(paths: list[str], workspace: str) -> None:
    """把 apply 施加过的非 workspace 路径追加进注册表历史清单 (去重)."""
    _require_windows()
    # 只记非 workspace 路径 (按 posix 归一比较).
    ws_norm = workspace.replace("\\", "/").rstrip("/").lower() if workspace else ""
    new_paths: list[str] = []
    new_seen: set[str] = set()  # 小写键, 大小写不敏感去重 (Windows FS 大小写不敏感)
    for p in paths:
        norm = p.replace("\\", "/").rstrip("/")
        if ws_norm and (norm.lower() == ws_norm or norm.lower().startswith(ws_norm + "/")):
            continue
        if norm.lower() not in new_seen:
            new_seen.add(norm.lower())
            new_paths.append(norm)

    if not new_paths:
        return

    existing = _load_applied_acl_paths()
    # 合并去重保序: seen 用小写键判断存在性, merged 保留首次出现的原始大小写.
    merged: list[str] = []
    seen: set[str] = set()
    for p in existing + new_paths:
        key = p.lower()
        if key in seen:
            continue  # 已有同一路径 (任意大小写), 跳过重复添加
        seen.add(key)
        merged.append(p)
    _save_applied_acl_paths(merged)


def revoke_stale_acl(
    current_policy_paths: list[str],
    workspace: str = "",
    sandbox_user_sid: str | None = None,
) -> list[str]:
    """启动时差集清理: 历史施加路径 − 当前 policy 路径 − 数据根 → 对差集 revoke."""
    _require_windows()
    from jiuwenbox.supervisor import win_acl

    historical = _load_applied_acl_paths()
    if not historical:
        return []

    # current: 当前 policy 路径 + workspace + 数据根.
    current: set[str] = set()
    ws_norm = workspace.replace("\\", "/").rstrip("/").lower() if workspace else ""
    if ws_norm:
        current.add(ws_norm)
    for p in current_policy_paths:
        if p:
            current.add(p.replace("\\", "/").rstrip("/").lower())
    for p in _data_root_paths():
        current.add(p.replace("\\", "/").rstrip("/").lower())

    # 差集: 历史里有、current 里没有的.
    stale = [p for p in historical if p.replace("\\", "/").rstrip("/").lower() not in current]
    if not stale:
        return []

    try:
        win_acl.revoke_sandbox_acl(stale, sandbox_user_sid=sandbox_user_sid)
    except Exception as exc:  # noqa: BLE001
        logger.warning("revoke_stale_acl 清理失败 (差集=%s): %s", stale, exc)
        return []

    # 清理成功: 从历史清单移除已清的差集路径, 只留仍有效的.
    stale_set = {p.replace("\\", "/").rstrip("/").lower() for p in stale}
    remaining = [p for p in historical if p.replace("\\", "/").rstrip("/").lower() not in stale_set]
    remaining = _gc_applied_acl_paths(remaining, current)
    _save_applied_acl_paths(remaining)

    logger.info("启动差集清理: 清除 %d 个残留路径的 ACE: %s", len(stale), stale)
    return stale


# 历史施加路径清单软上限. 超过则 GC: 路径不存在 + 不在当前 policy 的条目裁掉.
# 防止用户频繁改 policy 后清单只增不减 (差集清理只清"移除"的, 不清"替换"的).
_APPLIED_ACL_PATHS_SOFT_LIMIT = 256


def _gc_applied_acl_paths(paths: list[str], current_norm: "set[str]") -> list[str]:
    """超软上限时裁剪历史施加路径清单.

    保留两类: (1) 路径仍存在的 (载体在, ACE 可能在, 需保留以供下次差集清理);
    (2) 仍在当前 policy/workspace/数据根内的 (current_norm). 其余裁掉——
    这些是路径已删且不在当前配置的陈旧条目, ACE 随载体消失, 清单记录已无意义.
    未超上限原样返回 (GC 只在超限时触发, 避免每次启动都做存在性 stat).
    """
    if len(paths) <= _APPLIED_ACL_PATHS_SOFT_LIMIT:
        return paths
    kept: list[str] = []
    pruned = 0
    for p in paths:
        norm = p.replace("\\", "/").rstrip("/").lower()
        if norm in current_norm or os.path.exists(p):
            kept.append(p)
        else:
            pruned += 1
    if pruned:
        logger.info(
            "历史施加路径清单 GC: %d 条超限, 裁掉 %d 条陈旧路径, 保留 %d 条",
            len(paths), pruned, len(kept),
        )
    return kept


def _remove_sandbox_user_and_profile() -> None:
    """删 jbx-sandbox 用户/组/profile 及注册表密码 SID (幂等, 需管理员). 不卸 WFP."""
    sid_str = get_sandbox_user_sid()
    if not sid_str:
        try:
            sid_str = _lookup_user_sid(const.SANDBOX_USER_NAME)
        except Exception:  # noqa: BLE001
            sid_str = None
    if sid_str:
        logger.info("DeleteProfileW sid=%s", sid_str)
        try:
            _delete_profile_by_sid(sid_str)
        except Exception:  # noqa: BLE001
            logger.warning("DeleteProfileW 失败, 继续删 SAM 账户", exc_info=True)
    logger.info("清理残留 profile 目录")
    try:
        _purge_stale_profile_dirs()
    except Exception:  # noqa: BLE001
        logger.warning("清理残留 profile 失败, 继续删 SAM 账户", exc_info=True)
    netapi32 = _get_netapi32()
    ret = netapi32.NetUserDel(None, const.SANDBOX_USER_NAME)
    # 0 = 成功; 2221 = NERR_UserNotFound (已不存在, 幂等). 旧版误用 2201
    # (实为 NERR_BadPassword), 导致幂等卸载时落进 warning 分支 (实跑日志
    # "NetUserDel 返回 2221" 即被错判). 已据 lmerr.h 改回 2221.
    if ret not in (0, 2221):
        logger.warning("NetUserDel 返回 %d (继续)", ret)
    else:
        logger.info("删除沙箱用户 %s", const.SANDBOX_USER_NAME)
    ret = netapi32.NetLocalGroupDel(None, const.SANDBOX_USER_GROUP)
    # 0 = 成功; 2220 = NERR_GroupNotFound (已不存在, 幂等). 旧版误用 2201.
    if ret not in (0, 2220):
        logger.warning("NetLocalGroupDel 返回 %d (继续)", ret)
    else:
        logger.info("删除沙箱组 %s", const.SANDBOX_USER_GROUP)
    # 清注册表标记 + 密码/SID 缓存 (P1-9: 改随机密码后旧 DPAPI blob 必须清,
    # 否则 reinstall 覆盖不彻底会重现 1326; SYNTHETIC_WRITE_SID 合成 SID 固定
    # 可保留, reinstall 不变).
    _reg_set_str(const.REG_VALUE_INSTALLED, "")
    _reg_set_str(const.REG_VALUE_SANDBOX_USER_PW, "")
    _reg_set_str(const.REG_VALUE_SANDBOX_USER_SID, "")


def uninstall() -> None:
    """卸载: 删除 WFP filter + profile + 用户 + 注册表标记 (管理员)."""
    _require_windows()
    if not _is_admin():
        _elevate_uninstall()
        return
    try:
        from jiuwenbox.supervisor import win_wfp
        win_wfp.uninstall_wfp_filters()
        # 降级路径 (PowerShell New-NetFirewallRule) 装的防火墙规则也卸载:
        # WFP 主路径失败走降级时这两条规则残留会永久 Block 沙箱用户出站. 旧版漏卸载降级规则 (review B2).
        win_wfp.uninstall_firewall_rule_fallback()
    except Exception:  # noqa: BLE001
        logger.warning("WFP/防火墙卸载失败", exc_info=True)
    _remove_sandbox_user_and_profile()
    logger.info("Windows 沙箱卸载完成")


def _elevate_uninstall() -> None:
    shell32 = _get_shell32()
    py = (os.environ.get("JIUWENBOX_RUNNER_PYTHON") or "").strip() or sys.executable
    parts = ["-m", "jiuwenbox.supervisor.win_setup", const.UNINSTALL_SUBCOMMAND]
    uac_file, params = _uac_shell_execute_target(py, parts)
    # SW_HIDE: 与 install 一致, 卸载提权子进程也静默, 不弹 CMD.
    result = shell32.ShellExecuteW(
        None, "runas", uac_file, params, None, SW_HIDE,
    )
    if result <= 32:
        raise RuntimeError(f"UAC 提权卸载失败 (返回 {result})")


def _make_install_done_notifier(event_name: str | None):
    """返回一个闭包: 调它则 SetEvent 通知主进程 install 已结束.

    install 子进程用: install() 跑完 (成功或失败) 后调本闭包, 主进程的
    WaitForSingleObject(INFINITE) 解除阻塞. event_name 为空 (手动 CLI 跑
    install, 无主进程等待) 时返回空操作闭包.

    OpenEvent 失败 (event 名传错 / 主进程已 CloseHandle) 时静默 warning, 不抛
    错: install 子进程的核心职责是装沙箱, 通知主进程是次要的, 不能因通知
    失败而把 install 拖崩.
    """
    def _notify() -> None:
        if not event_name:
            return
        try:
            kernel32 = get_kernel32()
            # SYNCHRONIZE(0x00100000) 给 WaitForSingleObject; event_modify_state
            # (0x0002) 给 SetEvent. 主进程创建的 Global 事件默认 ACL 允许同会话/
            # 提权子进程用这两个权限打开.
            synchronize = 0x00100000  # noqa: N806 - Win32 常量
            event_modify_state = 0x0002  # noqa: N806 - Win32 常量
            h = kernel32.OpenEventW(
                synchronize | event_modify_state, False, event_name,
            )
            if not h:
                logger.warning(
                    "install 子进程 OpenEventW(%s) 失败 (WinError %d); "
                    "主进程可能仍在阻塞等待, 将靠超时/installed标记自行判断",
                    event_name, ctypes.get_last_error(),
                )
                return
            try:
                kernel32.SetEvent(wintypes.HANDLE(h))
            finally:
                kernel32.CloseHandle(wintypes.HANDLE(h))
        except Exception:  # noqa: BLE001
            logger.warning("install 完成通知失败", exc_info=True)

    return _notify


def _main(argv: list[str]) -> int:
    """CLI 入口: 接收 --install / --uninstall 及 install 的参数.

    install 支持的可选参数 (review MAJOR #9: 旧版不接, 导致 force/端口/
    preinstall 从命令行无法传入):
      --force                  强制重装
      --recreate-user          先删已有 jbx-sandbox 再重建
      --proxy-port-start N     WFP Permit 放行的 loopback 端口范围起点
      --proxy-port-end   N     范围终点
      --preinstall-paths JSON  读 ACL 预装路径列表 (JSON 编码字符串)
    """
    try:
        # install_force.log 优先用命令行 --install-log-path 指定的路径,
        # 否则 fallback 到用户数据目录 (~/.office-claw/.jiuwenclaw/jiuwenbox)
        _install_log_path_arg: str | None = None
        _strip_indices: list[int] = []
        for _i, _a in enumerate(argv):
            if _a == "--install-log-path" and _i + 1 < len(argv):
                _install_log_path_arg = argv[_i + 1]
                _strip_indices = [_i, _i + 1]
                break
            if _a.startswith("--install-log-path="):
                _install_log_path_arg = _a[len("--install-log-path="):]
                _strip_indices = [_i]
                break
        if _strip_indices:
            for _idx in sorted(_strip_indices, reverse=True):
                del argv[_idx]
        if _install_log_path_arg:
            _install_log_path = os.path.normpath(os.path.abspath(_install_log_path_arg))
        else:
            _install_log_path = _install_log_path()
        try:
            os.makedirs(os.path.dirname(_install_log_path), exist_ok=True)
        except OSError:
            pass
        _fh = logging.FileHandler(_install_log_path, mode="w", encoding="utf-8")
        _fh.setLevel(logging.DEBUG)
        _fh.setFormatter(logging.Formatter(
            "[%(asctime)s] %(levelname)s %(name)s: %(message)s",
        ))
        logging.getLogger().addHandler(_fh)
        logging.getLogger().setLevel(logging.DEBUG)
        # print(f"[win_setup] install 日志落盘: {_install_log_path}", flush=True)
    except Exception:  # noqa: BLE001
        pass
    # argparse 子命令不能带 "--" 前缀; 调用方 (UAC 提权) 用的是
    # "--install"/"--uninstall", 这里规整为 "install"/"uninstall".
    normalized = [a.lstrip("-") if a.startswith("--") and a.lstrip("-") in (
        const.INSTALL_SUBCOMMAND.lstrip("-"),
        const.UNINSTALL_SUBCOMMAND.lstrip("-"),
    ) else a for a in argv]
    parser = argparse.ArgumentParser(
        prog="jiuwenbox.supervisor.win_setup",
        description="Windows 沙箱一次性环境准备 (管理员)",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_install = sub.add_parser(
        const.INSTALL_SUBCOMMAND.lstrip("-"), help="执行安装",
    )
    p_install.add_argument("--force", action="store_true", help="强制重装")
    p_install.add_argument(
        "--recreate-user", action="store_true",
        help="先删已有 jbx-sandbox 再重建 (安装器重装用; 运行时补预装不要加)",
    )
    p_install.add_argument(
        "--proxy-port-start", type=int,
        default=const.DEFAULT_PROXY_PORT_RANGE_START,
    )
    p_install.add_argument(
        "--proxy-port-end", type=int,
        default=const.DEFAULT_PROXY_PORT_RANGE_END,
    )
    p_install.add_argument(
        "--preinstall-paths", default=None,
        help="读 ACL 预装路径列表 (base64 编码的 JSON 字符串; base64 避开"
        "Windows 命令行引号/空格转义问题)",
    )
    p_install.add_argument(
        "--policy-path", default=None,
        help="windows-policy.yaml 路径; install 时读其 read_acl_preinstall + "
        "tool_paths 合并进预装路径 (用户改 tool_paths 后 --force 重装用)",
    )
    p_install.add_argument(
        "--install-done-event", default=None,
        help="命名 Event 名 (主进程预创建), install 跑完后 SetEvent 通知主进程"
        "解除阻塞; 不传则不通知 (手动 CLI 跑 install 时用)",
    )
    sub.add_parser(
        const.UNINSTALL_SUBCOMMAND.lstrip("-"), help="执行卸载",
    )
    args = parser.parse_args(normalized)

    if args.cmd == const.INSTALL_SUBCOMMAND.lstrip("-"):
        preinstall_paths = None
        if args.preinstall_paths:
            try:
                # base64(JSON) 解码: 见 _elevate_and_run_install 的编码注释
                # (避开 Windows 命令行引号/空格转义).
                import base64
                decoded = base64.b64decode(args.preinstall_paths).decode("utf-8")
                preinstall_paths = json.loads(decoded)
            except Exception as exc:
                # print(f"--preinstall-paths 解析失败: {exc}")
                return 2
        # install 跑完 (成功或失败) 都要 SetEvent 通知主进程解除阻塞, 否则主进程
        # WaitForSingleObject(INFINITE) 会死等. 用 finally 保证: install 内部致命
        # 失败会 raise (走 except 回滚), 这里捕获后仍 SetEvent 再 re-raise.
        _notify = _make_install_done_notifier(args.install_done_event)
        try:
            # --policy-path: install 内部会读 policy 合并预装路径, 这里只透传路径.
            install(
                force=args.force,
                preinstall_paths=preinstall_paths,
                proxy_ports=(args.proxy_port_start, args.proxy_port_end),
                policy_path=args.policy_path,
                recreate_user=args.recreate_user,
            )
        except BaseException:
            _notify()
            raise
        else:
            _notify()
        return 0
    if args.cmd == const.UNINSTALL_SUBCOMMAND.lstrip("-"):
        uninstall()
        return 0
    return 2


if __name__ == "__main__":  # pragma: no cover - 模块入口
    raise SystemExit(_main(sys.argv[1:]))
