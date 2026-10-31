# 七项会话查询与查询拦截示例

将本目录作为已有后端扩展包安装。包不创建 EventBus、Builder 或 SessionContext，也不改平台 Adapter。

一轮结束时，现有平台通知发布 Token Weather 订阅的 `context.usage.updated` 并绑定可信会话。`on_usage` 调用 collect_session_parameters，七个 Ops 各自触发同名查询事件；本包的 `on_query` 观察调用前后并 `next`，平台 BUILTIN 计时、CORE 读取真实数据。收集函数返回 cwd/root/model/repo/messages/turns/usage 参数字典，再交给用户自己的 on_parameters；本例只记录不含消息正文的摘要，原通知继续。不调用 ui.render，也不修改 Token Weather 的渲染代码。

| 方法 | 用户拿到什么 | 应该怎样消费 |
| --- | --- | --- |
| cwd / root | 绝对路径字符串或 None | None 是未提供，不拼成另一个目录 |
| model | 模型名字符串或 None | 直接显示；不再读取 model_name/source/activated |
| repo | root/remote/internal/name 对象或 None | root 为主工作树根；不再读取 branch/head |
| messages | 最近最多 4096 项的 user/assistant 消息数组，从旧到新 | 使用 text/toolUses/toolResults；只看最近五条用 messages[-5:] |
| turns | 原生用户输入轮次或 None | 0 与未知 None 不同 |
| usage | context/rateLimits/可选 cost | 缺失数字就是未知；不把未知费用写成 0 |

事件 Handler 收到无参数 `{}`（usage 可含 breakdown/columns），`next` 返回 `{"value": ...}` 或 `{"deny": "原因"}`。普通 Ops 调用者只得到解包后的值。拒绝需捕获 `SessionQueryDenied`；查询 Hook 不应再次调用同名 Ops 获取默认值。

`current_query_origin()` 是 Server 提供的只读来源辅助函数；其中 package 是发起查询的包，不是当前 Handler 所属包。当前 Core 未提供 Claude 的 next.origin / 跳过调用 Hook，本实现以查询调用域拒绝循环。

详细字段、拒绝与改写示例见 [查询公共机制与特性索引](../../docs/designs/session-query-events.md) 及其链接的专项特性文档。
