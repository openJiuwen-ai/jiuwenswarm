# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Windows 沙箱常量定义 (纯 Python, 无 win32 依赖).

集中存放 Token / Job Object / WFP / 文件 ACL 相关的 magic number 与
结构体字段常量. 模块顶层不加载任何 win32 库 (``ctypes``/``pywin32``),
因此在 Linux 下可正常 import, 也可被单元测试直接断言常量值.

运行时的 win32 API 调用统一延迟到 ``win_*.py`` 各功能模块内部, 并以
``sys.platform == "win32"`` 守卫, 详见 ``docs/window沙箱.md`` 第6章.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# 合成 SID (Synthetic SID) - Windows 沙箱文件写入控制的核心标记.
#
# 该 SID 不关联任何真实账户, 仅作为 "允许写入" 的权限标记出现在 NTFS
# DACL 中. 沙箱进程的 Write-Restricted Token 必须携带此 SID 才能写入
# 白名单路径. 详见 ``docs/window沙箱.md`` 2.2 / 6.7.
#
# 格式: S-1-5-21-<machine>-<sub-authority>-<RID>. 这里用一个固定的
# sub-authority 序列 + 一个明显高于真实用户 RID 的值, 避免与真实账户
# 碰撞 (真实账户 RID 通常 < 1000).
# ---------------------------------------------------------------------------
SANDBOX_USER_NAME = "jbx-sandbox"
SANDBOX_USER_GROUP = "jbx-sandbox-users"
SYNTHETIC_WRITE_SID_PREFIX = "S-1-5-21"
# 固定的 sub-authority 区段, 与任何真实域/机器账户错开.
SYNTHETIC_WRITE_SID_SUBAUTHS: tuple[int, ...] = (
    0xBABE0013,  # 机器标识占位 (实际取自安装机器)
    0x00002000,  # 子权限
)
SYNTHETIC_WRITE_SID_RID = 0x0000C0DE  # 合成 SID 的 RID

# 沙箱用户密码长度 (安装时随机生成).
SANDBOX_USER_PASSWORD_LENGTH = 64

# ---------------------------------------------------------------------------
# Token 信息类 (TOKEN_INFORMATION_CLASS) - GetTokenInformation 参数.
# 仅列出沙箱用到的几个, 完整列表见 winnt.h.
# ---------------------------------------------------------------------------
TOKEN_USER = 1
TOKEN_GROUPS = 2
TOKEN_PRIVILEGES = 3
TOKEN_OWNER = 4
TOKEN_PRIMARY_GROUP = 5
TOKEN_DEFAULT_DACL = 6
TOKEN_SOURCE = 7
TOKEN_TYPE = 8
TOKEN_IMPERSONATION_LEVEL = 9
TOKEN_STATISTICS = 11
TOKEN_RESTRICTIONS = 13
TOKEN_SESSION_ID = 14
TOKEN_GROUPS_AND_PRIVILEGES = 15
TOKEN_SESSION_REFERENCE = 16
TOKEN_SANDBOX_INERT = 29

# ---------------------------------------------------------------------------
# CreateRestrictedToken 标志 (对齐 winnt.h).
#   DISABLE_MAX_PRIVILEGE = 0x1 : 清除 token 中所有特权.
#   SANDBOX_INERT        = 0x2 : 标记 token 为沙箱 inert (某些路径豁免检查).
#   LUA_TOKEN            = 0x4 : 创建 UAC 筛选 token (低完整性), 非本项目所需.
#   WRITE_RESTRICTED     = 0x8 : 只对写操作做 Restricted SID 双重 ACL 检查.
#
# 注意: 旧版误把 SANDBOX_INERT 标成 0x4 (实为 LUA_TOKEN 的值), RESTRICTED_TOKEN_FLAGS
# 实际组合出 DISABLE_MAX_PRIVILEGE | LUA_TOKEN | WRITE_RESTRICTED, 与文档 6.5
# 要求的 SANDBOX_INERT 语义不符. 已据 winnt.h 改回 0x2.
# ---------------------------------------------------------------------------
DISABLE_MAX_PRIVILEGE = 0x1
SANDBOX_INERT = 0x2
WRITE_RESTRICTED = 0x8

