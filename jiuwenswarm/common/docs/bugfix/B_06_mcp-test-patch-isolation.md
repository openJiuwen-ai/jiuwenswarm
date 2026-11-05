# 全量 UT 已安装 MCP 补丁时测试重复包装导致锁等待超时

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-10-30 |
| 严重程度 | 中：PR CI 的 33 项 MCP 单测失败 |
| 范围 | MCP timeout、recovery、lifecycle 测试 fixture |
| 测试基线 | Python 3.11 双进程、Python 3.12 各 387 项 MCP 回归通过 |
| 证据 | [PR #9175 UT 报告](https://openjiuwen-ci.obs.cn-north-4.myhuaweicloud.com/jiuwenswarm/ut/9175/unit_test_report.html)，2026-10-29 11:17:56 |

## 现象与根因

CI 使用 Python 3.11.5、AnyIO 4.15.1、pytest-xdist，报告 12988 项通过、33 项失败。
失败均位于 test_mcp_call_timeout_patch.py 与 test_mcp_remote_lifecycle.py。
调用栈显示一层超时 wrapper 已持有客户端 I/O 锁，内部另一层 wrapper 再次获取同一锁，
最终超时；重复安装 ToolMgr factory 还可能叠加 owner。

测试 fixture 清空了 `_PATCHED` 和 wrapper 注册集合，但保留了前序 adapter 已安装的
类方法与 factory。重新 apply 因而不是从原始 SDK 开始。recovery fixture 同时存在
只清标记、没有完整恢复 SDK 绑定的问题。独立小范围测试缺少前序补丁安装，未触发故障。

## 决策

- timeout 与 lifecycle fixture 遇到已安装补丁直接复用，不重置 guard 后重复包装。
- recovery fixture 同时快照并还原 SDK 方法、ToolMgr factory、补丁标记与注册集合。
- AbilityManager 模块绑定、Tool invoke 和子类钩子恢复进入测试时的精确状态；不能
  无条件退回原始 invoke，否则会移除前序已安装的超时桥。
- 增加独立进程回归：收集结束后提前安装生产补丁，交错运行 recovery、timeout 和
  lifecycle 用例，并检查整个测试结束后的方法绑定和 guard 与安装后相同。

## 拒绝的方案

不放宽生产超时、不跳过失败用例、不仅提高测试时间阈值，也不为测试重复安装而改变
生产补丁的生命周期结构。问题在 fixture 的状态隔离，应成对恢复绑定与标记。

## 验证

修复前，提前安装补丁即可稳定复现 SSE、HTTP 正常发现两项失败。修复后，在相同
预安装条件下 Python 3.11 双进程和 Python 3.12 的 387 项 MCP 回归全部通过；新增
子进程回归验证实际安装状态没有被测试污染。公共模块另有 536 项通过。

公共模块全量收集受本机缺失 openjiuwen_runtime.foundation.security 限制，因此
536 项结果排除了 test_link_mtls.py；没有将其计为通过。未重跑远端完整 13062 项
UT，仍需推送后由 CI 确认。静态致命错误检查与 git diff --check 通过。

## 边界

本修复处理 UT 失败，与 P2 清理等待修复一同提交。CLA 由提交者处理；CI 报告另有
开源合规失败，其详情页面公开读取被拦截，本次不宣称该项已解决。
