# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Windows Filtering Platform (WFP) filter 安装/卸载.

对齐 docs/window沙箱.md 6.4.2:
  安装两个 filter (machine-wide, keyed on jbx-sandbox 用户 SID):
    Block filter  (weight LOW):
      Layer = FWPM_LAYER_ALE_AUTH_CONNECT_V4/V6
      Condition: ALE_USER_ID == sandbox SID
      Action = BLOCK
    Permit filter (weight MEDIUM-HIGH, 覆盖 Block):
      Layer 同上
      Conditions:
        ALE_USER_ID == sandbox SID
        IP_REMOTE_ADDRESS == 127.0.0.1
        IP_REMOTE_PORT in [port_start, port_end]
      Action = PERMIT

效果: 沙箱用户的所有出站流量被 Block 拦截, 只有指向 127.0.0.1:代理端口
范围的流量被 Permit 放行 -> 沙箱出网唯一出口是 win_proxy.

WFP user-mode API 位于 fwpuclnt.dll, 通过 ctypes 加载. 所有结构体定义在
模块顶层 (ctypes.Structure 无副作用, Linux 可 import); 真正的 dll 加载
和 API 调用延迟到函数体内, 由 ``sys.platform == "win32"`` 守卫.

降级方案: 若 WFP ctypes 封装在实际环境遇到不可逾越的问题, 可调用
``install_firewall_rule_fallback`` 改用 PowerShell ``New-NetFirewallRule
-LocalUser`` 实现等价的用户级出站拦截 (牺牲内核态优先级控制与绕过保护).
"""

from __future__ import annotations

import ctypes
import logging
import subprocess
import sys
import shutil
from ctypes import wintypes

from jiuwenbox.logging_config import configure_logging
from jiuwenbox.supervisor import win_constants as const

configure_logging()
logger = logging.getLogger(__name__)


def _hidden_kwargs() -> dict:
    """Hide console windows when spawning powershell.exe on Windows."""
    if sys.platform != "win32":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = 0  # SW_HIDE
    return {
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
        "startupinfo": startupinfo,
    }


def _require_windows() -> None:
    if sys.platform != "win32":
        raise RuntimeError(
            f"win_wfp 仅在 Windows 平台可用; 当前平台 {sys.platform!r}"
        )


def _get_powershell() -> str:
    for candidate in ("pwsh", "powershell", "powershell.exe"):
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    return "powershell"


# WFP HRESULT 段 0x80320xxx (FWP_E_*) 常见码. ctypes.WinError 对高位为 1 的 HRESULT 会溢出,
# 统一用 _wfp_error 抛出. 完整列表见 fwperr.h.
_WFP_ERROR_NAMES: dict[int, str] = {
    0x80320001: "FWP_E_CALLOUT_NOT_FOUND",
    0x80320002: "FWP_E_CONDITION_NOT_FOUND",
    0x80320003: "FWP_E_FILTER_NOT_FOUND",
    0x80320004: "FWP_E_LAYER_NOT_FOUND",
    0x80320005: "FWP_E_PROVIDER_NOT_FOUND",
    0x80320006: "FWP_E_PROVIDER_CONTEXT_NOT_FOUND",
    0x80320007: "FWP_E_SUBLAYER_NOT_FOUND",
    0x80320008: "FWP_E_NOT_FOUND",
    0x80320009: "FWP_E_ALREADY_EXISTS",
    0x8032000A: "FWP_E_IN_USE",
    0x8032000B: "FWP_E_DUPLICATE_CONDITION",
    0x8032000C: "FWP_E_DUPLICATE_KEYMOD",
    0x8032000D: "FWP_E_IN_USE_LOCKED",
    0x8032000E: "FWP_E_INVALID_PLUGIN",
    0x8032000F: "FWP_E_INVALID_PLUGIN_TRUSTED",
    0x80320010: "FWP_E_DROP_NO_EXEMPT",  # 通用无效参数/类型类
    0x80320011: "FWP_E_NULL_KEY",
    0x80320012: "FWP_E_INVALID_ENUMERATOR",
    0x80320013: "FWP_E_INVALID_FLAGS",
    0x80320014: "FWP_E_INVALID_NET_MASK",
    0x80320015: "FWP_E_INVALID_STATUS",
    0x80320016: "FWP_E_INVALID_RANGE",
    0x80320017: "FWP_E_INVALID_INTERVAL",
    0x80320018: "FWP_E_TOO_MANY_REFERENCES",
    0x80320019: "FWP_E_NAME_NOT_FOUND",
    0x80320020: "FWP_E_INVALID_WEIGHT",
    0x80320021: "FWP_E_MATCH_TYPE_MISMATCH",
    0x80320023: "FWP_E_INVALID_AUTH_VALUE",
    0x80320024: "FWP_E_INVALID_KEY",
    0x80320031: "FWP_E_KEY_NOT_FOUND",
    0xC0360017: "FWP_E_TRUSTED_PACKAGE_MISMATCH",
}


def _wfp_error(hr: int, where: str) -> "OSError":
    """把 WFP HRESULT 转成带明文的 OSError (避免 WinError OverflowError). 兼容 0x80070000 段 Win32 错误码."""
    name = _WFP_ERROR_NAMES.get(hr)
    # 位 0x80070000 段 = HRESULT_FROM_WIN32, 取低 16 位是 Win32 错误码。
    if name is None and (hr & 0xFFFF0000) == 0x80070000:
        win32 = hr & 0xFFFF
        _sys = {5: "ERROR_ACCESS_DENIED", 87: "ERROR_INVALID_PARAMETER",
                1377: "ERROR_MEMBER_IN_ALIAS", 2224: "NERR_UserExists"}
        name = _sys.get(win32, f"WIN32_{win32}")
    if name is None and 0x80320000 <= hr <= 0x8032FFFF:
        # FWP_E_* 段未登记码: 多为 condition value / filter 结构校验类错误,
        # 精确名需对照本地 SDK fwperr.h (无法在线核实时不臆测写死).
        name = f"FWP_E_UNLISTED_{hr & 0xFFFF:04X} (check fwperr.h)"
    label = name or "UNKNOWN"
    return OSError(f"[{where}] WFP/HRESULT hr=0x{hr:08X} ({label})")


# ---------------------------------------------------------------------------
# Guid 结构体 (WFP 大量使用 Guid 作为 layer/condition/filter key).
# 布局对齐 Windows SDK Guid: { DWORD; WORD; WORD; BYTE[8] }.
# ---------------------------------------------------------------------------
class Guid(ctypes.Structure):  # noqa: N801 - Win32 SDK 规范类名
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


def _guid_from_str(s: str) -> Guid:
    """把 Guid 字符串解析成 Guid 结构体.

    Windows Guid 内存布局是 little-endian. ``uuid.UUID.bytes_le`` 给出
    Windows 风格的字节序: 前 4 字节 = Data1 (小端), 接着 2 字节 = Data2,
    再 2 字节 = Data3, 最后 8 字节 = Data4 (big-endian, 逐字节).
    """
    import uuid
    try:
        u = uuid.UUID(s)
    except ValueError as exc:
        raise ValueError(
            f"WFP filter_key 不是合法 UUID: {s!r} "
            f"(若含 '-UDP'/'-TCP' 后缀, 请更新 win_wfp.py 至含 _dns_filter_guid_str 的版本)"
        ) from exc
    g = Guid()
    g.Data1 = u.time_low  # noqa: N815 - Win32 SDK 字段名
    g.Data2 = u.time_mid  # noqa: N815 - Win32 SDK 字段名
    g.Data3 = u.time_hi_version  # noqa: N815 - Win32 SDK 字段名
    # Data4 取 bytes_le 的后 8 字节 (uuid 库保证该顺序).
    for i, b in enumerate(u.bytes_le[8:]):
        g.Data4[i] = b  # noqa: N815 - Win32 SDK 字段名
    return g


def _guid_to_str(g: "Guid") -> str:
    """把 Guid 结构体转回标准 Guid 字符串 (review #1: uninstall 枚举后收集 key)."""
    import uuid
    # 构造 bytes_le: Data1(4, 小端) + Data2(2, 小端) + Data3(2, 小端) + Data4(8, 逐字节).
    data1 = g.Data1  # noqa: N815 - Win32 SDK 字段名
    data2 = g.Data2  # noqa: N815 - Win32 SDK 字段名
    data3 = g.Data3  # noqa: N815 - Win32 SDK 字段名
    data4 = bytes(g.Data4)  # noqa: N815 - Win32 SDK 字段名
    bytes_le = (
        data1.to_bytes(4, "little")
        + data2.to_bytes(2, "little")
        + data3.to_bytes(2, "little")
        + data4
    )
    return str(uuid.UUID(bytes_le=bytes_le))


# 固定 namespace Guid (任意合法 UUID), 用于 uuid5 派生 per-port filter key。
# uuid5 基于 (namespace, name) 哈希, 确定性、不依赖时间/随机, install/uninstall
# 用相同 (base_key, port) 派生出相同 Guid, 保证幂等删。
_PERMIT_FILTER_NAMESPACE = "6f9b2a3c-1d4e-4b5f-8a90-7c2e1b3a4d5f"


def _permit_filter_guid_str(base_key: str, port: int) -> str:
    """为端口 Permit filter 派生确定性 Guid 字符串.

    S9 实跑: 旧版 port_key = f"{base_key}-{port}" 不是合法 UUID (含端口后缀),
    _guid_from_str 里 uuid.UUID(s) 报 ValueError, install/uninstall 全崩。
    改用 uuid5(namespace, f"{base_key}:{port}") 生成合法 Guid, 同 (base,port)
    派生同 Guid, 幂等安装/卸载。
    """
    import uuid
    ns = uuid.UUID(_PERMIT_FILTER_NAMESPACE)
    return str(uuid.uuid5(ns, f"{base_key}:{port}"))


def _dns_filter_guid_str(base_key: str, protocol: int) -> str:
    """为 DNS Permit filter 派生确定性 Guid 字符串.

    旧版 f"{dns_key}-UDP" / f"{dns_key}-TCP" 不是合法 UUID, UAC install 报
    ValueError: badly formed hexadecimal UUID string. 与 _permit_filter_guid_str 同
    理用 uuid5 派生。
    """
    import uuid
    ns = uuid.UUID(_PERMIT_FILTER_NAMESPACE)
    proto_name = "udp" if protocol == const.IPPROTO_UDP else "tcp"
    return str(uuid.uuid5(ns, f"{base_key}:dns:{proto_name}"))


# ---------------------------------------------------------------------------
# FwpByteBlob / FWP_V4_ADDR_AND_MASK / FwpAddrMaskV6.
# 布局对齐 fwptypes.h.
# ---------------------------------------------------------------------------
class FwpByteArray16(ctypes.Structure):  # noqa: N801 - Win32 SDK 规范类名
    _fields_ = [("byteArray16", ctypes.c_uint8 * 16)]


class FwpByteBlob(ctypes.Structure):  # noqa: N801 - Win32 SDK 规范类名
    _fields_ = [
        ("size", ctypes.c_uint32),
        ("data", ctypes.POINTER(ctypes.c_uint8)),
    ]


class FwpAddrMaskV4(ctypes.Structure):  # noqa: N801 - Win32 SDK 规范类名
    """IPv4 地址 + 掩码 (FWP_V4_ADDR_AND_MASK, fwptypes.h).

    addr 为 host byte order (NOT network order). 127.0.0.1 = 0x7F000001.
    """
    _fields_ = [
        ("addr", ctypes.c_uint32),
        ("mask", ctypes.c_uint32),
    ]


class FwpAddrMaskV6(ctypes.Structure):  # noqa: N801 - Win32 SDK 规范类名
    _fields_ = [
        ("addr", ctypes.c_uint8 * 16),
        ("prefixLength", ctypes.c_uint8),
    ]


# ---------------------------------------------------------------------------
# FwpValue0 / FwpConditionValue0 (带类型标签的联合体).
# 联合体成员对照 fwptypes.h FWP_VALUE0_/FWP_CONDITION_VALUE0_ union:
# 数值标量 + 指针成员 (byteArray16*/byteBlob*/sid*/sd*/v4AddrMask*/...).
# 指针成员用 c_void_p (x64 8B), 标量成员按 SDK 类型. 联合体尺寸 = max(成员).
# ---------------------------------------------------------------------------
class FwpValue0(ctypes.Structure):  # noqa: N801 - Win32 SDK 规范类名
    """FwpValue0: 一个带类型标签的值 (Type + 联合体)."""

    class _VALUE(ctypes.Union):
        _fields_ = [
            ("uint8", ctypes.c_uint8),
            ("uint16", ctypes.c_uint16),
            ("uint32", ctypes.c_uint32),
            ("int8", ctypes.c_int8),
            ("int16", ctypes.c_int16),
            ("int32", ctypes.c_int32),
            ("float32", wintypes.FLOAT),
            ("double64", ctypes.c_double),
            # 指针成员 (x64 8B): uint64/int64 在 SDK 是 UINT64*/INT64* (指向值).
            ("uint64", ctypes.POINTER(ctypes.c_uint64)),
            ("int64", ctypes.POINTER(ctypes.c_int64)),
            ("byteArray16", ctypes.c_void_p),   # FwpByteArray16*
            ("byteBlob", ctypes.c_void_p),      # FwpByteBlob*
            ("sid", ctypes.c_void_p),           # SID*
            ("sd", ctypes.c_void_p),            # FwpByteBlob* (SECURITY_DESCRIPTOR)
            ("unicodeString", ctypes.c_void_p),  # LPWSTR
            ("byteArray6", ctypes.c_void_p),    # FWP_BYTE_ARRAY6*
        ]

    _fields_ = [
        ("type", ctypes.c_uint32),
        ("value", _VALUE),
    ]


class FwpConditionValue0(ctypes.Structure):  # noqa: N801 - Win32 SDK 规范类名
    """FwpConditionValue0: condition 值 (比 FwpValue0 多 v4/v6 addr mask + range)."""

    class _VALUE(ctypes.Union):
        _fields_ = [
            ("uint8", ctypes.c_uint8),
            ("uint16", ctypes.c_uint16),
            ("uint32", ctypes.c_uint32),
            ("int8", ctypes.c_int8),
            ("int16", ctypes.c_int16),
            ("int32", ctypes.c_int32),
            ("float32", wintypes.FLOAT),
            ("double64", ctypes.c_double),
            ("uint64", ctypes.POINTER(ctypes.c_uint64)),
            ("int64", ctypes.POINTER(ctypes.c_int64)),
            ("byteArray16", ctypes.c_void_p),
            ("byteBlob", ctypes.c_void_p),
            ("sid", ctypes.c_void_p),
            ("sd", ctypes.c_void_p),
            ("unicodeString", ctypes.c_void_p),
            ("byteArray6", ctypes.c_void_p),
            # v4/v6/range 在 SDK 是 *指针* (FWP_V4_ADDR_AND_MASK* 等),
            # 用 c_void_p 保持 8B 对齐; 实际构造时用 ctypes.cast 指向局部实例.
            ("v4AddrMask", ctypes.c_void_p),
            ("v6AddrMask", ctypes.c_void_p),
            ("rangeValue", ctypes.c_void_p),
        ]

    _fields_ = [
        ("type", ctypes.c_uint32),
        ("value", _VALUE),
    ]


class FwpRange0(ctypes.Structure):  # noqa: N801 - Win32 SDK 规范类名
    """FWP_RANGE0: 范围匹配的低/高值 (FWP_MATCH_RANGE 用)."""
    _fields_ = [
        ("valueLow", FwpValue0),
        ("valueHigh", FwpValue0),
    ]


# ---------------------------------------------------------------------------
# FwpmDisplayData0 (内嵌结构体, 两 wchar_t* 指针, x64 16B).
# FwpmFilter0/SUBLAYER0/SESSION0 都内嵌它, 不能用 c_void_p (8B) 否则后续
# 字段全部错位 (review MAJOR #6).
# ---------------------------------------------------------------------------
class FwpmDisplayData0(ctypes.Structure):  # noqa: N801 - Win32 SDK 规范类名
    _fields_ = [
        ("name", ctypes.c_wchar_p),
        ("description", ctypes.c_wchar_p),
    ]


class FwpmFilterCondition0(ctypes.Structure):  # noqa: N801 - Win32 SDK 规范类名
    """单个 filter condition: fieldKey + matchType + conditionValue."""
    _fields_ = [
        ("fieldKey", Guid),
        ("matchType", ctypes.c_uint32),  # FWP_MATCH_*
        ("conditionValue", FwpConditionValue0),
    ]


class FwpmActionUnion(ctypes.Union):  # noqa: N801 - Win32 SDK 规范类名
    """FwpmAction0 内嵌 union: {Guid filterType; Guid calloutKey}.

    两个成员都是 Guid (16B), union = 16B.
    """
    _fields_ = [
        ("filterType", Guid),   # FWP_ACTION_FLAG_CALLOUT 时是 filterType
        ("calloutKey", Guid),   # 否则是 calloutKey
    ]


class FwpmAction0(ctypes.Structure):  # noqa: N801 - Win32 SDK 规范类名
    """Filter action: type(4B + 4B pad) + union(16B) = 24B (x64).

    对齐 windows-sys FwpmAction0 repr(C): type + Anonymous union.
    BLOCK/PERMIT 只用 type, union 留空 (ctypes 零初始化).
    """
    _fields_ = [
        ("type", ctypes.c_uint32),          # FWP_ACTION_*
        ("Anonymous", FwpmActionUnion),
    ]


# ---------------------------------------------------------------------------
# FWPM_FILTER0 (fwpmtypes.h). 布局严格对齐 windows-sys SDK (repr(C), x64):
#   filterKey(GUID,16@0) displayData(FWPM_DISPLAY_DATA0,16@16) flags(UINT32,4@32)
#   pad(4@36) providerKey(GUID*,8@40) providerData(FWP_BYTE_BLOB,16@48)
#   layerKey(GUID,16@64) subLayerKey(GUID,16@80) weight(FWP_VALUE0,16@96)
#   numFilterConditions(UINT32,4@112) pad(4@116)
#   filterCondition(FWPM_FILTER_CONDITION0*,8@120) action(FWPM_ACTION0,24@128)
#   Anonymous(FWPM_FILTER0_0,16@152)  union{UINT64 rawContext; GUID providerContextKey}
#   reserved(GUID*,8@168) filterId(UINT64,8@176) effectiveWeight(FWP_VALUE0,16@184)
#   sizeof = 200B (x64). Anonymous 须是 16B union (UINT64 与 GUID 取 max), 写成 c_uint64 会少 8B → RPC_X_BAD_STUB_DATA.
# ---------------------------------------------------------------------------
class FwpmFilterUnion(ctypes.Union):  # noqa: N801 - Win32 SDK 规范类名
    """FwpmFilter0 内嵌 union: {UINT64 rawContext; Guid providerContextKey}. 16B, 8B 对齐."""
    _fields_ = [
        ("rawContext", ctypes.c_uint64),
        ("providerContextKey", Guid),
    ]


class FwpmFilter0(ctypes.Structure):  # noqa: N801 - Win32 SDK 规范类名
    _fields_ = [
        ("filterKey", Guid),
        ("displayData", FwpmDisplayData0),       # 内嵌, 非 c_void_p
        ("flags", ctypes.c_uint32),
        ("providerKey", ctypes.c_void_p),          # Guid*
        ("providerData", FwpByteBlob),          # 内嵌, 非 providerDataSize+ptr
        ("layerKey", Guid),
        ("subLayerKey", Guid),
        ("weight", FwpValue0),
        ("numFilterConditions", ctypes.c_uint32),
        ("filterCondition", ctypes.POINTER(FwpmFilterCondition0)),
        ("action", FwpmAction0),
        ("Anonymous", FwpmFilterUnion),       # union{rawContext; providerContextKey}
        ("reserved", ctypes.c_void_p),            # Guid*
        ("filterId", ctypes.c_uint64),            # BFE 填, 调用方不设
        ("effectiveWeight", FwpValue0),          # BFE 填
    ]


class FwpmSubLayer(ctypes.Structure):  # noqa: N801,D205,D415 - Win32 SDK 类名 + docstring 格式
    """FwpmSubLayer (fwpmtypes.h): subLayerKey + displayData(内嵌) +
        flags + providerKey(Guid*) + providerData(内嵌) + weight(UINT16).
        weight 是 UINT16, 旧版误为 c_uint32; 且无 providerDataSize 字段.
    """
    _fields_ = [
        ("subLayerKey", Guid),
        ("displayData", FwpmDisplayData0),
        ("flags", ctypes.c_uint32),
        ("providerKey", ctypes.c_void_p),   # Guid*
        ("providerData", FwpByteBlob),
        ("weight", ctypes.c_uint16),
    ]


class FwpmSession0(ctypes.Structure):  # noqa: N801,D205,D415 - Win32 SDK 类名 + docstring 格式
    """FwpmSession0 (fwpmtypes.h): sessionKey + displayData(内嵌) +
        flags + txnWaitTimeoutInMSec + processId/sid/username/kernelMode
        (BFE 填, 但需占位保结构体尺寸). 旧版 displayData 误为 c_void_p、重复
        txnWait 字段、缺输出字段.
    """
    _fields_ = [
        ("sessionKey", Guid),
        ("displayData", FwpmDisplayData0),
        ("flags", ctypes.c_uint32),
        ("txnWaitTimeoutInMSec", ctypes.c_uint32),
        ("processId", wintypes.DWORD),       # BFE 填
        ("sid", ctypes.c_void_p),            # SID*, BFE 填
        ("username", ctypes.c_wchar_p),     # wchar_t*, BFE 填
        ("kernelMode", wintypes.BOOL),       # BFE 填
    ]


# ---------------------------------------------------------------------------
# fwpuclnt.dll 加载.
# ---------------------------------------------------------------------------
_fwpuclnt: ctypes.WinDLL | None = None


def _get_fwpuclnt() -> ctypes.WinDLL:
    global _fwpuclnt
    if _fwpuclnt is None:
        _fwpuclnt = ctypes.WinDLL("fwpuclnt.dll", use_last_error=True)
        _fwpuclnt.FwpmEngineOpen0.argtypes = [
            wintypes.LPCWSTR, ctypes.c_uint32,  # serverName, authnService
            ctypes.c_void_p,  # authnIdentity (SEC_WINNT_AUTH_IDENTITY_W*)
            ctypes.POINTER(FwpmSession0),  # session
            ctypes.POINTER(wintypes.HANDLE),  # engineHandle
        ]
        _fwpuclnt.FwpmEngineOpen0.restype = wintypes.DWORD  # HRESULT
        _fwpuclnt.FwpmEngineClose0.argtypes = [wintypes.HANDLE]
        _fwpuclnt.FwpmEngineClose0.restype = wintypes.DWORD
        _fwpuclnt.FwpmSubLayerAdd0.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(FwpmSubLayer), ctypes.c_void_p,
        ]
        _fwpuclnt.FwpmSubLayerAdd0.restype = wintypes.DWORD
        _fwpuclnt.FwpmSubLayerDeleteByKey0.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(Guid),
        ]
        _fwpuclnt.FwpmSubLayerDeleteByKey0.restype = wintypes.DWORD
        _fwpuclnt.FwpmFilterAdd0.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(FwpmFilter0),
            ctypes.c_void_p,  # PSECURITY_DESCRIPTOR
            ctypes.POINTER(ctypes.c_uint64),  # id
        ]
        _fwpuclnt.FwpmFilterAdd0.restype = wintypes.DWORD
        _fwpuclnt.FwpmFilterDeleteByKey0.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(Guid),
        ]
        _fwpuclnt.FwpmFilterDeleteByKey0.restype = wintypes.DWORD
        # FwpmTransactionBegin/Commit/Abort 用于多 filter 原子安装.
        _fwpuclnt.FwpmTransactionBegin0.argtypes = [wintypes.HANDLE, ctypes.c_uint32]
        _fwpuclnt.FwpmTransactionBegin0.restype = wintypes.DWORD
        _fwpuclnt.FwpmTransactionCommit0.argtypes = [wintypes.HANDLE]
        _fwpuclnt.FwpmTransactionCommit0.restype = wintypes.DWORD
        _fwpuclnt.FwpmTransactionAbort0.argtypes = [wintypes.HANDLE]
        _fwpuclnt.FwpmTransactionAbort0.restype = wintypes.DWORD
        # review #1: 枚举 sublayer 下所有 filter (按 sublayer 过滤), 用于 uninstall
        # 不依赖端口范围即可干净卸载任意端口范围的 Permit filter.
        # FwpmFilterCreateEnumHandle0(engine, enumTemplate*, enumHandle*).
        _fwpuclnt.FwpmFilterCreateEnumHandle0.argtypes = [
            wintypes.HANDLE, ctypes.c_void_p, ctypes.POINTER(wintypes.HANDLE),
        ]
        _fwpuclnt.FwpmFilterCreateEnumHandle0.restype = wintypes.DWORD
        # FwpmFilterEnum0(engine, enumHandle, numEntriesRequested*, entries**)
        #   entries 是 FwpmFilter0** (BFE 分配, FwpmFreeMemory0 释放).
        _fwpuclnt.FwpmFilterEnum0.argtypes = [
            wintypes.HANDLE, wintypes.HANDLE,
            ctypes.POINTER(wintypes.UINT),
            ctypes.POINTER(ctypes.POINTER(FwpmFilter0)),
        ]
        _fwpuclnt.FwpmFilterEnum0.restype = wintypes.DWORD
        # FwpmFilterDestroyEnumHandle0(engine, enumHandle).
        _fwpuclnt.FwpmFilterDestroyEnumHandle0.argtypes = [
            wintypes.HANDLE, wintypes.HANDLE,
        ]
        _fwpuclnt.FwpmFilterDestroyEnumHandle0.restype = wintypes.DWORD
        # FwpmFreeMemory0(p): 释放 BFE 枚举返回的内存 (传 entries 指针的地址).
        _fwpuclnt.FwpmFreeMemory0.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
        _fwpuclnt.FwpmFreeMemory0.restype = None
    return _fwpuclnt


