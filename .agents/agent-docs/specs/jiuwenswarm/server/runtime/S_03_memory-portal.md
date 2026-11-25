---
id: S_03_memory-portal
title: 记忆 Portal 页面开关契约
type: S
author: Wang Tao
author_email: zzyyww0808@gmail.com
modules: [jiuwenswarm/server/runtime, jiuwenswarm/agents/harness/common/memory, jiuwenswarm/common/config_panel, jiuwenswarm/channels/web/frontend, agent-gateway/agent_gateway/channel_manager/web]
files: [jiuwenswarm/server/runtime/memory_portal.py, jiuwenswarm/agents/harness/common/memory/portal_registry.py, jiuwenswarm/common/config_panel/memory_config.py]
---

# 记忆 Portal 页面开关契约

## 边界与请求

Swarm 提供 Portal 页面开关、独立记忆页面并获取授权链接。连接与身份沿用宿主配置。Memory 提供 Portal 页面、记忆业务、鉴权和模型配置管理。
Gateway 转发 Web 的 `memory.portal.open` 到目标 AgentServer；不开 Memory Runtime，不代理 iframe 内部 CRUD。
请求不接受前端自报的记忆身份覆盖；AgentServer 依据自身当前配置初始化和绑定 Provider。

响应为 `{status, mode, agent?, code?, url?}`：

- `ready`：返回本次签发的 HTTP(S) URL，仅供当前页面使用。
- `disabled`：Portal 页面开关关闭或未选择 JiuwenMemory。
- `unavailable`：依赖缺失、超时或链接获取失败；不返回异常正文或凭据。

后端打开请求统一使用 30 秒超时预算，包含等待锁、配置切换、Provider 初始化和链接签发；超时取消后执行必要的资源清理。
前端请求等待上限 45 秒。重连重新签发链接。
URL 不持久化到配置，不进入日志；HTTP 服务 API Key 不返回前端，设置页仅读写 Portal 页面开关。

## 身份与实例

身份沿用 get_jiuwen_memory_scope，运行 Agent 名称不覆盖配置 agent_id，session_id 原样透传。
按配置指纹和配置 Agent 身份选择最早注册且仍有效的普通 Provider，绑定在有效期间保持稳定。
Team 成员 Provider 不参与管理页的自动候选选择。无匹配者则创建记忆专用 Provider。
SDK 使用现有 document_runtime_holder，页面不分配聊天会话级 instance_id，不指定固定端口。
跨循环调用回到 Provider 初始化时的事件循环；服务只关闭自己创建的 Provider，不关闭借用的会话 Provider。
连接、身份或模型配置改变时丢弃旧页面绑定并释放自有 Provider；页面开关不改变实例绑定。
Swarm 退出时释放自有 Provider；共享 Runtime 的存活由 holder 引用计数决定。

## 开关和配置

`memory.builtin.jiuwen.portal.enabled` 默认 false（缺省配置同样关闭），仅控制 Portal 页面入口和新链接签发。
设置页展示「记忆管理」，通过 `memory_portal_enabled` 字段读写；连接与身份选项保留在配置文件。
默认 provider=jiuwen、连接方式 sdk，模型、文档根和身份使用现有装配规则。
Work/Code 的模式开关、mode/engine 限制及 Team 的 provider 选择保持原有语义。
旧的顶层 `memory.enabled` 不再作为新增总开关消费，不影响记忆读写。

关闭页面不拦截 Rail/Provider 创建、成员登记、预取、回写、工具调用或会话结束抽取。
已创建的 Portal 实例在页面关闭期间保留，重新开启时复用服务并签发新链接，避免反复销毁监听端口。
仅保存页面开关时更新前端可见性和新链接签发许可，不重载 Agent、不回收暂停团队。
页面开关不计入记忆 Rail 或 Portal 连接配置指纹；与模型等配置一起保存时，其他字段仍按原规则生效。
Portal 配置同步异常仅记录异常类型，不阻断 Agent 配置重载；Agent 自身重载失败仍按原接口报告。
HTTP API Key 优先使用非空配置值，缺失或空值回退到 `JIUWEN_MEMORY_API_KEY`；非空值沿用凭据解密流程。
关闭不删除已有记忆；已经签发给浏览器的授权会话仍遵循 Memory 原有有效期，本开关不代替权限撤销。

左侧导航及菜单的「记忆」入口读取同一页面开关，关闭时隐藏并卸载 iframe；
正在页面中时回到聊天，设置页入口保留。刷新从后端恢复；失败保存不发布新的导航状态。
前端丢弃旧请求和过期配置快照，不将授权 URL 保存到配置或浏览器存储。
保存成功后通过同源 storage 事件同步页面开关，使同一浏览器其他已打开页面及时隐藏入口、卸载 iframe；
通知只携带开关与时间戳，同时触发现有配置刷新事件，使已打开的设置表单重新读取服务端状态。
刷新不再次保存配置；无效通知不触发刷新。存储不可用时保持当前页正常生效。

Portal 独立于 Settings 上下文，首次进入或重连调用 `memory.portal.open`。
JiuwenMate 的记忆菜单复用同一 MemoryPage 和页面开关，不回退到设置页面。
SDK 不传 `allowed_frame_origins`，HTTP 不要求 `http.portal_frame_origins`；
嵌入不再需要来源白名单，但仍通过 Memory 签发的链接建立浏览器授权会话。
保留 iframe sandbox 和 no-referrer。HTTP 部署仍由配置文件指定，Swarm 不启动远程服务。

Portal 的模型配置属于 Memory，Swarm 不另存一套。只读状态、应用范围及持久化由 Memory 返回。

## 回归入口

Swarm：tests/unit_tests/agentserver/memory/test_memory_portal.py、tests/integration/test_memory_portal.py、
前端 tests/memoryPortal.test.mjs。Gateway 复用协议登记与 Web handler 测试夹具。
设计取舍与边界见 [特性设计](../../../../features/F_20261125_1245_memory-portal-team-migration.md)。
