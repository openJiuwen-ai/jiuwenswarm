# 会话查询公共机制与七项特性索引

## 1. 范围、状态与依据

本分支已实现。修订日期 2026-10-31，维护者刘瑜，开发台账 EXTV2-22。基于已合入 Server c6a07f909 / Core bdde48a8，不依赖 Core !110 或 Server !99。仅调整已开放的 cwd、root、model、repo、messages、turns、usage 七项；不改 append、files/read_file、原 Token Weather 渲染或 Front/Supervisor。

用户确认以 Claude 返回格式为主，同步迁移所有本次涉及的分支案例。固定参考 Claude Code Mods 2.1.277，官方提交 80c6959d28c4c30ecd14fc1cdf172f19d9adad81。公共查询协议主责为 [S_16](../specs/S_16_session-query-events.md)，逐字段解释分别由下表七篇独立特性维护；不把运行 Claude 私有宿主作为已完成项。

交付 PR：[!100](https://gitcode.com/huenrui_/jiuwenswarm/merge_requests/100)，目标分支 rsi_native_agent_os；本 PR 不修改原 Token Weather 渲染代码。


本页由 PR !100 尚未合入的 F_27 公共说明移入 designs，保留共享实现、迁移和验收记录。F_27 编号已使用，不再复用。每个查询的使用契约、字段解释、注册示例和独立验收分别在以下七篇特性中维护；本页作为公共机制与导航。第 6 节保留事件化实现阶段的既有验证，本次文档拆分只检查内容、示例、链接和差异，不重新执行其中的业务测试。

| 唯一查询 Ops | 对应 Event | 独立特性文档 |
| --- | --- | --- |
| ops.session.cwd() | session.cwd | [F_13](../features/F_13_session-workspace-ops.md) |
| ops.session.root() | session.root | [F_28](../features/F_28_session-root-query.md) |
| ops.session.model() | session.model | [F_14](../features/F_14_session-model-ops.md) |
| ops.session.repo() | session.repo | [F_15](../features/F_15_session-repo-ops.md) |
| ops.session.messages() | session.messages | [F_23](../features/F_23_session-messages-ops.md) |
| ops.session.turns() | session.turns | [F_29](../features/F_29_session-turns-query.md) |
| ops.session.usage() | session.usage | [F_21](../features/F_21_session-usage-ops.md) |

## 2. 能力说明与 Claude 对照

公开 Ops 现在派发同名查询 Event，USER 可继续、增加前后操作、返回类型兼容的值或拒绝，BUILTIN 计时，CORE 读平台真实默认数据；Ops 验证并解包。只读描述默认数据访问不写业务数据，不排除可拦截查询。

| 方法 | 事件输入 | 公开返回 |
| --- | --- | --- |
| session.cwd | {}（无参数） | str 或 None |
| session.root | {}（无参数） | str 或 None |
| session.model | {}（无参数） | str 或 None |
| session.repo | {}（无参数） | dict 或 None |
| session.messages | {}（无参数） | list[dict] |
| session.turns | {}（无参数） | int 或 None |
| session.usage | 可选 breakdown/columns | dict |

model 对齐字符串；repo 对齐 root/remote/internal/name（linked worktree 返回主根）；messages 对齐 role/text/toolUses/toolResults 数组；usage 对齐 context/rateLimits/可选 cost。未知目录、模型与轮次保留 None；未知用量数字省略。没有构建内部仓库清单则 internal=False/name=None，没有费用账本则省略 cost。summary 只投影已观察分类，不编造终端网格或工具/记忆明细；full 默认明确报不可用，不悄悄发 Token 计数请求。

## 3. 事件契约

```text
示例入口：原生回合完成，平台收集可信会话资料
  → notify_context_usage(...) 安排 context.usage.updated
  → Worker EventBus 给实际 USER on_usage 绑定本次 Ops
  → USER 调用 ops.session.cwd() / ops.session.root() / ops.session.model() / ops.session.repo() / ops.session.messages() / ops.session.turns() / ops.session.usage()
  → 查询代理：验证参数与会话，固定原调用包，建立循环保护
  → 同一 Worker EventBus.trigger(同名查询 Event, 原可信 SessionContext)
  → 查询 USER：前置操作 → next_handler(event)
  → 查询 BUILTIN：计时 → next_handler(event)
  → 查询 CORE：直接读取平台事实或服务，不调用同名公共 Ops
  ← {'value': 查询结果} / {'deny': 原因} 逐层返回
  ← 代理验证、复制、解包；拒绝转 SessionQueryDenied
  ← 外层 USER 使用值；按原事件契约 next，继续原通知 CORE
```

这个入口只是现成示例，不是查询前置依赖。任何已由平台绑定相应会话的 Handler 都可调用；用户不用修改 Adapter、创建 EventBus 或先调用 notify 取得上下文。Front/Supervisor 不新增 EventBus。

| 字段 | 类型 | 出现条件 | 含义 | 调用方式 |
| --- | --- | --- | --- | --- |
| value | 按方法定义 | 成功时唯一顶层键 | 下游或 CORE 给出的查询值；合法 None 仍须包装成 {'value': None} | 普通 Ops 调用已解包，不读取 result['value'] |
| deny | str，去空白后非空，最多 512 字符 | 拒绝时唯一顶层键 | 明确拒绝原因；不能与 value 同时出现，也不靠 raise 来代替 | return {'deny': 'WORKSPACE_QUERY_DISABLED'} |

源自平台的会话与调用包通过原 Ops 绑定，参数不包含可改写的 session_id。current_query_origin() 是新增 Server 来源辅助函数，不是伪造 Core next.origin。首版按 S_16 防止同名或间接循环；不改 Core 私有注册表，不借公开白名单模拟跳过调用 Hook。普通异常隔离和明确 deny 分开；取消传播并清理调用域。

## 4. 方法契约

| 旧代码涉及的方法 | 迁移方式 |
| --- | --- |
| cwd | 方法返回类型保持；内部现在派发 session.cwd。用户 Handler 新增拦截后可改变本次返回视图，真实执行目录不变。 |
| root | 方法返回类型保持；内部现在派发 session.root。改写查询值不会改变平台项目绑定或文件访问根。 |
| model | 旧 info['model_name'] / info.get('provider') 等访问改为直接消费字符串；需要模型配置详情的业务应使用自己已有的配置能力，不从名称反推供应商、凭据或激活状态。 |
| repo | repo_root 改为 root；移除 branch/head/detached 访问。注意 linked worktree 的 root 现在对齐 Claude 的主工作树根；当前执行目录仍由 cwd() 给出。平台内部原 Git 身份读取服务保留。 |
| messages | 删除 limit/cursor 参数和 page 包装；page['messages'][0]['content'] 改为 messages[-1]['text']。只需少量条目时在返回数组切片；产品原分页服务保留在平台内部，不作为新的第八个 Ops 暴露。 |
| turns | 返回类型和默认口径保持；现在会派发 session.turns。不要再把累计收到的通知数当作权威轮次。 |
| usage | 删除 breakdown=True/False；无明细用 usage()，需要已有分类用 breakdown='summary'。旧 context.context_window.input_tokens 改成 context.tokens，limit_tokens 改成 window，occupancy_rate 改成 percent（百分比单位）。旧 turn 不再是公开结果，单轮消耗仍可从现有用量通知的输入字段获取。 |

调用者可从任何已绑定会话的业务 Handler 使用，不必须依赖用量通知。六项无参数，usage 可用参数对象或命名参数（两者不能混用）。返回值是独立视图，不修改平台真实状态、不缓存进 SessionContext、不自动触发界面更新。

## 5. 接入与兼容方案

### 5.1 实现职责

- event_types/session_queries.py：七项名称、输入/输出类型与验证、注册类型。
- handler_ops/session_queries.py：封闭方法代理；调用者不能覆盖 Bus 或 Event 类型。
- query_dispatch.py：可信会话和原包、调用域/循环保护、实际 trigger、验证/解包。
- event_handlers/session_queries.py：共享 BUILTIN 计时与唯一默认 CORE。
- server/runtime/session/query_views.py、query_repo.py、query_messages.py：真实数据读取和 Claude 字段投影。
- bootstrap.py：先类型，再 Handler/Builder；保留一个共享 Ops 目录。

原始平台分页、模型详情、Git 分支/HEAD 和用量存储服务保留，避免影响其他平台流程；这些不是七项公开返回。旧直接读取辅助函数未再登记到共享目录，不能据其存在判断 Ops 仍未事件化。新增查询定义模块不放 trigger；本次不迁移已有 notify 入口的位置。

### 5.2 用户侧返回参数案例

```python
parameters = await collect_session_parameters(ops)
# parameters 包含 cwd/root/model/repo/messages/turns/usage 七个真实返回值。
user_business_function(**parameters)
return await next_handler(event)  # 原用量通知仍按自己的回执协议继续
```

实际可安装包见 [session_query_audit](../../examples/session_query_audit)。其 on_usage 复用 Token Weather 订阅的 context.usage.updated；收集函数返回七个结果，由用户自己的 on_parameters 消费。没有把参数字典冒充用量通知 CORE 的 accepted 回执，也没有进入 ui.render。独立 model/repo/usage/history 示例同步采用新返回形态。

## 6. 验收与未决问题

| 本次实际验证 | 结果 | 范围 |
| --- | --- | --- |
| 扩展模块回归 | 632 项通过 | 生产装配、七项查询、包加载/卸载、原历史与既有扩展行为；源码使用已合入 Core bdde48a8 |
| 相关平台回归 | 140 项通过 | 原生用量入口、历史路径/写入/游标、模型选择迁移及运行配置 |
| 独立进程参数验收 | 通过，7 项返回值逐字段一致 | 真实 ZIP 包→平台 notify→context.usage.updated→七个 Ops/同名查询 Event→返回参数→卸载/释放 |
| 不进入渲染链 | ui.render 调用 0 次 | 验收中 render 一旦被调用即失败；原 Token Weather 渲染文件保持原样 |

验证命令（Python 环境中把本仓与已合入 Core 放入 PYTHONPATH；-o addopts= 时显式保留 asyncio auto）：

```text
python -m pytest -q -o addopts= --asyncio-mode=auto --no-cov tests/unit_tests/extensions_v2
python -m pytest -q -o addopts= --asyncio-mode=auto --no-cov tests/unit_tests/agentserver/test_context_usage.py tests/unit_tests/agentserver/test_session_history_path_safety.py tests/unit_tests/test_session_history_atomic_write.py tests/unit_tests/test_session_history_cursor.py tests/unit_tests/test_session_history_empty_filter.py tests/unit_tests/test_session_history_jsonl_read.py tests/unit_tests/test_session_history_paths.py tests/unit_tests/test_model_selection_migration.py tests/unit_tests/common/test_runtime_model_config.py
```

独立进程验收使用真实 ZIP 安装、真实 JSONL/Git、平台 notify 和实际 Registry/Bus/Builder；模型为受控的已绑定运行快照，没有新增模型 API 请求。返回参数逐字段核对后卸载包、关闭装配。ui.render 被设置为一旦进入就失败，最终调用 0 次。未执行的浏览器、Claude 宿主和 Front/Supervisor 跨进程验收不列为通过。

## 7. 参考资料

- [Claude OpEventOf](https://github.com/anthropics/claude-code/blob/80c6959d28c4c30ecd14fc1cdf172f19d9adad81/mods/types/claude-code.d.ts#L5529-L5624)
- [Claude Session API](https://github.com/anthropics/claude-code/blob/80c6959d28c4c30ecd14fc1cdf172f19d9adad81/mods/types/claude-code.d.ts#L2342-L2447)
- [Claude 查询输出](https://github.com/anthropics/claude-code/blob/80c6959d28c4c30ecd14fc1cdf172f19d9adad81/mods/types/claude-code.d.ts#L5832-L5867)
- [S_16](../specs/S_16_session-query-events.md)；七篇独立特性的准确入口见第 1 节索引。