# CreateRestrictedToken 组合: 文档 6.5 要求的受限 SID 列表 =
# [Everyone, 当前 LogonSession, JHXSandboxWrite].
# 受限 token 实跑验证失败 (2026-08-02): WRITE_RESTRICTED 下 bash/python 启动即
# 0xC0000142 (STATUS_DLL_INIT_FAILED), 故 exec 不用受限 token (改用 runner 未受限
# primary token). _create_restricted_token 仍被 runner_main 构造但 exec 不消费
# (dead code). WRITE_RESTRICTED 暂不 OR 进 flags, 待受限 token 0xC0000142 根因
# (desktop/全局对象机制) 解决后再恢复.
RESTRICTED_TOKEN_FLAGS = DISABLE_MAX_PRIVILEGE | SANDBOX_INERT  # 去掉 WRITE_RESTRICTED(0x8)

# ---------------------------------------------------------------------------
# WellKnownSid 类型 (CreateWellKnownSid 的枚举值).
#   WinWorldSid   -> Everyone (S-1-1-0)
#   WinNullSid    -> S-1-0-0
#   WinLocalSystemSid -> S-1-5-18
# ---------------------------------------------------------------------------
WIN_WORLD_SID = 1  # WinWorldSid -> Everyone

# ---------------------------------------------------------------------------
# LogonUser / CreateProcessWithLogonW / CreateProcessAsUser 标志.
# ---------------------------------------------------------------------------
LOGON32_LOGON_INTERACTIVE = 2
LOGON32_PROVIDER_DEFAULT = 0

# CreateProcessW / CreateProcessAsUserW dwCreationFlags.
CREATE_SUSPENDED = 0x00000004
CREATE_NEW_PROCESS_GROUP = 0x00000200
# 传入 Unicode (UTF-16) 环境块时必须带此 flag, 否则按 ANSI 解析 env block →
# WinError 87 (参数错误). two_hop_spawn / _create_process_as_user 传 env 块时用.
CREATE_UNICODE_ENVIRONMENT = 0x00000400
CREATE_NO_WINDOW = 0x08000000
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
EXTENDED_STARTUPINFO_PRESENT = 0x00080000

# 进程/线程优先级类.
NORMAL_PRIORITY_CLASS = 0x20

# DUPLICATE_... 权限 (DuplicateTokenEx).
DUPLICATE_SAME_ACCESS = 0x2

# TOKEN 类型 (DuplicateTokenEx).
TOKEN_PRIMARY = 1
TOKEN_IMPERSONATION = 2

# 安全模拟级别 (SECURITY_IMPERSONATION_LEVEL).
SecurityImpersonation = 2  # noqa: N806 - Win32 SDK 常量

# ---------------------------------------------------------------------------
# 用户账户标志 (USER_INFO_1.usri1_flags / NetUserAdd), 对齐 lmaccess.h.
#   UF_SCRIPT             = 0x0001   (NetUserAdd 强制要求)
#   UF_ACCOUNTDISABLE     = 0x0002  (禁用账户; 沙箱用户不能设, 否则 LogonUser 失败)
#   UF_HOMEDIR_REQUIRED   = 0x0008
#   UF_PASSWD_CANT_CHANGE = 0x0040   (用户不能改密码)
#   UF_NORMAL_ACCOUNT     = 0x0200   (普通账户类型)
#   UF_DONT_EXPIRE_PASSWD = 0x10000  (密码不过期)
#
# 旧版误把 UF_DONT_EXPIRE_PASSWD 标成 0x0200 (实为 UF_NORMAL_ACCOUNT 的值),
# 并声称"二者历史上同占 0x0200 但语义域不同故不冲突" — 这与 lmaccess.h 实际定义
# 不符: UF_DONT_EXPIRE_PASSWD 是 0x10000, 与 UF_NORMAL_ACCOUNT(0x0200) 位宽
# 完全不重叠. 旧值导致 SANDBOX_USER_FLAGS 漏设"密码不过期"位, 沙箱用户密码
# 会按本地账户策略过期. 已据 lmaccess.h 改回 0x10000.
# ---------------------------------------------------------------------------
UF_SCRIPT = 0x0001
UF_ACCOUNTDISABLE = 0x0002
UF_HOMEDIR_REQUIRED = 0x0008
UF_PASSWD_CANT_CHANGE = 0x0040
UF_DONT_EXPIRE_PASSWD = 0x10000
UF_NORMAL_ACCOUNT = 0x0200

