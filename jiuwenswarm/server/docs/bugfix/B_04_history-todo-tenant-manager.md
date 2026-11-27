# 历史 Todo 读取误用默认管理器

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-11-11 |
| 范围 | 历史 Todo handler、管理器路由回归 |
| 关联 spec | [S_02 request-pipeline-dispatch](../specs/S_02_request-pipeline-dispatch.md) |

## 现象与根因

OfficeClaw 聊天正常、Todo 文件存在时，history.get 仍可能只返回对话历史。历史入口取得服务端默认 AgentManager，仅在该对象本身是 TenantAgentPool 时才解析租户；实际服务通过独立的 tenant_pool() 提供租户池，因而该分支不执行。读取落到 agent_default 而非聊天所属的 agent_office，随后默认模型配置校验失败，handler省略Todo快照。

原租户单测把 agent_manager 直接替换为租户池，未模拟真实服务门面，遗漏了错误路由。

## 修复与验证

复用聊天的 `_uses_tenant_pool(request)` 判定：租户请求经 tenant_pool() 按 agent/service/workspace 获取管理器；普通请求保留默认管理器。租户出错时保持历史消息可读，不回退到默认管理器或宿主旧副本。后端读取、增量/CAS语义、事件和默认legacy字段不变。

回归使用实际 AgentServerServices 门面，覆盖OfficeClaw、非默认agent/service、普通默认请求及租户失败不回退。修复前4失败/1通过；修复后历史、快照及租户写入相关40项通过。宿主已有有效租户配置后，实际AgentServer进程重启的冷态、暖态、空清单和文件缺失读取均通过；运行验证采用隔离文件数据，不调用模型生成。证据存放于项目Checkpoint的tools_failure_analysis/validation_20261110/fix-implementation/live-20261111/correction。本地运行结果不代表远端沙箱或正式制品验收。

## 2026-11-12：历史读取补齐空闲回收

read_todo_snapshot 会为冷会话创建并缓存适配器，但此前读取结束没有触发既有空闲回收。连续浏览历史而不聊天时，过期适配器会继续保留。现以 try/finally 覆盖取得适配器及后续读取，复用 _evict_idle_session_adapters；不修改 TTL、批次上限或活跃/审批/子代理/会话锁保护。

回归保留真实读取入口、创建缓存与回收逻辑，仅替代重量级实例初始化、后端返回和磁盘上下文恢复。十项新增用例在修改前均复现过期适配器残留；修改后历史与会话回收两文件共49项通过。覆盖正常结果、无工具、无快照、非权威快照、读取异常/取消，以及保护中的会话保留。本轮未运行实例、模型或远程CI；证据在 Checkpoint 的 tools_failure_analysis/validation_20261110/fix-implementation/review-rnd2/jiuwen-history-idle。

## 2026-11-17：冷恢复模式与缓存参数对齐

旧会话只有 mode=agent/flash、work_mode=work 时，历史入口误把 work 当成实现名而提前退出；即使模式可用，冷创建漏传 session_id/sub_mode 也会与聊天使用不同的缓存键。

历史入口改为在请求副本上复用聊天的模式解析，保留 Flash 亲和与显式 Code 规则，并传入完整会话缓存参数。不调用聊天准备函数，因为读取历史不应改请求、同步元数据或启动任务；不改 AgentManager 缓存算法，也不把读取失败降级为宿主旧副本。

测试覆盖真实旧 metadata 文件、Flash 开关/亲和、Code/Plan、未知模式，以及受控实例创建下真实管理器的“历史→聊天→历史”和 generation 推进。相关 11 个测试文件共 357 项通过；首次测试误把非 Plan 渠道当成可恢复 Plan，已修正为 Web 渠道，未放宽产品 Plan 门控。该结果为本地定向回归，不代表浏览器、Windows 或远端验收。逐项改动与原始日志见工程根 `Debug/FEATBIND-flash-todo-update/iteration-2/`。

## 2026-11-18：未知模式、Team 与 OfficeClaw 冷恢复

业务 mode 的 unknown 是已有元数据占位值，不应遮蔽有效 adapter_mode；空白也先归一化再回退。普通 Team 的 manager mode=team 原本可由工厂映射为 DeepAdapter，历史入口允许列表遗漏 team 导致读取提前结束。现保留 team 模式及其缓存身份，不按建议降级为 agent，避免与聊天另建宿主。

OfficeClaw 显式 agent 的聊天请求会关闭 Flash 默认注入，但 history.get 方法不在该策略的适用集合中。冷恢复现仅在请求副本上以 CHAT_SEND 身份调用已有纯路由解析器；原历史请求、元数据与执行流程均不变。相比对所有渠道永久锁定 adapter_mode=agent，此方式继续复用 Web/TUI 及后台来源原有默认规则。

修复前定向用例10失败/28通过；修复后历史、OfficeClaw 路由、快照、事件及相关 Todo 回归12文件共422通过。测试包含真实 metadata 文件、真实管理器缓存路由和当前 generation 投影，实例创建/后端使用受控替身；未进行浏览器、远端沙箱或全量CI验收。新合并上游对应的 Core 与安全仓源码按本轮锁重新选择，未沿用前轮依赖环境结论；证据见 iteration-2/review/rnd1 的20261118日志及环境记录。

## 2026-11-18：Plan 上一模式与 AutoHarness 映射

OfficeClaw Plan 冷恢复此前只补聊天的方法身份，未像聊天准备一样补持久化的上一模式，因此已进入的 Plan 仍会被门控降级，再被普通会话亲和选回 Flash。这是原有缺陷的修复遗漏。现仅在解析副本中传入 metadata.mode 对应的既有内部字段，保持原 Plan 门控，不另造准入状态。

AutoHarness 的业务模式需要像聊天及租户查找一样映射到 agent 管理器；历史入口遗漏映射后被允许列表拒绝，属于本PR引入的回归。现仅补这一已有映射，不新增适配器种类。

缓存一致性测试改为串联真实 `_prepare_code_mode_chat_turn`，不再直接传预设聊天模式。覆盖冷历史→聊天准备→generation推进→历史、有/无项目目录、legacy/versioned，并保留其他模式对照与原请求不变断言。修复前10失败/45通过；修复后8个相关文件385通过。后端返回、重量级实例创建及预热/借用为受控替身，聊天准备使用只读元数据路径，不调用模型。未做浏览器、Windows、远端沙箱或全量CI验收。原始结果维护在工程根iteration-2/review/rnd2。
