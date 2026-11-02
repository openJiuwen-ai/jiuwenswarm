# 请求上下文与工具支持

## 元信息

| 项 | 值 |
|---|---|
| 类型 | spec |
| 编号 / slug | S_13 / request-context-and-tools |
| 关联模块 | jiuwenswarm/common/{request_ext, request_identity, chat_final, tool_display, tool_ownership, coding_memory_paths}.py |
| 最近一次修订日期 | 2026-10-13 |
| 关联 feature | 暂无 |

## 范围 / 边界

本规约管 **Web 请求的身份与扩展上下文、对话终态注解、工具调用的人类可读支持**：路由
身份四字段、ext 白名单透传链、chat.final 协议注解、工具展示名、工具归属注册、Coding
Memory 路径与迁移。E2A 层对路由身份的转换消费归 [[S_02]]；`Message.metadata` 的整体
结构归 [[S_03]]。

## 不变量

- **N1 路由身份四字段唯一落点**：`user_id` / `group_id` / `bot_id` / `gateway_id`——
  **`user_id` 只在 metadata 顶层，路由三字段只在 `metadata.routing`**，不摊平、不进
  业务 params。**写一次（Gateway 入口 `apply_routing_metadata`）、读多处**；列表值取
  首元素、strip；`merge_routing_into_params` 仅限 Gateway 本地 handler（cron.*）做调用
  副本不回写。
- **N2 ext 白名单链单向**：Web query/header → `Message.metadata['ext']` → ContextVar →
  Agent rail `get_ext()`。白名单由 env（Swarm/Claw 双名）或运行时覆盖声明，优先级
  override > env > 空（关闭）。内部头 `X-Jiuwenswarm-Request-Ext` 为 Base64URL(JSON)，
  **仅传输编码、非鉴权**；入站抽取时**禁止自封装**（防客户端自称内部调用）。
- **N3 ext 限额**：JSON 4 KiB / 编码 6 KiB，超限抛 `RequestExtCodecError`（这是本域
  少见的 fail-closed——超限 ext 宁可拒不可截断，截断会产生语义损坏的透传）。
- **N4 final_mode 注解不覆盖**：`annotate_chat_final` 已有显式 final_mode 不覆盖；默认
  `patch_segment`；空回复填充的窄门——非空不覆盖、本段已流出可见 chat.delta 不填
  （空 final 仅是去重尾标记）、**有 reasoning 才填**固定文案（绝不提升思维链），中英
  双语。
- **N5 call_goal 优先 + display_name 禁触**：工具展示名优先取模型随 tool_call 附带的
  `call_goal`（执行前从 arguments 剥离防 schema 校验失败、截断 200 字符）；规则兜底
  = 动词表 + 参数键名启发。**绝不触碰 `display_name`**——team 工具业务字段，撞名会
  误剥参数持续报错。
- **N6 工具归属唯一事实源**：`tool_ownership` 是工具实例进程级注册归属的权威（agent
  独占 vs 全 agent 共享），对齐 openjiuwen `ToolCard.stateless`；`stateless` 默认
  False = agent-owned，id 限定 `{name}_{owner}` 防并发覆盖且可随 teardown 回收；共享
  注册幂等；`threading.Lock` 双检 + "already exist" 视为成功。
- **N7 Coding Memory 迁移 fail-open 尽力**：legacy 源只读、不复制 DB / 缓存；
  portalocker 1s 超时锁防并发；SHA256 内容指纹去重 / 冲突重命名（`__legacy_<hash>`）；
  state token 快速路径跳过重复迁移；MEMORY.md 索引上限 200 行；全部原子写
  （tempfile + fsync + os.replace）；symlink 源与目标一律跳过。

## 接口契约

| 模块 | 关键符号 | 语义要点 |
|---|---|---|
| `request_identity.py` | `normalize_routing_identity` / `apply_routing_metadata` / `web_routing_identity` / `merge_routing_into_params` / `WEB_ROUTING_ID_FIELDS` | N1 |
| `request_ext.py` | `build_ext_from_source` / `encode/decode_internal_header` / `set_current` / `lift_from_metadata` / `get_ext` / `set_forward_headers` | N2 / N3 |
| `chat_final.py` | `annotate_chat_final` / `ensure_final_mode_inplace` / `fill_reasoning_only_empty_final_content` / `FINAL_MODE_*` | N4 |
| `tool_display.py` | `build_tool_display_name` / `extract_call_goal` / `inject_call_goal_schema` / `_VERB_BY_TOOL` | N5 |
| `tool_ownership.py` | `register_tool` / `unregister_tool` / `mark_stateless` / `ensure_tool_registered` / `qualify_tool_id` | N6 |
| `coding_memory_paths.py` | `prepare_project_coding_memory_dir` / `resolve_project_coding_memory_dir` / `resolve_coding_memory_project_name` / `CodingMemoryMigrationResult` | N7 |

## 执行路径 / 消费者

- request_identity（24 处）：`gateway/channel_manager/web`、`message_handler`、
  `server/agent_http_*`、`handlers/bootstrap`、`routing/agent_rest_map`、企业包。
- request_ext：`ws_transport`、`http_dispatch`、`agent_rest_map`、`server/pipeline`、
  `agent_http_server/routes`。
- chat_final：`interface` / `interface_deep`。
- tool_display：`task_execution_rail`、`stream_event_rail`、`skill_selection`。
- tool_ownership：`interface_deep`、`interface_code`、`agents/swarm/providers`、
  `agents/harness/common/tools`。
- coding_memory_paths：`interface_code`、`memory_rpc`、`auto_memory/extraction_runner`。

## 边界 case 清单

| 场景 | 现行为 | 测试 |
|---|---|---|
| 路由字段出现在业务 params | 违反 N1（仅 merge_into_params 副本例外） | `test_request_identity.py` |
| 客户端伪造 `X-Jiuwenswarm-Request-Ext` 自称内部 | 入站抽取禁止自封装 | ext 测试 |
| ext 超 4 KiB | 抛 RequestExtCodecError | ext 测试 |
| 空 final 但本段已有 chat.delta | 不填兜底文案 | chat_final 测试 |
| 工具名与 display_name 业务字段撞名 | 不触碰（N5） | tool_display 测试 |
| 并发注册同名工具 | `{name}_{owner}` 隔离 / 共享幂等 | tool_ownership 测试 |
| 迁移中断 / 重复触发 | state token + 原子写 + 指纹去重 | `test_coding_memory_paths.py` |

## 修改检查单

- 改路由身份落点 → N1 双层结构（顶层 user_id / metadata.routing 三字段）全仓 grep
  24 处消费点 + [[S_02]] agent_compat web 方向同步。
- 扩 ext 白名单 → override > env > 空 优先级回归 + 双名（Swarm/Claw）env 同步。
- 改 final_mode 枚举 → 前端折叠逻辑联动（这是给前端免启发式的协议契约）。
- 改工具归属语义 → stateless 对齐 openjiuwen ToolCard + teardown 回收路径回归。
- 改迁移逻辑 → 只读源 / 锁 / 指纹 / 原子写四要素回归。

## 与其它 spec 的关系

- [[S_02]]：agent_compat 消费 web_routing_identity（入站装配）。
- [[S_03]]：`Message.metadata` 的整体结构与 [[S_08]] context_keys 的共享键。