# 沙箱用户最终 flag: 脚本位 + 不改密码 + 不过期 + 普通账户. 不设 DISABLE.
# 显式 OR UF_NORMAL_ACCOUNT: 旧值 UF_DONT_EXPIRE_PASSWD=0x0200 与 UF_NORMAL_ACCOUNT
# 同值, 隐式带上了普通账户位; 改回 0x10000 后二者不再重叠, 必须显式列出
# UF_NORMAL_ACCOUNT, 否则账户缺普通账户类型标记.
SANDBOX_USER_FLAGS = (
    UF_SCRIPT | UF_PASSWD_CANT_CHANGE | UF_DONT_EXPIRE_PASSWD | UF_NORMAL_ACCOUNT
)

# NetLocalGroupAddMembers 预定义级别.
LOCALGROUP_MEMBERS_INFO_0 = 0

# NetUserAdd 信息级别.
USER_INFO_1_LEVEL = 1

# ---------------------------------------------------------------------------
# Job Object 信息类 (JOBOBJECTINFOCLASS).
#   JobObjectBasicLimitInformation        = 2  (进程数上限等)
#   JobObject ExtendedLimitInformation   = 9  (内存上限 / KILL_ON_CLOSE 等)
#   JobObject CpuRateControlInformation  = 15 (CPU 速率)
#   JobObject AssociateCompletionPortInformation = 7
#   JobObject GroupInformation            = 11
# ---------------------------------------------------------------------------
JobObjectBasicLimitInformation = 2  # noqa: N806 - Win32 SDK 常量
JobObjectExtendedLimitInformation = 9  # noqa: N806 - Win32 SDK 常量
JobObjectCpuRateControlInformation = 15  # noqa: N806 - Win32 SDK 常量

# Job Object 基本限制标志 (JOBOBJECT_BASIC_LIMIT_INFORMATION.LimitFlags).
JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0x00000400
JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK = 0x00001000

# Job Object 扩展限制标志 (JOBOBJECT_EXTENDED_LIMIT_INFORMATION.BasicLimit.LimitFlags).
# 内存限制位.
JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200
JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION = 0x00000400

# CPU 速率控制标志 (JOBOBJECT_CPU_RATE_CONTROL_INFORMATION.ControlFlags).
JOB_OBJECT_CPU_RATE_CONTROL_ENABLE = 0x00000001
JOB_OBJECT_CPU_RATE_CONTROL_WEIGHT_BASED = 0x00000002
JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP = 0x00000004
JOB_OBJECT_CPU_RATE_CONTROL_NOTIFY = 0x00000008

# CPU 速率以 0.01% 为单位, 范围 [1, 10000] (即 0.01% ~ 100%).
CPU_RATE_MIN = 1
CPU_RATE_MAX = 10000

# ---------------------------------------------------------------------------
# 文件 ACL 访问掩码 (Access Mask) - 用于 ACE 的权限位.
# 对齐 docs/window沙箱.md 6.7: allow_write 施加 Allow Write+Execute+Delete.
# ---------------------------------------------------------------------------
FILE_GENERIC_READ = 0x00120089
FILE_GENERIC_WRITE = 0x00120116
FILE_GENERIC_EXECUTE = 0x001200A0
FILE_DELETE_ACCESS = 0x00010000  # DELETE 位
FILE_READ_ATTRIBUTES = 0x00000080
# STANDARD_RIGHTS: install 预授给当前用户, 运行时才能改外部目录 DACL.
# winnt.h: READ_CONTROL=0x20000, WRITE_DAC=0x40000.
READ_CONTROL = 0x00020000
WRITE_DAC = 0x00040000
# FILE_EXECUTE / FILE_TRAVERSE 同值; 父目录非递归 traverse 用.
FILE_TRAVERSE = 0x00000020

FILE_WRITE_DATA = 0x00000002
FILE_APPEND_DATA = 0x00000004
FILE_WRITE_EA = 0x00000010
FILE_WRITE_ATTRIBUTES = 0x00000100

# allow_write 路径授予的写权限组合.
ALLOW_WRITE_RIGHTS = FILE_GENERIC_WRITE | FILE_GENERIC_EXECUTE | FILE_DELETE_ACCESS
# 工具目录 / python.exe 预装: FILE_GENERIC_READ 不含 FILE_EXECUTE/FILE_TRAVERSE,
# 只授 Read 时 jbx-sandbox 无法 CreateProcessWithLogonW (WinError 5).
ALLOW_READ_EXECUTE_RIGHTS = FILE_GENERIC_READ | FILE_GENERIC_EXECUTE
# deny_write 路径封锁的写权限组合: 只拒绝写特定位, 不含 SYNCHRONIZE/READ_CONTROL
# (这两个位也属于 FILE_GENERIC_READ, 若出现在 Deny mask 中会阻断读访问).
DENY_WRITE_RIGHTS = FILE_WRITE_DATA | FILE_APPEND_DATA | FILE_WRITE_EA | FILE_WRITE_ATTRIBUTES
# read 控制中 deny 施加的读权限.
DENY_READ_RIGHTS = FILE_GENERIC_READ

