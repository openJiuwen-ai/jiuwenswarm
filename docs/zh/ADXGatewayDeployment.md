# Python Gateway 接入 ADX

在 Gateway 配置中按下面的示例设置 ADX 接入参数，并将 `gateway.agent_client.type` 设置为 `adx_router`。这是整套 Gateway 的后端开关；Jiuwen 原有认证、客户端协议、配置和 Cron 接口继续使用。切回原后端使用原有配置。本适配当前支持 Web、TUI 和 Cron，其他渠道尚未适配。

```yaml
gateway:
  agent_client:
    type: adx_router
  adx:
    ingress_url: https://adx.example:8443
    tenant: appliance
    template: jiuwen
    version: appliance-v1
    token_env: ADX_PLATFORM_API_KEY
    service_port: 18092
    connect_timeout_seconds: 180
    max_connections: 128
    parameters:
      model_api_base: http://model.internal:8080/v1
```

通过进程环境设置 `ADX_PLATFORM_API_KEY`，其所属 ADX 租户必须与 `tenant` 一致。这是平台服务凭据，与 Jiuwen 用户身份不同。Ingress 使用 HTTPS，证书须被系统信任。不要将真实凭据放入配置模板。

文件 HTTP 接口复用 WebChannel 已有的业务鉴权回调（`set_handshake_auth`）。回调必须返回可信的 Jiuwen 业务 `user_id`；未配置回调或没有认证身份时返回 401，请求声明的用户与认证身份不符时返回 403。ADX 平台凭据不能充当业务身份凭据，也不接入 ADX IAM。

## ADX 与 AgentServer 准备

部署侧通过 ADX 的公共 Template API 发布指定版本。Template 使用可从各节点拉取的 AgentServer 镜像，声明 `service: [{protocol: ws, port: 18092}]`，按实际 AgentServer 端口调整。AgentServer 配置使用 `sandbox.enabled=false`，文件和 Shell 工具直接运行在当前 ADX 沙箱中。

持久化挂载由 Template 声明，例如：

```json
{
  "env": {"API_BASE": "${model_api_base}"},
  "mounts": [{
    "type": "bind",
    "host_path": "/mnt/workspaces/${workspace}",
    "target": "/home/agentos",
    "options": ["rw"],
    "create_if_missing": true
  }]
}
```

这是 Template 片段，完整请求仍需提供 name、version、image、isolation_runtime 和 resources。API_BASE 仅是渲染示例，实际环境变量须由业务镜像的启动方式消费。模型和其他启动配置由 Template 与 parameters 注入，Gateway 不再向 Activator 发送私有启动快照。工作区父目录由部署侧管理；跨节点共享由预先挂载的分布式文件系统提供。ADX 内置 Jiuwen 模块无需启用。

## 首次访问与重建

Gateway 将已有可信 Jiuwen `user_id` 映射为稳定 Binding，并自动计算 `workspace`，该参数不能在配置中覆盖。一个用户的并发请求复用同一条后端连接，不同用户使用不同连接和 Binding。

前端全部离线 60 秒后尝试关闭该用户的空闲后端连接；另每 30 秒检查一次，回收已空闲 600 秒且前端离线的连接。连接池满时优先回收满足条件的最旧连接，在线用户和在途请求均受保护；没有可回收连接时返回 `ADX_CAPACITY_EXCEEDED`。这些回收只关闭 WebSocket，不删除 Binding 或沙箱。

Gateway 直接连接 `/agent/ws`，携带平台认证和 `X-ADX-Parameters`。后者是 URL 表单编码的键值对，无 JSON 或 Base64 包装。Gateway 无需配置独立 Activator 地址，也不预调用 prepare、resolve 或 activate。

仅首次缺失 Binding 时，ADX 解析参数、渲染 Template 并原子创建 Binding。已有 Binding 完全忽略后续初始化 Header，配置修改不会覆盖其已保存参数。需要应用新的启动参数时，先走已有删除/重建入口删除 Binding，再重新访问；持久化工作区独立于 Binding 数据保留。并发首次访问以成功持久化的参数为准。

通用参数目前以明文保存于 ADX Binding。不要用该配置传递生产模型密钥；本阶段可使用无需认证的测试模型。后续业务密钥保护由 Jiuwen 侧处理。

## 文件与故障处理

文件、附件和 Cron 使用原 Gateway 入口。适配器按当前用户确定 Binding；文件操作通过公共 Binding 查询获得 Environment，再走 ADX 通用转发。一次文件传输固定目标，重建或目标变化后中止，调用方可重新发起操作。

文件 HTTP 请求的参数校验失败返回 400；ADX 连接或 Binding 查询不可用返回 502，并保留具体错误码。

建目录直接调用 ADX `file.mkdir`，沿用其递归创建父目录的行为。

ADX 模式保留 Gateway 原有周期探活流程，暂由 `adx_router` 返回 mock 成功响应，不访问 Ingress 或 AgentServer，也不创建 Binding、连接或沙箱；该响应不代表后端实际就绪。全局 AgentServer 预热仍不启用，沙箱就绪探针由部署侧在 ADX Template 中按实际 AgentServer 配置。

流式响应沿用 E2A 消息协议。连接断开时，未完成请求返回失败；恢复连接时保留 ADX 认证与初始化 Header。已经发送的业务请求不自动重放，避免重复执行工具或消息。冷启动握手阶段的暂时不可用可在连接期限内重试。

排查时先检查 Template 版本、平台凭据租户、证书信任、模型网络 ACL 和 AgentServer 的 WS 端口，再检查 Gateway、ADX 与沙箱日志。避免在日志中输出认证 Header 或含敏感信息的 parameters。
