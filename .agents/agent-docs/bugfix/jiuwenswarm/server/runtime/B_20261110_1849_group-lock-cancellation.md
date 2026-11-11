---
id: B_20261110_1849_group-lock-cancellation
title: 群任务取消期间等待文件锁清理完成
type: B
author: ShowHsiang
author_email: xiangxiaoneng@h-partners.com
modules:
  - jiuwenswarm/server/runtime
  - tests/unit_tests/server
files:
  - jiuwenswarm/server/runtime/group_service.py
  - tests/unit_tests/server/test_group_lock.py
  - tests/conftest.py
designs: []
design_note: 修复现有群锁的取消清理契约，不新增管理器或调整任务并发策略。
---

# 群任务取消期间等待文件锁清理完成

## 现象与证据

GitCode !9841 的 `e5bbb24d50726a76e2117cb26e5c87740a06fe2b` 在
2026-11-10 17:54 生成的 UT 报告中有 15,585 passed、29 skipped、1 error。
原报告为用户提供的 `unit_test_report (3).html`。

错误节点是
`tests/unit_tests/server/test_group_task_continuation.py::test_proxy_holds_at_most_five_unfinished_private_runs`。
失败发生于 pytest-asyncio fixture teardown，达到 60 秒限制；线程栈停在
`asyncio.to_thread -> guard.__enter__ -> portalocker.Lock.acquire`，不是搜索功能断言失败。

该报告没有完整 asyncio task 栈，不能仅凭线程栈还原那一次 CI 的全部调度。
原用例在本机单独运行可以通过，但可控时序回归在旧实现上稳定发现三个取消清理缺口。

## 根因与修复决策

`group_lock` 原实现只保护第一次获取锁的等待。在第一次取消后直接等待 acquisition，
第二次取消可取消该 asyncio task，而工作线程仍会继续获取文件锁。
释放锁的 `to_thread` 等待也未保护，取消可以让上下文先退出，无法保证锁已释放。
这与报告中的清理阶段锁等待一致；相关实现来自既有 `131c18483e`，并非本轮 setuptools 修改。

修复复用原来的进程内锁和 SDK 文件锁，只增加一个小的 I/O 等待辅助函数：

- 获取与释放线程操作都通过 shield 等待；重复取消被记录，不能中断清理所有权。
- 获取期间收到取消时，获取完成后直接释放，不能执行临界区业务。
- 释放完成后继续传播 CancelledError，不把取消误报为成功。
- 进程内锁直到文件锁释放完成才退出，不提前放行下一个 writer。
- 获取失败和临界区异常继续传播，不增加自动重试、提高超时或修改五任务上限。

本地 Windows 验证还发现测试退出时日志 handler 未解除，临时 home 删除报 WinError 32。
测试 unconfigure 先移除并关闭 Swarm handlers，再 shutdown logging，最后清理临时目录；
只改测试资源清理，不修改生产日志策略或吞掉测试失败。

## 回归证据

环境：Windows、Python 3.11.5，官方 Agent Core
`2dc7477af89ceedbad550de88aac3291db578506`，并非 `D:/agent-core` editable。

- 旧实现：获取期间三次取消、释放期间一次/三次取消三个定向用例失败。
- 新实现：七个群锁用例通过，覆盖真实文件锁重获、取消、获取失败、业务异常和串行互斥。
- PR 工作区：群锁与完整私有团队 continuation 用例共 40 passed。
- 扩大到群服务/执行存储：163 passed；另一个既有符号链接测试因本机 Windows
  缺少创建 symlink 的权限失败。没有修改或跳过 CI 的该用例。
- 主工作区合入官方 develop `5c3c443d8`、保留本地搜索增量后，浏览器路由、搜索配置、
  工具适配、提示词及群任务定向回归共 241 passed。
- 原 CI 失败节点使用十个独立 pytest 进程重复验证，10/10 通过；未运行完整 Linux CI。

执行命令使用 `python -m pytest ... -o addopts='' --asyncio-mode=auto --timeout=60`，
未延长 CI 原用例的超时；`--asyncio-mode=auto` 与仓库默认配置保持一致。

## 依赖更新与遗留

主工作区 develop 合入官方最新源码后，Core 必须保留官方 `2dc7477a`，不能机械回退到
Swarm 官方模板的 `f3f8f576`：后者不包含搜索修复 `79f6ec656`，前者包含它。
所有 SDK 依赖仍来自官方仓；`uv sync --locked` 与安装元数据核验均通过。
保留官方其余包版本和平台 marker，锁文件仅变更 SDK 来源、根依赖引用及 SDK 新增的
`markdown-it-py` 依赖边；再次执行 `uv lock --check --offline` 和
`uv sync --locked --offline` 通过。

Gateway !76 尚待合入，Swarm 当前 Gateway pin 仍依赖旧 SDK；uv 使用既有 override
统一 SDK，不代表正式 wheel 的 pip 依赖冲突已修复。该问题不归因于本次 UT 取消修复。
官方 Gateway 合入后，应再更新 Swarm 的 Gateway pin 并验证 st_gateway。

## develop PR 提交前复验（2026-11-11）

用户确认将先前暂缓的修复提交到 !9841。基线为已合并官方 develop 的 `da644889d`，
没有合入其他本地运行时改动，也不修改 Gateway 或 SDK pin。

独立环境使用官方 Core `2dc7477a`、Gateway `54e5330d`。直接执行 pytest，未使用
临时日志清理插件：群锁和 `test_group_task_continuation.py` 共 **50 passed**，
进程正常退出，包含原 CI 错误节点与七个取消/文件锁回归用例。
本次修复仍需新一轮 Linux CI 确认整体稳定性，未宣称全仓 CI 或 st_gateway 已通过。