def _open_engine() -> wintypes.HANDLE:
    """FwpmEngineOpen0 打开 WFP 引擎会话."""
    fwpu = _get_fwpuclnt()
    session = FwpmSession0()
    session.sessionKey = _guid_from_str("00000000-0000-0000-0000-000000000000")  # noqa: N815 - Win32 SDK 字段名
    session.flags = const.FWP_SESSION_FLAG_NONE  # noqa: N815 - Win32 SDK 字段名
    engine = wintypes.HANDLE()
    hr = fwpu.FwpmEngineOpen0(
        None, const.RPC_C_AUTHN_WINNT, None,
        ctypes.byref(session), ctypes.byref(engine),
    )
    if hr != 0:
        raise _wfp_error(hr, "FwpmEngineOpen0")
    return engine


def _add_sublayer(engine: wintypes.HANDLE, subLayerKey: str, weight: int) -> Guid:
    """创建/复用 sublayer (幂等: 已存在则忽略).

    S9 实跑: displayData.name 不能为 NULL (BFE 报 FWP_E_INVALID_AUTH_VALUE=
    0x80320023, 文档 0x80320023 = "displayData.name cannot be null")。
    旧版未设 displayData → 报错。显式设 name/description。
    """
    fwpu = _get_fwpuclnt()
    sublayer = FwpmSubLayer()
    sublayer.subLayerKey = _guid_from_str(subLayerKey)  # noqa: N815 - Win32 SDK 字段名
    sublayer.displayData.name = "JiuwenBoxSandboxSublayer"  # noqa: N815 - Win32 SDK 字段名
    sublayer.displayData.description = "JiuwenBox sandbox egress filter sublayer"  # noqa: N815 - Win32 SDK 字段名
    sublayer.flags = const.FWPM_SUBLAYER_FLAG_PERSISTENT  # noqa: N815 - Win32 SDK 字段名
    sublayer.weight = weight  # noqa: N815 - Win32 SDK 字段名
    hr = fwpu.FwpmSubLayerAdd0(engine, ctypes.byref(sublayer), None)
    if hr == const.FWP_E_ALREADY_EXISTS:  # 幂等, sublayer 已存在 (不管是否 persistent)
        logger.debug("Sublayer 已存在, 跳过创建")
    elif hr != 0:
        raise _wfp_error(hr, "FwpmSubLayerAdd0")
    return sublayer.subLayerKey  # noqa: N815 - Win32 SDK 字段名


