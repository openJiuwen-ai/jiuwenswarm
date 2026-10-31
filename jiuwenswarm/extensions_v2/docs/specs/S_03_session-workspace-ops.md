# 会话工作区 Ops 规约

| 项 | 值 |
| --- | --- |
| 编号 | S_03 / session-workspace-ops |
| 最近修订日期 | 2026-10-29 |
| 主责模块 | `jiuwenswarm/extensions_v2/handler_ops/session_ops.py` |
| 关联特性 | [F_13 会话工作区查询能力](../features/F_13_session-workspace-ops.md) |

## 1. 范围与前提

本规约定义 Worker 内扩展 Handler 的两项只读能力。宿主在现有 `Registry.init` 的注册回调中登记 session Ops Build Handler，由 Core 触发唯一一次 `ops.build`。它们不是业务事件，也不会触发事件链。

宿主对当前会话的授权和目录归属负责。业务入口构造的 `SessionContext` 是本次调用的事实快照；Bus 负责规范化、冻结和按 Handler 绑定。扩展不能通过事件输入中的路径改变这两项能力的读取目标。

## 2. 数据与返回规则

| 能力 | 事实来源 | 返回值 |
| --- | --- | --- |
| `await ops.session.cwd()` | 当次 `SessionContext.cwd` | 当前会话执行工作目录的绝对路径字符串；缺失时为 `None` |
| `await ops.session.root()` | 当次 `SessionContext.metadata["project_root"]` | 会话已绑定项目根目录的绝对路径字符串；没有绑定时为 `None` |

cwd 可以是项目根下的子目录，也可以是无项目会话的任务工作目录。root 表达项目身份，不自动等于 cwd、内部 Agent workspace、Git 仓库根或临时任务目录。

能力签名不接收会话 ID 或目录参数。读取不会解析事件输入、读取进程当前目录、创建目录、切换目录、启动会话、访问 Git 或调用模型；不检查目录在读取时是否仍然存在。

## 3. 错误与隔离

- 没有 `SessionContext` 时抛出 `RuntimeError`，区分缺失会话与该会话缺少目录资料。
- 能力函数自身的校验为：目录值为 `None` 时返回 `None`；非字符串抛出 `TypeError`；空字符串或相对路径抛出 `ValueError`。经 Bus 调用时，不合法的上下文也可能先被 Core 的输入校验拒绝。
- 路径必须采用宿主平台的绝对路径语法。宿主在事件边界提供规范化路径，Ops 不展开 `~` 或依赖当前目录拼接相对路径。
- 目录来自单次调用的不可变上下文，不缓存 Ops、SessionContext 或会话路径。并发和嵌套事件分别绑定自己的上下文；异常传播遵循 Core 的 Handler 层级规则。

## 4. 当前业务接入与兼容

`notify_context_usage(update, *, cwd=None, project_root=None)` 在调度前创建 `SessionContext`，将项目根写入约定的 metadata 字段。不提供新参数的调用保持原有行为，两个目录值缺失。

原生单 Agent 的回合结束入口从 Core 当前任务的 CWD 状态和已绑定项目字段提供上述参数，事件名及 `ContextUsageUpdate` 不变。其它仅传入会话 ID 的业务入口仍返回缺失值，不进行隐式目录发现。

## 5. 用户接入

用户扩展按现有 manifest 和 registrar 登记普通 Handler，在 Handler 内等待两个 Ops，并继续原有事件链：

```python
async def on_usage(event, ops, next_handler):
    cwd = await ops.session.cwd()
    project_root = await ops.session.root()
    # Use the invocation's workspace facts; None means unavailable.
    return await next_handler(event)
```

用户不登记这两个 Ops，也不需要为只读查询登记新的 Event。用户选择订阅已有事件，实际触发仍由宿主业务入口负责。

## 6. 验证索引

验收场景和实际执行记录见 [F_13 第 6 节](../features/F_13_session-workspace-ops.md#6-验收与未决问题)。

## 字段填写与文档同步（2026-10-29）

cwd/root 都无参数，返回整体为 str/None，不是包含路径字段的包装字典；调用方不填写路径或目标会话。路径校验不代表目录存在，不展开相对路径。逐字段来源、默认、示例、错误与完整用户包见 F_13 第 4、5 节。原第 5 节验证锚点保留，当前历史记录归入新第 6 节。

本轮仅文档，不改变业务代码，未重跑历史业务验证。完整说明按七段式编写指南维护，历史验收保留原日期和范围。