# ACL/ACE 类型.
ACCESS_ALLOWED_ACE_TYPE = 0
ACCESS_DENIED_ACE_TYPE = 1
INHERITED_ACE = 0x10

# ACE 继承标志 (AceFlags), 用于容器/子对象继承.
CONTAINER_INHERIT_ACE = 0x2
OBJECT_INHERIT_ACE = 0x1
# SUB_CONTAINERS_AND_OBJECTS_INHERIT: 容器+对象都继承. 注意: 这是 CONTAINER_INHERIT
# | OBJECT_INHERIT 的组合常量 (=0x3), 不是单独的 0x7. 旧版误写成 0x7 (含
# NO_PROPAGATE_INHERIT_ACE=0x4), 导致 recursive grant 的 ACE 只继承到直接子项、
# 不向下传播到孙目录 (实测: workspace\.tmp\playwright-download-* 子目录没继承
# 合成 SID/jbx-sandbox ACE → child 在子目录里写文件 EPERM).
SUB_CONTAINERS_AND_OBJECTS_INHERIT = CONTAINER_INHERIT_ACE | OBJECT_INHERIT_ACE
INHERIT_ONLY_ACE = 0x8
NO_PROPAGATE_INHERIT_ACE = 0x4

# 递归施加 ACE 时使用的继承标志 (目录 + 所有子对象, 向下传播到整棵子树).
RECURSIVE_ACE_FLAGS = (
    CONTAINER_INHERIT_ACE
    | OBJECT_INHERIT_ACE
    | SUB_CONTAINERS_AND_OBJECTS_INHERIT
)

# SECURITY_INFORMATION 标志 (Get/SetNamedSecurityInfo).
DACL_SECURITY_INFORMATION = 0x4
OWNER_SECURITY_INFORMATION = 0x1
GROUP_SECURITY_INFORMATION = 0x2
PROTECTED_DACL_SECURITY_INFORMATION = 0x40000000  # 阻止继承的 DACL

# SE_OBJECT_TYPE (Get/SetNamedSecurityInfo 第一个参数类型).
SE_FILE_OBJECT = 1

# ---------------------------------------------------------------------------
# WFP (Windows Filtering Platform) 常量.
# 详见 fwpmtypes.h / fwpsu.h. 文档 6.4.2 要求安装 Block + Permit filter.
# ---------------------------------------------------------------------------

# RPC_C_AUTHN_WINNT 鉴别级别 (FwpmEngineOpen).
RPC_C_AUTHN_WINNT = 10
RPC_C_AUTHN_LEVEL_DEFAULT = 0

# Filter Action 类型 (FWP_ACTION_TYPE). 取值对齐 fwptypes.h.
FWP_ACTION_FLAG_TERMINATING = 0x00001000
FWP_ACTION_BLOCK = 0x00000001 | FWP_ACTION_FLAG_TERMINATING
FWP_ACTION_PERMIT = 0x00000002 | FWP_ACTION_FLAG_TERMINATING
FWP_ACTION_CALLOUT_TERMINATING = 0x00004000 | FWP_ACTION_FLAG_TERMINATING

# FWP_ACTRL_* (条件匹配权限位). ALE_USER_ID 条件评估 SD 时, 检查
# FWP_ACTRL_MATCH_FILTER 访问权是否被 DACL 授予 (SDK: Permitting and
# Blocking Applications and Users).
FWP_ACTRL_MATCH_FILTER = 0x00000001

# Filter Weight (FWP_VALUE0.uint8 范围 0..15, 文档要求 Permit > Block).
FWP_WEIGHT_BLOCK = 0x0  # Block filter 权重最低
FWP_WEIGHT_PERMIT = 0xF  # Permit filter 权重最高, 覆盖 Block

