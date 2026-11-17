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
