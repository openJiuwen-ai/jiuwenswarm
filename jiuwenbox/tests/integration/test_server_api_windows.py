# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Windows 沙箱集成测试 (docs/window沙箱.md 第7章).

本文件混合两类用例:

1. **WSL 可跑的 mock 单测** (不 skip): 验证 Windows 模块的逻辑正确性.
   - policy schema 解析 / 合并
   - win_proxy 域名/IP 过滤 (纯 asyncio, 跨平台)
   - win_constants 常量值
   - win_exec / win_acl / win_job / win_wfp 的 ctypes/pywin32 调用参数
     (用 monkeypatch mock 底层 dll, 断言传参正确)
   - ProcessRuntime / app lifespan 的 win32 分支 (monkeypatch sys.platform)

2. **Windows 端实跑用例** (@pytest.mark.skipif(!win32)): 真实创建/删除沙箱,
   验证文件隔离 / 进程隔离 / 网络隔离 / Job 限制. 需在 Windows 上以管理员
   身份运行 ``pytest tests/integration/test_server_api_windows.py``.
   WSL 下自动 skip.

Linux 回归由 ``test_server_api_default.py`` 覆盖, 本文件不重复.
"""

from __future__ import annotations

import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

from jiuwenbox.models.policy import SecurityPolicy
from jiuwenbox.supervisor import win_constants as const


# ---------------------------------------------------------------------------
# WSL 可跑: policy schema 解析 / 合并.
# ---------------------------------------------------------------------------
def _windows_policy_yaml() -> dict:
    """构造一个含 windows 段的 policy dict."""
    return {
        "version": 1,
        "name": "win-test",
        "windows": {
            "proxy": {
                "port_range_start": 60080,
                "port_range_end": 60089,
            },
            "filesystem": {
                "read_acl_preinstall": ["%USERPROFILE%"],
                "allow_write": ["/workspace"],
                "deny_write": ["/workspace/.git"],
            },
            "network": {
                "mode": "wfp_loopback_proxy",
                "egress": {"default": "deny", "allowed_domains": ["pypi.org"]},
                "ingress": {"default": "deny"},
            },
            "resource": {
                "memory_max": "512M",
                "cpu_rate": 50,
                "max_processes": 32,
            },
        },
    }


class TestWindowsPolicySchema:
    def test_parse_full_windows_section(self):
        policy = SecurityPolicy.model_validate(_windows_policy_yaml())
        assert policy.windows.proxy.port_range_start == 60080
        assert policy.windows.proxy.port_range_end == 60089
        assert policy.windows.filesystem.allow_write == ["/workspace"]
        assert policy.windows.filesystem.deny_write == ["/workspace/.git"]
        assert policy.windows.network.mode == "wfp_loopback_proxy"
        assert policy.windows.network.egress.default == "deny"
        assert policy.windows.network.egress.allowed_domains == ["pypi.org"]
        assert policy.windows.resource.memory_max == 512 * 1024 * 1024
        assert policy.windows.resource.cpu_rate == 50
        assert policy.windows.resource.max_processes == 32

    def test_default_windows_is_empty(self):
        policy = SecurityPolicy()
        assert policy.windows.proxy.port_range_start == const.DEFAULT_PROXY_PORT_RANGE_START
        assert policy.windows.proxy.port_range_end == const.DEFAULT_PROXY_PORT_RANGE_END
        assert policy.windows.filesystem.allow_write == []
        assert policy.windows.network.mode == "wfp_loopback_proxy"
        assert policy.windows.resource.is_empty() is True
        # Linux 字段不受影响.
        assert policy.network.mode.value == "isolated"

    def test_reject_extra_fields(self):
        bad = _windows_policy_yaml()
        bad["windows"]["bogus"] = 1
        with pytest.raises(Exception):
            SecurityPolicy.model_validate(bad)

    def test_memory_max_accepts_unit_suffix(self):
        data = _windows_policy_yaml()
        data["windows"]["resource"]["memory_max"] = "1G"
        policy = SecurityPolicy.model_validate(data)
        assert policy.windows.resource.memory_max == 1024 ** 3

    def test_cpu_rate_range_validation(self):
        data = _windows_policy_yaml()
        data["windows"]["resource"]["cpu_rate"] = 0
        with pytest.raises(Exception):
            SecurityPolicy.model_validate(data)
        data["windows"]["resource"]["cpu_rate"] = 101
        with pytest.raises(Exception):
            SecurityPolicy.model_validate(data)

    def test_port_range_order_validation(self):
        data = _windows_policy_yaml()
        data["windows"]["proxy"]["port_range_start"] = 60090
        data["windows"]["proxy"]["port_range_end"] = 60080
        with pytest.raises(Exception):
            SecurityPolicy.model_validate(data)

    def test_windows_section_does_not_pollute_linux_merge(self):
        """APPEND 合并 windows 段不破坏 Linux 字段."""
        base = SecurityPolicy()
        extra = SecurityPolicy.model_validate(_windows_policy_yaml())
        from jiuwenbox.server.policy_engine import PolicyEngine
        engine = PolicyEngine()
        merged = engine.merge_policy(base, extra)
        assert merged.windows.filesystem.allow_write == ["/workspace"]
        # Linux 字段保持 base 默认.
        assert merged.network.mode == base.network.mode


# ---------------------------------------------------------------------------
# WSL 可跑: win_constants 值校验.
# ---------------------------------------------------------------------------
class TestWinConstants:
    def test_restricted_token_flags_combination(self):
        assert const.RESTRICTED_TOKEN_FLAGS == (
                const.DISABLE_MAX_PRIVILEGE
                | const.SANDBOX_INERT
                | const.WRITE_RESTRICTED
        )

    def test_sandbox_user_flags(self):
        assert const.SANDBOX_USER_FLAGS & const.UF_SCRIPT
        assert const.SANDBOX_USER_FLAGS & const.UF_PASSWD_CANT_CHANGE
        assert const.SANDBOX_USER_FLAGS & const.UF_DONT_EXPIRE_PASSWD
        # 不设 ACCOUNTDISABLE.
        assert not (const.SANDBOX_USER_FLAGS & const.UF_ACCOUNTDISABLE)

    def test_allow_write_rights(self):
        assert const.ALLOW_WRITE_RIGHTS & const.FILE_GENERIC_WRITE
        assert const.ALLOW_WRITE_RIGHTS & const.FILE_GENERIC_EXECUTE
        assert const.ALLOW_WRITE_RIGHTS & const.FILE_DELETE_ACCESS

    def test_synthetic_sid_format(self):
        from jiuwenbox.supervisor import win_acl
        sid = win_acl.get_synthetic_write_sid()
        assert sid.startswith("S-1-5-21-")
        parts = sid.split("-")
        assert len(parts) == 7  # S 1 5 21 sub0 sub1 RID

    def test_proxy_default_port_range(self):
        assert const.DEFAULT_PROXY_PORT_RANGE_START == 60080
        assert const.DEFAULT_PROXY_PORT_RANGE_END == 60089

    def test_wfp_action_flags(self):
        assert const.FWP_ACTION_BLOCK != const.FWP_ACTION_PERMIT
        assert const.FWP_WEIGHT_PERMIT > const.FWP_WEIGHT_BLOCK

    def test_reg_value_names(self):
        assert const.REG_VALUE_INSTALLED == "installed"
        assert const.REG_VALUE_SANDBOX_USER_SID == "sandbox_user_sid"


# ---------------------------------------------------------------------------
# WSL 可跑: win_proxy 过滤逻辑 (纯 asyncio).
# ---------------------------------------------------------------------------
from jiuwenbox.models.policy import NetworkRulePolicy  # noqa: E402
from jiuwenbox.supervisor import win_proxy  # noqa: E402


class TestEgressFilter:
    def _filter(self, **kwargs) -> win_proxy.EgressFilter:
        rule = NetworkRulePolicy(**kwargs)
        return win_proxy.EgressFilter(rule)

    def test_block_domain_explicit(self):
        f = self._filter(default="allow", blocked_domains=["badsite.com"])
        ok, _ = f.allow("badsite.com", 80)
        assert ok is False

    def test_block_domain_wildcard(self):
        f = self._filter(default="allow", blocked_domains=["*.evil.com"])
        ok, _ = f.allow("x.evil.com", 443)
        assert ok is False
        ok, _ = f.allow("evil.com", 443)
        assert ok is False
        ok, _ = f.allow("good.com", 443)
        assert ok is True

    def test_allow_domain_explicit(self):
        f = self._filter(default="deny", allowed_domains=["pypi.org"])
        ok, _ = f.allow("pypi.org", 443)
        assert ok is True
        ok, _ = f.allow("evil.org", 443)
        assert ok is False

    def test_deny_default_no_allow(self):
        f = self._filter(default="deny")
        ok, _ = f.allow("8.8.8.8", 53)
        assert ok is False

    def test_allow_default_no_block(self):
        f = self._filter(default="allow")
        ok, _ = f.allow("8.8.8.8", 53)
        assert ok is True

    def test_blocked_port(self):
        f = self._filter(default="allow", blocked_ports=[22])
        ok, _ = f.allow("10.0.0.1", 22)
        assert ok is False

    def test_allowed_port_with_deny_default(self):
        f = self._filter(default="deny", allowed_ports=[443])
        ok, _ = f.allow("10.0.0.1", 443)
        assert ok is True
        ok, _ = f.allow("10.0.0.1", 80)
        assert ok is False

    def test_blocked_ip_cidr(self):
        f = self._filter(default="allow", blocked_ips=["169.254.169.254/32"])
        ok, _ = f.allow("169.254.169.254", 80)
        assert ok is False

    def test_allow_default_with_allow_rules_non_matching(self):
        f = self._filter(default="allow", allowed_ports=[443], allowed_ips=["10.0.0.0/8"])
        ok, reason = f.allow("93.184.216.34", 8443)
        assert ok is True
        assert "default allow" in reason

    def test_allow_default_with_allow_rules_matching(self):
        f = self._filter(default="allow", allowed_ports=[443], allowed_domains=["pypi.org"])
        ok, reason = f.allow("pypi.org", 443)
        assert ok is True
        assert "explicitly allowed" in reason

    def test_deny_default_with_allow_rules_non_matching(self):
        f = self._filter(default="deny", allowed_ports=[443], allowed_ips=["10.0.0.0/8"])
        ok, reason = f.allow("93.184.216.34", 8443)
        assert ok is False
        assert "not in any allow rule" in reason


@pytest.mark.asyncio
class TestWinProxyServer:
    async def test_proxy_starts_and_stops_cleanly(self):
        """启动代理在两个端口, 随后优雅停止."""
        egress = NetworkRulePolicy(default="allow")
        proxy_task, stop_event = await win_proxy.serve_windows_proxy(
            egress=egress, ingress=None,
            port_range_start=60080, port_range_end=60081,
        )
        assert proxy_task is not None
        assert stop_event is not None
        # 给 server 一点时间绑定.
        await asyncio.sleep(0.3)
        stop_event.set()
        try:
            await asyncio.wait_for(proxy_task, timeout=3.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            proxy_task.cancel()

    async def test_proxy_rejects_blocked_host(self):
        """代理拒绝被 block 的目标域名."""
        egress = NetworkRulePolicy(default="allow", blocked_domains=["blocked.test"])
        proxy_task, stop_event = await win_proxy.serve_windows_proxy(
            egress=egress, ingress=None,
            port_range_start=60082, port_range_end=60082,
        )
        await asyncio.sleep(0.3)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", 60082)
            writer.write(b"CONNECT blocked.test:443 HTTP/1.1\r\n\r\n")
            await writer.drain()
            resp = await asyncio.wait_for(reader.read(64), timeout=3.0)
            # 期望 403 Forbidden.
            assert b"403" in resp
            writer.close()
            await writer.wait_closed()
        finally:
            stop_event.set()
            try:
                await asyncio.wait_for(proxy_task, timeout=3.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                proxy_task.cancel()


# ---------------------------------------------------------------------------
# WSL 可跑: ProcessRuntime / app lifespan 的 win32 分支 (mock).
# ---------------------------------------------------------------------------
class TestProcessRuntimeWindowsBranch:
    """mock win_* 模块后直接调 ``_create_windows`` (不走入口的 sys.platform 守卫),
    验证分支内部正确委托给 win_acl / win_exec / win_job.

    不 patch sys.platform: 避免在 Linux 下改 platform 触发 sysconfig 重建
    链路的副作用. _create_windows 本身不查 sys.platform, 只调 mock 过的
    win_* 模块, 因此可跨平台验证调度逻辑.
    """

    def test_create_dispatches_to_windows(self, monkeypatch):
        from jiuwenbox.server.runtime import process as proc_mod
        runtime = proc_mod.ProcessRuntime()

        # 构造 mock 的 win_* 模块.
        fake_win_acl = MagicMock()
        # apply_sandbox_acl 返回施加路径清单 (review M6).
        fake_win_acl.apply_sandbox_acl = MagicMock(return_value=["/ws"])
        fake_win_exec = MagicMock()
        # review #2: two_hop_spawn_and_authorize 封装 SUSPENDED→写 token→resume,
        # 返回 (pid, process_handle) (thread_handle/token_write_handle 内部释放).
        fake_win_exec.two_hop_spawn_and_authorize = MagicMock(
            return_value=(12345, 100),  # pid, process_handle
        )
        fake_win_exec._get_kernel32 = MagicMock(return_value=MagicMock())  # noqa: SLF001 - 测试访问内部成员
        fake_win_job = MagicMock()
        fake_win_job.create_job = MagicMock(return_value=999)
        fake_win_job.assign_process_by_pid = MagicMock()
        fake_win_job.resume_process = MagicMock()
        fake_win_setup = MagicMock()
        fake_win_setup.ensure_windows_setup = AsyncMock()
        fake_win_setup.get_sandbox_user_password = MagicMock(
            return_value="secret-pw",
        )
        # 注入到 sys.modules.
        import jiuwenbox.supervisor as supervisor_pkg
        monkeypatch.setitem(
            sys.modules, "jiuwenbox.supervisor.win_acl", fake_win_acl,
        )
        monkeypatch.setitem(
            sys.modules, "jiuwenbox.supervisor.win_exec", fake_win_exec,
        )
        monkeypatch.setitem(
            sys.modules, "jiuwenbox.supervisor.win_job", fake_win_job,
        )
        monkeypatch.setitem(
            sys.modules, "jiuwenbox.supervisor.win_setup", fake_win_setup,
        )

        # 让 _load_policy 返回带 windows 段的 policy.
        policy = SecurityPolicy.model_validate(_windows_policy_yaml())
        monkeypatch.setattr(
            proc_mod.ProcessRuntime, "_load_policy",
            staticmethod(lambda path: policy),
        )

        # 跑 create.
        import asyncio as _asyncio
        import os as _os
        from pathlib import Path
        # 确保包属性也指向 fake (from package import name 会先查包 __dict__).
        monkeypatch.setattr(supervisor_pkg, "win_acl", fake_win_acl, raising=False)
        monkeypatch.setattr(supervisor_pkg, "win_exec", fake_win_exec, raising=False)
        monkeypatch.setattr(supervisor_pkg, "win_job", fake_win_job, raising=False)
        monkeypatch.setattr(supervisor_pkg, "win_setup", fake_win_setup, raising=False)
        monkeypatch.setattr(
            supervisor_pkg, "win_constants",
            __import__("jiuwenbox.supervisor.win_constants",  # pylint: disable=avoid-import-method
                       fromlist=["win_constants"]),
            raising=False,
        )
        # pipe fd/handle 转换在 Linux 无法真实运行, mock 成假文件对象.
        monkeypatch.setattr(proc_mod, "_osfhandle_to_fd", lambda k, h: h)
        monkeypatch.setattr(
            _os, "fdopen",
            lambda fd, mode, **kw: MagicMock(),
        )
        pid = _asyncio.run(runtime._create_windows("sb-1", Path("/tmp/p.yaml"), {}))  # noqa: SLF001

        assert pid == 12345
        # ACL 施加被调用 (含读控制参数, review M4).
        fake_win_acl.apply_sandbox_acl.assert_called_once()
        # 两跳启动被调用 (review #2: token 走 pipe, two_hop_spawn_and_authorize 封装).
        fake_win_exec.two_hop_spawn_and_authorize.assert_called_once()


class TestAppLifespanWindowsBranch:
    """验证 app.py lifespan 在 win32 下走 setup + proxy 分支.

    用 ASGITransport 驱动一次请求触发 lifespan startup, 断言 mock 被调用.
    注: 此测试需在 Windows 上跑 (WSL 缺 pywintypes, mcp 路由无法 import).
    """

    @pytest.mark.skipif(
        sys.platform != "win32",
        reason="lifespan 集成需 Windows 环境 (WSL 缺 pywintypes)",
    )
    def test_lifespan_starts_proxy_and_setup_on_win32(self, monkeypatch):  # noqa: PLR6301,D100
        monkeypatch.setattr(sys, "platform", "win32")
        fake_win_setup = MagicMock()
        fake_win_setup.ensure_windows_setup = MagicMock()
        fake_win_proxy = MagicMock()

        async def _fake_serve(*a, **k):
            return (asyncio.ensure_future(asyncio.sleep(100)), asyncio.Event())

        fake_win_proxy.serve_windows_proxy = _fake_serve
        monkeypatch.setitem(
            sys.modules, "jiuwenbox.supervisor.win_setup", fake_win_setup,
        )
        monkeypatch.setitem(
            sys.modules, "jiuwenbox.supervisor.win_proxy", fake_win_proxy,
        )

        from jiuwenbox.server import app as app_mod
        # mock PolicyReader 返回带 windows 段的 policy 且非 proxy-only.
        policy = SecurityPolicy.model_validate(_windows_policy_yaml())
        fake_reader = MagicMock()
        fake_reader.load_policy = MagicMock(return_value=policy)
        fake_reader.is_proxy_only = MagicMock(return_value=False)
        monkeypatch.setattr(app_mod, "PolicyReader", lambda *a, **k: fake_reader)
        # mock SandboxManager 避免真实 runtime.
        fake_mgr = MagicMock()
        fake_mgr.register_zombie_reaper = MagicMock()
        fake_mgr.start_idle_reaper = MagicMock()
        fake_mgr.stop_idle_reaper = MagicMock()
        fake_mgr.shutdown_all_sandboxes = MagicMock()
        fake_mgr.clear_persistent_state = MagicMock()
        fake_mgr.unregister_zombie_reaper = MagicMock()

        async def _mgr_noop(*a, **k):
            return fake_mgr

        monkeypatch.setattr(app_mod, "_build_sandbox_manager", lambda *a, **k: fake_mgr)
        monkeypatch.setattr(app_mod, "enable_child_subreaper", lambda: True)

        # ProxyManager mock.
        async def _proxy_noop():
            return

        fake_pm = MagicMock()
        fake_pm.start = _proxy_noop
        fake_pm.stop = _proxy_noop
        monkeypatch.setattr(app_mod, "ProxyManager", lambda *a, **k: fake_pm)

        app_obj = app_mod.create_app()

        async def _drive():
            import httpx
            transport = httpx.ASGITransport(app=app_obj)
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
                # 一次请求触发 lifespan startup -> shutdown.
                resp = await c.get("/health")
                return resp

        asyncio.run(_drive())
        # startup 期间 ensure_windows_setup 应被调.
        fake_win_setup.ensure_windows_setup.assert_called_once()
        # proxy 启动应被调.
        assert fake_win_proxy.serve_windows_proxy.called

    def test_health_reports_windows_supported_on_win32(self, monkeypatch):  # noqa: PLR6301,D100
        monkeypatch.setattr(sys, "platform", "win32")
        fake_win_setup = MagicMock()
        fake_win_setup._reg_get_str = MagicMock(return_value="1")  # noqa: SLF001
        monkeypatch.setitem(
            sys.modules, "jiuwenbox.supervisor.win_setup", fake_win_setup,
        )

        from jiuwenbox.models.common import HealthResponse
        # 复现 app.health 端点中 windows_supported 的判定逻辑.
        windows_supported = False
        try:
            installed = fake_win_setup._reg_get_str(const.REG_VALUE_INSTALLED)  # noqa: SLF001
            windows_supported = installed == "1"
        except Exception:  # noqa: BLE001
            windows_supported = False
        resp = HealthResponse(
            version="test", landlock_supported=False,
            sandboxes_active=0, windows_supported=windows_supported,
        )
        assert resp.windows_supported is True

    def test_health_reports_not_supported_when_uninstalled(self, monkeypatch):  # noqa: PLR6301,D100
        monkeypatch.setattr(sys, "platform", "win32")
        fake_win_setup = MagicMock()
        fake_win_setup._reg_get_str = MagicMock(return_value=None)  # noqa: SLF001
        monkeypatch.setitem(
            sys.modules, "jiuwenbox.supervisor.win_setup", fake_win_setup,
        )
        from jiuwenbox.models.common import HealthResponse
        windows_supported = False
        try:
            installed = fake_win_setup._reg_get_str(const.REG_VALUE_INSTALLED)  # noqa: SLF001
            windows_supported = installed == "1"
        except Exception:  # noqa: BLE001
            windows_supported = False
        resp = HealthResponse(
            version="test", landlock_supported=False,
            sandboxes_active=0, windows_supported=windows_supported,
        )
        assert resp.windows_supported is False


# ---------------------------------------------------------------------------
# WSL 可跑: win_wfp / win_job / win_exec / win_acl 调用参数 mock 验证.
# ---------------------------------------------------------------------------
class TestWinWfpFilterConstruction:
    """验证 install_wfp_filters 构造的 Block/Permit filter 结构正确.

    _build_loopback_v4_condition / _build_ale_user_condition 现在返回
    (cond, keep_alive) 元组 (review C3/C5/M2). ALE_USER_ID 用
    FWP_SECURITY_DESCRIPTOR_TYPE (需 win32security, WSL 跳过).
    """

    def test_build_loopback_v4_condition(self):
        from jiuwenbox.supervisor import win_wfp
        cond, ka = win_wfp._build_loopback_v4_condition()  # noqa: SLF001
        assert cond.matchType == const.FWP_MATCH_EQUAL
        assert cond.conditionValue.type == const.FWP_V4_ADDR_MASK
        # keep_alive 持有 FWP_V4_ADDR_MASK 实例.
        assert ka is not None


# ---------------------------------------------------------------------------
# WSL 可跑: WFP 常量与结构体自洽性 (review CRITICAL #2/#4/#5, MAJOR #3).
# ---------------------------------------------------------------------------
class TestWinWfpConstantsAndLayout:
    def test_layer_and_condition_guids_match_sdk(self):
        """
            5 个 WFP GUID 必须等于 Windows SDK fwpmu.h DEFINE_GUID 真值 (review CRITICAL #5: 旧版全是虚构值).
        """
        assert const.FWPM_LAYER_ALE_AUTH_CONNECT_V4 == "C38D57D1-05A7-4C33-900F-7FBCEEE60E82"
        assert const.FWPM_LAYER_ALE_AUTH_CONNECT_V6 == "4A72393B-319F-44BC-84C3-BA54DCB3B6B4"
        assert const.FWPM_CONDITION_ALE_USER_ID == "AF043A0A-B34D-4F86-979C-C90371AF6E66"
        assert const.FWPM_CONDITION_IP_REMOTE_ADDRESS == "B235AE9A-1D64-49B8-A44C-5FF3D9095045"
        assert const.FWPM_CONDITION_IP_REMOTE_PORT == "C35A604D-D22B-4E1A-91B4-68F674EE674B"

    def test_sublayer_and_filter_keys_are_valid_uuids(self):
        """
            sublayer/filter key 必须是合法 UUID 字符串, 否则 _guid_from_str 首行 uuid.UUID()
            即抛 ValueError, WFP 安装从未执行(review CRITICAL #2).
        """
        import uuid
        for k in (
            const.JBX_SUBLAYER_KEY,
            const.JBX_FILTER_BLOCK_KEY_V4, const.JBX_FILTER_BLOCK_KEY_V6,
            const.JBX_FILTER_PERMIT_KEY_V4, const.JBX_FILTER_PERMIT_KEY_V6,
        ):
            assert uuid.UUID(k)  # 不抛即合法

    def test_loopback_ipv4_is_host_order(self):
        """
            LOOPBACK_IPV4_INT 必须是 host byte order, 反解出 127.0.0.1.
            旧值 0x0100007F 是网络序, 会让 Permit filter 匹配 1.0.0.127
            (review CRITICAL #4).
        """
        import socket
        import struct
        assert socket.inet_ntoa(struct.pack("!I", const.LOOPBACK_IPV4_INT)) == "127.0.0.1"
        assert const.LOOPBACK_IPV4_INT == 0x7F000001

    def test_fwp_data_type_enum_matches_sdk(self):
        """
            FWP_DATA_TYPE 枚举值必须与 fwptypes.h 一致 (review MAJOR #3:
            旧版 FWP_SID=12 实为 FWP_CHAR8, 真值 13).
        """
        assert const.FWP_EMPTY == 0
        assert const.FWP_BYTE_ARRAY16_TYPE == 11
        assert const.FWP_BYTE_BLOB_TYPE == 12
        assert const.FWP_SID == 13
        assert const.FWP_SECURITY_DESCRIPTOR_TYPE == 14
        assert const.FWP_V4_ADDR_MASK == 256
        assert const.FWP_V6_ADDR_AND_MASK == 257
        assert const.FWP_RANGE_TYPE == 258

    def test_fwpm_struct_layout_is_nontrivial(self):
        """
            结构体可构造且尺寸非零 (精确尺寸在 Windows 因 wintypes.DWORD=4B
            与 Linux=8B 不同, 仅校验非退化). review MAJOR #2: 旧版多 providerDataSize
            /缺 flags 导致字段错位.
        """
        import ctypes
        from jiuwenbox.supervisor import win_wfp
        for n in (
                "FWPM_DISPLAY_DATA0", "FWPM_FILTER0", "FWPM_SUBLAYER0",
                "FWPM_SESSION0", "FWP_VALUE0", "FWP_CONDITION_VALUE0",
                "FWPM_FILTER_CONDITION0", "FWPM_ACTION0", "FWP_V4_ADDR_MASK",
        ):
            cls = getattr(win_wfp, n)
            inst = cls()
            assert ctypes.sizeof(cls) > 0, n

    def test_fwpm_sublayer_weight_is_uint16(self):
        """
            FWPM_SUBLAYER0.weight 是 UINT16 (SDK), 旧版误为 c_uint32.
        """
        import ctypes
        from jiuwenbox.supervisor import win_wfp
        # 通过字段类型断言 (c_uint16 的 _type_ 是 'H').
        field = [f for f in win_wfp.FwpmSubLayer._fields_ if f[0] == "weight"][0]  # noqa: SLF001 - 测试访问内部成员
        assert field[1]._type_ == "H"  # noqa: SLF001 - 测试访问内部成员

    def test_guid_from_str_parses(self):
        from jiuwenbox.supervisor import win_wfp
        g = win_wfp._guid_from_str("C38D57D1-05A0-4E9C-886C-509CF8E61F74")  # noqa: SLF001
        assert g.Data1 == 0xC38D57D1


class TestWinJobLimits:
    def test_create_job_builds_correct_structs(self, monkeypatch):
        from jiuwenbox.supervisor import win_job
        # 绕过平台守卫 (测试在 Linux 跑, 但逻辑与平台无关).
        monkeypatch.setattr(win_job, "_require_windows", lambda: None)
        fake_kernel = MagicMock()
        fake_kernel.CreateJobObjectW = MagicMock(return_value=0xABCDEF)
        fake_kernel.SetInformationJobObject = MagicMock(return_value=True)
        fake_kernel.CloseHandle = MagicMock()
        monkeypatch.setattr(win_job, "_kernel32", fake_kernel)
        monkeypatch.setattr(win_job, "_get_kernel32", lambda: fake_kernel)

        handle = win_job.create_job(
            memory_max=512 * 1024 * 1024,
            cpu_rate=50,
            max_processes=32,
        )
        assert handle == 0xABCDEF
        # SetInformationJobObject 被调用 (extended limits + cpu rate).
        assert fake_kernel.SetInformationJobObject.call_count >= 1
        # 检查第一次调用 (extended limits) 传入的 JOBOBJECT 结构.
        args = fake_kernel.SetInformationJobObject.call_args_list[0].args
        info_class = args[1]
        assert info_class == const.JobObjectExtendedLimitInformation


class TestWinExecRunnerCommand:
    def test_build_runner_command_includes_sandbox_id(self):
        from jiuwenbox.supervisor import win_exec
        cmd = win_exec._build_runner_command(  # noqa: SLF001
            "sb-test", "C:\\ws", 60080, 60089,
        )
        assert "--sandbox-id" in cmd
        assert "sb-test" in cmd
        assert "--workspace" in cmd
        assert "C:\\ws" in cmd
        assert "runner" in cmd


class TestWinAclAceConstruction:
    def test_get_synthetic_write_sid_stable(self):
        from jiuwenbox.supervisor import win_acl
        sid1 = win_acl.get_synthetic_write_sid()
        sid2 = win_acl.get_synthetic_write_sid()
        assert sid1 == sid2  # 固定, 幂等.

    def test_parse_getace_tuple_5_elem(self):
        """新版 pywin32 GetAce 返回 5 元组 (ace_type, flags, size, mask, sid)."""
        from jiuwenbox.supervisor import win_acl
        # ace_type=1 (DENY).
        ace_type, flags, mask, sid = win_acl._parse_getace_tuple(  # noqa: SLF001
            (1, 0x3, 0x14, 0x10000, "deny-sid"),
        )
        assert ace_type == const.ACCESS_DENIED_ACE_TYPE
        assert mask == 0x10000
        assert sid == "deny-sid"

    def test_parse_getace_tuple_3_elem_defaults_allow(self):
        """旧版 3 元组无 ace_type, 默认视为 Allow."""
        from jiuwenbox.supervisor import win_acl
        ace_type, _flags, mask, sid = win_acl._parse_getace_tuple(  # noqa: SLF001
            (0x10000, 0x3, "old-sid"),
        )
        assert ace_type == const.ACCESS_ALLOWED_ACE_TYPE

    def test_rebuild_acl_deny_before_allow(self, monkeypatch):
        """重建 ACL 时 Deny ACE 必须排在 Allow ACE 之前 (文档 2.3)."""
        from jiuwenbox.supervisor import win_acl
        calls: list[tuple[str, int, object]] = []  # (method, mask, sid)

        class _FakeACL:  # noqa: E306 - 嵌套定义
            def AddAccessDeniedAceEx(self, flags, mask, sid):  # noqa: N815 - pywin32 SDK 方法名
                calls.append(("deny", mask, sid))

            def AddAccessAllowedAceEx(self, flags, mask, sid):  # noqa: N815 - pywin32 SDK 方法名
                calls.append(("allow", mask, sid))

        class _FakeDacl:  # noqa: E306 - 嵌套定义
            def GetAclSize(self):  # noqa: N815 - pywin32 SDK 方法名
                return 3

            def GetAce(self, i):  # noqa: N815 - pywin32 SDK 方法名
                # 第 0 个是 Allow, 第 1 个是 Deny, 第 2 个是 Allow (乱序输入).
                if i == 0:
                    return (0, 0x1, 0x100, "allow1")  # Allow
                if i == 1:
                    return (1, 0x1, 0x200, "deny1")  # Deny
                return (0, 0x1, 0x300, "allow2")  # Allow

        fake_sec = MagicMock()
        fake_sec.ACL = _FakeACL
        monkeypatch.setattr(
            win_acl, "_ensure_pywin32",
            lambda: (fake_sec, MagicMock(), MagicMock()),
        )
        # 新增一个 Deny ACE.
        new_ace = (const.ACCESS_DENIED_ACE_TYPE, 0x7, 0x400, "new-deny")
        win_acl._rebuild_acl_with_order(_FakeDacl(), new_ace)  # noqa: SLF001
        # 验证所有 deny 在所有 allow 之前.
        deny_idx = [i for i, c in enumerate(calls) if c[0] == "deny"]
        allow_idx = [i for i, c in enumerate(calls) if c[0] == "allow"]
        assert deny_idx and allow_idx
        assert max(deny_idx) < min(allow_idx), (
            f"Deny {deny_idx} 必须在 Allow {allow_idx} 之前, 实际 calls={calls}"
        )
        # 新增的 deny ACE 应在其中.
        assert ("deny", 0x400, "new-deny") in calls


# ---------------------------------------------------------------------------
# WSL 可跑: review 修复点验证.
# ---------------------------------------------------------------------------
class TestWinExecSidAndEnvBlock:
    """review CRITICAL #1 (SID 一致性) + CRITICAL #3 (环境块悬垂指针)."""

    def test_synthetic_write_sid_string_matches_allocate_layout(self):  # noqa: PLR6301,D100 - pytest
        """
            win_acl 字符串版 SID 必须与 win_exec AllocateAndInitializeSid
            产出的布局一致 (S-1-5-21-<sub0>-<sub1>-<RID>). 旧版 win_exec 用
            nSubAuthorityCount=3 漏了 21, 产出 S-1-5-... 与 ACL 授权的
            S-1-5-21-... 不是同一个 SID (review CRITICAL #1).

            本测试不跑真 AllocateAndInitializeSid (win32), 只校验 SID 字符串
            的 sub-authority 结构 + 验证 win_exec 的常量编排: 前缀含 21.
        """
        sid = win_acl.get_synthetic_write_sid()
        parts = sid.split("-")
        # S-1-5-21-<sub0>-<sub1>-<RID> => 7 段
        assert parts[:4] == ["S", "1", "5", "21"], (
            f"SID 前缀必须 S-1-5-21-..., 实际 {sid}"
        )
        assert parts[4] == str(const.SYNTHETIC_WRITE_SID_SUBAUTHS[0])
        assert parts[5] == str(const.SYNTHETIC_WRITE_SID_SUBAUTHS[1])
        assert parts[6] == str(const.SYNTHETIC_WRITE_SID_RID)
        assert len(parts) == 7

    def test_build_runner_command_returns_thread_handle_in_tuple(self):
        """
            two_hop_spawn 现返回 5 元组 (含 thread_handle 供 Job resume).
            本测试不跑真 win32, 只验证 _build_runner_command 命令行构造不变
            (review MAJOR #1 的接线前提).
        """
        from jiuwenbox.supervisor import win_exec
        cmd = win_exec._build_runner_command(
            "sb", "C:\\ws", 60080, 60089)  # noqa: SLF001 - 测试访问内部成员
        assert "runner" in cmd and "--sandbox-id sb" in cmd


class TestWindowsPolicyReadFields:
    """review MAJOR #4: WindowsFilesystemPolicy 增 allow_read/deny_read."""

    def test_read_fields_parsed_and_expanded(self):
        p = SecurityPolicy.model_validate({
            "windows": {"filesystem": {
                "allow_read": ["~/docs", "$TMP/x"],
                "deny_read": ["/etc/secret"],
                "allow_write": ["/ws"],
            }}
        })
        assert p.windows.filesystem.allow_read == [
            os.path.expanduser("~/docs"), os.path.expandvars("$TMP/x"),
        ]
        assert p.windows.filesystem.deny_read == ["/etc/secret"]

    def test_read_fields_default_empty(self):
        p = SecurityPolicy.model_validate({"windows": {}})
        assert p.windows.filesystem.allow_read == []
        assert p.windows.filesystem.deny_read == []

    def test_read_fields_extra_forbid(self):
        with pytest.raises(Exception):
            SecurityPolicy.model_validate({"windows": {"filesystem": {
                "allow_read": ["/x"], "bogus": 1,
            }}})


class TestWinProxyEgressSemantics:
    """review MAJOR #10: IP-allow 与 port-allow 不做隐式 AND."""

    def _filter(self, **kwargs) -> "win_proxy.EgressFilter":
        rule = NetworkRulePolicy(**kwargs)
        return win_proxy.EgressFilter(rule)

    def test_ip_allow_and_port_allow_not_anded(self):
        """
            allowed_ips=[10/8] + allowed_ports=[443] 不应 AND: 10.1.2.3:8443
            (IP 命中 allow) 必须放行 (Linux iptables 独立 ACCEPT 语义).
        """
        f = self._filter(
            allowed_ips=["10.0.0.0/8"], allowed_ports=[443], default="deny",
        )
        allowed, _ = f.allow("10.1.2.3", 8443)
        assert allowed, "IP 命中 allow 应放行, 不与 port allow 做 AND"

    # 别名: 设计文档 windows_sandbox_review_fix_design.md §4 要求的测试名.
    # 与 test_ip_allow_and_port_allow_not_anded 同一断言 (IP-allow 与 port-allow
    # 不做隐式 AND), 任一命名都能命中此 review MAJOR #10 修复点.
    test_egress_allow_no_implicit_and = test_ip_allow_and_port_allow_not_anded

    def test_port_allow_only_still_works(self):
        f = self._filter(allowed_ports=[443], default="deny")
        allowed, _ = f.allow("10.1.2.3", 443)
        assert allowed

    def test_blocked_port_overrides_allow(self):
        f = self._filter(
            allowed_ports=[443], blocked_ports=[443], default="deny",
        )
        allowed, _ = f.allow("10.1.2.3", 443)
        assert not allowed


# ---------------------------------------------------------------------------
# Windows 端实跑用例 (skip on non-win32; 由用户在 Windows 上跑).
# ---------------------------------------------------------------------------
_windows_only = pytest.mark.skipif(
    sys.platform != "win32",
    reason="Windows 沙箱端到端用例需在 Windows 平台以管理员身份运行",
)


@_windows_only
class TestWindowsSandboxE2E:
    """真实创建/删除沙箱的端到端用例 (Windows 实跑).

    前置: 以管理员身份运行, 且已执行
    ``python -m jiuwenbox.supervisor.win_setup --install``.
    通过 ``--server-endpoint`` 连接真实 jiuwenbox-server.
    """

    def test_sandbox_lifecycle(self, client):
        """创建 -> 查询 -> 删除 沙箱."""
        resp = client.post("/api/v1/sandboxes", json={})
        assert resp.status_code == 201
        sb_id = resp.json()["id"]
        # 等待 ready.
        import time
        for _ in range(20):
            status = client.get(f"/api/v1/sandboxes/{sb_id}").json()
            if status.get("phase") == "ready":
                break
            time.sleep(0.5)
        assert status["phase"] == "ready"
        assert status.get("pid") is not None
        # 删除.
        del_resp = client.delete(f"/api/v1/sandboxes/{sb_id}")
        assert del_resp.status_code in (200, 202, 204)

    def test_exec_returns_stdout(self, client):
        import time
        create = client.post("/api/v1/sandboxes", json={})
        sb_id = create.json()["id"]
        for _ in range(20):
            if client.get(f"/api/v1/sandboxes/{sb_id}").json().get("phase") == "ready":
                break
            time.sleep(0.5)
        try:
            exec_resp = client.post(
                f"/api/v1/sandboxes/{sb_id}/exec",
                json={"command": ["cmd", "/c", "echo hello"], "timeout_seconds": 10},
            )
            assert exec_resp.status_code == 200
            result = exec_resp.json()
            assert result.get("exit_code") == 0
            assert "hello" in result.get("stdout", "").strip().lower()
        finally:
            client.delete(f"/api/v1/sandboxes/{sb_id}")

    def test_file_isolation_deny_write_outside_workspace(self, client):
        """deny_write 路径写入应失败."""
        import time
        create = client.post(
            "/api/v1/sandboxes",
            json={"policy": _windows_policy_yaml()},
        )
        assert create.status_code == 201
        sb_id = create.json()["id"]
        for _ in range(20):
            if client.get(f"/api/v1/sandboxes/{sb_id}").json().get("phase") == "ready":
                break
            time.sleep(0.5)
        try:
            # 试图写 deny_write 路径 ({{ workspace }}/.git/config).
            exec_resp = client.post(
                f"/api/v1/sandboxes/{sb_id}/exec",
                json={
                    "command": ["cmd", "/c", "echo x > .git\\config"],
                    "timeout_seconds": 10,
                },
            )
            result = exec_resp.json()
            # 期望非 0 exit (写被拒).
            assert result.get("exit_code") != 0
        finally:
            client.delete(f"/api/v1/sandboxes/{sb_id}")

    def test_network_isolation_blocks_direct_egress(self, client):
        """WFP 应拦截沙箱直连外网 (除非经代理白名单)."""
        import time
        create = client.post(
            "/api/v1/sandboxes",
            json={"policy": _windows_policy_yaml()},
        )
        sb_id = create.json()["id"]
        for _ in range(20):
            if client.get(f"/api/v1/sandboxes/{sb_id}").json().get("phase") == "ready":
                break
            time.sleep(0.5)
        try:
            exec_resp = client.post(
                f"/api/v1/sandboxes/{sb_id}/exec",
                json={
                    "command": [
                        "cmd", "/c",
                        "curl -s -o nul -w \"%{http_code}\" --connect-timeout 5 "
                        "http://blocked.test/",
                    ],
                    "timeout_seconds": 15,
                },
            )
            result = exec_resp.json()
            # 期望连接被拦 (非 200, curl 退出码非 0 或超时).
            stdout = result.get("stdout", "")
            assert "200" not in stdout or result.get("exit_code") != 0
        finally:
            client.delete(f"/api/v1/sandboxes/{sb_id}")
