# 记忆 Portal

在「设置 → 记忆」中使用 **记忆管理** 开关，默认关闭，仅控制 Portal 页面入口。Work、Code、Team 的记忆功能沿用各自原有配置。
开启后点击左侧导航或菜单中的「记忆」，进入内嵌 Portal。关闭后入口隐藏，已打开的页面退出；已有记忆保留。

## 个人版默认连接

安装的 JiuwenMemory 支持 Portal 时，Portal 与记忆插件共用内置 HTTP 服务和存储，即使旧入口的模式仍配置为 `sdk`，也不会另起 SDK 实例。
当前锁定的 JiuwenMemory `0.2.1` 支持通过该服务读写记忆，但不提供 Portal 页面及其模型设置。
不需要另启服务、为 Portal 配置密钥或设置嵌入来源；浏览器需要能访问 Swarm 所在机器的本地监听地址。
内置服务不可用时，Portal 不会回退到另一份 SDK 存储。页面只展示 Memory 允许查看的个人记忆，不绕过项目或团队权限。

以下配置显式开启管理页面：

```yaml
memory:
  builtin:
    provider: jiuwen
    jiuwen:
      portal:
        enabled: true
```

企业版继续使用其已配置的记忆 Provider。

## 开关与页面

- 关闭后隐藏入口并卸载已打开的页面；记忆召回、自动回写和工具调用继续按原配置执行。
- 页面开关保存到 `memory.builtin.jiuwen.portal.enabled`，不修改模式开关、provider、连接和身份。
- 刷新恢复已保存状态，同一浏览器其他标签页同步入口和设置开关状态；保存不重载 Agent、不回收团队。
- 关闭页面不停止插件的内置服务；重新开启时复用服务并申请新授权链接。旧入口在连接配置变化或 Swarm 退出时释放自有 Provider，不关闭借用的会话 Provider。
- 重新进入或点击「重新连接」会申请新授权链接；链接不持久化。已有授权会话沿用 Memory 的有效期。
- 页面内的记忆管理、权限和模型设置由 Memory 提供。

个人版「模型设置」修改的是内置记忆服务支持的 LLM，不会修改 Swarm 对话模型。保存后对后续模型调用生效，重启后保留。
覆盖值保存在 Memory 的本地配置数据库，不回写 Swarm 的 YAML；恢复字段默认值会移除覆盖，重新使用启动配置。

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

密钥优先使用非空 `api_key`；配置值尝试解密，无法解密时保留原值以兼容历史明文配置。
未填写或留空时直接读取环境变量 `JIUWEN_MEMORY_API_KEY` 的明文值。

Swarm 后端和浏览器都须能访问该服务。此 Portal 连接不会改变个人版插件使用的存储。
不要求配置 `http.portal_frame_origins`。必须由后端获取授权链接，不能用裸 `/portal/` 替代。

Portal 授权链接不得复制分享。iframe 的 sandbox 和 no-referrer 不替代服务端抗点击劫持保护；
需要限制嵌入来源时，请在 Memory 服务端或反向代理设置 CSP `frame-ancestors`，仅允许可信 Swarm 来源。

Portal 初始化期间保存开关无需等待初始化；连接配置变化时等待服务锁最多 1 秒。
若锁等待超时，普通配置重载继续执行，下一次打开或重新连接 Portal 时按最新配置重新绑定。
