# 远程 MCP 跨任务退出 AnyIO 上下文导致取消回调持续运行

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-10-28 |
| 严重程度 | 高：后台工具扫描亦可触发，业务结束后仍持续占用 CPU |
| 范围 | mcp_config.py、mcp_call_timeout_patch.py、mcp_server_registry.py |
| 关联 spec | S_09_mcp-integration.md |

## 现象与根因

Streamable HTTP 在 connect 调用任务上进入 transport 和 ClientSession 的 AnyIO
TaskGroup。旧实现仅把 connect 交给 asyncio.wait_for，disconnect 却在调用方执行。
Python 3.11 的 wait_for 创建子任务，因而破坏上下文必须在原任务退出的不变量。
Python 3.12 的正超时实现直接 await 协程，可能掩盖该错误，验证必须覆盖 3.11。

超时补丁另有同构问题：丢弃旧 stack 后，等宿主任务结束再新建任务 aclose。
延迟执行不会转移 AnyIO 上下文归属；SSE 已自行关闭资源，重放旧 stack 还可能重复退出。
因此正常发现、调用超时、重连和后台扫描都需要一起检查。

## 决策

- 发现封装完整生命周期，connect/list 各保留原阶段超时，外层等待整个生命周期。
- 长连接复用原池化 worker，connect 与可重连的调用使用 asyncio.timeout 留在 owner。
  初始化取消也排干队列，避免首个调用者永远等不到结果。
- ToolMgr 的 HTTP client 没有现成 owner，增加一个仅限该入口的串行任务；SSE 仍用
  SDK owner，不叠加任务；补齐该 owner 在建连取消时的 finally 清理，避免断开排队等
  initialize 的长超时。排队调用者取消不打断其它正在运行的调用。
- SSE public disconnect 在命令返回后还负责停止 owner：将这个完整 API 流程保留到
  完成，防止等待者取消后只关资源却遗留空闲 owner。此处新增的是 API 等待任务，
  传输与 session 的 __aexit__ 仍在原 SDK owner 执行。
- 已进入取消处理后，屏蔽等待者的后续取消向 owner 传播，等待同任务清理完成再
  re-raise 原取消。AnyIO shield 仅包等待过程，避免外层取消 scope 反复唤醒等待循环。
- 取消按原异常传播。超时保留 SDK timeout/read timeout 传递，死会话仍最多重试一次。
- disconnect 成功后才重置状态；False 或异常上报，不再延迟 aclose 或丢弃未关资源。
  同步 force_invalidate 只标记不可复用，实际关闭由 owner 执行。owner 已结束而资源
  仍未清时，禁止换新 owner 复用 client。
- registry 等待整次发现结束才返回，扫描停止时先等发现 finally，再关闭全局 worker；
  没启动 scanner 也必须关闭已有 worker。

## 拒绝的方案

不改 AnyIO 私有状态，不全局改 wait_for，不删除超时，不吞取消或断开失败。
不把每个池化连接再包一层通用 owner 框架；只有没有 owner 的 ToolMgr HTTP 入口新增任务。
不修改 agent-core；SSE 内部的资源关闭职责继续由 SDK 承担。

## 验证

`test_mcp_remote_lifecycle.py` 使用真实 SDK 客户端、真实 AnyIO TaskGroup 和内存传输，
不访问外网、不需要凭据。故意跨任务退出的反例放在有界子进程，避免污染 pytest 循环。
成功、initialize 失败/超时、发现超时/取消、worker 退出/重建、ToolMgr 重连/取消、
registry 扫描超时/停止均检查资源子任务已结束，取消回调在若干轮事件循环后不再增长。
修复前的 mcp_config 在 Python 3.11 下运行同一发现回归用例，产生明确的
`Attempted to exit cancel scope in a different task than it was entered in`，并因
进入/退出任务不一致而失败。修复后 Python 3.11 和 3.12 各通过 358 项相关测试，
包含 41 项生命周期用例及既有 MCP timeout/recovery、配置、工具刷新、registry、
request-scoped、retire、schema cache、browser isolation 和 stdio 回归。
继续审查中，先用事件控制的慢清理复现两处遗漏：SSE 断开期间取消留下空闲 owner；
HTTP 调用者第二次取消再次中断 owner 清理。修复后同时覆盖两种 transport 的建连/
调用重复取消，以及 AnyIO 外层超时取消，检查清理未被二次取消且无遗留任务。
新增生命周期测试通过 Ruff；修改模块通过致命语法/未定义名称检查及 git diff --check。

## 已知边界

模拟传输证明已覆盖路径的任务归属及清理，不等价于真实服务和所有 SDK 版本的集成验证。
底层 disconnect 若自行返回 False、抛错或不响应取消，Jiuwenswarm 不能承诺资源全部关闭；
此时会报告失败并保留不可复用状态，不再伪报成功或异步跨任务补关。
模型断流、专家团长耗时未被该证据链证明与本取消忙循环直接相关。
