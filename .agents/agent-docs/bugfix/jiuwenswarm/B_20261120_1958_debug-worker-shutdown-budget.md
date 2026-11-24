---
id: B_20261120_1958_debug-worker-shutdown-budget
title: Debug 重启提前终止正常清理中的 Worker
type: B
author: alan_cheng
author_email: chengshuo5@huawei.com
modules:
  - jiuwenswarm/debug_launcher.py
files:
  - jiuwenswarm/debug_launcher.py
  - Makefile
  - tests/unit_tests/test_debug_launcher.py
  - tests/unit_tests/test_debug_makefile.py
  - tests/unit_tests/agentserver/test_debug_worker_shutdown_budget.py
designs: []
design_note: 用户确认经真实子进程对照验证的局部修复；保留已有生命周期和身份保护，无独立设计文档。
---

# 现象与根因

`make restart-debug` 遇到缺少 deployment identity 的历史 Worker 时，停止命令要求显式选择 PID 并中断重启。这一身份保护符合预期；Makefile 原先没有停止参数透传入口。

另一个可确认的问题是 debug 默认将同一 15 秒预算传给 Front 和 Worker，而 Worker 内部有 30 秒清理期限。生命周期管理器还从外层预算中预留进程回收时间，实际可能约 14 秒就强杀仍正常清理的 Worker。

修复前隔离实验让相同 Runtime 替身执行 18 秒保存：默认停止在 14.20 秒强杀且保存未完成；仅指定 `--timeout 35` 时，18.43 秒完成保存并正常退出。32 秒保存则在内部 30 秒期限后明确失败。实验使用真实 CLI、IPC 和 Worker 进程，未加载真实业务 Runtime；不能据此确定用户旧进程当时阻塞的具体原因。

# 修复与取舍

- 默认 Front 保持 15 秒，Worker 使用 35 秒；显式 `--timeout` 仍应用于每个阶段，新增 `--worker-timeout` 单独覆盖 Worker。以 `None` 区分未指定和显式传入 15 秒，避免破坏旧调用的预算语义。
- 不在 debug 模块导入时加载 Worker/Runtime；默认预算以轻量常量表达。
- Makefile 三个停止/重启入口通过 `STOP_ARGS` 透传 legacy PID 与预算；停止失败继续中断重启。
- 不自动认领无部署身份的旧进程，不根据相同解释器直接强杀，也不将强杀或保存未确认视作成功。
- 不修改 Worker 的 30 秒内部期限；单纯将 Front 和 Worker 的默认值一同改为 35 秒，会无必要地延长 Front 阶段，并掩盖两个阶段的职责区别。

当前契约见 [S_01_debug-shutdown](../../specs/jiuwenswarm/debug_launcher/S_01_debug-shutdown.md)。中英文运行说明已同步默认值、显式覆盖规则和 Makefile 恢复命令。

# 验证

修复候选基于 swarm `c86d8c76c`，本地 core editable 依赖不入本次修改。环境为 macOS arm64、Python 3.13.11。

新增行为测试覆盖预算选择及显式旧 PID、非法超时输入、真实 Makefile 参数透传和失败中断，以及真实 Worker 在默认预算下完成 18 秒保存。显式 2 秒 Worker 预算遇到 5 秒保存时仍必须失败、未保存且保留 debug 状态。真实进程用例仅替换业务加载，沿用生产 IPC、HELLO、关闭应答和进程退出流程；所有资源在临时目录，测试仅回收自己启动的子进程。

本轮定向回归 135 项通过（60.91 秒），覆盖 debug launcher、Makefile、真实 Worker 预算、resident 关闭、关闭期限和 legacy 兼容六个测试文件：

```bash
source .venv/bin/activate
export PYTHONPATH=.:$PYTHONPATH
TMPDIR=/private/tmp python -m pytest \
  tests/unit_tests/test_debug_launcher.py \
  tests/unit_tests/test_debug_makefile.py \
  tests/unit_tests/agentserver/test_debug_worker_shutdown_budget.py \
  tests/unit_tests/agentserver/test_resident_worker_shutdown.py \
  tests/unit_tests/agentserver/test_worker_shutdown_deadline.py \
  tests/unit_tests/agentserver/test_worker_legacy_compatibility.py \
  -o addopts= --asyncio-mode=auto --basetemp=/private/tmp/jw-debug-fix-final \
  -o log_cli=false -q --tb=short
```

首次扩大回归为 131 通过、2 失败：macOS 临时目录 `/var/...` 与规范化后的 `/private/var/...` 不同，导致旧用例的 socket 参数不匹配和启动进程未被同路径识别。切换为规范化的短 `TMPDIR` 和 `--basetemp` 后，上述用例均通过；未放宽生产身份校验，也未修改这些旧用例。原始 JUnit 输出在本机 `/tmp/jw-debug-fix-final.xml`。

本地差异检视未发现本次范围内的可确认问题。未执行全仓测试、Windows/Linux 实机验证或真实业务会话持久化验证。