# ALE (Application Layer Enforcement) Layer GUIDs (网络出站拦截层).
# 取值对齐 fwpmu.h DEFINE_GUID (真实 SDK 值, 非 DCE UUID 字面量需注意
# Windows GUID 内存布局 little-endian, 见 win_wfp._guid_from_str).
# FWPM_LAYER_ALE_AUTH_CONNECT_V4 / V6 - 出站连接授权层.
# S12 旧 bug: V4 的 GUID 第 4 段 904F 误写成 900F, BFE 返回
# FWP_E_LAYER_NOT_FOUND (0x80320004). 对照 fwpmu.h DEFINE_GUID 修正.
FWPM_LAYER_ALE_AUTH_CONNECT_V4 = "C38D57D1-05A7-4C33-904F-7FBCEEE60E82"
FWPM_LAYER_ALE_AUTH_CONNECT_V6 = "4A72393B-319F-44BC-84C3-BA54DCB3B6B4"

# Filter Condition 字段 (FWPM_CONDITION_...). 取值对齐 fwpmu.h DEFINE_GUID.
FWPM_CONDITION_ALE_USER_ID = "AF043A0A-B34D-4F86-979C-C90371AF6E66"
FWPM_CONDITION_IP_REMOTE_ADDRESS = "B235AE9A-1D64-49B8-A44C-5FF3D9095045"
FWPM_CONDITION_IP_REMOTE_PORT = "C35A604D-D22B-4E1A-91B4-68F674EE674B"

# Condition 匹配类型 (FWP_MATCH_TYPE, fwptypes.h).
FWP_MATCH_EQUAL = 0
FWP_MATCH_GREATER = 1
FWP_MATCH_LESS = 2
FWP_MATCH_GREATER_OR_EQUAL = 3
FWP_MATCH_LESS_OR_EQUAL = 4
FWP_MATCH_RANGE = 5
FWP_MATCH_FLAGS_ALL_SET = 6
FWP_MATCH_FLAGS_ANY_SET = 7
FWP_MATCH_FLAGS_NONE_SET = 8
FWP_MATCH_EQUAL_CASE_INSENSITIVE = 9
FWP_MATCH_NOT_EQUAL = 10

# FWP_DATA_TYPE (FWP_VALUE0.Type / FWP_CONDITION_VALUE0.Type).
# 取值严格对齐 fwptypes.h FWP_DATA_TYPE 枚举 (注意: 旧版常量值与 SDK 错位,
# FWP_SID 应为 13 而非 12; FWP_BYTE_BLOB_TYPE 应为 12 而非 16).
FWP_EMPTY = 0
FWP_UINT8 = 1
FWP_UINT16 = 2
FWP_UINT32 = 3
FWP_UINT64 = 4
FWP_INT8 = 5
FWP_INT16 = 6
FWP_INT32 = 7
FWP_INT64 = 8
FWP_FLOAT = 9
FWP_DOUBLE = 10
FWP_BYTE_ARRAY16_TYPE = 11
FWP_BYTE_BLOB_TYPE = 12
FWP_SID = 13
FWP_SECURITY_DESCRIPTOR_TYPE = 14
FWP_TOKEN_INFORMATION_TYPE = 15
FWP_TOKEN_ACCESS_INFORMATION_TYPE = 16
FWP_UNICODE_STRING_TYPE = 17
FWP_BYTE_ARRAY6_TYPE = 18
FWP_SINGLE_DATA_TYPE_MAX = 0xFF
# FWP_CONDITION_VALUE0 扩展类型 (FWP_DATA_TYPE 续, 值 > 0xFF).
FWP_V4_ADDR_MASK = 0x100   # 256
FWP_V6_ADDR_AND_MASK = 0x101  # 257
FWP_RANGE_TYPE = 0x102     # 258
# 别名 (win_wfp.py 用 _TYPE 后缀形式引用, 保持向后兼容).
FWP_V4_ADDR_MASK_TYPE = FWP_V4_ADDR_MASK
FWP_V6_ADDR_AND_MASK_TYPE = FWP_V6_ADDR_AND_MASK

# FWPM_SESSION0 flags (FWPM_SESSION_FLAG_*).
FWPM_SESSION_FLAG_NONE = 0x0
FWPM_SESSION_FLAG_DYNAMIC = 0x00000001  # session 内添加的对象随 session 结束自动删除
# 别名: 旧代码用 FWP_SESSION_FLAG_NONE 命名 (SDK 实际为 FWPM_SESSION_FLAG_*).
FWP_SESSION_FLAG_NONE = FWPM_SESSION_FLAG_NONE

# FWPM_SUBLAYER0 flags.
FWPM_SUBLAYER_FLAG_PERSISTENT = 0x00000001

