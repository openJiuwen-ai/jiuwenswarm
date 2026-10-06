# SpreadsheetBench / Multi-SWE-bench：看护 Agent 短测

**真实公开原题、真实模型、远程执行。能自动触发打断，但本轮未证明提速或质量提升，仍存在复核误报。**

2026-09-17：[13-1 失败原因复盘](duplex-13-1-regression-20260917.md)：打断前反复解析、看护漏检逻辑错误，打断后多了一轮 10.1 秒的无效确认。

后续：[13-1 修复后复测](duplex-13-1-rerun-20260917.md)：600 秒上限下普通组 120/120、全双工组 71/120，均无打断；以下保留原短测结果。

| 数据集原题 | A2A | 耗时（秒） | 原始评分 | 实际打断 | 停止原因 |
|---|---|---:|---|---:|---|
| SpreadsheetBench Verified `13-1` | 普通 | 181.3 | 120/120 单元格正确 | 0 | 超时 |
| SpreadsheetBench Verified `13-1` | 全双工 | 181.3 | 未生成输出表格 | 1 | 超时 |
| Multi-SWE-bench Flash `iamkun__dayjs-1953` | 普通 | 241.3 | 730/730 测试通过，Bug 已修复 | 0 | 超时 |
| Multi-SWE-bench Flash `iamkun__dayjs-1953` | 全双工 | 227.6 | 730/730 测试通过，Bug 已修复 | 2 | 模型调用预算耗尽 |

时间为团队启动至停止，不含准备和最终评分。**代码全双工少用的 13.7 秒不能算提速：它先耗尽调用预算，两组都未正常结束。** 每策略仅一次，不能推断总体效果。

实际发生的打断：

- 办公：执行脚本混排日期对象与字符串，出现 `TypeError`；报错后 **4.42 秒**触发打断。
- 代码：Agent 自己把测试放错目录，Jest 找不到测试；报错后 **3.46 秒**打断。随后测试导入路径写错；报错后 **4.10 秒**再次打断。
- 错误由 Agent 做原题时自然产生，未注入人工故障或打断指令。这证明通路可用，尚未证明打断比执行 Agent 自己处理报错更有效。

改动：看护改为异步检查真实工具结果和文件变化；正常读文件、基线复现失败、`grep/rg` 无匹配不调用复核模型；重复纠错去重，旧文件版本的意见丢弃。普通 / 全双工均为执行 Agent + 看护 Agent，消息走 Jiuwen DB。路由能看到实际命令和输出。

仍有问题：办公普通组的产物评分全对，看护却误以为应该按金额排序。代码普通 / 全双工看护调用为 **1 / 5** 次（后者含 1 次超时、1 次因团队停止而取消）；办公为 **1 / 1** 次。主要耗时仍包括执行 Agent 反复查找、测试调用错误和模型响应等待。

数据来源：[SpreadsheetBench 官方仓库](https://github.com/RUCKBReasoning/SpreadsheetBench)、[Verified 400 原始包](https://huggingface.co/datasets/KAKA22/SpreadsheetBench/blob/main/spreadsheetbench_verified_400.tar.gz)；[Multi-SWE-bench 官方仓库](https://github.com/multi-swe-bench/multi-swe-bench)。本次取 Verified 首题、Flash 首道 JavaScript 题。表格按上游函数检查 `LISTS!A3:D32`；代码固定基线 `cbe91fd1087767337ff1089325c96bf2a1915eac`，评分端应用原始隐藏测试补丁。Agent 额外拿到的 smoke test 仅来自公开 Issue 的 `0202-01-01` 示例。

配置：服务器 `wzr@47.79.124.13:31442`；执行 `DeepSeek-V3.2`，看护 / 路由 `Qwen3.5-9B`，temperature=0，关闭 thinking。办公上限 180 秒 / 18 次模型 / 16 次 Bash；代码上限 240 秒 / 24 次模型 / 24 次 Bash。同题两策略预算相同。Node 22.23.1 / Jest 22.4.4 / UTC；LibreOffice 7.6.7.2。原生环境试跑，非官方容器或全量榜单成绩；办公与最终代码轮有并行时段。

表中办公为 `live-review-quick-v3`，代码为 `v4`（额外修正搜索返回码）。其余调试运行全部保留，包含 `grep` 无匹配导致的误打断，不混入上表。代码验证：11 项单元测试、2 项 A2A 接线测试通过，Ruff 通过；这些只验证机制，不算数据集成绩。

日志、原始结果、产物、补丁和源码快照：`/workspace/wzr/duplex-benchmark-20260908/office-code-20260915/duplex-live-review-quick-20260916.tar.gz`。本地副本与 `live-review-summary.json`：`D:\jiusi_agent\_deps\duplex-review-20260916`；摘要中 `final_comparison=true` 为上表四次运行，归档旁附 SHA256。