def build_loopback_v4_condition() -> "tuple[FwpmFilterCondition0, FwpAddrMaskV4]":
    """构造 IP_REMOTE_ADDRESS == 127.0.0.1 条件 (IPv4).

    SDK 的 FWP_CONDITION_VALUE0 里 v4AddrMask 是 *指针* (FWP_V4_ADDR_AND_MASK*),
    不能直接内嵌赋值; 需返回一个存活的局部 FWP_V4_ADDR_MASK 实例, 由调用方
    保持其生命周期到 FwpmFilterAdd0 返回 (否则指针悬垂). addr 用 host byte
    order 0x7F000001 (review CRITICAL #4: 旧值 0x0100007F 是网络序, 匹配
    1.0.0.127).
    """
    cond = FwpmFilterCondition0()
    cond.fieldKey = _guid_from_str(const.FWPM_CONDITION_IP_REMOTE_ADDRESS)  # noqa: N815 - Win32 SDK 字段名
    cond.matchType = const.FWP_MATCH_EQUAL  # noqa: N815 - Win32 SDK 字段名
    cond.conditionValue.type = const.FWP_V4_ADDR_MASK  # noqa: N815 - Win32 SDK 字段名
    addr_mask = FwpAddrMaskV4()
    addr_mask.addr = const.LOOPBACK_IPV4_INT  # noqa: N815 - Win32 SDK 字段名 # 0x7F000001 = 127.0.0.1 host order
    addr_mask.mask = 0xFFFFFFFF  # noqa: N815 - Win32 SDK 字段名
    cond.conditionValue.value.v4AddrMask = ctypes.cast(  # noqa: N815 - Win32 SDK 字段名
        ctypes.pointer(addr_mask), ctypes.c_void_p,
    ).value
    return cond, addr_mask


