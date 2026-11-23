---
id: B_20261119_1104_paper-snapshot-restart
title: 论文优化历史快照的重启恢复
type: B
author: l_x_d
author_email: linxiangda@huawei.com
modules:
  - jiuwenswarm/agents/harness/common/rsi
files:
  - jiuwenswarm/agents/harness/common/rsi/paper_workspace_provider.py
  - jiuwenswarm/agents/harness/common/rsi/provider_factory.py
  - tests/unit_tests/rsi/test_provider_snapshot_restart.py
designs: []
design_note: 按确认的重启恢复用例修复既有行为，无独立设计。
---

# 论文优化历史快照的重启恢复

AgentServer 重启后，旧论文任务的用量和结果文件仍存在，详情和报告却可能缺少 token、进度、分数和最优产物。问题来自 agent-core 论文 Provider 在查询时使用进程内运行目录表。新进程没有历史任务的映射。

workswarm 装配的论文 Provider 先使用现行运行目录，历史查询才从当前任务根目录解析并确认快照位置。状态、报告、优化树和产物继续由 agent-core 读取和投影。用量缺失及已记录的零值遵循 S_01_observation-projection 的约定。

新增回归通过真实 Provider 和 RSI 请求处理器从磁盘查询历史任务，覆盖首次读取用量、详情、报告、优化树和最优产物。程序优化、Harness 优化及无用量的旧论文快照作为对照。发布分支修复前的八个用例中，五个论文恢复用例失败，三个对照通过。

修复后，`releases/0.2.9` 与 `develop` 各自在其锁定 agent-core 版本下运行 `pytest tests/unit_tests/rsi`，均通过 319 项。测试使用临时目录和持久化快照，不启动模型。
