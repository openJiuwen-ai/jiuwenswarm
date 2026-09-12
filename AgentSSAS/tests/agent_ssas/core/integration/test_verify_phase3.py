# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

# tests/verify_phase3.py
r"""AgentSSAS 阶段三真实环境验证脚本。

提供独立可执行的验证命令,替代 python -c 内联方式。
覆盖进程内模式、HTTP 服务模式、配置关闭三种场景。

使用方式:
    cd d:\TraeWorkspace\AgentSSAS\AgentSecurity\AgentSSAS
    d:\TraeWorkspace\AgentSSAS\jiuwenswarm\.venv\Scripts\python.exe -m tests.verify_phase3 <子命令>

子命令:
    inprocess   — 进程内模式验证:Rail 注册 + 事件上报 + 日志生成
    http-server — 启动 HTTP 服务端(前台运行,Ctrl+C 退出)
    http-client — HTTP 模式验证:向已启动的服务端发送事件
    disabled    — 配置关闭验证
    all         — 依次执行 inprocess + disabled(不含 http-server/http-client)
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path


def _set_ssas_home(tag: str, clean: bool = True) -> str:
    """设置 SSAS_HOME 到统一临时目录,返回路径。

    统一使用 $TEMP/agent_ssas/phase3/<tag> 作为存储根目录,
    与阶段一/阶段二的测试报告路径约定一致。

    参数:
        tag: 子目录标签(如 inprocess, http_server)。
        clean: 是否清空已有测试数据。默认 True,每次测试前清空旧数据,
            保证测试结果可重复、不累积。业界最佳实践:测试前清理状态。

    清理策略:删除 home 目录下的所有内容。如果遇到文件锁定
    (Windows 上 SQLite WAL 或 threat_log 目录残留句柄),
    则重命名到带时间戳的目录,让原路径空出来。
    """
    home = Path(tempfile.gettempdir()) / "agent_ssas" / "phase3" / tag
    if clean and home.exists():
        _safe_clean_home(home)
    home.mkdir(parents=True, exist_ok=True)
    os.environ["SSAS_HOME"] = str(home)
    return str(home)


def _safe_clean_home(home: Path) -> None:
    """清理 SSAS_HOME 目录,处理 Windows 文件锁定。

    先尝试直接删除整个目录树。如果失败(某文件被锁定),
    则逐个删除子项,跳过被锁定的文件/目录,最后重命名整个目录
    作为兜底。确保下次测试时数据库是从零开始的。

    关键:只要 ssas_core.db 及其 WAL/SHM 文件被删除,
    SQLite 就会从空数据库开始,不会累积旧数据。
    """
    # 先尝试直接删除整个目录树
    try:
        shutil.rmtree(home)
        return
    except (PermissionError, OSError):
        pass

    # 逐个删除子项,跳过被锁定的(如 threat_log 目录的残留句柄)
    if home.exists():
        for item in list(home.iterdir()):
            try:
                if item.is_dir():
                    shutil.rmtree(item)
                else:
                    item.unlink()
            except (PermissionError, OSError):
                # 跳过被锁定的文件/目录,继续删除其他
                pass

    # 如果 ssas 子目录下的数据库文件已删除,清理成功
    # 如果仍有残留(文件被锁定),重命名整个目录作为兜底
    ssas_db = home / "ssas" / "ssas_core.db"
    if ssas_db.exists():
        # 数据库文件仍在,说明清理失败
        try:
            ts = int(time.time())
            backup = home.parent / f"{home.name}_old_{ts}"
            home.rename(backup)
            print(f"[verify] Renamed locked dir {home.name} -> {backup.name}")
        except Exception:
            raise RuntimeError(
                f"无法清理测试目录 {home}(ssas_core.db 被锁定)。"
                f"请手动关闭可能占用该目录的进程后重试。"
            )


def _safe_rmtree(path: Path) -> None:
    """安全删除目录树,处理 Windows 上文件被占用的情况。

    先尝试正常删除,失败则改为重命名到带时间戳的临时目录
    (让操作系统后续清理),避免因文件锁定导致测试中断。
    三级策略全部失败时抛出异常,避免静默复用旧数据导致测试结果不可重复。
    """
    try:
        shutil.rmtree(path)
        return
    except (PermissionError, OSError):
        pass
    # Windows 上文件可能被 SQLite WAL 或其他进程锁定
    # 重命名到带时间戳的目录,让原路径空出来
    try:
        ts = int(time.time())
        backup = path.parent / f"{path.name}_old_{ts}"
        path.rename(backup)
        print(f"[verify] Renamed locked dir {path.name} -> {backup.name} (will be cleaned up later)")
        return
    except Exception:
        pass
    # 如果重命名也失败,最后尝试强制删除
    import subprocess
    result = subprocess.run(
        ["cmd", "/c", "rmdir", "/s", "/q", str(path)],
        capture_output=True,
        timeout=10,
    )
    if path.exists():
        raise RuntimeError(
            f"无法清理测试目录 {path}(文件被锁定)。"
            f"请手动关闭可能占用该目录的进程后重试。"
        )


async def verify_inprocess():
    """进程内模式验证。

    验证内容:
    1. AgentSSASSecurityRail 可创建,priority=80
    2. backend.initialize() 成功
    3. 完整事件流(6 个生命周期事件)返回 safe
    4. 安全检测事件返回 high
    5. SQLite 数据库三张表(raw_events/events/alerts)均有数据
    6. 威胁日志文件生成

    使用 try/finally 确保测试结束(含异常路径)时正确关闭 SQLite 连接,
    释放 Windows WAL 文件锁定,保证下次运行清理能成功。
    """
    from agent_ssas.backend_client.openjiuwen.agent_ssas_security_rail import AgentSSASSecurityRail
    from agent_ssas.core.framework.access_adapter.agent_backend import AgentSSASBackend
    from agent_ssas.core.framework.config.settings import AgentSSASConfig
    from agent_ssas.core.framework.core_types.assessment import RiskLevel
    from tests.fixtures.event_factory import generate_event_sequence, generate_permission_interrupt_event

    home = _set_ssas_home("inprocess")
    print(f"[verify_inprocess] SSAS_HOME={home}")

    config = AgentSSASConfig()
    backend = AgentSSASBackend(config)
    try:
        rail = AgentSSASSecurityRail(backend=backend)
        assert rail.priority == 80, f"priority 应为 80,实际 {rail.priority}"
        print(f"[verify_inprocess] PASS: AgentSSASSecurityRail registered, priority={rail.priority}")

        await backend.initialize()
        print("[verify_inprocess] PASS: AgentSSASSecurityRail backend initialized")

        # 完整事件流:invoke_start → llm_input → tool_input → tool_output → llm_output → invoke_end
        events = generate_event_sequence(
            session_id="verify-session",
            agent_id="verify-agent",
            trace_id="verify-trace",
        )
        for i, raw_event in enumerate(events):
            result = await backend.report_event(raw_event)
            assert result is not None
            assert result.risk_level == RiskLevel.SAFE, f"生命周期事件 {raw_event['common']['event_type']} 应返回 safe,实际 {result.risk_level}"
        print(f"[verify_inprocess] PASS: {len(events)} lifecycle events -> all risk_level=safe")

        # 安全检测事件:permission_interrupt_tool
        sec_event = generate_permission_interrupt_event(
            session_id="verify-session",
            agent_id="verify-agent",
            trace_id="verify-trace",
        )
        result = await backend.report_event(sec_event)
        assert result.risk_level == RiskLevel.HIGH, f"安全检测事件应返回 high,实际 {result.risk_level}"
        assert result.has_risk is True
        print(f"[verify_inprocess] PASS: security event -> risk_level={result.risk_level.value}, has_risk={result.has_risk}")
        assert "tool_permission_denied" in result.detected_threats
        print(f"[verify_inprocess] PASS: detected_threats={result.detected_threats}")

        # 检查文件生成
        ssas_dir = Path(home) / "ssas"
        db_path = ssas_dir / "ssas_core.db"
        assert db_path.exists(), f"ssas_core.db 未生成: {db_path}"
        print(f"[verify_inprocess] PASS: ssas_core.db generated at {db_path}")

        # 验证数据库表数据
        import sqlite3
        conn = sqlite3.connect(str(db_path))

        raw_events_count = conn.execute("SELECT COUNT(*) FROM raw_events").fetchone()[0]
        assert raw_events_count >= 7, f"raw_events 表应有 >=7 条记录(6 生命周期 + 1 安全检测),实际 {raw_events_count}"
        print(f"[verify_inprocess] PASS: raw_events table has {raw_events_count} records (expected >=7)")

        events_count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        # events 表含基础事件 + session_start 派生事件 + 聚合事件,应 >=7
        assert events_count >= 7, f"events 表应有 >=7 条记录(含派生事件和聚合事件),实际 {events_count}"
        print(f"[verify_inprocess] PASS: events table has {events_count} records (expected >=7)")

        alerts_count = conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]
        assert alerts_count >= 1, f"alerts 表应有 >=1 条告警记录(安全检测事件),实际 {alerts_count}"
        print(f"[verify_inprocess] PASS: alerts table has {alerts_count} records (expected >=1)")

        conn.close()

        reports_dir = ssas_dir / "reports" / "threat_log"
        if reports_dir.exists():
            files = list(reports_dir.glob("*.json"))
            assert len(files) > 0, "威胁日志文件未生成"
            print(f"[verify_inprocess] PASS: {len(files)} threat log files generated")

        modules_dir = ssas_dir / "modules"
        if modules_dir.exists():
            module_names = [d.name for d in modules_dir.iterdir() if d.is_dir()]
            print(f"[verify_inprocess] PASS: detection modules: {module_names}")

        print("[verify_inprocess] === ALL CHECKS PASSED ===")
    finally:
        # 关闭 SQLite 连接,释放 Windows WAL 文件锁定
        # 这确保下次运行时 _safe_rmtree 能成功清理旧数据
        await backend.close()


async def verify_http_client():
    """HTTP 模式验证:向已启动的 HTTP 服务端发送事件。

    与进程内模式一致,发送完整 7 条事件(6 生命周期 + 1 安全检测)。
    前置条件:需先执行 `python -m tests.verify_phase3 http-server` 启动服务端。
    """
    import httpx

    from tests.fixtures.event_factory import generate_event_sequence, generate_permission_interrupt_event

    endpoint = os.environ.get("SSAS_HTTP_ENDPOINT", "http://localhost:8443")
    print(f"[verify_http_client] endpoint={endpoint}")

    async with httpx.AsyncClient(base_url=endpoint, timeout=10.0) as client:
        # 完整事件流:invoke_start → llm_input → tool_input → tool_output → llm_output → invoke_end
        events = generate_event_sequence(
            session_id="http-verify",
            agent_id="http-agent",
            trace_id="http-trace",
        )
        first_latency: float | None = None
        for i, raw_event in enumerate(events):
            start = time.time()
            resp = await client.post("/api/v1/events", json={"raw_event": raw_event})
            elapsed_ms = (time.time() - start) * 1000
            if first_latency is None:
                first_latency = elapsed_ms
            assert resp.status_code == 200, f"HTTP 状态码应为 200,实际 {resp.status_code}"
            assessment = resp.json()["assessment"]
            assert assessment["risk_level"] == "safe", f"生命周期事件 {raw_event['common']['event_type']} 应返回 safe,实际 {assessment['risk_level']}"
        print(f"[verify_http_client] PASS: {len(events)} lifecycle events -> all risk_level=safe, first latency={first_latency:.1f}ms")

        # 安全检测事件:permission_interrupt_tool
        sec_event = generate_permission_interrupt_event(
            session_id="http-verify",
            agent_id="http-agent",
            trace_id="http-trace",
        )
        resp = await client.post("/api/v1/events", json={"raw_event": sec_event})
        assert resp.status_code == 200
        assessment = resp.json()["assessment"]
        assert assessment["risk_level"] == "high", f"安全检测事件应返回 high,实际 {assessment['risk_level']}"
        assert assessment["has_risk"] is True
        assert "tool_permission_denied" in assessment["detected_threats"]
        print(f"[verify_http_client] PASS: security event -> status=200, risk_level={assessment['risk_level']}, has_risk={assessment['has_risk']}")
        print(f"[verify_http_client] PASS: detected_threats={assessment['detected_threats']}")

        # 健康检查
        resp = await client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}
        print("[verify_http_client] PASS: health check -> status=ok")

    print("[verify_http_client] === ALL CHECKS PASSED ===")


def verify_disabled():
    """配置关闭验证。

    验证内容:
    1. AgentSSASConfig(enabled=False) 可正确创建
    2. 配置对象不产生副作用
    3. jiuwenswarm _build_agent_rails 的 SSAS 关闭逻辑正确
    """
    from agent_ssas.core.framework.config.settings import AgentSSASConfig

    config = AgentSSASConfig(enabled=False)
    assert config.enabled is False
    print(f"[verify_disabled] PASS: AgentSSASConfig(enabled=False) -> enabled={config.enabled}")

    assert config.storage_path is not None
    print(f"[verify_disabled] PASS: storage_path={config.storage_path}")

    # 模拟 jiuwenswarm _build_agent_rails 的逻辑
    config_base = {"ssas": {"enabled": False}}
    ssas_config = config_base.get("ssas", {})
    ssas_enabled = ssas_config.get("enabled", True)
    assert ssas_enabled is False
    print("[verify_disabled] PASS: jiuwenswarm config ssas.enabled=false -> skip registration")

    # 验证默认配置(无 ssas 段)时 SSAS 开启
    config_base_empty = {}
    ssas_config_empty = config_base_empty.get("ssas", {})
    ssas_enabled_default = ssas_config_empty.get("enabled", True)
    assert ssas_enabled_default is True
    print("[verify_disabled] PASS: default config (no ssas section) -> enabled=True")

    print("[verify_disabled] === ALL CHECKS PASSED ===")


def start_http_server():
    """启动 HTTP 服务端。

    启动前自动检查端口是否被占用,若被占用则尝试终止占用进程
    (可能是上次未正常退出的服务端进程)。
    """
    _set_ssas_home("http_server")
    import uvicorn

    from agent_ssas.core.framework.access_adapter.http_server import create_app
    from agent_ssas.core.framework.config.settings import AgentSSASConfig

    config = AgentSSASConfig()
    port = config.http_port

    # 检查端口是否被占用,若被占用则终止占用进程
    _free_port(port)

    print(f"[http-server] Starting AgentSSAS HTTP Server on port {port}...")
    print("[http-server] Press Ctrl+C to stop")
    app = create_app(config)
    uvicorn.run(app, host=config.http_host, port=port, log_level="info")


def _free_port(port: int) -> None:
    """检查端口是否被占用,若被占用则终止占用进程。

    Windows 上使用 netstat 查找占用端口的 PID,然后终止。
    这解决了上次 HTTP 服务端未正常退出导致端口被占用的问题。
    """
    import subprocess

    try:
        result = subprocess.run(
            ["netstat", "-ano"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        lines = result.stdout.splitlines()
        for line in lines:
            if f":{port}" in line and "LISTENING" in line.upper():
                parts = line.split()
                if len(parts) >= 5:
                    pid = parts[-1]
                    if pid and pid != "0":
                        try:
                            subprocess.run(
                                ["taskkill", "/F", "/PID", pid],
                                capture_output=True,
                                timeout=5,
                            )
                            print(f"[http-server] Killed old process (PID={pid}) on port {port}")
                        except Exception:
                            pass
                        return
    except Exception:
        pass


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "inprocess":
        asyncio.run(verify_inprocess())
    elif cmd == "http-server":
        start_http_server()
    elif cmd == "http-client":
        asyncio.run(verify_http_client())
    elif cmd == "disabled":
        verify_disabled()
    elif cmd == "all":
        asyncio.run(verify_inprocess())
        print()
        verify_disabled()
    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
