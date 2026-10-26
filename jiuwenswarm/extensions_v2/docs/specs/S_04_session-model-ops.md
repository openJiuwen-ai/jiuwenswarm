# 会话主模型 Ops 规约

| 项 | 值 |
| --- | --- |
| 编号 | S_04 / session-model-ops |
| 最近修订日期 | 2026-10-26 |
| 主责模块 | `handler_ops/session_ops.py` 与 `server/runtime/session/model_selection_store.py` |
| 关联特性 | [F_14](../features/F_14_session-model-ops.md) |

## 1. 接口

`await ops.session.model() -> dict[str, object] | None`，不接受会话 ID、模型 ID 或路径参数。目标仅来自宿主绑定的 `SessionContext.session_id`。

| 字段 | 含义 |
| --- | --- |
| `selection` | 保存的 `type`、`id`、可选 `route_id`；运行快照不混入旧保存选择，值为 None |
| `model_name` | 运行模型配置名称，或会话保存的旧版模型名；稳定选择未解析时可为 None |
| `provider` | 运行模型提供方；持久化选择没有已确认提供方时为 None |
| `source` | `runtime` 表示当次运行快照，`session` 表示会话保存记录，不是凭据或模型目录来源 |
| `activated` | 运行快照为 True；仅有保存记录时为 None，表示未知，而非已确认未激活 |

稳定模型组选择保留其组与 route 标识，不创建路由器或猜测实际物理模型。运行模型名称也不保证是模型组最后一次实际路由成员。

## 2. 不变量

- SM-01：优先读取 `SessionContext.metadata["session_model_info"]`，缺失时才在线程中读取保存记录。忽略事件输入中的模型、路径和会话 ID。
- SM-02：新方法加入已有 session Build Handler，不重复新增 session 命名空间，不新增同名查询事件。
- SM-03：公开字段采用白名单，返回全新的普通字典，不能修改调用上下文或保存记录。不能泄露 API Key、URL、Header、请求参数或可变模型对象。
- SM-04：原生入口在模型应用后立即采集局部摘要，发布函数在创建任务前再次复制。并发与嵌套调用不共享可变摘要。
- SM-05：查询不创建或切换模型、不调用模型、不写 metadata、不读全局默认配置来补造会话事实。

## 3. 缺失与错误

没有 SessionContext 时抛出 RuntimeError。会话无保存模型且无运行快照时返回 None。非法会话 ID 通过现有路径验证拒绝，损坏 metadata 或选择格式失败沿现有 ModelSelectionError 传播。非法宿主模型摘要按 TypeError / ValueError 拒绝，不把输入错误解释为真实缺失。

异常经 USER Handler 调用后的处理遵循 Core EventBus 故障隔离规则；上述错误描述能力函数自身的行为。

## 4. 业务接入与验证

原生回合复用 `context.usage.updated` 作为运行快照接入点，沿用原事件 CORE。模型查询不依赖该 CORE，也不通过该 CORE 执行。验证记录和使用示例见 F_14 与 `examples/session_model_info/`。
