# SpreadsheetBench / Multi-SWE-bench 复核对照｜2026-09-16

后续看护改动与原题短测见 [看护 Agent 短测](duplex-live-review-quick-20260916.md)。下表为此前三 Agent 版本。

**各取 1 道原题，加入复核 Agent，在 `wzr@47.79.124.13:31442` 跑普通 / 全双工对照。实际打断均为 0，未证明全双工提速。**

| 数据集 | 本次题号 | 内容 |
|---|---|---|
| SpreadsheetBench Verified（400 题） | `13-1` | 表格合并、分组汇总、排序与合计 |
| Multi-SWE-bench Flash（300 题） | `iamkun__dayjs-1953` | 修复小于 1000 年时 YYYY 未补足四位 |

| 数据集 | A2A | 运行秒数 | 最终产物质量 | 停止原因 | 实际打断 |
|---|---|---:|---|---|---:|
| SpreadsheetBench Verified | 普通 | 641.6 | 120/120 单元格正确 | API TPM 限流 | 0 |
| SpreadsheetBench Verified | 全双工 | 617.5 | 120/120 单元格正确 | 正常完成 | 0 |
| Multi-SWE-bench Flash | 普通 | 224.6 | 730/730 测试通过，Bug 已修复 | API TPM 限流 | 0 |
| Multi-SWE-bench Flash | 全双工 | 439.6 | 729/730 测试通过，Bug 未修复 | 调用预算耗尽 | 0 |

秒数是启动团队至停止的时长，不含环境准备和最终评分。异常停止的产物也独立评分；上表不能用来判断正常完成速度的优劣。

三 Agent：办公为执行 / 分析 / 复核；开发为修复 / 定位 / 复核。复核用带 SHA256 的真实文件快照，消息走 Jiuwen DB；未注入人工错误或打断指令。办公全双工完成了实际产物复核；开发全双工复核在约 405 秒才发来正确定位，执行 Agent 随后耗尽预算。消息主要是补充和确认，未触发 INTERRUPT。

统一设置：做题 `DeepSeek-V3.2`，路由 `Qwen3.5-9B`，temperature=0、关闭 thinking；每 Agent 30 次做题调用、每队 90 次 Bash、720 秒上限。每策略仅一次。

评分：表格经 LibreOffice **7.6.7.2** 重算，用上游比较函数检查 `LISTS!A3:D32`；代码基线 `cbe91fd1087767337ff1089325c96bf2a1915eac`，评分端应用原测试补丁，Node 22.23.1 / Jest 22.4.4 / UTC 跑测试。原始输入失败、参考答案通过的校验均已完成；隐藏答案不提供给 Agent。这是原生环境试跑，非全量或官方容器成绩。

初轮 4 次也保留在 `review-runs-v1`：480 秒上限，仅办公全双工产物通过，均超时或耗尽预算、0 次打断。上表是明确职责后重跑的 `review-runs-v2`，两轮不混算。

日志、产物、补丁、评分和复现代码：`/workspace/wzr/duplex-benchmark-20260908/office-code-20260915/duplex-review-datasets-20260916.tar.gz`。逐次结果见包内 `summary.json`，本地副本在 `D:\jiusi_agent\_deps\duplex-review-20260916`。

代码验证：6 项单元测试、2 项三 Agent 接线测试通过；Ruff 检查通过。