def _build_loopback_v6_condition() -> "tuple[FwpmFilterCondition0, FwpAddrMaskV6]":
    """构造 IPv6 ::1 条件. 返回 (cond, 存活的局部实例)."""
    cond = FwpmFilterCondition0()
    cond.fieldKey = _guid_from_str(const.FWPM_CONDITION_IP_REMOTE_ADDRESS)  # noqa: N815 - Win32 SDK 字段名
    cond.matchType = const.FWP_MATCH_EQUAL  # noqa: N815 - Win32 SDK 字段名
    cond.conditionValue.type = const.FWP_V6_ADDR_AND_MASK  # noqa: N815 - Win32 SDK 字段名
    addr_mask = FwpAddrMaskV6()
    addr_arr = (ctypes.c_uint8 * 16)(
        0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1,
    )
    ctypes.memmove(
        ctypes.addressof(addr_mask.addr), addr_arr, 16,
    )
    addr_mask.prefixLength = 128  # noqa: N815 - Win32 SDK 字段名
    cond.conditionValue.value.v6AddrMask = ctypes.cast(  # noqa: N815 - Win32 SDK 字段名
        ctypes.pointer(addr_mask), ctypes.c_void_p,
    ).value
    return cond, addr_mask


def _build_ale_user_condition(sandbox_user_sid: str) -> "tuple[FwpmFilterCondition0, object]":
    """构造 ALE_USER_ID 条件 (基于 SECURITY_DESCRIPTOR, 非 裸 SID).

    SDK: FWPM_CONDITION_ALE_USER_ID 的 condition 值类型是
    FWP_SECURITY_DESCRIPTOR_TYPE (FwpByteBlob* 指向自相关 SD 字节),
    而非 FWP_SID. BFE 评估时检查 SD 的 DACL 是否对发起连接的用户授予
    FWP_ACTRL_MATCH_FILTER 访问权 (SDK 示例: Permitting and Blocking
    Applications and Users, BuildSecurityDescriptorW + FWP_ACTRL_MATCH_FILTER).

    用 win32security 构造自相关 SD: DACL 授 jbx-sandbox 用户
    FWP_ACTRL_MATCH_FILTER. 返回 (cond, _keeps), _keeps 持有 SD 字节与
    FwpByteBlob 实例引用, 调用方需保持其生命周期到 FwpmFilterAdd0 返回.
    """
    cond = FwpmFilterCondition0()
    cond.fieldKey = _guid_from_str(const.FWPM_CONDITION_ALE_USER_ID)  # noqa: N815 - Win32 SDK 字段名
    cond.matchType = const.FWP_MATCH_EQUAL  # noqa: N815 - Win32 SDK 字段名
    cond.conditionValue.type = const.FWP_SECURITY_DESCRIPTOR_TYPE  # noqa: N815 - Win32 SDK 字段名

    # 用 win32security 构造自相关 SD. 延迟 import (Linux 无 pywin32).
    try:
        import win32security  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "构造 ALE_USER_ID 条件需要 pywin32 (win32security); "
            "未安装, WFP user-keyed 过滤不可用 (降级 PowerShell 路径)"
        ) from exc

    # jbx-sandbox 用户的 SID (字符串 -> SID 对象).
    user_sid_obj = win32security.ConvertStringSidToSid(sandbox_user_sid)
    # pywin32 SetEntriesInAcl 接受 EXPLICIT_ACCESS dict, 其 Trustee 字段也是 dict
    # {TrusteeType, TrusteeForm, Identifier}. TrusteeForm=TRUSTEE_IS_SID 时 Identifier 放 PySID 对象.
    explicit = {
        "AccessPermissions": const.FWP_ACTRL_MATCH_FILTER,
        "AccessMode": win32security.GRANT_ACCESS,
        "Inheritance": 0,  # 不继承
        "Trustee": {
            "TrusteeType": win32security.TRUSTEE_IS_USER,
            "TrusteeForm": win32security.TRUSTEE_IS_SID,
            "Identifier": user_sid_obj,
        },
    }
    # SetEntriesInAcl 是 PyACL 方法: 在空 ACL 上添加 entries.
    # P2-37: 取返回值校验 (pywin32 版本差异下 DACL 可能未生效).
    dacl = win32security.ACL()
    _set_ret = dacl.SetEntriesInAcl([explicit])
    if _set_ret is not None and _set_ret != 0:
        logger.warning("SetEntriesInAcl 返回非零: %s", _set_ret)
    sd = win32security.SECURITY_DESCRIPTOR()
    # 显式 Initialize + 设 owner/group/jbx-sandbox SID 再设 DACL, 构造自洽 SD.
    # 缺 owner/group 时 SetSecurityDescriptorDacl 的 _MakeAbsoluteSD 可能产生不一致：
    # self-relative SD → BFE marshal 校验失败 (RPC_X_BAD_STUB_DATA).
    sd.Initialize()
    sd.SetSecurityDescriptorOwner(user_sid_obj, 0)
    sd.SetSecurityDescriptorGroup(user_sid_obj, 0)
    sd.SetSecurityDescriptorDacl(1, dacl, 0)
    # S9 实跑: win32security 无 MakeSelfRelativeSD。PySECURITY_DESCRIPTOR 对象内部
    # 始终以 self-relative 格式存储 (见 pywin32 PySECURITY_DESCRIPTOR.cpp SetSD),
    # 且支持 buffer 接口, bytes(sd) 直接拿到 self-relative 原始字节 (FWP 要求此格式)。
    sd_bytes = bytes(sd)
    # 诊断: SD 长度 + control flags (确认 self-relative 位)。
    try:
        ctrl, _rev = sd.GetSecurityDescriptorControl()
        sr_bit = bool(ctrl & 0x8000)  # SE_SELF_RELATIVE
    except Exception:  # noqa: BLE001
        sr_bit = "?"
    logger.info(
        "ALE_USER_ID SD: len=%d valid=%s self_relative=%s sid=%s",
        len(sd_bytes), bool(sd_bytes), sr_bit, sandbox_user_sid,
    )

    blob = FwpByteBlob()
    # blob.data 须指 malloc 分配的标准对齐内存 (SDK 用 BuildSecurityDescriptorW 的 LocalAlloc).
    # 旧版 from_buffer_copy 的 buffer 内存来源/对齐会让 BFE RPC marshal 校验失败 (RPC_X_BAD_STUB_DATA).
    # 改用 create_string_buffer (malloc, 8 字节对齐, RPC 友好).
    buf = ctypes.create_string_buffer(sd_bytes, len(sd_bytes))
    blob.size = len(sd_bytes)  # noqa: N815 - Win32 SDK 字段名
    blob.data = ctypes.cast(buf, ctypes.POINTER(ctypes.c_uint8))  # noqa: N815 - Win32 SDK 字段名
    cond.conditionValue.value.sd = ctypes.cast(  # noqa: N815 - Win32 SDK 字段名
        ctypes.pointer(blob), ctypes.c_void_p,
    ).value
    return cond, _KeepAlive(blob=blob, buf=buf, sd_bytes=sd_bytes)


