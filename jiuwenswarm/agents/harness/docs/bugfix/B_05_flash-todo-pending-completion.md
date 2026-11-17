# B_05 — Flash Todo 参数恢复与 pending-only 收尾

## 元信息

| 项 | 值 |
| --- | --- |
| 类型 | bugfix |
| 编号 / slug | B_05 / flash-todo-pending-completion |
| 修复日期 | 2026-11-06 |
| 关联模块 | `flash/tools/flash_todo.py`、`flash/rails/flash_todo_rail.py` |
| 关联 spec | [S_03](../specs/S_03_todo-close-flow-control.md)、[S_05](../specs/S_05_profile-todo-variants.md) |
| 源码基线 | 官方 `dev-stable` `be550b2fba`，修复分支 `fix/flash-todo-premature-finish` |

## 现象与原因

Windows 1.6.0 现场创建 15 页 PPT 空壳后结束。模型以顶层 id/status 更新 env，因缺 todos 失败；之后没有先完成 env 就启动 build，两次违反单个进行中约束。依次完成 env/outline/style 后仅剩 pending 项，原催办只关注进行中项，最后“继续生成”的纯文本被按回答结束。

该记录解释了失败链，尚未确定模型为何最后未调用工具，也未确认具体回归 PR。不能把请求正常结束当作 PPT 完成交付。

## 修复与取舍

- 明确数组参数及正确示例；冲突时保留原错误并补充可读取的进行中 ID。非法更新不写文件。
- 本轮成功操作后，跟踪 pending/in_progress 执行项，持久提醒不进入续接集合。复用原一次催办及流控，提示实际执行或说明阻塞；失败操作、单纯查询和历史残留不会新启动催办。
- 尊重结构化中断、异常、force-finish、截断和轮数预算。仍结束时补充待办尚未确认完成的说明，不修改 Todo 或推定产物是否合格。
- 沿用现有接口和事件，普通 TaskExecutionRail 不变。本修复无需原 scope 分支中的 Core 默认创建及只读兼容改动。

未采用放宽状态校验、自动完成或取消清单，以及无限催办：它们不能证明业务已经完成，也会改变用户对任务的控制。

## 验证与限制

Python 3.13.15、WSL/Linux；从 Git 临时导出 `uv.lock` 锁定的官方 Core `6db33ad057`，配合新分支源码运行。Flash 工具面、Todo 集成、收尾、文本流控、拒答、普通收尾及权限投影/生命周期组合共 **173 通过**，不依赖相邻未合入 Core 源码。

用例包括错误参数不调用引擎、状态冲突不写文件、合法批量切换、pending-only 一次续接、预算耗尽说明、成功完成后不附加说明、旧任务/提醒排除以及中断和取消退出。

早先使用相邻较新 Core `9335ac8a3` 及临时安全依赖运行扩大组合，发生权限适配失败（74 失败、108 通过）；该组合不是锁定安装版本，不据此修改本次无关的权限模块。

以上为本地工具与钩子测试，没有重跑 Windows、真实模型、正式包及 CI。自然语言等待原因依赖模型明确表达；一次催办不能保证后续业务必然成功。
