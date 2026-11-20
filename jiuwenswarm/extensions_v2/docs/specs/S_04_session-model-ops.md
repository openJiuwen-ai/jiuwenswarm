# session.model / ops.session.model 规约

最近修订 2026-10-31。七项查询公共机制见 [S_08](S_08_session-query-events.md)，使用说明见 [F_14](../features/F_14_session-model.md)。本规约维护字段和默认语义，特性文档解释填写方式。

## 1. 能力与默认来源

| Ops / Event | 默认来源 | 返回类型 |
| --- | --- | --- |
| session.model | 绑定 session_model_info 经公开字段校验后取 model_name；否则 get_session_model_info(session_id)。 | str 或 None |

## 2. 输入输出字段

### session.model：输入

| 字段 | 类型 / 默认 | 谁填写 | 含义与边界 | 怎样填 / 例子 |
| --- | --- | --- | --- | --- |
| 方法参数 | 无 | 调用者不填写 | 查询已绑定会话，不接受 session_id、路径、模型名或包 ID | 直接调用 await ops.session.<方法>() |
| Event.event_input | 空映射 {} | Ops 代理自动构造 | 查询 Handler 收到空参数对象；不是 SessionContext，也不携带可改写目标身份 | 注册时使用 match_pattern={} |

### session.model：返回值字段

| 字段 | 类型 / 出现条件 | 谁提供 | 准确含义 | 消费方式 / 例子 |
| --- | --- | --- | --- | --- |
| 方法返回 / value | str 或 None | 由平台 CORE 或查询 Handler 返回 | 模型名称字符串；只有选择 ID 而没有实际名称时返回 None，不把路由 ID 当模型名 | example-model；不是模型详情字典 |
| 不再返回的旧键 | selection/model_name/provider/source/activated | 调用者删除旧访问代码 | 这些是平台内部模型事实或保存选择结构，不再是公开 model() 的返回字段 | 把 info['model_name'] 改成 model_name = await ops.session.model() |

## 3. 事件、可信绑定与错误

每个公开 Ops 触发同名查询 Event。USER 返回 value/deny 封装，Ops 解包；默认 CORE 调用原始服务，不经公共 Ops 再次发起同名查询。调用者不填 session_id，不从返回视图决定平台真实身份或授权根。缺失值、真实零值、读取故障不同；拒绝和重入见 S_08。查询结果不写平台数据、不自动渲染。

## 4. 兼容迁移与差异

| 方法 | 迁移 | Claude 对照差异 |
| --- | --- | --- |
| model | 旧 info['model_name'] / info.get('provider') 等访问改为直接消费字符串；需要模型配置详情的业务应使用自己已有的配置能力，不从名称反推供应商、凭据或激活状态。 | 返回主体对齐 Claude 字符串；本地缺失事实保留 None。保存名称不保证此刻已经激活，运行快照仍优先于保存信息。 |