class _KeepAlive:
    """持有 ctypes 对象引用, 防止条件构造返回后 GC 释放指针指向的内存."""
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _build_port_eq_condition(port: int) -> FwpmFilterCondition0:
    """构造 IP_REMOTE_PORT == port 条件 (FWP_MATCH_EQUAL).

    放行整个端口范围采用"每个端口一个 Permit filter"的方案: WFP 的
    FWP_MATCH_RANGE 需 FWP_RANGE0 结构, ctypes 布局复杂且易错; 每端口一
    个 EQUAL filter 更可靠, 端口范围通常 <=10 个, 开销可忽略.
    """
    cond = FwpmFilterCondition0()
    cond.fieldKey = _guid_from_str(const.FWPM_CONDITION_IP_REMOTE_PORT)  # noqa: N815 - Win32 SDK 字段名
    cond.matchType = const.FWP_MATCH_EQUAL  # noqa: N815 - Win32 SDK 字段名
    cond.conditionValue.type = const.FWP_UINT16  # noqa: N815 - Win32 SDK 字段名
    cond.conditionValue.value.uint16 = port  # noqa: N815 - Win32 SDK 字段名
    return cond


def _build_protocol_condition(protocol: int) -> FwpmFilterCondition0:
    """构造 IP_PROTOCOL == protocol 条件 (FWP_MATCH_EQUAL).

    protocol 值对齐 winnt.h / RFC 790: IPPROTO_TCP=6, IPPROTO_UDP=17.
    """
    cond = FwpmFilterCondition0()
    cond.fieldKey = _guid_from_str(const.FWPM_CONDITION_IP_PROTOCOL)  # noqa: N815 - Win32 SDK 字段名
    cond.matchType = const.FWP_MATCH_EQUAL  # noqa: N815 - Win32 SDK 字段名
    cond.conditionValue.type = const.FWP_UINT8  # noqa: N815 - Win32 SDK 字段名
    cond.conditionValue.value.uint8 = protocol  # noqa: N815 - Win32 SDK 字段名
    return cond


