---
id: B_20261118_1017_issue6796-desktop-worker-shutdown
title: Python 桌面退出等待常驻 Worker 清理
type: B
author: 刘瑜
author_email: liuyu524@huawei.com
modules:
  - jiuwenswarm/channels/desktop
files:
  - jiuwenswarm/channels/desktop/desktop_app.py
  - tests/unit_tests/test_desktop_close_behavior.py
  - tests/unit_tests/common/test_update_shutdown.py
designs: []
design_note: 对现有应用退出契约的时序修复，无独立设计。
---

# Python 桌面退出等待常驻 Worker 清理

[Issue #6796](https://gitcode.com/openJiuwen/jiuwenswarm/issues/6796) 中，窗口关闭回调已开始停止 Front，主线程的重复关闭调用直接返回。Python 开始结束解释器后，关闭线程在进程扫描的 `asyncio.to_thread()` 处失败，常驻 Worker 未收到最终关闭请求。

第一次调用负责清理并写入升级回执，在 `finally` 中发布完成信号。主线程发起关闭后等待该信号。窗口回调、启动失败处理和主线程共用一次清理，等待期间不持有服务注册锁。Worker 的身份检查、关闭期限、确认退出和失败回执仍由现有关闭链路处理。

主线程与清理线程共用从关闭开始计时的 30 秒期限，另留 5 秒回收进程和写回执。等待超时只记录关闭未完成，升级仍要求负责清理的线程写出真实的成功回执。测试使用守护线程和有界结果等待，覆盖清理迟迟不结束时的回执状态。

在 Windows / Python 3.13.9 上，两条分支的旧版本均复现原异常并留下 Worker。修复后，原生 WebView2 窗口关闭和启动失败时退出均收到一次成功应答、观察到 IPC stub Worker 退出，并在主线程返回前写好回执。回归用例检查成功、Worker 关闭失败和清理提前抛异常，后两种情况下升级安装器仍拒绝继续。
