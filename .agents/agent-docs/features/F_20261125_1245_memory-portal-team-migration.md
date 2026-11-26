---
id: F_20261125_1245_memory-portal-team-migration
title: 共享文档 Runtime 下的记忆 Portal 接入迁移
type: F
author: Wang Tao
author_email: zzyyww0808@gmail.com
modules: [jiuwenswarm/agents/harness/common/memory, jiuwenswarm/server/runtime, jiuwenswarm/common/config_panel, jiuwenswarm/channels/web/frontend, agent-gateway/agent_gateway/channel_manager/web]
files: [jiuwenswarm/agents/harness/common/memory/portal_registry.py, jiuwenswarm/server/runtime/memory_portal.py, jiuwenswarm/common/config_panel/memory_config.py, jiuwenswarm/channels/web/frontend/src/features/memory/MemoryPage.tsx]
designs: []
design_note: 无独立设计文档；本文记录 Portal 接入、页面开关及共享 Runtime 的设计取舍。
---

# 共享文档 Runtime 下的记忆 Portal 接入

## 功能边界

Swarm 提供「设置 → 记忆 → 记忆管理」页面开关、左侧导航和菜单入口、Portal 授权链接获取及实例绑定。
Memory 提供内嵌页面、记忆业务、权限校验和模型配置管理；Gateway 仅转发 memory.portal.open。
当前接口、配置和生命周期约束见 [S_03_memory-portal](../specs/jiuwenswarm/server/runtime/S_03_memory-portal.md)。

页面开关复用 memory.builtin.jiuwen.portal.enabled，默认关闭，仅控制管理入口和新链接签发。
侧栏与设置菜单共用脑形记忆图标，区别于智能体和知识入口。
Work/Code 的模式开关和 Team 的 Provider 选择保持既有语义；关闭管理页不停止记忆读写，也不删除数据。
不采用统一记忆总开关，避免页面操作改变会话记忆行为或重建运行中的 Agent/团队。

## 实例和身份

- 普通 Work/Code Provider 初始化成功后登记；按宿主配置指纹和配置 Agent 身份选择并保持稳定绑定。
- Team 成员 Provider 不参与页面自动选择，保留团队成员的原有身份和访问边界。
- 无匹配 Provider 时仅创建记忆 Provider，不创建聊天 Agent；通过 document_runtime_holder 共享底层 Runtime。
- SDK 由 Memory 分配端口，不设置聊天会话级 instance_id；HTTP 连接已启动的 Memory 服务。
- 跨循环调用回到 Provider 初始化时的事件循环；Portal 只关闭自己创建的 Provider，不关闭借用实例。

## 页面交互与故障处理

保存页面开关后，同源标签页同步导航状态，并通过配置刷新事件更新已打开的设置表单。
刷新仅从服务端读取，不再次写入配置；过期快照和无效通知不能覆盖已确认状态。
关闭隐藏入口并卸载 iframe，保留已创建的 Portal 实例；重新进入或重连时申请新授权链接。
页面开关不参与连接配置指纹，避免开关页面导致服务重建及本地端口转发短暂不可用。
实例占用的资源保留到连接配置改变或 Swarm 退出；链接不持久化、不写入日志。

打开操作共用 30 秒超时预算，涵盖锁等待、配置切换、Provider 初始化和链接签发。
超时返回 unavailable/timeout，取消后清理必要资源；后续请求可以重试。
不存在可用依赖或链接签发失败时展示不可用状态，不在 Gateway 初始化替代 Runtime。
Portal 配置同步与 Agent 重载隔离，避免管理页实例关闭失败影响普通模型或权限配置生效。
关闭失败的自有 Provider 保留引用供后续清理重试，不通过强制丢弃引用掩盖资源释放失败。
JiuwenMate 菜单复用 MemoryPage；HTTP 空密钥保留环境变量回退，兼容既有部署方式。

## 部署与限制

安装依赖须包含 Memory 的 Portal 授权接口和 Gateway 的 memory.portal.open 转发能力。
Memory 已取消嵌入来源白名单；Swarm 保留 iframe sandbox 和 no-referrer，仍使用 Memory 签发的授权链接。
共享 Runtime 沿用首份启动配置，连接配置变化不保证存量租约立即重建。
Portal 模型配置是否可写及生效范围由 Memory 决定；已有浏览器授权会话沿用 Memory 的有效期，页面开关不代替权限撤销。

## 验证范围

- 后端：配置读写、身份绑定、并发复用、跨循环调用、关闭清理，以及锁等待和配置切换超时后的恢复；HTTP 密钥回退与解密、Portal 异常下两个配置入口的重载结果。
- 前端：开关与导航联动、过期快照处理、跨标签页表单刷新、无效通知过滤、iframe 卸载和重新连接，以及 JiuwenMate 菜单中的 Portal 挂载与关闭。
- 集成：SDK/HTTP 授权链接获取、SDK 反复开关后监听地址复用和新链接授权；关闭页面不影响 Work/Code/Team 原有记忆装配与读写。
- Gateway：协议登记、目标 AgentServer 转发及可信连接身份透传。

回归入口由接口规约统一列出；这些检查不替代各模式完整的真实 LLM 业务验收。
