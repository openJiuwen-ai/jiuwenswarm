# 记忆 Portal

在「设置 → 记忆」中使用 **记忆管理** 开关，默认关闭，仅控制 Portal 页面入口。Work、Code、Team 的记忆功能沿用各自原有配置。
开启后点击左侧导航或菜单中的「记忆」，进入内嵌 Portal。关闭后入口隐藏，已打开的页面退出；已有记忆保留。

## 默认使用

默认使用 JiuwenMemory SDK，无需另启 Memory HTTP 服务或配置嵌入来源。
配置好 Swarm 对话模型后，记忆沿用现有模型注入机制；默认本地 hashing 向量无需额外 embedding 服务。
有条件时可配置现有 embedding 入口改善检索，修改已建索引的模型/维度需遵循 Memory 的迁移规则。

以下配置显式开启管理页面：

```yaml
memory:
  builtin:
    provider: jiuwen
    jiuwen:
      mode: sdk
      portal:
        enabled: true
      scope_id: __default__
      user_id: __default__
      agent_id: main_agent
      session_id: ""
      space: ""
```

身份、项目/团队隔离及文档目录沿用当前接入规则。SDK 复用已有 document_runtime_holder，
Portal 端口由 Memory 分配；没有聊天会话时也可以打开页面，仅初始化记忆 Provider。

## 开关与页面

- 关闭后隐藏入口并卸载已打开的页面；记忆召回、自动回写和工具调用继续按原配置执行。
- 页面开关保存到 `memory.builtin.jiuwen.portal.enabled`，不修改模式开关、provider、连接和身份。
- 刷新恢复已保存状态，同一浏览器其他标签页同步入口和设置开关状态；保存不重载 Agent、不回收团队。
- 关闭页面保留已创建的 Portal 实例；重新开启时复用服务并申请新授权链接。连接配置变化或 Swarm 退出时释放自有 Provider，不关闭借用的会话 Provider。
- 重新进入或点击「重新连接」会申请新授权链接；链接不持久化。已有授权会话沿用 Memory 的有效期。
- 页面内的记忆管理、权限和模型设置由 Memory 提供。

## 可选 HTTP 部署

配置文件仍支持连接独立服务：

以下配置显式开启管理页面：

```yaml
memory:
  builtin:
    provider: jiuwen
    jiuwen:
      mode: http
      portal:
        enabled: true
      base_url: http://127.0.0.1:8137
      api_key: "替换为服务密钥"
      identity_org: local
      identity_user: developer
```

密钥优先使用非空 `api_key`；未填写或留空时读取环境变量 `JIUWEN_MEMORY_API_KEY`。

Swarm 后端和浏览器都须能访问该服务。HTTP 不创建本地 Memory Runtime，
也不要求配置 `http.portal_frame_origins`。必须由后端获取授权链接，不能用裸 `/portal/` 替代。
