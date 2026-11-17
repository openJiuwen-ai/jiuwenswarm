---
id: S_01_observation-projection
title: RSI 进度、报告与前端终态投影
type: S
author: l_x_d
author_email: linxiangda@huawei.com
modules:
  - jiuwenswarm/agents/harness/common/rsi
  - jiuwenswarm/channels/web/frontend/src/features/rsi
files:
  - jiuwenswarm/agents/harness/common/rsi/artifact_adapter.py
  - jiuwenswarm/agents/harness/common/rsi/services.py
  - jiuwenswarm/channels/web/frontend/src/features/rsi/rsiStore.ts
  - jiuwenswarm/channels/web/frontend/src/features/rsi/components/RsiResultSummary.tsx
---

# RSI 观测投影

最近修订：2026-11-14。范围为 Provider 快照经服务映射到 Web 的报告、详情和侧栏；引擎搜索预算与错误生成由 agent-core 维护。

## 状态与不变量

- TaskStore 为公开任务状态权威；Web 详情轮询同步相同任务的列表 status/running。非 RUNNING 状态不显示运行点。
- 实时 progress 仅作为运行态展示来源。完成、失败、终止、暂停后清除 liveProgress，终态/暂停态拒绝晚到 progress。
- 已有终态不会被在途轮询的旧运行态覆盖。选中任务收到终态推送时，若轮询正在进行，则在其结束后再获取最终报告。
- 汇总在非运行态使用报告 metrics.iterations，回退 task.progress.iteration。节点标题的第 N 轮可以先于完成计数出现。

## 报告接口

rsi.report.get 保留 Provider 显式 metrics，包括零值。缺失 pruned_count 时，若 get_tree 可用则统计 type=PRUNED 的节点，REJECTED 和 PROVISIONAL 不计入；树不可用时为 null，不把未知伪装为零。
缺失 iterations 时使用 Provider state.iteration，再回退 tree.iteration；不能以节点编号或总节点数代替程序完成计数。
论文展示已知剪枝数，程序不展示剪枝列；组合评测列只用于 Harness。

## 兼容与验证

旧 Provider 不实现 get_tree 时，报告接口继续可用，剪枝数未知。Provider 自身的轮次定义保持不变。
回归入口：tests/unit_tests/rsi/test_report_metrics.py、frontend/tests/rsiPolling.test.mjs、frontend/tests/rsiHarnessPreview.test.mjs。
