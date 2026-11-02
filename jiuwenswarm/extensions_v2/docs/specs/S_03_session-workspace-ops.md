# session.cwd / session.root 与会话工作区 Ops 规约

最近修订 2026-10-31。七项查询公共机制见 [S_08](S_08_session-query-events.md)，cwd 使用说明见 [F_13](../features/F_13_session-cwd.md)，root 使用说明见 [F_19](../features/F_19_session-root.md)。本规约维护字段和默认语义，特性文档解释填写方式。

## 1. 能力与默认来源

| Ops / Event | 默认来源 | 返回类型 |
| --- | --- | --- |
| session.cwd | SessionContext.cwd；经本机绝对路径校验。 | str 或 None |
| session.root | SessionContext.metadata['project_root']；未知为 None。 | str 或 None |

## 2. 输入输出字段

### session.cwd：输入

| 字段 | 类型 / 默认 | 谁填写 | 含义与边界 | 怎样填 / 例子 |
| --- | --- | --- | --- | --- |
| 方法参数 | 无 | 调用者不填写 | 查询已绑定会话，不接受 session_id、路径、模型名或包 ID | 直接调用 await ops.session.<方法>() |
| Event.event_input | 空映射 {} | Ops 代理自动构造 | 查询 Handler 收到空参数对象；不是 SessionContext，也不携带可改写目标身份 | 注册时使用 match_pattern={} |

### session.cwd：返回值字段

| 字段 | 类型 / 出现条件 | 谁提供 | 准确含义 | 消费方式 / 例子 |
| --- | --- | --- | --- | --- |
| 方法返回 / value | str 或 None | 由平台 CORE 或查询 Handler 返回 | 执行目录绝对路径；None 表示这次绑定没有目录资料，不代表磁盘路径不存在 | D:/projects/demo/src；不是 {'cwd': ...} |

### session.root：输入

| 字段 | 类型 / 默认 | 谁填写 | 含义与边界 | 怎样填 / 例子 |
| --- | --- | --- | --- | --- |
| 方法参数 | 无 | 调用者不填写 | 查询已绑定会话，不接受 session_id、路径、模型名或包 ID | 直接调用 await ops.session.<方法>() |
| Event.event_input | 空映射 {} | Ops 代理自动构造 | 查询 Handler 收到空参数对象；不是 SessionContext，也不携带可改写目标身份 | 注册时使用 match_pattern={} |

### session.root：返回值字段

| 字段 | 类型 / 出现条件 | 谁提供 | 准确含义 | 消费方式 / 例子 |
| --- | --- | --- | --- | --- |
| 方法返回 / value | str 或 None | 由平台 CORE 或查询 Handler 返回 | 用户项目绑定的绝对根目录；无绑定为 None，不用 cwd 或 Git 根兜底 | D:/projects/demo；cwd 可以是 demo/src |

## 3. 事件、可信绑定与错误

每个公开 Ops 触发同名查询 Event。USER 返回 value/deny 封装，Ops 解包；默认 CORE 调用原始服务，不经公共 Ops 再次发起同名查询。调用者不填 session_id，不从返回视图决定平台真实身份或授权根。缺失值、真实零值、读取故障不同；拒绝和重入见 S_08。查询结果不写平台数据、不自动渲染。

## 4. 兼容迁移与差异

| 方法 | 迁移 | Claude 对照差异 |
| --- | --- | --- |
| cwd | 方法返回类型保持；内部现在派发 session.cwd。用户 Handler 新增拦截后可改变本次返回视图，真实执行目录不变。 | Claude 返回 string；本地在平台没有目录资料时保留 None，不伪造当前进程目录。 |
| root | 方法返回类型保持；内部现在派发 session.root。改写查询值不会改变平台项目绑定或文件访问根。 | Claude 返回 string；本地无项目时保留 None。 |
