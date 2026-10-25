# 模型保存与后台发布刷新规约

## 元信息

| 项 | 值 |
| --- | --- |
| 编号 / slug | S_03 / model-refresh |
| 修订日期 | 2026-10-23 |
| 关联模块 | AgentServer 模型配置保存、JiuwenMateWebAdapter、Gateway Web 代理、模型设置页面 |
| 关联 feature | [F_02](../features/F_02_colleague-management.md) |

## 1. 范围与适用条件

适用于本地命名空间的 Persistent 同事模型热更新。复用既有配置持久化与个人版运行时重载，不改变个人版语义、草稿显式发布边界或执行中的 Request 版本。此处状态不是用户任务状态，也不是模型连通性检查。

## 2. 数据与接口契约

模型保存先持久化配置并执行原有运行时重载，再安排 Persistent 后台刷新；保存响应不等待同事全量重发布。`applied_without_restart` 只表示既有重载接受结果，不能证明 Persistent 同事全部生效。显式 `agent.reload_config` 的成功载荷附带 `persistent_model_refresh`，结构与下述查询一致；非模型 scope 不触发刷新。

`models.refresh_status` 为只读 Web RPC，请求参数 `{}`。Gateway 在共享目录和远程部署下都代理至持有队列的 AgentServer Worker；不可用时沿用代理的 `SERVICE_UNAVAILABLE` 错误，不从本地配置推断状态。查询不初始化数据库、同事或执行运行时。

| 成功载荷字段 | 类型与约束 | 含义 |
| --- | --- | --- |
| `status` | `pending`、`applied`、`failed` | 最新一代刷新未结束、已应用或未全部应用 |
| `generation` | 非负整数，进程内递增 | 用于区分连续通知；不是配置版本，不能跨 Worker 重启比较 |
| `detail` | 可选字符串 | 安全说明，不含配置、凭据或原始异常文本 |

首次尚未通知时返回 `applied / 0`，仅表示没有待处理通知，不证明所有存量快照已经复核。页面不为该初始值展示“全部已生效”。状态为 Worker 内存事实，不写入 `agents.sqlite3`。

## 3. 不变量与生命周期

1. 同一队列串行刷新；保存配置在入队前复制。运行中的一代完成后，只处理最新待处理配置，中间通知允许合并；旧一代不能覆盖最新一代状态。不含 `models` 的后续通知沿用最近一次明确提供的模型配置，不能丢弃尚未应用或失败的保存快照；只有新的模型配置才能替换它。
2. 有未发布修改的同事不自动发布。部分同事失败时保留原版本并继续其他同事，最新状态为 `failed`；不撤销已保存配置，也不把部分失败转换成保存失败或网关自动重启。
3. 新 Request 在后台刷新生效后读取新版本；刷新期间可读取尚未更新的版本。已受理的 Request 仍固定原版本。不同同事的发布不是全局事务。
4. 发布不准备或下载 tokenizer；可保留离线可选元数据，由 agent-core 在缺少本地 tokenizer 时使用其计数回退。不改 core 的容量、80% 换窗触发或新窗口 40% 上限。
5. 宿主正常关闭时等待已排队刷新结束，避免取消 await 后线程仍写库；适配器再次使用时允许继续刷新。进程强制终止不恢复内存通知，已有启动刷新从持久化模型配置重新生成快照。

合法状态转移：初始 `applied / 0`；新通知使任意状态变为新一代 `pending`；仅当前代完成可变为 `applied` 或 `failed`。下一次保存可从失败重新入队，不设置无限自动重试。

## 4. 页面与兼容边界

保存弹窗不等待刷新查询。自定义模型、模型组和免费模型设置成功保存后都通知页面重新查询；拒绝保存或无修改关闭不产生通知。页面独立查询并在 `pending` 时轮询；超时或断连不能显示为已应用，重连后重新查询；卸载或新查询使旧响应失效。失败保留独立状态提示，不掩盖配置保存结果。

新版前端连接没有此 RPC 的旧 Worker 时，显示状态不可用，不回退为“全部已应用”。旧版前端仍可消费原有保存字段，但不能据此判断后台完成；升级前后端应配套。此规约不提供多 Worker 的全局刷新一致性或实时生效时限。

## 5. 验证索引

- 保存回执、串行合并、代次、状态代理与关闭：`tests/unit_tests/server/jiuwenmate/test_model_refresh.py`。
- 真实 SQLite 发布、草稿及配置来源：`tests/unit_tests/server/test_config_adapter_permission_reload.py`、`test_persistent_agent_service.py`。
- 页面轮询与失败提示：前端 `tests/modelRefreshStatus.test.mjs`。
- 命令、执行结果及未验证范围统一见 [F_02 第 8 节](../features/F_02_colleague-management.md#8-验证设计执行记录与结论)。
