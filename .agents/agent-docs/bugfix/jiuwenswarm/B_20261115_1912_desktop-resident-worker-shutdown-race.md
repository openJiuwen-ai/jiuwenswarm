---
id: B_20261115_1912_desktop-resident-worker-shutdown-race
title: 桌面端关闭竞速导致常驻 Worker 优雅收尾必现失败
type: B
author: CharlesWang2030
author_email: 93426525@qq.com
designs: []
design_note: 依据真实运行日志（7/7 必现）与源码调用链定位，无独立设计文档；修复为进程生命周期收尾汇合点补齐与子进程降级兜底。
modules:
  - jiuwenswarm/channels/desktop
  - jiuwenswarm/server/supervisor
files:
  - jiuwenswarm/channels/desktop/desktop_app.py
---

# 桌面端关闭竞速导致常驻 Worker 优雅收尾必现失败

## 现象

每次正常点击窗口关闭退出 WorkSwarm（Windows，Python 3.13），`desktop.log` 必现一条 ERROR：

```
ERROR [desktop] resident Worker final shutdown incomplete
RuntimeError: cannot schedule new futures after interpreter shutdown
```

本机 2026-11-13 ~ 11-15 共 7 次正常关闭，7 次全部命中。常驻 Worker 的
`retire_worker`（排空、持久化、资源释放）从未执行，下游表现为：

- 下次启动 `replacing resident Worker from a different code build`、
  `worker bring-up failed: runtime worker ipc not ready`；
- 历史上出现 `RuntimeError: Kernel still has active resource leases` 阻断 Worker 启动；
- 关闭回执被写成 `ok=False`，更新/重启流程认为上次退出不干净。

## 根因

pywebview 的 `Event.set()` 将 `closed` 事件处理器放进一个新起的非 daemon
线程（下称 T 线程）执行；WinForms 后端在 GUI 主线程触发 `closed.set()`
后立即继续 `Application.Exit()`，主线程随后退出 `webview.start()` 并返回
`main()`。解释器终结时 `threading._shutdown` 先执行
`concurrent.futures.thread._python_exit`（置全局线程池关闭标志）再 join T
线程。主线程该路径几乎零耗时，T 线程尚在创建事件循环，竞赛必败——
T 中任何 `executor.submit` 都抛 `cannot schedule new futures after
interpreter shutdown`。

失败点为 `shutdown_resident_worker()` 第一条有效语句
`await asyncio.to_thread(_pending_workers, paths)`
（`jiuwenswarm/server/supervisor/shutdown.py:136`）。该关闭链路对线程池的
依赖不止此一处：

| 步骤 | 位置 | executor 依赖 |
|---|---|---|
| 枚举 pending workers | `server/supervisor/shutdown.py:136` | 默认 executor（`to_thread`） |
| `try_connect` / `connect` | `server/ipc/client.py:39` | 专用 `_PIPE_CONNECT_POOL` |
| IPC `send` / `recv` | `server/ipc/stream.py:322` | 连接级 writer/reader 线程池 |

全局关闭标志对所有 `ThreadPoolExecutor` 生效，因此任何单点同步化（如仅把
`:136` 改为直接调用）都只会把失败点顺移到下一步 executor 提交，无法修复。

## 修复决策

1. **主修复：补齐主线程与关闭线程的汇合点**（方案 1）。
   `DesktopRuntime.shutdown()` 拆出 `_shutdown_once()` 并以
   `try/finally` 保证 `_shutdown_complete` Event 必被置位；`main()` 的
   `finally` 在 `runtime.shutdown()` 之后调用新增的
   `wait_for_shutdown(timeout=40)` 再返回。等待上限 40s 大于内部 30s
   deadline 加子进程兜底约 5s 的最坏耗时。这样 T 线程的整个优雅关闭都在
   解释器健康期内完成，不改变窗口关闭 UX，同时覆盖 macOS（cocoa 同样以
   异步线程触发 `closed`）。
2. **兜底：`except` 分支改走隔离生命周期子进程**（方案 3）。
   进程内 `asyncio.run(shutdown_resident_worker(...))` 失败时，调用
   `shutdown_application_worker(workspace=get_user_workspace_dir(), ...)`
   以 `start_new_session` 子进程收尾，对解释器状态免疫；成功则记 WARNING
   恢复日志，失败才置 `shutdown_ok=False`。原「进程内同步降级」思路不可行
   （IPC 收发本身依赖线程池）。

不采用把 shutdown 提前到 `closing` 事件的方案：`closing` 处理器在 GUI
线程同步执行，会阻塞消息循环导致窗口假死最长 30s，且 `_on_closing` 在
非 Windows 平台直接返回，需要额外的平台分叉。

## 验证

- `uv run python -m py_compile jiuwenswarm/channels/desktop/desktop_app.py` 通过；
- 真机验证项（合入后执行）：正常关闭后 `desktop.log` 无该 ERROR 且回执
  `ok=True`；关闭后立即重启无 `replacing resident Worker` /
  `worker bring-up failed` / `active resource leases`；窗口 X、托盘退出、
  更新重启三条路径逐一验证。

## 遗留

- 仓库根目录存在误生成的 `nul` 保留名文件与未跟踪 `vendor/`，与本修复
  无关，未处理；
- `shutdown.py:136` 的 `asyncio.to_thread` 在解释器健康期无害，保持原样。
