# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


# 说明：本文件中依赖 bundled `playwright_runtime` 包（browser-move/src）的
# 子进程用例已删除——upstream 在 8465de8d2 移除了该内置运行时，`_CONFIG_SCRIPT`
# 的导入必然 ModuleNotFoundError。Electron 精确 target 的参数拼装现在由
# electron/main.cjs 直接下发 JSON argv，Python 侧契约由 interface_deep 的
# electron 分支与下方 openjiuwen runtime 用例覆盖。

_ACTIVE_CONFIG_SCRIPT = """
import json
from openjiuwen.harness.tools.browser_move.playwright_runtime.config import (
    BrowserInstanceConfig,
    build_playwright_mcp_config,
)
config = build_playwright_mcp_config(BrowserInstanceConfig(key="member-one"))
print(json.dumps({
    "args": config.params["args"],
    "env": config.params.get("env", {}),
    "server_id": config.server_id,
}))
"""

_ELECTRON_MAIN = (
    Path(__file__).resolve().parents[2]
    / "jiuwenswarm"
    / "channels"
    / "desktop"
    / "electron"
    / "main.cjs"
)


def _build_active_config_in_subprocess(overrides: dict[str, str]) -> dict:
    env = dict(os.environ)
    env.update(overrides)
    for key in (
        "PLAYWRIGHT_MCP_ARGS",
        "PLAYWRIGHT_MCP_TARGET_ID",
        "PLAYWRIGHT_MCP_CDP_ENDPOINT",
        "PLAYWRIGHT_MCP_ENV_JSON",
    ):
        if key not in overrides:
            env.pop(key, None)
    result = subprocess.run(
        [sys.executable, "-c", _ACTIVE_CONFIG_SCRIPT],
        cwd=Path(__file__).resolve().parents[2],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_active_browser_agent_uses_target_wrapper() -> None:
    wrapper = (
        Path(__file__).resolve().parents[2]
        / "jiuwenswarm"
        / "channels"
        / "desktop"
        / "electron"
        / "target_mcp_wrapper.cjs"
    )
    config = _build_active_config_in_subprocess(
        {
            "BROWSER_DRIVER": "remote",
            "PLAYWRIGHT_MCP_CDP_ENDPOINT": "http://127.0.0.1:43123",
            "PLAYWRIGHT_MCP_TARGET_ID": "sideview-target",
            "PLAYWRIGHT_MCP_ARGS": json.dumps(
                [
                    "-y",
                    "--package",
                    "@playwright/mcp@0.0.78",
                    "node",
                    str(wrapper),
                ]
            ),
            "PLAYWRIGHT_MCP_ENV_JSON": json.dumps(
                {
                    "PLAYWRIGHT_MCP_CDP_ENDPOINT": "http://127.0.0.1:43123",
                    "PLAYWRIGHT_MCP_TARGET_ID": "sideview-target",
                    "PLAYWRIGHT_MCP_DIAGNOSTIC_LOG": "C:/Users/test/.jiuwenswarm/agent/.logs/target_mcp_wrapper.log",
                }
            ),
        }
    )
    assert config["args"][:4] == [
        "-y",
        "--package",
        "@playwright/mcp@0.0.78",
        "node",
    ]
    assert Path(config["args"][4]).name == "target_mcp_wrapper.cjs"
    assert config["env"]["PLAYWRIGHT_MCP_TARGET_ID"] == "sideview-target"
    assert config["env"]["PLAYWRIGHT_MCP_DIAGNOSTIC_LOG"].endswith(
        "/.jiuwenswarm/agent/.logs/target_mcp_wrapper.log"
    )
    assert config["server_id"].endswith("__member-one")


def test_electron_forwards_target_wrapper_diagnostic_log() -> None:
    source = _ELECTRON_MAIN.read_text(encoding="utf-8")

    assert "'target_mcp_wrapper.log'" in source
    assert "env.PLAYWRIGHT_MCP_DIAGNOSTIC_LOG = targetMcpDiagnosticLog" in source
    assert "PLAYWRIGHT_MCP_DIAGNOSTIC_LOG: targetMcpDiagnosticLog" in source


def test_electron_spawns_services_with_session_target_resolver() -> None:
    """每会话隔离契约：spawn env 携带 resolver 而非静态全局 TargetID。"""
    source = _ELECTRON_MAIN.read_text(encoding="utf-8")

    assert "env.PLAYWRIGHT_MCP_TARGET_RESOLVER =" in source
    assert "PLAYWRIGHT_MCP_TARGET_RESOLVER: env.PLAYWRIGHT_MCP_TARGET_RESOLVER" in source
    # 静态 TargetID 通道已废弃：每会话视图由 Python 侧按会话经 resolver 注入。
    assert "env.PLAYWRIGHT_MCP_TARGET_ID =" not in source
    assert "PLAYWRIGHT_MCP_TARGET_ID: browserTargetId" not in source


def test_electron_ensures_session_view_target_before_starting_services() -> None:
    source = _ELECTRON_MAIN.read_text(encoding="utf-8")

    # ensureBrowserView 内不变量：首屏提交本地空白页使 CDP target 立即注册；
    # 最后浏览页面/默认页仅后台加载，不得阻塞创建（见 ensureBrowserView 注释）。
    blank_commit = source.index("await view.webContents.loadURL(`about:blank#jiuwen-session=")
    target_capture = source.index(
        "entry.targetId = view.webContents.getOrCreateDevToolsTargetId()",
        blank_commit,
    )
    target_ready = source.index("await waitForCdpTarget(entry.targetId)", target_capture)
    background_nav = source.index(
        "void view.webContents.loadURL(restoreUrl)",
        target_ready,
    )
    resolver_start = source.index("browserTargetResolver = await startBrowserTargetResolver()")
    backend_start = source.index("await startBackendServices(")
    web_start = source.index("await startWebService(")

    assert blank_commit < target_capture < target_ready < background_nav
    # resolver（提供 spawn env 端口）必须在 agent/gateway spawn 前就绪。
    assert resolver_start < backend_start
    # web 服务不消费 CDP target env，先于 resolver 建立 spawn，使冻结后端的
    # 多秒级 Python 启动与渲染进程/CDP 初始化重叠（首屏加速不变量）。
    assert web_start < resolver_start


def test_electron_persists_session_urls_across_restart() -> None:
    """重启还原契约：导航记录防抖落盘，启动读回，退出冲刷。"""
    source = _ELECTRON_MAIN.read_text(encoding="utf-8")

    # recordLastUrl 触发防抖落盘调度。
    record = source.index("const recordLastUrl = () =>")
    assert source.index("scheduleSessionUrlsSave()", record) > record

    # 启动时先读回上次保存的会话页面 URL，再创建主窗口（内部启动 resolver）。
    when_ready = source.index("app.whenReady()")
    load_call = source.index("loadSessionLastUrls();", when_ready)
    await_main = source.index("await createMainWindow()", when_ready)
    assert when_ready < load_call < await_main

    # 退出路径（before-quit）先冲刷 URL，再在 blob 落盘与服务的并行清理完成后退出。
    quit_handler = source.index("app.on('before-quit'")
    flush = source.index("saveSessionLastUrls();", quit_handler)
    teardown = source.index(
        "void Promise.all([abortAllBlobSaves(), stopServices()])", quit_handler
    )
    assert quit_handler < flush < teardown