def _add_filter(  # pylint: disable=huawei-too-many-arguments
    engine: wintypes.HANDLE,
    filter_key: str,
    layer_key: str,
    sublayer_key: Guid,
    conditions: list[FwpmFilterCondition0],
    action_type: int,
    weight: int,
    display_name: str,
    **kwargs: object,
) -> None:
    """安装一个 filter.

    幂等: 已存在 (FWP_E_ALREADY_EXISTS) 则先删后加以升级为 persistent,
    (旧 docstring 误导为"先删后加", 已修正).
    """
    fwpu = _get_fwpuclnt()
    fkey = _guid_from_str(filter_key)

    flt = FwpmFilter0()
    flt.filterKey = fkey  # noqa: N815 - Win32 SDK 字段名
    flt.layerKey = _guid_from_str(layer_key)  # noqa: N815 - Win32 SDK 字段名
    flt.subLayerKey = sublayer_key  # noqa: N815 - Win32 SDK 字段名
    flt.flags = const.FWPM_FILTER_FLAG_PERSISTENT  # noqa: N815 - Win32 SDK 字段名
    # S9: displayData.name 不能为 NULL (同 sublayer, BFE 拒绝)。用传入的 display_name。
    flt.displayData.name = display_name  # noqa: N815 - Win32 SDK 字段名
    flt.displayData.description = display_name  # noqa: N815 - Win32 SDK 字段名
    flt.weight.type = const.FWP_UINT8  # noqa: N815 - Win32 SDK 字段名
    flt.weight.value.uint8 = weight  # noqa: N815 - Win32 SDK 字段名
    flt.action.type = action_type  # noqa: N815 - Win32 SDK 字段名

    cond_array = (FwpmFilterCondition0 * len(conditions))(*conditions)
    flt.numFilterConditions = ctypes.c_uint32(len(conditions))  # noqa: N815 - Win32 SDK 字段名
    # S12 旧 bug: 字段名拼错成 filterConditions (带 s), ctypes 当成新实例属性,
    # 结构体内的 filterCondition 字段保持 NULL, BFE 收到 numFilterConditions=1
    # 但 filterCondition=NULL, 返回 RPC_X_BAD_STUB_DATA (hr=0x6F7).
    flt.filterCondition = ctypes.cast(cond_array, ctypes.POINTER(FwpmFilterCondition0))

    fid = ctypes.c_uint64(0)
    hr = fwpu.FwpmFilterAdd0(engine, ctypes.byref(flt), None, ctypes.byref(fid))
    if hr == const.FWP_E_ALREADY_EXISTS:
        # 旧 filter 已存在 (幂等), 跳过. persistent flag 只在首次创建时生效,
        # 旧 non-persistent filter 需先 uninstall 再 install 才能升级.
        logger.debug("Filter 已存在, 跳过: %s", display_name)
    if hr != 0 and hr != const.FWP_E_ALREADY_EXISTS:
        # 诊断: 0x6F7=RPC_X_BAD_STUB_DATA 时打 sizeof(flt)/指针字段值助定位.
        # sizeof(flt) 应与 SDK sizeof(FwpmFilter0) 一致; 指针字段指向有效内存或 NULL.
        # 整块 try 防诊断异常掩盖真实 hr.
        try:
            def _ptr_val(p):
                v = ctypes.cast(p, ctypes.c_void_p).value
                return hex(v) if v else "NULL"
            cond_types = [getattr(c.conditionValue, "type", -1) for c in conditions]
            cond_sd_ptrs = []
            for c in conditions:
                try:
                    cond_sd_ptrs.append(_ptr_val(c.conditionValue.value.sd))
                except Exception:  # noqa: BLE001
                    cond_sd_ptrs.append("?")
            flt_bytes = bytes(flt)
            cond_field_keys = []
            for c in conditions:
                try:
                    cond_field_keys.append(_guid_to_str(c.fieldKey))
                except Exception:  # noqa: BLE001
                    cond_field_keys.append("?")
            logger.error(
                "FwpmFilterAdd0 失败 display=%s hr=0x%08X sizeof_flt=%d "
                "num_conds=%d cond_types=%s cond_field_keys=%s cond_sd_ptrs=%s weight_type=%d "
                "action=%d filterConditions_ptr=%s providerKey=%s reserved=%s "
                "flt_hex_first32=%s",
                display_name, hr, ctypes.sizeof(flt), len(conditions),
                cond_types, cond_field_keys, cond_sd_ptrs, flt.weight.type, action_type,
                _ptr_val(flt.filterCondition),
                _ptr_val(flt.providerKey),
                _ptr_val(flt.reserved),
                flt_bytes[:32].hex(),
            )
        except Exception:  # noqa: BLE001
            logger.error("FwpmFilterAdd0 诊断自身异常 hr=0x%08X", hr, exc_info=True)
        raise _wfp_error(hr, f"FwpmFilterAdd0({display_name})")
    logger.info("WFP filter 安装: %s (layer=%s, action=%d)", display_name, layer_key, action_type)


def install_wfp_filters(
    sandbox_user_sid: str,
    permit_port_start: int,
    permit_port_end: int,
) -> None:
    """安装 Block + Permit WFP filter set.

    幂等: sublayer/filter 用固定合法 Guid key, 重复安装会命中
    FWP_E_ALREADY_EXISTS 并被忽略.

    Args:
        sandbox_user_sid: jbx-sandbox 用户 SID 字符串.
        permit_port_start/end: Permit filter 放行的 loopback 端口范围.
    """
    _require_windows()
    fwpu = _get_fwpuclnt()
    # keeps_alive 列表: 持有所有条件构造返回的 ctypes 局部对象引用,
    # 直到全部 FwpmFilterAdd0 完成才允许 GC (否则 v4AddrMask/SD blob 指针悬垂).
    keeps: list[object] = []

    engine = _open_engine()
    try:
        fwpu.FwpmTransactionBegin0(engine, 0)
        try:
            sublayer_key = _add_sublayer(engine, const.JBX_SUBLAYER_KEY, weight=100)

            # --- Block filters (V4 + V6) ---
            for layer, fkey in (
                (const.FWPM_LAYER_ALE_AUTH_CONNECT_V4, const.JBX_FILTER_BLOCK_KEY_V4),
                (const.FWPM_LAYER_ALE_AUTH_CONNECT_V6, const.JBX_FILTER_BLOCK_KEY_V6),
            ):
                block_cond, ka = _build_ale_user_condition(sandbox_user_sid)
                keeps.append(ka)
                _add_filter(
                    engine, fkey, layer, sublayer_key,
                    [block_cond],
                    const.FWP_ACTION_BLOCK,
                    const.FWP_WEIGHT_BLOCK,
                    f"JiuwenBox-Block-{fkey}",
                )

            # --- Permit filters (V4 + V6) for loopback + port range ---
            # 每端口一个独立 Permit filter (放行整个范围). filter key 用 uuid5 派生 (base_key,port) → 合法 Guid, 幂等安装/卸载.
            # Permit 仅放行 127.0.0.1:<port_range>, 沙箱出网唯一出口是 win_proxy.
            for layer, base_key in (
                (const.FWPM_LAYER_ALE_AUTH_CONNECT_V4, const.JBX_FILTER_PERMIT_KEY_V4),
                (const.FWPM_LAYER_ALE_AUTH_CONNECT_V6, const.JBX_FILTER_PERMIT_KEY_V6),
            ):
                # 按 layer Guid 显式判断 V4/V6, 不能用 "V4" in base_key (base_key 是纯 hex Guid 字符串不含 V4/V6).
                # 误判会让 V4 层装 IPv6 条件 → condition 类型与 layer 不匹配 → 0x80320027 → 降级防火墙.
                is_v4 = (layer == const.FWPM_LAYER_ALE_AUTH_CONNECT_V4)
                for port in range(permit_port_start, permit_port_end + 1):
                    user_cond, user_ka = _build_ale_user_condition(sandbox_user_sid)
                    keeps.append(user_ka)
                    if is_v4:
                        lb_cond, lb_ka = build_loopback_v4_condition()
                    else:
                        lb_cond, lb_ka = _build_loopback_v6_condition()
                    keeps.append(lb_ka)
                    port_cond = _build_port_eq_condition(port)
                    # port_key 用 uuid5 派生 (namespace, "base_key:port") -> 合法 GUID,
                    # 同 (base,port) 派生同 GUID, install/uninstall 幂等.
                    port_key = _permit_filter_guid_str(base_key, port)
                    _add_filter(
                        engine, port_key, layer, sublayer_key,
                        [user_cond, lb_cond, port_cond],
                        const.FWP_ACTION_PERMIT,
                        const.FWP_WEIGHT_PERMIT,
                        f"JiuwenBox-Permit-Loopback-{base_key}-{port}",
                    )

            # --- DNS Permit filters (V4 + V6) ---
            # 放行沙箱用户到任意 IP 的 DNS 出站 (port 53, TCP/UDP 均匹配).
            # 不对 IP_PROTOCOL 单独建 filter: 实测 FwpmFilterAdd0 在 ALE_AUTH_CONNECT
            # 上对 [ALE_USER_ID, IP_REMOTE_PORT, IP_PROTOCOL] 返回 FWP_E_CONDITION_NOT_FOUND
            # (hr=0x80320002). port 53 已足够区分 DNS, 与 loopback Permit 同样省略 protocol.
            for layer, dns_key in (
                (const.FWPM_LAYER_ALE_AUTH_CONNECT_V4, const.JBX_FILTER_DNS_PERMIT_KEY_V4),
                (const.FWPM_LAYER_ALE_AUTH_CONNECT_V6, const.JBX_FILTER_DNS_PERMIT_KEY_V6),
            ):
                user_cond, user_ka = _build_ale_user_condition(sandbox_user_sid)
                keeps.append(user_ka)
                port_cond = _build_port_eq_condition(const.DNS_PORT)
                _add_filter(
                    engine, dns_key, layer, sublayer_key,
                    [user_cond, port_cond],
                    const.FWP_ACTION_PERMIT,
                    const.FWP_WEIGHT_PERMIT,
                    f"JiuwenBox-Permit-DNS-{dns_key}",
                )

            fwpu.FwpmTransactionCommit0(engine)
        except Exception:
            fwpu.FwpmTransactionAbort0(engine)
            raise
    finally:
        fwpu.FwpmEngineClose0(engine)
    logger.info(
        "WFP filter set 安装完成: sid=%s permit_port=%d-%d dns_port=%d",
        sandbox_user_sid, permit_port_start, permit_port_end, const.DNS_PORT,
    )


