# 远程 MCP 内部取消误传给调用者且取消后的清理等待无上限

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-10-28 |
| 严重程度 | 高：单个连接器失败可能中断请求，清理失活可能阻塞取消 |
| 范围 | mcp_config.py、mcp_call_timeout_patch.py |
| 测试基线 | Python 3.11 / 3.12 各 366 项相关测试通过 |
| 关联 spec | S_09_mcp-integration.md，N7 |
| 评审来源 | [PR #9175](https://gitcode.com/openJiuwen/jiuwenswarm/pull/9175/discuss) |

## 现象

worker 初始化被取消时，排队调用者收到 CancelledError，无法区分工具初始化失败和
用户主动取消。发现阶段的内部取消也可跳出连接器隔离边界。SSE public disconnect
自身取消时，shield 向等待者抛出相同异常，原实现把它误判为等待者取消。

清理等待持续屏蔽取消，若 owner 无回执或底层任务不退出，取消流程便无限等待。
本修复延续 B_02 的任务归属约束，补齐异常分类和清理等待的时间边界。

## 根因

- 初始化分支捕获 BaseException 后，直接把原异常写入所有排队请求的 future；
  worker 的生命周期取消因而变成业务调用者的取消。
- discovery 只捕获 Exception，内部 CancelledError 穿过隔离层；SSE 断开等待
  则没有检查当前等待者是否真的收到 cancel 请求。
- `_finish_mcp_task_after_cancel` 在 task.done 前无限循环 shield，没有截止时间。
  单纯为外层添加超时不能终止这个吞掉后续取消的等待循环。

## 决策

- worker 初始化中的非 Exception 异常映射为 McpWorkerStoppedError，通知全部排队
  请求并完成队列记账；worker 自身继续传播原异常。
- discovery 检查当前任务的 cancelling 状态：真实外部取消继续传播，内部取消
  记录警告并返回空发现结果，仅跳过当前连接器。
- SSE 断开内部取消映射为普通清理失败，并标记连接不可复用；等待者真实取消仍
  在清理后传播原 CancelledError。
- 清理等待最多 10 秒，超时取消目标后最多再等待 1 秒；后续取消不重置截止时间。
  SSE 从 public disconnect 启动时计时，避免外层超时后额外等待完整清理预算。
  asyncio.wait 只等待任务，不在等待者取消时顺带取消目标。AnyIO shield 仅保护等待。
- 超时报告清理失败并保留不可复用状态；不丢弃资源句柄。仍不响应取消的任务若
  后续结束，取走异常以免出现未处理任务异常，资源清理仍由原 owner 负责。
- 补充清理失败后恢复的断言：后续 disconnect 成功时允许重连，替换旧 session，
  清除 needs_reconnect，避免失败状态永久阻止恢复。

## 同构路径检查

| 入口 | 确认结果 |
|---|---|
| 池化 worker 的 `_enter_remote_mcp_session` | HTTP 生命周期在 worker；SSE 使用统一 owner 清理包装，初始化排队失败统一映射 |
| `_list_remote_mcp_connector_tools` | 完整发现生命周期留在同一任务，SSE 使用同一包装，外层区分内部与外部取消 |
| ToolMgr `_create_client` 补丁 | HTTP 独立 owner 与 SSE SDK owner 共用有界清理等待，清理失败标记不可复用 |
| DeepAdapter 安装入口 | `interface_deep.py` 仍统一安装补丁，没有新增绕过路径 |
| registry 扫描、请求级连接器 | 通过既有取消隔离、停止、worker retire 回归，未改变关闭所属任务 |

## 拒绝的方案

不统一吞掉 CancelledError，否则用户取消会失效；不直接向排队调用者重放 worker
取消，否则单个工具失败会被误认为请求取消。不对清理使用无限 shield，也不在超时
后无界 await 被取消的任务。不在其它任务补做 aclose、不修改 AnyIO 私有状态，避免
重新引入 B_02 的跨任务退出问题。

## 验证

先运行内部取消、worker 初始化取消和清理预算回归，在原实现上得到 10 项失败。
修复后生命周期与恢复测试共 71 项通过。扩大到 MCP timeout、配置、工具刷新、
registry、request-scoped、retire、schema cache、browser isolation 和 stdio，
Python 3.11 和 3.12 各 366 项通过。

生命周期测试通过完整 Ruff 检查；修改模块及恢复测试通过致命语法和未定义名称
检查，git diff --check 通过。恢复测试文件完整 Ruff 的 17 项历史告警与 HEAD
一致，本次未新增。

新增测试覆盖双 transport 的 connect/list 内部取消、多个排队请求排干、SSE 内部
断开取消、断开无回执，以及目标忽略 cancel 的有界收尾。既有真实 SDK + 内存 AnyIO
传输用例继续验证真实外部取消、重复取消和 AnyIO 外层取消不破坏 owner 归属。

## 已知遗留

Python 无法强制终止忽略取消的协程；超时后保证等待者能退出并报告失败，不承诺底层
资源已全部释放。不能安全复用的 client 应被替换，不能跨任务强制关闭其上下文。
测试不访问真实远程服务；测试环境对缺失的 openjiuwen_runtime DB 模块使用兼容桩，
MCP 生命周期用例使用本机 agent-core 的真实客户端，结果不代表所有 SDK 版本的集成验证。