# FWPM_FILTER0 flags.
FWPM_FILTER_FLAG_NONE = 0x00000000
FWPM_FILTER_FLAG_PERSISTENT = 0x00000001

# WFP HRESULT error codes (fwptypes.h / fwpmtypes.h).
FWP_E_ALREADY_EXISTS = 0x80320009

# Sublayer key (固定合法 GUID, 幂等安装/卸载时用同一 key). 必须是合法
# UUID 字符串, win_wfp._guid_from_str 用 uuid.UUID() 解析, 非法字符串会
# 直接 ValueError 导致 WFP 安装从未执行 (review CRITICAL #2).
JBX_SUBLAYER_KEY = "8F2A1B3C-4D5E-6F70-8190-123456789ABC"

# Filter key (固定合法 GUID, 幂等安装/卸载时按 key 删除).
JBX_FILTER_BLOCK_KEY_V4 = "9A3B2C1D-5E6F-7081-9012-3456789ABCDE"
JBX_FILTER_BLOCK_KEY_V6 = "AB4C3D2E-6F70-8192-0123-456789ABCDEF"
JBX_FILTER_PERMIT_KEY_V4 = "BC5D4E3F-7081-9203-1234-56789ABCDEF0"
JBX_FILTER_PERMIT_KEY_V6 = "CD6E5F40-8192-0314-2345-6789ABCDEF01"

# DNS Permit filter key (放行沙箱 DNS 出站, 对齐 Linux network.py port 53 放行).
JBX_FILTER_DNS_PERMIT_KEY_V4 = "DE7F6051-9203-1425-3456-789ABCDEF012"
JBX_FILTER_DNS_PERMIT_KEY_V6 = "EF806162-0314-2536-4567-89ABCDEF0123"

# DNS 端口 (UDP/TCP 53).
DNS_PORT = 53
# IP 协议号 (对齐 winnt.h / RFC 790).
IPPROTO_TCP = 6
IPPROTO_UDP = 17

# FWPM_CONDITION_IP_PROTOCOL 条件 GUID (fwpmu.h DEFINE_GUID).
FWPM_CONDITION_IP_PROTOCOL = "D7A1D977-38F8-4D6D-8E5C-FA9A1E7706C3"

# 出站代理端口范围 (默认, 与 docs 6.6 对齐).
DEFAULT_PROXY_PORT_RANGE_START = 60080
DEFAULT_PROXY_PORT_RANGE_END = 60089

# Permit filter 放行的 loopback 地址 (IPv4 整数表示, 127.0.0.1).
# WFP FWP_V4_ADDR_AND_MASK.addr 要求 host byte order (NOT network order).
# host order: 127.0.0.1 -> 0x7F000001; 旧值 0x0100007F 是网络序, 会让
# Permit filter 匹配 1.0.0.127 而非 127.0.0.1 (review CRITICAL #4).
LOOPBACK_IPV4_INT = 0x7F000001  # 127.0.0.1 in host byte order

# ---------------------------------------------------------------------------
# 安装状态注册表路径 (幂等标记 + SID 缓存 + 代理端口等).
# HKLM\Software\JiuwenBox\WindowsSandbox
# ---------------------------------------------------------------------------
REG_BASE_KEY = r"Software\JiuwenBox\WindowsSandbox"
REG_VALUE_INSTALLED = "installed"
REG_VALUE_SANDBOX_USER_SID = "sandbox_user_sid"
REG_VALUE_SYNTHETIC_WRITE_SID = "synthetic_write_sid"
REG_VALUE_SANDBOX_USER_PW = "sandbox_user_pw_encrypted"
REG_VALUE_READ_ACL_PROGRESS = "read_acl_progress"
# 已预装读 ACL 的完整路径集合 (JSON). ensure_windows_setup 幂等检查时对比
# 本次 preinstall_paths, 若有新增路径 (如用户改了 tool_paths 后首次起 sandbox)
# 则提示需 --force 重装让管理员补预装; 运行时普通用户无权改外部目录 DACL.
REG_VALUE_PREINSTALLED_PATHS = "preinstalled_paths"
# install 时已预授 WRITE_DAC 的 deny/allow 路径集合 (JSON).
# ensure_windows_setup 增量检测: runtime policy 新增 deny/allow 路径时自动弹 UAC 补授权.
REG_VALUE_ACL_POLICY_PATHS = "acl_policy_paths"

# UAC 提权子进程的命令行标记.
INSTALL_SUBCOMMAND = "--install"
UNINSTALL_SUBCOMMAND = "--uninstall"