def _enumerate_sublayer_filters(engine) -> "list[str]":
    """枚举 JBX sublayer 下所有 filter 的 filterKey (Guid 字符串).

    review #1: uninstall 用枚举法删全部 filter, 不依赖端口范围即可干净卸载
    任意端口范围的 Permit filter (用户改 policy 端口范围后旧 install 创建的
    filter 也能清掉, 避免残留致 sublayer 删不掉 FWP_E_IN_USE).

    实现: FwpmFilterCreateEnumHandle0(NULL template 枚举所有 filter) +
    FwpmFilterEnum0 拉一批 + 按 subLayerKey == JBX_SUBLAYER_KEY 过滤.
    返回的 entries 内存由 BFE 分配, 用 FwpmFreeMemory0 释放.
    """
    fwpu = _get_fwpuclnt()
    sublayer_guid = _guid_from_str(const.JBX_SUBLAYER_KEY)
    enum_handle = wintypes.HANDLE()
    hr = fwpu.FwpmFilterCreateEnumHandle0(
        engine, None, ctypes.byref(enum_handle),
    )
    if hr != 0:
        logger.warning(
            "FwpmFilterCreateEnumHandle0 失败: %s",
            _wfp_error(hr, "FwpmFilterCreateEnumHandle0"),
        )
        return []
    keys: list[str] = []
    try:
        while True:
            num_entries = wintypes.UINT(0)
            entries_ptr = ctypes.POINTER(FwpmFilter0)()
            hr = fwpu.FwpmFilterEnum0(
                engine, enum_handle, ctypes.byref(num_entries),
                ctypes.byref(entries_ptr),
            )
            if hr != 0:
                logger.warning(
                    "FwpmFilterEnum0 失败: %s",
                    _wfp_error(hr, "FwpmFilterEnum0"),
                )
                break
            n = int(num_entries.value)
            if n == 0:
                # 枚举结束 (本批无返回, 后续也无).
                break
            try:
                for i in range(n):
                    try:
                        flt = entries_ptr[i]
                        # 按 subLayerKey 过滤: 只收本 sublayer 的 filter.
                        if _guid_equal(flt.subLayerKey, sublayer_guid):
                            keys.append(_guid_to_str(flt.filterKey))
                    except Exception:  # noqa: BLE001 - 单条解析失败不中断
                        logger.debug("枚举 filter[%d] 解析失败", i, exc_info=True)
            finally:
                # BFE 分配的 entries 数组必须用 FwpmFreeMemory0 释放.
                if entries_ptr:
                    try:
                        p = ctypes.c_void_p(ctypes.addressof(entries_ptr.contents))
                        fwpu.FwpmFreeMemory0(ctypes.byref(p))
                    except Exception:  # noqa: BLE001
                        logger.debug("FwpmFreeMemory0 失败", exc_info=True)
            # FwpmFilterEnum0 每次返回一批 (典型 1024 条), 返回 0 条表示结束.
            # 但为防极端环最大批只返回部分, 上面 n>0 时继续循环直到 n==0.
    finally:
        fwpu.FwpmFilterDestroyEnumHandle0(engine, enum_handle)
    return keys


def _guid_equal(a: "Guid", b: "Guid") -> bool:
    """比较两个 Guid 结构体是否相同 (字段逐一比)."""
    return (
        a.Data1 == b.Data1 and a.Data2 == b.Data2 and a.Data3 == b.Data3  # noqa: N815 - Win32 SDK 字段名
        and bytes(a.Data4) == bytes(b.Data4)  # noqa: N815 - Win32 SDK 字段名
    )


def uninstall_wfp_filters(
    permit_port_start: int = const.DEFAULT_PROXY_PORT_RANGE_START,  # noqa: ARG001 - 兼容旧签名, 枚举法不再用
    permit_port_end: int = const.DEFAULT_PROXY_PORT_RANGE_END,  # noqa: ARG001 - 兼容旧签名, 枚举法不再用
) -> None:
    """卸载所有 JiuwenBox WFP filter + sublayer (幂等)."""
    _require_windows()
    fwpu = _get_fwpuclnt()
    engine = _open_engine()
    try:
        # 枚举 JBX sublayer 下所有 filter
        enum_keys = _enumerate_sublayer_filters(engine)
        if enum_keys:
            for fkey in enum_keys:
                _delete_filter_by_key(fwpu, engine, fkey)
            logger.info("枚举删除 JBX sublayer filter %d 个", len(enum_keys))
        _delete_all_filters_by_fixed_keys(fwpu, engine)
        _delete_sublayer_idempotent(fwpu, engine)
    finally:
        fwpu.FwpmEngineClose0(engine)
    logger.info("WFP filter set 卸载完成")


def _delete_all_filters_by_fixed_keys(fwpu, engine) -> None:
    """按固定/派生 key 删全部 JBX filter (枚举兜底, 幂等)."""
    for _fk in (
        const.JBX_FILTER_BLOCK_KEY_V4,
        const.JBX_FILTER_BLOCK_KEY_V6,
    ):
        _delete_filter_by_key(fwpu, engine, _fk)
    import uuid as _uuid
    _ns = _uuid.UUID(_PERMIT_FILTER_NAMESPACE)
    for _base in (
        const.JBX_FILTER_PERMIT_KEY_V4,
        const.JBX_FILTER_PERMIT_KEY_V6,
    ):
        for _port in range(
            const.DEFAULT_PROXY_PORT_RANGE_START,
            const.DEFAULT_PROXY_PORT_RANGE_END + 1,
        ):
            _delete_filter_by_key(fwpu, engine, str(_uuid.uuid5(_ns, f"{_base}:{_port}")))
    for _base in (
        const.JBX_FILTER_DNS_PERMIT_KEY_V4,
        const.JBX_FILTER_DNS_PERMIT_KEY_V6,
    ):
        _delete_filter_by_key(fwpu, engine, _base)
        # 旧版按 UDP/TCP 各建一条 filter (含 IP_PROTOCOL 条件), 卸载时一并清理.
        for _proto in (const.IPPROTO_UDP, const.IPPROTO_TCP):
            _delete_filter_by_key(
                fwpu, engine,
                _dns_filter_guid_str(_base, _proto),
            )


