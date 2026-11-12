---
id: B_20261112_1030_task-round-settlement
title: 全双工连续任务等待外层轮次收尾
type: B
author: yzdnh123
author_email: yzdnh123@gmail.com
modules:
  - jiuwenswarm/server/runtime/video_task
files:
  - jiuwenswarm/server/runtime/video_task/execution.py
  - jiuwenswarm/extensions/video_duplex/tests/backend/test_managed_tasks.py
designs: []
design_note: 修复已有串行队列的执行收尾证据缺口，无独立设计。
---

用户报告连续语音创建两个任务时，第二个任务可能开始即失败。无现场日志，未认定测试现场的根因。

## 根因与决策

capture_execution 在调度器句柄存在时只收集该句柄，未同时保留外层 _interaction_round_task。调度器已结束但外层轮次仍清理时，settle 可提前确认；队列因此可能放行下一任务，而启动检查仍检测到 active_round，抛出会话忙碌异常。TaskService 将普通启动异常记为 failed。

修复同时收集调度器与外层轮次句柄，沿用原有等待和当前任务排除机制，不增加重试、额外超时或绕过 busy 检查。旧输出关闭失败继续拒绝交接，不能将释放不确定当成安全成功。

```mermaid
flowchart LR
 A[调度器结束] --> B[等待外层轮次清理]
 B --> C[确认执行收尾]
 C --> D[允许下一个排队任务启动]
```

## 验证

Python 3.13，使用主工作区虚拟环境，在当前候选工作区执行。新增确定性测试控制调度器先完成、外层轮次延后释放；修复前 execution_settled 错误为 true，断言失败；修复后保持 false，待外层释放才变 true。测试还通过真实 bind_task_execution 入口确认该窗口中下一请求会被 busy 检查拒绝。

运行 test_managed_tasks.py 与 tests/unit_tests/agentserver/test_interrupt_output_handoff.py，参数 -o addopts='' --asyncio-mode=auto -q：78 passed。git diff --check 通过。pytest 退出清理历史临时目录时出现 WinError 5，不影响测试结果，进程退出码为 0。

覆盖真实 SQLite/checkpoint 协议及受控执行句柄，未进行真实 Qwen 连续语音端到端测试，未证明现场 Core 必定出现此时序。未重启正在运行的服务。

## 关联问题与交付范围

关联 GitCode #6454 / GitHub #11274。配套移植主服务工作区已实现的生命周期修复：移除工具 RPC、卡片恢复确认、取消与执行收尾的全双工额外期限；等待由 Core 实际状态终结。卡片替换、失效、服务关闭可结束答案等待，同一卡片的并发提交拒绝重复处理。关联 GitCode #6446 / GitHub #11264。保留连接发送保护与模型原有超时。