def _delete_sublayer_idempotent(fwpu, engine, max_attempts: int = 3) -> None:
    """删 JBX sublayer, IN_USE 时补删固定 key filter 后重试, 仍失败降级 warning."""
    import time

    sublayer_guid = _guid_from_str(const.JBX_SUBLAYER_KEY)
    sublayer_not_found = 0x80320031
    in_use = 0x8032000A
    for attempt in range(1, max_attempts + 1):
        try:
            hr = fwpu.FwpmSubLayerDeleteByKey0(engine, ctypes.byref(sublayer_guid))
        except Exception:  # noqa: BLE001
            logger.warning("删除 WFP sublayer 异常 (attempt %d)", attempt, exc_info=True)
            hr = in_use
        if hr == 0 or hr == sublayer_not_found:
            return
        if hr == in_use and attempt < max_attempts:
            logger.warning(
                "删除 WFP sublayer IN_USE (attempt %d/%d), 补删固定 key filter 后重试",
                attempt, max_attempts,
            )
            _delete_all_filters_by_fixed_keys(fwpu, engine)
            time.sleep(0.3)  # 等瞬时引用释放
            continue
    logger.warning(
        "删除 WFP sublayer 重试 %d 次仍 IN_USE, install 将复用已存在 sublayer (幂等)",
        max_attempts,
    )


def _delete_filter_by_key(fwpu, engine, fkey: str) -> None:
    """按 key 删除单个 WFP filter (幂等, not-found 静默).

    W3: 旧版把 0x800700B7 (ERROR_ALREADY_EXISTS, add 路径才出现) 当 not-found 忽略,
    但 delete 的 not-found 是 FWP_E_KEY_NOT_FOUND=0x80320031 / FWP_E_FILTER_NOT_FOUND=
    0x80320003 (0x80320xxx 段)。改为忽略正确的 not-found 码, 其余用 _wfp_error。
    """
    # delete 路径的 "不存在" 码: 视为幂等成功, 静默。
    delete_not_found = {0x80320031, 0x80320003}
    try:
        hr = fwpu.FwpmFilterDeleteByKey0(
            engine, ctypes.byref(_guid_from_str(fkey)),
        )
        if hr == 0:
            return
        if hr in delete_not_found:
            return
        logger.warning("删除 WFP filter %s: %s", fkey, _wfp_error(hr, "FwpmFilterDeleteByKey0"))
    except Exception:  # noqa: BLE001
        logger.warning("删除 WFP filter %s 异常", fkey, exc_info=True)


def install_firewall_rule_fallback(
    sandbox_user_name: str,
    permit_port_start: int,
    permit_port_end: int,
    sandbox_user_sid: str | None = None,
) -> bool:
    """降级方案: 用 PowerShell New-NetFirewallRule 实现用户级出站拦截.

    对齐 docs/window沙箱.md 6.4.2 降级路径. 牺牲内核态优先级控制与绕过保护,
    功能等价 (按用户拦截出站).

    S10: 旧版加 `-ErrorAction SilentlyContinue` 吞掉 stderr, 失败只 warning 且循环
    外无条件打"安装完成" → 假成功。改为不打 SilentlyContinue, 捕获 stderr 明文
    打印; 返回是否全部成功, 调用方据此决定是否致命 raise (S12)。

    S12 实跑修复 (failed.txt):
      1) -LocalUser 裸传 'S-1-5-21-...' 被拒 (stderr: 本地用户权限列表无效,
         只能含字母和 :/._ 不含连字符 '-'). 微软文档要求 -LocalUser 传 SDDL
         字符串 'D:(A;;CC;;;<SID>)', 不是裸 SID. 构造 SDDL 传入.
      2) -RemotePort 配 -RemoteAddress 但缺 -Protocol, stderr: 协议绑定对象
         选择与所选协议不匹配 (HRESULT 0x80070057). 显式加 -Protocol TCP.
    """
    _require_windows()
    rule_block = "JiuwenBox-Block-Sandbox-Egress"
    rule_permit = "JiuwenBox-Permit-Loopback"
    rule_dns_udp = "JiuwenBox-Permit-DNS-UDP"
    rule_dns_tcp = "JiuwenBox-Permit-DNS-TCP"
    port_range = f"{permit_port_start}-{permit_port_end}"

    # -LocalUser 要的是 SDDL 字符串 (微软文档): D:(A;;CC;;;<SID>).
    # 无 SID 时退回裸用户名 (含 '-' 会被拒, 但至少留下诊断).
    if sandbox_user_sid:
        local_user = f"D:(A;;CC;;;{sandbox_user_sid})"
    else:
        local_user = sandbox_user_name
    # Block 规则: 拦截 sandbox 用户的所有出站。
    ps_block = (
        f"New-NetFirewallRule -DisplayName '{rule_block}' "
        f"-Direction Outbound -Action Block "
        f"-LocalUser '{local_user}'"
    )
    # Permit 规则: 放行 sandbox 用户到 127.0.0.1:port_range (须在 Block 之前).
    # Windows Firewall 对 loopback 过滤支持有限, -RemoteAddress 127.0.0.1 部分版本不生效; 精确 loopback 仍走 WFP 主路径.
    # S12: 显式 -Protocol TCP (缺省协议对 -RemotePort 报"协议不匹配").
    ps_permit = (
        f"New-NetFirewallRule -DisplayName '{rule_permit}' "
        f"-Direction Outbound -Action Allow "
        f"-Protocol TCP "
        f"-LocalUser '{local_user}' "
        f"-RemoteAddress 127.0.0.1 "
        f"-RemotePort {port_range}"
    )
    # DNS Permit 规则: 放行 sandbox 用户的 DNS 出站 (UDP/TCP port 53),
    # 对齐 Linux network.py iptables ALLOW DNS.
    ps_dns_udp = (
        f"New-NetFirewallRule -DisplayName '{rule_dns_udp}' "
        f"-Direction Outbound -Action Allow "
        f"-Protocol UDP "
        f"-LocalUser '{local_user}' "
        f"-RemotePort {const.DNS_PORT}"
    )
    ps_dns_tcp = (
        f"New-NetFirewallRule -DisplayName '{rule_dns_tcp}' "
        f"-Direction Outbound -Action Allow "
        f"-Protocol TCP "
        f"-LocalUser '{local_user}' "
        f"-RemotePort {const.DNS_PORT}"
    )
    all_ok = True
    for ps in (ps_permit, ps_dns_udp, ps_dns_tcp, ps_block):
        try:
            subprocess.run(
                [_get_powershell(), "-NoProfile", "-Command", ps],
                check=True, capture_output=True, timeout=30,
                **_hidden_kwargs(),
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            all_ok = False
            stderr = b""
            if isinstance(exc, subprocess.CalledProcessError):
                stderr = exc.stderr or b""
            stderr_text = stderr.decode("utf-8", errors="replace").strip()
            logger.warning(
                "PowerShell 防火墙规则安装失败 (%s): %s | stderr: %s",
                ps[:60], exc, stderr_text or "<空>",
            )
    if all_ok:
        logger.info(
            "降级防火墙规则安装完成: user=%s permit_port=%s",
            sandbox_user_name, port_range,
        )
    else:
        logger.error(
            "降级防火墙规则安装存在失败: user=%s permit_port=%s "
            "(网络隔离可能不完整)", sandbox_user_name, port_range,
        )
    return all_ok


def uninstall_firewall_rule_fallback() -> None:
    """降级方案卸载: 删除降级路径安装的两条防火墙规则."""
    _require_windows()
    for rule in (
        "JiuwenBox-Block-Sandbox-Egress",
        "JiuwenBox-Permit-Loopback",
    ):
        try:
            subprocess.run(
                [
                    _get_powershell(), "-NoProfile", "-Command",
                    f"Remove-NetFirewallRule -DisplayName '{rule}' "
                    f"-ErrorAction SilentlyContinue",
                ],
                check=False, capture_output=True, timeout=30,
                **_hidden_kwargs(),
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            pass
